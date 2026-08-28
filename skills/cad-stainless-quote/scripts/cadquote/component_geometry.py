"""Bounded, read-only geometry probes for reviewed component close-ups.

The semantic CAD index deliberately omits most linework.  This module revisits
the immutable DXF, expands only block references that intersect a component
render bbox, and exposes world-coordinate geometry for review.  It never assigns
measurement roles or promotes a quantity/takeoff item.
"""

from __future__ import annotations

import hashlib
import json
import math
from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import ezdxf
from ezdxf import bbox as ezbbox
from ezdxf.path import make_path

from .io import write_json_atomic

_SUPPORTED_TYPES = frozenset(
    {"LINE", "ARC", "CIRCLE", "LWPOLYLINE", "POLYLINE", "SPLINE", "ELLIPSE"}
)


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
    parts = tuple(_finite(part) for part in value)
    if any(part is None for part in parts):
        return None
    result = tuple(float(part) for part in parts)
    return result if result[2] > result[0] and result[3] > result[1] else None


def _intersects(
    left: tuple[float, float, float, float],
    right: tuple[float, float, float, float],
) -> bool:
    return not (
        left[2] < right[0]
        or left[0] > right[2]
        or left[3] < right[1]
        or left[1] > right[3]
    )


def _box_from_points(
    points: Sequence[tuple[float, float]],
) -> tuple[float, float, float, float] | None:
    if not points:
        return None
    xs = [point[0] for point in points]
    ys = [point[1] for point in points]
    return min(xs), min(ys), max(xs), max(ys)


def _entity_bbox(entity: Any, *, fast: bool) -> tuple[float, float, float, float] | None:
    try:
        extents = ezbbox.extents([entity], fast=fast)
        if not extents.has_data:
            return None
        result = (
            float(extents.extmin.x),
            float(extents.extmin.y),
            float(extents.extmax.x),
            float(extents.extmax.y),
        )
    except (AttributeError, TypeError, ValueError, ezdxf.DXFError):
        return None
    if not all(math.isfinite(value) for value in result):
        return None
    return result


def _point(value: Any) -> tuple[float, float]:
    return float(value.x), float(value.y)


def _distance_sum(points: Sequence[tuple[float, float]]) -> float:
    return sum(math.dist(left, right) for left, right in zip(points, points[1:], strict=False))


def _arc_sweep_radians(start_angle: float, end_angle: float) -> float:
    sweep = math.radians(end_angle - start_angle) % math.tau
    return math.tau if math.isclose(sweep, 0.0, abs_tol=1e-12) else sweep


def _source_handle(entity: Any) -> str | None:
    """Follow virtual-copy provenance to the original block entity handle."""

    current = entity
    result: str | None = None
    seen: set[int] = set()
    for _ in range(32):
        identity = id(current)
        if identity in seen:
            break
        seen.add(identity)
        try:
            handle = str(current.dxf.get("handle", "") or "").strip()
        except (AttributeError, ezdxf.DXFError):
            handle = ""
        if handle:
            result = handle
        source = getattr(current, "source_of_copy", None)
        if source is None:
            break
        current = source
    return result


def _effective_layer(raw_layer: str, inherited_layer: str | None) -> str:
    if raw_layer == "0" and inherited_layer:
        return inherited_layer
    return raw_layer


def _flatten_points(entity: Any, tolerance: float) -> list[tuple[float, float]]:
    return [_point(value) for value in make_path(entity).flattening(tolerance)]


def _polyline_exact_length(entity: Any) -> tuple[float, bool]:
    """Return segment length and whether every segment has an exact formula."""

    length = 0.0
    exact = True
    try:
        segments = list(entity.virtual_entities())
    except (AttributeError, TypeError, ValueError, ezdxf.DXFError):
        return 0.0, False
    if not segments:
        return 0.0, False
    for segment in segments:
        kind = segment.dxftype()
        if kind == "LINE":
            length += math.dist(_point(segment.dxf.start), _point(segment.dxf.end))
        elif kind == "ARC":
            radius = abs(float(segment.dxf.radius))
            length += radius * _arc_sweep_radians(
                float(segment.dxf.start_angle), float(segment.dxf.end_angle)
            )
        else:
            exact = False
    return length, exact


