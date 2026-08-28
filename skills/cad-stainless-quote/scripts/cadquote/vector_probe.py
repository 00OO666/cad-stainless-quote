"""Fail-closed local vector probes for physical-instance review.

The semantic CAD index intentionally excludes most drawing linework.  Humans,
however, often derive quantity from repeated geometry rather than from a QTY
label.  This module revisits the immutable source DXF around an MT leader target
and reports congruent, independent polyline instances.  A result is always a
REVIEW candidate: repeated linework alone is not proof of billable quantity.
"""

from __future__ import annotations

import hashlib
import json
import math
from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import ezdxf
from ezdxf import bbox as ezbbox

from .models import MtOccurrence, Sheet

_SUPPORTED_VECTOR_TYPES = frozenset({"INSERT", "LINE", "LWPOLYLINE", "POLYLINE"})
_DEFAULT_RADIUS = 1_500.0
_DEFAULT_GEOMETRY_TOLERANCE = 0.5
_MAX_RECOMMENDED_INSTANCES = 20
_MAX_QUANTITY_CANDIDATES = 6

_ANNOTATION_TOKENS = (
    "annotation",
    "axis",
    "axle",
    "blip",
    "callout",
    "dimension",
    "index",
    "label",
    "leader",
    "sign",
    "text",
    "tag",
    "标注",
    "文字",
    "索引",
    "轴号",
)
_NON_OBJECT_LAYER_TOKENS = (
    "annotation",
    "axis",
    "axle",
    "dimension",
    "hatch",
    "index",
    "text",
    "标注",
    "填充",
    "文字",
    "索引",
    "轴号",
)


def _finite(value: object) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _clean_points(
    values: Sequence[tuple[float, float]],
    tolerance: float,
) -> tuple[tuple[float, float], ...]:
    output: list[tuple[float, float]] = []
    for point in values:
        if not output or math.dist(point, output[-1]) > tolerance:
            output.append(point)
    return tuple(output)


def _polyline_points(entity: Any) -> tuple[tuple[float, float], ...] | None:
    """Return straight 2D vertices; curved/bulged polylines are withheld."""

    try:
        if entity.dxftype() == "LWPOLYLINE":
            raw = list(entity.get_points("xyb"))
            if any(abs(float(value[2])) > 1e-9 for value in raw):
                return None
            points = [(float(value[0]), float(value[1])) for value in raw]
        elif entity.dxftype() == "POLYLINE":
            if not bool(getattr(entity, "is_2d_polyline", False)):
                return None
            vertices = list(entity.vertices)
            if any(abs(float(vertex.dxf.get("bulge", 0.0) or 0.0)) > 1e-9 for vertex in vertices):
                return None
            points = [
                (float(vertex.dxf.location.x), float(vertex.dxf.location.y))
                for vertex in vertices
            ]
        else:
            return None
    except (AttributeError, TypeError, ValueError, ezdxf.DXFError):
        return None
    return tuple(points) if len(points) >= 2 else None


def _line_points(entity: Any) -> tuple[tuple[float, float], ...] | None:
    try:
        start = entity.dxf.start
        end = entity.dxf.end
        return ((float(start.x), float(start.y)), (float(end.x), float(end.y)))
    except (AttributeError, TypeError, ValueError, ezdxf.DXFError):
        return None


def _is_annotation(value: str) -> bool:
    normalized = value.casefold().replace("_", " ").replace("-", " ")
    return any(token in normalized for token in _ANNOTATION_TOKENS)


def _is_object_layer(value: str) -> bool:
    normalized = value.casefold().replace("_", " ").replace("-", " ")
    return not any(token in normalized for token in _NON_OBJECT_LAYER_TOKENS)


def _canonical_deltas(
    points: Sequence[tuple[float, float]],
    *,
    closed: bool,
    tolerance: float,
) -> tuple[tuple[int, int], ...]:
    pairs = list(zip(points, points[1:], strict=False))
    if closed:
        pairs.append((points[-1], points[0]))
    deltas = tuple(
        (
            round((right[0] - left[0]) / tolerance),
            round((right[1] - left[1]) / tolerance),
        )
        for left, right in pairs
    )
    reverse = tuple((-x, -y) for x, y in reversed(deltas))
    if not closed:
        return min(deltas, reverse)

    def rotations(value: tuple[tuple[int, int], ...]) -> list[tuple[tuple[int, int], ...]]:
        return [value[index:] + value[:index] for index in range(len(value))]

    return min([*rotations(deltas), *rotations(reverse)])


