"""Inventory native model dimensions across explicitly scoped paper viewports.

Window fragments are evidence locations, never lengths or physical quantities.
"""

from __future__ import annotations

import math

from ezdxf import bbox
from ezdxf.math import Vec3
from shapely.geometry import LineString, Polygon
from shapely.ops import unary_union


def probe_model_dimension_scope(
    doc,
    *,
    source_file_id,
    scope_id,
    layout_name,
    viewport_handles,
    max_model_entities=200000,
    tolerance=1e-4,
):
    """Retain native dimensions whose origins and connecting span are in scope.

    A gap in model coordinates cannot supply a complete span. The caller supplies
    the logical scope; geometric coverage does not prove physical identity,
    material or billing role.
    """
    from .group_materials import native_group_dimension_record

    if max_model_entities < 1 or not math.isfinite(tolerance) or tolerance <= 0:
        raise ValueError("positive finite limits required")
    if not viewport_handles or len(set(viewport_handles)) != len(viewport_handles):
        raise ValueError("nonempty unique viewport scope required")
    layout, views = doc.layouts.get(layout_name), {}
    for h in viewport_handles:
        vp = doc.entitydb.get(h)
        if (
            vp is None
            or vp.dxftype() != "VIEWPORT"
            or vp.get_layout() is not layout
            or vp.dxf.status <= 0
            or vp.dxf.id <= 1
            or vp.has_extended_clipping_path
            or vp.dxf.flags & 1
        ):
            raise ValueError("active native rectangular same-layout viewport required")
        direction = Vec3(vp.dxf.view_direction_vector)
        if direction.z <= 0 or abs(direction.x) > 1e-7 or abs(direction.y) > 1e-7:
            raise ValueError("top-view orthographic projection required")
        forward = vp.get_transformation_matrix()
        inverse = forward.copy()
        inverse.inverse()
        c, w, height = vp.dxf.center, vp.dxf.width, vp.dxf.height
        corners = [
            inverse.transform(Vec3(c.x + sx * w / 2, c.y + sy * height / 2, 0))
            for sx, sy in [(-1, -1), (1, -1), (1, 1), (-1, 1)]
        ]
        views[h] = (vp, forward, Polygon([(p.x, p.y) for p in corners]))
    result = {
        "schema_version": "native-model-dimension-scope/1.0",
        "scope_id": scope_id,
        "source_file_id": source_file_id,
        "source_insunits": doc.header.get("$INSUNITS"),
        "layout_name": layout_name,
        "viewport_handles": list(viewport_handles),
        "state": "REVIEW",
        "mutates_takeoff": False,
        "physical_quantity": None,
        "dimensions": [],
        "rejected_cross_window_spans": [],
        "truncated": False,
    }
    cache = bbox.Cache()
    for n, dim in enumerate(doc.modelspace(), 1):
        if n > max_model_entities:
            result["truncated"] = True
            result["dimensions"] = []  # Never return a capped inventory as complete.
            break
        if dim.dxftype() != "DIMENSION" or dim.dxf.dimtype & 15 not in {0, 1}:
            continue
        layer = doc.layers.get(dim.dxf.layer)
        if dim.dxf.get("invisible", 0) or layer.is_off() or layer.is_frozen():
            continue
        points = [dim.dxf.get("defpoint2"), dim.dxf.get("defpoint3")]
        if any(p is None or not all(math.isfinite(v) for v in p) for p in points):
            continue
        ownership = [[], []]
        visible_polygons = []
        for h, (vp, forward, polygon) in views.items():
            if dim.dxf.layer in vp.frozen_layers:
                continue
            visible_polygons.append(polygon)
            c = vp.dxf.center
            for i, p in enumerate(points):
                q = forward.transform(p)
                if abs(q.x - c.x) <= vp.dxf.width / 2 and abs(q.y - c.y) <= vp.dxf.height / 2:
                    ownership[i].append({"viewport_handle": h, "paper_point": [q.x, q.y]})
        if not all(ownership):
            continue
        xy = [(p.x, p.y) for p in points]
        if math.dist(*xy) <= tolerance:
            continue
        common = set(x["viewport_handle"] for x in ownership[0]) & set(
            x["viewport_handle"] for x in ownership[1]
        )
        span = LineString(xy)
        if not unary_union(visible_polygons).buffer(tolerance).covers(span):
            result["rejected_cross_window_spans"].append(
                {
                    "dimension_handle": dim.dxf.handle,
                    "reason": "MODEL_SPAN_NOT_FULLY_COVERED",
                    "extension_origin_ownership": ownership,
                }
            )
            continue
        record = native_group_dimension_record(dim, source_file_id, scope_id, "model", cache)
        result["dimensions"].append(
            {
                "entity": record.model_dump(mode="json"),
                "coordinate_space": "model",
                "extension_origin_ownership": ownership,
                "common_viewport_handles": sorted(common),
                "scope_relation": "SAME_VIEWPORT" if common else "SPLIT_VIEWPORT_ORIGINS",
                "model_span_covered": True,
                "dimension_role": None,
                "state": "REVIEW",
            }
        )
    return result
