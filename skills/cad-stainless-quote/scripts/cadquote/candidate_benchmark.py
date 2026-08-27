"""Diagnostic comparison of CAD candidates against candidate human gold rows."""

from __future__ import annotations

import re
from collections import Counter, defaultdict
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from .linking import extract_reference_codes
from .models import CadEntity, ComponentInstance, MeasurementCandidate, MtOccurrence, Sheet


def _first_material_code(row: Mapping[str, Any]) -> str:
    raw = str(row.get("source_material_code") or row.get("item", {}).get("mt_code") or "")
    return next((value.strip() for value in raw.replace("\r", "").split("\n") if value.strip()), "")


def _page_code(value: Any) -> str:
    text = str(value or "").strip()
    references = sorted(
        extract_reference_codes(text),
        key=lambda code: (-code.count("-"), -len(code), code),
    )
    code = references[0] if references else text
    match = re.fullmatch(r"(?P<prefix>.*[-_])(?P<number>\d+)", code)
    if match is None:
        return code
    return f"{match.group('prefix')}{int(match.group('number')):02d}"


def _dimension_value(entity: CadEntity) -> float | None:
    raw = entity.geometry.get("display_measurement")
    if not isinstance(raw, (int, float)):
        raw = entity.value
    if not isinstance(raw, (int, float)) or raw <= 0:
        return None
    return float(raw)


def _numeric_probe(expected: Any, values: Sequence[float]) -> dict[str, Any]:
    if not isinstance(expected, (int, float)):
        return {"expected": expected, "hit": None, "closest": None, "relative_error": None}
    expected_value = float(expected)
    if not values:
        return {
            "expected": expected_value,
            "hit": False,
            "closest": None,
            "relative_error": None,
        }
    closest = min(values, key=lambda value: (abs(value - expected_value), value))
    error = abs(closest - expected_value) / abs(expected_value) if expected_value else None
    hit = abs(closest - expected_value) <= max(1.0, abs(expected_value) * 0.05)
    return {
        "expected": expected_value,
        "hit": hit,
        "closest": round(closest, 6),
        "relative_error": round(error, 8) if error is not None else None,
    }


def _auto_measurement(
    candidates: Sequence[MeasurementCandidate],
    role: str,
) -> MeasurementCandidate | None:
    values = [candidate for candidate in candidates if candidate.role == role]
    values.sort(
        key=lambda candidate: (
            -candidate.confidence,
            candidate.distance if candidate.distance is not None else float("inf"),
            candidate.numeric_value,
            candidate.id,
        )
    )
    if not values or values[0].confidence < 0.72:
        return None
    distinct = [
        candidate
        for candidate in values[1:]
        if abs(candidate.numeric_value - values[0].numeric_value) > 1e-6
    ]
    if distinct and values[0].confidence - distinct[0].confidence < 0.05:
        return None
    return values[0]


