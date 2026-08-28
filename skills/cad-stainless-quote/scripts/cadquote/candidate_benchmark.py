"""Diagnostic comparison of CAD candidates against candidate human gold rows."""

from __future__ import annotations

import re
from collections import Counter, defaultdict
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from .linking import extract_reference_codes
from .models import (
    CadEntity,
    ComponentInstance,
    MaterialMention,
    MeasurementCandidate,
    MtOccurrence,
    Sheet,
)


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


def _business_text(value: Any) -> str:
    return re.sub(r"[\s,，。()（）/|｜:：;；._\-—–]+", "", str(value or "")).casefold()


_GENERIC_MATERIAL_LABELS = frozenset(
    {
        "不锈钢",
        "灰色不锈钢",
        "蓝色不锈钢",
        "不锈钢构件",
        "金属雕花",
        "木转印铝板",
        "玻璃",
        "镜子",
        "银镜",
    }
)
_GENERIC_MATERIAL_KEYS = frozenset(_business_text(value) for value in _GENERIC_MATERIAL_LABELS)
_NON_COMPONENT_LABEL_RE = re.compile(
    r"(?:版权(?:归|所有)|copyright|(?:^|\W)(?:tel|email|www)(?:\W|$)|https?://)",
    re.I,
)


def _is_physical_label(value: Any) -> bool:
    text = str(value or "").strip()
    if not text or _business_text(text) in _GENERIC_MATERIAL_KEYS:
        return False
    if _NON_COMPONENT_LABEL_RE.search(text):
        return False
    # Long address/title-block strings are a common false component hint.  A
    # real room name can contain ``室`` or ``区``; require both an address token
    # and a digit before rejecting it.
    if len(text) >= 8 and re.search(r"\d", text) and re.search(r"[省市区县路街道号座室]", text):
        return False
    return True


def _physical_label_candidates(
    component: ComponentInstance,
    item: Mapping[str, Any],
    component_sheets: Sequence[Sheet],
) -> list[str]:
    """Return only categorical component labels, never numeric takeoff facts.

    Material descriptions such as ``灰色不锈钢`` are not physical-component
    names.  A specific viewport title can still be useful (for example a named
    service-counter elevation), while a bare ``立面图`` cannot distinguish two
    components.
    """

    values: list[str] = []
    for raw in (component.name, component.room, item.get("name")):
        text = str(raw or "").strip()
        if _is_physical_label(text):
            values.append(text)
    for sheet in component_sheets:
        title = re.sub(
            r"\s*SCALE\s*[:：]?\s*1\s*[/：:]\s*\d+\s*$",
            "",
            sheet.title or "",
            flags=re.I,
        )
        title = title.strip()
        semantic_title = re.sub(
            r"(?:平面图|正立面图|背立面图|侧立面图|立面图|剖面图|节点图|大样图)$",
            "",
            title,
        ).strip()
        if semantic_title and semantic_title != title or (
            semantic_title
            and not re.fullmatch(r"(?:平面|立面|剖面|节点|大样)", semantic_title)
        ):
            if _is_physical_label(semantic_title):
                values.append(semantic_title)
    return list(dict.fromkeys(values))


def _categorical_name_score(gold_name: Any, label_candidates: Sequence[str]) -> int:
    """Score names without dimensions, quantities, or engineering amounts."""

    gold = _business_text(gold_name)
    if not gold:
        return 0
    normalized = [_business_text(value) for value in label_candidates if _business_text(value)]
    if gold in normalized:
        return 4
    for value in normalized:
        shorter, longer = sorted((gold, value), key=len)
        if len(shorter) >= 3 and shorter in longer and len(shorter) / len(longer) >= 0.6:
            return 2
    return 0


