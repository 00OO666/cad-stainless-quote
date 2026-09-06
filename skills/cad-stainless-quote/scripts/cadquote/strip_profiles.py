"""Recover a straight-segment constant-width strip's geometric middle path.

An audit helper over an explicitly selected native outline, not a fabrication
unfold calculator. Bend allowance, physical identity and material are separate.
"""

from __future__ import annotations

import itertools
import math

from shapely.geometry import LineString, Polygon


def analyze_strip_outline(
    points,
    *,
    source_file_id,
    entity_handle,
    units="millimeters",
    tolerance=1e-4,
    max_vertices=256,
    max_cap_pairs=64,
):
    """Pair two equal shortest end caps and verify both boundary skins.

    Every candidate must reconstruct the original polygon by a flat-capped,
    mitred buffer of its middle path. Multiple solutions remain ambiguous.
    No nearest-number selection or blanket perimeter/2 shortcut is used.
    """
    if not source_file_id or not entity_handle:
        raise ValueError("source and native entity identity required")
    if not math.isfinite(tolerance) or tolerance <= 0 or min(max_vertices, max_cap_pairs) < 1:
        raise ValueError("positive finite tolerance and resource limits required")
    result = {
        "schema_version": "native-strip-profile/1.0",
        "state": "REVIEW",
        "source_file_id": source_file_id,
        "entity_handle": entity_handle,
        "mutates_takeoff": False,
        "measurement_role": None,
        "physical_quantity": None,
        "unfolded_width_mm": None,
        "manufacturing_blank_width_mm": None,
        "profile_state": "UNRESOLVED",
        "candidates": [],
        "issues": [],
        "truncated": False,
        "limits": {
            "max_vertices": max_vertices,
            "max_cap_pairs": max_cap_pairs,
            "tolerance_mm": tolerance,
        },
        "limitations": [
            "Geometry only: does not prove metal, ownership or quantity.",
            "Middle path is not an approved bend allowance or manufacturing blank.",
            "Only straight-segment constant-width strips with identifiable shortest "
            "end caps are supported.",
        ],
    }
    if units != "millimeters":
        result["issues"].append("MILLIMETRE_UNITS_NOT_VERIFIED")
        return result
    if len(points) > max_vertices + 1:
        result["truncated"] = True
        result["issues"].append("VERTEX_CAP")
        return result
    try:
        pts = [tuple(float(v) for v in p) for p in points]
    except (ValueError, TypeError):
        result["issues"].append("INVALID_POINTS")
        return result
    if any(
        len(p) not in {2, 3}
        or not all(math.isfinite(v) for v in p)
        or (len(p) == 3 and abs(p[2]) > tolerance)
        for p in pts
    ):
        result["issues"].append("INVALID_OR_NON_PLANAR_POINTS")
        return result
    pts = [p[:2] for p in pts]
    if len(pts) > 1 and math.dist(pts[0], pts[-1]) <= tolerance:
        pts.pop()
    if len(pts) > max_vertices:
        result["truncated"] = True
        result["issues"].append("VERTEX_CAP")
        return result
    if len(pts) < 4:
        result["issues"].append("INSUFFICIENT_CLOSED_OUTLINE")
        return result
    shape = Polygon(pts)
    lengths = [math.dist(p, pts[(i + 1) % len(pts)]) for i, p in enumerate(pts)]
    if not shape.is_valid or shape.area <= tolerance**2 or min(lengths) <= tolerance:
        result["issues"].append("INVALID_POLYGON_OR_ZERO_SEGMENT")
        return result
    result["source_outline_points"] = [list(p) for p in pts]
    result["outline_perimeter_mm"] = shape.length
    result["outline_area_mm2"] = shape.area
    result["projection_bbox_mm"] = list(shape.bounds)
    shortest = min(lengths)
    caps = [i for i, length in enumerate(lengths) if abs(length - shortest) <= tolerance]
    result["shortest_edge_indices"] = caps
    if len(caps) * (len(caps) - 1) // 2 > max_cap_pairs:
        result["truncated"] = True
        result["issues"].append("END_CAP_PAIR_CAP")
        return result
    n = len(pts)
    for first, second in itertools.combinations(caps, 2):
        if second - first in {1, n - 1}:
            continue
        # Walk from either end of cap A to the corresponding end of cap B.
        inner = pts[first + 1 : second + 1]
        outer = [pts[i % n] for i in range(first, second - n, -1)]
        if len(inner) != len(outer) or len(inner) < 2:
            continue
        thickness = (lengths[first] + lengths[second]) / 2
        middle = [((a[0] + b[0]) / 2, (a[1] + b[1]) / 2) for a, b in zip(inner, outer, strict=True)]
        pairs = []
        valid = True
        for i in range(len(inner) - 1):
            a, b, c, d = inner[i], inner[i + 1], outer[i], outer[i + 1]
            av = (b[0] - a[0], b[1] - a[1])
            bv = (d[0] - c[0], d[1] - c[1])
            la, lb = math.hypot(*av), math.hypot(*bv)
            if min(la, lb) <= tolerance:
                valid = False
                break
            u, v = (av[0] / la, av[1] / la), (bv[0] / lb, bv[1] / lb)
            offset = abs(u[0] * (c[1] - a[1]) - u[1] * (c[0] - a[0]))
            if math.dist(u, v) * max(la, lb) > tolerance or abs(offset - thickness) > tolerance:
                valid = False
                break
            pairs.append(
                {
                    "segment_index": i,
                    "skin_a_length_mm": la,
                    "skin_b_length_mm": lb,
                    "normal_separation_mm": offset,
                    "middle_length_mm": math.dist(middle[i], middle[i + 1]),
                }
            )
        if not valid:
            continue
        line = LineString(middle)
        if not line.is_simple or line.length <= tolerance:
            continue
        rebuilt = line.buffer(thickness / 2, cap_style=2, join_style=2, mitre_limit=100)
        # Check the complete polygon, not just equal area or a local nearest edge.
        difference = shape.symmetric_difference(rebuilt).area
        if (
            difference > tolerance * shape.length
            or shape.boundary.hausdorff_distance(rebuilt.boundary) > tolerance
        ):
            continue
        result["candidates"].append(
            {
                "end_cap_edge_indices": [first, second],
                "geometric_thickness_mm": thickness,
                "skin_a_points": [list(p) for p in inner],
                "skin_b_points": [list(p) for p in outer],
                "middle_path_points": [list(p) for p in middle],
                "skin_a_length_mm": sum(p["skin_a_length_mm"] for p in pairs),
                "skin_b_length_mm": sum(p["skin_b_length_mm"] for p in pairs),
                "geometric_middle_path_length_mm": line.length,
                "segment_pairs": pairs,
                "polygon_reconstruction_difference_mm2": difference,
                "state": "REVIEW",
                "measurement_role": None,
            }
        )
    result["profile_state"] = (
        "UNIQUE_GEOMETRIC_STRIP"
        if len(result["candidates"]) == 1
        else "AMBIGUOUS_GEOMETRIC_STRIP"
        if result["candidates"]
        else "UNSUPPORTED_STRIP_TOPOLOGY"
    )
    return result


def analyze_native_strip_profile(doc, *, source_file_id, entity_handle, **limits):
    """Fail closed for open, curved, wide-stroked, nested or nonplanar DXF paths."""
    entity = doc.entitydb.get(entity_handle)
    if (
        entity is None
        or entity.dxftype() != "LWPOLYLINE"
        or not entity.closed
        or entity.has_arc
        or entity.has_width
    ):
        raise ValueError("one closed straight zero-stroke-width LWPOLYLINE required")
    if entity.dxf.owner != doc.modelspace().block_record_handle:
        raise ValueError(
            "top-level model-space outline required; nested transforms must be verified separately"
        )
    return analyze_strip_outline(
        [list(p) for p in entity.vertices_in_wcs()],
        source_file_id=source_file_id,
        entity_handle=entity_handle,
        units="millimeters" if doc.header.get("$INSUNITS") == 4 else "unknown",
        **limits,
    )
