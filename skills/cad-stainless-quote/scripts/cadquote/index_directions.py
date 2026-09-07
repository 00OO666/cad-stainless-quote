"""Read circular elevation-index arrow geometry from an immutable native DXF.

Only same-parent attributes and a supported symmetric radial wedge are used.
The arrow vector is CAD geometry, NOT a confirmed physical-component/view link.
Unsupported, hidden, nested or ambiguous symbols remain explicit REVIEW records.
"""

from __future__ import annotations

import math
import re
from collections import Counter
from copy import deepcopy

from ezdxf.math import Vec3, bulge_to_arc


def _visible(entity, doc):
    if entity.dxf.get("invisible", 0):
        return False
    if entity.dxftype() in {"ATTRIB", "ATTDEF"} and entity.dxf.get("flags", 0) & 1:
        return False
    layer = doc.layers.get(entity.dxf.layer)
    return not (layer.is_off() or layer.is_frozen())


def _point(point):
    return [float(point[0]), float(point[1])]


def _unit(a, b):
    dx, dy = b[0] - a[0], b[1] - a[1]
    norm = math.hypot(dx, dy)
    return [dx / norm, dy / norm] if math.isfinite(norm) and norm > 1e-9 else None


def _wedge_tip(vertices, center, radius):
    """One curved base on the circle, two equal straight sides, radial apex."""
    if len(vertices) != 3 or any(not math.isfinite(v) for p in vertices for v in p):
        return None
    curved = [i for i, p in enumerate(vertices) if abs(p[2]) > 1e-9]
    if len(curved) != 1:
        return None
    i = curved[0]
    a, b, tip = vertices[i], vertices[(i + 1) % 3], vertices[(i + 2) % 3]
    tol = max(radius * 1e-5, 1e-7)
    if any(abs(math.dist(p[:2], center) - radius) > tol for p in (a, b)):
        return None
    arc_center, _, _, arc_radius = bulge_to_arc(a[:2], b[:2], a[2])
    if math.dist(arc_center, center) > tol or abs(arc_radius - radius) > tol:
        return None
    if abs(math.dist(a[:2], tip[:2]) - math.dist(b[:2], tip[:2])) > tol:
        return None
    if math.dist(tip[:2], center) <= radius + tol:
        return None
    mid = [(a[k] + b[k]) / 2 for k in (0, 1)]
    radial, bisector = _unit(center, tip), _unit(center, mid)
    if radial is None or bisector is None or math.dist(radial, bisector) > 1e-5:
        return None
    return list(tip[:2])


def _five_vertex_tip(vertices, center, radius):
    """Open symmetric chevron: inner base, outer wing, apex, wing, base.

    Both inner ends lie on the circle diameter; all four base points lie
    on that diameter. This is not a generic longest-vertex heuristic.
    """
    if len(vertices) != 5 or any(not math.isfinite(v) for p in vertices for v in p):
        return None
    if any(abs(p[2]) > 1e-9 for p in vertices):
        return None
    tip = vertices[2][:2]
    axis = _unit(center, tip)
    tol = max(radius * 1e-5, 1e-7)
    if axis is None or math.dist(center, tip) <= radius + tol:
        return None
    offsets = []
    for p in (vertices[0], vertices[1], vertices[3], vertices[4]):
        delta = [p[i] - center[i] for i in (0, 1)]
        if abs(sum(delta[i] * axis[i] for i in (0, 1))) > tol:
            return None
        offsets.append(delta[0] * (-axis[1]) + delta[1] * axis[0])
    a, b, c, d = offsets
    if (
        abs(abs(a) - radius) > tol
        or abs(abs(d) - radius) > tol
        or abs(a + d) > tol
        or abs(b + c) > tol
        or a * b <= 0
        or abs(b) <= radius + tol
    ):
        return None
    return list(tip)


