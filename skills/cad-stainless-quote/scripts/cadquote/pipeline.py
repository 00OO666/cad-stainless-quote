"""End-to-end orchestration for traceable CAD quantity takeoff and quotation."""

from __future__ import annotations

import hashlib
import json
from collections import Counter, defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

from .cad_index import CadIndexBundle, index_dxf, write_index_sqlite
from .calculation import calculate_item
from .converter import ConversionAudit, convert_dwgs
from .exporter import build_quote_workbook
from .ingest import IngestLimits, IngestResult, ingest_input
from .io import write_json_atomic
from .linking import rank_evidence_edges
from .materials import (
    DEFAULT_REVIEW_CODE_FAMILIES,
    DEFAULT_STAINLESS_CODE_FAMILIES,
    annotate_material_conflicts,
    load_docx_material_specs,
    load_material_specs,
    normalize_text,
    parse_cad_material_specs,
)
from .models import (
    CadEntity,
    EvidenceEdge,
    MaterialMention,
    MaterialSpec,
    MeasurementCandidate,
    MtOccurrence,
    ProjectManifest,
    ReviewStatus,
    RunIssue,
    Severity,
    Sheet,
    SourceFile,
    TakeoffItem,
)
from .mt import (
    deduplicate_occurrences,
    detect_material_mentions,
    detect_mt_occurrences,
    link_docx_material_mentions,
)
from .panels import PanelExpansion, choose_analysis_view, expand_viewport_panels
from .pricing import apply_price, load_price_book
from .render import render_regions
from .takeoff import TakeoffBuildResult, build_takeoff
from .vector_probe import probe_repeated_vectors

_MATERIAL_WORKBOOK_TERMS = (
    "材料",
    "物料",
    "选型",
    "material",
    "finish",
    "sample",
)


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _price_context(metadata: Mapping[str, Any]) -> dict[str, Any]:
    value = metadata.get("price")
    return dict(value) if isinstance(value, Mapping) else {}


def _blocked_pricing_items(
    items: Sequence[TakeoffItem],
    reason: str,
) -> list[TakeoffItem]:
    blocked: list[TakeoffItem] = []
    for item in items:
        note = "；".join(value for value in (item.note, reason) if value)
        blocked.append(
            item.model_copy(
                update={
                    "status": ReviewStatus.BLOCK,
                    "unit_price": None,
                    "price_entry_id": None,
                    "amount": None,
                    "note": note,
                }
            )
        )
    return blocked


@dataclass(slots=True)
class PipelineResult:
    status: ReviewStatus
    run_dir: str
    quote_path: str | None = None
    manifest_path: str | None = None
    counts: dict[str, int] = field(default_factory=dict)
    issues: list[RunIssue] = field(default_factory=list)
    paths: dict[str, str] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status.value,
            "run_dir": self.run_dir,
            "quote_path": self.quote_path,
            "manifest_path": self.manifest_path,
            "counts": self.counts,
            "issues": [issue.model_dump(mode="json") for issue in self.issues],
            "paths": self.paths,
        }


@dataclass(slots=True)
class ConfirmationBundle:
    """Normalized reviewer choices plus canonical reviewer audit context."""

    selections: dict[str, dict[str, str | list[str]]] = field(default_factory=dict)
    audit: dict[str, dict[str, Any]] = field(default_factory=dict)
    schema_version: str = "legacy"
    source_path: str | None = None


def _issue(
    stage: str,
    severity: Severity,
    code: str,
    message: str,
    *,
    source_id: str | None = None,
    evidence: Sequence[str] = (),
    action: str | None = None,
) -> RunIssue:
    return RunIssue(
        stage=stage,
        severity=severity,
        code=code,
        message=message,
        source_id=source_id,
        evidence=list(evidence),
        suggested_action=action,
    )


def _is_material_workbook(file: SourceFile) -> bool:
    if file.suffix not in {".xls", ".xlsx", ".xlsm"}:
        return False
    name = file.relative_path.casefold()
    return any(term in name for term in _MATERIAL_WORKBOOK_TERMS)


def _load_materials(
    files: Sequence[SourceFile],
    *,
    stainless_code_families: Sequence[str] | None = None,
    review_code_families: Sequence[str] | None = None,
) -> tuple[list[MaterialSpec], list[RunIssue]]:
    materials: list[MaterialSpec] = []
    issues: list[RunIssue] = []
    workbook_candidates = [file for file in files if _is_material_workbook(file)]
    docx_candidates = [file for file in files if file.suffix == ".docx"]
    for file in workbook_candidates:
        try:
            values = load_material_specs(file.absolute_path, source_file_id=file.id)
        except Exception as exc:
            issues.append(
                _issue(
                    "materials",
                    Severity.WARNING,
                    "MATERIAL_WORKBOOK_READ_FAILED",
                    f"材料表读取失败：{type(exc).__name__}: {exc}",
                    source_id=file.id,
                    evidence=[file.relative_path],
                    action="将旧XLS另存为XLSX，或从CAD/PDF材料表交叉确认",
                )
            )
        else:
            materials.extend(values)
    recovered_docx_count = 0
    for file in docx_candidates:
        try:
            values = load_docx_material_specs(
                file.absolute_path,
                source_file_id=file.id,
                expected_sha256=file.sha256,
                stainless_families=stainless_code_families,
                review_families=review_code_families,
            )
        except Exception as exc:
            issues.append(
                _issue(
                    "materials",
                    Severity.WARNING,
                    "DOCX_MATERIAL_BOOK_READ_FAILED",
                    f"DOCX项目物料书读取失败：{type(exc).__name__}: {exc}",
                    source_id=file.id,
                    evidence=[file.relative_path, f"sha256:{file.sha256}"],
                    action="确认DOCX为有效Office Open XML文件，并人工复核物料编号映射",
                )
            )
        else:
            materials.extend(values)
            recovered_docx_count += len(values)
    if recovered_docx_count:
        issues.append(
            _issue(
                "materials",
                Severity.INFO,
                "DOCX_MATERIAL_SPECS_RECOVERED",
                f"从DOCX项目物料书恢复{recovered_docx_count}条显式材料编号映射；均保持REVIEW。",
                evidence=[
                    material.id
                    for material in materials
                    if material.source_type == "docx_material_book"
                ][:24],
            )
        )
    if not workbook_candidates and not docx_candidates:
        issues.append(
            _issue(
                "materials",
                Severity.WARNING,
                "MATERIAL_WORKBOOK_NOT_FOUND",
                "未找到名称可识别的材料表；MT仍会检测，但材质字段需要复核。",
                action="提供材料选型表，或在CAD材料表中确认MT定义",
            )
        )
    unique = {material.id: material for material in materials}
    return sorted(unique.values(), key=lambda value: (value.mt_code, value.id)), issues


def _block_docx_cross_source_conflicts(
    materials: Sequence[MaterialSpec],
) -> tuple[list[MaterialSpec], list[RunIssue]]:
    """Keep DOCX/CAD or DOCX/workbook conflicts explicit and non-commercial."""

    by_code: dict[str, list[MaterialSpec]] = defaultdict(list)
    for material in materials:
        by_code[material.mt_code].append(material)
    blocked_codes = {
        code
        for code, values in by_code.items()
        if any(value.source_type == "docx_material_book" for value in values)
        and any(value.source_type != "docx_material_book" for value in values)
        and any(value.conflicts for value in values)
    }
    if not blocked_codes:
        return list(materials), []
    updated = [
        material.model_copy(update={"status": ReviewStatus.BLOCK})
        if material.mt_code in blocked_codes
        else material
        for material in materials
    ]
    return updated, [
        _issue(
            "materials",
            Severity.BLOCK,
            "DOCX_MATERIAL_DEFINITION_CONFLICT",
            f"DOCX物料书与其他材料证据对{code}的定义冲突；未覆盖任一来源。",
            evidence=[value.id for value in by_code[code]],
            action="人工核对DOCX物料书、CAD材料表和材料清单后确认唯一版本",
        )
        for code in sorted(blocked_codes)
    ]


def _selected_candidates(value: Any, *, label: str) -> dict[str, str | list[str]]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{label} must be an object mapping roles to candidate IDs")
    result: dict[str, str | list[str]] = {}
    for role, candidate in value.items():
        normalized_role = str(role)
        if normalized_role == "merge_component_ids" and isinstance(candidate, list):
            normalized_ids: list[str] = []
            seen_ids: set[str] = set()
            for index, component_id in enumerate(candidate):
                if not isinstance(component_id, str) or not component_id.strip():
                    raise ValueError(f"{label}[{role!r}][{index}] must be a non-empty component ID")
                normalized_id = component_id.strip()
                if normalized_id not in seen_ids:
                    normalized_ids.append(normalized_id)
                    seen_ids.add(normalized_id)
            if not normalized_ids:
                raise ValueError(f"{label}[{role!r}] must not be an empty array")
            result[normalized_role] = normalized_ids
            continue
        if not isinstance(candidate, str) or not candidate.strip():
            raise ValueError(f"{label}[{role!r}] must be a non-empty candidate ID")
        result[normalized_role] = candidate.strip()
    return result


def _reviewed_at(value: Any, *, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} must be a non-empty ISO 8601 timestamp with timezone")
    candidate = value.strip()
    try:
        parsed = datetime.fromisoformat(candidate)
    except ValueError as exc:
        raise ValueError(f"{label} must be a valid ISO 8601 timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{label} must include a timezone offset")
    return parsed.isoformat()


def _confirmation_audit_record(
    record: Mapping[str, Any],
    selected: Mapping[str, str | list[str]],
    *,
    label: str,
) -> dict[str, Any]:
    """Canonicalize and validate audit metadata for an effective selection."""

    audit = {str(key): value for key, value in record.items()}
    audit["selected"] = dict(selected)

    reviewed_at = audit.get("reviewed_at")
    if (reviewed_at is None or reviewed_at == "") and "timestamp" in audit:
        reviewed_at = audit["timestamp"]
    audit.pop("timestamp", None)

    if not selected:
        if reviewed_at is not None:
            audit["reviewed_at"] = reviewed_at
        return audit

    reviewer = audit.get("reviewer")
    if not isinstance(reviewer, str) or not reviewer.strip():
        raise ValueError(f"{label}.reviewer must be a non-empty string")
    reason = audit.get("reason")
    if not isinstance(reason, str) or not reason.strip():
        raise ValueError(f"{label}.reason must be a non-empty string")
    audit["reviewer"] = reviewer.strip()
    audit["reason"] = reason.strip()
    audit["reviewed_at"] = _reviewed_at(reviewed_at, label=f"{label}.reviewed_at")
    return audit


