"""Native, endpoint-connected section-symbol geometry, always REVIEW evidence.

This module does not identify material, drawing role, dimensions or quantity.
The caller must review object handles and authenticate the source document. A
positive result proves only that one native symbol path properly crosses the
selected native object geometry. A bounding box never supplies that proof.
"""

from __future__ import annotations

import hashlib
import json
import math
from collections import defaultdict

from ezdxf import bbox as ezbbox
from ezdxf.math import Matrix44, Vec3

from .component_geometry import _effective_layer
from .native_paper_render import native_viewport_region


def _record(value):
    return value.model_dump(mode="python") if hasattr(value, "model_dump") else dict(value)


def _id(kind, value):
    digest = hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()[:24]
    return f"native-cut-{kind}:{digest}"


def _sub(a, b):
    return a[0] - b[0], a[1] - b[1]


def _dot(a, b):
    return a[0] * b[0] + a[1] * b[1]


def _cross(a, b):
    return a[0] * b[1] - a[1] * b[0]


def _unit(a):
    length = math.hypot(*a)
    return (a[0] / length, a[1] / length) if length else (0.0, 0.0)


def _mid(a, b):
    return [(a[0] + b[0]) / 2, (a[1] + b[1]) / 2]


def _inside_box(point, box, tolerance):
    return (
        box[0] - tolerance <= point[0] <= box[2] + tolerance
        and box[1] - tolerance <= point[1] <= box[3] + tolerance
    )


def _triangle(points, tolerance):
    """Recognize a closed, elongated, approximately symmetric native triangle.

    Recognition precedes INSERT scaling, so mirrored and anisotropic blocks
    retain their original arrow tip rather than picking a new shortest edge.
    """
    unique = []
    for point in points:
        if not any(math.dist(point[:2], old[:2]) <= tolerance for old in unique):
            unique.append(point)
    if len(unique) != 3:
        return None
    choices = []
    for index, tip in enumerate(unique):
        base = [p for j, p in enumerate(unique) if j != index]
        midpoint = _mid(*base)
        axis, edge = _sub(tip, midpoint), _sub(base[1], base[0])
        height, width = math.hypot(*axis), math.hypot(*edge)
        if width <= tolerance or height < width * 0.75:
            continue
        if abs(_dot(axis, edge)) > height * width * 0.12:
            continue
        choices.append((tip, midpoint))
    return choices[0] if len(choices) == 1 else None


def _intersection(a, b, c, d, tolerance):
    """Return exact linear crossing parameters; endpoint and overlap are touches."""
    u, v, w = _sub(b, a), _sub(d, c), _sub(c, a)
    lu, lv = math.hypot(*u), math.hypot(*v)
    if min(lu, lv) <= tolerance:
        return None
    denominator = _cross(u, v)
    if abs(denominator) <= lu * lv * 1e-12:
        if abs(_cross(w, u)) > tolerance * lu:
            return None
        start, end = sorted((_dot(w, u) / lu**2, _dot(_sub(d, a), u) / lu**2))
        if max(0, start) <= min(1, end) + tolerance / lu:
            return {"kind": "COLLINEAR_BOUNDARY_TOUCH", "point": None}
        return None
    t, s = _cross(w, v) / denominator, _cross(w, u) / denominator
    et, es = tolerance / lu, tolerance / lv
    if not (-et <= t <= 1 + et and -es <= s <= 1 + es):
        return None
    proper = et < t < 1 - et and es < s < 1 - es
    return {
        "kind": "PROPER_CROSSING" if proper else "ENDPOINT_BOUNDARY_TOUCH",
        "point": [a[0] + t * u[0], a[1] + t * u[1]],
        "path_parameter": t,
        "object_parameter": s,
    }


def _strict_inside(point, polygon, tolerance):
    inside = False
    for a, b in zip(polygon, polygon[1:] + polygon[:1], strict=True):
        edge = _sub(b, a)
        length = math.hypot(*edge)
        if (
            length
            and abs(_cross(_sub(point, a), edge)) <= tolerance * length
            and _dot(_sub(point, a), _sub(point, b)) <= tolerance**2
        ):
            return False
        if (a[1] > point[1]) != (b[1] > point[1]) and point[0] < (b[0] - a[0]) * (
            point[1] - a[1]
        ) / (b[1] - a[1]) + a[0]:
            inside = not inside
    return inside


