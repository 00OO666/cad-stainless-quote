"""Generate bounded REVIEW candidates inside an already selected detail panel.

Selecting the correct detail *panel* is not sufficient when that viewport contains
several nodes or material callouts.  This helper groups material ATTRIBs with their
leaders, follows the leader arrow back into model geometry, and returns small,
auditable subview candidates.  It may rank candidates from textual/component
context, but it deliberately does not use dimension measurements as features and
never assigns a takeoff value.
"""

from __future__ import annotations

import hashlib
import math
import re
from collections import defaultdict
from collections.abc import Mapping, Sequence
from typing import Any

_TEXT_TYPES = frozenset({"TEXT", "MTEXT", "ATTRIB", "ATTDEF"})
_LEADER_TYPES = frozenset({"LEADER", "MLEADER", "MULTILEADER"})
_DIMENSION_TYPES = frozenset({"DIMENSION", "ARC_DIMENSION", "LARGE_RADIAL_DIMENSION"})
_FRAME_TYPES = frozenset({"LWPOLYLINE", "POLYLINE", "RECTANGLE"})
_MATERIAL_CODE_RE = re.compile(
    r"(?<![A-Z0-9])(?:[A-Z]{1,5}-){1,3}[A-Z0-9]{1,8}(?![A-Z0-9])",
    re.I,
)
_VIEW_REFERENCE_RE = re.compile(
    r"(?<![A-Z0-9])(?:[A-Z0-9]{1,5}-){1,4}\d{1,3}(?![A-Z0-9])",
    re.I,
)
_TITLE_WORDS = ("DETAIL", "NODE", "SECTION", "节点", "大样", "剖面", "详图")


def _finite(value: object) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def _bbox(value: object) -> tuple[float, float, float, float] | None:
    if (
        not isinstance(value, Sequence)
        or isinstance(value, (str, bytes, bytearray))
        or len(value) != 4
    ):
        return None
    values = tuple(_finite(part) for part in value)
    if any(part is None for part in values):
        return None
    result = tuple(float(part) for part in values)
    if result[2] <= result[0] or result[3] <= result[1]:
        return None
    return result


def _point(value: object) -> tuple[float, float] | None:
    if (
        not isinstance(value, Sequence)
        or isinstance(value, (str, bytes, bytearray))
        or len(value) < 2
    ):
        return None
    x_value, y_value = _finite(value[0]), _finite(value[1])
    if x_value is None or y_value is None:
        return None
    return x_value, y_value


def _center(value: tuple[float, float, float, float]) -> tuple[float, float]:
    return (value[0] + value[2]) / 2, (value[1] + value[3]) / 2


def _area(value: tuple[float, float, float, float]) -> float:
    return (value[2] - value[0]) * (value[3] - value[1])


def _intersects(
    left: tuple[float, float, float, float],
    right: tuple[float, float, float, float],
) -> bool:
    return not (
        left[2] < right[0] or left[0] > right[2] or left[3] < right[1] or left[1] > right[3]
    )


def _clip(
    value: tuple[float, float, float, float],
    boundary: tuple[float, float, float, float],
) -> tuple[float, float, float, float] | None:
    result = (
        max(value[0], boundary[0]),
        max(value[1], boundary[1]),
        min(value[2], boundary[2]),
        min(value[3], boundary[3]),
    )
    return result if result[2] > result[0] and result[3] > result[1] else None


def _entity_type(entity: Mapping[str, Any]) -> str:
    return str(entity.get("entity_type") or "").upper()


def _entity_id(entity: Mapping[str, Any]) -> str:
    return str(entity.get("id") or "")


def _entity_center(entity: Mapping[str, Any]) -> tuple[float, float] | None:
    box = _bbox(entity.get("bbox"))
    if box is not None:
        return _center(box)
    return _point(entity.get("insert"))


def _geometry(entity: Mapping[str, Any]) -> Mapping[str, Any]:
    value = entity.get("geometry")
    return value if isinstance(value, Mapping) else {}