def _load_confirmations(value: Path | str | None) -> ConfirmationBundle:
    """Accept the legacy mapping and the audit-preserving review schema.

    Legacy format::

        {"component:...": {"length": "measurement:..."}}

    Review format::

        {"schema_version": "1.0", "components": {
          "component:...": {"selected": {...}, "reviewer": "...", ...}
        }}
    """

    if value is None:
        return ConfirmationBundle()
    path = Path(value).expanduser().resolve()
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("confirmations JSON must be an object")

    if "components" in payload:
        component_payload = payload["components"]
        if not isinstance(component_payload, Mapping):
            raise ValueError("confirmations.components must be an object")
        schema_version = str(payload.get("schema_version") or "1.0")
        selections: dict[str, dict[str, str | list[str]]] = {}
        audit: dict[str, dict[str, Any]] = {}
        for component_id, record in component_payload.items():
            if not isinstance(record, Mapping):
                raise ValueError(f"confirmations.components[{component_id!r}] must be an object")
            selected = _selected_candidates(
                record.get("selected", {}),
                label=f"confirmations.components[{component_id!r}].selected",
            )
            normalized_id = str(component_id)
            selections[normalized_id] = selected
            audit[normalized_id] = _confirmation_audit_record(
                record,
                selected,
                label=f"confirmations.components[{component_id!r}]",
            )
        return ConfirmationBundle(
            selections=selections,
            audit=audit,
            schema_version=schema_version,
            source_path=str(path),
        )

    selections = {}
    audit = {}
    for component_id, choices in payload.items():
        normalized_id = str(component_id)
        selected = _selected_candidates(
            choices,
            label=f"confirmations[{component_id!r}]",
        )
        selections[normalized_id] = selected
        audit[normalized_id] = {"selected": selected, "format": "legacy"}
    return ConfirmationBundle(
        selections=selections,
        audit=audit,
        source_path=str(path),
    )


def load_confirmation_bundle(value: Path | str | None) -> ConfirmationBundle:
    """Public parser shared by full, resume and partial takeoff commands."""

    return _load_confirmations(value)


def _load_manifest_confirmations(path: Path) -> ConfirmationBundle:
    if not path.is_file():
        return ConfirmationBundle()
    payload = json.loads(path.read_text(encoding="utf-8"))
    metadata = payload.get("metadata", {}) if isinstance(payload, Mapping) else {}
    audit_payload = metadata.get("confirmation_audit", {})
    if not isinstance(audit_payload, Mapping):
        raise ValueError("manifest metadata.confirmation_audit must be an object")
    schema_version = str(metadata.get("confirmation_schema_version") or "1.0")
    selections: dict[str, dict[str, str | list[str]]] = {}
    audit: dict[str, dict[str, Any]] = {}
    for component_id, record in audit_payload.items():
        if not isinstance(record, Mapping):
            raise ValueError(f"manifest confirmation audit {component_id!r} must be an object")
        selected = _selected_candidates(
            record.get("selected", {}),
            label=f"manifest confirmation audit {component_id!r}.selected",
        )
        selections[str(component_id)] = selected
        if schema_version == "legacy" or record.get("format") == "legacy":
            audit[str(component_id)] = {str(key): value for key, value in record.items()}
            audit[str(component_id)]["selected"] = selected
        else:
            audit[str(component_id)] = _confirmation_audit_record(
                record,
                selected,
                label=f"manifest confirmation audit {component_id!r}",
            )
    return ConfirmationBundle(
        selections=selections,
        audit=audit,
        schema_version=schema_version,
        source_path=(
            str(metadata["confirmation_source"]) if metadata.get("confirmation_source") else None
        ),
    )


def _load_index_snapshot(path: Path) -> tuple[list[Sheet], list[CadEntity], list[dict[str, Any]]]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    sources = payload.get("sources") if isinstance(payload, Mapping) else None
    if not isinstance(sources, list):
        raise ValueError(f"CAD index has no sources array: {path}")
    sheets = [
        Sheet.model_validate(sheet)
        for source in sources
        if isinstance(source, Mapping)
        for sheet in source.get("sheets", [])
    ]
    entities = [
        CadEntity.model_validate(entity)
        for source in sources
        if isinstance(source, Mapping)
        for entity in source.get("entities", [])
    ]
    return sheets, entities, sources


def _snapshot_index_issues(sources: Sequence[Mapping[str, Any]]) -> list[RunIssue]:
    issues: list[RunIssue] = []
    for source in sources:
        source_id = str(source.get("source_file_id") or "") or None
        source_path = str(source.get("source_path") or "")
        audit_errors = int(source.get("audit_error_count", 0) or 0)
        audit_fixes = int(source.get("audit_fix_count", 0) or 0)
        if audit_errors:
            issues.append(
                _issue(
                    "index",
                    Severity.ERROR,
                    "DXF_AUDIT_ERRORS",
                    f"DXF审计发现{audit_errors}个错误，不能自动升为PASS。",
                    source_id=source_id,
                    evidence=[source_path],
                    action="在CAD中修复源图后重新转换和索引",
                )
            )
        if bool(source.get("recovered")):
            issues.append(
                _issue(
                    "index",
                    Severity.ERROR,
                    "DXF_RECOVERED_INPUT",
                    "DXF只能通过恢复模式读取；几何或语义可能不完整。",
                    source_id=source_id,
                    evidence=[source_path],
                    action="回原DWG执行AUDIT/RECOVER并重新导出",
                )
            )
        if audit_fixes:
            issues.append(
                _issue(
                    "index",
                    Severity.WARNING,
                    "DXF_AUDIT_FIXES_APPLIED",
                    f"DXF读取器应用了{audit_fixes}个审计修复。",
                    source_id=source_id,
                    evidence=[source_path],
                    action="对受修复图纸的关键尺寸和索引关系人工复核",
                )
            )
    return issues


def _load_panel_snapshot(path: Path) -> PanelExpansion:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, Mapping):
        raise ValueError(f"panel snapshot must be an object: {path}")
    return PanelExpansion(
        sheets=[Sheet.model_validate(value) for value in payload.get("sheets", [])],
        entities=[CadEntity.model_validate(value) for value in payload.get("entities", [])],
        source_panel_counts={
            str(key): int(value)
            for key, value in dict(payload.get("source_panel_counts", {})).items()
        },
        warnings=[str(value) for value in payload.get("warnings", [])],
    )


def _source_ref(source: SourceFile | None) -> dict[str, Any] | None:
    if source is None:
        return None
    return {
        "source_file_id": source.id,
        "relative_path": source.relative_path,
        "sha256": source.sha256,
    }


def _sheet_ref(
    sheet: Sheet | None,
    sources: Mapping[str, SourceFile] | None = None,
) -> dict[str, Any] | None:
    if sheet is None:
        return None
    return {
        "sheet_id": sheet.id,
        "source_file_id": sheet.source_file_id,
        "source_file": _source_ref((sources or {}).get(sheet.source_file_id)),
        "drawing_number": sheet.drawing_number,
        "title": sheet.title,
        "layout": sheet.layout,
        "kind": sheet.kind,
        "bbox": list(sheet.bbox) if sheet.bbox is not None else None,
    }


def _entity_ref(
    entity: CadEntity,
) -> dict[str, Any]:
    return {
        "entity_id": entity.id,
        "source_file_id": entity.source_file_id,
        "source_file_catalog_ref": f"evidence_catalog.source_files.{entity.source_file_id}",
        "sheet_id": entity.sheet_id,
        "sheet_catalog_ref": (
            f"evidence_catalog.sheets.{entity.sheet_id}" if entity.sheet_id else None
        ),
        "handle": entity.handle,
        "layer": entity.layer,
        "space": entity.space,
        "bbox": list(entity.bbox) if entity.bbox is not None else None,
        "text": entity.text,
        "entity_type": entity.entity_type,
    }


