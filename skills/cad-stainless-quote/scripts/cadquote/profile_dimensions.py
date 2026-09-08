"""Bind native dimension endpoints to leader-contacted straight strip skins.

This produces an auditable, raw-unit nominal-boundary hypothesis, not a
manufacturing blank, a physical count, or a confirmed quotation measurement.
"""

from __future__ import annotations

import math

from ezdxf.math import Vec3
from shapely.geometry import LineString, Polygon


def _skin_pair(a, b, tol):
    """Require constant signed offset, matching turns and complete strip area."""
    if len(a) != len(b) or len(a) < 3:
        return None
    for bb in (b, list(reversed(b))):
        offsets, valid = [], True
        for p, q, r, s in zip(a, a[1:], bb, bb[1:], strict=False):
            la, lb = math.dist(p, q), math.dist(r, s)
            if min(la, lb) <= tol:
                valid = False
                break
            u = ((q[0] - p[0]) / la, (q[1] - p[1]) / la)
            v = ((s[0] - r[0]) / lb, (s[1] - r[1]) / lb)
            if math.dist(u, v) * max(la, lb) > tol:
                valid = False
                break
            offsets.append(u[0] * (r[1] - p[1]) - u[1] * (r[0] - p[0]))
        if not valid or abs(offsets[0]) <= tol:
            continue
        thickness = abs(offsets[0])
        if max(abs(x - offsets[0]) for x in offsets) > tol:
            continue
        middle = [((p[0] + q[0]) / 2, (p[1] + q[1]) / 2) for p, q in zip(a, bb, strict=True)]
        polygon = Polygon(a + list(reversed(bb)))
        line = LineString(middle)
        if not polygon.is_valid or not line.is_simple:
            continue
        rebuilt = line.buffer(thickness / 2, cap_style=2, join_style=2, mitre_limit=100)
        if (
            polygon.symmetric_difference(rebuilt).area > tol * polygon.length
            or polygon.boundary.hausdorff_distance(rebuilt.boundary) > tol
        ):
            continue
        return {"normal_separation_raw": thickness, "geometric_middle_length_raw": line.length}
    return None


def bind_boundary_dimensions(points, dimension_records, *, tolerance=1e-4):
    """Only exact vertices or vertex-anchored normal projections can bind.

    Equal values and nearby text never supply ownership. Each dimension can
    support at most one segment; conflicting displayed dimensions are retained.
    """
    bindings, conflicts, ambiguous = [], [], []
    for record in dimension_records:
        e = record["entity"]
        g = e.get("geometry", {})
        if record.get("coordinate_space") != "model" or (g.get("dimtype", -1) & 15) not in {0, 1}:
            continue
        raw_points = [g.get("defpoint2"), g.get("defpoint3")]
        if any(p is None or len(p) < 2 for p in raw_points):
            continue
        p0, p1 = [p[:2] for p in raw_points]
        if not all(any(math.dist(p, v) <= tolerance for v in points) for p in (p0, p1)):
            continue
        exact, projected = [], []
        for i, (a, b) in enumerate(zip(points, points[1:], strict=False)):
            length = math.dist(a, b)
            if length <= tolerance:
                continue
            u = ((b[0] - a[0]) / length, (b[1] - a[1]) / length)
            for p, q in ((p0, p1), (p1, p0)):
                if math.dist(p, a) <= tolerance and math.dist(q, b) <= tolerance:
                    exact.append(i)
                    break
                if (
                    abs((p[0] - a[0]) * u[0] + (p[1] - a[1]) * u[1]) <= tolerance
                    and abs((q[0] - b[0]) * u[0] + (q[1] - b[1]) * u[1]) <= tolerance
                    and abs((q[0] - p[0]) * u[1] - (q[1] - p[1]) * u[0]) <= tolerance
                ):
                    projected.append(i)
                    break
        candidates = sorted(set(exact or projected))
        if not candidates:
            continue
        if len(candidates) != 1:
            ambiguous.append({"dimension_handle": e["handle"], "segment_indices": candidates})
            continue
        i = candidates[0]
        length = math.dist(points[i], points[i + 1])
        value, geometric = e.get("value"), g.get("geometric_measurement")
        proof = {
            "dimension_handle": e["handle"],
            "dimension_entity_id": e["id"],
            "segment_index": i,
            "segment_length_raw": length,
            "displayed_value": value,
            "extension_points": raw_points,
            "text_override": e.get("text_override"),
            "binding": "EXACT_SEGMENT_ENDPOINTS" if exact else "PROFILE_VERTEX_NORMAL_PROJECTION",
        }
        override = e.get("text_override")
        try:
            override_ok = override in (None, "", "<>") or abs(float(override) - length) <= tolerance
        except (TypeError, ValueError):
            override_ok = False
        if (
            not override_ok
            or g.get("rounding_increment") != 0
            or not isinstance(value, (float, int))
            or not math.isfinite(value)
            or not isinstance(geometric, (float, int))
            or not math.isfinite(geometric)
            or abs(value - length) > tolerance
            or abs(geometric - length) > tolerance
            or abs(g.get("measurement_factor", 1) - 1) > tolerance
        ):
            conflicts.append(proof)
        else:
            bindings.append(proof)
    return {"bindings": bindings, "conflicts": conflicts, "ambiguous_segment_bindings": ambiguous}


