"""Adapt ranked stage candidates into bounded REVIEW-only analysis regions."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from .io import write_json_atomic

_STAGES = {"plan", "elevation", "detail"}


def _sequence(value: Any) -> list[Any]:
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return list(value)
    return []


def _bbox(value: Any) -> list[float] | None:
    values = _sequence(value)
    if len(values) != 4:
        return None
    try:
        result = [float(part) for part in values]
    except (TypeError, ValueError):
        return None
    if result[2] <= result[0] or result[3] <= result[1]:
        return None
    return result


def build_stage_candidate_regions(
    panel_payload: Mapping[str, Any],
    stage_payload: Mapping[str, Any],
    panel_catalog: Mapping[str, Any],
    output_path: Path | str,
    *,
    stage: str = "detail",
    maximum_per_selection: int = 3,
) -> dict[str, Any]:
    """Build a closeup-compatible manifest without selecting a stage candidate.

    Candidate ordering is consumed only as a bounded retrieval shortlist.  The
    resulting regions remain REVIEW and cannot satisfy a relation edge or
    measurement-role requirement on their own.
    """

    normalized_stage = str(stage).casefold()
    if normalized_stage not in _STAGES:
        raise ValueError(f"unknown evidence stage: {stage}")
    if maximum_per_selection < 1:
        raise ValueError("maximum_per_selection must be at least 1")

    sheets = {
        str(value["id"]): value
        for value in _sequence(panel_payload.get("sheets"))
        if isinstance(value, Mapping) and value.get("id")
    }
    catalog = panel_catalog.get("panels", {})
    if not isinstance(catalog, Mapping):
        raise ValueError("panel catalog must contain a panels mapping")

    records: list[dict[str, Any]] = []
    evidence_count = 0
    missing_count = 0
    truncated_count = 0
    for record_index, raw_record in enumerate(
        _sequence(stage_payload.get("records")), start=1
    ):
        if not isinstance(raw_record, Mapping):
            continue
        selection_key = str(
            raw_record.get("selection_key")
            or raw_record.get("component_id")
            or raw_record.get("sequence")
            or f"selection:{record_index}"
        )
        stages = raw_record.get("stages", {})
        stage_record = stages.get(normalized_stage, {}) if isinstance(stages, Mapping) else {}
        candidates = _sequence(
            stage_record.get("candidates") if isinstance(stage_record, Mapping) else []
        )
        unique_candidates: list[Mapping[str, Any]] = []
        seen_sheet_ids: set[str] = set()
        for candidate in candidates:
            if not isinstance(candidate, Mapping):
                continue
            sheet_id = str(candidate.get("sheet_id") or "")
            if not sheet_id or sheet_id in seen_sheet_ids:
                continue
            seen_sheet_ids.add(sheet_id)
            unique_candidates.append(candidate)
        if len(unique_candidates) > maximum_per_selection:
            truncated_count += 1
        chosen = unique_candidates[:maximum_per_selection]
        evidence: list[dict[str, Any]] = []
        reason_codes: list[str] = ["STAGE_CANDIDATE_SELECTION_REQUIRED"]
        for rank, candidate in enumerate(chosen, start=1):
            sheet_id = str(candidate.get("sheet_id") or "")
            sheet = sheets.get(sheet_id)
            panel = catalog.get(sheet_id)
            if not isinstance(sheet, Mapping) or not isinstance(panel, Mapping):
                missing_count += 1
                reason_codes.append("STAGE_CANDIDATE_PANEL_MISSING")
                continue
            render_bbox = _bbox(candidate.get("panel_bbox") or sheet.get("bbox"))
            image_path = candidate.get("context_image") or panel.get("absolute_path")
            if render_bbox is None or not image_path:
                missing_count += 1
                reason_codes.append("STAGE_CANDIDATE_REGION_OR_IMAGE_MISSING")
                continue
            evidence.append(
                {
                    "stage": normalized_stage,
                    "stage_candidate_rank": rank,
                    "stage_candidate_id": candidate.get("candidate_id"),
                    "candidate_source": candidate.get("source"),
                    "relation_edge_id": candidate.get("relation_edge_id"),
                    "relation_basis": list(_sequence(candidate.get("relation_basis"))),
                    "retrieval_rank_score": candidate.get("retrieval_rank_score"),
                    "component_semantic_support": candidate.get(
                        "component_semantic_support"
                    ),
                    "sheet_id": sheet_id,
                    "source_file_id": sheet.get("source_file_id"),
                    "drawing_number": sheet.get("drawing_number"),
                    "kind": sheet.get("kind"),
                    "requested_layout": sheet.get("layout"),
                    "layout": "Model",
                    "render_bbox": render_bbox,
                    "absolute_path": str(Path(str(image_path)).resolve()),
                    "source_image_sha256": panel.get("image_sha256"),
                    "state": "REVIEW",
                    "reason_codes": [
                        "STAGE_CANDIDATE_DOES_NOT_PROVE_COMPONENT_OR_MEASUREMENT_ROLE"
                    ],
                }
            )
            evidence_count += 1
        if not evidence:
            reason_codes.append("NO_USABLE_STAGE_CANDIDATE_REGION")
        records.append(
            {
                "selection_key": selection_key,
                "component_id": raw_record.get("component_id"),
                "sequence": raw_record.get("sequence", record_index),
                "name": raw_record.get("name"),
                "stage": normalized_stage,
                "state": "REVIEW" if evidence else "MISSING",
                "reason_codes": list(dict.fromkeys(reason_codes)),
                "evidence": evidence,
            }
        )

    result = {
        "schema_version": "1.0",
        "purpose": "ranked_stage_candidate_analysis_regions_review_only",
        "path_scope": "local_run_diagnostics",
        "warning": (
            "Candidate order only bounds analysis cost. These regions do not select a physical "
            "component, confirm a stage relation, assign a measurement role, or permit PASS."
        ),
        "stage": normalized_stage,
        "maximum_per_selection": maximum_per_selection,
        "selection_count": len(records),
        "evidence_count": evidence_count,
        "missing_count": missing_count,
        "truncated_selection_count": truncated_count,
        "records": records,
    }
    write_json_atomic(Path(output_path), result)
    return result


__all__ = ["build_stage_candidate_regions"]