def extract_index_directions(
    doc,
    *,
    source_file_id: str,
    layout_name: str,
    max_inserts: int = 2000,
    max_block_entities: int = 256,
):
    """Inspect only a named native layout; never read human rows or dimensions.

    Current family: circular callout plus filled, curved-base radial arrowhead.
    Native INSERT matrix supplies rotation, base point and reflection. Geometry
    is in layout WCS. A separate viewport mapping is required for model use.
    """
    if not source_file_id or max_inserts < 1 or max_block_entities < 1:
        raise ValueError("source identity and positive resource limits are required")
    layout = doc.layouts.get(layout_name)
    records, truncated = [], False
    examined = 0
    for insert in layout.query("INSERT"):
        examined += 1
        if examined > max_inserts:
            truncated = True
            break
        attrs = [a for a in insert.attribs if _visible(a, doc)]
        pages = [
            a
            for a in attrs
            if a.dxf.tag.upper().rstrip(".") == "DWG_NO"
            or re.fullmatch(r"\d+\.\d+-[A-Z]\d+", a.dxf.tag.upper())
        ]
        views = [
            a
            for a in attrs
            if a.dxf.tag.upper().rstrip(".") in {"NO", "NUM"}
            or re.fullmatch(r"E\d+", a.dxf.tag.upper())
        ]
        # No proximity join, and no forced page-code rewrite between code families.
        if not any(
            re.fullmatch(r"(?:[A-Z0-9]+-)*(?:EL|E|QS|DE|DT|D)-?\d{1,3}", a.dxf.text.strip(), re.I)
            for a in pages
        ):
            continue
        row = {
            "source_file_id": source_file_id,
            "layout": layout_name,
            "space": "model" if layout_name == "Model" else "paper:" + layout_name,
            "parent_insert_handle": insert.dxf.handle,
            "block_name": insert.dxf.name,
            "attribute_handles": [a.dxf.handle for a in [*pages, *views]],
            "raw_page_values": [a.dxf.text for a in pages],
            "raw_view_values": [a.dxf.text for a in views],
            "page_code": None,
            "view_number": None,
            "center": None,
            "tip": None,
            "arrow_vector": None,
            "geometry_state": "UNRESOLVED",
            "state": "REVIEW",
            "reason_codes": [],
            "physical_component_id": None,
            "physical_quantity": None,
            "view_binding_confirmed": False,
        }
        records.append(row)
        if len(pages) != 1 or len(views) != 1 or not re.fullmatch(r"\d{1,3}", views[0].dxf.text):
            row["reason_codes"].append("AMBIGUOUS_OR_MISSING_SAME_PARENT_ATTRIBUTES")
            continue
        row.update(page_code=pages[0].dxf.text.strip().upper(), view_number=views[0].dxf.text)
        if not _visible(insert, doc):
            row["reason_codes"].append("HIDDEN_INSERT")
            continue
        block = doc.blocks.get(insert.dxf.name)
        if block is None or len(block) > max_block_entities:
            row["reason_codes"].append("MISSING_OR_OVERSIZED_BLOCK")
            continue
        entities = [e for e in block if _visible(e, doc)]
        if any(e.dxftype() == "INSERT" for e in entities):
            row["reason_codes"].append("NESTED_SYMBOL_UNSUPPORTED")
            continue
        circles = [e for e in entities if e.dxftype() in {"ARC", "CIRCLE"}]
        if not circles:
            row["reason_codes"].append("CIRCULAR_CALLOUT_MISSING")
            continue
        center, radius = _point(circles[0].dxf.center), float(circles[0].dxf.radius)
        if (
            not all(math.isfinite(v) for v in [*center, radius])
            or radius <= 0
            or any(
                math.dist(_point(e.dxf.center), center) > radius * 1e-5
                or abs(e.dxf.radius - radius) > radius * 1e-5
                for e in circles
            )
        ):
            row["reason_codes"].append("AMBIGUOUS_CIRCULAR_CALLOUT")
            continue
        graphics = [*circles, *(e for e in entities if e.dxftype() in {"HATCH", "LWPOLYLINE"})]
        if (
            Vec3(insert.dxf.extrusion).distance(Vec3(0, 0, 1)) > 1e-7
            or any(Vec3(e.dxf.extrusion).distance(Vec3(0, 0, 1)) > 1e-7 for e in graphics)
            or any(abs(e.dxf.center.z) > 1e-7 for e in circles)
            or any(abs(e.dxf.elevation.z) > 1e-7 for e in graphics if e.dxftype() == "HATCH")
            or any(abs(e.dxf.elevation) > 1e-7 for e in graphics if e.dxftype() == "LWPOLYLINE")
        ):
            row["reason_codes"].append("NON_PLANAR_SYMBOL_UNSUPPORTED")
            continue
        tips = []
        for hatch in (e for e in entities if e.dxftype() == "HATCH" and e.dxf.solid_fill):
            for path_no, path in enumerate(hatch.paths):
                vertices = getattr(path, "vertices", None)
                if vertices is None or not getattr(path, "is_closed", False):
                    continue
                tip = _wedge_tip(vertices, center, radius)
                if tip is not None:
                    tips.append((tip, hatch.dxf.handle, path_no, "HATCH"))
        for poly in (e for e in entities if e.dxftype() == "LWPOLYLINE"):
            tip = _five_vertex_tip(list(poly.get_points("xyb")), center, radius)
            if tip is not None:
                tips.append((tip, poly.dxf.handle, None, "LWPOLYLINE"))
        if len(tips) != 1:
            row["reason_codes"].append("NO_UNIQUE_SUPPORTED_RADIAL_ARROW")
            continue
        matrix = insert.matrix44()
        mapped_center = _point(matrix.transform(Vec3(*center)))
        mapped_tip = _point(matrix.transform(Vec3(*tips[0][0])))
        vector = _unit(mapped_center, mapped_tip)
        if vector is None:
            row["reason_codes"].append("DEGENERATE_INSERT_TRANSFORM")
            continue
        row.update(
            center=mapped_center,
            tip=mapped_tip,
            arrow_vector=vector,
            geometry_state="GEOMETRY_RESOLVED",
            geometry_evidence={
                "circle_handles": [e.dxf.handle for e in circles],
                "hatch_handle": tips[0][1] if tips[0][3] == "HATCH" else None,
                "arrow_entity_handle": tips[0][1],
                "arrow_entity_type": tips[0][3],
                "boundary_path_index": tips[0][2],
                "local_center": center,
                "local_tip": tips[0][0],
                "insert_matrix": list(matrix),
                "method": (
                    "same_block_circular_radial_wedge_v1"
                    if tips[0][3] == "HATCH"
                    else "same_block_circular_five_vertex_chevron_v1"
                ),
            },
        )
    return {
        "schema_version": "native-elevation-index-directions/1.0",
        "path_scope": "local_run_diagnostics",
        "state": "REVIEW",
        "numeric_gold_accessed": False,
        "mutates_takeoff": False,
        "layout": layout_name,
        "source_file_id": source_file_id,
        "truncated": truncated,
        "inserts_examined": min(examined, max_inserts),
        "limits": {"max_inserts": max_inserts, "max_block_entities": max_block_entities},
        "summary": dict(Counter(r["geometry_state"] for r in records)),
        "records": records,
    }