def resolve_group_profile_dimensions(
    doc, group_probe, *, max_model_entities=200000, tolerance=1e-4
):
    """Use complete same-viewport native skins, preserving competing solutions."""
    if max_model_entities < 1 or not math.isfinite(tolerance) or tolerance <= 0:
        raise ValueError("positive finite limits required")
    result = {
        "schema_version": "dimension-supported-profile/1.0",
        "state": "REVIEW",
        "source_file_id": group_probe["source_file_id"],
        "group_id": group_probe["group_id"],
        "source_insunits": doc.header.get("$INSUNITS"),
        "records": [],
        "truncated": False,
        "mutates_takeoff": False,
        "physical_quantity": None,
        "measurement_role": None,
        "manufacturing_blank_width_mm": None,
        "limits": {"max_model_entities": max_model_entities, "tolerance_raw": tolerance},
        "limitations": [
            "Supported topology only: two fully visible open straight constant-offset skins.",
            "A dimension-supported boundary remains a nominal-profile hypothesis, not approval.",
            "Undimensioned segments retain geometry, not additional dimension text.",
            "Raw sum does not establish units, billable run, quantity or bend allowance.",
        ],
    }
    branches = [p for p in group_probe["probes"] if p["profile_candidates"]]
    if not branches or group_probe.get("truncated"):
        result["truncated"] = bool(group_probe.get("truncated"))
        return result
    profiles = []
    for n, e in enumerate(doc.modelspace(), 1):
        if n > max_model_entities:
            result["truncated"] = True
            return result
        if e.dxftype() != "LWPOLYLINE" or e.closed or e.has_arc or e.has_width:
            continue
        layer = doc.layers.get(e.dxf.layer)
        if e.dxf.get("invisible", 0) or layer.is_off() or layer.is_frozen():
            continue
        points = list(e.vertices_in_wcs())
        if len(points) < 3 or len(points) > 256 or any(abs(p.z) > tolerance for p in points):
            continue
        xy = [[p.x, p.y] for p in points]
        if LineString(xy).is_simple and all(
            math.dist(a, b) > tolerance for a, b in zip(xy, xy[1:], strict=False)
        ):
            profiles.append((e, xy))
    by_handle = {e.dxf.handle: xy for e, xy in profiles}
    for branch in branches:
        vp = doc.entitydb[branch["viewport_handle"]]
        forward, center = vp.get_transformation_matrix(), vp.dxf.center

        def visible(e, points, vp=vp, forward=forward, center=center):
            if e.dxf.layer in vp.frozen_layers:
                return False
            pp = [forward.transform(Vec3(*p)) for p in points]
            return all(
                abs(p.x - center.x) <= vp.dxf.width / 2 and abs(p.y - center.y) <= vp.dxf.height / 2
                for p in pp
            )

        dimensions = [
            d
            for d in group_probe["dimension_candidates"]
            if d["entity"]["source_file_id"] == group_probe["source_file_id"]
            and d["entity"]["sheet_id"] == group_probe["group_id"]
            and any(
                v["viewport_handle"] == branch["viewport_handle"] for v in d.get("visible_in", [])
            )
        ]
        candidates, seen_pairs = [], set()
        for hit in branch["profile_candidates"]:
            anchor = by_handle.get(hit["handle"])
            if anchor is None or not visible(doc.entitydb[hit["handle"]], anchor):
                continue
            for e, points in profiles:
                pair_key = tuple(sorted((hit["handle"], e.dxf.handle)))
                if (
                    e.dxf.handle == hit["handle"]
                    or pair_key in seen_pairs
                    or not visible(e, points)
                ):
                    continue
                paired = _skin_pair(anchor, points, tolerance)
                if paired is None:
                    continue
                seen_pairs.add(pair_key)
                skins = []
                for handle, xy in ((hit["handle"], anchor), (e.dxf.handle, points)):
                    proof = bind_boundary_dimensions(xy, dimensions, tolerance=tolerance)
                    indices = {b["segment_index"] for b in proof["bindings"]}
                    directions = []
                    for i in indices:
                        a, b = xy[i], xy[i + 1]
                        length = math.dist(a, b)
                        directions.append(((b[0] - a[0]) / length, (b[1] - a[1]) / length))
                    nonparallel = any(
                        abs(a[0] * b[1] - a[1] * b[0]) > 1e-6
                        for a in directions
                        for b in directions
                    )
                    lengths = [math.dist(a, b) for a, b in zip(xy, xy[1:], strict=False)]
                    skins.append(
                        {
                            "handle": handle,
                            "vertices": xy,
                            "segments_raw": lengths,
                            "length_raw": sum(lengths),
                            **proof,
                            "supported": nonparallel
                            and len(indices) >= 2
                            and not proof["conflicts"],
                            "undimensioned_segment_indices": [
                                i for i in range(len(lengths)) if i not in indices
                            ],
                        }
                    )
                candidates.append({"skin_handles": list(pair_key), **paired, "skins": skins})
        supported = [s for c in candidates for s in c["skins"] if s["supported"]]
        unique = (
            len(branch["profile_candidates"]) == 1
            and len(candidates) == 1
            and len(supported) == 1
            and not any(s["conflicts"] for c in candidates for s in c["skins"])
        )
        selected = supported[0] if unique else None
        result["records"].append(
            {
                "leader_handle": branch["leader_handle"],
                "annotation_handle": branch["annotation_handle"],
                "material_code": branch["material_code"],
                "viewport_handle": branch["viewport_handle"],
                "contact_handles": [p["handle"] for p in branch["profile_candidates"]],
                "candidates": candidates,
                "binding_state": "DIMENSION_SUPPORTED_BOUNDARY_CANDIDATE"
                if unique
                else "UNRESOLVED",
                "selected_boundary_handle": selected["handle"] if selected else None,
                "nominal_segments_raw": selected["segments_raw"] if selected else None,
                "nominal_boundary_sum_raw": selected["length_raw"] if selected else None,
                "unfolded_width_mm": None,
                "physical_quantity": None,
                "state": "REVIEW",
            }
        )
    return result
