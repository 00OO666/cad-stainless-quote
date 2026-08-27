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

from .models import MtOccurrence, Sheet

_SUPPORTED_VECTOR_TYPES = frozenset({"LWPOLYLINE", "POLYLINE"})
_DEFAULT_RADIUS = 1_500.0
_DEFAULT_GEOMETRY_TOLERANCE = 0.5
_MAX_RECOMMENDED_INSTANCES = 20


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

    def distance_to(self, point: tuple[float, float]) -> float:
        pairs = list(zip(self.points, self.points[1:], strict=False))
        if self.closed:
            pairs.append((self.points[-1], self.points[0]))
        return min(_segment_distance(point, left, right) for left, right in pairs)


def _primitive(
    entity: Any,
    *,
    geometry_tolerance: float,
) -> _VectorPrimitive | None:
    raw_points = _polyline_points(entity)
    if raw_points is None:
        return None
    points = _clean_points(raw_points, geometry_tolerance * 0.01)
    if len(points) < 2:
        return None
    closed = bool(getattr(entity, "closed", False))
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
        "family": "POLYLINE",
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
    for entity in _layout(document, layout_name):
        if entity.dxftype() not in _SUPPORTED_VECTOR_TYPES:
            continue
        supported_seen += 1
        if len(primitives) >= max_primitives:
            truncated = True
            break
        value = _primitive(entity, geometry_tolerance=geometry_tolerance)
        if value is not None:
            primitives.append(value)
    return primitives, truncated, supported_seen


def _probe_target(
    primitives: Sequence[_VectorPrimitive],
    *,
    occurrence: MtOccurrence,
    target: tuple[float, float],
    radius: float,
    geometry_tolerance: float,
    source_truncated: bool,
) -> dict[str, Any]:
    local = [value for value in primitives if _bbox_distance(value.bbox, target) <= radius]
    groups: dict[str, list[_VectorPrimitive]] = defaultdict(list)
    for primitive in local:
        groups[primitive.signature].append(primitive)
    anchor_tolerance = max(5.0, min(25.0, radius * 0.01))
    repeated: list[dict[str, Any]] = []
    recommended_groups: list[dict[str, Any]] = []
    for signature, values in sorted(groups.items()):
        unique = {value.handle: value for value in values}
        instances = sorted(unique.values(), key=lambda value: value.handle)
        if len(instances) < 2:
            continue
        anchor_handles = [
            value.handle for value in instances if value.distance_to(target) <= anchor_tolerance
        ]
        group = {
            "signature": signature,
            "instance_count": len(instances),
            "handles": [value.handle for value in instances],
            "anchor_handles": anchor_handles,
            "layer": instances[0].layer,
            "closed": instances[0].closed,
            "segment_count": (
                len(instances[0].points)
                if instances[0].closed
                else len(instances[0].points) - 1
            ),
            "representative_length_drawing_units": round(instances[0].length, 6),
        }
        repeated.append(group)
        if (
            len(anchor_handles) == 1
            and len(instances) <= _MAX_RECOMMENDED_INSTANCES
            and not source_truncated
        ):
            recommended_groups.append(group)

    recommended = recommended_groups[0] if len(recommended_groups) == 1 else None
    basis = [
        "raw_dxf_local_polyline_probe",
        "translation_invariant_same_layer_shape_fingerprint",
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
        "status": "REVIEW" if recommended else "BLOCK",
        "confidence": 0.48 if recommended else 0.0,
        "basis": basis,
        "groups": repeated[:50],
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
            probes.append(
                _probe_target(
                    primitives,
                    occurrence=occurrence,
                    target=target,
                    radius=radius,
                    geometry_tolerance=geometry_tolerance,
                    source_truncated=truncated,
                )
            )

    return {
        "schema_version": "1.0",
        "policy": {
            "status": "REVIEW_ONLY",
            "auto_quantity": False,
            "supported_entities": sorted(_SUPPORTED_VECTOR_TYPES),
            "radius_drawing_units": radius,
            "geometry_tolerance_drawing_units": geometry_tolerance,
            "max_primitives_per_source_layout": max_primitives,
        },
        "summary": {
            "occurrence_count": len(occurrences),
            "probed_occurrence_count": len(probes),
            "review_candidate_count": sum(
                value["recommended_quantity"] is not None for value in probes
            ),
            "skipped_without_leader_target_count": len(skipped_without_target),
            "issue_count": len(issues),
        },
        "scans": scan_metadata,
        "probes": probes,
        "skipped_without_leader_target_ids": sorted(skipped_without_target),
        "issues": issues,
    }