class _Collector:
    def __init__(
        self,
        document,
        tolerance,
        max_entities,
        max_segments,
        max_depth,
        frozen=(),
        *,
        output_plane_z=0.0,
    ):
        self.doc = document
        self.tolerance = tolerance
        self.max_entities = max_entities
        self.max_segments = max_segments
        self.max_depth = max_depth
        self.frozen = {str(name).casefold() for name in frozen}
        # None is permitted only for a reviewed object's native constant plane
        # under an independently validated parallel top-view projection.
        self.output_plane_z = output_plane_z
        self.visited = 0
        self.segments = []
        self.polygons = []
        self.arrows = []
        self.arcs = []
        self.circular_markers = []
        self.leader_pointer_terminals = []
        self.limitations = []
        self.exclusions = []

    def planar(self, points):
        if not points or not all(math.isfinite(v) for point in points for v in point):
            return False
        if self.output_plane_z is None:
            self.output_plane_z = points[0].z
        return all(abs(point.z - self.output_plane_z) <= self.tolerance for point in points)

    def issue(self, code, entity, root, *, blocking=True, matrix=None):
        record = {
            "code": code,
            "handle": entity.dxf.get("handle"),
            "root_handle": root,
            "entity_type": entity.dxftype(),
        }
        if matrix is not None:
            record["matrix_to_output"] = list(matrix)
            try:
                # Some opaque DXF records expose native positional bounds even
                # though ezdxf cannot render or copy their embedded content.
                bounds = (
                    entity.bbox()
                    if entity.dxftype() == "OLE2FRAME"
                    else ezbbox.extents([entity], fast=False)
                )
                if bounds.has_data:
                    corners = [
                        matrix.transform((x, y, z))
                        for x in (bounds.extmin.x, bounds.extmax.x)
                        for y in (bounds.extmin.y, bounds.extmax.y)
                        for z in (bounds.extmin.z, bounds.extmax.z)
                    ]
                    if all(math.isfinite(v) for p in corners for v in p):
                        record["output_bbox"] = [
                            min(p.x for p in corners),
                            min(p.y for p in corners),
                            max(p.x for p in corners),
                            max(p.y for p in corners),
                        ]
                        record["exclusion_bounds_basis"] = (
                            "original_DXF_bounds_transformed_by_native_INSERT_and_viewport_matrices"
                        )
            except Exception:
                pass
        (self.limitations if blocking else self.exclusions).append(record)

    def visible(self, entity, inherited=None):
        layer_name = _effective_layer(entity.dxf.get("layer", "0"), inherited)
        try:
            layer = self.doc.layers.get(layer_name)
            return not (
                entity.dxf.get("invisible", 0)
                or entity.dxftype() in {"ATTRIB", "ATTDEF"}
                and entity.dxf.get("flags", 0) & 1
                or layer.is_off()
                or layer.is_frozen()
                or layer_name.casefold() in self.frozen
            )
        except Exception:
            return False

    def walk(self, entity, *, root, matrix=None, chain=(), inherited=None, target=False, blocks=()):
        if self.visited >= self.max_entities:
            self.issue("SOURCE_ENTITY_CAP", entity, root)
            return
        self.visited += 1
        matrix = matrix if matrix is not None else Matrix44()
        kind = entity.dxftype()
        if kind in {"TEXT", "MTEXT", "ATTRIB", "ATTDEF", "SEQEND", "VIEWPORT", "POINT"}:
            return
        layer = _effective_layer(entity.dxf.get("layer", "0"), inherited)
        try:
            self.doc.layers.get(layer)
        except Exception:
            self.issue("NATIVE_LAYER_VISIBILITY_UNKNOWN", entity, root, matrix=matrix)
            return
        if not self.visible(entity, inherited):
            self.issue("HIDDEN_OR_FROZEN_NATIVE_GEOMETRY", entity, root, blocking=target)
            return
        if kind == "INSERT":
            if len(chain) >= self.max_depth:
                self.issue("BLOCK_RECURSION_CAP", entity, root)
                return
            if entity.dxf.name in blocks:
                self.issue("RECURSIVE_BLOCK", entity, root)
                return
            if entity.mcount != 1:
                self.issue("MINSERT_UNSUPPORTED", entity, root)
                return
            if entity.has_extension_dict and "ACAD_FILTER" in entity.extension_dict:
                self.issue("INSERT_CLIPPING_UNSUPPORTED", entity, root, matrix=matrix)
                return
            block = entity.block()
            if block is None or block.block.is_xref:
                self.issue("MISSING_OR_XREF_BLOCK", entity, root, matrix=matrix)
                return
            try:
                local_matrix = entity.matrix44()
                combined = local_matrix @ matrix
                if not all(math.isfinite(x) for x in combined):
                    raise ValueError("nonfinite matrix")
                origin = combined.transform((0, 0, 0))
                if any(
                    abs(combined.transform(axis).z - origin.z) > self.tolerance
                    for axis in ((1, 0, 0), (0, 1, 0))
                ):
                    self.issue("INSERT_PLANE_TILT_UNSUPPORTED", entity, root, matrix=matrix)
                    return
                provenance = {
                    "handle": entity.dxf.handle,
                    "block_name": entity.dxf.name,
                    "matrix_to_parent": list(local_matrix),
                    "matrix_to_output": list(combined),
                }
                for ordinal, child in enumerate(block):
                    self.walk(
                        child,
                        root=root,
                        matrix=combined,
                        chain=(*chain, provenance),
                        inherited=layer,
                        target=target,
                        blocks=(*blocks, entity.dxf.name),
                    )
                    if self.visited >= self.max_entities or len(self.segments) >= self.max_segments:
                        if ordinal + 1 < len(block):
                            self.issue(
                                "SOURCE_ENTITY_CAP"
                                if self.visited >= self.max_entities
                                else "NATIVE_SEGMENT_CAP",
                                entity,
                                root,
                            )
                        break
            except Exception:
                self.issue("BLOCK_TRANSFORM_UNSUPPORTED", entity, root)
            return
        if kind == "LEADER":
            # Use original vertices and the native DIMSTYLE/override graphics.
            # This includes hook lines and standard closed arrowheads instead
            # of treating the leader's large overall bbox as its actual path.
            if int(entity.dxf.path_type) != 0:
                self.issue("CURVED_NATIVE_LEADER_UNSUPPORTED", entity, root, matrix=matrix)
                return
            if not entity.dxf.normal_vector.isclose(Vec3(0, 0, 1)):
                self.issue("LEADER_PLANE_UNSUPPORTED", entity, root, matrix=matrix)
                return
            try:
                self.doc.dimstyles.get(entity.dxf.dimstyle)
                override = entity.override()
                native_style = {
                    key: override.get(key)
                    for key in (
                        "dimtad",
                        "dimgap",
                        "dimscale",
                        "dimclrd",
                        "dimltype",
                        "dimlwd",
                        "dimldrblk",
                        "dimasz",
                    )
                }
                graphics = list(entity.virtual_entities())
                if not graphics:
                    raise ValueError("native LEADER has no graphic entities")
                metadata = {
                    "handle": entity.dxf.handle,
                    "entity_type": "LEADER",
                    "leader_native_vertices": [list(p) for p in entity.vertices],
                    "leader_dimstyle": entity.dxf.dimstyle,
                    "leader_effective_style": native_style,
                    "leader_native_settings": {
                        key: entity.dxf.get(key)
                        for key in (
                            "has_arrowhead",
                            "path_type",
                            "annotation_type",
                            "has_hookline",
                            "hookline_direction",
                            "text_height",
                            "text_width",
                            "annotation_handle",
                        )
                    },
                    "leader_graphics_basis": (
                        "native_straight_vertices_and_DIMSTYLE_override_graphics"
                    ),
                }
                initial_arrows = len(self.arrows)
                for ordinal, graphic in enumerate(graphics):
                    if graphic.dxftype() not in {
                        "LINE",
                        "SOLID",
                        "TRACE",
                        "LWPOLYLINE",
                        "POLYLINE",
                    }:
                        self.issue("LEADER_GRAPHIC_TYPE_UNSUPPORTED", entity, root, matrix=matrix)
                        return
                    tracked = (
                        (self.segments, len(self.segments), "segment"),
                        (self.arrows, len(self.arrows), "arrow"),
                        (self.polygons, len(self.polygons), None),
                        (self.limitations, len(self.limitations), None),
                        (self.exclusions, len(self.exclusions), None),
                    )
                    self.walk(
                        graphic,
                        root=root,
                        matrix=matrix,
                        chain=chain,
                        inherited=layer,
                        target=target,
                        blocks=blocks,
                    )
                    for records, start, identifier in tracked:
                        for record in records[start:]:
                            record.update(
                                metadata,
                                native_graphic_type=graphic.dxftype(),
                                leader_graphic_index=ordinal,
                            )
                            if identifier:
                                record.pop("id", None)
                                record["id"] = _id(identifier, record)
                    if self.visited >= self.max_entities or len(self.segments) >= self.max_segments:
                        if ordinal + 1 < len(graphics):
                            self.issue(
                                "SOURCE_ENTITY_CAP"
                                if self.visited >= self.max_entities
                                else "NATIVE_SEGMENT_CAP",
                                entity,
                                root,
                            )
                        break
                if entity.dxf.has_arrowhead:
                    first = matrix.transform(entity.vertices[0])
                    second = matrix.transform(entity.vertices[1])
                    if self.planar([first, second]):
                        pointer = {
                            **metadata,
                            "root_handle": root,
                            "output_point": [first.x, first.y],
                            "output_plane_z": self.output_plane_z,
                            "matrix_to_output": list(matrix),
                            "insert_chain": list(chain),
                            "direction": list(_unit((first.x - second.x, first.y - second.y))),
                            "closed_graphic_arrowhead_recognized": len(self.arrows)
                            > initial_arrows,
                            "basis": "native_LEADER_has_arrowhead_first_vertex_terminal_only",
                            "usable_as_source_symbol": False,
                        }
                        pointer["id"] = _id("leader-pointer", pointer)
                        self.leader_pointer_terminals.append(pointer)
            except Exception:
                self.issue("NATIVE_LEADER_GRAPHICS_UNRESOLVED", entity, root, matrix=matrix)
            return
        points, closed = [], False
        try:
            if kind == "LINE":
                points = [entity.dxf.start, entity.dxf.end]
            elif kind in {"LWPOLYLINE", "POLYLINE"}:
                if entity.has_width:
                    self.issue("POLYLINE_WIDTH_GEOMETRY_UNSUPPORTED", entity, root, matrix=matrix)
                    return
                if kind == "POLYLINE" and not entity.is_2d_polyline:
                    self.issue("POLYLINE_3D_UNSUPPORTED", entity, root, matrix=matrix)
                    return
                parts = list(entity.virtual_entities())
                if not parts or any(part.dxftype() != "LINE" for part in parts):
                    self.issue("CURVED_POLYLINE_UNSUPPORTED", entity, root, matrix=matrix)
                    return
                points = [parts[0].dxf.start, *[part.dxf.end for part in parts]]
                closed = bool(entity.closed if kind == "LWPOLYLINE" else entity.is_closed)
                if closed and math.dist(points[0], points[-1]) <= self.tolerance:
                    points.pop()
            elif kind == "ARC":
                center = entity.ocs().to_wcs(entity.dxf.center)
                if not entity.dxf.extrusion.isclose(Vec3(0, 0, 1)):
                    self.issue("ARC_PLANE_UNSUPPORTED", entity, root, matrix=matrix)
                    return
                radius = float(entity.dxf.radius)
                start = math.radians(float(entity.dxf.start_angle)) % math.tau
                sweep = (
                    math.radians(float(entity.dxf.end_angle) - float(entity.dxf.start_angle))
                    % math.tau
                )
                if not math.isfinite(radius) or radius <= 0 or sweep <= 1e-12:
                    self.issue("ARC_GEOMETRY_INVALID", entity, root, matrix=matrix)
                    return
                arc_plane_points = [
                    matrix.transform(point)
                    for point in (
                        center,
                        (center.x + radius, center.y, center.z),
                        (center.x, center.y + radius, center.z),
                    )
                ]
                if not self.planar(arc_plane_points):
                    self.issue("ARC_PLANE_UNSUPPORTED", entity, root, matrix=matrix)
                    return
                self.arcs.append(
                    {
                        "root_handle": root,
                        "handle": entity.dxf.handle,
                        "entity_type": kind,
                        "layer": layer,
                        "insert_chain": list(chain),
                        "matrix_to_output": list(matrix),
                        "native_center": list(center),
                        "native_radius": radius,
                        "start_angle_radians": start,
                        "sweep_radians": sweep,
                        "output_plane_z": self.output_plane_z,
                    }
                )
                self.issue("UNSUPPORTED_NATIVE_ARC", entity, root, matrix=matrix)
                return
            elif kind in {"SOLID", "TRACE"}:
                points = list(entity.wcs_vertices(close=False))
                closed = True
            else:
                # Closed circles and dimensions are explicit non-path graphics.
                # Other unsupported native shapes may hide a continuation and
                # need a scoped native-bound exclusion, never a type-based guess.
                self.issue(
                    "UNSUPPORTED_NATIVE_" + kind,
                    entity,
                    root,
                    blocking=target or kind not in {"CIRCLE", "DIMENSION"},
                    matrix=matrix,
                )
                return
            points = [Vec3(p) for p in points]
            native_triangle = (
                _triangle([tuple(p) for p in points], self.tolerance) if closed else None
            )
            world3 = [matrix.transform(point) for point in points]
            if not self.planar(world3):
                self.issue("NONPLANAR_OR_NONFINITE_GEOMETRY", entity, root, matrix=matrix)
                return
            world = [[p.x, p.y] for p in world3]
            provenance = {
                "root_handle": root,
                "handle": entity.dxf.handle,
                "entity_type": kind,
                "layer": layer,
                "insert_chain": list(chain),
                "matrix_to_output": list(matrix),
                "native_points": [[p.x, p.y, p.z] for p in points],
                "output_plane_z": self.output_plane_z,
            }
            if native_triangle:
                tip3 = matrix.transform(native_triangle[0])
                base3 = matrix.transform((*native_triangle[1], points[0].z))
                arrow = {
                    **provenance,
                    "tip": [tip3.x, tip3.y],
                    "base_midpoint": [base3.x, base3.y],
                    "closed_vertices": world,
                }
                arrow["direction"] = list(_unit(_sub(arrow["tip"], arrow["base_midpoint"])))
                arrow["id"] = _id("arrow", arrow)
                self.arrows.append(arrow)
                if not target:
                    return
            if closed and not target:
                # Closed label frames are not cutting-line edges.
                self.issue("CLOSED_NON_ARROW_EXCLUDED_FROM_PATH", entity, root, blocking=False)
                return
            if closed:
                self.polygons.append({**provenance, "points": world})
            pairs = list(zip(world, world[1:], strict=False))
            if closed:
                pairs.append((world[-1], world[0]))
            for ordinal, (a, b) in enumerate(pairs):
                if math.dist(a, b) <= self.tolerance:
                    continue
                if len(self.segments) >= self.max_segments:
                    self.issue("NATIVE_SEGMENT_CAP", entity, root)
                    return
                segment = {**provenance, "segment_index": ordinal, "start": a, "end": b}
                segment["id"] = _id("segment", segment)
                self.segments.append(segment)
        except Exception:
            self.issue("NATIVE_GEOMETRY_EXTRACTION_FAILED", entity, root, matrix=matrix)