def project_index_direction(record, viewport, *, native_viewport=None):
    """Map a record using the ORIGINAL viewport, never a cropped panel bbox.

    This creates retrieval evidence only. Competing viewports must be retained
    separately by the caller; this helper does not pick the nearest viewport.
    """
    from .panels import _PaperToModelTransform

    row = deepcopy(record)
    row.update(
        model_center=None,
        model_tip=None,
        model_arrow_vector=None,
        projection_state="UNRESOLVED",
        viewport_handle=viewport.handle,
    )

    def reject(reason):
        row["reason_codes"].append(reason)
        return row

    if (
        viewport.entity_type != "VIEWPORT"
        or viewport.source_file_id != row["source_file_id"]
        or viewport.space != row["space"]
    ):
        return reject("VIEWPORT_SOURCE_OR_SPACE_MISMATCH")
    if row["geometry_state"] != "GEOMETRY_RESOLVED":
        return reject("ARROW_GEOMETRY_UNRESOLVED")
    if native_viewport is not None:
        return _project_native(row, viewport, native_viewport, reject)
    box = viewport.geometry.get("model_bbox")
    shifted = viewport.geometry.get("model_bbox_target_shifted")
    if box is None or (shifted and math.dist(shifted, box) > 1e-6):
        return reject("NO_UNIQUE_ORIGINAL_VIEWPORT_MAPPING")
    direction = viewport.geometry.get("view_direction_vector", [0, 0, 1])
    if len(direction) != 3 or direction[2] <= 0:
        return reject("UNSUPPORTED_VIEWPORT_DIRECTION")
    transform = _PaperToModelTransform.build(viewport, box)
    if transform is None:
        return reject("UNSUPPORTED_VIEWPORT_TRANSFORM")
    pb = transform.paper_box
    if any(
        not (pb[0] <= p[0] <= pb[2] and pb[1] <= p[1] <= pb[3]) for p in [row["center"], row["tip"]]
    ):
        return reject("SYMBOL_OUTSIDE_VIEWPORT")
    center, tip = transform.point(row["center"]), transform.point(row["tip"])
    row.update(
        model_center=center,
        model_tip=tip,
        model_arrow_vector=_unit(center, tip),
        projection_state="GEOMETRY_RESOLVED",
        projection_basis={
            "paper_bbox": list(pb),
            "original_model_bbox": list(box),
            "scale_x": transform.scale_x,
            "scale_y": transform.scale_y,
        },
    )
    return row


