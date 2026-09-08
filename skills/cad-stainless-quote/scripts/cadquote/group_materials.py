"""Native group-level evidence probe with separate projection per viewport."""

from __future__ import annotations

import math
from pathlib import Path
from time import perf_counter

import ezdxf
from ezdxf import bbox
from ezdxf.math import Vec3

from .cad_index import _record_entity
from .detail_materials import probe_detail_materials
from .io import sha256_file
from .profile_dimensions import resolve_group_profile_dimensions
from .viewport_groups import _union


def native_group_dimension_record(dim, source_file_id, group_id, space, cache):
    """Refresh profile-proof metadata even with the earlier public index schema."""
    record = _record_entity(dim, source_file_id, group_id, space, cache)
    record.geometry["rounding_increment"] = float(dim.override().get("dimrnd", 0) or 0)
    return record


def probe_node_view_group(doc, group, *, max_model_entities=200000):
    if group["group_state"] != "UNIQUE_TITLE_GROUP_CANDIDATE":
        raise ValueError("unambiguous numbered view group required")
    layout_name = group["space"].removeprefix("paper:")
    layout = doc.layouts.get(layout_name)
    views = {}
    for member in group["members"]:
        h = member["viewport_handle"]
        vp = doc.entitydb.get(h)
        if vp is None or vp.dxftype() != "VIEWPORT" or vp.get_layout() is not layout:
            raise ValueError("native same-layout viewport required")
        c, w, height = vp.dxf.center, vp.dxf.width, vp.dxf.height
        native_box = [c.x - w / 2, c.y - height / 2, c.x + w / 2, c.y + height / 2]
        if any(abs(a - b) > 1e-5 for a, b in zip(native_box, member["paper_bbox"], strict=True)):
            raise ValueError("indexed viewport footprint changed")
        if vp.dxf.status <= 0 or vp.has_extended_clipping_path:
            raise ValueError("inactive or nonrectangular viewport unsupported for group probe")
        views[h] = native_box

    def owners(point):
        return sorted(
            h for h, b in views.items() if b[0] <= point[0] <= b[2] and b[1] <= point[1] <= b[3]
        )

    member_results, issues, probes = [], [], []
    crop_boxes = [group["paper_context_bbox"]]
    for h in views:
        result = probe_detail_materials(
            doc,
            source_file_id=group["source_file_id"],
            sheet_id=group["group_id"],
            layout_name=layout_name,
            viewport_handle=h,
            # Native annotations can be far outside a viewport. Start with the
            # containing page; exact leader tip membership supplies the scope.
            paper_bbox=group["paper_frame_bbox"],
            max_model_entities=max_model_entities,
        )
        member_results.append(result)
        for p in result["probes"]:
            membership = owners(p["paper_tip"])
            if membership != [h]:
                issues.append(
                    {
                        "reason": "LEADER_TIP_VIEWPORT_AMBIGUOUS",
                        "leader_handle": p["leader_handle"],
                        "viewport_handles": membership,
                    }
                )
                continue
            probes.append(p)
            for handle in (p["annotation_handle"], p["leader_handle"]):
                bounds = bbox.extents([doc.entitydb[handle]], fast=True)
                if bounds.has_data:
                    crop_boxes.append(
                        [bounds.extmin.x, bounds.extmin.y, bounds.extmax.x, bounds.extmax.y]
                    )
    dimensions, cache = [], bbox.Cache()
    for dim in layout.query("DIMENSION"):
        layer = doc.layers.get(dim.dxf.layer)
        if dim.dxf.get("invisible", 0) or layer.is_off() or layer.is_frozen():
            continue
        # Only extension-point-owned linear dimensions are offered. An overall
        # paper distance crossing broken windows is not recomputed as model size.
        if dim.dxf.dimtype & 15 not in {0, 1}:
            continue
        p0, p1 = dim.dxf.get("defpoint2"), dim.dxf.get("defpoint3")
        if p0 is None or p1 is None:
            continue
        a, b = owners(p0), owners(p1)
        if len(a) != 1 or len(b) != 1:
            continue
        record = native_group_dimension_record(
            dim, group["source_file_id"], group["group_id"], group["space"], cache
        )
        dimensions.append(
            {
                "coordinate_space": group["space"],
                "entity": record.model_dump(mode="json"),
                "endpoint_viewport_handles": [a[0], b[0]],
                "crosses_broken_viewports": a != b,
                "dimension_role": None,
                "state": "REVIEW",
            }
        )
        if record.bbox:
            crop_boxes.append(list(record.bbox))
    model_scan_truncated = False
    for n, dim in enumerate(doc.modelspace(), start=1):
        if n > max_model_entities:
            model_scan_truncated = True
            break
        if dim.dxftype() != "DIMENSION" or dim.dxf.dimtype & 15 not in {0, 1}:
            continue
        layer = doc.layers.get(dim.dxf.layer)
        if dim.dxf.get("invisible", 0) or layer.is_off() or layer.is_frozen():
            continue
        p0, p1 = dim.dxf.get("defpoint2"), dim.dxf.get("defpoint3")
        if p0 is None or p1 is None:
            continue
        visible_in = []
        for h, box in views.items():
            vp = doc.entitydb[h]
            if dim.dxf.layer in vp.frozen_layers:
                continue
            forward = vp.get_transformation_matrix()
            points = [forward.transform(Vec3(p)) for p in (p0, p1)]
            if all(box[0] <= p.x <= box[2] and box[1] <= p.y <= box[3] for p in points):
                visible_in.append(
                    {"viewport_handle": h, "paper_extension_points": [[p.x, p.y] for p in points]}
                )
        if visible_in:
            record = native_group_dimension_record(
                dim, group["source_file_id"], group["group_id"], "model", cache
            )
            dimensions.append(
                {
                    "entity": record.model_dump(mode="json"),
                    "coordinate_space": "model",
                    "visible_in": visible_in,
                    "dimension_role": None,
                    "state": "REVIEW",
                }
            )
    envelope = _union(crop_boxes)
    frame = group["paper_frame_bbox"]
    if not all(math.isfinite(v) for v in envelope):
        raise ValueError("nonfinite evidence envelope")
    clipped = [
        max(envelope[0], frame[0]),
        max(envelope[1], frame[1]),
        min(envelope[2], frame[2]),
        min(envelope[3], frame[3]),
    ]
    if clipped != envelope:
        issues.append({"reason": "EVIDENCE_ENVELOPE_CLIPPED_TO_PAGE"})
    result = {
        "schema_version": "native-node-group-probe/1.0",
        "group_id": group["group_id"],
        "source_file_id": group["source_file_id"],
        "source_insunits": doc.header.get("$INSUNITS"),
        "space": group["space"],
        "viewport_handles": list(views),
        "member_results": member_results,
        "probes": probes,
        "dimension_candidates": dimensions,
        "paper_evidence_bbox": clipped,
        "issues": issues,
        "state": "REVIEW",
        "truncated": model_scan_truncated or any(m["truncated"] for m in member_results),
        "mutates_takeoff": False,
        "physical_quantity": None,
        "measurement_role": None,
        "limitations": [
            "Dimensions are extension-point-owned candidates, not unfolded sizes.",
            "No physical item count or units are inferred from fragment count.",
            "Profile probe remains top-level straight-polyline-only.",
            "Context images can include unbound neighbouring paper annotations.",
        ],
    }
    result["dimension_supported_profiles"] = resolve_group_profile_dimensions(
        doc, result, max_model_entities=max_model_entities
    )
    result["truncated"] = result["truncated"] or result["dimension_supported_profiles"]["truncated"]
    return result