def _build_review_pack(
    takeoff: TakeoffBuildResult,
    priced_items: Sequence[TakeoffItem],
    sheets: Sequence[Sheet],
    entities: Sequence[CadEntity],
    occurrences: Sequence[MtOccurrence],
    relation_edges: Sequence[EvidenceEdge],
    confirmations: ConfirmationBundle,
    *,
    source_files: Sequence[SourceFile] = (),
    materials: Sequence[MaterialSpec] = (),
    material_mentions: Sequence[MaterialMention] = (),
    material_mention_edges: Sequence[EvidenceEdge] = (),
    vector_probe_payload: Mapping[str, Any] | None = None,
    issues: Sequence[RunIssue] = (),
    metadata: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Group every selectable candidate and its source around a component."""

    sheet_by_id = {sheet.id: sheet for sheet in sheets}
    entity_by_id = {entity.id: entity for entity in entities}
    source_by_id = {source.id: source for source in source_files}
    occurrence_by_id = {occurrence.id: occurrence for occurrence in occurrences}
    material_by_id = {material.id: material for material in materials}
    mention_by_id = {mention.id: mention for mention in material_mentions}
    vector_probes = [
        value
        for value in (vector_probe_payload or {}).get("probes", [])
        if isinstance(value, Mapping)
    ]
    vector_probes_by_occurrence: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for probe in vector_probes:
        occurrence_id = probe.get("occurrence_id")
        if isinstance(occurrence_id, str):
            vector_probes_by_occurrence[occurrence_id].append(probe)
    item_by_component = {
        item.component_id: item for item in priced_items if item.component_id is not None
    }
    measurements_by_component: dict[str, list[MeasurementCandidate]] = defaultdict(list)
    for candidate in takeoff.measurements:
        measurements_by_component[candidate.component_id].append(candidate)
    dimension_edges_by_component: dict[str, list[EvidenceEdge]] = defaultdict(list)
    occurrence_edges_by_component: dict[str, list[EvidenceEdge]] = defaultdict(list)
    price_edges_by_component: dict[str, list[EvidenceEdge]] = defaultdict(list)
    for edge in takeoff.evidence_edges:
        if edge.relation == "component_to_dimension":
            dimension_edges_by_component[edge.source_id].append(edge)
        elif edge.relation == "occurrence_to_component":
            occurrence_edges_by_component[edge.target_id].append(edge)
        elif edge.relation == "component_to_price":
            price_edges_by_component[edge.source_id].append(edge)

    assigned_relation_ids: set[str] = set()
    catalog_entity_ids: set[str] = set()
    catalog_sheet_ids = {
        identifier
        for edge in relation_edges
        for identifier in (edge.source_id, edge.target_id)
        if identifier in sheet_by_id
    }
    catalog_source_ids: set[str] = set()
    groups: list[dict[str, Any]] = []
    for component in takeoff.components:
        component_occurrences = [
            occurrence_by_id[occurrence_id]
            for occurrence_id in [
                *component.plan_occurrence_ids,
                *component.elevation_occurrence_ids,
            ]
            if occurrence_id in occurrence_by_id
        ]
        component_vector_probes = [
            probe
            for occurrence in component_occurrences
            for probe in vector_probes_by_occurrence.get(occurrence.id, ())
        ]
        relevant_sheet_ids = {
            occurrence.sheet_id
            for occurrence in component_occurrences
            if occurrence.sheet_id is not None
        } | set(component.detail_sheet_ids)
        catalog_sheet_ids.update(relevant_sheet_ids)
        plan_sheet_ids = {
            occurrence_by_id[occurrence_id].sheet_id
            for occurrence_id in component.plan_occurrence_ids
            if occurrence_id in occurrence_by_id
            and occurrence_by_id[occurrence_id].sheet_id is not None
        }
        elevation_sheet_ids = {
            occurrence_by_id[occurrence_id].sheet_id
            for occurrence_id in component.elevation_occurrence_ids
            if occurrence_id in occurrence_by_id
            and occurrence_by_id[occurrence_id].sheet_id is not None
        }
        plan_candidates = [
            edge
            for edge in relation_edges
            if edge.relation == "plan_to_elevation"
            and (edge.source_id in plan_sheet_ids or edge.target_id in elevation_sheet_ids)
        ]
        candidate_elevation_sheet_ids = elevation_sheet_ids | {
            edge.target_id for edge in plan_candidates
        }
        detail_candidates = [
            edge
            for edge in relation_edges
            if edge.relation == "elevation_to_detail"
            and edge.source_id in candidate_elevation_sheet_ids
        ]
        edge_pool = [*plan_candidates, *detail_candidates]
        edge_counts = Counter(edge.relation for edge in edge_pool)
        candidate_edges: list[EvidenceEdge] = []
        relation_candidate_truncation: dict[str, dict[str, int]] = {}
        for relation in sorted(edge_counts):
            ranked = sorted(
                (edge for edge in edge_pool if edge.relation == relation),
                key=lambda edge: (-edge.confidence, edge.id),
            )
            kept = ranked[:24]
            candidate_edges.extend(kept)
            if len(ranked) > len(kept):
                relation_candidate_truncation[relation] = {
                    "total": len(ranked),
                    "kept": len(kept),
                }
        assigned_relation_ids.update(edge.id for edge in candidate_edges)
        edge_records = []
        for edge in candidate_edges:
            edge_record = edge.model_dump(mode="json")
            edge_record["source_ref"] = _sheet_ref(
                sheet_by_id.get(edge.source_id),
                source_by_id,
            )
            edge_record["target_ref"] = _sheet_ref(
                sheet_by_id.get(edge.target_id),
                source_by_id,
            )
            edge_records.append(edge_record)

        measurements = sorted(
            measurements_by_component.get(component.id, []),
            key=lambda value: (value.role, -value.confidence, value.id),
        )
        source_file_ids = {occurrence.source_file_id for occurrence in component_occurrences} | {
            candidate.source_file_id for candidate in measurements
        }
        referenced_entity_ids = {
            entity_id
            for occurrence in component_occurrences
            for entity_id in [*occurrence.entity_ids, occurrence.leader_entity_id]
            if entity_id is not None
        } | {entity_id for candidate in measurements for entity_id in candidate.entity_ids}
        catalog_entity_ids.update(referenced_entity_ids)
        catalog_source_ids.update(source_file_ids)
        measurement_records = []
        for candidate in measurements:
            record = candidate.model_dump(mode="json")
            source = source_by_id.get(candidate.source_file_id)
            record["source_ref"] = {
                "source_file_id": candidate.source_file_id,
                "relative_path": source.relative_path if source else None,
                "source_file_catalog_ref": (
                    f"evidence_catalog.source_files.{candidate.source_file_id}"
                ),
                "sheet_id": candidate.sheet_id,
                "sheet_catalog_ref": (
                    f"evidence_catalog.sheets.{candidate.sheet_id}" if candidate.sheet_id else None
                ),
                "entity_ids": candidate.entity_ids,
                "entity_catalog_refs": [
                    f"evidence_catalog.entities.{entity_id}" for entity_id in candidate.entity_ids
                ],
            }
            measurement_records.append(record)
        measurement_edge_records = []
        for edge in sorted(
            dimension_edges_by_component.get(component.id, []),
            key=lambda value: value.id,
        ):
            record = edge.model_dump(mode="json")
            record["source_ref"] = {
                "component_id": component.id,
                "mt_code": component.mt_code,
                "room": component.room,
            }
            record["target_ref"] = {
                "measurement_candidate_id": edge.target_id,
                "component_id": component.id,
            }
            measurement_edge_records.append(record)
        occurrence_edge_records = []
        for edge in sorted(
            occurrence_edges_by_component.get(component.id, []),
            key=lambda value: value.id,
        ):
            record = edge.model_dump(mode="json")
            occurrence = occurrence_by_id.get(edge.source_id)
            record["source_ref"] = (
                {
                    **occurrence.model_dump(mode="json"),
                    "source_file": _source_ref(source_by_id.get(occurrence.source_file_id)),
                    "sheet": _sheet_ref(
                        sheet_by_id.get(occurrence.sheet_id or ""),
                        source_by_id,
                    ),
                }
                if occurrence is not None
                else None
            )
            record["target_ref"] = {
                "component_id": component.id,
                "mt_code": component.mt_code,
            }
            occurrence_edge_records.append(record)
        price_edge_records = []
        for edge in sorted(
            price_edges_by_component.get(component.id, []),
            key=lambda value: value.id,
        ):
            record = edge.model_dump(mode="json")
            record["source_ref"] = {
                "component_id": component.id,
                "mt_code": component.mt_code,
            }
            record["target_ref"] = {
                "price_entry_id": edge.target_id,
                "unit_price": (
                    item_by_component[component.id].unit_price
                    if component.id in item_by_component
                    else None
                ),
                "price_book": (metadata or {}).get("price_book"),
                "price_book_version": (metadata or {}).get("price_book_version"),
            }
            price_edge_records.append(record)
        groups.append(
            {
                "component": component.model_dump(mode="json"),
                "current_item": (
                    item_by_component[component.id].model_dump(mode="json")
                    if component.id in item_by_component
                    else None
                ),
                "confirmation": confirmations.audit.get(component.id),
                "sources": {
                    "source_file_ids": sorted(source_file_ids),
                    "source_files": [
                        _source_ref(source_by_id.get(source_id))
                        for source_id in sorted(source_file_ids)
                        if source_id in source_by_id
                    ],
                    "sheet_refs": [
                        ref
                        for ref in (
                            _sheet_ref(sheet_by_id.get(sheet_id), source_by_id)
                            for sheet_id in sorted(relevant_sheet_ids)
                        )
                        if ref is not None
                    ],
                    "occurrences": [
                        occurrence.model_dump(mode="json")
                        for occurrence in sorted(
                            component_occurrences,
                            key=lambda value: value.id,
                        )
                    ],
                    "entity_ids": sorted(referenced_entity_ids),
                },
                "measurement_candidates": measurement_records,
                "vector_quantity_probes": component_vector_probes,
                "measurement_edges": measurement_edge_records,
                "occurrence_edges": occurrence_edge_records,
                "price_edges": price_edge_records,
                "relation_edge_candidates": [
                    value for value in sorted(edge_records, key=lambda value: value["id"])
                ],
                "relation_candidate_truncation": relation_candidate_truncation,
            }
        )

    for sheet_id in catalog_sheet_ids:
        sheet = sheet_by_id.get(sheet_id)
        if sheet is not None:
            catalog_source_ids.add(sheet.source_file_id)
    for entity_id in catalog_entity_ids:
        entity = entity_by_id.get(entity_id)
        if entity is not None:
            catalog_source_ids.add(entity.source_file_id)
            if entity.sheet_id:
                catalog_sheet_ids.add(entity.sheet_id)
    for edge in material_mention_edges:
        mention = mention_by_id.get(edge.source_id)
        material = material_by_id.get(edge.target_id)
        if mention is not None:
            catalog_source_ids.add(mention.source_file_id)
            catalog_entity_ids.update(mention.entity_ids)
            if mention.sheet_id:
                catalog_sheet_ids.add(mention.sheet_id)
        if material is not None and material.source_file_id:
            catalog_source_ids.add(material.source_file_id)

    return {
        "schema_version": "1.0",
        "generated_at": datetime.now(UTC).isoformat(),
        "metadata": dict(metadata or {}),
        "confirmation_source": confirmations.source_path,
        "confirmation_schema_version": confirmations.schema_version,
        "confirmation_audit": confirmations.audit,
        "issues": [issue.model_dump(mode="json") for issue in issues],
        "confirmation_template": {
            "schema_version": "1.0",
            "optional_selection_examples": {
                "merge_component_ids": ["component:SOURCE_COMPONENT_ID_FROM_THIS_REVIEW_PACK"],
                "merge_component_ids_comma_separated": (
                    "component:SOURCE_ID_1,component:SOURCE_ID_2"
                ),
            },
            "components": {
                component.id: {
                    "selected": {},
                    "reviewer": "",
                    "reviewed_at": "",
                    "reason": "",
                }
                for component in takeoff.components
            },
        },
        "summary": {
            "component_count": len(takeoff.components),
            "measurement_candidate_count": len(takeoff.measurements),
            "relation_edge_candidate_count": len(relation_edges),
            "price_edge_count": sum(
                edge.relation == "component_to_price" for edge in takeoff.evidence_edges
            ),
            "confirmed_component_count": sum(
                bool(value) for value in confirmations.selections.values()
            ),
            "docx_material_count": sum(
                value.source_type == "docx_material_book" for value in materials
            ),
            "material_mention_count": len(material_mentions),
            "material_mention_match_candidate_count": len(material_mention_edges),
            "material_mention_unique_match_count": sum(
                edge.status == ReviewStatus.REVIEW for edge in material_mention_edges
            ),
            "material_mention_blocked_candidate_count": sum(
                edge.status == ReviewStatus.BLOCK for edge in material_mention_edges
            ),
            "vector_quantity_review_candidate_count": sum(
                probe.get("recommended_quantity") is not None for probe in vector_probes
            ),
        },
        "components": groups,
        "material_evidence": {
            "docx_materials": [
                material.model_dump(mode="json")
                for material in sorted(materials, key=lambda value: value.id)
                if material.source_type == "docx_material_book"
            ],
            "unnumbered_mentions": [
                mention.model_dump(mode="json")
                for mention in sorted(material_mentions, key=lambda value: value.id)
            ],
            "mention_to_material_candidates": [
                {
                    **edge.model_dump(mode="json"),
                    "source_ref": (
                        mention_by_id[edge.source_id].model_dump(mode="json")
                        if edge.source_id in mention_by_id
                        else None
                    ),
                    "target_ref": (
                        material_by_id[edge.target_id].model_dump(mode="json")
                        if edge.target_id in material_by_id
                        else None
                    ),
                }
                for edge in sorted(material_mention_edges, key=lambda value: value.id)
            ],
        },
        "vector_quantity_evidence": dict(vector_probe_payload or {}),
        "evidence_catalog": {
            "source_files": {
                source_id: _source_ref(source_by_id[source_id])
                for source_id in sorted(catalog_source_ids)
                if source_id in source_by_id
            },
            "sheets": {
                sheet_id: _sheet_ref(sheet_by_id[sheet_id], source_by_id)
                for sheet_id in sorted(catalog_sheet_ids)
                if sheet_id in sheet_by_id
            },
            "entities": {
                entity_id: _entity_ref(entity_by_id[entity_id])
                for entity_id in sorted(catalog_entity_ids)
                if entity_id in entity_by_id
            },
        },
        "unassigned_relation_edges": [
            {
                **edge.model_dump(mode="json"),
                "source_ref": _sheet_ref(sheet_by_id.get(edge.source_id), source_by_id),
                "target_ref": _sheet_ref(sheet_by_id.get(edge.target_id), source_by_id),
            }
            for edge in relation_edges
            if edge.id not in assigned_relation_ids
        ],
    }


def _overall_status(
    items: Sequence[TakeoffItem],
    issues: Sequence[RunIssue] = (),
) -> ReviewStatus:
    if not items or all(item.status == ReviewStatus.BLOCK for item in items):
        return ReviewStatus.BLOCK
    severe_issue = any(issue.severity in {Severity.ERROR, Severity.BLOCK} for issue in issues)
    if not severe_issue and all(
        item.status == ReviewStatus.PASS and item.amount is not None for item in items
    ):
        return ReviewStatus.PASS
    return ReviewStatus.REVIEW


def _confirmation_issue(bundle: ConfirmationBundle) -> RunIssue | None:
    if bundle.source_path is None:
        return None
    selected = [component_id for component_id, choices in bundle.selections.items() if choices]
    if bundle.schema_version == "legacy":
        return _issue(
            "takeoff",
            Severity.BLOCK,
            "LEGACY_CONFIRMATIONS_UNAUDITED",
            "已读取旧版扁平确认，但其中没有完整审核人、审核时间和理由；本次结果不能成为商业PASS。",
            evidence=[bundle.source_path, *selected[:24]],
            action="改用components结构，并为每个非空selected填写reviewer、reviewed_at和reason",
        )
    return _issue(
        "takeoff",
        Severity.INFO,
        "CONFIRMATIONS_APPLIED",
        f"已载入{len(selected)}个构件的人工选择；审核人、时间和理由保留在review-pack.json。",
        evidence=[bundle.source_path, *selected[:24]],
    )


def _index_inputs(
    dxf_paths: Sequence[Path],
    source_ids: Mapping[str, str],
    index_dir: Path,
) -> tuple[CadIndexBundle, list[RunIssue]]:
    results = []
    issues: list[RunIssue] = []
    for path in sorted(dxf_paths, key=lambda value: str(value).casefold()):
        source_id = source_ids.get(str(path.resolve()))
        try:
            results.append(index_dxf(path, source_file_id=source_id))
        except Exception as exc:
            issues.append(
                _issue(
                    "index",
                    Severity.ERROR,
                    "DXF_INDEX_FAILED",
                    f"DXF索引失败：{type(exc).__name__}: {exc}",
                    source_id=source_id,
                    evidence=[str(path)],
                    action="检查转换日志并用CAD修复原图",
                )
            )
    bundle = CadIndexBundle(results)
    for indexed in results:
        if indexed.audit_error_count > 0:
            issues.append(
                _issue(
                    "index",
                    Severity.ERROR,
                    "DXF_AUDIT_ERRORS",
                    f"DXF审计发现{indexed.audit_error_count}个错误，不能自动升为PASS。",
                    source_id=indexed.source_file_id,
                    evidence=[indexed.source_path],
                    action="在CAD中修复源图后重新转换和索引",
                )
            )
        if indexed.recovered:
            issues.append(
                _issue(
                    "index",
                    Severity.ERROR,
                    "DXF_RECOVERED_INPUT",
                    "DXF只能通过恢复模式读取；几何或语义可能不完整。",
                    source_id=indexed.source_file_id,
                    evidence=[indexed.source_path],
                    action="回原DWG执行AUDIT/RECOVER并重新导出",
                )
            )
        if indexed.audit_fix_count > 0:
            issues.append(
                _issue(
                    "index",
                    Severity.WARNING,
                    "DXF_AUDIT_FIXES_APPLIED",
                    f"DXF读取器应用了{indexed.audit_fix_count}个审计修复。",
                    source_id=indexed.source_file_id,
                    evidence=[indexed.source_path],
                    action="对受修复图纸的关键尺寸和索引关系人工复核",
                )
            )
        if indexed.warnings:
            issues.append(
                _issue(
                    "index",
                    Severity.WARNING,
                    "CAD_SEMANTIC_EXPANSION_WARNINGS",
                    f"CAD语义展开产生{len(indexed.warnings)}条告警；部分代理对象或块内容不可读取。",
                    source_id=indexed.source_file_id,
                    evidence=indexed.warnings[:10],
                    action="对受影响构件回原DWG或证据图复核，不得自动升为PASS",
                )
            )
        if indexed.block_expansion_truncated:
            issues.append(
                _issue(
                    "index",
                    Severity.ERROR,
                    "BLOCK_EXPANSION_TRUNCATED",
                    "块内语义实体达到安全上限，索引不完整。",
                    source_id=indexed.source_file_id,
                    action="提高受控上限后单独重跑，或拆分/清理原图",
                )
            )
    write_index_sqlite(bundle, index_dir / "cad_index.sqlite")
    write_json_atomic(index_dir / "cad_index.json", bundle.to_dict())
    return bundle, issues


def _price_items(
    items: Sequence[TakeoffItem],
    materials: Sequence[MaterialSpec],
    price_book_path: Path | str | None,
    *,
    quote_date: date | str | None = None,
    currency: str = "CNY",
    tax_included: bool | None = None,
) -> tuple[list[TakeoffItem], list[RunIssue], dict[str, Any]]:
    effective_quote_date = quote_date or date.today().isoformat()
    pricing_context = {
        "quote_date": str(effective_quote_date)[:10],
        "currency": currency,
        "tax_included": tax_included,
    }
    material_groups: dict[str, list[MaterialSpec]] = defaultdict(list)
    for material in materials:
        material_groups[material.mt_code].append(material)
    if price_book_path is None:
        updated = []
        for item in items:
            note = "；".join(value for value in (item.note, "未提供经批准价格库") if value)
            updated.append(
                item.model_copy(
                    update={
                        "status": (
                            ReviewStatus.REVIEW if item.status == ReviewStatus.PASS else item.status
                        ),
                        "note": note,
                    }
                )
            )
        return (
            updated,
            [
                _issue(
                    "pricing",
                    Severity.WARNING,
                    "PRICE_BOOK_NOT_PROVIDED",
                    "未提供经批准的价格库；本次输出是算量草稿，不是完整商业报价。",
                    action="提供带版本、批准状态和计价口径的价格库",
                )
            ],
            {
                "price_book": None,
                "price_book_sha256": None,
                "price_book_version": None,
                "price_book_approved": None,
                "price_book_integrity": "NOT_PROVIDED",
                **pricing_context,
            },
        )
    resolved_price_book = Path(price_book_path).expanduser().resolve()
    try:
        price_book_sha256 = _sha256_file(resolved_price_book)
    except Exception as exc:
        return (
            _blocked_pricing_items(items, "价格库文件不可读取，商业报价已阻断"),
            [
                _issue(
                    "pricing",
                    Severity.BLOCK,
                    "PRICE_BOOK_READ_FAILED",
                    f"价格库读取失败：{type(exc).__name__}: {exc}",
                    evidence=[str(resolved_price_book)],
                )
            ],
            {
                "price_book": str(resolved_price_book),
                "price_book_sha256": None,
                "price_book_version": None,
                "price_book_approved": None,
                "price_book_integrity": "UNREADABLE",
                **pricing_context,
            },
        )
    try:
        book = load_price_book(resolved_price_book)
    except Exception as exc:
        return (
            _blocked_pricing_items(items, "价格库内容无效，商业报价已阻断"),
            [
                _issue(
                    "pricing",
                    Severity.BLOCK,
                    "PRICE_BOOK_READ_FAILED",
                    f"价格库读取失败：{type(exc).__name__}: {exc}",
                    evidence=[str(resolved_price_book)],
                )
            ],
            {
                "price_book": str(resolved_price_book),
                "price_book_sha256": price_book_sha256,
                "price_book_version": None,
                "price_book_approved": None,
                "price_book_integrity": "INVALID",
                **pricing_context,
            },
        )
    try:
        observed_after_load = _sha256_file(resolved_price_book)
    except Exception as exc:
        return (
            _blocked_pricing_items(items, "价格库读取过程中失效，商业报价已阻断"),
            [
                _issue(
                    "pricing",
                    Severity.BLOCK,
                    "PRICE_BOOK_CHANGED_DURING_READ",
                    f"价格库读取后无法再次校验：{type(exc).__name__}: {exc}",
                    evidence=[str(resolved_price_book), f"before:{price_book_sha256}"],
                )
            ],
            {
                "price_book": str(resolved_price_book),
                "price_book_sha256": price_book_sha256,
                "observed_price_book_sha256": None,
                "price_book_version": book.version,
                "price_book_approved": book.approved,
                "price_book_integrity": "CHANGED_DURING_READ",
                **pricing_context,
            },
        )
    if observed_after_load != price_book_sha256:
        return (
            _blocked_pricing_items(items, "价格库读取过程中发生变化，商业报价已阻断"),
            [
                _issue(
                    "pricing",
                    Severity.BLOCK,
                    "PRICE_BOOK_CHANGED_DURING_READ",
                    "价格库在读取过程中发生变化，无法确认本次报价使用的确切版本。",
                    evidence=[
                        str(resolved_price_book),
                        f"before:{price_book_sha256}",
                        f"after:{observed_after_load}",
                    ],
                )
            ],
            {
                "price_book": str(resolved_price_book),
                "price_book_sha256": price_book_sha256,
                "observed_price_book_sha256": observed_after_load,
                "price_book_version": book.version,
                "price_book_approved": book.approved,
                "price_book_integrity": "CHANGED_DURING_READ",
                **pricing_context,
            },
        )
    updated: list[TakeoffItem] = []
    issues: list[RunIssue] = []
    for item in items:
        candidates = material_groups.get(item.mt_code, [])
        material_keys = {
            (
                normalize_text(candidate.name).casefold(),
                normalize_text(candidate.grade).casefold(),
                (
                    round(float(candidate.thickness_mm), 6)
                    if candidate.thickness_mm is not None
                    else None
                ),
                normalize_text(candidate.finish).casefold(),
                normalize_text(candidate.process).casefold(),
            )
            for candidate in candidates
        }
        material = (
            sorted(candidates, key=lambda candidate: candidate.id)[0]
            if candidates
            and len(material_keys) == 1
            and not any(candidate.conflicts for candidate in candidates)
            and not any(candidate.status == ReviewStatus.BLOCK for candidate in candidates)
            else None
        )
        priced, price_issues = apply_price(
            item,
            material,
            book,
            quote_date=effective_quote_date,
            currency=currency,
            tax_included=tax_included,
        )
        if price_issues:
            note = "；".join([*(value for value in (item.note,) if value), *price_issues])
            priced = priced.model_copy(
                update={
                    "status": (
                        ReviewStatus.REVIEW if priced.status == ReviewStatus.PASS else priced.status
                    ),
                    "note": note,
                }
            )
            issues.append(
                _issue(
                    "pricing",
                    Severity.WARNING,
                    "PRICE_MATCH_REVIEW_REQUIRED",
                    f"{item.mt_code}: {'; '.join(price_issues)}",
                    source_id=item.component_id,
                )
            )
        updated.append(calculate_item(priced))
    return (
        updated,
        issues,
        {
            "price_book": str(resolved_price_book),
            "price_book_sha256": price_book_sha256,
            "price_book_version": book.version,
            "price_book_approved": book.approved,
            "price_book_integrity": "VERIFIED",
            **pricing_context,
        },
    )


def _resolve_resume_pricing_context(
    metadata: Mapping[str, Any],
    *,
    price_book: Path | str | None,
    quote_date: date | str | None,
    currency: str | None,
    tax_included: bool | None,
) -> tuple[
    Path | str | None,
    date | str | None,
    str,
    bool | None,
    dict[str, Any],
    list[str],
]:
    previous = _price_context(metadata)
    explicit_fields: list[str] = []

    if price_book is not None:
        selected_price_book: Path | str | None = price_book
        explicit_fields.append("price_book")
    else:
        selected_price_book = previous.get("price_book")

    if quote_date is not None:
        selected_quote_date: date | str | None = quote_date
        explicit_fields.append("quote_date")
    else:
        selected_quote_date = previous.get("quote_date")

    if currency is not None:
        selected_currency = currency
        explicit_fields.append("currency")
    else:
        selected_currency = str(previous.get("currency") or "CNY")

    if tax_included is not None:
        selected_tax_included = tax_included
        explicit_fields.append("tax_included")
    else:
        selected_tax_included = previous.get("tax_included")
        if not isinstance(selected_tax_included, bool) and selected_tax_included is not None:
            selected_tax_included = None

    return (
        selected_price_book,
        selected_quote_date,
        selected_currency,
        selected_tax_included,
        previous,
        explicit_fields,
    )


def _verify_preserved_price_book(
    previous: Mapping[str, Any],
    selected_price_book: Path | str | None,
) -> tuple[RunIssue | None, dict[str, Any]]:
    previous_path_value = previous.get("price_book")
    if not previous_path_value:
        return None, {"status": "NOT_APPLICABLE"}
    previous_path = Path(str(previous_path_value)).expanduser().resolve()
    if selected_price_book is None:
        return (
            _issue(
                "resume",
                Severity.BLOCK,
                "PRICE_BOOK_CONTEXT_LOST",
                "原运行记录了价格库，但续跑未能恢复其路径。",
                evidence=[str(previous_path)],
            ),
            {
                "status": "FAILED",
                "expected_path": str(previous_path),
                "expected_sha256": previous.get("price_book_sha256"),
                "observed_sha256": None,
            },
        )

    selected_path = Path(selected_price_book).expanduser().resolve()
    if str(selected_path).casefold() != str(previous_path).casefold():
        return (
            None,
            {
                "status": "REPLACED_EXPLICITLY",
                "expected_path": str(previous_path),
                "selected_path": str(selected_path),
                "expected_sha256": previous.get("price_book_sha256"),
            },
        )

    expected_sha256 = previous.get("price_book_sha256")
    if not expected_sha256:
        return (
            _issue(
                "resume",
                Severity.BLOCK,
                "PRICE_BOOK_HASH_MISSING",
                "原运行未保存价格库SHA-256，无法证明续跑仍使用同一版本。",
                evidence=[str(previous_path)],
                action="重新执行完整run，或显式选择另一路径且版本受控的价格库",
            ),
            {
                "status": "FAILED",
                "expected_path": str(previous_path),
                "expected_sha256": None,
                "observed_sha256": None,
            },
        )
    try:
        observed_sha256 = _sha256_file(selected_path)
    except Exception as exc:
        return (
            _issue(
                "resume",
                Severity.BLOCK,
                "PRICE_BOOK_ORIGINAL_MISSING",
                f"原价格库无法读取，续跑已阻断：{type(exc).__name__}: {exc}",
                evidence=[str(previous_path), f"expected_sha256:{expected_sha256}"],
                action="恢复原文件，或显式指定另一路径的新版本价格库",
            ),
            {
                "status": "FAILED",
                "expected_path": str(previous_path),
                "expected_sha256": expected_sha256,
                "observed_sha256": None,
            },
        )
    if observed_sha256 != expected_sha256:
        return (
            _issue(
                "resume",
                Severity.BLOCK,
                "PRICE_BOOK_HASH_MISMATCH",
                "原价格库内容已变化，续跑拒绝静默使用被替换的文件。",
                evidence=[
                    str(previous_path),
                    f"expected_sha256:{expected_sha256}",
                    f"observed_sha256:{observed_sha256}",
                ],
                action="恢复原文件，或以新路径显式指定受控的新价格库版本",
            ),
            {
                "status": "FAILED",
                "expected_path": str(previous_path),
                "expected_sha256": expected_sha256,
                "observed_sha256": observed_sha256,
            },
        )
    return (
        None,
        {
            "status": "VERIFIED",
            "expected_path": str(previous_path),
            "expected_sha256": expected_sha256,
            "observed_sha256": observed_sha256,
        },
    )


def _pricing_audit_changes(
    previous: Mapping[str, Any],
    current: Mapping[str, Any],
) -> dict[str, dict[str, Any]]:
    fields = (
        "price_book",
        "price_book_sha256",
        "price_book_version",
        "quote_date",
        "currency",
        "tax_included",
    )
    return {
        field: {"from": previous.get(field), "to": current.get(field)}
        for field in fields
        if previous.get(field) != current.get(field)
    }


def _price_evidence_edges(
    items: Sequence[TakeoffItem],
    price_metadata: Mapping[str, Any],
) -> list[EvidenceEdge]:
    edges: list[EvidenceEdge] = []
    for item in items:
        if not item.component_id or not item.price_entry_id or item.unit_price is None:
            continue
        identity = json.dumps(
            ["component_to_price", item.component_id, item.price_entry_id],
            ensure_ascii=False,
            separators=(",", ":"),
        )
        edge_id = f"edge:{hashlib.sha256(identity.encode()).hexdigest()[:24]}"
        edges.append(
            EvidenceEdge(
                id=edge_id,
                relation="component_to_price",
                source_id=item.component_id,
                target_id=item.price_entry_id,
                basis=[
                    "exact_approved_price_match",
                    f"price_book:{price_metadata.get('price_book')}",
                    f"version:{price_metadata.get('price_book_version')}",
                    f"quote_date:{price_metadata.get('quote_date')}",
                    f"currency:{price_metadata.get('currency')}",
                    f"tax_included:{price_metadata.get('tax_included')}",
                    f"unit_price:{item.unit_price}",
                ],
                confidence=1.0,
                status=ReviewStatus.PASS,
            )
        )
    return edges


def _render_occurrences(
    occurrences: Sequence[Any],
    sheets: Sequence[Any],
    source_dxfs: Mapping[str, Path],
    output_dir: Path,
    issues: list[RunIssue],
    *,
    maximum: int = 250,
) -> dict[str, Any]:
    sheet_by_id = {sheet.id: sheet for sheet in sheets}
    grouped: dict[
        tuple[str, str],
        dict[str, tuple[float, float, float, float]],
    ] = defaultdict(dict)
    for occurrence in sorted(occurrences, key=lambda value: value.id)[:maximum]:
        point = occurrence.leader_target or occurrence.anchor
        if point is None:
            continue
        sheet = sheet_by_id.get(occurrence.sheet_id)
        radius = 600.0
        if sheet and sheet.bbox:
            diagonal = (
                (sheet.bbox[2] - sheet.bbox[0]) ** 2 + (sheet.bbox[3] - sheet.bbox[1]) ** 2
            ) ** 0.5
            radius = max(150.0, min(1_500.0, diagonal * 0.04))
        sheet_layout = sheet.layout if sheet is not None else None
        layout = (
            sheet_layout
            if sheet_layout and sheet_layout != "Model" and "#viewport:" not in sheet_layout
            else "Model"
        )
        grouped[(occurrence.source_file_id, layout)][occurrence.id] = (
            point[0] - radius,
            point[1] - radius,
            point[0] + radius,
            point[1] + radius,
        )
    render_index: dict[str, Any] = {
        "schema_version": "1.1",
        "groups": [],
    }
    for (source_id, layout), regions in sorted(grouped.items()):
        source = source_dxfs.get(source_id)
        if source is None:
            issues.append(
                _issue(
                    "render",
                    Severity.WARNING,
                    "EVIDENCE_SOURCE_DXF_MISSING",
                    f"找不到{source_id}的DXF，无法渲染{len(regions)}个证据区域。",
                    source_id=source_id,
                    evidence=sorted(regions)[:24],
                )
            )
            continue
        group_digest = hashlib.sha256(f"{source_id}\0{layout}".encode()).hexdigest()[:12]
        try:
            result = render_regions(
                source,
                regions,
                output_dir / source_id.replace(":", "_") / group_digest,
                layout=layout,
                target_px=1_600,
            )
        except Exception as exc:
            issues.append(
                _issue(
                    "render",
                    Severity.WARNING,
                    "EVIDENCE_RENDER_FAILED",
                    f"证据图渲染失败：{type(exc).__name__}: {exc}",
                    source_id=source_id,
                    evidence=[str(source)],
                )
            )
        else:
            render_index["groups"].append(
                {
                    "source_file_id": source_id,
                    "source": str(source),
                    "layout": layout,
                    "occurrence_ids": sorted(regions),
                    "regions": {
                        occurrence_id: {
                            "occurrence_id": occurrence_id,
                            "bbox": list(regions[occurrence_id]),
                            **result.get("regions", {}).get(occurrence_id, {}),
                        }
                        for occurrence_id in sorted(regions)
                    },
                    "requested_count": result.get("requested_count", len(regions)),
                    "rendered_count": result.get("rendered_count", 0),
                    "skipped_entity_count": result.get("skipped_entity_count", 0),
                    "skipped_entity_type_counts": result.get(
                        "skipped_entity_type_counts", {}
                    ),
                }
            )
    if len(occurrences) > maximum:
        issues.append(
            _issue(
                "render",
                Severity.WARNING,
                "EVIDENCE_RENDER_LIMIT",
                f"MT候选共{len(occurrences)}个，本次只渲染前{maximum}个。",
            )
        )
    render_index["group_count"] = len(render_index["groups"])
    render_index["occurrence_count"] = sum(
        len(group["occurrence_ids"]) for group in render_index["groups"]
    )
    skipped_type_counts: Counter[str] = Counter()
    for group in render_index["groups"]:
        skipped_type_counts.update(group.get("skipped_entity_type_counts", {}))
    render_index["skipped_entity_count"] = sum(skipped_type_counts.values())
    render_index["skipped_entity_type_counts"] = dict(sorted(skipped_type_counts.items()))
    write_json_atomic(output_dir / "index.json", render_index)
    return render_index


def run_pipeline(
    input_path: Path | str,
    run_dir: Path | str,
    *,
    price_book: Path | str | None = None,
    confirmations: Path | str | None = None,
    quote_date: date | str | None = None,
    currency: str = "CNY",
    tax_included: bool | None = None,
    ingest_limits: IngestLimits | None = None,
    render_evidence: bool = True,
    stainless_code_families: Sequence[str] | None = None,
    review_code_families: Sequence[str] | None = None,
) -> PipelineResult:
    """Run every deterministic stage and always leave a structured diagnostic trail."""

    root = Path(run_dir).expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True)
    analysis_dir = root / "analysis"
    index_dir = root / "index"
    converted_dir = root / "converted"
    output_dir = root / "outputs"
    for directory in (analysis_dir, index_dir, converted_dir, output_dir):
        directory.mkdir(parents=True, exist_ok=True)
    issues: list[RunIssue] = []
    result = PipelineResult(status=ReviewStatus.BLOCK, run_dir=str(root), issues=issues)
    stainless_families = tuple(
        DEFAULT_STAINLESS_CODE_FAMILIES
        if stainless_code_families is None
        else stainless_code_families
    )
    review_families = tuple(
        DEFAULT_REVIEW_CODE_FAMILIES if review_code_families is None else review_code_families
    )

    ingest = ingest_input(input_path, root, limits=ingest_limits)
    write_json_atomic(root / "ingest.json", ingest.model_dump(mode="json"))
    issues.extend(ingest.issues)
    if not ingest.succeeded:
        write_json_atomic(root / "run.json", result.to_dict())
        return result

    dwg_files = [Path(file.absolute_path) for file in ingest.files if file.suffix == ".dwg"]
    direct_dxfs = [Path(file.absolute_path) for file in ingest.files if file.suffix == ".dxf"]
    file_by_absolute = {str(Path(file.absolute_path).resolve()): file for file in ingest.files}
    conversion: ConversionAudit = convert_dwgs(dwg_files, converted_dir)
    write_json_atomic(root / "conversion.json", conversion.to_dict())
    dxf_paths = list(direct_dxfs)
    source_ids: dict[str, str] = {
        str(path.resolve()): file_by_absolute[str(path.resolve())].id for path in direct_dxfs
    }
    source_dxfs: dict[str, Path] = {
        file_by_absolute[str(path.resolve())].id: path for path in direct_dxfs
    }
    for record in conversion.records:
        source_file = file_by_absolute.get(str(Path(record.source).resolve()))
        if record.status != "converted":
            issues.append(
                _issue(
                    "conversion",
                    Severity.ERROR,
                    "DWG_CONVERSION_FAILED",
                    record.error or "DWG转换失败",
                    source_id=source_file.id if source_file else None,
                    evidence=[record.source, record.stderr],
                )
            )
            continue
        destination = Path(record.destination)
        dxf_paths.append(destination)
        if source_file:
            source_file.converted_paths.append(str(destination))
            source_ids[str(destination.resolve())] = source_file.id
            source_dxfs[source_file.id] = destination
    if not dxf_paths:
        issues.append(
            _issue(
                "index",
                Severity.BLOCK,
                "NO_READABLE_CAD",
                "输入中没有可读取的DXF，且DWG未成功转换。",
            )
        )
        write_json_atomic(root / "issues.json", [value.model_dump(mode="json") for value in issues])
        write_json_atomic(root / "run.json", result.to_dict())
        return result

    bundle, index_issues = _index_inputs(dxf_paths, source_ids, index_dir)
    issues.extend(index_issues)
    source_names = {file.id: file.relative_path for file in ingest.files}
    panel_expansion = expand_viewport_panels(
        bundle.sheets,
        bundle.entities,
        source_names=source_names,
    )
    write_json_atomic(analysis_dir / "panels.json", panel_expansion.to_dict())
    sheets, entities = choose_analysis_view(bundle.sheets, bundle.entities, panel_expansion)

    workbook_materials, material_issues = _load_materials(
        ingest.files,
        stainless_code_families=stainless_families,
        review_code_families=review_families,
    )
    issues.extend(material_issues)
    cad_materials = parse_cad_material_specs(entities)
    materials = annotate_material_conflicts([*workbook_materials, *cad_materials])
    materials, material_conflict_issues = _block_docx_cross_source_conflicts(materials)
    issues.extend(material_conflict_issues)
    if cad_materials:
        issues.append(
            _issue(
                "materials",
                Severity.INFO,
                "CAD_MATERIAL_SPECS_RECOVERED",
                f"从CAD材料表恢复{len(cad_materials)}条MT定义，并与工作簿材料交叉标注冲突。",
                evidence=[material.id for material in cad_materials[:24]],
            )
        )
    write_json_atomic(
        analysis_dir / "materials.json",
        [material.model_dump(mode="json") for material in materials],
    )
    explicit_occurrences = detect_mt_occurrences(
        entities,
        materials=(),
        stainless_code_families=stainless_families,
        review_code_families=review_families,
    )
    material_mentions = detect_material_mentions(
        entities,
        occurrences=explicit_occurrences,
    )
    material_mention_edges, docx_occurrences = link_docx_material_mentions(
        material_mentions,
        materials,
        stainless_code_families=stainless_families,
    )
    occurrences = deduplicate_occurrences([*explicit_occurrences, *docx_occurrences])
    write_json_atomic(
        analysis_dir / "mt_occurrences.json",
        [occurrence.model_dump(mode="json") for occurrence in occurrences],
    )
    write_json_atomic(
        analysis_dir / "material_mentions.json",
        [mention.model_dump(mode="json") for mention in material_mentions],
    )
    write_json_atomic(
        analysis_dir / "material_mention_matches.json",
        [edge.model_dump(mode="json") for edge in material_mention_edges],
    )
    vector_probe_payload = probe_repeated_vectors(
        source_dxfs,
        sheets,
        occurrences,
    )
    vector_probe_path = analysis_dir / "vector_quantity_probes.json"
    write_json_atomic(vector_probe_path, vector_probe_payload)
    vector_review_count = int(
        vector_probe_payload.get("summary", {}).get("review_candidate_count", 0) or 0
    )
    if vector_review_count:
        issues.append(
            _issue(
                "takeoff",
                Severity.INFO,
                "VECTOR_QUANTITY_REVIEW_CANDIDATES",
                f"原始DXF局部重复图形产生{vector_review_count}条数量审核候选；"
                "仅供REVIEW，未自动写入工程量。",
                evidence=[
                    str(probe.get("occurrence_id"))
                    for probe in vector_probe_payload.get("probes", [])
                    if probe.get("recommended_quantity") is not None
                ][:24],
            )
        )
    if vector_probe_payload.get("issues"):
        issues.append(
            _issue(
                "takeoff",
                Severity.WARNING,
                "VECTOR_QUANTITY_PROBE_PARTIAL",
                "部分原始DXF局部图形无法读取；对应项目未生成数量候选。",
                evidence=[
                    str(value.get("source_file_id") or value.get("code"))
                    for value in vector_probe_payload["issues"]
                ][:24],
            )
        )
    if material_mentions:
        issues.append(
            _issue(
                "materials",
                Severity.INFO,
                "UNNUMBERED_METAL_MATERIAL_MENTIONS",
                f"发现{len(material_mentions)}条无可识别材料代号的金属/不锈钢描述；"
                "仅保留为低置信审核候选，未伪造MT编号。",
                evidence=[mention.id for mention in material_mentions[:24]],
            )
        )
    unique_docx_mentions = {
        edge.source_id for edge in material_mention_edges if edge.status == ReviewStatus.REVIEW
    }
    blocked_docx_mentions = {
        edge.source_id for edge in material_mention_edges if edge.status == ReviewStatus.BLOCK
    }
    if unique_docx_mentions:
        issues.append(
            _issue(
                "materials",
                Severity.INFO,
                "DOCX_MATERIAL_MENTION_CANDIDATES",
                f"{len(unique_docx_mentions)}条无编号CAD描述唯一匹配DOCX材料代号；"
                "仅建立低置信REVIEW候选，未直接确认构件或数量。",
                evidence=sorted(unique_docx_mentions)[:24],
            )
        )
    if blocked_docx_mentions:
        issues.append(
            _issue(
                "materials",
                Severity.BLOCK,
                "DOCX_MATERIAL_MENTION_AMBIGUOUS",
                f"{len(blocked_docx_mentions)}条CAD材料描述对应多个代号或冲突定义；"
                "未生成带代号构件候选。",
                evidence=sorted(blocked_docx_mentions)[:24],
                action="在review-pack.json中核对文档材料候选并人工确认唯一代号",
            )
        )
    relation_edges = rank_evidence_edges(
        sheets,
        occurrences,
        entities,
        promote_explicit=True,
    )
    write_json_atomic(
        analysis_dir / "relation_edges.json",
        [edge.model_dump(mode="json") for edge in relation_edges],
    )
    try:
        confirmation_bundle = _load_confirmations(confirmations)
    except Exception as exc:
        confirmation_bundle = ConfirmationBundle()
        issues.append(
            _issue(
                "takeoff",
                Severity.ERROR,
                "CONFIRMATIONS_READ_FAILED",
                f"确认文件读取失败：{type(exc).__name__}: {exc}",
                evidence=[str(confirmations)],
            )
        )
    confirmation_issue = _confirmation_issue(confirmation_bundle)
    if confirmation_issue is not None:
        issues.append(confirmation_issue)
    takeoff: TakeoffBuildResult = build_takeoff(
        sheets,
        entities,
        occurrences,
        relation_edges,
        materials=materials,
        confirmations=confirmation_bundle.selections,
    )
    takeoff.evidence_edges.extend(material_mention_edges)
    issues.extend(takeoff.issues)
    write_json_atomic(analysis_dir / "takeoff_draft.json", takeoff.to_dict())
    priced_items, price_issues, price_metadata = _price_items(
        takeoff.items,
        materials,
        price_book,
        quote_date=quote_date,
        currency=currency,
        tax_included=tax_included,
    )
    issues.extend(price_issues)
    takeoff.evidence_edges.extend(_price_evidence_edges(priced_items, price_metadata))

    review_pack_path = root / "review-pack.json"
    write_json_atomic(
        review_pack_path,
        _build_review_pack(
            takeoff,
            priced_items,
            sheets,
            entities,
            occurrences,
            relation_edges,
            confirmation_bundle,
            source_files=ingest.files,
            materials=materials,
            material_mentions=material_mentions,
            material_mention_edges=material_mention_edges,
            vector_probe_payload=vector_probe_payload,
            issues=issues,
            metadata={
                "run_mode": "full",
                "input_sha256": ingest.input_sha256,
                **price_metadata,
            },
        ),
    )

    if render_evidence and occurrences:
        _render_occurrences(
            occurrences,
            sheets,
            source_dxfs,
            root / "evidence_images",
            issues,
        )
    quote_path = build_quote_workbook(
        priced_items,
        output_dir / "不锈钢算量报价.xlsx",
        edges=takeoff.evidence_edges,
        measurements=takeoff.measurements,
        issues=issues,
        metadata={
            "input_sha256": ingest.input_sha256,
            "source_files": len(ingest.files),
            "cad_sources": len(bundle.results),
            "virtual_panels": len(panel_expansion.sheets),
            "mt_occurrences": len(occurrences),
            "material_mentions": len(material_mentions),
            "docx_materials": sum(
                material.source_type == "docx_material_book" for material in materials
            ),
            "material_mention_match_candidates": len(material_mention_edges),
            "material_mention_unique_matches": len(unique_docx_mentions),
            "material_mention_blocked_matches": len(blocked_docx_mentions),
            "vector_quantity_review_candidates": vector_review_count,
            "confirmation_schema_version": confirmation_bundle.schema_version,
            "confirmed_components": sum(
                bool(value) for value in confirmation_bundle.selections.values()
            ),
            **price_metadata,
        },
    )
    write_json_atomic(
        output_dir / "takeoff.json",
        [item.model_dump(mode="json") for item in priced_items],
    )
    write_json_atomic(
        output_dir / "evidence_graph.json",
        [edge.model_dump(mode="json") for edge in takeoff.evidence_edges],
    )
    write_json_atomic(
        root / "issues.json",
        [value.model_dump(mode="json") for value in issues],
    )

    manifest = ProjectManifest(
        project_id=f"project:{(ingest.input_sha256 or 'unknown')[:24]}",
        project_name=Path(input_path).stem,
        input_path=str(Path(input_path).resolve()),
        run_dir=str(root),
        created_at=datetime.now(UTC).isoformat(),
        files=ingest.files,
        sheets=sheets,
        issues=issues,
        metadata={
            "conversion": conversion.to_dict(),
            "confirmation_audit": confirmation_bundle.audit,
            "confirmation_schema_version": confirmation_bundle.schema_version,
            "confirmation_source": confirmation_bundle.source_path,
            "price": price_metadata,
            "material_code_families": {
                "stainless": sorted(stainless_families),
                "review": sorted(review_families),
            },
            "counts": {
                "source_files": len(ingest.files),
                "cad_sources": len(bundle.results),
                "sheets": len(sheets),
                "entities": len(entities),
                "materials": len(materials),
                "mt_occurrences": len(occurrences),
                "material_mentions": len(material_mentions),
                "docx_materials": sum(
                    material.source_type == "docx_material_book" for material in materials
                ),
                "material_mention_match_candidates": len(material_mention_edges),
                "material_mention_unique_matches": len(unique_docx_mentions),
                "material_mention_blocked_matches": len(blocked_docx_mentions),
                "vector_quantity_review_candidates": vector_review_count,
                "components": len(takeoff.components),
                "takeoff_items": len(priced_items),
            },
        },
    )
    manifest_path = root / "manifest.json"
    write_json_atomic(manifest_path, manifest.model_dump(mode="json"))

    status = _overall_status(priced_items, issues)
    result.status = status
    result.quote_path = str(quote_path)
    result.manifest_path = str(manifest_path)
    result.counts = manifest.metadata["counts"]  # type: ignore[assignment]
    result.paths = {
        "ingest": str(root / "ingest.json"),
        "conversion": str(root / "conversion.json"),
        "index_json": str(index_dir / "cad_index.json"),
        "index_sqlite": str(index_dir / "cad_index.sqlite"),
        "materials": str(analysis_dir / "materials.json"),
        "panels": str(analysis_dir / "panels.json"),
        "mt_occurrences": str(analysis_dir / "mt_occurrences.json"),
        "material_mentions": str(analysis_dir / "material_mentions.json"),
        "material_mention_matches": str(analysis_dir / "material_mention_matches.json"),
        "vector_quantity_probes": str(vector_probe_path),
        "relation_edges": str(analysis_dir / "relation_edges.json"),
        "takeoff": str(output_dir / "takeoff.json"),
        "evidence_graph": str(output_dir / "evidence_graph.json"),
        "review_pack": str(review_pack_path),
        "issues": str(root / "issues.json"),
    }
    write_json_atomic(root / "run.json", result.to_dict())
    return result


def _dedupe_issues(values: Sequence[RunIssue]) -> list[RunIssue]:
    output: list[RunIssue] = []
    seen: set[tuple[str, str, str | None, str]] = set()
    for value in values:
        key = (value.stage, value.code, value.source_id, value.message)
        if key not in seen:
            seen.add(key)
            output.append(value)
    return output


def resume_pipeline(
    run_dir: Path | str,
    *,
    price_book: Path | str | None = None,
    confirmations: Path | str | None = None,
    quote_date: date | str | None = None,
    currency: str | None = None,
    tax_included: bool | None = None,
    render_evidence: bool = True,
) -> PipelineResult:
    """Re-run review, pricing and export using immutable stage snapshots.

    This deliberately does not call ingestion, DWG conversion, CAD indexing,
    viewport expansion, MT detection or relation ranking. Missing snapshots are
    reported as a hard error instead of being regenerated implicitly.
    """

    root = Path(run_dir).expanduser().resolve()
    analysis_dir = root / "analysis"
    index_path = root / "index" / "cad_index.json"
    panels_path = analysis_dir / "panels.json"
    materials_path = analysis_dir / "materials.json"
    occurrences_path = analysis_dir / "mt_occurrences.json"
    mentions_path = analysis_dir / "material_mentions.json"
    mention_matches_path = analysis_dir / "material_mention_matches.json"
    vector_probe_path = analysis_dir / "vector_quantity_probes.json"
    edges_path = analysis_dir / "relation_edges.json"
    ingest_path = root / "ingest.json"
    required = [index_path, panels_path, materials_path, occurrences_path, edges_path, ingest_path]
    missing = [str(path) for path in required if not path.is_file()]
    if missing:
        raise FileNotFoundError(
            "resume requires existing stage snapshots; missing: " + ", ".join(missing)
        )

    manifest_path = root / "manifest.json"
    existing_manifest: ProjectManifest | None = None
    if manifest_path.is_file():
        existing_manifest = ProjectManifest.model_validate(
            json.loads(manifest_path.read_text(encoding="utf-8"))
        )
    previous_metadata = dict(existing_manifest.metadata) if existing_manifest else {}
    (
        selected_price_book,
        selected_quote_date,
        selected_currency,
        selected_tax_included,
        previous_price_metadata,
        explicit_price_fields,
    ) = _resolve_resume_pricing_context(
        previous_metadata,
        price_book=price_book,
        quote_date=quote_date,
        currency=currency,
        tax_included=tax_included,
    )
    price_integrity_issue, price_integrity_audit = _verify_preserved_price_book(
        previous_price_metadata,
        selected_price_book,
    )
    price_book_for_pricing = None if price_integrity_issue else selected_price_book

    output_dir = root / "outputs"
    output_dir.mkdir(parents=True, exist_ok=True)
    ingest = IngestResult.model_validate(json.loads(ingest_path.read_text(encoding="utf-8")))
    original_sheets, original_entities, index_sources = _load_index_snapshot(index_path)
    panel_expansion = _load_panel_snapshot(panels_path)
    sheets, entities = choose_analysis_view(
        original_sheets,
        original_entities,
        panel_expansion,
    )
    snapshot_materials = [
        MaterialSpec.model_validate(value)
        for value in json.loads(materials_path.read_text(encoding="utf-8"))
    ]
    # The material snapshot is already the immutable output of the full run's
    # workbook/CAD merge and conflict annotation. Resume must consume it as-is:
    # reparsing the reused CAD index rewrites a stable stage and can duplicate
    # diagnostics without adding evidence.
    materials = snapshot_materials
    occurrences = [
        MtOccurrence.model_validate(value)
        for value in json.loads(occurrences_path.read_text(encoding="utf-8"))
    ]
    material_mentions = (
        [
            MaterialMention.model_validate(value)
            for value in json.loads(mentions_path.read_text(encoding="utf-8"))
        ]
        if mentions_path.is_file()
        else []
    )
    material_mention_edges = (
        [
            EvidenceEdge.model_validate(value)
            for value in json.loads(mention_matches_path.read_text(encoding="utf-8"))
        ]
        if mention_matches_path.is_file()
        else []
    )
    vector_probe_payload = (
        json.loads(vector_probe_path.read_text(encoding="utf-8"))
        if vector_probe_path.is_file()
        else {}
    )
    vector_review_count = int(
        vector_probe_payload.get("summary", {}).get("review_candidate_count", 0) or 0
    )
    relation_edges = [
        EvidenceEdge.model_validate(value)
        for value in json.loads(edges_path.read_text(encoding="utf-8"))
    ]

    issues: list[RunIssue] = []
    issues_path = root / "issues.json"
    if issues_path.is_file():
        previous = json.loads(issues_path.read_text(encoding="utf-8"))
        issues.extend(
            issue
            for issue in (RunIssue.model_validate(value) for value in previous)
            if issue.stage not in {"takeoff", "pricing", "render", "export", "resume"}
        )
    issues.extend(ingest.issues)
    issues.extend(_snapshot_index_issues(index_sources))
    try:
        confirmation_bundle = (
            _load_confirmations(confirmations)
            if confirmations is not None
            else _load_manifest_confirmations(root / "manifest.json")
        )
    except Exception as exc:
        confirmation_bundle = ConfirmationBundle()
        issues.append(
            _issue(
                "takeoff",
                Severity.ERROR,
                "CONFIRMATIONS_READ_FAILED",
                f"确认文件读取失败：{type(exc).__name__}: {exc}",
                evidence=[str(confirmations)],
            )
        )
    confirmation_issue = _confirmation_issue(confirmation_bundle)
    if confirmation_issue is not None:
        issues.append(confirmation_issue)

    takeoff = build_takeoff(
        sheets,
        entities,
        occurrences,
        relation_edges,
        materials=materials,
        confirmations=confirmation_bundle.selections,
    )
    takeoff.evidence_edges.extend(material_mention_edges)
    issues.extend(takeoff.issues)
    write_json_atomic(analysis_dir / "takeoff_draft.json", takeoff.to_dict())
    priced_items, price_issues, price_metadata = _price_items(
        takeoff.items,
        materials,
        price_book_for_pricing,
        quote_date=selected_quote_date,
        currency=selected_currency,
        tax_included=selected_tax_included,
    )
    issues.extend(price_issues)
    if price_integrity_issue is not None:
        issues.append(price_integrity_issue)
        priced_items = _blocked_pricing_items(
            priced_items,
            "原价格库完整性校验失败，续跑商业报价已阻断",
        )
        price_metadata = {
            **price_metadata,
            "price_book": previous_price_metadata.get("price_book"),
            "price_book_sha256": previous_price_metadata.get("price_book_sha256"),
            "price_book_version": previous_price_metadata.get("price_book_version"),
            "price_book_approved": previous_price_metadata.get("price_book_approved"),
            "price_book_integrity": "FAILED",
            "observed_price_book_sha256": price_integrity_audit.get("observed_sha256"),
        }
    takeoff.evidence_edges.extend(_price_evidence_edges(priced_items, price_metadata))

    resumed_at = datetime.now(UTC).isoformat()
    pricing_changes = _pricing_audit_changes(previous_price_metadata, price_metadata)
    pricing_audit_event = {
        "event": "resume",
        "timestamp": resumed_at,
        "explicit_fields": sorted(explicit_price_fields),
        "inherited_fields": sorted(
            {"price_book", "quote_date", "currency", "tax_included"} - set(explicit_price_fields)
        ),
        "changes": pricing_changes,
        "price_book_integrity": price_integrity_audit,
    }
    if explicit_price_fields:
        issues.append(
            _issue(
                "resume",
                Severity.INFO,
                "PRICING_CONTEXT_EXPLICIT_OVERRIDE",
                "续跑显式传入了价格上下文；字段和值变化已写入manifest审计记录。",
                evidence=[
                    f"explicit:{','.join(sorted(explicit_price_fields))}",
                    *(
                        f"{field}:{change['from']}->{change['to']}"
                        for field, change in sorted(pricing_changes.items())
                    ),
                ],
            )
        )
    issues = _dedupe_issues(issues)

    review_pack_path = root / "review-pack.json"
    write_json_atomic(
        review_pack_path,
        _build_review_pack(
            takeoff,
            priced_items,
            sheets,
            entities,
            occurrences,
            relation_edges,
            confirmation_bundle,
            source_files=ingest.files,
            materials=materials,
            material_mentions=material_mentions,
            material_mention_edges=material_mention_edges,
            vector_probe_payload=vector_probe_payload,
            issues=issues,
            metadata={
                "run_mode": "resume",
                "resumed_at": resumed_at,
                "reused_snapshots": [str(path) for path in required],
                "pricing_context_audit": pricing_audit_event,
                **price_metadata,
            },
        ),
    )

    source_dxfs = {
        str(source.get("source_file_id")): Path(str(source.get("source_path")))
        for source in index_sources
        if source.get("source_file_id") and source.get("source_path")
    }
    if render_evidence and occurrences:
        _render_occurrences(
            occurrences,
            sheets,
            source_dxfs,
            root / "evidence_images",
            issues,
        )
        issues = _dedupe_issues(issues)

    quote_path = build_quote_workbook(
        priced_items,
        output_dir / "不锈钢算量报价.xlsx",
        edges=takeoff.evidence_edges,
        measurements=takeoff.measurements,
        issues=issues,
        metadata={
            "run_mode": "resume",
            "resumed_at": resumed_at,
            "input_sha256": ingest.input_sha256,
            "source_files": len(ingest.files),
            "cad_sources": len(index_sources),
            "virtual_panels": len(panel_expansion.sheets),
            "mt_occurrences": len(occurrences),
            "material_mentions": len(material_mentions),
            "docx_materials": sum(
                material.source_type == "docx_material_book" for material in materials
            ),
            "material_mention_match_candidates": len(material_mention_edges),
            "vector_quantity_review_candidates": vector_review_count,
            "confirmation_schema_version": confirmation_bundle.schema_version,
            "confirmed_components": sum(
                bool(value) for value in confirmation_bundle.selections.values()
            ),
            **price_metadata,
        },
    )
    write_json_atomic(
        output_dir / "takeoff.json",
        [item.model_dump(mode="json") for item in priced_items],
    )
    write_json_atomic(
        output_dir / "evidence_graph.json",
        [edge.model_dump(mode="json") for edge in takeoff.evidence_edges],
    )
    write_json_atomic(
        issues_path,
        [issue.model_dump(mode="json") for issue in issues],
    )

    counts = {
        "source_files": len(ingest.files),
        "cad_sources": len(index_sources),
        "sheets": len(sheets),
        "entities": len(entities),
        "materials": len(materials),
        "mt_occurrences": len(occurrences),
        "material_mentions": len(material_mentions),
        "docx_materials": sum(
            material.source_type == "docx_material_book" for material in materials
        ),
        "material_mention_match_candidates": len(material_mention_edges),
        "vector_quantity_review_candidates": vector_review_count,
        "material_mention_unique_matches": len(
            {
                edge.source_id
                for edge in material_mention_edges
                if edge.status == ReviewStatus.REVIEW
            }
        ),
        "material_mention_blocked_matches": len(
            {edge.source_id for edge in material_mention_edges if edge.status == ReviewStatus.BLOCK}
        ),
        "components": len(takeoff.components),
        "takeoff_items": len(priced_items),
    }
    previous_resume_count = int(previous_metadata.get("resume_count", 0) or 0)
    conversion_path = root / "conversion.json"
    conversion_metadata = (
        json.loads(conversion_path.read_text(encoding="utf-8"))
        if conversion_path.is_file()
        else previous_metadata.get("conversion")
    )
    manifest = ProjectManifest(
        project_id=(
            existing_manifest.project_id
            if existing_manifest
            else f"project:{(ingest.input_sha256 or 'unknown')[:24]}"
        ),
        project_name=(
            existing_manifest.project_name if existing_manifest else Path(ingest.input_path).stem
        ),
        input_path=ingest.input_path,
        run_dir=str(root),
        created_at=(existing_manifest.created_at if existing_manifest else resumed_at),
        files=ingest.files,
        sheets=sheets,
        issues=issues,
        metadata={
            **previous_metadata,
            "conversion": conversion_metadata,
            "counts": counts,
            "last_resumed_at": resumed_at,
            "resume_count": previous_resume_count + 1,
            "confirmation_audit": confirmation_bundle.audit,
            "confirmation_schema_version": confirmation_bundle.schema_version,
            "confirmation_source": confirmation_bundle.source_path,
            "price": price_metadata,
            "pricing_context_audit": [
                *(
                    previous_metadata.get("pricing_context_audit", [])
                    if isinstance(previous_metadata.get("pricing_context_audit"), list)
                    else []
                ),
                pricing_audit_event,
            ],
        },
    )
    write_json_atomic(manifest_path, manifest.model_dump(mode="json"))

    result = PipelineResult(
        status=_overall_status(priced_items, issues),
        run_dir=str(root),
        quote_path=str(quote_path),
        manifest_path=str(manifest_path),
        counts=counts,
        issues=issues,
        paths={
            "ingest": str(ingest_path),
            "conversion": str(conversion_path),
            "index_json": str(index_path),
            "index_sqlite": str(root / "index" / "cad_index.sqlite"),
            "panels": str(panels_path),
            "materials": str(materials_path),
            "mt_occurrences": str(occurrences_path),
            "material_mention_matches": str(mention_matches_path),
            "relation_edges": str(edges_path),
            "takeoff": str(output_dir / "takeoff.json"),
            "evidence_graph": str(output_dir / "evidence_graph.json"),
            "review_pack": str(review_pack_path),
            "issues": str(issues_path),
        },
    )
    if mentions_path.is_file():
        result.paths["material_mentions"] = str(mentions_path)
    if vector_probe_path.is_file():
        result.paths["vector_quantity_probes"] = str(vector_probe_path)
    write_json_atomic(root / "run.json", result.to_dict())
    return result
