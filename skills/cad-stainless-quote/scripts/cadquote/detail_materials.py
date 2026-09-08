"""Bounded native node material/leader/path probe, without quantity selection."""

from __future__ import annotations

import math
from pathlib import Path
from time import perf_counter

import ezdxf
from ezdxf import bbox
from ezdxf.math import Vec3
from shapely.geometry import LineString, Point

from .cad_index import _record_entity
from .io import sha256_file
from .material_branches import discover_material_leader_branches
from .materials import find_material_codes
from .models import MtOccurrence


def probe_detail_materials(
    doc,
    *,
    source_file_id,
    sheet_id,
    layout_name,
    viewport_handle,
    paper_bbox,
    paper_contact_tolerance=0.01,
    max_model_entities=200000,
):
    """No nearest label selection: use exact native annotation-border contact.

    Model profile hits are raw geometric candidates. A boundary line is not
    automatically a manufacturing centreline or unfolded width.
    """
    if (
        not math.isfinite(paper_contact_tolerance)
        or paper_contact_tolerance <= 0
        or max_model_entities < 1
    ):
        raise ValueError("positive limits required")
    x0, y0, x1, y1 = paper_bbox
    if not all(math.isfinite(v) for v in paper_bbox) or x0 >= x1 or y0 >= y1:
        raise ValueError("finite nondegenerate paper bbox required")
    layout = doc.layouts.get(layout_name)
    vp = doc.entitydb.get(viewport_handle)
    if (
        vp is None
        or vp.dxftype() != "VIEWPORT"
        or vp.get_layout() is not layout
        or vp.dxf.id <= 1
        or vp.dxf.flags & 1
    ):
        raise ValueError("native same-layout orthographic viewport required")
    direction = Vec3(vp.dxf.view_direction_vector)
    if direction.z <= 0 or abs(direction.x) > 1e-7 or abs(direction.y) > 1e-7:
        raise ValueError("only top-view CAD projection is supported")
    cache, entities, occurrences = bbox.Cache(), [], []
    space = "paper:" + layout_name

    def inside(point):
        return x0 <= point[0] <= x1 and y0 <= point[1] <= y1

    def visible(entity):
        layer = doc.layers.get(entity.dxf.layer)
        return not entity.dxf.get("invisible", 0) and not layer.is_off() and not layer.is_frozen()

    for ins in layout.query("INSERT"):
        if not visible(ins):
            continue
        codes = [
            (a, m)
            for a in ins.attribs
            for m in find_material_codes(a.dxf.text)
            if visible(a) and not a.dxf.flags & 1
        ]
        if not codes or not any(inside(a.dxf.insert) for a, _ in codes):
            continue
        frame = _record_entity(ins, source_file_id, sheet_id, space, cache)
        entities.append(frame)
        for a, m in codes:
            label = _record_entity(
                a, source_file_id, sheet_id, space, cache, parent_insert_handle=ins.dxf.handle
            )
            entities.append(label)
            occurrences.append(
                MtOccurrence(
                    id="native-label:" + label.id,
                    source_file_id=source_file_id,
                    sheet_id=sheet_id,
                    mt_code=m.normalized_code,
                    entity_ids=[label.id],
                )
            )
    for leader in layout.query("LEADER"):
        if not visible(leader):
            continue
        vertices = list(leader.vertices)
        if vertices and inside(vertices[0]) and inside(vertices[-1]):
            entities.append(_record_entity(leader, source_file_id, sheet_id, space, cache))
    branches = discover_material_leader_branches(entities, occurrences, allow_terminal_entry=True)
    forward = vp.get_transformation_matrix()
    inverse = forward.copy()
    inverse.inverse()
    origin = inverse.transform(Vec3(0, 0, 0))
    unit = inverse.transform(Vec3(paper_contact_tolerance, 0, 0))
    tolerance = origin.distance(unit)
    probes = []
    for record in branches["records"]:
        for branch in record["branches"]:
            if not branch["geometry_routing_eligible"] or not branch["leader_target"]:
                continue
            paper_tip = branch["leader_target"]
            c, w, h = vp.dxf.center, vp.dxf.width, vp.dxf.height
            if not (
                c.x - w / 2 <= paper_tip[0] <= c.x + w / 2
                and c.y - h / 2 <= paper_tip[1] <= c.y + h / 2
            ):
                continue
            tip = inverse.transform(Vec3(*paper_tip))
            residual = math.dist(list(forward.transform(tip))[:2], paper_tip)
            probes.append(
                {
                    "annotation_handle": record["annotation_handle"],
                    "material_code": record["mt_code"],
                    "leader_handle": branch["leader_handle"],
                    "branch_id": branch["branch_id"],
                    "paper_tip": list(paper_tip),
                    "viewport_handle": viewport_handle,
                    "model_tip": [tip.x, tip.y],
                    "roundtrip_residual": residual,
                    "profile_candidates": [],
                    "state": "REVIEW",
                    "measurement_role": None,
                }
            )
    examined, truncated = 0, False
    for entity in doc.modelspace() if probes else ():
        examined += 1
        if examined > max_model_entities:
            truncated = True
            break
        if entity.dxftype() != "LWPOLYLINE" or entity.dxf.get("invisible", 0):
            continue
        layer = doc.layers.get(entity.dxf.layer)
        if layer.is_off() or layer.is_frozen() or entity.dxf.layer in vp.frozen_layers:
            continue
        points = list(entity.vertices_in_wcs())
        if len(points) < 2 or any(abs(p.z) > 1e-6 for p in points):
            continue
        if any(abs(p[2]) > 1e-12 for p in entity.get_points("xyb")):
            continue
        xy = [[p.x, p.y] for p in points]
        if entity.closed:
            xy.append(xy[0])
        line = LineString(xy)
        for probe in probes:
            distance = line.distance(Point(probe["model_tip"]))
            if distance <= tolerance:
                probe["profile_candidates"].append(
                    {
                        "handle": entity.dxf.handle,
                        "layer": entity.dxf.layer,
                        "closed": entity.closed,
                        "vertices": xy,
                        "segments_raw": [math.dist(a, b) for a, b in zip(xy, xy[1:], strict=False)],
                        "length_raw": line.length,
                        "tip_distance_raw": distance,
                        "state": "CANDIDATE",
                        "measurement_role": None,
                    }
                )
    return {
        "schema_version": "native-detail-material-probe/1.0",
        "state": "REVIEW",
        "source_file_id": source_file_id,
        "sheet_id": sheet_id,
        "layout": layout_name,
        "viewport_handle": viewport_handle,
        "source_insunits": doc.header.get("$INSUNITS"),
        "paper_bbox": list(paper_bbox),
        "branches": branches,
        "probes": probes,
        "mutates_takeoff": False,
        "max_model_entities": max_model_entities,
        "model_entities_examined": min(examined, max_model_entities),
        "truncated": truncated or branches["truncated"],
        "contact_tolerance_raw": tolerance,
        "native_model_to_paper_matrix": list(forward),
        "native_paper_to_model_matrix": list(inverse),
        "limitations": [
            "Straight top-level model polylines only; nested/arc paths unsupported.",
            "Geometric contact is not confirmed steel-face or unfolded-width ownership.",
            "Native units are not defaulted to millimetres. No quantity is inferred.",
        ],
    }