def _same_instance(left, right):
    return (
        left["root_handle"] == right["root_handle"]
        and left["matrix_to_output"] == right["matrix_to_output"]
        and [e["handle"] for e in left.get("insert_chain", [])]
        == [e["handle"] for e in right.get("insert_chain", [])]
    )


def _recognize_circular_markers(collector):
    """Require visible native arc coverage and one actual diameter in one INSERT."""
    groups = []
    for arc in collector.arcs:
        if not arc["insert_chain"]:
            continue
        found = next(
            (
                g
                for g in groups
                if _same_instance(g[0], arc)
                and math.dist(g[0]["native_center"], arc["native_center"]) <= 1e-8
                and math.isclose(
                    g[0]["native_radius"], arc["native_radius"], rel_tol=1e-10, abs_tol=1e-8
                )
            ),
            None,
        )
        if found is None:
            groups.append([arc])
        else:
            found.append(arc)
    consumed_diameters, consumed_arcs = set(), set()
    for arcs in groups:
        if len(arcs) < 2:
            continue
        intervals = []
        for arc in arcs:
            start, end = (
                arc["start_angle_radians"],
                arc["start_angle_radians"] + arc["sweep_radians"],
            )
            intervals.extend(
                [(start, min(end, math.tau))] + ([(0, end - math.tau)] if end > math.tau else [])
            )
        end = -1.0
        disjoint = True
        for lo, hi in sorted(intervals):
            if lo < end - 1e-8:
                disjoint = False
                break
            end = hi
        if not disjoint:
            continue
        exemplar = arcs[0]
        center, radius = exemplar["native_center"], exemplar["native_radius"]
        matrix = Matrix44(exemplar["matrix_to_output"])
        diameter_candidates = []
        for segment in collector.segments:
            if segment["entity_type"] != "LINE" or not _same_instance(exemplar, segment):
                continue
            a, b = segment["native_points"]
            if math.dist(_mid(a, b), center[:2]) > 1e-8:
                continue
            if max(abs(math.dist(p[:2], center[:2]) - radius) for p in (a, b)) > 1e-8:
                continue
            if not all(
                any(
                    (math.atan2(p[1] - center[1], p[0] - center[0]) - arc["start_angle_radians"])
                    % math.tau
                    <= arc["sweep_radians"] + 1e-9
                    for arc in arcs
                )
                for p in (a, b)
            ):
                continue
            diameter_candidates.append(segment)
        if len(diameter_candidates) != 1:
            continue
        diameter = diameter_candidates[0]
        output_center = matrix.transform(center)
        axis_x = matrix.transform((center[0] + radius, center[1], center[2])) - output_center
        axis_y = matrix.transform((center[0], center[1] + radius, center[2])) - output_center
        if (
            max(abs(output_center.z - exemplar["output_plane_z"]), abs(axis_x.z), abs(axis_y.z))
            > collector.tolerance
        ):
            continue
        dx, dy = math.hypot(axis_x.x, axis_y.x), math.hypot(axis_x.y, axis_y.y)
        marker = {
            "family": "CIRCULAR_DIVIDER",
            "root_handle": exemplar["root_handle"],
            "native_center": center,
            "native_radius": radius,
            "output_center": [output_center.x, output_center.y],
            "output_plane_z": exemplar["output_plane_z"],
            "matrix_to_output": exemplar["matrix_to_output"],
            "insert_chain": exemplar["insert_chain"],
            "arcs": arcs,
            "diameter_segment": diameter,
            "boundary_bounds_corners": [
                [output_center.x - dx, output_center.y - dy],
                [output_center.x + dx, output_center.y + dy],
            ],
            "basis": (
                "visible_nonoverlapping_same_native_circle_ARC_sweeps_with_"
                "unique_native_diameter_LINE_endpoints_on_visible_sweeps"
            ),
            "arc_gaps_are_not_bridged": True,
        }
        marker["id"] = _id("circular-marker", marker)
        collector.circular_markers.append(marker)
        consumed_diameters.add(diameter["id"])
        for arc in arcs:
            consumed_arcs.add((arc["root_handle"], arc["handle"], tuple(arc["matrix_to_output"])))
    collector.segments = [s for s in collector.segments if s["id"] not in consumed_diameters]
    collector.limitations = [
        issue
        for issue in collector.limitations
        if not (
            issue["code"] == "UNSUPPORTED_NATIVE_ARC"
            and (issue["root_handle"], issue["handle"], tuple(issue.get("matrix_to_output", [])))
            in consumed_arcs
        )
    ]