def _bbox(points: Sequence[tuple[float, float]]) -> tuple[float, float, float, float]:
    xs = [point[0] for point in points]
    ys = [point[1] for point in points]
    return min(xs), min(ys), max(xs), max(ys)


def _bbox_distance(
    value: tuple[float, float, float, float],
    point: tuple[float, float],
) -> float:
    dx = max(value[0] - point[0], 0.0, point[0] - value[2])
    dy = max(value[1] - point[1], 0.0, point[1] - value[3])
    return math.hypot(dx, dy)


def _bbox_intersects(
    left: tuple[float, float, float, float],
    right: tuple[float, float, float, float],
) -> bool:
    return not (
        left[2] < right[0]
        or left[0] > right[2]
        or left[3] < right[1]
        or left[1] > right[3]
    )


def _segment_distance(
    point: tuple[float, float],
    left: tuple[float, float],
    right: tuple[float, float],
) -> float:
    dx = right[0] - left[0]
    dy = right[1] - left[1]
    denominator = dx * dx + dy * dy
    if denominator <= 1e-18:
        return math.dist(point, left)
    ratio = ((point[0] - left[0]) * dx + (point[1] - left[1]) * dy) / denominator
    ratio = min(1.0, max(0.0, ratio))
    projected = left[0] + ratio * dx, left[1] + ratio * dy
    return math.dist(point, projected)


@dataclass(frozen=True, slots=True)
class _VectorPrimitive:
    handle: str
    layer: str
    points: tuple[tuple[float, float], ...]
    closed: bool
    bbox: tuple[float, float, float, float]
    length: float
    signature: str
    family: str = "POLYLINE"
    multiplicity: int = 1

    def distance_to(self, point: tuple[float, float]) -> float:
        pairs = list(zip(self.points, self.points[1:], strict=False))
        if self.closed:
            pairs.append((self.points[-1], self.points[0]))
        return min(_segment_distance(point, left, right) for left, right in pairs)

    def anchor_distance(self, point: tuple[float, float]) -> float:
        """Distance to an object envelope, with interior points treated as anchored."""

        return _bbox_distance(self.bbox, point)


def _primitive(
    entity: Any,
    *,
    geometry_tolerance: float,
) -> _VectorPrimitive | None:
    raw_points = (
        _line_points(entity) if entity.dxftype() == "LINE" else _polyline_points(entity)
    )
    if raw_points is None:
        return None
    points = _clean_points(raw_points, geometry_tolerance * 0.01)
    if len(points) < 2:
        return None
    closed = bool(getattr(entity, "closed", False)) if entity.dxftype() != "LINE" else False
    pairs = list(zip(points, points[1:], strict=False))
    if closed:
        pairs.append((points[-1], points[0]))
    length = sum(math.dist(left, right) for left, right in pairs)
    if not math.isfinite(length) or length <= geometry_tolerance:
        return None
    layer = str(entity.dxf.get("layer", "") or "").strip()
    handle = str(entity.dxf.get("handle", "") or "").strip()
    if not handle:
        return None
    signature_basis = {
        "family": entity.dxftype(),
        "layer": layer.casefold(),
        "closed": closed,
        "deltas": _canonical_deltas(
            points,
            closed=closed,
            tolerance=geometry_tolerance,
        ),
    }
    canonical = json.dumps(signature_basis, ensure_ascii=False, sort_keys=True)
    signature = hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:24]
    return _VectorPrimitive(
        handle=handle,
        layer=layer,
        points=points,
        closed=closed,
        bbox=_bbox(points),
        length=length,
        signature=signature,
        family=entity.dxftype(),
    )