def _leader_points(
    entity: Mapping[str, Any],
) -> tuple[tuple[float, float] | None, tuple[float, float] | None]:
    """Return ``(arrow, label)`` using explicit fields, then DXF vertex order."""

    geometry = _geometry(entity)
    arrow = next(
        (
            point
            for key in ("leader_target", "target", "arrowhead", "arrow_point", "arrow", "tip")
            if (point := _point(geometry.get(key))) is not None
        ),
        None,
    )
    label = next(
        (
            point
            for key in ("label_point", "landing", "landing_point", "text_location", "text_point")
            if (point := _point(geometry.get(key))) is not None
        ),
        None,
    )
    raw_vertices = geometry.get("vertices") or geometry.get("points")
    vertices: list[tuple[float, float]] = []
    if isinstance(raw_vertices, Sequence) and not isinstance(raw_vertices, (str, bytes, bytearray)):
        vertices = [point for raw in raw_vertices if (point := _point(raw)) is not None]
    if arrow is None and vertices:
        arrow = vertices[0]
    if label is None and vertices:
        label = vertices[-1]
    return arrow, label


def _codes(text: str) -> set[str]:
    return {value.upper().replace("_", "-") for value in _MATERIAL_CODE_RE.findall(text)}


def _normalise_text(value: object) -> str:
    return re.sub(r"\s+", "", str(value or "")).upper().replace("_", "-")


def _text_tokens(value: object) -> set[str]:
    text = _normalise_text(value)
    tokens = {token for token in re.split(r"[^0-9A-Z\u3400-\u9FFF]+", text) if token}
    chinese = "".join(character for character in text if "\u3400" <= character <= "\u9fff")
    tokens.update(chinese[index : index + 2] for index in range(max(0, len(chinese) - 1)))
    return tokens


def _bbox_distance(point: tuple[float, float], value: tuple[float, float, float, float]) -> float:
    dx = max(value[0] - point[0], 0.0, point[0] - value[2])
    dy = max(value[1] - point[1], 0.0, point[1] - value[3])
    return math.hypot(dx, dy)