def build_candidate_benchmark(
    panel_payload: Mapping[str, Any],
    occurrences: Sequence[MtOccurrence],
    takeoff_payload: Mapping[str, Any],
    gold_payload: Mapping[str, Any],
    *,
    evidence_payload: Mapping[str, Any] | None = None,
    evidence_root: Path | None = None,
    gold_image_payload: Mapping[str, Any] | None = None,
    gold_image_root: Path | None = None,
) -> dict[str, Any]:
    """Measure candidate recall without treating gold values as predictions."""

    sheets = [Sheet.model_validate(value) for value in panel_payload.get("sheets", [])]
    entities = [CadEntity.model_validate(value) for value in panel_payload.get("entities", [])]
    components = [
        ComponentInstance.model_validate(value) for value in takeoff_payload.get("components", [])
    ]
    measurements = [
        MeasurementCandidate.model_validate(value)
        for value in takeoff_payload.get("measurements", [])
    ]
    items_by_component = {
        str(value.get("component_id")): value for value in takeoff_payload.get("items", [])
    }
    sheet_by_id = {sheet.id: sheet for sheet in sheets}
    occurrence_by_id = {occurrence.id: occurrence for occurrence in occurrences}
    occurrences_by_page_code: dict[tuple[str, str], list[MtOccurrence]] = defaultdict(list)
    for occurrence in occurrences:
        sheet = sheet_by_id.get(occurrence.sheet_id or "")
        if sheet and sheet.drawing_number:
            occurrences_by_page_code[(_page_code(sheet.drawing_number), occurrence.mt_code)].append(
                occurrence
            )
    component_by_occurrence: dict[str, list[str]] = defaultdict(list)
    for component in components:
        for occurrence_id in [
            *component.plan_occurrence_ids,
            *component.elevation_occurrence_ids,
        ]:
            component_by_occurrence[occurrence_id].append(component.id)
    measurements_by_component: dict[str, list[MeasurementCandidate]] = defaultdict(list)
    for measurement in measurements:
        measurements_by_component[measurement.component_id].append(measurement)
    dimensions_by_page: dict[str, list[float]] = defaultdict(list)
    for entity in entities:
        if entity.entity_type not in {"DIMENSION", "ARC_DIMENSION", "LARGE_RADIAL_DIMENSION"}:
            continue
        sheet = sheet_by_id.get(entity.sheet_id or "")
        value = _dimension_value(entity)
        if sheet and sheet.drawing_number and value is not None:
            dimensions_by_page[_page_code(sheet.drawing_number)].append(value)

    evidence_by_occurrence = (
        evidence_payload.get("occurrences", {}) if evidence_payload else {}
    )
    gold_images_by_row: dict[int, list[str]] = defaultdict(list)
    for asset in (gold_image_payload or {}).get("assets", []):
        try:
            row_number = int(asset.get("row"))
        except (TypeError, ValueError):
            continue
        relative = asset.get("export_relative_path")
        if relative and gold_image_root is not None:
            gold_images_by_row[row_number].append(str((gold_image_root / relative).resolve()))

    comparison_rows: list[dict[str, Any]] = []
    for wrapper in gold_payload.get("rows", []):
        item = wrapper.get("item", {})
        raw_location = str(item.get("plan_location") or "").strip()
        page = _page_code(raw_location)
        code = _first_material_code(wrapper)
        candidate_occurrences = sorted(
            occurrences_by_page_code.get((page, code), []), key=lambda value: value.id
        )
        component_ids = sorted(
            {
                component_id
                for occurrence in candidate_occurrences
                for component_id in component_by_occurrence.get(occurrence.id, ())
            }
        )
        component_measurements = [
            measurement
            for component_id in component_ids
            for measurement in measurements_by_component.get(component_id, ())
        ]
        page_values = sorted(set(round(value, 6) for value in dimensions_by_page.get(page, ())))
        component_values = sorted(
            {
                round(measurement.numeric_value, 6)
                for measurement in component_measurements
                if measurement.role in {"length", "height", "width", "unfolded_spec"}
            }
        )
        numeric_values = sorted(set([*page_values, *component_values]))
        width_probe = _numeric_probe(item.get("width_mm"), numeric_values)
        length_probe = _numeric_probe(item.get("length_mm"), numeric_values)
        quantity_values = sorted(
            {
                measurement.numeric_value
                for measurement in component_measurements
                if measurement.role == "quantity"
            }
        )
        quantity_probe = _numeric_probe(item.get("quantity"), quantity_values)
        leader_count = sum(value.leader_target is not None for value in candidate_occurrences)
        if not candidate_occurrences:
            readiness = "MISSING_PAGE_CODE_CANDIDATE"
        elif width_probe["hit"] and length_probe["hit"] and quantity_probe["hit"]:
            readiness = "FIELD_CANDIDATES_COMPLETE"
        elif width_probe["hit"] and length_probe["hit"]:
            readiness = "DIMENSION_CANDIDATES_COMPLETE"
        else:
            readiness = "PARTIAL_CANDIDATES"
        evidence_paths = []
        for occurrence in candidate_occurrences:
            record = evidence_by_occurrence.get(occurrence.id, {})
            relative = record.get("file") if isinstance(record, Mapping) else None
            if relative and evidence_root is not None:
                evidence_paths.append(str((evidence_root / relative).resolve()))
        comparison_rows.append(
            {
                "gold_row_id": wrapper.get("id"),
                "excel_row": wrapper.get("row"),
                "sequence": item.get("sequence"),
                "name": item.get("name"),
                "material_code": code,
                "page": page,
                "gold_location": raw_location,
                "gold_width_mm": item.get("width_mm"),
                "gold_length_mm": item.get("length_mm"),
                "gold_quantity": item.get("quantity"),
                "gold_engineering_quantity": item.get("engineering_quantity"),
                "candidate_occurrence_count": len(candidate_occurrences),
                "leader_backed_occurrence_count": leader_count,
                "candidate_component_count": len(component_ids),
                "candidate_occurrence_ids": [value.id for value in candidate_occurrences],
                "candidate_component_ids": component_ids,
                "width_probe": width_probe,
                "length_probe": length_probe,
                "quantity_probe": quantity_probe,
                "readiness": readiness,
                "ai_evidence_paths": list(dict.fromkeys(evidence_paths)),
                "human_evidence_paths": gold_images_by_row.get(int(wrapper.get("row") or 0), []),
            }
        )

    auto_rows: list[dict[str, Any]] = []
    for component in sorted(components, key=lambda value: value.id):
        occurrence_ids = [
            *component.plan_occurrence_ids,
            *component.elevation_occurrence_ids,
        ]
        component_occurrences = [
            occurrence_by_id[value]
            for value in occurrence_ids
            if value in occurrence_by_id
        ]
        sheet = next(
            (
                sheet_by_id.get(occurrence.sheet_id or "")
                for occurrence in component_occurrences
                if sheet_by_id.get(occurrence.sheet_id or "") is not None
            ),
            None,
        )
        candidates = measurements_by_component.get(component.id, [])
        auto_length = _auto_measurement(candidates, "length")
        auto_unfolded = _auto_measurement(candidates, "unfolded_spec")
        quantity_candidates = [
            value
            for value in candidates
            if value.role == "quantity"
            and value.numeric_value.is_integer()
            and value.confidence >= 0.60
        ]
        distinct_quantities = sorted({value.numeric_value for value in quantity_candidates})
        auto_quantity = distinct_quantities[0] if len(distinct_quantities) == 1 else None
        item = items_by_component.get(component.id, {})
        evidence_paths = []
        for occurrence in component_occurrences:
            record = evidence_by_occurrence.get(occurrence.id, {})
            relative = record.get("file") if isinstance(record, Mapping) else None
            if relative and evidence_root is not None:
                evidence_paths.append(str((evidence_root / relative).resolve()))
        auto_rows.append(
            {
                "component_id": component.id,
                "name": item.get("name") or component.name or "不锈钢构件",
                "mt_code": component.mt_code,
                "page": sheet.drawing_number if sheet else None,
                "sheet_kind": sheet.kind if sheet else None,
                "sheet_title": sheet.title if sheet else None,
                "unfolded_spec": auto_unfolded.raw_value if auto_unfolded else None,
                "width_mm": auto_unfolded.numeric_value if auto_unfolded else None,
                "length_mm": auto_length.numeric_value if auto_length else None,
                "quantity": auto_quantity,
                "engineering_quantity": None,
                "unit": None,
                "unit_price": None,
                "amount": None,
                "status": "REVIEW",
                "occurrence_ids": [value.id for value in component_occurrences],
                "evidence_paths": list(dict.fromkeys(evidence_paths)),
                "length_candidate_count": sum(value.role == "length" for value in candidates),
                "quantity_candidate_count": sum(value.role == "quantity" for value in candidates),
                "note": "尚未完成物理构件聚合和尺寸角色裁定，不进入报价合计",
            }
        )

    readiness_counts = Counter(row["readiness"] for row in comparison_rows)
    summary = {
        "gold_row_count": len(comparison_rows),
        "auto_raw_row_count": len(auto_rows),
        "page_code_candidate_coverage_count": sum(
            row["candidate_occurrence_count"] > 0 for row in comparison_rows
        ),
        "width_candidate_hit_count": sum(
            row["width_probe"]["hit"] is True for row in comparison_rows
        ),
        "length_candidate_hit_count": sum(
            row["length_probe"]["hit"] is True for row in comparison_rows
        ),
        "quantity_candidate_hit_count": sum(
            row["quantity_probe"]["hit"] is True for row in comparison_rows
        ),
        "readiness_distribution": dict(sorted(readiness_counts.items())),
        "warning": (
            "This report measures candidate retrieval only. It does not select gold-fitted "
            "values as AI predictions and must not be reported as 95% row accuracy."
        ),
    }
    return {
        "schema_version": "1.0",
        "summary": summary,
        "auto_rows": auto_rows,
        "comparison_rows": comparison_rows,
    }
