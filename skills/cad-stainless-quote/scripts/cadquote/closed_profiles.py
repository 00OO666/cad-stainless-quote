"""Separate leader-contacted closed strip outlines into their two native skins."""

from __future__ import annotations

import math

from ezdxf.math import Vec3

from .profile_dimensions import bind_boundary_dimensions
from .strip_profiles import analyze_strip_geometry


def probe_closed_group_profiles(
    doc, group_probe, *, tolerance=1e-4, max_vertices=256, max_cap_pairs=64
):
    """Raw geometry only; viewport fragments, skin sides and end caps are not pieces."""
    if not math.isfinite(tolerance) or tolerance <= 0 or min(max_vertices, max_cap_pairs) < 1:
        raise ValueError("positive finite tolerance and limits required")
    out = {
        "schema_version": "closed-group-strip-probe/1.0",
        "state": "REVIEW",
        "source_file_id": group_probe["source_file_id"],
        "group_id": group_probe["group_id"],
        "source_insunits": doc.header.get("$INSUNITS"),
        "records": [],
        "truncated": bool(group_probe.get("truncated")),
        "mutates_takeoff": False,
        "physical_quantity": None,
        "unfolded_width_mm": None,
        "limits": {
            "tolerance_raw": tolerance,
            "max_vertices": max_vertices,
            "max_cap_pairs": max_cap_pairs,
        },
        "limitations": [
            "A closed outline includes both skins and thickness end caps, not one unfolded path.",
            "Constant-width geometry does not identify a material or billable scope by itself.",
            "Dimension-supported skin is a nominal-boundary hypothesis, not a fabrication blank.",
            "No implicit millimetres, sheet count, component length or quotation acceptance.",
        ],
    }
    if out["truncated"]:
        return out
    for branch in group_probe["probes"]:
        hits = [h for h in branch["profile_candidates"] if h["closed"]]
        if not hits:
            continue
        record = {
            "leader_handle": branch["leader_handle"],
            "annotation_handle": branch["annotation_handle"],
            "material_code": branch["material_code"],
            "viewport_handle": branch["viewport_handle"],
            "contact_handles": [h["handle"] for h in branch["profile_candidates"]],
            "profiles": [],
            "issues": [],
            "selected_boundary": None,
            "state": "REVIEW",
            "binding_state": "UNRESOLVED",
        }
        out["records"].append(record)
        vp = doc.entitydb.get(branch["viewport_handle"])
        if (
            vp is None
            or vp.dxftype() != "VIEWPORT"
            or vp.dxf.status <= 0
            or vp.dxf.id <= 1
            or vp.has_extended_clipping_path
            or vp.dxf.flags & 1
            or vp.get_layout().name != group_probe["space"].removeprefix("paper:")
        ):
            record["issues"].append("INVALID_NATIVE_VIEWPORT")
            continue
        direction = Vec3(vp.dxf.view_direction_vector)
        if direction.z <= 0 or abs(direction.x) > 1e-7 or abs(direction.y) > 1e-7:
            record["issues"].append("UNSUPPORTED_PROJECTION")
            continue
        forward, center = vp.get_transformation_matrix(), vp.dxf.center
        dimensions = [
            d
            for d in group_probe["dimension_candidates"]
            if d["entity"]["source_file_id"] == group_probe["source_file_id"]
            and d["entity"]["sheet_id"] == group_probe["group_id"]
            and any(
                v["viewport_handle"] == branch["viewport_handle"] for v in d.get("visible_in", [])
            )
        ]
        supported = []
        for hit in hits:
            e = doc.entitydb.get(hit["handle"])
            if (
                e is None
                or e.dxftype() != "LWPOLYLINE"
                or not e.closed
                or e.has_arc
                or e.has_width
                or e.dxf.owner != doc.modelspace().block_record_handle
            ):
                record["issues"].append("UNSUPPORTED_NATIVE_OUTLINE:" + hit["handle"])
                continue
            layer = doc.layers.get(e.dxf.layer)
            if (
                e.dxf.get("invisible", 0)
                or layer.is_off()
                or layer.is_frozen()
                or e.dxf.layer in vp.frozen_layers
            ):
                record["issues"].append("INVISIBLE_NATIVE_OUTLINE:" + hit["handle"])
                continue
            pts = [list(p) for p in e.vertices_in_wcs()]
            if len(pts) > max_vertices:
                out["truncated"] = True
                record["issues"].append("VERTEX_CAP:" + hit["handle"])
                continue
            # A previously projected hit must still be the same complete native shape.
            old = hit["vertices"]
            expected = pts + [pts[0]] if pts else []
            if len(old) != len(expected) or any(
                math.dist(p[:2], q[:2]) > tolerance for p, q in zip(old, expected, strict=False)
            ):
                record["issues"].append("NATIVE_PROFILE_CHANGED:" + hit["handle"])
                continue
            pp = [forward.transform(Vec3(p)) for p in pts]
            if not all(
                abs(p.x - center.x) <= vp.dxf.width / 2 and abs(p.y - center.y) <= vp.dxf.height / 2
                for p in pp
            ):
                record["issues"].append("OUTLINE_CLIPPED_BY_VIEWPORT:" + hit["handle"])
                continue
            geometry = analyze_strip_geometry(
                pts,
                source_file_id=group_probe["source_file_id"],
                entity_handle=hit["handle"],
                tolerance=tolerance,
                max_vertices=max_vertices,
                max_cap_pairs=max_cap_pairs,
            )
            out["truncated"] = out["truncated"] or geometry["truncated"]
            for candidate_index, candidate in enumerate(geometry["candidates"]):
                skins = []
                for side in ["a", "b"]:
                    xy = candidate["skin_" + side + "_points"]
                    proof = bind_boundary_dimensions(xy, dimensions, tolerance=tolerance)
                    indices = {p["segment_index"] for p in proof["bindings"]}
                    lengths = [math.dist(a, b) for a, b in zip(xy, xy[1:], strict=False)]
                    directions = [
                        (
                            (xy[i + 1][0] - xy[i][0]) / lengths[i],
                            (xy[i + 1][1] - xy[i][1]) / lengths[i],
                        )
                        for i in indices
                    ]
                    nonparallel = any(
                        abs(a[0] * b[1] - a[1] * b[0]) > 1e-6
                        for a in directions
                        for b in directions
                    )
                    skin = {
                        "outline_handle": hit["handle"],
                        "candidate_index": candidate_index,
                        "skin_side": side,
                        "vertices": xy,
                        "segments_raw": lengths,
                        "length_raw": sum(lengths),
                        **proof,
                        "supported": len(indices) >= 2 and nonparallel and not proof["conflicts"],
                        "undimensioned_segment_indices": [
                            i for i in range(len(lengths)) if i not in indices
                        ],
                    }
                    skins.append(skin)
                    if skin["supported"]:
                        supported.append(skin)
                candidate["dimension_skins"] = skins
            record["profiles"].append(geometry)
        if (
            len(branch["profile_candidates"]) == 1
            and len(record["profiles"]) == 1
            and record["profiles"][0]["profile_state"] == "UNIQUE_GEOMETRIC_STRIP"
            and len(supported) == 1
            and not record["issues"]
            and not out["truncated"]
            and not any(
                s["conflicts"]
                for p in record["profiles"]
                for c in p["candidates"]
                for s in c["dimension_skins"]
            )
        ):
            record["selected_boundary"] = supported[0]
            record["binding_state"] = "DIMENSION_SUPPORTED_CLOSED_SKIN_CANDIDATE"
    # A later capped branch also invalidates earlier recommendations in this group.
    if out["truncated"]:
        for record in out["records"]:
            record["selected_boundary"] = None
            record["binding_state"] = "UNRESOLVED"
    return out
