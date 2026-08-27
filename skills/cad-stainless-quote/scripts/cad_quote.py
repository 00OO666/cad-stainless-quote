#!/usr/bin/env python3
"""CLI for traceable stainless-steel CAD takeoff and quotation."""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import sys
from collections import Counter
from pathlib import Path
from typing import Any

from cadquote.cad_index import build_cad_index
from cadquote.candidate_benchmark import build_candidate_benchmark
from cadquote.candidate_boards import render_occurrence_candidate_boards
from cadquote.converter import convert_dwgs
from cadquote.doctor import run_doctor
from cadquote.evaluation import (
    evaluate_takeoff,
    evaluation_batch_markdown,
    summarize_evaluation_batch,
)
from cadquote.evidence_quality import audit_evidence_quality
from cadquote.exporter import build_quote_workbook
from cadquote.gold import import_gold_workbook
from cadquote.gold_images import MANIFEST_NAME, export_gold_image_assets
from cadquote.image_matching import register_screenshot_to_panel, write_registration
from cadquote.ingest import ingest_input
from cadquote.io import sha256_file, write_json_atomic
from cadquote.linking import rank_evidence_edges
from cadquote.materials import (
    DEFAULT_REVIEW_CODE_FAMILIES,
    DEFAULT_STAINLESS_CODE_FAMILIES,
    load_docx_material_specs,
    load_material_specs,
)
from cadquote.models import (
    CadEntity,
    EvidenceEdge,
    MaterialMention,
    MaterialSpec,
    MeasurementCandidate,
    MtOccurrence,
    ReviewStatus,
    RunIssue,
    Severity,
    Sheet,
    TakeoffItem,
)
from cadquote.mt import detect_material_mentions, detect_mt_occurrences
from cadquote.panels import PanelExpansion, choose_analysis_view, expand_viewport_panels
from cadquote.pipeline import (
    _render_occurrences,
    load_confirmation_bundle,
    resume_pipeline,
    run_pipeline,
)
from cadquote.render import (
    load_regions_json,
    render_indexed_occurrences,
    render_panel_occurrence_crops,
    render_regions,
    viewport_model_regions,
)
from cadquote.takeoff import build_takeoff
from cadquote.vector_probe import probe_repeated_vectors


def _print(payload: Any) -> None:
    text = json.dumps(payload, ensure_ascii=False, indent=2, default=str)
    output_encoding = sys.stdout.encoding or "utf-8"
    try:
        text.encode(output_encoding, errors="strict")
    except (LookupError, UnicodeEncodeError):
        # Raw ZIP lookup keys can contain valid CP437 glyphs that a GBK Windows
        # console cannot represent.  Escape only the terminal copy; JSON artifacts
        # on disk retain their original Unicode audit data.
        text = json.dumps(payload, ensure_ascii=True, indent=2, default=str)
    sys.stdout.write(f"{text}\n")


def _load_json(path: Path | str) -> Any:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _pipeline_cli_summary(result: Any) -> dict[str, Any]:
    severity_counts = Counter(issue.severity.value for issue in result.issues)
    code_counts = Counter(issue.code for issue in result.issues)
    return {
        "status": result.status.value,
        "safe_outcome": (
            "BLOCK means unresolved evidence was withheld; inspect review_pack and issues"
            if result.status.value == "BLOCK"
            else None
        ),
        "run_dir": result.run_dir,
        "quote_path": result.quote_path,
        "manifest_path": result.manifest_path,
        "counts": result.counts,
        "issue_count": len(result.issues),
        "issue_severity_counts": dict(sorted(severity_counts.items())),
        "top_issue_codes": dict(code_counts.most_common(12)),
        "paths": {
            key: value
            for key, value in result.paths.items()
            if key in {"review_pack", "issues", "takeoff", "evidence_graph"}
        },
    }


def _parse_bool(value: str) -> bool:
    normalized = value.strip().casefold()
    if normalized in {"true", "1", "yes", "y", "是"}:
        return True
    if normalized in {"false", "0", "no", "n", "否"}:
        return False
    raise argparse.ArgumentTypeError("must be true or false")


