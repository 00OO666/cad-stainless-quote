"""Build a fail-closed plan -> elevation -> detail evidence manifest.

Candidate order is never a decision. A confirmed chain requires explicit,
stable candidate identities, two connected relation edges, verified evidence
images, reviewer attestation, and detail-sheet DIMENSION measurements.
"""

from __future__ import annotations

import hashlib
import math
import re
from collections import Counter, defaultdict
from collections.abc import Mapping, Sequence
from datetime import datetime
from pathlib import Path
from typing import Any

from PIL import Image

from .evidence_stages import canonical_stage_for_kind
from .io import sha256_file, write_json_atomic

STAGES = ("plan", "elevation", "detail")
_EXPLICIT_BASIS_PREFIXES = ("explicit_reference:", "view_reference:", "confirmed:")
_ENTITY_ID_RE = re.compile(r"(?:entity|panel_(?:paper_)?entity):[0-9a-f]+", re.I)
_ALLOWED_RENDER_PROFILES = {"cad-dark", "cad-dark-full"}
_COMPONENT_BINDING_CONFIRMATION_BLOCK_REASONS = {
    "LEGACY_SEQUENCE_BINDING_CANNOT_CONFIRM",
    "CONFIRMED_CHAIN_REQUIRES_COMPONENT_ID",
    "CONFIRMED_CHAIN_COMPONENT_ID_NOT_UNIQUE",
}