def _insert_primitive(
    entity: Any,
    *,
    geometry_tolerance: float,
    cache: ezbbox.Cache,
) -> _VectorPrimitive | None:
    """Return one auditable INSERT instance without exploding nested contents."""

    try:
        name = str(entity.dxf.get("name", "") or "").strip()
        layer = str(entity.dxf.get("layer", "") or "").strip()
        handle = str(entity.dxf.get("handle", "") or "").strip()
        if not name or not handle or _is_annotation(name) or _is_annotation(layer):
            return None
        extents = ezbbox.extents([entity], fast=True, cache=cache)
        if not extents.has_data:
            return None
        bounds = (
            float(extents.extmin.x),
            float(extents.extmin.y),
            float(extents.extmax.x),
            float(extents.extmax.y),
        )
        width = bounds[2] - bounds[0]
        height = bounds[3] - bounds[1]
        if not all(math.isfinite(value) for value in bounds) or max(width, height) <= 0:
            return None
        xscale = abs(float(entity.dxf.get("xscale", 1.0) or 1.0))
        yscale = abs(float(entity.dxf.get("yscale", 1.0) or 1.0))
        rotation = float(entity.dxf.get("rotation", 0.0) or 0.0) % 180.0
        row_count = max(1, int(entity.dxf.get("row_count", 1) or 1))
        column_count = max(1, int(entity.dxf.get("column_count", 1) or 1))
    except (AttributeError, TypeError, ValueError, ezdxf.DXFError):
        return None
    points = (
        (bounds[0], bounds[1]),
        (bounds[2], bounds[1]),
        (bounds[2], bounds[3]),
        (bounds[0], bounds[3]),
    )
    signature_basis = {
        "family": "INSERT",
        "block": name.casefold(),
        "layer": layer.casefold(),
        "xscale": round(xscale, 6),
        "yscale": round(yscale, 6),
        "rotation_mod_180": round(rotation, 4),
    }
    canonical = json.dumps(signature_basis, ensure_ascii=False, sort_keys=True)
    signature = hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:24]
    return _VectorPrimitive(
        handle=handle,
        layer=layer,
        points=points,
        closed=True,
        bbox=bounds,
        length=2 * (width + height),
        signature=signature,
        family="INSERT",
        multiplicity=row_count * column_count,
    )


def _layout(document: Any, layout_name: str) -> Any:
    if layout_name.casefold() == "model":
        return document.modelspace()
    try:
        return document.layouts.get(layout_name)
    except Exception as exc:
        raise ValueError(f"DXF layout does not exist: {layout_name}") from exc


def _extract_primitives(
    dxf_path: Path,
    layout_name: str,
    *,
    geometry_tolerance: float,
    max_primitives: int,
) -> tuple[list[_VectorPrimitive], bool, int]:
    document = ezdxf.readfile(dxf_path)
    primitives: list[_VectorPrimitive] = []
    supported_seen = 0
    truncated = False
    bbox_cache = ezbbox.Cache()
    for entity in _layout(document, layout_name):
        if entity.dxftype() not in _SUPPORTED_VECTOR_TYPES:
            continue
        supported_seen += 1
        if len(primitives) >= max_primitives:
            truncated = True
            break
        value = (
            _insert_primitive(
                entity,
                geometry_tolerance=geometry_tolerance,
                cache=bbox_cache,
            )
            if entity.dxftype() == "INSERT"
            else _primitive(entity, geometry_tolerance=geometry_tolerance)
        )
        if value is not None:
            primitives.append(value)
    return primitives, truncated, supported_seen


def _bbox_union(
    values: Sequence[tuple[float, float, float, float]],
) -> tuple[float, float, float, float]:
    return (
        min(value[0] for value in values),
        min(value[1] for value in values),
        max(value[2] for value in values),
        max(value[3] for value in values),
    )


def _primitive_segments(
    primitive: _VectorPrimitive,
) -> list[tuple[tuple[float, float], tuple[float, float]]]:
    values = list(zip(primitive.points, primitive.points[1:], strict=False))
    if primitive.closed:
        values.append((primitive.points[-1], primitive.points[0]))
    return values


@dataclass(frozen=True, slots=True)
class _ConnectedInstance:
    signature: str
    handles: tuple[str, ...]
    layer: str
    bbox: tuple[float, float, float, float]
    segment_count: int
    length: float