def _takeoff_rows(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return payload
    if not isinstance(payload, dict):
        raise ValueError("takeoff JSON must be an array or object")
    if isinstance(payload.get("items"), list):
        return payload["items"]
    if isinstance(payload.get("rows"), list):
        return [
            value["item"] if isinstance(value, dict) and "item" in value else value
            for value in payload["rows"]
        ]
    raise ValueError("takeoff JSON has no items or gold rows")


def _evaluation_rows(payload: Any) -> tuple[list[dict[str, Any]], list[str | None]]:
    """Load takeoff rows while retaining stable wrapper IDs from gold-import JSON."""

    if isinstance(payload, list):
        raw_rows = payload
    elif isinstance(payload, dict) and isinstance(payload.get("items"), list):
        raw_rows = payload["items"]
    elif isinstance(payload, dict) and isinstance(payload.get("rows"), list):
        raw_rows = payload["rows"]
    else:
        raise ValueError("takeoff JSON has no items or gold rows")
    rows: list[dict[str, Any]] = []
    row_ids: list[str | None] = []
    for value in raw_rows:
        if not isinstance(value, dict):
            raise ValueError("every evaluation row must be an object")
        if isinstance(value.get("item"), dict):
            item = dict(value["item"])
            row_id = value.get("gold_id") or value.get("row_id") or value.get("id")
        else:
            item = dict(value)
            row_id = item.pop("gold_id", None) or item.pop("row_id", None) or item.pop("id", None)
            item.pop("project_id", None)
        rows.append(item)
        row_ids.append(str(row_id) if row_id is not None else None)
    return rows, row_ids


def _evaluation_project_id(payload: Any, fallback: str) -> str:
    if not isinstance(payload, dict):
        return fallback
    metadata = payload.get("metadata")
    values = (
        payload.get("project_id"),
        payload.get("project_name"),
        metadata.get("project_id") if isinstance(metadata, dict) else None,
        metadata.get("project_name") if isinstance(metadata, dict) else None,
    )
    return next((str(value).strip() for value in values if value and str(value).strip()), fallback)


def _evaluation_batch_manifest(path: Path) -> dict[str, Any]:
    payload = _load_json(path)
    if not isinstance(payload, dict):
        raise ValueError("evaluation batch manifest must be an object")
    allowed = {"schema_version", "batch_id", "policy", "tolerance", "projects"}
    unexpected = sorted(set(payload) - allowed)
    if unexpected:
        raise ValueError("unexpected evaluation batch fields: " + ", ".join(unexpected))
    if payload.get("schema_version", "1.0") != "1.0":
        raise ValueError("evaluation batch schema_version must be 1.0")
    batch_id = str(payload.get("batch_id") or path.stem).strip()
    if not batch_id:
        raise ValueError("evaluation batch_id cannot be empty")
    global_policy = payload.get("policy")
    if global_policy is not None and not isinstance(global_policy, str):
        raise ValueError("evaluation batch policy must be a path string")
    global_tolerance = payload.get("tolerance", 1e-3)
    if (
        isinstance(global_tolerance, bool)
        or not isinstance(global_tolerance, (int, float))
        or global_tolerance < 0
    ):
        raise ValueError("evaluation batch tolerance must be a non-negative number")
    raw_projects = payload.get("projects")
    if not isinstance(raw_projects, list) or not raw_projects:
        raise ValueError("evaluation batch projects must be a non-empty array")
    project_ids: set[str] = set()
    projects: list[dict[str, Any]] = []
    project_allowed = {"project_id", "predicted", "gold", "policy", "tolerance"}
    for index, value in enumerate(raw_projects, start=1):
        if not isinstance(value, dict):
            raise ValueError(f"evaluation batch project {index} must be an object")
        unexpected = sorted(set(value) - project_allowed)
        if unexpected:
            raise ValueError(
                f"unexpected fields in evaluation batch project {index}: " + ", ".join(unexpected)
            )
        project_id = str(value.get("project_id") or "").strip()
        if not project_id:
            raise ValueError(f"evaluation batch project {index} has no project_id")
        if project_id in project_ids:
            raise ValueError(f"duplicate evaluation project_id: {project_id}")
        project_ids.add(project_id)
        predicted = value.get("predicted")
        gold = value.get("gold")
        if not isinstance(predicted, str) or not predicted.strip():
            raise ValueError(f"evaluation batch project {project_id} has no predicted path")
        if not isinstance(gold, str) or not gold.strip():
            raise ValueError(f"evaluation batch project {project_id} has no gold path")
        policy = value.get("policy", global_policy)
        if policy is not None and (not isinstance(policy, str) or not policy.strip()):
            raise ValueError(f"evaluation batch project {project_id} policy must be a path string")
        tolerance = value.get("tolerance", global_tolerance)
        if isinstance(tolerance, bool) or not isinstance(tolerance, (int, float)) or tolerance < 0:
            raise ValueError(
                f"evaluation batch project {project_id} tolerance must be non-negative"
            )
        projects.append(
            {
                "project_id": project_id,
                "predicted": predicted.strip(),
                "gold": gold.strip(),
                "policy": policy.strip() if isinstance(policy, str) else None,
                "tolerance": float(tolerance),
            }
        )
    return {
        "schema_version": "1.0",
        "batch_id": batch_id,
        "projects": projects,
    }


def _manifest_file(base: Path, value: str, label: str) -> Path:
    candidate = Path(value)
    resolved = (candidate if candidate.is_absolute() else base / candidate).resolve()
    if not resolved.is_file():
        raise FileNotFoundError(f"{label} is not a file: {resolved}")
    return resolved


def _batch_project_report_name(index: int, project_id: str) -> str:
    digest = hashlib.sha256(project_id.encode("utf-8")).hexdigest()[:12]
    return f"{index:03d}-{digest}.json"


def _assert_batch_outputs_preserve_inputs(
    manifest_path: Path,
    manifest: dict[str, Any],
    output_dir: Path,
) -> None:
    manifest_base = manifest_path.parent
    inputs = {manifest_path.resolve()}
    for project in manifest["projects"]:
        for field in ("predicted", "gold", "policy"):
            value = project.get(field)
            if not value:
                continue
            candidate = Path(value)
            inputs.add(
                (candidate if candidate.is_absolute() else manifest_base / candidate).resolve()
            )
    outputs = {
        (output_dir / "summary.json").resolve(),
        (output_dir / "summary.md").resolve(),
        *(
            (
                output_dir / "projects" / _batch_project_report_name(index, project["project_id"])
            ).resolve()
            for index, project in enumerate(manifest["projects"], start=1)
        ),
    }
    collisions = sorted(inputs & outputs, key=str)
    if collisions:
        raise ValueError(
            "evaluation batch outputs would overwrite input files: "
            + ", ".join(str(path) for path in collisions)
        )


def _batch_project_summary(
    report: dict[str, Any],
    *,
    project_id: str,
    report_path: str,
    inputs: dict[str, Any],
) -> dict[str, Any]:
    return {
        "project_id": project_id,
        "overall_gate": report.get("overall_gate", "BLOCKED"),
        "gold_rows": report.get("gold_count", report.get("gold_rows")),
        "predicted_rows": report.get("predicted_count", report.get("predicted_rows")),
        "eligible_gold_rows": report.get("eligible_gold_rows"),
        "correct_rows": report.get("correct_rows"),
        "matched_rows": report.get("matched_count"),
        "missing_rows": report.get("missing_count"),
        "unexpected_rows": report.get("unexpected_count"),
        "replication_recall": report.get("replication_recall"),
        "output_precision": report.get("output_precision"),
        "target_accuracy": report.get("target_accuracy"),
        "meets_target": report.get("meets_target"),
        "policy_version": report.get("policy_version"),
        "policy_hash": report.get("policy_hash"),
        "policy_pending_fields": report.get("policy_pending_fields", []),
        "duplicate_predicted_count": report.get("duplicate_predicted_count"),
        "duplicate_gold_count": report.get("duplicate_gold_count"),
        "report_path": report_path,
        "inputs": inputs,
        "error": report.get("batch_error"),
    }


def _load_index(path: Path | str) -> tuple[list[Sheet], list[CadEntity]]:
    payload = _load_json(path)
    sources = payload.get("sources", []) if isinstance(payload, dict) else []
    sheets = [
        Sheet.model_validate(value) for source in sources for value in source.get("sheets", [])
    ]
    entities = [
        CadEntity.model_validate(value)
        for source in sources
        for value in source.get("entities", [])
    ]
    return sheets, entities


def _apply_panels(
    sheets: list[Sheet],
    entities: list[CadEntity],
    panels_path: Path | str | None,
) -> tuple[list[Sheet], list[CadEntity]]:
    if panels_path is None:
        return sheets, entities
    panel_payload = _load_json(panels_path)
    expansion = PanelExpansion(
        sheets=[Sheet.model_validate(value) for value in panel_payload.get("sheets", [])],
        entities=[CadEntity.model_validate(value) for value in panel_payload.get("entities", [])],
        source_panel_counts={
            str(key): int(value)
            for key, value in panel_payload.get("source_panel_counts", {}).items()
        },
        warnings=[str(value) for value in panel_payload.get("warnings", [])],
    )
    return choose_analysis_view(sheets, entities, expansion)


def command_doctor(_: argparse.Namespace) -> int:
    report = run_doctor()
    _print(report)
    return 0 if report["status"] == "PASS" else 2


def command_run(args: argparse.Namespace) -> int:
    stainless_families = {
        *DEFAULT_STAINLESS_CODE_FAMILIES,
        *(args.stainless_code_family or []),
    }
    review_families = {
        *DEFAULT_REVIEW_CODE_FAMILIES,
        *(args.review_code_family or []),
    } - stainless_families
    result = run_pipeline(
        args.input,
        args.out,
        price_book=args.price_book,
        confirmations=args.confirmations,
        quote_date=args.quote_date,
        currency=args.currency,
        tax_included=args.tax_included,
        render_evidence=not args.no_render,
        stainless_code_families=sorted(stainless_families),
        review_code_families=sorted(review_families),
    )
    _print(_pipeline_cli_summary(result))
    return 0 if result.status.value in {"PASS", "REVIEW"} else 2


def command_resume(args: argparse.Namespace) -> int:
    result = resume_pipeline(
        args.run_dir,
        price_book=args.price_book,
        confirmations=args.confirmations,
        quote_date=args.quote_date,
        currency=args.currency,
        tax_included=args.tax_included,
        render_evidence=not args.no_render,
    )
    _print(_pipeline_cli_summary(result))
    return 0 if result.status.value in {"PASS", "REVIEW"} else 2


def command_ingest(args: argparse.Namespace) -> int:
    result = ingest_input(args.input, args.out)
    payload = result.model_dump(mode="json")
    write_json_atomic(Path(args.out) / "ingest.json", payload)
    _print(payload)
    return 0 if result.succeeded else 2


def command_convert(args: argparse.Namespace) -> int:
    audit = convert_dwgs(args.input, args.out)
    payload = audit.to_dict()
    write_json_atomic(Path(args.out) / "conversion.json", payload)
    _print(payload)
    return 0 if audit.failed_count == 0 else 2


def command_index(args: argparse.Namespace) -> int:
    output = Path(args.out)
    output.mkdir(parents=True, exist_ok=True)
    bundle = build_cad_index(
        args.dxf,
        sqlite_path=output / "cad_index.sqlite",
        json_path=output / "cad_index.json",
    )
    _print(
        {
            "source_count": len(bundle.results),
            "sheet_count": len(bundle.sheets),
            "entity_count": len(bundle.entities),
            "json": str(output / "cad_index.json"),
            "sqlite": str(output / "cad_index.sqlite"),
        }
    )
    return 0


def command_panels(args: argparse.Namespace) -> int:
    """Rebuild viewport panels from an immutable CAD index without re-conversion."""

    payload = _load_json(args.index)
    sources = payload.get("sources", []) if isinstance(payload, dict) else []
    sheets = [
        Sheet.model_validate(value) for source in sources for value in source.get("sheets", [])
    ]
    entities = [
        CadEntity.model_validate(value)
        for source in sources
        for value in source.get("entities", [])
    ]
    source_names = {
        str(source.get("source_file_id")): str(source.get("source_path") or "")
        for source in sources
        if source.get("source_file_id")
    }
    expansion = expand_viewport_panels(sheets, entities, source_names=source_names)
    write_json_atomic(Path(args.out), expansion.to_dict())
    _print(
        {
            "panel_count": len(expansion.sheets),
            "entity_count": len(expansion.entities),
            "warning_count": len(expansion.warnings),
            "kind_distribution": dict(
                sorted(Counter(sheet.kind for sheet in expansion.sheets).items())
            ),
            "output": str(Path(args.out).resolve()),
        }
    )
    return 0


def command_vector_probe(args: argparse.Namespace) -> int:
    """Revisit raw DXF polylines near MT leader targets for REVIEW-only counts."""

    payload = _load_json(args.index)
    sheets, entities = _load_index(args.index)
    sheets, _ = _apply_panels(sheets, entities, args.panels)
    occurrences = [MtOccurrence.model_validate(value) for value in _load_json(args.occurrences)]
    sources = payload.get("sources", []) if isinstance(payload, dict) else []
    source_paths = {
        str(source.get("source_file_id")): Path(str(source.get("source_path")))
        for source in sources
        if source.get("source_file_id") and source.get("source_path")
    }
    result = probe_repeated_vectors(
        source_paths,
        sheets,
        occurrences,
        radius=args.radius,
        geometry_tolerance=args.geometry_tolerance,
        max_primitives=args.max_primitives,
    )
    write_json_atomic(Path(args.out), result)
    _print({**result["summary"], "output": str(Path(args.out).resolve())})
    return 0 if not result["issues"] else 2


def command_materials(args: argparse.Namespace) -> int:
    stainless_families = {
        *DEFAULT_STAINLESS_CODE_FAMILIES,
        *(args.stainless_code_family or []),
    }
    review_families = {
        *DEFAULT_REVIEW_CODE_FAMILIES,
        *(args.review_code_family or []),
    } - stainless_families
    values = (
        load_docx_material_specs(
            args.workbook,
            stainless_families=stainless_families,
            review_families=review_families,
        )
        if args.workbook.suffix.lower() == ".docx"
        else load_material_specs(args.workbook)
    )
    payload = [value.model_dump(mode="json") for value in values]
    write_json_atomic(Path(args.out), payload)
    _print(
        {
            "material_count": len(values),
            "docx_material_count": sum(
                value.source_type == "docx_material_book" for value in values
            ),
            "output": str(Path(args.out).resolve()),
        }
    )
    return 0


def command_detect_mt(args: argparse.Namespace) -> int:
    sheets, entities = _load_index(args.index)
    _, entities = _apply_panels(sheets, entities, args.panels)
    materials = (
        [MaterialSpec.model_validate(value) for value in _load_json(args.materials)]
        if args.materials
        else []
    )
    stainless_families = {
        *DEFAULT_STAINLESS_CODE_FAMILIES,
        *(args.stainless_code_family or []),
    }
    review_families = {
        *DEFAULT_REVIEW_CODE_FAMILIES,
        *(args.review_code_family or []),
    } - stainless_families
    values = detect_mt_occurrences(
        entities,
        materials=materials,
        stainless_code_families=stainless_families,
        review_code_families=review_families,
    )
    payload = [value.model_dump(mode="json") for value in values]
    write_json_atomic(Path(args.out), payload)
    mentions = detect_material_mentions(entities, occurrences=values)
    if args.mentions_out:
        write_json_atomic(
            Path(args.mentions_out),
            [value.model_dump(mode="json") for value in mentions],
        )
    _print(
        {
            "occurrence_count": len(values),
            "material_mention_count": len(mentions),
            "code_family_distribution": dict(
                sorted(Counter(value.material_code_family or "unknown" for value in values).items())
            ),
            "stainless_code_families": sorted(stainless_families),
            "review_code_families": sorted(review_families),
            "output": str(Path(args.out).resolve()),
            "mentions_output": (
                str(Path(args.mentions_out).resolve()) if args.mentions_out else None
            ),
        }
    )
    return 0


def command_link(args: argparse.Namespace) -> int:
    sheets, entities = _load_index(args.index)
    sheets, entities = _apply_panels(sheets, entities, args.panels)
    occurrences = [MtOccurrence.model_validate(value) for value in _load_json(args.occurrences)]
    values = rank_evidence_edges(
        sheets,
        occurrences,
        entities,
        promote_explicit=args.promote_explicit,
    )
    payload = [value.model_dump(mode="json") for value in values]
    write_json_atomic(Path(args.out), payload)
    _print({"edge_count": len(values), "output": str(Path(args.out).resolve())})
    return 0


def command_takeoff(args: argparse.Namespace) -> int:
    sheets, entities = _load_index(args.index)
    sheets, entities = _apply_panels(sheets, entities, args.panels)
    occurrences = [MtOccurrence.model_validate(value) for value in _load_json(args.occurrences)]
    edges = [EvidenceEdge.model_validate(value) for value in _load_json(args.edges)]
    materials = (
        [MaterialSpec.model_validate(value) for value in _load_json(args.materials)]
        if args.materials
        else []
    )
    confirmations = load_confirmation_bundle(args.confirmations).selections
    result = build_takeoff(
        sheets,
        entities,
        occurrences,
        edges,
        materials=materials,
        confirmations=confirmations,
    )
    write_json_atomic(Path(args.out), result.to_dict())
    _print(
        {
            "component_count": len(result.components),
            "item_count": len(result.items),
            "review_issue_count": len(result.issues),
            "output": str(Path(args.out).resolve()),
        }
    )
    return 0


def command_quote(args: argparse.Namespace) -> int:
    payload = _load_json(args.takeoff)
    rows = _takeoff_rows(payload)
    items = [TakeoffItem.model_validate(value) for value in rows]
    edges: list[EvidenceEdge] = []
    measurements: list[MeasurementCandidate] = []
    issues: list[RunIssue] = []
    metadata: dict[str, Any] = {}
    limitations: list[str] = []
    if isinstance(payload, dict):
        edges = [
            EvidenceEdge.model_validate(value)
            for value in payload.get("evidence_edges", payload.get("edges", []))
        ]
        measurements = [
            MeasurementCandidate.model_validate(value) for value in payload.get("measurements", [])
        ]
        issues = [RunIssue.model_validate(value) for value in payload.get("issues", [])]
        raw_metadata = payload.get("metadata", {})
        metadata = dict(raw_metadata) if isinstance(raw_metadata, dict) else {}
        missing_sections = [name for name in ("measurements", "issues") if name not in payload]
        if "evidence_edges" not in payload and "edges" not in payload:
            missing_sections.insert(0, "evidence_edges")
        if missing_sections:
            limitations.append("输入对象缺少证据区段: " + ", ".join(missing_sections))
    else:
        limitations.append("输入是旧版纯行数组，无法恢复证据边、尺寸候选和阶段问题")
    if limitations:
        issues.append(
            RunIssue(
                stage="export",
                severity=Severity.WARNING,
                code="PARTIAL_QUOTE_EVIDENCE_UNAVAILABLE",
                message="；".join(limitations),
                suggested_action="改用takeoff_draft.json或完整run/resume命令导出",
            )
        )
        metadata["export_limitations"] = limitations
    output = build_quote_workbook(
        items,
        args.out,
        edges=edges,
        measurements=measurements,
        issues=issues,
        metadata=metadata,
    )
    _print(
        {
            "item_count": len(items),
            "edge_count": len(edges),
            "measurement_count": len(measurements),
            "issue_count": len(issues),
            "limitations": limitations,
            "output": str(output.resolve()),
        }
    )
    return 0


def command_render(args: argparse.Namespace) -> int:
    if args.regions:
        regions = load_regions_json(args.regions)
        layout = args.layout
    else:
        regions = viewport_model_regions(args.dxf, layout=args.layout)
        layout = "Model"
    result = render_regions(args.dxf, regions, args.out, layout=layout)
    _print(result)
    return 0


def command_render_occurrences(args: argparse.Namespace) -> int:
    """Render MT evidence from saved index/panel snapshots without exporting XLSX."""

    if args.maximum < 1:
        raise ValueError("maximum must be at least 1")
    if args.raw_dxf and args.panel_crops:
        raise ValueError("raw-dxf and panel-crops are mutually exclusive")
    index_payload = _load_json(args.index)
    sheets, entities = _load_index(args.index)
    sheets, entities = _apply_panels(sheets, entities, args.panels)
    occurrences = [MtOccurrence.model_validate(value) for value in _load_json(args.occurrences)]
    sources = index_payload.get("sources", []) if isinstance(index_payload, dict) else []
    source_dxfs = {
        str(source["source_file_id"]): Path(str(source["source_path"])).resolve()
        for source in sources
        if source.get("source_file_id") and source.get("source_path")
    }
    if args.panel_crops:
        result = render_panel_occurrence_crops(
            sheets,
            occurrences,
            source_dxfs,
            Path(args.out).resolve(),
            maximum=args.maximum,
        )
        _print(
            {
                "backend": "raw-panel-then-crop",
                "panel_count": result.get("panel_count", 0),
                "occurrence_count": result.get("rendered_count", 0),
                "skipped_entity_count": result.get("skipped_entity_count", 0),
                "issue_count": 0,
                "output": str(Path(args.out).resolve()),
            }
        )
        return 0
    if not args.raw_dxf:
        result = render_indexed_occurrences(
            sheets,
            entities,
            occurrences,
            Path(args.out).resolve(),
            maximum=args.maximum,
        )
        _print(
            {
                "backend": "indexed-matplotlib-agg",
                "occurrence_count": result.get("rendered_count", 0),
                "skipped_entity_count": result.get("skipped_entity_count", 0),
                "issue_count": 0,
                "output": str(Path(args.out).resolve()),
            }
        )
        return 0
    issues: list[RunIssue] = []
    result = _render_occurrences(
        occurrences,
        sheets,
        source_dxfs,
        Path(args.out).resolve(),
        issues,
        maximum=args.maximum,
    )
    write_json_atomic(
        Path(args.out).resolve() / "issues.json",
        [issue.model_dump(mode="json") for issue in issues],
    )
    _print(
        {
            "group_count": result.get("group_count", 0),
            "occurrence_count": result.get("occurrence_count", 0),
            "skipped_entity_count": result.get("skipped_entity_count", 0),
            "issue_count": len(issues),
            "output": str(Path(args.out).resolve()),
        }
    )
    return 0


def command_candidate_boards(args: argparse.Namespace) -> int:
    """Render numbered same-sheet/same-MT boards for explicit component review."""

    index_payload = _load_json(args.index)
    sheets, _ = _load_index(args.index)
    sheets, _ = _apply_panels(sheets, [], args.panels)
    occurrences = [MtOccurrence.model_validate(value) for value in _load_json(args.occurrences)]
    sources = index_payload.get("sources", []) if isinstance(index_payload, dict) else []
    source_dxfs = {
        str(source["source_file_id"]): Path(str(source["source_path"])).resolve()
        for source in sources
        if source.get("source_file_id") and source.get("source_path")
    }
    result = render_occurrence_candidate_boards(
        sheets,
        occurrences,
        source_dxfs,
        Path(args.out).resolve(),
        maximum_groups=args.maximum_groups,
        target_px=args.target_px,
        render_profile=args.render_profile,
    )
    _print(
        {
            "rendered_group_count": result.get("rendered_group_count", 0),
            "ambiguous_group_count": result.get("ambiguous_group_count", 0),
            "output": str(Path(args.out).resolve()),
        }
    )
    return 0


def command_evidence_quality(args: argparse.Namespace) -> int:
    """Reject missing, reused, blank, or unreadably embedded evidence images."""

    payload = _load_json(args.records)
    if isinstance(payload, list):
        records = payload
    elif isinstance(payload, dict):
        records = payload.get("records") or payload.get("comparison_rows") or payload.get("rows")
    else:
        records = None
    if not isinstance(records, list):
        raise ValueError("evidence-quality input must be a row list or contain records/rows")
    thresholds = _load_json(args.thresholds) if args.thresholds else None
    report = audit_evidence_quality(
        records,
        thresholds=thresholds,
        base_dir=args.base_dir or args.records.parent,
    )
    report.write_json(args.out)
    _print(
        {
            "status": report.status.value,
            **report.summary.model_dump(mode="json"),
            "output": str(args.out.resolve()),
        }
    )
    return 0 if report.status == ReviewStatus.PASS else 2


def command_image_match(args: argparse.Namespace) -> int:
    result = register_screenshot_to_panel(args.screenshot, args.panel)
    if args.out:
        write_registration(args.out, result)
    _print(result)
    return 0 if result["status"] in {"MATCH", "REVIEW"} else 2


def command_candidate_benchmark(args: argparse.Namespace) -> int:
    panel_payload = _load_json(args.panels)
    occurrences = [MtOccurrence.model_validate(value) for value in _load_json(args.occurrences)]
    takeoff_payload = _load_json(args.takeoff)
    gold_payload = _load_json(args.gold)
    material_mentions = (
        [MaterialMention.model_validate(value) for value in _load_json(args.material_mentions)]
        if args.material_mentions
        else []
    )
    vector_probe_payload = _load_json(args.vector_probes) if args.vector_probes else None
    evidence_payload = _load_json(args.evidence_index) if args.evidence_index else None
    gold_image_payload = (
        _load_json(args.gold_image_manifest) if args.gold_image_manifest else None
    )
    result = build_candidate_benchmark(
        panel_payload,
        occurrences,
        takeoff_payload,
        gold_payload,
        material_mentions=material_mentions,
        vector_probe_payload=vector_probe_payload,
        evidence_payload=evidence_payload,
        evidence_root=args.evidence_index.parent if args.evidence_index else None,
        gold_image_payload=gold_image_payload,
        gold_image_root=(
            args.gold_image_manifest.parent if args.gold_image_manifest else None
        ),
    )
    write_json_atomic(args.out, result)
    _print({**result["summary"], "output": str(args.out.resolve())})
    return 0


def command_evaluate(args: argparse.Namespace) -> int:
    predicted_payload = _load_json(args.predicted)
    gold_payload = _load_json(args.gold)
    predicted_rows, predicted_row_ids = _evaluation_rows(predicted_payload)
    gold_rows, gold_row_ids = _evaluation_rows(gold_payload)
    policy = _load_json(args.policy) if args.policy else None
    project_id = args.project_id or _evaluation_project_id(gold_payload, Path(args.gold).stem)
    report = evaluate_takeoff(
        [TakeoffItem.model_validate(value) for value in predicted_rows],
        [TakeoffItem.model_validate(value) for value in gold_rows],
        tolerance=args.tolerance,
        policy=policy,
        predicted_row_ids=predicted_row_ids,
        gold_row_ids=gold_row_ids,
        project_id=project_id,
    )
    if args.out:
        write_json_atomic(Path(args.out), report)
    # The detailed row-key lists can be very large on real projects. Keep the
    # terminal result readable while preserving the complete report on disk.
    summary: dict[str, Any] = {
        key: value
        for key, value in report.items()
        if key
        not in {
            "missing_keys",
            "unexpected_keys",
            "row_results",
            "projects",
            "policy",
            "duplicate_groups",
            "invalid_gold_rows",
        }
    }
    summary["project_summaries"] = [
        {
            key: value
            for key, value in project.items()
            if key
            not in {
                "row_results",
                "duplicate_groups",
                "invalid_gold_rows",
                "missing_rows",
                "unexpected_rows",
            }
        }
        for project in report["projects"]
    ]
    if args.out:
        summary["output"] = str(Path(args.out).resolve())
    _print(summary)
    return 0


def command_evaluate_batch(args: argparse.Namespace) -> int:
    manifest_path = Path(args.manifest).resolve()
    manifest = _evaluation_batch_manifest(manifest_path)
    manifest_base = manifest_path.parent
    output_dir = Path(args.out).resolve()
    _assert_batch_outputs_preserve_inputs(manifest_path, manifest, output_dir)
    projects_dir = output_dir / "projects"
    project_summaries: list[dict[str, Any]] = []
    for index, project in enumerate(manifest["projects"], start=1):
        project_id = project["project_id"]
        report_name = _batch_project_report_name(index, project_id)
        report_relative = (Path("projects") / report_name).as_posix()
        report_path = projects_dir / report_name
        inputs: dict[str, Any] = {
            "predicted": project["predicted"],
            "gold": project["gold"],
            "policy": project["policy"],
            "legacy_tolerance": project["tolerance"],
        }
        try:
            predicted_path = _manifest_file(
                manifest_base,
                project["predicted"],
                f"{project_id} predicted",
            )
            gold_path = _manifest_file(
                manifest_base,
                project["gold"],
                f"{project_id} gold",
            )
            policy_path = (
                _manifest_file(
                    manifest_base,
                    project["policy"],
                    f"{project_id} policy",
                )
                if project["policy"]
                else None
            )
            predicted_payload = _load_json(predicted_path)
            gold_payload = _load_json(gold_path)
            predicted_rows, predicted_row_ids = _evaluation_rows(predicted_payload)
            gold_rows, gold_row_ids = _evaluation_rows(gold_payload)
            policy = _load_json(policy_path) if policy_path else None
            report = evaluate_takeoff(
                [TakeoffItem.model_validate(value) for value in predicted_rows],
                [TakeoffItem.model_validate(value) for value in gold_rows],
                tolerance=project["tolerance"],
                policy=policy,
                predicted_row_ids=predicted_row_ids,
                gold_row_ids=gold_row_ids,
                project_id=project_id,
            )
            inputs.update(
                {
                    "predicted_sha256": sha256_file(predicted_path),
                    "gold_sha256": sha256_file(gold_path),
                    "policy_file_sha256": (sha256_file(policy_path) if policy_path else None),
                }
            )
            report["batch_context"] = {
                "batch_id": manifest["batch_id"],
                "manifest_sha256": sha256_file(manifest_path),
                "inputs": inputs,
            }
        except Exception as exc:
            report = {
                "evaluation_schema_version": "2.0",
                "project_id": project_id,
                "overall_gate": "BLOCKED",
                "meets_target": None,
                "policy_pending_fields": [],
                "batch_error": {
                    "type": type(exc).__name__,
                    "message": str(exc),
                },
                "batch_context": {
                    "batch_id": manifest["batch_id"],
                    "manifest_sha256": sha256_file(manifest_path),
                    "inputs": inputs,
                },
            }
        write_json_atomic(report_path, report)
        project_summaries.append(
            _batch_project_summary(
                report,
                project_id=project_id,
                report_path=report_relative,
                inputs=inputs,
            )
        )

    summary = summarize_evaluation_batch(
        project_summaries,
        batch_id=manifest["batch_id"],
        manifest_sha256=sha256_file(manifest_path),
    )
    summary["manifest_path"] = str(manifest_path)
    summary["outputs"] = {
        "summary_json": "summary.json",
        "summary_markdown": "summary.md",
        "projects_directory": "projects",
    }
    summary_json_path = output_dir / "summary.json"
    summary_markdown_path = output_dir / "summary.md"
    write_json_atomic(summary_json_path, summary)
    summary_markdown_path.write_text(
        evaluation_batch_markdown(summary),
        encoding="utf-8",
        newline="\n",
    )
    _print(
        {
            "batch_id": summary["batch_id"],
            "overall_gate": summary["overall_gate"],
            "project_count": summary["project_count"],
            "gate_counts": summary["gate_counts"],
            "aggregate": summary["aggregate"],
            "summary_json": str(summary_json_path),
            "summary_markdown": str(summary_markdown_path),
        }
    )
    return 0


def command_gold_import(args: argparse.Namespace) -> int:
    result = import_gold_workbook(args.workbook)
    result.write_json(args.out)
    image_manifest = None
    image_asset_count = 0
    image_issue_count = 0
    image_export_blocked = False
    if args.image_assets_dir:
        image_result = export_gold_image_assets(
            args.workbook,
            args.image_assets_dir,
            gold_result=result,
        )
        image_manifest = str(Path(args.image_assets_dir).resolve() / MANIFEST_NAME)
        image_asset_count = image_result.asset_count
        image_issue_count = len(image_result.issues)
        image_export_blocked = any(issue.severity == "BLOCK" for issue in image_result.issues)
    _print(
        {
            "row_count": result.summary.row_count,
            "review_count": result.summary.review_count,
            "block_count": result.summary.block_count,
            "issue_count": result.summary.audit_issue_count,
            "output": str(Path(args.out).resolve()),
            "image_asset_count": image_asset_count,
            "image_issue_count": image_issue_count,
            "image_manifest": image_manifest,
        }
    )
    return 0 if result.summary.row_count > 0 and not image_export_blocked else 2


def command_gold_image_export(args: argparse.Namespace) -> int:
    result = export_gold_image_assets(args.workbook, args.out)
    _print(
        {
            "asset_count": result.asset_count,
            "unique_file_count": result.unique_file_count,
            "issue_count": len(result.issues),
            "manifest": str(Path(args.out).resolve() / MANIFEST_NAME),
        }
    )
    return 2 if any(issue.severity == "BLOCK" for issue in result.issues) else 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    doctor = subparsers.add_parser("doctor", help="检查Python、DWG转换器和归档后端")
    doctor.set_defaults(handler=command_doctor)

    run = subparsers.add_parser("run", help="执行安全解包到报价输出的完整流程")
    run.add_argument("input", type=Path)
    run.add_argument("--out", type=Path, required=True)
    run.add_argument("--price-book", type=Path)
    run.add_argument("--confirmations", type=Path)
    run.add_argument("--quote-date", help="报价基准日期（YYYY-MM-DD；默认当天）")
    run.add_argument("--currency", default="CNY", help="价格币种（默认CNY）")
    run.add_argument("--tax-included", type=_parse_bool, help="要求价格含税口径true/false")
    run.add_argument(
        "--stainless-code-family",
        action="append",
        help="追加明确作为不锈钢处理的材料代号家族（可重复）",
    )
    run.add_argument(
        "--review-code-family",
        action="append",
        help="追加只进入低置信审核的材料代号家族（可重复）",
    )
    run.add_argument("--no-render", action="store_true", help="跳过MT局部证据图渲染")
    run.set_defaults(handler=command_run)

    resume = subparsers.add_parser("resume", help="复用已有索引和候选，重跑审核、计价和导出")
    resume.add_argument("run_dir", type=Path)
    resume.add_argument("--price-book", type=Path)
    resume.add_argument("--confirmations", type=Path)
    resume.add_argument("--quote-date", help="报价基准日期（YYYY-MM-DD；默认继承原运行）")
    resume.add_argument("--currency", help="价格币种（默认继承原运行）")
    resume.add_argument(
        "--tax-included",
        type=_parse_bool,
        help="要求价格含税口径true/false（默认继承原运行）",
    )
    resume.add_argument("--no-render", action="store_true", help="跳过MT局部证据图渲染")
    resume.set_defaults(handler=command_resume)

    ingest = subparsers.add_parser("ingest", help="安全复制或解包输入")
    ingest.add_argument("input", type=Path)
    ingest.add_argument("--out", type=Path, required=True)
    ingest.set_defaults(handler=command_ingest)

    convert = subparsers.add_parser("convert", help="批量转换DWG为DXF并审计")
    convert.add_argument("input", type=Path)
    convert.add_argument("--out", type=Path, required=True)
    convert.set_defaults(handler=command_convert)

    index = subparsers.add_parser("index", help="构建DXF JSON/SQLite语义索引")
    index.add_argument("dxf", type=Path, nargs="+")
    index.add_argument("--out", type=Path, required=True)
    index.set_defaults(handler=command_index)

    panels = subparsers.add_parser(
        "panels",
        help="从既有CAD索引重建纸空间视口面板（不重复转换）",
    )
    panels.add_argument("index", type=Path)
    panels.add_argument("--out", type=Path, required=True)
    panels.set_defaults(handler=command_panels)

    vector_probe = subparsers.add_parser(
        "vector-probe",
        help="按MT箭头局部读取原始多段线，输出仅供审核的重复实例数量候选",
    )
    vector_probe.add_argument("index", type=Path)
    vector_probe.add_argument("occurrences", type=Path)
    vector_probe.add_argument("--panels", type=Path)
    vector_probe.add_argument("--radius", type=float, default=1_500.0)
    vector_probe.add_argument("--geometry-tolerance", type=float, default=0.5)
    vector_probe.add_argument("--max-primitives", type=int, default=250_000)
    vector_probe.add_argument("--out", type=Path, required=True)
    vector_probe.set_defaults(handler=command_vector_probe)

    materials = subparsers.add_parser("materials", help="解析XLS/XLSX或DOCX材料表")
    materials.add_argument("workbook", type=Path)
    materials.add_argument(
        "--stainless-code-family",
        action="append",
        help="追加明确作为不锈钢处理的材料代号家族（可重复）",
    )
    materials.add_argument(
        "--review-code-family",
        action="append",
        help="追加只进入低置信审核的材料代号家族（可重复）",
    )
    materials.add_argument("--out", type=Path, required=True)
    materials.set_defaults(handler=command_materials)

    detect = subparsers.add_parser("detect-mt", help="从CAD索引检测MT标注")
    detect.add_argument("index", type=Path)
    detect.add_argument("--materials", type=Path)
    detect.add_argument("--panels", type=Path, help="使用视口面板并保留未覆盖Model实体")
    detect.add_argument(
        "--stainless-code-family",
        action="append",
        help="追加明确作为不锈钢处理的代号家族；例如GC-MT（可重复）",
    )
    detect.add_argument(
        "--review-code-family",
        action="append",
        help="追加只进入低置信审核的代号家族（可重复）",
    )
    detect.add_argument("--mentions-out", type=Path, help="输出无编号不锈钢描述诊断JSON")
    detect.add_argument("--out", type=Path, required=True)
    detect.set_defaults(handler=command_detect_mt)

    link = subparsers.add_parser("link", help="生成平面→立面→节点候选关系")
    link.add_argument("index", type=Path)
    link.add_argument("occurrences", type=Path)
    link.add_argument("--panels", type=Path)
    link.add_argument("--out", type=Path, required=True)
    link.add_argument("--promote-explicit", action="store_true")
    link.set_defaults(handler=command_link)

    takeoff = subparsers.add_parser("takeoff", help="组装构件并生成尺寸候选/算量草稿")
    takeoff.add_argument("index", type=Path)
    takeoff.add_argument("occurrences", type=Path)
    takeoff.add_argument("edges", type=Path)
    takeoff.add_argument("--panels", type=Path)
    takeoff.add_argument("--materials", type=Path)
    takeoff.add_argument("--confirmations", type=Path)
    takeoff.add_argument("--out", type=Path, required=True)
    takeoff.set_defaults(handler=command_takeoff)

    quote = subparsers.add_parser("quote", help="将takeoff JSON导出便携报价工作簿")
    quote.add_argument("takeoff", type=Path)
    quote.add_argument("--out", type=Path, required=True)
    quote.set_defaults(handler=command_quote)

    render = subparsers.add_parser("render", help="渲染DXF局部证据图")
    render.add_argument("dxf", type=Path)
    render.add_argument("--out", type=Path, required=True)
    render.add_argument("--layout", default="Model")
    source_group = render.add_mutually_exclusive_group(required=True)
    source_group.add_argument("--regions", type=Path)
    source_group.add_argument("--viewports", action="store_true")
    render.set_defaults(handler=command_render)

    render_occurrences = subparsers.add_parser(
        "render-occurrences",
        help="从既有索引和MT候选批量渲染局部证据（不导出工作簿）",
    )
    render_occurrences.add_argument("index", type=Path)
    render_occurrences.add_argument("occurrences", type=Path)
    render_occurrences.add_argument("--panels", type=Path)
    render_occurrences.add_argument("--out", type=Path, required=True)
    render_occurrences.add_argument("--maximum", type=int, default=250)
    render_occurrences.add_argument(
        "--raw-dxf",
        action="store_true",
        help="使用较慢的原始DXF渲染；默认使用含纸空间投影标注的快速索引渲染",
    )
    render_occurrences.add_argument(
        "--panel-crops",
        action="store_true",
        help="每个视口只渲染一次，再按MT引线批量裁证据图（推荐）",
    )
    render_occurrences.set_defaults(handler=command_render_occurrences)

    candidate_boards = subparsers.add_parser(
        "candidate-boards",
        help="将同页同MT候选编号成审核板，禁止默认取第一个候选",
    )
    candidate_boards.add_argument("index", type=Path)
    candidate_boards.add_argument("occurrences", type=Path)
    candidate_boards.add_argument("--panels", type=Path, required=True)
    candidate_boards.add_argument("--out", type=Path, required=True)
    candidate_boards.add_argument("--maximum-groups", type=int, default=200)
    candidate_boards.add_argument("--target-px", type=int, default=2_600)
    candidate_boards.add_argument(
        "--render-profile",
        choices=("white-fast", "cad-dark", "cad-dark-full"),
        default="cad-dark",
    )
    candidate_boards.set_defaults(handler=command_candidate_boards)

    evidence_quality = subparsers.add_parser(
        "evidence-quality",
        help="检查截图缺失、串项复用、同图冒充多阶段、空白率和显示缩放",
    )
    evidence_quality.add_argument("records", type=Path)
    evidence_quality.add_argument("--out", type=Path, required=True)
    evidence_quality.add_argument("--thresholds", type=Path)
    evidence_quality.add_argument("--base-dir", type=Path)
    evidence_quality.set_defaults(handler=command_evidence_quality)

    image_match = subparsers.add_parser(
        "image-match",
        help="将人工清单中的CAD截图配准到自动渲染面板",
    )
    image_match.add_argument("screenshot", type=Path)
    image_match.add_argument("panel", type=Path)
    image_match.add_argument("--out", type=Path)
    image_match.set_defaults(handler=command_image_match)

    candidate_benchmark = subparsers.add_parser(
        "candidate-benchmark",
        help="对人工候选金标准统计页码/代号/尺寸/数量候选召回，不冒充逐行准确率",
    )
    candidate_benchmark.add_argument("panels", type=Path)
    candidate_benchmark.add_argument("occurrences", type=Path)
    candidate_benchmark.add_argument("takeoff", type=Path)
    candidate_benchmark.add_argument("gold", type=Path)
    candidate_benchmark.add_argument("--evidence-index", type=Path)
    candidate_benchmark.add_argument("--material-mentions", type=Path)
    candidate_benchmark.add_argument(
        "--vector-probes",
        type=Path,
        help="加入原始DXF局部重复图形的REVIEW数量候选诊断",
    )
    candidate_benchmark.add_argument("--gold-image-manifest", type=Path)
    candidate_benchmark.add_argument("--out", type=Path, required=True)
    candidate_benchmark.set_defaults(handler=command_candidate_benchmark)

    evaluate = subparsers.add_parser("evaluate", help="比较预测算量与审核金标准")
    evaluate.add_argument("predicted", type=Path)
    evaluate.add_argument("gold", type=Path)
    evaluate.add_argument("--out", type=Path)
    evaluate.add_argument(
        "--policy",
        type=Path,
        help="版本化验收策略JSON；省略时长度/数量容差保持待确认，结果不能PASS",
    )
    evaluate.add_argument(
        "--project-id",
        help="本次项目标识；默认取gold元数据或文件名",
    )
    evaluate.add_argument(
        "--tolerance",
        type=float,
        default=1e-3,
        help="旧版工程量绝对精确指标容差（不影响新版95%% gate）",
    )
    evaluate.set_defaults(handler=command_evaluate)

    evaluate_batch = subparsers.add_parser(
        "evaluate-batch",
        help="按项目清单批量评测并输出逐项目JSON与JSON/Markdown汇总",
    )
    evaluate_batch.add_argument("manifest", type=Path, help="批量评测清单JSON")
    evaluate_batch.add_argument("--out", type=Path, required=True, help="批量评测输出目录")
    evaluate_batch.set_defaults(handler=command_evaluate_batch)

    gold = subparsers.add_parser("gold-import", help="导入并审计人工算量清单金标准")
    gold.add_argument("workbook", type=Path)
    gold.add_argument("--out", type=Path, required=True)
    gold.add_argument(
        "--image-assets-dir",
        type=Path,
        help="可选：只读导出 XLSX/XLSM 原始图片及确定性 manifest",
    )
    gold.set_defaults(handler=command_gold_import)

    gold_images = subparsers.add_parser(
        "gold-image-export",
        help="只读导出人工 XLSX/XLSM 的嵌入图片和 DISPIMG 原始资产",
    )
    gold_images.add_argument("workbook", type=Path)
    gold_images.add_argument("--out", type=Path, required=True, help="图片资产输出目录")
    gold_images.set_defaults(handler=command_gold_image_export)
    return parser


def main(argv: list[str] | None = None) -> int:
    # Proxy-object diagnostics are retained in the structured CAD index issues;
    # avoid flooding an interactive terminal with thousands of duplicate lines.
    logging.getLogger("ezdxf").setLevel(logging.ERROR)
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.handler(args))
    except KeyboardInterrupt:
        return 130
    except Exception as exc:
        _print({"status": "BLOCK", "error": f"{type(exc).__name__}: {exc}"})
        return 2


if __name__ == "__main__":
    sys.exit(main())