def _list(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return list(value)
    return [value]


def _stable_selection_key(selection: Mapping[str, Any]) -> str | None:
    for field in ("component_id", "gold_row_id", "row_id"):
        if selection.get(field) not in (None, ""):
            return str(selection[field])
    return None


def _selection_key(selection: Mapping[str, Any], index: int) -> str:
    stable = _stable_selection_key(selection)
    if stable is not None:
        return stable
    if selection.get("sequence") not in (None, ""):
        return str(selection["sequence"])
    return f"selection:{index}"


def _identity_value(value: Mapping[str, Any], fields: Sequence[str]) -> str:
    for field in fields:
        if value.get(field) not in (None, ""):
            return " ".join(str(value[field]).strip().casefold().split())
    return ""


def _sequence_identity_matches(
    selection: Mapping[str, Any], selected_record: Mapping[str, Any]
) -> bool:
    """Require sequence fallback to agree on every declared identity field."""

    comparisons = (
        (("name", "component_name"), ("name", "component_name")),
        (
            ("room_or_location", "room", "location", "plan_location"),
            ("room_or_location", "room", "location", "plan_location"),
        ),
        (("mt_code",), ("mt_code",)),
    )
    checked = 0
    for selection_fields, record_fields in comparisons:
        expected = _identity_value(selection, selection_fields)
        actual = _identity_value(selected_record, record_fields)
        if not expected:
            continue
        # Legacy selected-evidence records may not carry mt_code. Name and
        # location are part of the current schema and cannot silently vanish.
        if not actual and selection_fields == ("mt_code",):
            continue
        checked += 1
        if not actual or actual != expected:
            return False
    return checked > 0


def _bind_selected_record(
    selection: Mapping[str, Any],
    selected_by_key: Mapping[str, Sequence[Mapping[str, Any]]],
    selected_by_sequence: Mapping[str, Sequence[Mapping[str, Any]]],
) -> tuple[Mapping[str, Any] | None, str | None, str | None]:
    stable_key = _stable_selection_key(selection)
    if stable_key is not None:
        exact = list(selected_by_key.get(stable_key, ()))
        if len(exact) == 1:
            return exact[0], None, "stable_key"
        if len(exact) > 1:
            return None, "SELECTED_EVIDENCE_STABLE_KEY_AMBIGUOUS", None
        # A declared stable identity is authoritative. Falling back to a row
        # number here can silently bind a different physical component after
        # rows are inserted, deleted, or reordered.
        return None, "SELECTED_EVIDENCE_STABLE_KEY_NOT_FOUND", None

    sequence = selection.get("sequence")
    if sequence in (None, ""):
        return None, "SELECTED_EVIDENCE_BINDING_NOT_FOUND", None
    fallback = list(selected_by_sequence.get(str(sequence), ()))
    if not fallback:
        return None, "SELECTED_EVIDENCE_BINDING_NOT_FOUND", None
    if len(fallback) != 1:
        return None, "SELECTED_EVIDENCE_SEQUENCE_AMBIGUOUS", None
    if not _sequence_identity_matches(selection, fallback[0]):
        return None, "SELECTED_EVIDENCE_SEQUENCE_IDENTITY_MISMATCH", None
    return fallback[0], None, "sequence_fallback"


def _explicit_edge(edge: Mapping[str, Any]) -> bool:
    if str(edge.get("status") or "").upper() == "PASS":
        return True
    return any(
        str(basis).startswith(_EXPLICIT_BASIS_PREFIXES) for basis in _list(edge.get("basis"))
    )


def _reference_entity_ids(edge: Mapping[str, Any]) -> list[str]:
    output: set[str] = set()
    for basis in _list(edge.get("basis")):
        output.update(_ENTITY_ID_RE.findall(str(basis)))
    return sorted(output)


def _validate_bbox(value: Any, panel_bbox: Any) -> list[float] | None:
    if value is None:
        return None
    if (
        not isinstance(value, Sequence)
        or isinstance(value, (str, bytes, bytearray))
        or len(value) != 4
        or not isinstance(panel_bbox, Sequence)
        or len(panel_bbox) != 4
    ):
        raise ValueError("object_bbox and panel bbox must contain four coordinates")
    bbox = [float(part) for part in value]
    panel = [float(part) for part in panel_bbox]
    if not all(math.isfinite(part) for part in bbox + panel):
        raise ValueError("bbox coordinates must be finite")
    if bbox[2] <= bbox[0] or bbox[3] <= bbox[1]:
        raise ValueError("object_bbox must have positive area")
    if bbox[0] < panel[0] or bbox[1] < panel[1] or bbox[2] > panel[2] or bbox[3] > panel[3]:
        raise ValueError("object_bbox must stay inside its panel bbox")
    return bbox


def _stage_selection_map(selection: Mapping[str, Any]) -> dict[str, Mapping[str, Any]]:
    result: dict[str, Mapping[str, Any]] = {}
    raw = selection.get("stages") or selection.get("stage_selections")
    if isinstance(raw, Mapping):
        iterator = [
            dict(value, stage=key) for key, value in raw.items() if isinstance(value, Mapping)
        ]
    else:
        iterator = [value for value in _list(raw) if isinstance(value, Mapping)]
    for value in iterator:
        stage = str(value.get("stage") or "").casefold()
        if stage not in STAGES:
            raise ValueError(f"Unknown evidence stage: {stage or '<empty>'}")
        if stage in result:
            raise ValueError(f"Duplicate explicit stage selection: {stage}")
        result[stage] = value
    return result


def _resolve_image_path(value: Any, root: Path) -> str | None:
    if value in (None, ""):
        return None
    path = Path(str(value))
    if not path.is_absolute():
        path = root / path
    return str(path.resolve())


def _panel_candidate(
    *,
    stage: str,
    sheet: Mapping[str, Any],
    panel: Mapping[str, Any] | None,
    source: str,
    relation_edge: Mapping[str, Any] | None = None,
    occurrence_ids: Sequence[str] = (),
    context_image: str | None = None,
    closeup_image: str | None = None,
    context_sha256: str | None = None,
    closeup_sha256: str | None = None,
    object_bbox: Sequence[float] | None = None,
    measurement_ids: Sequence[str] = (),
) -> dict[str, Any]:
    edge_id = str(relation_edge.get("id")) if relation_edge else None
    normalized_occurrence_ids = sorted(set(str(value) for value in occurrence_ids if value))
    payload = "\0".join(
        (stage, str(sheet.get("id")), edge_id or "", ",".join(normalized_occurrence_ids))
    )
    candidate_id = f"stage-candidate:{hashlib.sha256(payload.encode()).hexdigest()[:20]}"
    panel_path = str(panel.get("absolute_path")) if panel and panel.get("absolute_path") else None
    return {
        "candidate_id": candidate_id,
        "stage": stage,
        "source": source,
        "sheet_id": sheet.get("id"),
        "sheet_kind": sheet.get("kind"),
        "drawing_number": sheet.get("drawing_number"),
        "title": sheet.get("title"),
        "occurrence_ids": normalized_occurrence_ids,
        "relation_edge_id": edge_id,
        "relation": relation_edge.get("relation") if relation_edge else None,
        "relation_confidence": relation_edge.get("confidence") if relation_edge else None,
        "relation_basis": list(relation_edge.get("basis", [])) if relation_edge else [],
        "reference_entity_ids": _reference_entity_ids(relation_edge or {}),
        "object_bbox": list(object_bbox) if object_bbox is not None else None,
        "measurement_ids": sorted(set(str(value) for value in measurement_ids if value)),
        "context_image": context_image or panel_path,
        "closeup_image": closeup_image,
        "context_sha256": context_sha256 or (panel.get("image_sha256") if panel else None),
        "closeup_sha256": closeup_sha256,
        "panel_bbox": list(sheet.get("bbox")) if sheet.get("bbox") is not None else None,
        "render_profile": panel.get("render_profile") if panel else None,
        "state": "CANDIDATE",
        "reason_codes": [],
    }


def _review_timestamp_valid(value: Any) -> bool:
    if not isinstance(value, str) or not value.strip():
        return False
    normalized = value.strip()
    if normalized.endswith(("Z", "z")):
        normalized = f"{normalized[:-1]}+00:00"
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        return False
    return parsed.tzinfo is not None and parsed.utcoffset() is not None


def _confirmed_image_errors(candidate: Mapping[str, Any]) -> tuple[list[str], dict[str, Any]]:
    reasons: list[str] = []
    details: dict[str, Any] = {}
    if str(candidate.get("render_profile") or "") not in _ALLOWED_RENDER_PROFILES:
        reasons.append("CONFIRMED_STAGE_RENDER_PROFILE_INVALID")

    missing_roles = [role for role in ("context", "closeup") if not candidate.get(f"{role}_image")]
    if missing_roles:
        reasons.append("CONFIRMED_STAGE_REQUIRES_CONTEXT_AND_CLOSEUP")
        details["missing_image_roles"] = missing_roles

    missing_files: list[str] = []
    invalid_files: list[str] = []
    missing_hashes: list[str] = []
    mismatched_hashes: list[str] = []
    for role in ("context", "closeup"):
        path_value = candidate.get(f"{role}_image")
        expected_hash = str(candidate.get(f"{role}_sha256") or "").casefold()
        if not path_value:
            continue
        path = Path(str(path_value))
        if not path.is_file():
            missing_files.append(role)
            continue
        try:
            with Image.open(path) as image:
                image.verify()
        except (OSError, ValueError):
            invalid_files.append(role)
            continue
        if not expected_hash:
            missing_hashes.append(role)
        elif sha256_file(path).casefold() != expected_hash:
            mismatched_hashes.append(role)
    if missing_files:
        reasons.append("CONFIRMED_STAGE_IMAGE_FILE_MISSING")
        details["missing_image_file_roles"] = missing_files
    if invalid_files:
        reasons.append("CONFIRMED_STAGE_IMAGE_INVALID")
        details["invalid_image_roles"] = invalid_files
    if missing_hashes:
        reasons.append("CONFIRMED_STAGE_IMAGE_SHA256_MISSING")
        details["missing_sha256_roles"] = missing_hashes
    if mismatched_hashes:
        reasons.append("CONFIRMED_STAGE_IMAGE_SHA256_MISMATCH")
        details["mismatched_sha256_roles"] = mismatched_hashes
    return reasons, details


def _add_reason(stage_payload: dict[str, Any], code: str) -> None:
    if code not in stage_payload["reason_codes"]:
        stage_payload["reason_codes"].append(code)


def _block_chosen(
    stage_payload: dict[str, Any], candidate: dict[str, Any], reasons: Sequence[str]
) -> None:
    candidate["state"] = "BLOCK"
    candidate["reason_codes"] = list(dict.fromkeys(str(value) for value in reasons))
    stage_payload["selected"] = [candidate]
    stage_payload["state"] = "BLOCK"
    for reason in reasons:
        _add_reason(stage_payload, str(reason))


def _block_selected_stage(stage_payload: dict[str, Any], reason: str) -> None:
    """Block a previously selected stage and keep candidate diagnostics aligned."""

    stage_payload["state"] = "BLOCK"
    _add_reason(stage_payload, reason)
    for candidate in stage_payload["selected"]:
        candidate["state"] = "BLOCK"
        candidate.setdefault("reason_codes", [])
        if reason not in candidate["reason_codes"]:
            candidate["reason_codes"].append(reason)


def _normalized_sha256(value: Any) -> str | None:
    normalized = str(value or "").strip().casefold()
    return normalized or None


def _block_confirmation_image_reuse(stage_map: Mapping[str, dict[str, Any]]) -> None:
    """Forbid one raster from proving two roles or drawing stages.

    There is deliberately no implicit shared-image exception. A future shared
    evidence rule must be explicit, versioned, and independently validated
    before it can relax this default-deny gate.
    """

    sha_uses: dict[str, list[tuple[str, str]]] = defaultdict(list)
    for stage in STAGES:
        for candidate in stage_map[stage]["selected"]:
            if not candidate.get("confirmation_requested"):
                continue
            context_sha = _normalized_sha256(candidate.get("context_sha256"))
            closeup_sha = _normalized_sha256(candidate.get("closeup_sha256"))
            if context_sha and closeup_sha and context_sha == closeup_sha:
                _block_selected_stage(stage_map[stage], "SAME_IMAGE_SHA_REUSED_WITHIN_STAGE_ROLES")
            if context_sha:
                sha_uses[context_sha].append((stage, "context"))
            if closeup_sha:
                sha_uses[closeup_sha].append((stage, "closeup"))

    for uses in sha_uses.values():
        reused_stages = sorted({stage for stage, _role in uses})
        if len(reused_stages) < 2:
            continue
        for stage in reused_stages:
            _block_selected_stage(stage_map[stage], "SAME_IMAGE_SHA_REUSED_ACROSS_STAGES")


def _connected_chain_error(
    stage_map: Mapping[str, Mapping[str, Any]],
    edges_by_id: Mapping[str, Sequence[Mapping[str, Any]]],
) -> str | None:
    chosen = {stage: stage_map[stage]["selected"][0] for stage in STAGES}
    plan_edge_id = str(chosen["plan"].get("relation_edge_id") or "")
    detail_edge_id = str(chosen["detail"].get("relation_edge_id") or "")
    plan_edges = list(edges_by_id.get(plan_edge_id, ()))
    detail_edges = list(edges_by_id.get(detail_edge_id, ()))
    if len(plan_edges) != 1 or len(detail_edges) != 1 or plan_edge_id == detail_edge_id:
        return "CONFIRMED_CHAIN_RELATION_EDGE_NOT_UNIQUE"
    plan_edge = plan_edges[0]
    detail_edge = detail_edges[0]
    plan_sheet = str(chosen["plan"].get("sheet_id") or "")
    elevation_sheet = str(chosen["elevation"].get("sheet_id") or "")
    detail_sheet = str(chosen["detail"].get("sheet_id") or "")
    if (
        plan_edge.get("relation") != "plan_to_elevation"
        or str(plan_edge.get("source_id") or "") != plan_sheet
        or str(plan_edge.get("target_id") or "") != elevation_sheet
        or detail_edge.get("relation") != "elevation_to_detail"
        or str(detail_edge.get("source_id") or "") != elevation_sheet
        or str(detail_edge.get("target_id") or "") != detail_sheet
    ):
        return "CONFIRMED_STAGE_CHAIN_DISCONNECTED"
    return None


def build_stage_evidence_manifest(
    panel_payload: Mapping[str, Any],
    relation_edges: Sequence[Mapping[str, Any]],
    selected_evidence: Mapping[str, Any],
    panel_catalog: Mapping[str, Any],
    selections: Sequence[Mapping[str, Any]],
    output_dir: Path | str,
    *,
    selected_evidence_root: Path | str | None = None,
) -> dict[str, Any]:
    """Assemble candidates and apply only explicit, auditable selections."""

    destination = Path(output_dir).resolve()
    destination.mkdir(parents=True, exist_ok=True)
    selected_root = (
        Path(selected_evidence_root).resolve()
        if selected_evidence_root is not None
        else destination
    )
    sheets = {
        str(sheet["id"]): sheet
        for sheet in panel_payload.get("sheets", [])
        if isinstance(sheet, Mapping) and sheet.get("id")
    }
    entities = {
        str(entity["id"]): entity
        for entity in panel_payload.get("entities", [])
        if isinstance(entity, Mapping) and entity.get("id")
    }
    catalog_panels = panel_catalog.get("panels", {})
    if not isinstance(catalog_panels, Mapping):
        raise ValueError("panel catalog must contain a panels mapping")

    component_id_counts = Counter(
        str(selection.get("component_id") or "").strip().casefold()
        for selection in selections
        if str(selection.get("component_id") or "").strip()
    )

    selected_by_key: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    selected_by_sequence: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for record in selected_evidence.get("records", []):
        if not isinstance(record, Mapping):
            continue
        if record.get("selection_key") not in (None, ""):
            selected_by_key[str(record["selection_key"])].append(record)
        if record.get("sequence") not in (None, ""):
            selected_by_sequence[str(record["sequence"])].append(record)

    incoming: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    outgoing: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    edges_by_id: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for edge in relation_edges:
        if not isinstance(edge, Mapping) or not edge.get("id"):
            continue
        edges_by_id[str(edge["id"])].append(edge)
        incoming[str(edge.get("target_id") or "")].append(edge)
        outgoing[str(edge.get("source_id") or "")].append(edge)

    records: list[dict[str, Any]] = []
    for index, selection in enumerate(selections, start=1):
        key = _selection_key(selection, index)
        component_id = str(selection.get("component_id") or "").strip() or None
        explicit_stages = _stage_selection_map(selection)
        confirmation_requested = any(
            str(value.get("state") or "").upper() == "CONFIRMED"
            for value in explicit_stages.values()
        )
        selected_record, binding_error, binding_mode = _bind_selected_record(
            selection, selected_by_key, selected_by_sequence
        )
        record_block_reasons = [binding_error] if binding_error else []
        record_notes: list[str] = []
        if binding_mode == "sequence_fallback":
            record_notes.append("SELECTED_EVIDENCE_BOUND_BY_LEGACY_SEQUENCE_REVIEW_ONLY")
            if confirmation_requested:
                record_block_reasons.append("LEGACY_SEQUENCE_BINDING_CANNOT_CONFIRM")
        if confirmation_requested and component_id is None:
            record_block_reasons.append("CONFIRMED_CHAIN_REQUIRES_COMPONENT_ID")
        elif confirmation_requested and component_id_counts.get(component_id.casefold(), 0) != 1:
            record_block_reasons.append("CONFIRMED_CHAIN_COMPONENT_ID_NOT_UNIQUE")
        stage_map: dict[str, dict[str, Any]] = {
            stage: {"state": "MISSING", "selected": [], "candidates": [], "reason_codes": []}
            for stage in STAGES
        }
        primary_sheet_ids: set[str] = set()
        if isinstance(selected_record, Mapping):
            for evidence in selected_record.get("evidence", []):
                if not isinstance(evidence, Mapping):
                    continue
                sheet_id = str(evidence.get("sheet_id") or "")
                sheet = sheets.get(sheet_id)
                if sheet is None:
                    continue
                stage = canonical_stage_for_kind(sheet.get("kind"))
                if stage is None:
                    record_notes.append("SELECTED_EVIDENCE_KIND_OUTSIDE_STAGE_MODEL")
                    continue
                # Only elevation occurrence evidence may anchor the two
                # cross-drawing relation candidate pools.
                if stage == "elevation":
                    primary_sheet_ids.add(sheet_id)
                candidate = _panel_candidate(
                    stage=stage,
                    sheet=sheet,
                    panel=catalog_panels.get(sheet_id),
                    source="selected_occurrence",
                    occurrence_ids=[
                        str(value) for value in evidence.get("selected_occurrence_ids", [])
                    ],
                    context_image=_resolve_image_path(evidence.get("locator_image"), selected_root),
                    closeup_image=_resolve_image_path(evidence.get("closeup_image"), selected_root),
                    context_sha256=evidence.get("locator_sha256"),
                    closeup_sha256=evidence.get("closeup_sha256"),
                    object_bbox=evidence.get("object_bbox"),
                )
                candidate["reason_codes"] = ["EXPLICIT_OCCURRENCE_NOT_FINAL_STAGE_CONFIRMATION"]
                stage_map[stage]["candidates"].append(candidate)

        for primary_sheet_id in sorted(primary_sheet_ids):
            for edge in sorted(
                incoming.get(primary_sheet_id, []), key=lambda value: str(value.get("id"))
            ):
                if edge.get("relation") != "plan_to_elevation" or not _explicit_edge(edge):
                    continue
                source_sheet = sheets.get(str(edge.get("source_id") or ""))
                if (
                    source_sheet is None
                    or canonical_stage_for_kind(source_sheet.get("kind")) != "plan"
                ):
                    continue
                stage_map["plan"]["candidates"].append(
                    _panel_candidate(
                        stage="plan",
                        sheet=source_sheet,
                        panel=catalog_panels.get(str(source_sheet["id"])),
                        source="relation_candidate",
                        relation_edge=edge,
                    )
                )
            for edge in sorted(
                outgoing.get(primary_sheet_id, []), key=lambda value: str(value.get("id"))
            ):
                if edge.get("relation") != "elevation_to_detail" or not _explicit_edge(edge):
                    continue
                target_sheet = sheets.get(str(edge.get("target_id") or ""))
                if (
                    target_sheet is None
                    or canonical_stage_for_kind(target_sheet.get("kind")) != "detail"
                ):
                    continue
                stage_map["detail"]["candidates"].append(
                    _panel_candidate(
                        stage="detail",
                        sheet=target_sheet,
                        panel=catalog_panels.get(str(target_sheet["id"])),
                        source="relation_candidate",
                        relation_edge=edge,
                    )
                )

        for stage in STAGES:
            unique: dict[str, dict[str, Any]] = {}
            for candidate in stage_map[stage]["candidates"]:
                unique[candidate["candidate_id"]] = candidate
            stage_map[stage]["candidates"] = sorted(
                unique.values(),
                key=lambda value: (
                    -(float(value.get("relation_confidence") or 0.0)),
                    str(value.get("drawing_number") or ""),
                    str(value["candidate_id"]),
                ),
            )

        for stage, explicit in explicit_stages.items():
            candidate_id = str(explicit.get("candidate_id") or "")
            edge_id = str(explicit.get("relation_edge_id") or "")
            if not candidate_id or (stage in {"plan", "detail"} and not edge_id):
                stage_map[stage]["state"] = "BLOCK"
                _add_reason(stage_map[stage], "EXPLICIT_STAGE_SELECTOR_INCOMPLETE")
                continue
            sheet_id = str(explicit.get("sheet_id") or "")
            matches = [
                candidate
                for candidate in stage_map[stage]["candidates"]
                if candidate["candidate_id"] == candidate_id
                and (not sheet_id or str(candidate.get("sheet_id")) == sheet_id)
                and (
                    stage == "elevation" or str(candidate.get("relation_edge_id") or "") == edge_id
                )
            ]
            if len(matches) != 1:
                stage_map[stage]["state"] = "BLOCK"
                _add_reason(
                    stage_map[stage], "EXPLICIT_STAGE_SELECTION_NOT_UNIQUE_IN_CANDIDATE_POOL"
                )
                continue
            chosen = dict(matches[0])
            for field in ("context_image", "closeup_image"):
                if field in explicit:
                    chosen[field] = _resolve_image_path(explicit.get(field), selected_root)
            for field in ("context_sha256", "closeup_sha256"):
                if field in explicit:
                    chosen[field] = explicit.get(field)
            try:
                chosen["object_bbox"] = _validate_bbox(
                    explicit.get("object_bbox", chosen.get("object_bbox")),
                    chosen.get("panel_bbox"),
                )
            except (TypeError, ValueError):
                _block_chosen(stage_map[stage], chosen, ["EXPLICIT_STAGE_OBJECT_BBOX_INVALID"])
                continue
            chosen["measurement_ids"] = sorted(
                set(str(value) for value in _list(explicit.get("measurement_ids")) if value)
            )
            wrong_sheet = [
                measurement_id
                for measurement_id in chosen["measurement_ids"]
                if str(entities.get(measurement_id, {}).get("sheet_id") or "")
                != str(chosen.get("sheet_id") or "")
            ]
            wrong_type = [
                measurement_id
                for measurement_id in chosen["measurement_ids"]
                if measurement_id not in wrong_sheet
                and str(entities.get(measurement_id, {}).get("entity_type") or "").upper()
                != "DIMENSION"
            ]
            measurement_errors: list[str] = []
            if wrong_sheet:
                measurement_errors.append("MEASUREMENT_OUTSIDE_SELECTED_STAGE_SHEET")
                chosen["mismatched_measurement_ids"] = wrong_sheet
            if wrong_type:
                measurement_errors.append("MEASUREMENT_ENTITY_TYPE_NOT_DIMENSION")
                chosen["non_dimension_measurement_ids"] = wrong_type
            if measurement_errors:
                _block_chosen(stage_map[stage], chosen, measurement_errors)
                continue

            requested_confirmation = str(explicit.get("state") or "").upper() == "CONFIRMED"
            review = explicit.get("review")
            confirmation_errors: list[str] = []
            if requested_confirmation and chosen["object_bbox"] is None:
                confirmation_errors.append("CONFIRMED_STAGE_REQUIRES_OBJECT_BBOX")
            if requested_confirmation and (
                not isinstance(review, Mapping)
                or any(
                    not str(review.get(field) or "").strip()
                    for field in ("reviewer", "reviewed_at", "reason")
                )
            ):
                confirmation_errors.append("CONFIRMED_STAGE_REQUIRES_REVIEW_METADATA")
            elif requested_confirmation and not _review_timestamp_valid(review.get("reviewed_at")):
                confirmation_errors.append("CONFIRMED_STAGE_REVIEWED_AT_INVALID")
            if requested_confirmation and stage == "detail" and not chosen["measurement_ids"]:
                confirmation_errors.append("CONFIRMED_DETAIL_REQUIRES_DIMENSION_MEASUREMENT")
            image_details: dict[str, Any] = {}
            if requested_confirmation:
                image_errors, image_details = _confirmed_image_errors(chosen)
                confirmation_errors.extend(image_errors)
            if confirmation_errors:
                chosen["confirmation_requested"] = requested_confirmation
                chosen.update(image_details)
                chosen["review"] = dict(review) if isinstance(review, Mapping) else None
                _block_chosen(stage_map[stage], chosen, confirmation_errors)
                continue
            chosen["review"] = dict(review) if isinstance(review, Mapping) else None
            chosen["confirmation_requested"] = requested_confirmation
            chosen["state"] = "CONFIRMED" if requested_confirmation else "CANDIDATE"
            chosen["reason_codes"] = (
                [] if chosen["state"] == "CONFIRMED" else ["STAGE_NOT_CONFIRMED"]
            )
            stage_map[stage]["selected"] = [chosen]
            stage_map[stage]["state"] = chosen["state"]

        selected_fingerprints: dict[tuple[Any, ...], str] = {}
        for stage in STAGES:
            for chosen in stage_map[stage]["selected"]:
                fingerprint = (
                    chosen.get("sheet_id"),
                    tuple(chosen.get("occurrence_ids") or ()),
                    tuple(chosen.get("object_bbox") or ()),
                )
                other_stage = selected_fingerprints.get(fingerprint)
                if other_stage is None:
                    selected_fingerprints[fingerprint] = stage
                    continue
                for value in (stage, other_stage):
                    _block_selected_stage(stage_map[value], "SAME_EVIDENCE_REUSED_ACROSS_STAGES")

        _block_confirmation_image_reuse(stage_map)

        component_binding_errors = [
            reason
            for reason in record_block_reasons
            if reason in _COMPONENT_BINDING_CONFIRMATION_BLOCK_REASONS
        ]
        for stage in STAGES:
            if not any(
                candidate.get("confirmation_requested")
                for candidate in stage_map[stage]["selected"]
            ):
                continue
            for reason in component_binding_errors:
                _block_selected_stage(stage_map[stage], reason)

        for stage in STAGES:
            if stage_map[stage]["state"] == "MISSING" and stage_map[stage]["candidates"]:
                stage_map[stage]["state"] = "REVIEW"
                _add_reason(stage_map[stage], "STAGE_CANDIDATES_REQUIRE_EXPLICIT_SELECTION")
            elif stage_map[stage]["state"] == "MISSING":
                _add_reason(stage_map[stage], "NO_STAGE_CANDIDATE")

        states = [stage_map[stage]["state"] for stage in STAGES]
        chain_error = None
        if all(state == "CONFIRMED" for state in states):
            chain_error = _connected_chain_error(stage_map, edges_by_id)
            if chain_error:
                for stage in ("plan", "detail"):
                    _block_selected_stage(stage_map[stage], chain_error)
                states = [stage_map[stage]["state"] for stage in STAGES]

        final_state = (
            "BLOCK"
            if record_block_reasons or any(state == "BLOCK" for state in states)
            else "CONFIRMED"
            if all(state == "CONFIRMED" for state in states)
            else "REVIEW"
        )
        reason_codes = list(dict.fromkeys(record_block_reasons + record_notes))
        if chain_error:
            reason_codes.append(chain_error)
        if final_state != "CONFIRMED":
            reason_codes.append("INCOMPLETE_PLAN_ELEVATION_DETAIL_CHAIN")
        records.append(
            {
                "selection_key": key,
                "sequence": selection.get("sequence", index),
                "component_id": component_id,
                "selected_evidence_binding": binding_mode,
                "name": selection.get("name") or selection.get("component_name"),
                "room_or_location": selection.get("room_or_location") or selection.get("location"),
                "mt_code": selection.get("mt_code"),
                "state": final_state,
                "reason_codes": list(dict.fromkeys(reason_codes)),
                "stages": stage_map,
            }
        )

    result = {
        "schema_version": "1.1",
        "purpose": "component_stage_evidence_review",
        "path_scope": "local_run_diagnostics",
        "warning": (
            "This local diagnostic manifest may contain absolute image paths and must not be "
            "published or committed. Candidate order is not a selection. Only explicit, "
            "connected, image-verified stage selections may be CONFIRMED."
        ),
        "required_stages": list(STAGES),
        "selection_count": len(selections),
        "confirmed_chain_count": sum(record["state"] == "CONFIRMED" for record in records),
        "review_chain_count": sum(record["state"] != "CONFIRMED" for record in records),
        "records": records,
    }
    write_json_atomic(destination / "stage_evidence.json", result)
    return result


__all__ = ["STAGES", "build_stage_evidence_manifest"]