def _circle_attachment(marker, point, tolerance):
    matrix = Matrix44(marker["matrix_to_output"])
    inverse = matrix.copy()
    try:
        inverse.inverse()
    except ZeroDivisionError:
        return None
    native = inverse.transform((*point, marker["output_plane_z"]))
    center, radius = marker["native_center"], marker["native_radius"]
    angle = math.atan2(native.y - center[1], native.x - center[0]) % math.tau
    on_circle = matrix.transform(
        (center[0] + radius * math.cos(angle), center[1] + radius * math.sin(angle), center[2])
    )
    if math.dist(point, (on_circle.x, on_circle.y)) > tolerance:
        return None
    arcs = [
        arc
        for arc in marker["arcs"]
        if (angle - arc["start_angle_radians"]) % math.tau <= arc["sweep_radians"] + 1e-9
    ]
    if not arcs:
        return None
    return {
        "kind": "actual_LINE_or_PLINE_endpoint_on_visible_native_ARC_sweep",
        "output_point": list(point),
        "output_plane_z": marker["output_plane_z"],
        "native_point": list(native),
        "native_angle_radians": angle,
        "arc_handles": sorted(arc["handle"] for arc in arcs),
        "diameter_handle": marker["diameter_segment"]["handle"],
    }


def _paths(collector, parent_handle, tolerance, max_paths):
    """Follow only endpoint-to-endpoint edges; never split at crossings."""
    segments = collector.segments
    markers = [
        a
        for a in [*collector.arrows, *collector.circular_markers]
        if a["root_handle"] == parent_handle
    ]
    bins = defaultdict(list)
    for i, segment in enumerate(segments):
        for end, point in enumerate((segment["start"], segment["end"])):
            bins[(math.floor(point[0] / tolerance), math.floor(point[1] / tolerance))].append(
                (i, end, point)
            )

    def incident(point):
        cell = math.floor(point[0] / tolerance), math.floor(point[1] / tolerance)
        found = []
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                for i, end, candidate in bins.get((cell[0] + dx, cell[1] + dy), ()):
                    if math.dist(point, candidate) <= tolerance:
                        found.append((i, end))
        return sorted(set(found))

    paths, reasons = [], []
    if not markers:
        return paths, ["NO_NATIVE_CLOSED_ARROWHEAD"]
    if len(markers) != 1:
        reasons.append("MULTIPLE_SOURCE_SYMBOL_MARKERS")
    for arrow in markers:
        seeds = []
        circle = arrow.get("family") == "CIRCULAR_DIVIDER"
        circle_attachments = []
        if circle:
            for index, segment in enumerate(segments):
                for end, point in enumerate((segment["start"], segment["end"])):
                    attachment = _circle_attachment(arrow, point, tolerance)
                    if attachment is None:
                        continue
                    far = segment["end"] if end == 0 else segment["start"]
                    radial = _sub(point, arrow["output_center"])
                    direction = _sub(far, point)
                    alignment = _dot(_unit(radial), _unit(direction))
                    at_diameter = any(
                        math.dist(point, arrow["diameter_segment"][key]) <= tolerance
                        for key in ("start", "end")
                    )
                    if (at_diameter and alignment > 1e-8) or alignment >= 1 - 1e-8:
                        location = len(circle_attachments)
                        circle_attachments.append(attachment)
                        seeds.append(
                            (
                                index,
                                end,
                                location,
                                "NATIVE_DIAMETER_ENDPOINT_OUTWARD_STEM"
                                if at_diameter
                                else "NATIVE_VISIBLE_ARC_RADIAL_STEM",
                            )
                        )
        for location in () if circle else ("tip", "base_midpoint"):
            for index, end in incident(arrow[location]):
                segment = segments[index]
                if segment["root_handle"] != parent_handle:
                    continue
                far = segment["end"] if end == 0 else segment["start"]
                direction = _unit(_sub(far, arrow[location]))
                alignment = _dot(direction, arrow["direction"])
                # A shaft recedes behind the arrow. A section cutting line can
                # meet its arrow at a right angle; preserve that distinct basis.
                if alignment < -0.985 or abs(alignment) < 0.015:
                    seeds.append(
                        (
                            index,
                            end,
                            location,
                            "BACKWARD_AXIAL_STEM"
                            if alignment < -0.985
                            else "PERPENDICULAR_CUT_STEM",
                        )
                    )
        seeds = list(dict.fromkeys(seeds))
        if not seeds:
            reasons.append(
                "CIRCULAR_MARKER_HAS_NO_NATIVE_OUTWARD_STEM"
                if circle
                else "ARROW_HAS_NO_NATIVE_DIRECTION_CONSTRAINED_STEM"
            )
        if len(seeds) > 1:
            reasons.append("MULTIPLE_DIRECTION_CONSTRAINED_STEMS")
        for index, end, location, basis in seeds:
            initial_point = segments[index]["start"] if end == 0 else segments[index]["end"]
            source_branch = any(i != index for i, _ in incident(initial_point))
            initial_issues = ["SOURCE_ATTACHMENT_BRANCH_AMBIGUITY"] if source_branch else []
            if any(
                (p["root_handle"], p["handle"]) != (arrow["root_handle"], arrow.get("handle"))
                and math.dist(initial_point, p["output_point"]) <= tolerance
                for p in collector.leader_pointer_terminals
            ):
                initial_issues.append("SOURCE_ATTACHMENT_COMPETING_LEADER_POINTER")
            reasons.extend(initial_issues)
            stack = [([(index, end)], {index}, initial_issues)]
            while stack:
                if len(paths) >= max_paths:
                    return paths, sorted(set([*reasons, "PATH_CANDIDATE_CAP"]))
                route, visited, route_issues = stack.pop()
                current, entry = route[-1]
                point = segments[current]["end"] if entry == 0 else segments[current]["start"]
                competitor = [
                    a["id"]
                    for a in collector.arrows
                    if a["id"] != arrow["id"]
                    and any(
                        math.dist(point, a[key]) <= tolerance for key in ("tip", "base_midpoint")
                    )
                ]
                competing_circles = [
                    a["id"]
                    for a in collector.circular_markers
                    if a["id"] != arrow["id"] and _circle_attachment(a, point, tolerance)
                ]
                competitor.extend(competing_circles)
                competing_pointers = [
                    p
                    for p in collector.leader_pointer_terminals
                    if (p["root_handle"], p["handle"])
                    != (arrow["root_handle"], arrow.get("handle"))
                    and math.dist(point, p["output_point"]) <= tolerance
                ]
                competitor.extend(p["id"] for p in competing_pointers)
                next_edges = [(i, e) for i, e in incident(point) if i not in visited]
                if competitor:
                    code = (
                        "PATH_REACHES_OTHER_CIRCULAR_MARKER"
                        if competing_circles
                        else "PATH_REACHES_OTHER_ARROWHEAD"
                    )
                    route_issues = [*route_issues, code]
                    reasons.append(code)
                    next_edges = []
                if len(next_edges) > 1:
                    reasons.append("ENDPOINT_BRANCH_AMBIGUITY")
                    route_issues = [*route_issues, "ENDPOINT_BRANCH_AMBIGUITY"]
                if next_edges:
                    if len(stack) + len(next_edges) + len(paths) > max_paths:
                        return paths, sorted(set([*reasons, "PATH_CANDIDATE_CAP"]))
                    for candidate, candidate_end in reversed(next_edges):
                        stack.append(
                            (
                                [*route, (candidate, candidate_end)],
                                visited | {candidate},
                                route_issues,
                            )
                        )
                    continue
                if any(i in visited and i != current for i, _ in incident(point)):
                    route_issues = [*route_issues, "CLOSED_OR_REJOINED_PATH_AMBIGUITY"]
                    reasons.append("CLOSED_OR_REJOINED_PATH_AMBIGUITY")
                path = {
                    "source_parent_handle": parent_handle,
                    "arrowhead": None if circle else arrow,
                    "symbol_marker": arrow,
                    "stem_attachment": circle_attachments[location] if circle else location,
                    "direction_constraint": basis,
                    "segments": [segments[i] for i, _ in route],
                    "segment_directions": [1 if end == 0 else -1 for _, end in route],
                    "reason_codes": sorted(set(route_issues)),
                    "terminal_point": list(point),
                    "competing_arrow_ids": competitor,
                    "competing_leader_pointer_terminals": competing_pointers,
                }
                path["id"] = _id("path", path)
                paths.append(path)
    if len(paths) > 1:
        reasons.append("MULTIPLE_COMPETING_NATIVE_PATHS")
    return paths, sorted(set(reasons))