def _connected_instances(
    primitives: Sequence[_VectorPrimitive],
    *,
    geometry_tolerance: float,
    radius: float,
) -> list[_ConnectedInstance]:
    """Create translation-invariant instances from endpoint-connected linework."""

    values = [
        value
        for value in primitives
        if value.family != "INSERT"
        and _is_object_layer(value.layer)
        and value.length <= radius * 4
    ]
    if len(values) < 4:
        return []
    connection_tolerance = max(1.0, geometry_tolerance * 4)
    parents = list(range(len(values)))

    def find(index: int) -> int:
        while parents[index] != index:
            parents[index] = parents[parents[index]]
            index = parents[index]
        return index

    def union(left: int, right: int) -> None:
        left_root = find(left)
        right_root = find(right)
        if left_root != right_root:
            parents[right_root] = left_root

    endpoint_buckets: dict[tuple[str, int, int], list[int]] = defaultdict(list)
    for index, primitive in enumerate(values):
        for point in primitive.points:
            bucket_x = round(point[0] / connection_tolerance)
            bucket_y = round(point[1] / connection_tolerance)
            layer = primitive.layer.casefold()
            for dx in (-1, 0, 1):
                for dy in (-1, 0, 1):
                    for other in endpoint_buckets.get((layer, bucket_x + dx, bucket_y + dy), ()):
                        if any(
                            math.dist(point, other_point) <= connection_tolerance
                            for other_point in values[other].points
                        ):
                            union(index, other)
            endpoint_buckets[(layer, bucket_x, bucket_y)].append(index)

    groups: dict[int, list[_VectorPrimitive]] = defaultdict(list)
    for index, primitive in enumerate(values):
        groups[find(index)].append(primitive)

    output: list[_ConnectedInstance] = []
    signature_tolerance = max(geometry_tolerance, 0.1)
    for members in groups.values():
        if len(members) < 2 or len(members) > 80:
            continue
        bounds = _bbox_union([member.bbox for member in members])
        width = bounds[2] - bounds[0]
        height = bounds[3] - bounds[1]
        if max(width, height) <= signature_tolerance or max(width, height) > radius * 3:
            continue
        segments: list[tuple[tuple[int, int], tuple[int, int]]] = []
        total_length = 0.0
        for member in members:
            total_length += member.length
            for left, right in _primitive_segments(member):
                normalized_left = (
                    round((left[0] - bounds[0]) / signature_tolerance),
                    round((left[1] - bounds[1]) / signature_tolerance),
                )
                normalized_right = (
                    round((right[0] - bounds[0]) / signature_tolerance),
                    round((right[1] - bounds[1]) / signature_tolerance),
                )
                segments.append(tuple(sorted((normalized_left, normalized_right))))
        signature_basis = {
            "family": "CONNECTED_GRAPH",
            "layer": members[0].layer.casefold(),
            "segments": sorted(segments),
        }
        canonical = json.dumps(signature_basis, ensure_ascii=False, sort_keys=True)
        output.append(
            _ConnectedInstance(
                signature=hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:24],
                handles=tuple(sorted(member.handle for member in members)),
                layer=members[0].layer,
                bbox=bounds,
                segment_count=len(segments),
                length=total_length,
            )
        )
    return output


def _candidate_sort_key(value: Mapping[str, Any]) -> tuple[float, float, int, str]:
    method_priority = {
        "repeated_insert": 0,
        "panel_repeated_insert": 0,
        "repeated_connected_graph": 1,
        "repeated_polyline": 2,
        "panel_repeated_polyline": 2,
        "single_insert": 3,
        "single_vector_instance": 4,
    }
    return (
        float(value.get("anchor_distance_drawing_units") or 0.0),
        float(method_priority.get(str(value.get("method")), 9)),
        int(value.get("value") or 0),
        str(value.get("signature") or ""),
    )


