"""Diagnostic union of native rectangular paths in an explicitly chosen band.

This is geometry, not material recognition or a takeoff approval. The caller
must separately bind material, component, views, units and annotated lengths.
In particular a closed outline perimeter is NOT the longitudinal run length.
"""

from __future__ import annotations

import math
from typing import Any


def _number(value: Any, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be a finite number")
    result = float(value)
    if not math.isfinite(result):
        raise ValueError(f"{name} must be a finite number")
    return result


def _rectangle(primitive: dict, tolerance: float) -> tuple[float, ...] | None:
    if primitive.get("entity_type") not in {"LWPOLYLINE", "POLYLINE"}:
        return None
    if primitive.get("closed") is not True:
        return None
    if (
        primitive.get("world_points_truncated") is not False
        or primitive.get("approximation") is not False
        or primitive.get("length_method") != "EXACT"
    ):
        return None
    geometry = primitive.get("geometry")
    if not isinstance(geometry, dict) or geometry.get("curve_segments_present") is not False:
        return None
    points = primitive.get("world_points")
    if not isinstance(points, list):
        return None
    try:
        points = [(_number(p[0], "x"), _number(p[1], "y")) for p in points]
    except (ValueError, TypeError, IndexError, KeyError):
        return None
    if len(points) == 5 and math.dist(points[0], points[-1]) <= tolerance:
        points = points[:-1]
    if len(points) != 4:
        return None
    xs, ys = zip(*points, strict=True)
    left, right, bottom, top = min(xs), max(xs), min(ys), max(ys)
    if right - left <= tolerance or top - bottom <= tolerance:
        return None
    corners = [(left, bottom), (left, top), (right, top), (right, bottom)]
    matches = [{i for i, c in enumerate(corners) if math.dist(p, c) <= tolerance} for p in points]
    if any(len(m) != 1 for m in matches) or len(set.union(*matches)) != 4:
        return None
    for p, q in zip(points, points[1:] + points[:1], strict=True):
        # Reject crossed corner order as well as slanted, non-rectangular paths.
        if min(abs(p[0] - q[0]), abs(p[1] - q[1])) > tolerance:
            return None
    return left, bottom, right, top


def summarize_rectangular_band(
    region: dict,
    *,
    baseline: float,
    height: float,
    extent: list[float] | tuple[float, float],
    tolerance: float = 0.05,
) -> dict:
    """Return exact longitudinal union in drawing units, always REVIEW-only.

    Only complete, non-approximated native rectangles spanning the requested
    height are considered. Overlapping/duplicate rectangles contribute once.
    The complement is called a gap, NOT automatically a door/opening deduction.
    No conversion to mm or count is inferred from drawing coordinates.
    """
    baseline = _number(baseline, "baseline")
    height = _number(height, "height")
    tolerance = _number(tolerance, "tolerance")
    if height <= 0 or tolerance <= 0 or tolerance >= height / 2:
        raise ValueError("height and tolerance must be positive, tolerance < height/2")
    if not isinstance(extent, (list, tuple)) or len(extent) != 2:
        raise ValueError("extent must contain two ordered endpoints")
    left, right = (_number(v, "extent") for v in extent)
    if left >= right:
        raise ValueError("extent must be increasing")
    if region.get("usable") is not True:
        raise ValueError("probe region is not usable")
    truncation = region.get("truncation")
    if (
        not isinstance(truncation, dict)
        or truncation.get("any") is not False
        or truncation.get("flags") != []
        or type(truncation.get("dropped_primitive_count")) is not int
        or truncation.get("dropped_primitive_count") != 0
        or type(truncation.get("dropped_path_candidate_count")) is not int
        or truncation.get("dropped_path_candidate_count") != 0
    ):
        raise ValueError("truncated probe region cannot establish band completeness")
    roi = region.get("render_bbox", [])
    if len(roi) != 4 or not (
        roi[0] <= left and roi[2] >= right and roi[1] <= baseline and roi[3] >= baseline + height
    ):
        raise ValueError("probe region must cover the whole requested band")
    candidates, rejected = [], []
    ids: set[str] = set()
    for primitive in region.get("primitives", []):
        bbox = _rectangle(primitive, tolerance)
        if bbox is None:
            continue
        x0, y0, x1, y1 = bbox
        if abs(y0 - baseline) > tolerance or abs(y1 - baseline - height) > tolerance:
            continue
        identifier = primitive.get("id")
        if not isinstance(identifier, str) or not identifier.strip() or identifier in ids:
            raise ValueError("band candidates require unique nonblank geometry IDs")
        ids.add(identifier)
        if x0 < left - tolerance or x1 > right + tolerance:
            rejected.append({"id": identifier, "reason": "EXTENDS_OUTSIDE_SELECTED_EXTENT"})
            continue
        candidates.append(
            {
                "id": identifier,
                "interval": [max(left, x0), min(right, x1)],
                "raw_interval": [x0, x1],
                "longitudinal_length": x1 - x0,
                "outline_perimeter": primitive.get("length_drawing_units"),
                "root_insert_handle": primitive.get("root_insert_handle"),
                "top_level_entity_handle": primitive.get("top_level_entity_handle"),
                "source_block_entity_handle": primitive.get("source_block_entity_handle"),
            }
        )
    candidates.sort(key=lambda c: (c["interval"][0], c["interval"][1], c["id"]))
    merged: list[dict] = []
    for candidate in candidates:
        x0, x1 = candidate["interval"]
        # Do not silently bridge even tiny positive gaps with a tolerance.
        if merged and x0 <= merged[-1]["interval"][1]:
            merged[-1]["interval"][1] = max(x1, merged[-1]["interval"][1])
            merged[-1]["geometry_ids"].append(candidate["id"])
        else:
            merged.append({"interval": [x0, x1], "geometry_ids": [candidate["id"]]})
    gaps, cursor = [], left
    for span in merged:
        x0, x1 = span["interval"]
        if x0 > cursor:
            gaps.append([cursor, x0])
        cursor = x1
    if cursor < right:
        gaps.append([cursor, right])
    total = math.fsum(b - a for a, b in (s["interval"] for s in merged))
    return {
        "schema_version": "rectangular-band-research/1",
        "state": "REVIEW",
        "material_confirmed": False,
        "quantity": None,
        "engineering_quantity": None,
        "drawing_units": region.get("units"),
        "source_sha256": region.get("source_sha256"),
        "sheet_id": region.get("sheet_id"),
        "baseline": baseline,
        "height": height,
        "extent": [left, right],
        "candidates": candidates,
        "union": merged,
        "gaps": gaps,
        "longitudinal_union_drawing_units": total,
        "gap_union_drawing_units": math.fsum(b - a for a, b in gaps),
        "partition_residual": (right - left) - total - math.fsum(b - a for a, b in gaps),
        "rejected": rejected,
        "warning": (
            "Geometric band only: bind material, component and native dimension semantics "
            "before takeoff. Gaps are not classified openings; span count is not assembly count."
        ),
    }