def probe_routed_details(index_payload, routes, *, max_nodes=20):
    """Read only indexed, hash-verified DXFs for unique navigation candidates."""
    if max_nodes < 1:
        raise ValueError("positive node cap required")
    sources = {s["source_file_id"]: s for s in index_payload.get("sources", [])}
    nodes = {}
    for r in routes["records"]:
        sid = r["suggested_detail_sheet_id"]
        if sid:
            nodes[sid] = next(c for c in r["candidates"] if c["sheet_id"] == sid)
    documents, results, issues = {}, {}, []
    for i, (sid, node) in enumerate(sorted(nodes.items())):
        if i >= max_nodes:
            issues.append({"sheet_id": sid, "reason": "NODE_PROBE_CAP"})
            continue
        started = perf_counter()
        try:
            source = sources[node["source_file_id"]]
            path = Path(source["source_path"])
            if path.suffix.lower() != ".dxf" or sha256_file(path) != source["source_sha256"]:
                raise ValueError("indexed DXF hash mismatch")
            if node["source_file_id"] not in documents:
                documents[node["source_file_id"]] = ezdxf.readfile(path)
            doc = documents[node["source_file_id"]]
            title = node["matching_titles"][0]
            box = list(title["paper_viewport_bbox"])
            box[1] = min(box[1], *(p[1] for p in title["paper_title_points"]))
            result = probe_detail_materials(
                doc,
                source_file_id=node["source_file_id"],
                sheet_id=sid,
                layout_name=node["layout"].split("#viewport:")[0],
                viewport_handle=node["viewport_handle"],
                paper_bbox=box,
            )
            result["source_sha256"] = source["source_sha256"]
            result["elapsed_seconds"] = perf_counter() - started
            results[sid] = result
            if sha256_file(path) != source["source_sha256"]:
                raise ValueError("source changed during probe")
        except Exception as exc:
            results.pop(sid, None)
            issues.append({"sheet_id": sid, "reason": type(exc).__name__ + ": " + str(exc)})
    return {
        "state": "REVIEW",
        "requested_nodes": len(nodes),
        "completed_nodes": len(results),
        "max_nodes": max_nodes,
        "results": results,
        "issues": issues,
        "truncated": len(nodes) > max_nodes,
    }
