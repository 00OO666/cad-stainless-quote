"""Extract native closed paper-frame rectangles without text/INSERT extents.

Completeness covers only the supported LINE/polyline traversal. It does not
certify arbitrary proxy/Xref graphics or establish page/component ownership.
"""

from __future__ import annotations

import math
from collections import Counter, defaultdict
from itertools import product
from typing import Any

from ezdxf.math import Vec3


def extract_insert_frame_geometry(
    insert: Any, *, max_entities: int = 20_000, max_depth: int = 8
) -> dict[str, Any]:
    """Return visible WCS-axis-aligned native rectangles from an INSERT.

    Nested insert matrices are applied to original vertices, preserving source
    handle chains. Mirror, orthogonal rotation and nonuniform scale are allowed;
    oblique/parallelogram results are not promoted to their enclosing bbox.
    Text, proxy/OLE and other non-frame primitives are inventoried but ignored.
    """
    if max_entities < 1 or max_depth < 1:
        raise ValueError("max_entities and max_depth must be positive")
    out: dict[str, Any] = {
        "schema": "native-paper-frame/1",
        "complete": True,
        "completeness_scope": "supported_native_line_and_polyline_primitives_only",
        "rectangles": [],
        "issues": [],
        "ignored_types": {},
        "unsupported_geometry": {},
        "visited_entities": 0,
        "root_insert_handle": str(insert.dxf.get("handle") or ""),
    }
    ignored: Counter[str] = Counter()
    unsupported: Counter[str] = Counter()
    lines: list[tuple[Vec3, Vec3, tuple[str, ...]]] = []
    polygons: list[tuple[list[Vec3], tuple[str, ...]]] = []
    doc = insert.doc

    def fail(reason: str) -> None:
        out["complete"] = False
        if reason not in out["issues"]:
            out["issues"].append(reason)

    def visible(entity: Any, inherited_layer: str) -> tuple[bool, str]:
        layer_name = str(entity.dxf.get("layer", "0"))
        if layer_name == "0":
            layer_name = inherited_layer
        if entity.dxf.get("invisible", 0):
            return False, layer_name
        try:
            layer = doc.layers.get(layer_name)
        except Exception:
            fail("MISSING_LAYER:" + layer_name)
            return False, layer_name
        return not (layer.is_off() or layer.is_frozen()), layer_name

    def transform(point: Any, matrices: tuple[Any, ...]) -> Vec3:
        value = Vec3(point)
        for matrix in reversed(matrices):
            value = matrix.transform(value)
        if not all(math.isfinite(v) for v in value):
            raise ValueError("non-finite transformed point")
        return value

    def walk(
        entity: Any,
        matrices: tuple[Any, ...],
        handles: tuple[str, ...],
        block_names: tuple[str, ...],
        layer_name: str,
        depth: int,
    ) -> None:
        if out["visited_entities"] >= max_entities:
            fail("ENTITY_CAP_EXCEEDED")
            return
        out["visited_entities"] += 1
        shown, effective_layer = visible(entity, layer_name)
        if not shown:
            ignored["HIDDEN"] += 1
            return
        handle = str(entity.dxf.get("handle") or "")
        chain = (*handles, handle) if handle else handles
        kind = entity.dxftype()
        try:
            if kind == "INSERT":
                if depth >= max_depth:
                    fail("DEPTH_CAP_EXCEEDED")
                    return
                name = str(entity.dxf.name)
                if name in block_names:
                    fail("CYCLIC_BLOCK_REFERENCE:" + name)
                    return
                block = doc.blocks.get(name)
                if block is None:
                    fail("MISSING_BLOCK:" + name)
                    return
                if block.block.is_xref:
                    fail("EXTERNAL_REFERENCE:" + name)
                    return
                if entity.dxf.get("row_count", 1) > 1 or entity.dxf.get("column_count", 1) > 1:
                    fail("UNSUPPORTED_MULTI_INSERT")
                    return
                scales = [float(entity.dxf.get(axis, 1)) for axis in ("xscale", "yscale", "zscale")]
                if not all(math.isfinite(s) and s != 0 for s in scales):
                    raise ValueError("singular insert scale")
                nested = (*matrices, entity.matrix44())
                for child in block:
                    walk(child, nested, chain, (*block_names, name), effective_layer, depth + 1)
                    if "ENTITY_CAP_EXCEEDED" in out["issues"]:
                        # The first attempted entity beyond the budget proves
                        # truncation; stop all enclosing block iterators too.
                        break
            elif kind == "LINE":
                lines.append(
                    (
                        transform(entity.dxf.start, matrices),
                        transform(entity.dxf.end, matrices),
                        chain,
                    )
                )
            elif kind == "LWPOLYLINE":
                if len(entity) not in (4, 5):
                    unsupported["NON_QUADRILATERAL"] += 1
                    return
                if not entity.closed or any(abs(p[2]) > 1e-12 for p in entity.get_points("xyb")):
                    unsupported["OPEN_OR_BULGED_POLYLINE"] += 1
                    return
                polygons.append(([transform(p, matrices) for p in entity.vertices_in_wcs()], chain))
            elif kind == "POLYLINE":
                if entity.is_poly_face_mesh or entity.is_polygon_mesh:
                    unsupported["MESH_POLYLINE"] += 1
                    return
                if len(entity.vertices) not in (4, 5):
                    unsupported["NON_QUADRILATERAL"] += 1
                    return
                if not entity.is_closed or any(
                    abs(v.dxf.get("bulge", 0)) > 1e-12 for v in entity.vertices
                ):
                    unsupported["OPEN_OR_BULGED_POLYLINE"] += 1
                    return
                polygons.append(([transform(p, matrices) for p in entity.points_in_wcs()], chain))
            else:
                ignored[kind] += 1
        except Exception as exc:
            fail("GEOMETRY_OR_TRANSFORM_ERROR:" + kind + ":" + type(exc).__name__)

    if insert.dxftype() != "INSERT" or doc is None:
        fail("ATTACHED_INSERT_REQUIRED")
    else:
        walk(insert, (), (), (), str(insert.dxf.get("layer", "0")), 0)

    points = [p for a, b, _ in lines for p in (a, b)] + [p for pts, _ in polygons for p in pts]
    if not points:
        out["ignored_types"] = dict(ignored)
        out["unsupported_geometry"] = dict(unsupported)
        return out
    # Relative to geometry span, not absolute world translation or drawing units.
    spans = [max(p[i] for p in points) - min(p[i] for p in points) for i in range(3)]
    if not all(math.isfinite(span) for span in spans):
        fail("NON_FINITE_GEOMETRY_SPAN")
        out["ignored_types"] = dict(ignored)
        out["unsupported_geometry"] = dict(unsupported)
        return out
    tolerance = max(1e-9, math.hypot(*spans) * 1e-10)
    out["closure_tolerance"] = tolerance
    seen: dict[tuple[float, ...], dict[str, Any]] = {}

    def emit(vertices: list[Vec3], chains: list[tuple[str, ...]], basis: str) -> None:
        if len(vertices) == 5 and vertices[0].distance(vertices[-1]) <= tolerance:
            vertices = vertices[:-1]
        if len(vertices) != 4:
            unsupported["NON_QUADRILATERAL"] += 1
            return
        edges = [b - a for a, b in zip(vertices, vertices[1:] + vertices[:1], strict=True)]
        if any(e.magnitude <= tolerance for e in edges):
            return
        if max(p.z for p in vertices) - min(p.z for p in vertices) > tolerance:
            unsupported["NON_PAPER_PLANE_RECTANGLE"] += 1
            return
        if any(
            abs(a.dot(b)) > tolerance * max(a.magnitude, b.magnitude)
            for a, b in zip(edges, edges[1:] + edges[:1], strict=True)
        ):
            unsupported["NON_RECTANGULAR_QUADRILATERAL"] += 1
            return
        if any(abs(e.x) > tolerance and abs(e.y) > tolerance for e in edges):
            unsupported["ROTATED_NON_AXIS_ALIGNED_RECTANGLE"] += 1
            return
        bounds = [
            min(p.x for p in vertices),
            min(p.y for p in vertices),
            max(p.x for p in vertices),
            max(p.y for p in vertices),
        ]
        if bounds[2] - bounds[0] <= tolerance or bounds[3] - bounds[1] <= tolerance:
            return
        # Geometry identity uses tolerance-sized bins; provenance retains every
        # original endpoint and handle chain rather than the quantized values.
        key = tuple(
            round((v - offset) / tolerance)
            for v, offset in zip(bounds, (origin.x, origin.y, origin.x, origin.y), strict=True)
        )
        record = seen.setdefault(
            key,
            {
                "bbox": bounds,
                "corners": [[p.x, p.y] for p in vertices],
                "axis_aligned": True,
                "source_handles": [],
                "source_handle_chains": [],
                "basis": basis,
            },
        )
        for chain in chains:
            if list(chain) not in record["source_handle_chains"]:
                record["source_handle_chains"].append(list(chain))
            if chain and chain[-1] not in record["source_handles"]:
                record["source_handles"].append(chain[-1])

    origin = Vec3(min(p.x for p in points), min(p.y for p in points), min(p.z for p in points))
    for vertices, chain in polygons:
        emit(vertices, [chain], "native_closed_zero_bulge_polyline")

    buckets: dict[tuple[int, int, int], list[int]] = defaultdict(list)
    nodes: list[Vec3] = []

    def cell(point: Vec3) -> tuple[int, int, int]:
        return tuple(round(v / tolerance) for v in point - origin)  # type: ignore[return-value]

    def find(point: Vec3) -> int | None:
        k = cell(point)
        for offset in product((-1, 0, 1), repeat=3):
            for candidate in buckets.get(tuple(a + b for a, b in zip(k, offset, strict=True)), ()):
                if nodes[candidate].distance(point) <= tolerance:
                    return candidate
        return None

    def node(point: Vec3) -> int:
        existing = find(point)
        if existing is not None:
            return existing
        index = len(nodes)
        nodes.append(point)
        buckets[cell(point)].append(index)
        return index

    adjacency: dict[int, set[int]] = defaultdict(set)
    provenance: dict[frozenset[int], list[tuple[str, ...]]] = defaultdict(list)
    for a, b, chain in lines:
        ia, ib = node(a), node(b)
        if ia == ib:
            continue
        adjacency[ia].add(ib)
        adjacency[ib].add(ia)
        provenance[frozenset((ia, ib))].append(chain)
    work = 0
    stop = False
    for a in sorted(adjacency):
        for b in sorted(adjacency[a]):
            for c in sorted(adjacency[b]):
                work += 1
                if work > max_entities * 16:
                    fail("LINE_CYCLE_WORK_CAP_EXCEEDED")
                    stop = True
                    break
                if c == a:
                    continue
                ab, bc = nodes[b] - nodes[a], nodes[c] - nodes[b]
                if abs(ab.dot(bc)) > tolerance * max(ab.magnitude, bc.magnitude):
                    continue
                d = find(nodes[a] + nodes[c] - nodes[b])
                if d is None or d in (a, b, c) or d not in adjacency[a] or d not in adjacency[c]:
                    continue
                if a != min(a, b, c, d) or b > d:
                    continue
                chains = [
                    chain
                    for edge in ((a, b), (b, c), (c, d), (d, a))
                    for chain in provenance[frozenset(edge)]
                ]
                emit([nodes[a], nodes[b], nodes[c], nodes[d]], chains, "native_four_line_cycle")
            if stop:
                break
        if stop:
            break
    out["line_cycle_work"] = work
    out["rectangles"] = sorted(seen.values(), key=lambda item: tuple(item["bbox"]))
    out["ignored_types"] = dict(ignored)
    out["unsupported_geometry"] = dict(unsupported)
    return out
