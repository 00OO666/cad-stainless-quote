"""Suggest review-only physical-component envelopes around selected MT leaders.

The geometry envelope improves screenshot framing but is deliberately not a
confirmation.  Continuous walls and floor lines can connect several physical
objects, so every suggested bbox carries a REVIEW state and cannot satisfy the
final evidence gate without an explicit reviewer confirmation.
"""

from __future__ import annotations

import math
from collections import defaultdict
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from .evidence_stages import canonical_stage_for_kind
from .io import write_json_atomic

ANNOTATION_TYPES = frozenset({"ATTRIB", "LEADER", "MTEXT", "TEXT", "VIEWPORT", "ACAD_TABLE"})
DIMENSION_TYPES = frozenset({"DIMENSION"})


def _list(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return list(value)
    return [value]


def _selection_key(selection: Mapping[str, Any], index: int) -> str:
    for field in ("component_id", "gold_row_id", "row_id", "sequence"):
        if selection.get(field) not in (None, ""):
            return str(selection[field])
    return f"selection:{index}"


def _box(value: Any) -> tuple[float, float, float, float] | None:
    if (
        not isinstance(value, Sequence)
        or isinstance(value, (str, bytes, bytearray))
        or len(value) != 4
    ):
        return None
    try:
        result = tuple(float(part) for part in value)
    except (TypeError, ValueError):
        return None
    if not all(math.isfinite(part) for part in result):
        return None
    if result[2] < result[0] or result[3] < result[1]:
        return None
    return result


def _intersects(left: Sequence[float], right: Sequence[float]) -> bool:
    return not (
        left[2] < right[0] or left[0] > right[2] or left[3] < right[1] or left[1] > right[3]
    )


def _inflate(
    bbox: Sequence[float],
    x_margin: float,
    y_margin: float,
    panel: Sequence[float],
) -> tuple[float, float, float, float]:
    return (
        max(float(panel[0]), float(bbox[0]) - x_margin),
        max(float(panel[1]), float(bbox[1]) - y_margin),
        min(float(panel[2]), float(bbox[2]) + x_margin),
        min(float(panel[3]), float(bbox[3]) + y_margin),
    )


def _union(boxes: Sequence[Sequence[float]]) -> tuple[float, float, float, float]:
    return (
        min(float(value[0]) for value in boxes),
        min(float(value[1]) for value in boxes),
        max(float(value[2]) for value in boxes),
        max(float(value[3]) for value in boxes),
    )


def _clip_box(
    bbox: Sequence[float], panel: Sequence[float]
) -> tuple[float, float, float, float] | None:
    """Clip intersecting evidence geometry to the rendered panel boundary."""

    clipped = (
        max(float(bbox[0]), float(panel[0])),
        max(float(bbox[1]), float(panel[1])),
        min(float(bbox[2]), float(panel[2])),
        min(float(bbox[3]), float(panel[3])),
    )
    return clipped if clipped[2] > clipped[0] and clipped[3] > clipped[1] else None


def _distance_to_box(point: Sequence[float], bbox: Sequence[float]) -> float:
    dx = max(float(bbox[0]) - point[0], 0.0, point[0] - float(bbox[2]))
    dy = max(float(bbox[1]) - point[1], 0.0, point[1] - float(bbox[3]))
    return math.hypot(dx, dy)


def _minimum_box(
    bbox: Sequence[float],
    point: Sequence[float],
    panel: Sequence[float],
    *,
    minimum_width_ratio: float = 0.12,
    minimum_height_ratio: float = 0.18,
) -> tuple[float, float, float, float]:
    panel_width = float(panel[2]) - float(panel[0])
    panel_height = float(panel[3]) - float(panel[1])
    width = max(float(bbox[2]) - float(bbox[0]), panel_width * minimum_width_ratio)
    height = max(float(bbox[3]) - float(bbox[1]), panel_height * minimum_height_ratio)
    center_x = min(max(float(point[0]), float(bbox[0])), float(bbox[2]))
    center_y = min(max(float(point[1]), float(bbox[1])), float(bbox[3]))
    return _inflate(
        (center_x - width / 2, center_y - height / 2, center_x + width / 2, center_y + height / 2),
        0,
        0,
        panel,
    )


def _suggest_bbox(
    panel_bbox: Sequence[float],
    points: Sequence[Sequence[float]],
    entities: Sequence[Mapping[str, Any]],
) -> tuple[list[float], list[list[float]], list[str]]:
    panel = tuple(float(value) for value in panel_bbox)
    panel_width = panel[2] - panel[0]
    panel_height = panel[3] - panel[1]
    center = (
        sum(float(point[0]) for point in points) / len(points),
        sum(float(point[1]) for point in points) / len(points),
    )
    search = _inflate(
        (center[0], center[1], center[0], center[1]),
        panel_width * 0.11,
        panel_height * 0.20,
        panel,
    )
    geometry: list[tuple[tuple[float, float, float, float], str]] = []
    dimensions: list[tuple[tuple[float, float, float, float], str]] = []
    for entity in entities:
        bbox = _box(entity.get("bbox"))
        if bbox is None or not _intersects(bbox, panel):
            continue
        entity_type = str(entity.get("entity_type") or "").upper()
        entity_id = str(entity.get("id") or "")
        if entity_type in DIMENSION_TYPES:
            dimensions.append((bbox, entity_id))
            continue
        if entity_type in ANNOTATION_TYPES:
            continue
        width_ratio = (bbox[2] - bbox[0]) / panel_width if panel_width else 1.0
        height_ratio = (bbox[3] - bbox[1]) / panel_height if panel_height else 1.0
        if width_ratio > 0.82 or height_ratio > 0.88:
            continue
        if _intersects(bbox, search):
            geometry.append((bbox, entity_id))

    seed_tolerance = max(panel_width, panel_height) * 0.018
    seeds = [
        value
        for value in geometry
        if min(_distance_to_box(point, value[0]) for point in points) <= seed_tolerance
    ]
    if not seeds and geometry:
        seeds = sorted(
            geometry,
            key=lambda value: min(_distance_to_box(point, value[0]) for point in points),
        )[: min(8, len(geometry))]
    if seeds:
        accepted = list(seeds)
        accepted_ids = {entity_id for _, entity_id in accepted}
        current = _union([bbox for bbox, _ in accepted])
        tolerance_x = panel_width * 0.006
        tolerance_y = panel_height * 0.009
        for _ in range(4):
            changed = False
            for bbox, entity_id in geometry:
                if entity_id in accepted_ids or not _intersects(
                    bbox, _inflate(current, tolerance_x, tolerance_y, panel)
                ):
                    continue
                proposed = _union([current, bbox])
                width_ratio = (proposed[2] - proposed[0]) / panel_width
                height_ratio = (proposed[3] - proposed[1]) / panel_height
                area_ratio = width_ratio * height_ratio
                if width_ratio > 0.48 or height_ratio > 0.82 or area_ratio > 0.34:
                    continue
                accepted.append((bbox, entity_id))
                accepted_ids.add(entity_id)
                current = proposed
                changed = True
            if not changed:
                break
        object_bbox = _inflate(current, panel_width * 0.025, panel_height * 0.04, panel)
        object_bbox = _minimum_box(object_bbox, center, panel)
        entity_ids = sorted(value for value in accepted_ids if value)
    else:
        object_bbox = _minimum_box(search, center, panel)
        entity_ids = []

    dimension_region = _inflate(object_bbox, panel_width * 0.08, panel_height * 0.10, panel)
    nearby_dimensions = sorted(
        (
            (min(_distance_to_box(point, bbox) for point in points), bbox, entity_id)
            for bbox, entity_id in dimensions
            if _intersects(bbox, dimension_region)
        ),
        key=lambda value: (value[0], value[2]),
    )[:8]
    clipped_dimensions = [
        (clipped, entity_id)
        for _, bbox, entity_id in nearby_dimensions
        if (clipped := _clip_box(bbox, panel)) is not None
    ]
    dimension_bboxes = [list(bbox) for bbox, _ in clipped_dimensions]
    entity_ids.extend(entity_id for _, entity_id in clipped_dimensions if entity_id)
    return [round(value, 6) for value in object_bbox], dimension_bboxes, sorted(set(entity_ids))


def suggest_component_frames(
    panel_payload: Mapping[str, Any],
    candidate_manifest: Mapping[str, Any],
    selections: Sequence[Mapping[str, Any]],
    output_dir: Path | str,
) -> dict[str, Any]:
    """Write bbox suggestions and an augmented selection file for rendering."""

    destination = Path(output_dir).resolve()
    destination.mkdir(parents=True, exist_ok=True)
    sheets = {
        str(sheet["id"]): sheet
        for sheet in panel_payload.get("sheets", [])
        if isinstance(sheet, Mapping) and sheet.get("id")
    }
    entities_by_sheet: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for entity in panel_payload.get("entities", []):
        if isinstance(entity, Mapping) and entity.get("sheet_id"):
            entities_by_sheet[str(entity["sheet_id"])].append(entity)
    groups = {
        str(group["group_id"]): group
        for group in candidate_manifest.get("groups", [])
        if isinstance(group, Mapping) and group.get("group_id")
    }

    records: list[dict[str, Any]] = []
    augmented: list[dict[str, Any]] = []
    for index, selection in enumerate(selections, start=1):
        key = _selection_key(selection, index)
        updated = dict(selection)
        object_bboxes: dict[str, list[float]] = {}
        dimension_bboxes: dict[str, list[list[float]]] = {}
        instance_object_bboxes: dict[str, list[dict[str, Any]]] = {}
        bbox_states: dict[str, str] = {}
        stages: dict[str, str] = {}
        selected_ids = {str(value) for value in _list(selection.get("selected_occurrence_ids"))}
        row_frames: list[dict[str, Any]] = []
        for group_id in [
            str(value)
            for value in _list(selection.get("group_id") or selection.get("group_ids"))
            if value
        ]:
            group = groups.get(group_id)
            if group is None:
                continue
            sheet_id = str(group.get("sheet_id") or "")
            sheet = sheets.get(sheet_id)
            if sheet is None or _box(sheet.get("bbox")) is None:
                continue
            point_records = [
                {
                    "occurrence_id": str(candidate.get("occurrence_id")),
                    "leader_target": candidate.get("leader_target"),
                }
                for candidate in group.get("candidates", [])
                if isinstance(candidate, Mapping)
                and str(candidate.get("occurrence_id")) in selected_ids
                and isinstance(candidate.get("leader_target"), Sequence)
            ]
            points = [value["leader_target"] for value in point_records]
            if not points:
                continue
            bbox, dimensions, entity_ids = _suggest_bbox(
                sheet["bbox"],
                points,
                entities_by_sheet.get(sheet_id, []),
            )
            object_bboxes[group_id] = bbox
            dimension_bboxes[group_id] = dimensions
            bbox_states[group_id] = "REVIEW"
            stages[group_id] = canonical_stage_for_kind(sheet.get("kind")) or "other"
            row_frames.append(
                {
                    "group_id": group_id,
                    "sheet_id": sheet_id,
                    "frame_role": "selection_aggregate",
                    "selected_occurrence_ids": sorted(
                        value["occurrence_id"] for value in point_records
                    ),
                    "object_bbox": bbox,
                    "dimension_bboxes": dimensions,
                    "entity_ids": entity_ids,
                    "state": "REVIEW",
                    "reason_codes": ["ALGORITHMIC_GEOMETRY_ENVELOPE_REQUIRES_CONFIRMATION"],
                }
            )
            # Preserve one local REVIEW proposal per selected leader as well as
            # the aggregate crop.  Multiple MT leaders may identify separate
            # physical instances, or several subparts of one assembly; the
            # geometry alone cannot decide which.  Exposing both levels avoids
            # averaging distant leaders into an empty middle crop while keeping
            # quantity and component identity explicitly unresolved.
            local_frames: list[dict[str, Any]] = []
            if len(point_records) > 1:
                for local_index, point_record in enumerate(point_records, start=1):
                    local_bbox, local_dimensions, local_entity_ids = _suggest_bbox(
                        sheet["bbox"],
                        [point_record["leader_target"]],
                        entities_by_sheet.get(sheet_id, []),
                    )
                    instance_id = f"{group_id}:leader:{local_index}"
                    local_frame = {
                        "instance_id": instance_id,
                        "group_id": group_id,
                        "sheet_id": sheet_id,
                        "frame_role": "occurrence_local",
                        "selected_occurrence_ids": [point_record["occurrence_id"]],
                        "leader_target": list(point_record["leader_target"]),
                        "object_bbox": local_bbox,
                        "dimension_bboxes": local_dimensions,
                        "entity_ids": local_entity_ids,
                        "state": "REVIEW",
                        "reason_codes": [
                            "LOCAL_LEADER_FRAME_DOES_NOT_PROVE_PHYSICAL_INSTANCE"
                        ],
                    }
                    local_frames.append(local_frame)
                    row_frames.append(local_frame)
            if local_frames:
                instance_object_bboxes[group_id] = [
                    {
                        "instance_id": value["instance_id"],
                        "selected_occurrence_ids": value["selected_occurrence_ids"],
                        "leader_target": value["leader_target"],
                        "object_bbox": value["object_bbox"],
                        "object_bbox_state": "REVIEW",
                        "reason_codes": value["reason_codes"],
                    }
                    for value in local_frames
                ]
        if object_bboxes:
            updated["object_bbox"] = object_bboxes
            updated["dimension_bboxes"] = dimension_bboxes
            updated["object_bbox_state"] = bbox_states
            updated["stage"] = stages
        if instance_object_bboxes:
            updated["instance_object_bboxes"] = instance_object_bboxes
        augmented.append(updated)
        records.append(
            {
                "selection_key": key,
                "sequence": selection.get("sequence", index),
                "state": "REVIEW" if row_frames else "MISSING",
                "reason_codes": (
                    ["ALGORITHMIC_GEOMETRY_ENVELOPE_REQUIRES_CONFIRMATION"]
                    if row_frames
                    else ["NO_FRAME_CANDIDATE"]
                ),
                "frames": row_frames,
            }
        )

    result = {
        "schema_version": "1.0",
        "purpose": "component_bbox_suggestion_review_only",
        "warning": (
            "Algorithmic envelopes improve screenshot framing but never confirm physical "
            "component identity or measurement roles."
        ),
        "selection_count": len(selections),
        "suggested_count": sum(bool(record["frames"]) for record in records),
        "records": records,
    }
    write_json_atomic(destination / "component_frames.json", result)
    write_json_atomic(
        destination / "selections_with_frame_candidates.json",
        {"schema_version": "1.0", "purpose": "review_render_input", "rows": augmented},
    )
    return result


__all__ = ["suggest_component_frames"]