def _assign_components_one_to_one(
    comparison_rows: Sequence[dict[str, Any]],
    auto_rows: Sequence[dict[str, Any]],
) -> dict[str, Any]:
    """Attach conservative categorical assignments without reusing components.

    Matching uses only page/material identity and exact or strongly-contained
    physical labels.  It deliberately ignores width, length, quantity, and
    engineering quantity so a human answer cannot leak into prediction.
    """

    gold_buckets: dict[tuple[str, str], list[int]] = defaultdict(list)
    auto_buckets: dict[tuple[str, str], list[int]] = defaultdict(list)
    for index, row in enumerate(comparison_rows):
        bucket_key = (
            str(row.get("page") or ""),
            str(row.get("material_code") or ""),
        )
        gold_buckets[bucket_key].append(index)
    for index, row in enumerate(auto_rows):
        auto_buckets[
            (_page_code(row.get("page")), str(row.get("mt_code") or ""))
        ].append(index)

    assignments: dict[int, tuple[int, str]] = {}
    used_auto: set[int] = set()
    for bucket_key, gold_indices in sorted(gold_buckets.items()):
        auto_indices = auto_buckets.get(bucket_key, [])
        if len(gold_indices) == 1 and len(auto_indices) == 1:
            assignments[gold_indices[0]] = (auto_indices[0], "unique_page_material")
            used_auto.add(auto_indices[0])
            continue
        if not auto_indices:
            continue

        scores = {
            (gold_index, auto_index): _categorical_name_score(
                comparison_rows[gold_index].get("name"),
                auto_rows[auto_index].get("name_candidates", []),
            )
            for gold_index in gold_indices
            for auto_index in auto_indices
        }
        proposals: list[tuple[int, int]] = []
        for gold_index in gold_indices:
            ranked = sorted(
                ((scores[(gold_index, auto_index)], auto_index) for auto_index in auto_indices),
                reverse=True,
            )
            if not ranked or ranked[0][0] <= 0:
                continue
            if len(ranked) > 1 and ranked[0][0] == ranked[1][0]:
                continue
            proposals.append((gold_index, ranked[0][1]))
        for gold_index, auto_index in proposals:
            competing = [
                other_gold
                for other_gold in gold_indices
                if scores[(other_gold, auto_index)] == scores[(gold_index, auto_index)]
                and scores[(other_gold, auto_index)] > 0
            ]
            if competing == [gold_index] and auto_index not in used_auto:
                assignments[gold_index] = (auto_index, "unique_categorical_name")
                used_auto.add(auto_index)

        # Categorical elimination is safe only when the bucket cardinalities
        # already agree and every other member has an explicit unique match.
        remaining_gold = [value for value in gold_indices if value not in assignments]
        remaining_auto = [value for value in auto_indices if value not in used_auto]
        if (
            len(gold_indices) == len(auto_indices)
            and len(remaining_gold) == 1
            and len(remaining_auto) == 1
        ):
            assignments[remaining_gold[0]] = (remaining_auto[0], "categorical_elimination")
            used_auto.add(remaining_auto[0])

    matched_ids: list[str] = []
    status_counts: Counter[str] = Counter()
    for gold_index, row in enumerate(comparison_rows):
        bucket = (str(row.get("page") or ""), str(row.get("material_code") or ""))
        candidate_indices = auto_buckets.get(bucket, [])
        candidate_ids = [str(auto_rows[index]["component_id"]) for index in candidate_indices]
        if gold_index in assignments:
            auto_index, method = assignments[gold_index]
            component_id = str(auto_rows[auto_index]["component_id"])
            matched_ids.append(component_id)
            assignment = {
                "status": "MATCHED",
                "method": method,
                "matched_component_id": component_id,
                "candidate_component_ids": candidate_ids,
            }
        elif candidate_ids:
            assignment = {
                "status": "AMBIGUOUS",
                "method": None,
                "matched_component_id": None,
                "candidate_component_ids": candidate_ids,
            }
        else:
            assignment = {
                "status": "MISSING",
                "method": None,
                "matched_component_id": None,
                "candidate_component_ids": [],
            }
        row["component_assignment"] = assignment
        status_counts[assignment["status"]] += 1

    auto_components_in_gold_buckets = sum(
        len(auto_buckets.get(bucket_key, ())) for bucket_key in gold_buckets
    )
    overcomplete_bucket_component_count = sum(
        max(0, len(auto_buckets.get(bucket_key, ())) - len(gold_indices))
        for bucket_key, gold_indices in gold_buckets.items()
    )
    return {
        "matched_count": status_counts["MATCHED"],
        "ambiguous_count": status_counts["AMBIGUOUS"],
        "missing_count": status_counts["MISSING"],
        "extra_auto_component_count": len(auto_rows) - len(set(matched_ids)),
        "duplicate_component_assignment_count": len(matched_ids) - len(set(matched_ids)),
        "auto_components_in_gold_buckets": auto_components_in_gold_buckets,
        "auto_components_outside_gold_buckets": len(auto_rows)
        - auto_components_in_gold_buckets,
        "overcomplete_bucket_component_count": overcomplete_bucket_component_count,
    }