def _probe_target(
    primitives: Sequence[_VectorPrimitive],
    *,
    occurrence: MtOccurrence,
    target: tuple[float, float],
    radius: float,
    geometry_tolerance: float,
    source_truncated: bool,
    context_bbox: tuple[float, float, float, float] | None = None,
) -> dict[str, Any]:
    local = [
        value
        for value in primitives
        if _bbox_distance(value.bbox, target) <= radius
        and max(value.bbox[2] - value.bbox[0], value.bbox[3] - value.bbox[1])
        <= radius * 6
    ]
    panel_context = (
        [
            value
            for value in primitives
            if _bbox_intersects(value.bbox, context_bbox)
            and max(value.bbox[2] - value.bbox[0], value.bbox[3] - value.bbox[1])
            <= radius * 6
        ]
        if context_bbox is not None
        else local
    )
    groups: dict[str, list[_VectorPrimitive]] = defaultdict(list)
    for primitive in local:
        groups[primitive.signature].append(primitive)
    anchor_tolerance = max(10.0, min(120.0, radius * 0.08))
    context_tolerance = max(anchor_tolerance, radius * 0.85)
    repeated: list[dict[str, Any]] = []
    recommended_groups: list[dict[str, Any]] = []
    quantity_candidates: list[dict[str, Any]] = []
    for signature, values in sorted(groups.items()):
        unique = {value.handle: value for value in values}
        instances = sorted(unique.values(), key=lambda value: value.handle)
        instance_count = sum(value.multiplicity for value in instances)
        if instance_count < 2 or instance_count > _MAX_RECOMMENDED_INSTANCES:
            continue
        if not _is_object_layer(instances[0].layer):
            continue
        envelope_anchor_handles = [
            value.handle
            for value in instances
            if value.anchor_distance(target) <= anchor_tolerance
        ]
        geometry_anchor_handles = [
            value.handle for value in instances if value.distance_to(target) <= anchor_tolerance
        ]
        anchor_handles = sorted(set([*envelope_anchor_handles, *geometry_anchor_handles]))
        minimum_distance = min(value.anchor_distance(target) for value in instances)
        method = (
            "repeated_insert"
            if instances[0].family == "INSERT"
            else "repeated_polyline"
        )
        group = {
            "signature": signature,
            "method": method,
            "instance_count": instance_count,
            "entity_instance_count": len(instances),
            "handles": [value.handle for value in instances],
            "anchor_handles": anchor_handles,
            "envelope_anchor_handles": envelope_anchor_handles,
            "geometry_anchor_handles": geometry_anchor_handles,
            "anchor_distance_drawing_units": round(minimum_distance, 6),
            "layer": instances[0].layer,
            "closed": instances[0].closed,
            "segment_count": (
                len(instances[0].points)
                if instances[0].closed
                else len(instances[0].points) - 1
            ),
            "representative_length_drawing_units": round(instances[0].length, 6),
            "instance_bboxes": [list(value.bbox) for value in instances[:20]],
        }
        repeated.append(group)
        if minimum_distance <= context_tolerance and not source_truncated:
            quantity_candidates.append(
                {
                    "value": instance_count,
                    "method": method,
                    "status": "REVIEW",
                    "confidence": (
                        0.56
                        if method == "repeated_insert" and anchor_handles
                        else 0.48
                        if anchor_handles
                        else 0.4
                    ),
                    "signature": signature,
                    "handles": group["handles"],
                    "anchor_handles": anchor_handles,
                    "anchor_distance_drawing_units": group[
                        "anchor_distance_drawing_units"
                    ],
                    "basis": [
                        "independent_raw_dxf_entities",
                        "translation_invariant_shape_fingerprint",
                        (
                            "leader_intersects_instance_envelope"
                            if envelope_anchor_handles
                            else "leader_near_repeated_instance_group"
                        ),
                        "review_only_not_billable_quantity",
                    ],
                }
            )
        if (
            len(anchor_handles) == 1
            and not source_truncated
        ):
            recommended_groups.append(group)

    panel_groups: dict[str, list[_VectorPrimitive]] = defaultdict(list)
    for primitive in panel_context:
        panel_groups[primitive.signature].append(primitive)
    for signature, values in sorted(panel_groups.items()):
        unique = {value.handle: value for value in values}
        instances = sorted(unique.values(), key=lambda value: value.handle)
        instance_count = sum(value.multiplicity for value in instances)
        local_instance_count = sum(
            value.multiplicity for value in groups.get(signature, ())
        )
        if (
            instance_count < 2
            or instance_count > _MAX_RECOMMENDED_INSTANCES
            or instance_count == local_instance_count
            or not _is_object_layer(instances[0].layer)
            or source_truncated
        ):
            continue
        envelope_anchor_handles = [
            value.handle
            for value in instances
            if value.anchor_distance(target) <= anchor_tolerance
        ]
        geometry_anchor_handles = [
            value.handle for value in instances if value.distance_to(target) <= anchor_tolerance
        ]
        anchor_handles = sorted(set([*envelope_anchor_handles, *geometry_anchor_handles]))
        if not anchor_handles:
            continue
        method = (
            "panel_repeated_insert"
            if instances[0].family == "INSERT"
            else "panel_repeated_polyline"
        )
        group = {
            "signature": signature,
            "method": method,
            "instance_count": instance_count,
            "entity_instance_count": len(instances),
            "handles": [value.handle for value in instances],
            "anchor_handles": anchor_handles,
            "envelope_anchor_handles": envelope_anchor_handles,
            "geometry_anchor_handles": geometry_anchor_handles,
            "anchor_distance_drawing_units": 0.0,
            "layer": instances[0].layer,
            "closed": instances[0].closed,
            "segment_count": (
                len(instances[0].points)
                if instances[0].closed
                else len(instances[0].points) - 1
            ),
            "representative_length_drawing_units": round(instances[0].length, 6),
            "instance_bboxes": [list(value.bbox) for value in instances[:20]],
        }
        repeated.append(group)
        quantity_candidates.append(
            {
                "value": instance_count,
                "method": method,
                "status": "REVIEW",
                "confidence": 0.5 if instances[0].family == "INSERT" else 0.44,
                "signature": signature,
                "handles": group["handles"],
                "anchor_handles": anchor_handles,
                "anchor_distance_drawing_units": 0.0,
                "basis": [
                    "leader_anchors_one_instance",
                    "congruent_instances_bounded_by_same_cad_panel",
                    "translation_invariant_shape_fingerprint",
                    "review_only_cross_view_duplication_not_resolved",
                ],
            }
        )

    connected_groups: dict[str, list[_ConnectedInstance]] = defaultdict(list)
    for instance in _connected_instances(
        local,
        geometry_tolerance=geometry_tolerance,
        radius=radius,
    ):
        connected_groups[instance.signature].append(instance)
    for signature, instances in sorted(connected_groups.items()):
        if len(instances) < 2 or len(instances) > _MAX_RECOMMENDED_INSTANCES:
            continue
        minimum_distance = min(_bbox_distance(value.bbox, target) for value in instances)
        if minimum_distance > context_tolerance or source_truncated:
            continue
        anchor_handles = sorted(
            handle
            for value in instances
            if _bbox_distance(value.bbox, target) <= anchor_tolerance
            for handle in value.handles
        )
        group = {
            "signature": signature,
            "method": "repeated_connected_graph",
            "instance_count": len(instances),
            "handles": [handle for value in instances for handle in value.handles],
            "anchor_handles": anchor_handles,
            "anchor_distance_drawing_units": round(minimum_distance, 6),
            "layer": instances[0].layer,
            "closed": False,
            "segment_count": instances[0].segment_count,
            "representative_length_drawing_units": round(instances[0].length, 6),
            "instance_bboxes": [list(value.bbox) for value in instances[:20]],
        }
        repeated.append(group)
        quantity_candidates.append(
            {
                "value": len(instances),
                "method": "repeated_connected_graph",
                "status": "REVIEW",
                "confidence": 0.52 if anchor_handles else 0.42,
                "signature": signature,
                "handles": group["handles"],
                "anchor_handles": anchor_handles,
                "anchor_distance_drawing_units": group[
                    "anchor_distance_drawing_units"
                ],
                "basis": [
                    "independent_endpoint_connected_linework",
                    "translation_invariant_connected_graph_fingerprint",
                    (
                        "leader_intersects_component_envelope"
                        if anchor_handles
                        else "leader_near_repeated_component_group"
                    ),
                    "review_only_not_billable_quantity",
                ],
            }
        )

    containing_inserts = [
        value
        for value in local
        if value.family == "INSERT"
        and value.anchor_distance(target) == 0
        and value.multiplicity == 1
        and max(value.bbox[2] - value.bbox[0], value.bbox[3] - value.bbox[1])
        <= radius * 3
    ]
    if len(containing_inserts) == 1 and not source_truncated:
        instance = containing_inserts[0]
        same_signature_count = sum(
            value.multiplicity
            for value in panel_context
            if value.family == "INSERT" and value.signature == instance.signature
        )
        if same_signature_count == 1:
            quantity_candidates.append(
                {
                    "value": 1,
                    "method": "single_insert",
                    "status": "REVIEW",
                    "confidence": 0.52,
                    "signature": instance.signature,
                    "handles": [instance.handle],
                    "anchor_handles": [instance.handle],
                    "anchor_distance_drawing_units": 0.0,
                    "basis": [
                        "single_non_annotation_insert_envelope_contains_leader",
                        "no_congruent_peer_in_local_probe",
                        "review_only_not_billable_quantity",
                    ],
                }
            )

    anchored_vectors = sorted(
        (
            (value.distance_to(target), value)
            for value in local
            if value.family != "INSERT"
            and _is_object_layer(value.layer)
            and value.length <= radius * 4
            and value.distance_to(target) <= anchor_tolerance
        ),
        key=lambda value: (value[0], value[1].handle),
    )
    single_vector: _VectorPrimitive | None = None
    if len(anchored_vectors) == 1:
        single_vector = anchored_vectors[0][1]
    elif (
        len(anchored_vectors) > 1
        and anchored_vectors[0][0] <= max(geometry_tolerance * 4, 2.0)
        and anchored_vectors[1][0] - anchored_vectors[0][0] >= anchor_tolerance * 0.5
    ):
        single_vector = anchored_vectors[0][1]
    if single_vector is not None and not source_truncated:
        same_signature_count = sum(
            value.multiplicity
            for value in panel_context
            if value.family != "INSERT" and value.signature == single_vector.signature
        )
        if same_signature_count == 1:
            quantity_candidates.append(
                {
                    "value": 1,
                    "method": "single_vector_instance",
                    "status": "REVIEW",
                    "confidence": 0.4,
                    "signature": single_vector.signature,
                    "handles": [single_vector.handle],
                    "anchor_handles": [single_vector.handle],
                    "anchor_distance_drawing_units": round(anchored_vectors[0][0], 6),
                    "basis": [
                        "one_vector_entity_uniquely_intersects_leader_target",
                        "no_congruent_peer_in_same_cad_panel",
                        "review_only_component_boundary_not_confirmed",
                    ],
                }
            )

    best_by_value: dict[int, dict[str, Any]] = {}
    for candidate in sorted(quantity_candidates, key=_candidate_sort_key):
        value = int(candidate["value"])
        if value not in best_by_value:
            best_by_value[value] = candidate
    quantity_candidates = sorted(best_by_value.values(), key=_candidate_sort_key)[
        :_MAX_QUANTITY_CANDIDATES
    ]

    recommended = recommended_groups[0] if len(recommended_groups) == 1 else None
    basis = [
        "raw_dxf_local_geometry_probe",
        "translation_invariant_instance_fingerprints",
        f"leader_target_tolerance:{anchor_tolerance:g}",
        "review_only_not_billable_quantity",
    ]
    if source_truncated:
        basis.append("source_vector_scan_truncated_no_recommendation")
    if len(recommended_groups) > 1:
        basis.append("multiple_anchored_repeated_groups_ambiguous")
    if recommended is not None:
        basis.extend(
            [
                f"shape_signature:{recommended['signature']}",
                f"independent_handles:{','.join(recommended['handles'])}",
            ]
        )
    return {
        "occurrence_id": occurrence.id,
        "source_file_id": occurrence.source_file_id,
        "sheet_id": occurrence.sheet_id,
        "leader_target": [target[0], target[1]],
        "probe_radius_drawing_units": radius,
        "local_primitive_count": len(local),
        "repeated_group_count": len(repeated),
        "recommended_quantity": recommended["instance_count"] if recommended else None,
        "quantity_candidates": quantity_candidates,
        "status": (
            "BLOCK"
            if len(recommended_groups) > 1
            else "REVIEW"
            if quantity_candidates
            else "BLOCK"
        ),
        "confidence": max(
            (float(value["confidence"]) for value in quantity_candidates),
            default=0.0,
        ),
        "basis": basis,
        "groups": sorted(repeated, key=lambda value: value["signature"])[:80],
    }