@dataclass(slots=True)
class _Geometry:
    entity_type: str
    bbox: tuple[float, float, float, float]
    endpoints: list[tuple[float, float]]
    world_points: list[tuple[float, float]]
    length: float
    closed: bool
    approximation: bool
    approximation_tolerance: float | None
    geometry: dict[str, Any]
    world_points_truncated: bool = False


def _geometry(
    entity: Any,
    *,
    flattening_tolerance: float,
    max_points: int,
) -> _Geometry | None:
    kind = entity.dxftype()
    points: list[tuple[float, float]]
    geometry: dict[str, Any]
    approximation = False
    tolerance: float | None = None
    closed = False

    try:
        if kind == "LINE":
            points = [_point(entity.dxf.start), _point(entity.dxf.end)]
            length = math.dist(points[0], points[1])
            geometry = {"start": list(points[0]), "end": list(points[1])}
        elif kind == "ARC":
            center = _point(entity.ocs().to_wcs(entity.dxf.center))
            radius = abs(float(entity.dxf.radius))
            start_angle = float(entity.dxf.start_angle)
            end_angle = float(entity.dxf.end_angle)
            sweep = _arc_sweep_radians(start_angle, end_angle)
            start = _point(entity.start_point)
            end = _point(entity.end_point)
            points = [start, end]
            length = radius * sweep
            geometry = {
                "center": list(center),
                "radius": radius,
                "start_angle_degrees": start_angle,
                "end_angle_degrees": end_angle,
                "sweep_degrees": math.degrees(sweep),
            }
        elif kind == "CIRCLE":
            center = _point(entity.ocs().to_wcs(entity.dxf.center))
            radius = abs(float(entity.dxf.radius))
            points = []
            length = math.tau * radius
            closed = True
            geometry = {"center": list(center), "radius": radius}
        elif kind in {"LWPOLYLINE", "POLYLINE"}:
            points = _flatten_points(entity, flattening_tolerance)
            exact_length, exact = _polyline_exact_length(entity)
            if exact:
                length = exact_length
            else:
                length = _distance_sum(points)
                approximation = True
                tolerance = flattening_tolerance
            closed = bool(getattr(entity, "closed", False) or getattr(entity, "is_closed", False))
            geometry = {
                "flattened_point_count": len(points),
                "curve_segments_present": not exact or not math.isclose(
                    length,
                    _distance_sum(points),
                    rel_tol=1e-9,
                    abs_tol=1e-9,
                ),
            }
        elif kind in {"SPLINE", "ELLIPSE"}:
            points = _flatten_points(entity, flattening_tolerance)
            length = _distance_sum(points)
            approximation = True
            tolerance = flattening_tolerance
            if kind == "ELLIPSE":
                start_param = float(entity.dxf.start_param)
                end_param = float(entity.dxf.end_param)
                sweep = (end_param - start_param) % math.tau
                closed = math.isclose(sweep, 0.0, abs_tol=1e-9)
                geometry = {
                    "center": list(_point(entity.dxf.center)),
                    "major_axis": list(_point(entity.dxf.major_axis)),
                    "ratio": float(entity.dxf.ratio),
                    "start_parameter": start_param,
                    "end_parameter": end_param,
                    "flattened_point_count": len(points),
                }
            else:
                closed = bool(getattr(entity, "closed", False))
                geometry = {
                    "degree": int(entity.dxf.degree),
                    "control_point_count": len(entity.control_points),
                    "flattened_point_count": len(points),
                }
        else:
            return None
    except (AttributeError, TypeError, ValueError, ZeroDivisionError, ezdxf.DXFError):
        return None

    if not math.isfinite(length) or length <= 1e-12:
        return None
    bbox = _entity_bbox(entity, fast=False) or _box_from_points(points)
    if bbox is None:
        return None
    if closed:
        endpoints: list[tuple[float, float]] = []
    elif len(points) >= 2:
        endpoints = [points[0], points[-1]]
    else:
        endpoints = []
    truncated = len(points) > max_points
    if truncated:
        # Retain both ends and a deterministic evenly-spaced audit sample.  Length
        # and bbox above are still computed from the complete flattened sequence.
        denominator = max_points - 1
        indexes = [round(index * (len(points) - 1) / denominator) for index in range(max_points)]
        points = [points[index] for index in indexes]
    return _Geometry(
        entity_type=kind,
        bbox=bbox,
        endpoints=endpoints,
        world_points=points,
        length=length,
        closed=closed,
        approximation=approximation,
        approximation_tolerance=tolerance,
        geometry=geometry,
        world_points_truncated=truncated,
    )