def probe_routed_groups(index_payload, routes, *, max_groups=20):
    if max_groups < 1:
        raise ValueError("positive group cap required")
    wanted = {
        r["suggested_detail_group_id"]
        for r in routes["records"]
        if r.get("suggested_detail_group_id")
    }
    groups = {g["group_id"]: g for g in routes["node_view_groups"]["groups"]}
    sources = {s["source_file_id"]: s for s in index_payload["sources"]}
    documents, results, issues = {}, {}, []
    for i, gid in enumerate(sorted(wanted)):
        if i >= max_groups:
            issues.append({"group_id": gid, "reason": "GROUP_PROBE_CAP"})
            continue
        try:
            group = groups[gid]
            source = sources[group["source_file_id"]]
            path = Path(source["source_path"])
            if path.suffix.lower() != ".dxf" or sha256_file(path) != source["source_sha256"]:
                raise ValueError("indexed DXF hash mismatch")
            started = perf_counter()
            if group["source_file_id"] not in documents:
                documents[group["source_file_id"]] = ezdxf.readfile(path)
            result = probe_node_view_group(documents[group["source_file_id"]], group)
            if sha256_file(path) != source["source_sha256"]:
                raise ValueError("source changed during probe")
            result["source_sha256"] = source["source_sha256"]
            result["elapsed_seconds"] = perf_counter() - started
            results[gid] = result
        except Exception as exc:
            issues.append({"group_id": gid, "reason": type(exc).__name__ + ": " + str(exc)})
    return {
        "state": "REVIEW",
        "requested_groups": len(wanted),
        "completed_groups": len(results),
        "max_groups": max_groups,
        "results": results,
        "issues": issues,
        "truncated": len(wanted) > max_groups or any(r["truncated"] for r in results.values()),
    }