def _source_layout_name(sheet: Sheet | None) -> str:
    if sheet is None or not sheet.layout:
        return "Model"
    if "#viewport:" in sheet.layout:
        return "Model"
    return sheet.layout.split("#subview:", 1)[0]


def probe_repeated_vectors(
    source_paths: Mapping[str, Path | str],
    sheets: Sequence[Sheet],
    occurrences: Sequence[MtOccurrence],
    *,
    radius: float = _DEFAULT_RADIUS,
    geometry_tolerance: float = _DEFAULT_GEOMETRY_TOLERANCE,
    max_primitives: int = 250_000,
) -> dict[str, Any]:
    """Probe raw DXF linework near leader targets without auto-accepting quantity."""

    if not math.isfinite(radius) or radius <= 0:
        raise ValueError("radius must be a positive finite number")
    if not math.isfinite(geometry_tolerance) or geometry_tolerance <= 0:
        raise ValueError("geometry_tolerance must be a positive finite number")
    if max_primitives < 1:
        raise ValueError("max_primitives must be at least 1")

    sheet_by_id = {sheet.id: sheet for sheet in sheets}
    grouped: dict[tuple[str, str], list[MtOccurrence]] = defaultdict(list)
    skipped_without_target: list[str] = []
    for occurrence in occurrences:
        if occurrence.leader_target is None:
            skipped_without_target.append(occurrence.id)
            continue
        sheet = sheet_by_id.get(occurrence.sheet_id or "")
        grouped[(occurrence.source_file_id, _source_layout_name(sheet))].append(occurrence)

    probes: list[dict[str, Any]] = []
    issues: list[dict[str, Any]] = []
    scan_metadata: list[dict[str, Any]] = []
    for (source_file_id, layout_name), values in sorted(grouped.items()):
        source_value = source_paths.get(source_file_id)
        if source_value is None:
            issues.append(
                {
                    "code": "VECTOR_SOURCE_DXF_MISSING",
                    "source_file_id": source_file_id,
                    "layout": layout_name,
                    "occurrence_ids": sorted(value.id for value in values),
                }
            )
            continue
        source_path = Path(source_value).expanduser().resolve()
        try:
            primitives, truncated, supported_seen = _extract_primitives(
                source_path,
                layout_name,
                geometry_tolerance=geometry_tolerance,
                max_primitives=max_primitives,
            )
        except Exception as exc:
            issues.append(
                {
                    "code": "VECTOR_SOURCE_READ_FAILED",
                    "source_file_id": source_file_id,
                    "layout": layout_name,
                    "error": f"{type(exc).__name__}: {exc}",
                    "occurrence_ids": sorted(value.id for value in values),
                }
            )
            continue
        scan_metadata.append(
            {
                "source_file_id": source_file_id,
                "layout": layout_name,
                "supported_entity_count_seen": supported_seen,
                "usable_primitive_count": len(primitives),
                "truncated": truncated,
            }
        )
        for occurrence in sorted(values, key=lambda value: value.id):
            target = occurrence.leader_target
            if target is None:
                continue
            sheet = sheet_by_id.get(occurrence.sheet_id or "")
            probes.append(
                _probe_target(
                    primitives,
                    occurrence=occurrence,
                    target=target,
                    radius=radius,
                    geometry_tolerance=geometry_tolerance,
                    source_truncated=truncated,
                    context_bbox=(tuple(sheet.bbox) if sheet and sheet.bbox else None),
                )
            )

    return {
        "schema_version": "1.1",
        "policy": {
            "status": "REVIEW_ONLY",
            "auto_quantity": False,
            "candidate_limit_per_occurrence": _MAX_QUANTITY_CANDIDATES,
            "single_instance_requires": (
                "one non-annotation INSERT envelope containing the leader and no "
                "congruent peer in the local probe"
            ),
            "supported_entities": sorted(_SUPPORTED_VECTOR_TYPES),
            "radius_drawing_units": radius,
            "geometry_tolerance_drawing_units": geometry_tolerance,
            "max_primitives_per_source_layout": max_primitives,
        },
        "summary": {
            "occurrence_count": len(occurrences),
            "probed_occurrence_count": len(probes),
            "review_candidate_count": sum(
                bool(value.get("quantity_candidates")) for value in probes
            ),
            "unambiguous_recommendation_count": sum(
                value["recommended_quantity"] is not None for value in probes
            ),
            "candidate_value_count": sum(
                len(value.get("quantity_candidates", ())) for value in probes
            ),
            "skipped_without_leader_target_count": len(skipped_without_target),
            "issue_count": len(issues),
        },
        "scans": scan_metadata,
        "probes": probes,
        "skipped_without_leader_target_ids": sorted(skipped_without_target),
        "issues": issues,
    }