@dataclass(slots=True)
class _Region:
    output: dict[str, Any]
    bbox: tuple[float, float, float, float]
    primitives: list[dict[str, Any]] = field(default_factory=list)
    dropped_primitive_count: int = 0
    flags: set[str] = field(default_factory=set)


@dataclass(slots=True)
class _ScanState:
    max_source_entities: int
    max_total_primitives: int
    visited_entity_count: int = 0
    current_source_entity_count: int = 0
    supported_entity_count_seen: int = 0
    output_primitive_count: int = 0
    skipped_entity_count: int = 0
    source_scan_truncated: bool = False
    current_source_scan_truncated: bool = False
    global_output_truncated: bool = False
    block_expansion_truncated: bool = False
    current_block_expansion_truncated: bool = False
    issues: list[dict[str, Any]] = field(default_factory=list)


class _StopSourceScan(RuntimeError):
    pass


def _primitive_id(
    source_file_id: str,
    top_level_entity_ordinal: int,
    root_insert_handle: str | None,
    block_path: Sequence[str],
    source_handle: str | None,
    ordinal_path: Sequence[int],
    geometry: _Geometry,
) -> str:
    basis = {
        "source_file_id": source_file_id,
        "top_level_entity_ordinal": top_level_entity_ordinal,
        "root_insert_handle": root_insert_handle,
        "block_path": list(block_path),
        "source_handle": source_handle,
        "ordinal_path": list(ordinal_path),
        "entity_type": geometry.entity_type,
        "bbox": geometry.bbox,
        "length": geometry.length,
    }
    digest = hashlib.sha256(
        json.dumps(basis, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()[:24]
    return f"component-geometry:{digest}"


def _record_primitive(
    entity: Any,
    *,
    source_file_id: str,
    top_level_entity_ordinal: int,
    root_insert_handle: str | None,
    root_insert_instance_ordinal: int | None,
    block_path: Sequence[str],
    ordinal_path: Sequence[int],
    inherited_layer: str | None,
    flattening_tolerance: float,
    max_points: int,
) -> dict[str, Any] | None:
    value = _geometry(
        entity,
        flattening_tolerance=flattening_tolerance,
        max_points=max_points,
    )
    if value is None:
        return None
    raw_layer = str(entity.dxf.get("layer", "") or "").strip()
    source_handle = _source_handle(entity) if root_insert_handle else None
    top_level_handle = (
        str(entity.dxf.get("handle", "") or "").strip() or None
        if root_insert_handle is None
        else None
    )
    primitive_id = _primitive_id(
        source_file_id,
        top_level_entity_ordinal,
        root_insert_handle,
        block_path,
        source_handle or top_level_handle,
        ordinal_path,
        value,
    )
    return {
        "id": primitive_id,
        "state": "REVIEW",
        "measurement_role": None,
        "coordinate_space": "WCS_XY",
        "entity_type": value.entity_type,
        "layer": _effective_layer(raw_layer, inherited_layer),
        "source_layer": raw_layer,
        "bbox": list(value.bbox),
        "endpoints": [list(point) for point in value.endpoints],
        "world_points": [list(point) for point in value.world_points],
        "world_points_truncated": value.world_points_truncated,
        "closed": value.closed,
        "length_drawing_units": value.length,
        "length_method": "APPROXIMATE_FLATTENING" if value.approximation else "EXACT",
        "approximation": value.approximation,
        "approximation_tolerance_drawing_units": value.approximation_tolerance,
        "root_insert_handle": root_insert_handle,
        "root_insert_instance_ordinal": root_insert_instance_ordinal,
        "top_level_entity_ordinal": top_level_entity_ordinal,
        "block_path": list(block_path),
        "top_level_entity_handle": top_level_handle,
        "source_block_entity_handle": source_handle,
        "source_block_entity_ordinal": list(ordinal_path) if root_insert_handle else None,
        "provenance_state": (
            "TOP_LEVEL_HANDLE"
            if top_level_handle
            else "BLOCK_ENTITY_HANDLE"
            if source_handle
            else "BLOCK_ENTITY_ORDINAL_ONLY"
        ),
        "geometry": value.geometry,
        "warning": "REVIEW-only geometry; no length, width, quantity, or pricing role is assigned.",
    }


def _active_for_entity(
    entity: Any,
    regions: Sequence[_Region],
    active: Sequence[int],
) -> list[int]:
    entity_bbox = _entity_bbox(entity, fast=True)
    if entity_bbox is None:
        return list(active)
    return [index for index in active if _intersects(entity_bbox, regions[index].bbox)]


def _walk_entity(
    entity: Any,
    *,
    source_file_id: str,
    top_level_entity_ordinal: int,
    regions: Sequence[_Region],
    active: Sequence[int],
    state: _ScanState,
    root_insert_handle: str | None,
    root_insert_instance_ordinal: int | None,
    block_path: Sequence[str],
    ordinal_path: Sequence[int],
    inherited_layer: str | None,
    depth: int,
    max_depth: int,
    max_primitives_per_region: int,
    flattening_tolerance: float,
    max_points: int,
) -> None:
    if not active:
        return
    state.visited_entity_count += 1
    state.current_source_entity_count += 1
    if state.current_source_entity_count > state.max_source_entities:
        state.source_scan_truncated = True
        state.current_source_scan_truncated = True
        for index in active:
            regions[index].flags.add("SOURCE_ENTITY_LIMIT_REACHED")
        raise _StopSourceScan

    kind = entity.dxftype()
    if kind == "INSERT":
        narrowed = _active_for_entity(entity, regions, active)
        if not narrowed:
            return
        if depth >= max_depth:
            state.block_expansion_truncated = True
            state.current_block_expansion_truncated = True
            for index in narrowed:
                regions[index].flags.add("RECURSION_DEPTH_LIMIT_REACHED")
            return
        raw_layer = str(entity.dxf.get("layer", "") or "").strip()
        effective_layer = _effective_layer(raw_layer, inherited_layer)
        name = str(entity.dxf.get("name", "") or "<unnamed-block>")
        next_path = [*block_path, name]
        if root_insert_handle is None:
            root_handle = str(entity.dxf.get("handle", "") or "").strip() or None
        else:
            root_handle = root_insert_handle
        try:
            inserts = (
                list(entity.multi_insert())
                if int(getattr(entity, "mcount", 1)) > 1
                else [entity]
            )
        except (AttributeError, TypeError, ValueError, ezdxf.DXFError):
            inserts = [entity]
        for instance_index, instance in enumerate(inserts, start=1):
            instance_active = _active_for_entity(instance, regions, narrowed)
            if not instance_active:
                continue
            instance_ordinal = root_insert_instance_ordinal
            if root_insert_handle is None:
                instance_ordinal = instance_index

            def skipped(original: Any, reason: str) -> None:
                state.skipped_entity_count += 1
                state.issues.append(
                    {
                        "code": "BLOCK_ENTITY_SKIPPED",
                        "source_file_id": source_file_id,
                        "root_insert_handle": root_handle,
                        "block_path": next_path,
                        "source_block_entity_handle": _source_handle(original),
                        "reason": reason,
                    }
                )

            try:
                children = list(instance.virtual_entities(skipped_entity_callback=skipped))
            except Exception as exc:  # pragma: no cover - corrupt/exotic CAD boundary
                state.issues.append(
                    {
                        "code": "BLOCK_EXPANSION_FAILED",
                        "source_file_id": source_file_id,
                        "root_insert_handle": root_handle,
                        "block_path": next_path,
                        "error": f"{type(exc).__name__}: {exc}",
                    }
                )
                for index in instance_active:
                    regions[index].flags.add("BLOCK_EXPANSION_FAILED")
                continue
            for child_index, child in enumerate(children, start=1):
                _walk_entity(
                    child,
                    source_file_id=source_file_id,
                    top_level_entity_ordinal=top_level_entity_ordinal,
                    regions=regions,
                    active=instance_active,
                    state=state,
                    root_insert_handle=root_handle,
                    root_insert_instance_ordinal=instance_ordinal,
                    block_path=next_path,
                    ordinal_path=[*ordinal_path, child_index],
                    inherited_layer=effective_layer,
                    depth=depth + 1,
                    max_depth=max_depth,
                    max_primitives_per_region=max_primitives_per_region,
                    flattening_tolerance=flattening_tolerance,
                    max_points=max_points,
                )
        return

    if kind not in _SUPPORTED_TYPES:
        return
    state.supported_entity_count_seen += 1
    record = _record_primitive(
        entity,
        source_file_id=source_file_id,
        top_level_entity_ordinal=top_level_entity_ordinal,
        root_insert_handle=root_insert_handle,
        root_insert_instance_ordinal=root_insert_instance_ordinal,
        block_path=block_path,
        ordinal_path=ordinal_path,
        inherited_layer=inherited_layer,
        flattening_tolerance=flattening_tolerance,
        max_points=max_points,
    )
    if record is None:
        state.issues.append(
            {
                "code": "SUPPORTED_GEOMETRY_EXTRACTION_FAILED",
                "source_file_id": source_file_id,
                "entity_type": kind,
                "root_insert_handle": root_insert_handle,
                "source_block_entity_ordinal": list(ordinal_path) or None,
            }
        )
        return
    record_bbox = tuple(float(value) for value in record["bbox"])
    matches = [index for index in active if _intersects(record_bbox, regions[index].bbox)]
    for index in matches:
        region = regions[index]
        if len(region.primitives) >= max_primitives_per_region:
            region.dropped_primitive_count += 1
            region.flags.add("PER_REGION_PRIMITIVE_LIMIT_REACHED")
            continue
        if state.output_primitive_count >= state.max_total_primitives:
            state.global_output_truncated = True
            for value in regions:
                value.flags.add("GLOBAL_PRIMITIVE_LIMIT_REACHED")
            raise _StopSourceScan
        region.primitives.append(dict(record))
        state.output_primitive_count += 1


class _UnionFind:
    def __init__(self, size: int):
        self.parent = list(range(size))

    def find(self, value: int) -> int:
        while self.parent[value] != value:
            self.parent[value] = self.parent[self.parent[value]]
            value = self.parent[value]
        return value

    def union(self, left: int, right: int) -> None:
        left_root = self.find(left)
        right_root = self.find(right)
        if left_root != right_root:
            self.parent[max(left_root, right_root)] = min(left_root, right_root)


def _path_candidates(
    region_id: str,
    primitives: Sequence[Mapping[str, Any]],
    *,
    endpoint_tolerance: float,
    maximum: int,
) -> tuple[list[dict[str, Any]], int]:
    if not primitives:
        return [], 0
    union = _UnionFind(len(primitives))
    buckets: dict[tuple[str, int, int], list[tuple[int, tuple[float, float]]]] = defaultdict(list)
    for index, primitive in enumerate(primitives):
        if primitive.get("closed"):
            continue
        layer = str(primitive.get("layer") or "")
        endpoints = primitive.get("endpoints") or []
        for raw_point in endpoints:
            point = float(raw_point[0]), float(raw_point[1])
            cell_x = math.floor(point[0] / endpoint_tolerance)
            cell_y = math.floor(point[1] / endpoint_tolerance)
            for dx in (-1, 0, 1):
                for dy in (-1, 0, 1):
                    for other_index, other_point in buckets.get(
                        (layer, cell_x + dx, cell_y + dy), ()
                    ):
                        if math.dist(point, other_point) <= endpoint_tolerance:
                            union.union(index, other_index)
            buckets[(layer, cell_x, cell_y)].append((index, point))

    groups: dict[int, list[Mapping[str, Any]]] = defaultdict(list)
    for index, primitive in enumerate(primitives):
        groups[union.find(index)].append(primitive)

    candidates: list[dict[str, Any]] = []
    for values in groups.values():
        primitive_ids = sorted(str(value["id"]) for value in values)
        boxes = [tuple(float(part) for part in value["bbox"]) for value in values]
        bbox = (
            min(value[0] for value in boxes),
            min(value[1] for value in boxes),
            max(value[2] for value in boxes),
            max(value[3] for value in boxes),
        )
        length = sum(float(value["length_drawing_units"]) for value in values)
        approximate = any(bool(value.get("approximation")) for value in values)
        tolerances = [
            float(value["approximation_tolerance_drawing_units"])
            for value in values
            if value.get("approximation_tolerance_drawing_units") is not None
        ]
        digest = hashlib.sha256(
            f"{region_id}\0{'|'.join(primitive_ids)}".encode()
        ).hexdigest()[:24]
        layers = sorted({str(value.get("layer") or "") for value in values})
        candidates.append(
            {
                "id": f"component-path:{digest}",
                "state": "REVIEW",
                "measurement_role": None,
                "method": "same_layer_endpoint_connected_network",
                "layer": layers[0] if len(layers) == 1 else None,
                "bbox": list(bbox),
                "bbox_width_candidate_drawing_units": bbox[2] - bbox[0],
                "bbox_height_candidate_drawing_units": bbox[3] - bbox[1],
                "path_length_candidate_drawing_units": length,
                "length_method": "APPROXIMATE_FLATTENING" if approximate else "EXACT",
                "approximation": approximate,
                "approximation_tolerance_drawing_units": max(tolerances, default=None),
                "primitive_ids": primitive_ids,
                "primitive_count": len(values),
                "basis": [
                    "same_effective_layer",
                    f"endpoints_within:{endpoint_tolerance:g}_drawing_units",
                    "review_only_no_measurement_role",
                ],
                "warning": (
                    "A connected CAD network can contain construction/decorative edges; "
                    "width, height, and path length are unassigned REVIEW candidates only."
                ),
            }
        )
    candidates.sort(
        key=lambda value: (
            -float(value["path_length_candidate_drawing_units"]),
            str(value["layer"]),
            str(value["id"]),
        )
    )
    dropped = max(0, len(candidates) - maximum)
    return candidates[:maximum], dropped


def _selection_key(record: Mapping[str, Any], index: int) -> str:
    for field_name in ("selection_key", "component_id", "gold_row_id", "row_id", "sequence"):
        value = record.get(field_name)
        if value not in (None, ""):
            return str(value)
    return f"selection:{index}"


def probe_component_geometry(
    index_payload: Mapping[str, Any],
    closeup_payload: Mapping[str, Any],
    output_dir: Path | str,
    *,
    flattening_tolerance: float = 0.5,
    endpoint_tolerance: float = 0.5,
    max_primitives_per_region: int = 20_000,
    max_total_primitives: int = 250_000,
    max_source_entities: int = 500_000,
    max_paths_per_region: int = 2_000,
    max_recursion_depth: int = 12,
    max_points_per_primitive: int = 4_096,
) -> dict[str, Any]:
    """Probe transformed component geometry without mutating or exploding a DXF."""

    if not math.isfinite(flattening_tolerance) or flattening_tolerance <= 0:
        raise ValueError("flattening_tolerance must be a positive finite number")
    if not math.isfinite(endpoint_tolerance) or endpoint_tolerance <= 0:
        raise ValueError("endpoint_tolerance must be a positive finite number")
    for name, value in (
        ("max_primitives_per_region", max_primitives_per_region),
        ("max_total_primitives", max_total_primitives),
        ("max_source_entities", max_source_entities),
        ("max_paths_per_region", max_paths_per_region),
        ("max_recursion_depth", max_recursion_depth),
    ):
        if value < 1:
            raise ValueError(f"{name} must be at least 1")
    if max_points_per_primitive < 2:
        raise ValueError("max_points_per_primitive must be at least 2")

    destination = Path(output_dir).resolve()
    destination.mkdir(parents=True, exist_ok=True)
    sources: dict[str, Mapping[str, Any]] = {
        str(value["source_file_id"]): value
        for value in index_payload.get("sources", [])
        if isinstance(value, Mapping) and value.get("source_file_id")
    }
    raw_records = closeup_payload.get("records", [])
    if not isinstance(raw_records, Sequence) or isinstance(
        raw_records, (str, bytes, bytearray)
    ):
        raise ValueError("closeup_payload.records must be an array")

    regions: list[_Region] = []
    issues: list[dict[str, Any]] = []
    grouped: dict[str, list[int]] = defaultdict(list)
    selection_count = 0
    selection_without_evidence_count = 0
    for record_index, raw_record in enumerate(raw_records, start=1):
        if not isinstance(raw_record, Mapping):
            continue
        selection_count += 1
        selection_key = _selection_key(raw_record, record_index)
        evidence_values = raw_record.get("evidence", [])
        if not isinstance(evidence_values, Sequence) or isinstance(
            evidence_values, (str, bytes, bytearray)
        ):
            evidence_values = []
        if not evidence_values:
            selection_without_evidence_count += 1
            issues.append(
                {
                    "code": "CLOSEUP_EVIDENCE_MISSING",
                    "selection_key": selection_key,
                    "sequence": raw_record.get("sequence", record_index),
                }
            )
        for evidence_index, evidence in enumerate(evidence_values, start=1):
            if not isinstance(evidence, Mapping):
                continue
            source_file_id = str(evidence.get("source_file_id") or "")
            render_bbox = _bbox(evidence.get("render_bbox"))
            basis = {
                "selection_key": selection_key,
                "source_file_id": source_file_id,
                "sheet_id": evidence.get("sheet_id"),
                "render_bbox": render_bbox,
                "evidence_index": evidence_index,
            }
            digest = hashlib.sha256(
                json.dumps(basis, ensure_ascii=False, sort_keys=True).encode("utf-8")
            ).hexdigest()[:24]
            requested_layout = str(evidence.get("layout") or "Model")
            output = {
                "region_id": f"component-region:{digest}",
                "state": "REVIEW",
                "measurement_roles_assigned": False,
                "selection_key": selection_key,
                "sequence": raw_record.get("sequence", record_index),
                "evidence_index": evidence_index,
                "source_file_id": source_file_id or None,
                "source_sha256": (
                    evidence.get("source_sha256")
                    or sources.get(source_file_id, {}).get("source_sha256")
                ),
                "sheet_id": evidence.get("sheet_id"),
                "layout": "Model",
                "coordinate_space": "WCS_XY",
                "render_bbox": list(render_bbox) if render_bbox else None,
                "units": sources.get(source_file_id, {}).get("units"),
                "usable": bool(render_bbox and source_file_id in sources),
                "reason_codes": ["GEOMETRY_REQUIRES_COMPONENT_AND_ROLE_REVIEW"],
                "truncation": {},
                "primitives": [],
                "path_candidates": [],
            }
            if render_bbox is None:
                output["usable"] = False
                output["reason_codes"].append("INVALID_RENDER_BBOX")
                render_bbox = (0.0, 0.0, 1.0, 1.0)
            if requested_layout.casefold() != "model":
                output["usable"] = False
                output["reason_codes"].append("ONLY_MODEL_LAYOUT_SUPPORTED")
            region = _Region(output=output, bbox=render_bbox)
            regions.append(region)
            if source_file_id in sources and output["usable"]:
                grouped[source_file_id].append(len(regions) - 1)
            elif source_file_id not in sources:
                output["usable"] = False
                output["reason_codes"].append("SOURCE_NOT_IN_CAD_INDEX")

    state = _ScanState(
        max_source_entities=max_source_entities,
        max_total_primitives=max_total_primitives,
    )
    scans: list[dict[str, Any]] = []
    for source_file_id, region_indexes in sorted(grouped.items()):
        source = sources[source_file_id]
        source_path = Path(str(source.get("source_path") or "")).expanduser().resolve()
        state.current_source_entity_count = 0
        state.current_source_scan_truncated = False
        state.current_block_expansion_truncated = False
        scan_start_visited = state.visited_entity_count
        scan_start_supported = state.supported_entity_count_seen
        scan_start_skipped = state.skipped_entity_count
        if not source_path.is_file():
            for index in region_indexes:
                regions[index].output["usable"] = False
                regions[index].output["reason_codes"].append("SOURCE_DXF_MISSING")
            issues.append(
                {
                    "code": "SOURCE_DXF_MISSING",
                    "source_file_id": source_file_id,
                    "source_path": str(source_path),
                }
            )
            continue
        try:
            document = ezdxf.readfile(source_path)
            layout: Iterable[Any] = document.modelspace()
            for top_index, entity in enumerate(layout, start=1):
                active = _active_for_entity(entity, regions, region_indexes)
                if not active:
                    continue
                _walk_entity(
                    entity,
                    source_file_id=source_file_id,
                    top_level_entity_ordinal=top_index,
                    regions=regions,
                    active=active,
                    state=state,
                    root_insert_handle=None,
                    root_insert_instance_ordinal=None,
                    block_path=[],
                    ordinal_path=[],
                    inherited_layer=None,
                    depth=0,
                    max_depth=max_recursion_depth,
                    max_primitives_per_region=max_primitives_per_region,
                    flattening_tolerance=flattening_tolerance,
                    max_points=max_points_per_primitive,
                )
        except _StopSourceScan:
            if state.current_source_scan_truncated:
                for index in region_indexes:
                    regions[index].flags.add("SOURCE_ENTITY_LIMIT_REACHED")
        except Exception as exc:  # pragma: no cover - corrupt/exotic CAD boundary
            issues.append(
                {
                    "code": "SOURCE_DXF_READ_OR_SCAN_FAILED",
                    "source_file_id": source_file_id,
                    "source_path": str(source_path),
                    "error": f"{type(exc).__name__}: {exc}",
                }
            )
            for index in region_indexes:
                regions[index].output["usable"] = False
                regions[index].output["reason_codes"].append("SOURCE_DXF_READ_OR_SCAN_FAILED")
        scans.append(
            {
                "source_file_id": source_file_id,
                "source_path": str(source_path),
                "layout": "Model",
                "region_count": len(region_indexes),
                "visited_entity_count": state.visited_entity_count - scan_start_visited,
                "supported_entity_count_seen": (
                    state.supported_entity_count_seen - scan_start_supported
                ),
                "skipped_entity_count": state.skipped_entity_count - scan_start_skipped,
                "source_scan_truncated": state.current_source_scan_truncated,
                "global_output_truncated": state.global_output_truncated,
                "block_expansion_truncated": state.current_block_expansion_truncated,
            }
        )
        if state.global_output_truncated:
            break

    issues.extend(state.issues)
    primitive_count = 0
    path_count = 0
    truncated_region_count = 0
    for region in regions:
        paths, dropped_paths = _path_candidates(
            str(region.output["region_id"]),
            region.primitives,
            endpoint_tolerance=endpoint_tolerance,
            maximum=max_paths_per_region,
        )
        if dropped_paths:
            region.flags.add("PATH_CANDIDATE_LIMIT_REACHED")
        region.output["primitives"] = sorted(
            region.primitives,
            key=lambda value: (
                str(value.get("layer") or ""),
                str(value.get("entity_type") or ""),
                str(value.get("id") or ""),
            ),
        )
        region.output["path_candidates"] = paths
        region.output["primitive_count"] = len(region.primitives)
        region.output["path_candidate_count"] = len(paths)
        region.output["truncation"] = {
            "any": bool(region.flags),
            "flags": sorted(region.flags),
            "dropped_primitive_count": region.dropped_primitive_count,
            "dropped_path_candidate_count": dropped_paths,
        }
        if region.flags:
            region.output["reason_codes"].append("GEOMETRY_SCAN_TRUNCATED")
            truncated_region_count += 1
        primitive_count += len(region.primitives)
        path_count += len(paths)

    result = {
        "schema_version": "1.0",
        "purpose": "bounded_component_world_geometry_review_only",
        "path_scope": "local_run_diagnostics",
        "policy": {
            "status": "REVIEW_ONLY",
            "read_only_source": True,
            "auto_assign_measurement_role": False,
            "auto_assign_length": False,
            "auto_assign_quantity": False,
            "layout": "Model",
            "coordinate_space": "WCS_XY",
            "supported_entities": sorted(_SUPPORTED_TYPES),
            "flattening_tolerance_drawing_units": flattening_tolerance,
            "endpoint_tolerance_drawing_units": endpoint_tolerance,
            "max_primitives_per_region": max_primitives_per_region,
            "max_total_primitives": max_total_primitives,
            "max_source_entities": max_source_entities,
            "max_paths_per_region": max_paths_per_region,
            "max_recursion_depth": max_recursion_depth,
            "max_points_per_primitive": max_points_per_primitive,
        },
        "warning": (
            "All primitives and connected-path measurements are REVIEW-only. Block expansion "
            "reveals geometry but does not prove which edge is the billable component, length, "
            "width, or quantity. Local absolute paths must not be published."
        ),
        "summary": {
            "selection_count": selection_count,
            "selection_without_evidence_count": selection_without_evidence_count,
            "region_count": len(regions),
            "usable_region_count": sum(bool(value.output["usable"]) for value in regions),
            "primitive_count": primitive_count,
            "path_candidate_count": path_count,
            "truncated_region_count": truncated_region_count,
            "source_scan_count": len(scans),
            "issue_count": len(issues),
            "global_output_truncated": state.global_output_truncated,
        },
        "scans": scans,
        "regions": [value.output for value in regions],
        "issues": issues,
    }
    write_json_atomic(destination / "component_geometry.json", result)
    return result


__all__ = ["probe_component_geometry"]