def _project_native(row, indexed, native, reject):
    """Use one native inverse; never add view_target a second time."""
    if (
        native.dxftype() != "VIEWPORT"
        or native.dxf.handle != indexed.handle
        or "paper:" + native.get_layout().name != row["space"]
    ):
        return reject("NATIVE_VIEWPORT_IDENTITY_MISMATCH")
    direction = Vec3(native.dxf.view_direction_vector)
    if (
        direction.z <= 0
        or abs(direction.x) > 1e-7
        or abs(direction.y) > 1e-7
        or native.dxf.flags & 1
        or native.dxf.id <= 1
    ):
        return reject("UNSUPPORTED_NATIVE_VIEWPORT")
    c, w, h = native.dxf.center, native.dxf.width, native.dxf.height
    pb = [c.x - w / 2, c.y - h / 2, c.x + w / 2, c.y + h / 2]
    if not all(math.isfinite(v) for v in pb) or min(w, h) <= 0:
        return reject("INVALID_NATIVE_VIEWPORT")
    if any(
        not (pb[0] <= p[0] <= pb[2] and pb[1] <= p[1] <= pb[3]) for p in [row["center"], row["tip"]]
    ):
        return reject("SYMBOL_OUTSIDE_VIEWPORT")
    try:
        forward = native.get_transformation_matrix()
        inverse = forward.copy()
        inverse.inverse()
        points = [inverse.transform(Vec3(*p)) for p in [row["center"], row["tip"]]]
        residual = max(
            math.dist(_point(forward.transform(p)), original)
            for p, original in zip(points, [row["center"], row["tip"]], strict=True)
        )
        if not math.isfinite(residual) or residual > 1e-6:
            return reject("NATIVE_PROJECTION_ROUNDTRIP_FAILED")
    except (ValueError, ZeroDivisionError, ArithmeticError):
        return reject("INVALID_NATIVE_VIEWPORT_MATRIX")
    center, tip = [_point(p) for p in points]
    row.update(
        model_center=center,
        model_tip=tip,
        model_arrow_vector=_unit(center, tip),
        projection_state="GEOMETRY_RESOLVED",
        projection_basis={
            "method": "native_viewport_inverse_v1",
            "paper_bbox": pb,
            "inverse_matrix": list(inverse),
            "roundtrip_residual": residual,
            "legacy_shifted_bbox_used": False,
        },
    )
    return row