def _numeric_probe(
    expected: Any,
    values: Sequence[float],
    *,
    minimum_absolute_tolerance: float = 1.0,
) -> dict[str, Any]:
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
    hit = abs(closest - expected_value) <= max(
        minimum_absolute_tolerance,
        abs(expected_value) * 0.05,
    )
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
    material_mentions: Sequence[MaterialMention] = (),
    vector_probe_payload: Mapping[str, Any] | None = None,
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
        # A detail viewport can repeat the same material code and drawing number
        # as its parent elevation.  It is valid detail evidence, but it is not a
        # second physical line-item occurrence on that elevation page.
        if sheet and sheet.drawing_number and sheet.kind != "detail":
            occurrences_by_page_code[(_page_code(sheet.drawing_number), occurrence.mt_code)].append(
                occurrence
            )
    mentions_by_page: dict[str, list[MaterialMention]] = defaultdict(list)
    for mention in material_mentions:
        sheet = sheet_by_id.get(mention.sheet_id or "")
        if sheet and sheet.drawing_number:
            mentions_by_page[_page_code(sheet.drawing_number)].append(mention)
    vector_probe_by_occurrence: dict[str, Mapping[str, Any]] = {}
    for probe in (vector_probe_payload or {}).get("probes", []):
        if not isinstance(probe, Mapping):
            continue
        occurrence_id = probe.get("occurrence_id")
        quantity_values = [
            value
            for value in [
                probe.get("recommended_quantity"),
                *[
                    candidate.get("value")
                    for candidate in probe.get("quantity_candidates", [])
                    if isinstance(candidate, Mapping)
                ],
            ]
            if isinstance(value, (int, float))
        ]
        if isinstance(occurrence_id, str) and quantity_values:
            vector_probe_by_occurrence[occurrence_id] = probe
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
        gold_name = _business_text(item.get("name"))
        candidate_occurrences = sorted(
            occurrences_by_page_code.get((page, code), []), key=lambda value: value.id
        )
        uncoded_material_mentions = sorted(
            (
                mention
                for mention in mentions_by_page.get(page, ())
                if not code
                and gold_name
                and (
                    gold_name in _business_text(mention.raw_text)
                    or _business_text(mention.raw_text) in gold_name
                )
            ),
            key=lambda value: value.id,
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
        measurement_quantity_values = {
            measurement.numeric_value
            for measurement in component_measurements
            if measurement.role == "quantity"
        }
        vector_quantity_probes = [
            vector_probe_by_occurrence[occurrence.id]
            for occurrence in candidate_occurrences
            if occurrence.id in vector_probe_by_occurrence
        ]
        vector_quantity_values = {
            float(value)
            for probe in vector_quantity_probes
            for value in [
                probe.get("recommended_quantity"),
                *[
                    candidate.get("value")
                    for candidate in probe.get("quantity_candidates", [])
                    if isinstance(candidate, Mapping)
                ],
            ]
            if isinstance(value, (int, float))
        }
        quantity_values = sorted(
            {
                *measurement_quantity_values,
                *vector_quantity_values,
            }
        )
        quantity_probe = _numeric_probe(
            item.get("quantity"),
            quantity_values,
            minimum_absolute_tolerance=0.0,
        )
        leader_count = sum(value.leader_target is not None for value in candidate_occurrences)
        if not candidate_occurrences and uncoded_material_mentions:
            readiness = "UNCODED_MATERIAL_CANDIDATE"
        elif not candidate_occurrences:
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
                "uncoded_material_mention_count": len(uncoded_material_mentions),
                "uncoded_material_mention_ids": [
                    value.id for value in uncoded_material_mentions
                ],
                "vector_quantity_probe_count": len(vector_quantity_probes),
                "vector_quantity_occurrence_ids": sorted(
                    str(value["occurrence_id"]) for value in vector_quantity_probes
                ),
                "vector_quantity_values": sorted(vector_quantity_values),
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
        component_sheets: list[Sheet] = []
        component_sheet_ids: set[str] = set()
        for occurrence in component_occurrences:
            candidate_sheet = sheet_by_id.get(occurrence.sheet_id or "")
            if candidate_sheet is None or candidate_sheet.id in component_sheet_ids:
                continue
            component_sheets.append(candidate_sheet)
            component_sheet_ids.add(candidate_sheet.id)
        elevation_sheet = next(
            (
                sheet_by_id.get(occurrence.sheet_id or "")
                for occurrence in component_occurrences
                if sheet_by_id.get(occurrence.sheet_id or "") is not None
                and sheet_by_id[occurrence.sheet_id or ""].kind in {"elevation", "door"}
            ),
            None,
        )
        plan_sheet = next(
            (
                sheet_by_id.get(occurrence.sheet_id or "")
                for occurrence in component_occurrences
                if sheet_by_id.get(occurrence.sheet_id or "") is not None
                and sheet_by_id[occurrence.sheet_id or ""].kind
                in {"plan", "elevation_index", "ceiling", "floor"}
            ),
            None,
        )
        sheet = elevation_sheet or plan_sheet or (component_sheets[0] if component_sheets else None)
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
        name_candidates = _physical_label_candidates(component, item, component_sheets)
        evidence_paths = []
        for occurrence in component_occurrences:
            record = evidence_by_occurrence.get(occurrence.id, {})
            relative = record.get("file") if isinstance(record, Mapping) else None
            if relative and evidence_root is not None:
                evidence_paths.append(str((evidence_root / relative).resolve()))
        auto_rows.append(
            {
                "component_id": component.id,
                "name": next(iter(name_candidates), None),
                "name_candidates": name_candidates,
                "room": component.room,
                "mt_code": component.mt_code,
                "page": sheet.drawing_number if sheet else None,
                "plan_page": plan_sheet.drawing_number if plan_sheet else None,
                "elevation_page": elevation_sheet.drawing_number if elevation_sheet else None,
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

    assignment_summary = _assign_components_one_to_one(comparison_rows, auto_rows)
    readiness_counts = Counter(row["readiness"] for row in comparison_rows)
    summary = {
        "gold_row_count": len(comparison_rows),
        "auto_raw_row_count": len(auto_rows),
        "page_code_candidate_coverage_count": sum(
            row["candidate_occurrence_count"] > 0 for row in comparison_rows
        ),
        "uncoded_material_candidate_coverage_count": sum(
            row["uncoded_material_mention_count"] > 0 for row in comparison_rows
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
        "vector_quantity_candidate_coverage_count": sum(
            row["vector_quantity_probe_count"] > 0 for row in comparison_rows
        ),
        "readiness_distribution": dict(sorted(readiness_counts.items())),
        "categorical_one_to_one_assignment": assignment_summary,
        "warning": (
            "This report measures candidate retrieval only. It does not select gold-fitted "
            "values as AI predictions and must not be reported as 95% row accuracy."
        ),
    }
    return {
        "schema_version": "1.1",
        "summary": summary,
        "auto_rows": auto_rows,
        "comparison_rows": comparison_rows,
    }