def trace_native_cut_paths(
    document,
    *,
    reference_entities,
    native_objects,
    viewport_handle=None,
    endpoint_tolerance=1e-6,
    max_source_entities=100_000,
    max_segments=20_000,
    max_depth=12,
    max_paths=64,
):
    """Return reproducible native geometry evidence with explicit limitations.

    ``reference_entities`` accepts one or more indexed CadEntity objects/dicts,
    all attached ATTRIBs of one original top-level INSERT. ``native_objects``
    accepts reviewed records containing ``id`` and top-level native ``handle``.
    Bboxes are deliberately ignored. Paper references require the actual
    ``viewport_handle`` to map paths into model space. Tolerances are output
    drawing units and are never inferred from text height or an object bbox.
    ``supported`` does not authorize PASS or establish the target drawing role.
    """
    if not math.isfinite(endpoint_tolerance) or endpoint_tolerance <= 0:
        raise ValueError("endpoint_tolerance must be finite and positive")
    if any(
        not isinstance(n, int) or isinstance(n, bool) or n < 1
        for n in (max_source_entities, max_segments, max_depth, max_paths)
    ):
        raise ValueError("native cut path caps must be positive integers")
    result = {
        "schema_version": "native-cut-paths/1",
        "state": "REVIEW",
        "supported": False,
        "complete": False,
        "symbol_recognized": False,
        "symbol_geometry_complete": False,
        "source_arrowhead_count": 0,
        "source_marker_count": 0,
        "source_symbol_evidence_present": False,
        "source_symbol_segment_count": 0,
        "full_layout_scan_complete": False,
        "reason_codes": [],
        "native_symbol_parents": [],
        "paths": [],
        "object_intersections": [],
        "candidate_object_ids": [],
        "crossing_object_ids": [],
        "limitations": [],
        "exclusions": [],
        "coordinate_space": "native_layout_XY",
        "endpoint_tolerance_drawing_units": endpoint_tolerance,
        "scope": "native_geometry_crossing_only",
        "assigns_material_role_quantity_or_pass": False,
    }

    def fail(reason):
        result["reason_codes"] = sorted(set([*result["reason_codes"], reason]))
        return result

    refs = [_record(e) for e in reference_entities]
    objects = [_record(e) for e in native_objects]
    if not refs:
        return fail("SOURCE_REFERENCE_REQUIRED")
    parent_handles = {str(e.get("geometry", {}).get("parent_insert_handle") or "") for e in refs}
    if len(parent_handles) != 1 or not next(iter(parent_handles)):
        return fail("SOURCE_PARENT_NOT_UNIQUE")
    parent_handle = next(iter(parent_handles))
    parent = document.entitydb.get(parent_handle)
    if (
        parent is None
        or not parent.is_alive
        or parent.dxftype() != "INSERT"
        or parent.get_layout() is None
    ):
        return fail("SOURCE_PARENT_NOT_NATIVE_TOP_LEVEL_INSERT")
    layout = parent.get_layout()
    if layout.name.casefold() != "model" and not layout.is_any_paperspace:
        return fail("SOURCE_PARENT_NOT_NATIVE_TOP_LEVEL_INSERT")
    attached = {a.dxf.handle: a for a in parent.attribs}
    visibility = _Collector(
        document, endpoint_tolerance, max_source_entities, max_segments, max_depth
    )
    for ref in refs:
        raw = attached.get(ref.get("handle"))
        if (
            raw is None
            or ref.get("entity_type") != "ATTRIB"
            or document.entitydb.get(ref.get("handle")) is not raw
        ):
            return fail("REFERENCE_NOT_ACTUAL_PARENT_ATTACHED_ATTRIB")
        if not visibility.visible(raw, parent.dxf.layer) or not visibility.visible(parent):
            return fail("HIDDEN_SOURCE_SYMBOL_OR_REFERENCE")
    result["native_symbol_parents"] = [
        {
            "handle": parent_handle,
            "block_name": parent.dxf.name,
            "layout": layout.name,
            "reference_handles": sorted(e["handle"] for e in refs),
            "native_matrix": list(parent.matrix44()),
        }
    ]
    mapping = Matrix44()
    viewport = document.entitydb.get(viewport_handle) if viewport_handle else None
    frozen, model_box = [], None
    if viewport_handle:
        if viewport is None or viewport.dxftype() != "VIEWPORT":
            return fail("NATIVE_VIEWPORT_MISSING")
        try:
            # Reuse the renderer's deliberately bounded native top-view policy.
            region = viewport.get_modelspace_limits()
            transform = native_viewport_region(viewport, region)
            result["native_viewport_transform"] = transform
            model_box = transform["native_model_bbox"]
            frozen = list(viewport.frozen_layers)
            if layout.name.casefold() != "model":
                if viewport.get_layout() is not layout:
                    return fail("SOURCE_VIEWPORT_LAYOUT_MISMATCH")
                mapping = viewport.get_transformation_matrix().copy()
                mapping.inverse()
            result["coordinate_space"] = "model_WCS_XY"
        except Exception as exc:
            return fail(str(exc) if str(exc) else "NATIVE_VIEWPORT_TRANSFORM_UNSUPPORTED")
    elif layout.name.casefold() != "model" and any(
        document.entitydb.get(e.get("handle")) is not None
        and document.entitydb[e["handle"]].get_layout() is not layout
        for e in objects
    ):
        return fail("PAPER_TO_MODEL_REQUIRES_NATIVE_VIEWPORT")
    source_plane_origin = mapping.transform((0, 0, 0))
    if not all(math.isfinite(v) for v in source_plane_origin) or any(
        abs(mapping.transform(axis).z - source_plane_origin.z) > endpoint_tolerance
        for axis in ((1, 0, 0), (0, 1, 0))
    ):
        return fail("SOURCE_PROJECTION_PLANE_TILT_UNSUPPORTED")
    result["projection_planes"] = {
        "basis": (
            "validated_parallel_top_view_XY_projection"
            if viewport
            else "same_native_layout_XY_plane"
        ),
        "source_native_plane_z": 0.0,
        "source_output_plane_z": source_plane_origin.z,
        "source_plane_origin_transformed": list(source_plane_origin),
        "objects": [],
        "claims_three_dimensional_intersection": False,
    }
    source = _Collector(
        document,
        endpoint_tolerance,
        max_source_entities,
        max_segments,
        max_depth,
        frozen if layout.name.casefold() == "model" else (),
        output_plane_z=source_plane_origin.z,
    )
    object_handles = {e.get("handle") for e in objects}
    if parent_handle in object_handles:
        return fail("SOURCE_SYMBOL_CANNOT_BE_SELECTED_NATIVE_OBJECT")
    # Prioritize the explicitly requested source, so an unrelated layout cap
    # cannot make an unvisited source look like an absent symbol.
    source.walk(parent, root=parent_handle, matrix=mapping)
    # Selected native objects are not candidate continuation edges.
    for ordinal, entity in enumerate(layout):
        if entity.dxf.handle in object_handles or entity.dxf.handle == parent_handle:
            continue
        source.walk(entity, root=entity.dxf.handle, matrix=mapping)
        if source.visited >= max_source_entities or len(source.segments) >= max_segments:
            if ordinal + 1 < len(layout):
                source.issue(
                    "SOURCE_ENTITY_CAP"
                    if source.visited >= max_source_entities
                    else "NATIVE_SEGMENT_CAP",
                    entity,
                    entity.dxf.handle,
                )
            break
    _recognize_circular_markers(source)
    paths, path_reasons = _paths(source, parent_handle, endpoint_tolerance, max_paths)
    result["source_arrowhead_count"] = sum(a["root_handle"] == parent_handle for a in source.arrows)
    result["source_marker_count"] = result["source_arrowhead_count"] + sum(
        a["root_handle"] == parent_handle for a in source.circular_markers
    )
    result["symbol_recognized"] = bool(result["source_marker_count"])
    result["source_symbol_segment_count"] = sum(
        s["root_handle"] == parent_handle for s in source.segments
    )
    result["source_symbol_evidence_present"] = (
        result["symbol_recognized"]
        or bool(result["source_symbol_segment_count"])
        or any(
            e["root_handle"] == parent_handle
            and e.get("entity_type") not in {"TEXT", "MTEXT", "ATTRIB", "ATTDEF", "SEQEND", "POINT"}
            for e in [*source.limitations, *source.exclusions]
            if e["code"] != "UNSUPPORTED_NATIVE_CIRCLE"
        )
    )
    result["symbol_geometry_complete"] = not any(
        e["root_handle"] == parent_handle for e in source.limitations
    )
    result["full_layout_scan_complete"] = not source.limitations
    result["paths"] = paths
    result["reason_codes"].extend(path_reasons)
    if "PATH_CANDIDATE_CAP" in path_reasons:
        result["limitations"].append({"code": "PATH_CANDIDATE_CAP", "root_handle": parent_handle})
    reachable_boxes = [
        [
            min(s["start"][0], s["end"][0]),
            min(s["start"][1], s["end"][1]),
            max(s["start"][0], s["end"][0]),
            max(s["start"][1], s["end"][1]),
        ]
        for path in paths
        for s in path["segments"]
    ]
    for issue in source.limitations:
        box = issue.get("output_bbox")
        # Bounds may prove an unsupported entity outside the connected native
        # geometry scope. They can never prove a positive object crossing.
        disjoint = (
            box
            and reachable_boxes
            and all(
                box[2] < candidate[0] - endpoint_tolerance
                or box[0] > candidate[2] + endpoint_tolerance
                or box[3] < candidate[1] - endpoint_tolerance
                or box[1] > candidate[3] + endpoint_tolerance
                for candidate in reachable_boxes
            )
        )
        if issue["root_handle"] != parent_handle and disjoint:
            result["exclusions"].append(
                {
                    **issue,
                    "scope_disposition": "NATIVE_BOUNDS_DISJOINT_FROM_ALL_REACHABLE_PATH_SEGMENTS",
                }
            )
        else:
            result["limitations"].append(issue)
    result["exclusions"].extend(source.exclusions)
    if model_box:
        for path in paths:
            points = [
                p for segment in path["segments"] for p in (segment["start"], segment["end"])
            ] + (
                path["arrowhead"]["closed_vertices"]
                if path["arrowhead"]
                else path["symbol_marker"]["boundary_bounds_corners"]
            )
            if any(not _inside_box(p, model_box, endpoint_tolerance) for p in points):
                result["limitations"].append(
                    {"code": "NATIVE_PATH_OUTSIDE_VIEWPORT", "path_id": path["id"]}
                )
    if not objects:
        result["reason_codes"].append("NO_REVIEWED_NATIVE_OBJECTS")
    seen_objects = set()
    for obj in objects:
        handle, object_id = obj.get("handle"), obj.get("id")
        if not handle or not object_id or handle in seen_objects:
            result["limitations"].append(
                {"code": "MISSING_OR_DUPLICATE_NATIVE_OBJECT_IDENTITY", "handle": handle}
            )
            continue
        seen_objects.add(handle)
        raw = document.entitydb.get(handle)
        expected_layout = document.modelspace() if viewport else layout
        if raw is None or not raw.is_alive or raw.get_layout() is not expected_layout:
            result["limitations"].append(
                {"code": "OBJECT_NATIVE_LAYOUT_MISMATCH", "handle": handle}
            )
            continue
        geometry = _Collector(
            document,
            endpoint_tolerance,
            max_source_entities,
            max_segments,
            max_depth,
            frozen,
            output_plane_z=None if viewport else 0.0,
        )
        geometry.walk(raw, root=handle, target=True)
        result["projection_planes"]["objects"].append(
            {
                "object_id": object_id,
                "handle": handle,
                "native_output_plane_z": geometry.output_plane_z,
                "constant_plane_verified": geometry.output_plane_z is not None
                and not geometry.limitations,
            }
        )
        result["limitations"].extend(geometry.limitations)
        result["exclusions"].extend(geometry.exclusions)
        if not geometry.segments:
            result["limitations"].append(
                {"code": "NO_SUPPORTED_NATIVE_OBJECT_GEOMETRY", "handle": handle}
            )
        if model_box and any(
            not _inside_box(p, model_box, endpoint_tolerance)
            for segment in geometry.segments
            for p in (segment["start"], segment["end"])
        ):
            result["limitations"].append(
                {"code": "NATIVE_OBJECT_OUTSIDE_VIEWPORT", "handle": handle}
            )
        for path in paths:
            events = []
            for segment in path["segments"]:
                for boundary in geometry.segments:
                    event = _intersection(
                        segment["start"],
                        segment["end"],
                        boundary["start"],
                        boundary["end"],
                        endpoint_tolerance,
                    )
                    if event:
                        events.append(
                            {**event, "path_segment_id": segment["id"], "object_segment": boundary}
                        )
            proper = [event for event in events if event["kind"] == "PROPER_CROSSING"]
            interior_spans = []
            # Reject mere polygon boundary grazing even when a vertex touch is
            # numerically shared. A strict interior interval is additional proof.
            for polygon in geometry.polygons:
                for segment in path["segments"]:
                    cuts = [0.0, 1.0]
                    for event in events:
                        if (
                            event["path_segment_id"] == segment["id"]
                            and event.get("path_parameter") is not None
                        ):
                            cuts.append(max(0.0, min(1.0, event["path_parameter"])))
                    cuts = sorted(set(cuts))
                    delta = _sub(segment["end"], segment["start"])
                    for a, b in zip(cuts, cuts[1:], strict=False):
                        middle = (a + b) / 2
                        point = [
                            segment["start"][0] + middle * delta[0],
                            segment["start"][1] + middle * delta[1],
                        ]
                        if (b - a) * math.hypot(*delta) > endpoint_tolerance and _strict_inside(
                            point, polygon["points"], endpoint_tolerance
                        ):
                            interior_spans.append(
                                {
                                    "path_segment_id": segment["id"],
                                    "object_handle": polygon["handle"],
                                    "parameter_interval": [a, b],
                                }
                            )
            crossed = bool(proper) and (not geometry.polygons or bool(interior_spans))
            result["object_intersections"].append(
                {
                    "path_id": path["id"],
                    "object_id": object_id,
                    "object_handle": handle,
                    "proper_crossing": crossed,
                    "events": events,
                    "strict_interior_spans": interior_spans,
                    "native_geometry_complete": not geometry.limitations,
                }
            )
            if crossed:
                result["candidate_object_ids"].append(object_id)
    result["candidate_object_ids"] = sorted(set(result["candidate_object_ids"]))
    result["crossing_object_ids"] = list(result["candidate_object_ids"])
    if not result["candidate_object_ids"]:
        result["reason_codes"].append("NO_PROPER_NATIVE_OBJECT_CROSSING")
    result["complete"] = not result["limitations"]
    result["supported"] = (
        result["complete"]
        and len(paths) == 1
        and bool(result["candidate_object_ids"])
        and not result["reason_codes"]
    )
    result["reason_codes"] = sorted(set(result["reason_codes"]))
    result["scan"] = {
        "source_entities_visited": source.visited,
        "source_segment_count": len(source.segments),
        "closed_arrowhead_count": len(source.arrows),
        "limits": {
            "max_source_entities": max_source_entities,
            "max_segments": max_segments,
            "max_depth": max_depth,
            "max_paths": max_paths,
        },
    }
    return result


__all__ = ["trace_native_cut_paths"]