def _annotation_groups(
    entities: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    grouped: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for entity in entities:
        if _entity_type(entity) not in _TEXT_TYPES:
            continue
        parent = _geometry(entity).get("parent_insert_handle")
        key = f"parent:{parent}" if parent else f"entity:{_entity_id(entity)}"
        grouped[key].append(entity)

    output: list[dict[str, Any]] = []
    for group_id, values in sorted(grouped.items()):
        texts = [str(value.get("text") or "") for value in values if value.get("text")]
        material_codes = sorted({code for text in texts for code in _codes(text)})
        boxes = [box for value in values if (box := _bbox(value.get("bbox"))) is not None]
        points = [point for value in values if (point := _entity_center(value)) is not None]
        if not points:
            continue
        if boxes:
            bbox = (
                min(value[0] for value in boxes),
                min(value[1] for value in boxes),
                max(value[2] for value in boxes),
                max(value[3] for value in boxes),
            )
        else:
            bbox = (points[0][0], points[0][1], points[0][0], points[0][1])
        output.append(
            {
                "group_id": group_id,
                "texts": texts,
                "material_codes": material_codes,
                "bbox": bbox,
                "entity_ids": sorted(filter(None, (_entity_id(value) for value in values))),
                "handles": sorted(
                    {str(value.get("handle")) for value in values if value.get("handle")}
                ),
            }
        )
    return output


def _local_bounds(
    panel_bbox: tuple[float, float, float, float],
    arrows: Sequence[tuple[float, float]],
    entities: Sequence[Mapping[str, Any]],
) -> tuple[tuple[float, float, float, float], list[Mapping[str, Any]]]:
    width = panel_bbox[2] - panel_bbox[0]
    height = panel_bbox[3] - panel_bbox[1]
    seed_box = (
        min(point[0] for point in arrows) - width * 0.18,
        min(point[1] for point in arrows) - height * 0.30,
        max(point[0] for point in arrows) + width * 0.18,
        max(point[1] for point in arrows) + height * 0.30,
    )
    accepted: list[Mapping[str, Any]] = []
    boxes: list[tuple[float, float, float, float]] = []
    for entity in entities:
        kind = _entity_type(entity)
        if kind not in _DIMENSION_TYPES | {"INSERT"} | _FRAME_TYPES:
            continue
        raw_box = _bbox(entity.get("bbox"))
        point = _entity_center(entity)
        if raw_box is not None:
            clipped = _clip(raw_box, panel_bbox)
            if clipped is None:
                continue
            # A page-spanning dimension/frame is context, not a local boundary.
            if clipped[2] - clipped[0] > width * 0.92 or clipped[3] - clipped[1] > height * 0.92:
                continue
            if not _intersects(clipped, seed_box):
                continue
            boxes.append(clipped)
        elif point is not None and (
            seed_box[0] <= point[0] <= seed_box[2] and seed_box[1] <= point[1] <= seed_box[3]
        ):
            boxes.append((point[0], point[1], point[0], point[1]))
        else:
            continue
        accepted.append(entity)

    xs = [point[0] for point in arrows]
    ys = [point[1] for point in arrows]
    for box in boxes:
        xs.extend((box[0], box[2]))
        ys.extend((box[1], box[3]))
    raw = (
        min(xs) - width * 0.06,
        min(ys) - height * 0.08,
        max(xs) + width * 0.06,
        max(ys) + height * 0.08,
    )
    minimum_width = width * 0.18
    minimum_height = height * 0.22
    center_x, center_y = _center(raw)
    raw = (
        min(raw[0], center_x - minimum_width / 2),
        min(raw[1], center_y - minimum_height / 2),
        max(raw[2], center_x + minimum_width / 2),
        max(raw[3], center_y + minimum_height / 2),
    )
    clipped = _clip(raw, panel_bbox) or panel_bbox
    return clipped, accepted


def _frame_candidates(
    panel_bbox: tuple[float, float, float, float],
    entities: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    panel_area = _area(panel_bbox)
    output: list[dict[str, Any]] = []
    for entity in entities:
        if _entity_type(entity) not in _FRAME_TYPES:
            continue
        geometry = _geometry(entity)
        if not bool(geometry.get("closed") or entity.get("closed")):
            continue
        value = _bbox(entity.get("bbox"))
        if value is None or (value := _clip(value, panel_bbox)) is None:
            continue
        ratio = _area(value) / panel_area
        if not 0.01 <= ratio <= 0.70:
            continue
        output.append(
            {
                "seed_kind": "closed_frame",
                "bbox": value,
                "entity_ids": [_entity_id(entity)] if _entity_id(entity) else [],
                "handles": [str(entity.get("handle"))] if entity.get("handle") else [],
                "material_codes": [],
                "texts": [],
                "leader_entity_ids": [],
                "leader_handles": [],
                "leader_targets": [],
                "local_entities": [entity],
            }
        )
    return output


def _dimension_records(entities: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for entity in entities:
        if _entity_type(entity) not in _DIMENSION_TYPES:
            continue
        geometry = _geometry(entity)
        records.append(
            {
                "entity_id": _entity_id(entity) or None,
                "handle": str(entity.get("handle")) if entity.get("handle") else None,
                "bbox": list(_bbox(entity.get("bbox")) or ()),
                # Measurements are exposed for review but never consulted by ranking.
                "value": entity.get("value"),
                "raw_measurement": geometry.get("raw_measurement"),
                "display_measurement": geometry.get("display_measurement"),
                "text_override": entity.get("text_override"),
            }
        )
    return records


def _candidate_score(
    candidate: Mapping[str, Any],
    panel: Mapping[str, Any],
    query: Mapping[str, Any],
) -> tuple[float, list[str], bool]:
    score = 0.0
    reasons: list[str] = []
    exact_semantic = False
    query_codes = {
        _normalise_text(value)
        for value in query.get("material_codes", [])
        if _normalise_text(value)
    }
    candidate_codes = {_normalise_text(value) for value in candidate.get("material_codes", [])}
    exact_codes = query_codes & candidate_codes
    if exact_codes:
        score += 4.0
        reasons.append("EXACT_MATERIAL_CODE")
    elif query_codes and any(
        left.rsplit("-", 1)[0] == right.rsplit("-", 1)[0]
        for left in query_codes
        for right in candidate_codes
    ):
        score += 1.0
        reasons.append("MATERIAL_CODE_FAMILY")

    candidate_text = " ".join(
        [str(panel.get("title") or ""), *map(str, candidate.get("texts", []))]
    )
    query_name_tokens = _text_tokens(query.get("component_name"))
    text_tokens = _text_tokens(candidate_text)
    overlap = query_name_tokens & text_tokens
    if overlap:
        score += min(3.0, 1.5 + 0.5 * len(overlap))
        reasons.append("COMPONENT_NAME_TEXT_OVERLAP")
        exact_semantic = True

    material_tokens = _text_tokens(query.get("material"))
    if material_tokens and material_tokens & text_tokens:
        score += 1.0
        reasons.append("MATERIAL_TEXT_OVERLAP")

    normalised_text = _normalise_text(candidate_text)
    view_references = {
        _normalise_text(value)
        for value in query.get("view_references", [])
        if _normalise_text(value)
    }
    matched_references = sorted(value for value in view_references if value in normalised_text)
    if matched_references:
        score += 3.0
        reasons.append("EXACT_VIEW_REFERENCE")
        exact_semantic = True

    candidate_ids = set(map(str, candidate.get("entity_ids", [])))
    reference_ids = set(map(str, query.get("reference_entity_ids", [])))
    if candidate_ids & reference_ids:
        score += 5.0
        reasons.append("EXACT_REFERENCE_ENTITY")
        exact_semantic = True

    if candidate.get("leader_entity_ids"):
        score += 0.8
        reasons.append("LEADER_BOUNDED")
    if candidate.get("seed_kind") == "closed_frame":
        score += 0.4
        reasons.append("CLOSED_FRAME_BOUNDED")
    dimension_count = len(candidate.get("dimensions", []))
    if dimension_count:
        score += min(0.8, 0.2 * dimension_count)
        reasons.append("DIMENSION_CONTEXT_PRESENT")
    return score, reasons, bool(exact_codes and exact_semantic)


def propose_detail_subviews(
    panel: Mapping[str, Any],
    entities: Sequence[Mapping[str, Any]],
    *,
    query: Mapping[str, Any] | None = None,
    top_k: int = 5,
) -> list[dict[str, Any]]:
    """Return blind-ranked, bounded subview candidates for one detail panel.

    ``query`` may contain ``component_name``, ``material``, ``material_codes``,
    ``view_references``, and ``reference_entity_ids``.  Gold dimensions, lengths,
    quantities, and areas are intentionally unsupported.  Candidate measurements
    are returned only as evidence for downstream review.
    """

    if top_k < 1:
        raise ValueError("top_k must be at least 1")
    panel_bbox = _bbox(panel.get("bbox"))
    if panel_bbox is None:
        return []
    query = query or {}
    panel_id = str(panel.get("id") or panel.get("panel_id") or "")
    panel_entities = [
        value
        for value in entities
        if isinstance(value, Mapping)
        and (not value.get("sheet_id") or str(value.get("sheet_id")) == panel_id)
    ]
    groups = _annotation_groups(panel_entities)
    leaders: list[dict[str, Any]] = []
    diagonal = math.hypot(panel_bbox[2] - panel_bbox[0], panel_bbox[3] - panel_bbox[1])
    for entity in panel_entities:
        if _entity_type(entity) not in _LEADER_TYPES:
            continue
        arrow, label = _leader_points(entity)
        if arrow is None:
            continue
        group = None
        if label is not None and groups:
            winner = min(groups, key=lambda value: _bbox_distance(label, value["bbox"]))
            if _bbox_distance(label, winner["bbox"]) <= max(diagonal * 0.22, 1.0):
                group = winner
        leaders.append({"entity": entity, "arrow": arrow, "label": label, "group": group})

    raw_candidates: list[dict[str, Any]] = []
    for leader in leaders:
        bounds, local_entities = _local_bounds(panel_bbox, [leader["arrow"]], panel_entities)
        group = leader["group"] or {}
        raw_candidates.append(
            {
                "seed_kind": "material_leader" if group.get("material_codes") else "leader",
                "bbox": bounds,
                "entity_ids": sorted(
                    {
                        _entity_id(leader["entity"]),
                        *map(str, group.get("entity_ids", [])),
                        *(_entity_id(value) for value in local_entities),
                    }
                    - {""}
                ),
                "handles": sorted(
                    {
                        str(leader["entity"].get("handle") or ""),
                        *map(str, group.get("handles", [])),
                        *(str(value.get("handle") or "") for value in local_entities),
                    }
                    - {""}
                ),
                "material_codes": list(group.get("material_codes", [])),
                "texts": list(group.get("texts", [])),
                "leader_entity_ids": [_entity_id(leader["entity"])],
                "leader_handles": [str(leader["entity"].get("handle") or "")],
                "leader_targets": [list(leader["arrow"])],
                "local_entities": local_entities,
            }
        )

    by_code: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for leader in leaders:
        for code in (leader["group"] or {}).get("material_codes", []):
            by_code[code].append(leader)
    for code, values in sorted(by_code.items()):
        if len(values) < 2:
            continue
        arrows = [value["arrow"] for value in values]
        spread_x = max(value[0] for value in arrows) - min(value[0] for value in arrows)
        spread_y = max(value[1] for value in arrows) - min(value[1] for value in arrows)
        if (
            spread_x > (panel_bbox[2] - panel_bbox[0]) * 0.60
            or spread_y > (panel_bbox[3] - panel_bbox[1]) * 0.65
        ):
            continue
        bounds, local_entities = _local_bounds(panel_bbox, arrows, panel_entities)
        groups_for_code = [value["group"] for value in values if value["group"]]
        raw_candidates.append(
            {
                "seed_kind": "same_material_leader_cluster",
                "bbox": bounds,
                "entity_ids": sorted(
                    {
                        *(_entity_id(value["entity"]) for value in values),
                        *(item for group in groups_for_code for item in group["entity_ids"]),
                        *(_entity_id(value) for value in local_entities),
                    }
                    - {""}
                ),
                "handles": sorted(
                    {
                        *(str(value["entity"].get("handle") or "") for value in values),
                        *(item for group in groups_for_code for item in group["handles"]),
                        *(str(value.get("handle") or "") for value in local_entities),
                    }
                    - {""}
                ),
                "material_codes": [code],
                "texts": sorted({item for group in groups_for_code for item in group["texts"]}),
                "leader_entity_ids": sorted(_entity_id(value["entity"]) for value in values),
                "leader_handles": sorted(
                    str(value["entity"].get("handle") or "") for value in values
                ),
                "leader_targets": [list(value) for value in arrows],
                "local_entities": local_entities,
            }
        )

    raw_candidates.extend(_frame_candidates(panel_bbox, panel_entities))
    deduplicated: dict[tuple[Any, ...], dict[str, Any]] = {}
    for candidate in raw_candidates:
        box = candidate["bbox"]
        candidate["dimensions"] = _dimension_records(candidate.pop("local_entities", []))
        key = (
            tuple(round(value, 6) for value in box),
            tuple(candidate.get("material_codes", [])),
            tuple(candidate.get("leader_entity_ids", [])),
        )
        deduplicated.setdefault(key, candidate)

    ranked: list[dict[str, Any]] = []
    for candidate in deduplicated.values():
        score, reasons, match_ready = _candidate_score(candidate, panel, query)
        basis = {
            "panel_id": panel_id,
            "bbox": [round(value, 6) for value in candidate["bbox"]],
            "leader_entity_ids": candidate.get("leader_entity_ids", []),
            "material_codes": candidate.get("material_codes", []),
        }
        digest = hashlib.sha256(repr(sorted(basis.items())).encode("utf-8")).hexdigest()[:24]
        ranked.append(
            {
                "candidate_id": f"detail-subview:{digest}",
                "panel_id": panel_id or None,
                "drawing_number": panel.get("drawing_number"),
                "panel_title": panel.get("title"),
                "seed_kind": candidate["seed_kind"],
                "bbox": [round(value, 6) for value in candidate["bbox"]],
                "material_codes": candidate.get("material_codes", []),
                "texts": candidate.get("texts", []),
                "leader_targets": candidate.get("leader_targets", []),
                "leader_entity_ids": candidate.get("leader_entity_ids", []),
                "leader_handles": candidate.get("leader_handles", []),
                "entity_ids": candidate.get("entity_ids", []),
                "handles": candidate.get("handles", []),
                "dimensions": candidate.get("dimensions", []),
                "score": round(score, 6),
                "score_reasons": reasons,
                "_match_ready": match_ready,
                "warning": (
                    "Subview boundary and dimensions are evidence candidates only; "
                    "no quantity or billable measurement has been assigned."
                ),
            }
        )

    ranked.sort(
        key=lambda value: (
            -float(value["score"]),
            -len(value["leader_entity_ids"]),
            -len(value["dimensions"]),
            float(value["bbox"][2]) - float(value["bbox"][0]),
            str(value["candidate_id"]),
        )
    )
    for index, candidate in enumerate(ranked):
        next_score = float(ranked[index + 1]["score"]) if index + 1 < len(ranked) else 0.0
        margin = float(candidate["score"]) - next_score
        candidate["rank"] = index + 1
        candidate["top1_top2_margin"] = round(margin, 6) if index == 0 else None
        if (
            index == 0
            and candidate.pop("_match_ready")
            and candidate["score"] >= 7.0
            and margin >= 1.5
        ):
            candidate["state"] = "MATCH"
        elif candidate["score"] >= 2.0 and (
            candidate["leader_entity_ids"] or candidate["score_reasons"]
        ):
            candidate["state"] = "REVIEW"
            candidate.pop("_match_ready", None)
        else:
            candidate["state"] = "UNRESOLVED"
            candidate.pop("_match_ready", None)
    return ranked[:top_k]


__all__ = ["propose_detail_subviews"]
