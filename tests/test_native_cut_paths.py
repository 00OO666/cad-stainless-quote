"""Synthetic native geometry only; no customer fixtures or quantity targets."""

import math

import ezdxf
import pytest
from cadquote.native_cut_paths import trace_native_cut_paths
from ezdxf.lldxf.tags import Tags
from ezdxf.lldxf.types import DXFTag, DXFVertex


def _case(*, rotation=0, scale=(1, 1), paper=False):
    doc = ezdxf.new("R2018")
    layout = doc.layouts.new("Sheet") if paper else doc.modelspace()
    block = doc.blocks.new("SECTION_SYMBOL")
    arrow = block.add_lwpolyline([(0, 0), (-2, -1), (-2, 1)], close=True)
    stem = block.add_line((-2, 0), (-5, 0))
    parent = layout.add_blockref(
        "SECTION_SYMBOL",
        (40, 40),
        dxfattribs={"rotation": rotation, "xscale": scale[0], "yscale": scale[1]},
    )
    attr = parent.add_attrib("SHEET_NUMBER", "SE-01", (40, 42))
    matrix = parent.matrix44()

    def point(x, y):
        value = matrix.transform((x, y, 0))
        return (value.x, value.y)

    continuation = layout.add_line(point(-5, 0), point(-20, 0))
    object_layout = doc.modelspace() if paper else layout
    obj = object_layout.add_lwpolyline(
        [point(-15, -2), point(-10, -2), point(-10, 2), point(-15, 2)], close=True
    )
    references = [
        {
            "id": "ref",
            "handle": attr.dxf.handle,
            "entity_type": "ATTRIB",
            "geometry": {"parent_insert_handle": parent.dxf.handle},
        }
    ]
    objects = [{"id": "selected-outline", "handle": obj.dxf.handle, "bbox": [0, 0, 1, 1]}]
    viewport = (
        layout.add_viewport(
            center=(50, 50), size=(100, 100), view_center_point=(50, 50), view_height=100
        )
        if paper
        else None
    )
    return {
        "doc": doc,
        "layout": layout,
        "block": block,
        "parent": parent,
        "arrow": arrow,
        "stem": stem,
        "attr": attr,
        "continuation": continuation,
        "obj": obj,
        "references": references,
        "objects": objects,
        "viewport": viewport,
        "point": point,
    }


def _trace(case, **kwargs):
    return trace_native_cut_paths(
        case["doc"],
        reference_entities=case["references"],
        native_objects=case["objects"],
        viewport_handle=case["viewport"].dxf.handle if case["viewport"] else None,
        **kwargs,
    )


@pytest.mark.parametrize(
    "rotation,scale", [(0, (1, 1)), (37, (2, 3)), (90, (-2, 0.5)), (180, (0.1, 2))]
)
def test_native_parent_geometry_and_finite_continuation(rotation, scale):
    case = _case(rotation=rotation, scale=scale)
    result = _trace(case)
    assert result["supported"], result
    assert result["state"] == "REVIEW"
    assert result["assigns_material_role_quantity_or_pass"] is False
    assert result["crossing_object_ids"] == ["selected-outline"]
    assert len(result["paths"]) == 1
    assert len(result["paths"][0]["segments"]) == 2
    assert result["paths"][0]["segments"][0]["handle"] == case["stem"].dxf.handle
    assert (
        result["paths"][0]["segments"][0]["insert_chain"][0]["handle"] == case["parent"].dxf.handle
    )
    assert result["object_intersections"][0]["strict_interior_spans"]


def test_native_paper_projection():
    case = _case(paper=True)
    result = _trace(case)
    assert result["supported"], result
    assert result["native_viewport_transform"]["viewport_handle"] == case["viewport"].dxf.handle


def test_gap_is_not_filled_by_proximity():
    case = _case()
    case["continuation"].dxf.start = case["point"](-5.01, 0)
    result = _trace(case)
    assert not result["supported"]
    assert not result["crossing_object_ids"]
    assert len(result["paths"][0]["segments"]) == 1


def test_crossing_segment_does_not_create_endpoint_junction():
    case = _case()
    case["layout"].delete_entity(case["continuation"])
    case["layout"].add_line(case["point"](-4, -20), case["point"](-4, 20))
    result = _trace(case)
    assert not result["supported"]
    assert len(result["paths"][0]["segments"]) == 1


def test_endpoint_branch_is_explicit_ambiguity():
    case = _case()
    case["layout"].add_line(case["point"](-5, 0), case["point"](-5, 10))
    result = _trace(case)
    assert not result["supported"]
    assert "ENDPOINT_BRANCH_AMBIGUITY" in result["reason_codes"]
    assert len(result["paths"]) == 2


def test_second_arrow_terminates_and_rejects_path():
    case = _case()
    case["layout"].add_blockref("SECTION_SYMBOL", case["point"](-20, 0))
    result = _trace(case)
    assert not result["supported"]
    assert "PATH_REACHES_OTHER_ARROWHEAD" in result["reason_codes"]


def test_bbox_overlap_and_boundary_grazing_do_not_prove_crossing():
    case = _case()
    case["obj"].set_points(
        [case["point"](-15, 0), case["point"](-10, 0), case["point"](-10, 2), case["point"](-15, 2)]
    )
    result = _trace(case)
    assert not result["supported"]
    assert not result["crossing_object_ids"]
    assert any(
        e["kind"] == "COLLINEAR_BOUNDARY_TOUCH" for e in result["object_intersections"][0]["events"]
    )


def test_multiple_reviewed_edges_are_one_component_geometry():
    case = _case()
    case["layout"].delete_entity(case["obj"])
    edges = [case["layout"].add_line(case["point"](x, -2), case["point"](x, 2)) for x in (-10, -15)]
    case["objects"] = [
        {"id": f"edge-{i}", "handle": edge.dxf.handle} for i, edge in enumerate(edges)
    ]
    result = _trace(case)
    assert result["supported"], result
    assert result["crossing_object_ids"] == ["edge-0", "edge-1"]


@pytest.mark.parametrize("which", ["parent", "attr", "stem", "arrow", "obj"])
def test_hidden_native_geometry_cannot_release_proof(which):
    case = _case()
    if which == "attr":
        case[which].dxf.flags = 1
    else:
        case[which].dxf.invisible = 1
    result = _trace(case)
    assert not result["supported"], result


def test_viewport_frozen_layer_and_clipping_are_reported():
    case = _case(paper=True)
    case["doc"].layers.new("OBJECT_LAYER")
    case["obj"].dxf.layer = "OBJECT_LAYER"
    case["viewport"].freeze("OBJECT_LAYER")
    result = _trace(case)
    assert not result["supported"]
    assert any(e["code"] == "HIDDEN_OR_FROZEN_NATIVE_GEOMETRY" for e in result["limitations"])
    case["viewport"].dxf.flags |= 65536
    result = _trace(case)
    assert not result["supported"]
    assert "VIEWPORT_CLIPPING_OR_PERSPECTIVE_UNSUPPORTED" in result["reason_codes"]


def test_reference_must_be_attached_to_actual_parent():
    case = _case()
    other = case["layout"].add_blockref("SECTION_SYMBOL", (70, 70))
    case["references"][0]["geometry"]["parent_insert_handle"] = other.dxf.handle
    assert "REFERENCE_NOT_ACTUAL_PARENT_ATTACHED_ATTRIB" in _trace(case)["reason_codes"]


@pytest.mark.parametrize(
    "option,value,code",
    [
        ("max_source_entities", 1, "SOURCE_ENTITY_CAP"),
        ("max_segments", 1, "NATIVE_SEGMENT_CAP"),
        ("max_depth", 1, "BLOCK_RECURSION_CAP"),
    ],
)
def test_scan_limits_cannot_release_proof(option, value, code):
    case = _case()
    if option == "max_depth":
        nested = case["doc"].blocks.new("NESTED")
        nested.add_blockref("SECTION_SYMBOL", (0, 0))
        case["parent"].dxf.name = "NESTED"
    result = _trace(case, **{option: value})
    assert not result["supported"]
    assert not result["complete"]
    assert any(e["code"] == code for e in result["limitations"]), result


def test_invalid_tolerance_is_rejected():
    case = _case()
    with pytest.raises(ValueError):
        _trace(case, endpoint_tolerance=math.inf)


@pytest.mark.parametrize("near", [False, True])
def test_opaque_native_bounds_are_scoped_without_type_guessing(near):
    case = _case()
    ole = case["layout"].new_entity("OLE2FRAME", {})
    lower, upper = ((24, 39, 0), (31, 42, 0)) if near else ((90, 90, 0), (99, 99, 0))
    ole.acdb_ole2frame = Tags(
        [DXFTag(100, "AcDbOle2Frame"), DXFVertex(10, lower), DXFVertex(11, upper)]
    )
    result = _trace(case)
    assert result["supported"] is not near, result
    assert not result["full_layout_scan_complete"]
    if not near:
        assert result["complete"]
        assert any(
            e.get("scope_disposition") == "NATIVE_BOUNDS_DISJOINT_FROM_ALL_REACHABLE_PATH_SEGMENTS"
            for e in result["exclusions"]
        )


def test_opaque_geometry_without_native_bounds_blocks_completeness():
    case = _case()
    case["layout"].new_entity("OLE2FRAME", {})
    result = _trace(case)
    assert not result["supported"]
    assert not result["complete"]


def test_hidden_template_arrows_are_not_competing_visible_arrows():
    case = _case()
    hidden = case["block"].add_solid([(0, 4), (-2, 3), (-2, 5)])
    hidden.dxf.invisible = 1
    result = _trace(case)
    assert result["supported"], result
    assert result["source_arrowhead_count"] == 1


def test_solid_arrow_and_native_nested_matrix_are_preserved():
    case = _case()
    case["block"].delete_entity(case["arrow"])
    triangle = case["block"].add_solid([(0, 0), (-2, -1), (-2, 1)])
    container = case["doc"].blocks.new("SYMBOL_CONTAINER")
    inner = container.add_blockref("SECTION_SYMBOL", (0, 0), dxfattribs={"rotation": 90})
    case["parent"].dxf.name = "SYMBOL_CONTAINER"
    case["parent"].dxf.rotation = -90
    result = _trace(case)
    assert result["supported"], result
    arrow = result["paths"][0]["arrowhead"]
    assert arrow["handle"] == triangle.dxf.handle
    assert [e["handle"] for e in arrow["insert_chain"]] == [
        case["parent"].dxf.handle,
        inner.dxf.handle,
    ]


def test_forward_pointing_ray_is_not_a_receding_stem():
    case = _case()
    case["stem"].dxf.start = (0, 0)
    case["stem"].dxf.end = (20, 0)
    result = _trace(case)
    assert result["symbol_recognized"]
    assert not result["supported"]
    assert "ARROW_HAS_NO_NATIVE_DIRECTION_CONSTRAINED_STEM" in result["reason_codes"]


def test_finite_path_stopping_on_object_boundary_is_only_touch():
    case = _case()
    case["continuation"].dxf.end = case["point"](-10, 0)
    result = _trace(case)
    assert not result["supported"]
    assert not result["object_intersections"][0]["proper_crossing"]


def test_unsupported_source_symbol_cannot_be_misreported_absent():
    case = _case()
    case["block"].add_spline([(0, 0), (-2, 3), (-4, 0)])
    result = _trace(case)
    assert result["symbol_recognized"]
    assert not result["symbol_geometry_complete"]
    assert not result["supported"]


def _circular_case(**kwargs):
    case = _case(**kwargs)
    case["block"].delete_entity(case["arrow"])
    case["block"].delete_entity(case["stem"])
    # These two native sweeps have real gaps; they are never filled implicitly.
    case["arcs"] = [
        case["block"].add_arc((0, 0), 2, -10, 190),
        case["block"].add_arc((0, 0), 2, 240, 300),
    ]
    case["diameter"] = case["block"].add_line((-2, 0), (2, 0))
    case["continuation"].dxf.start = case["point"](-2, 0)
    return case


@pytest.mark.parametrize("rotation,scale", [(0, (1, 1)), (37, (2, 3)), (90, (-2, 0.5))])
def test_visible_circular_divider_native_diameter_connection(rotation, scale):
    case = _circular_case(rotation=rotation, scale=scale)
    result = _trace(case)
    assert result["supported"], result
    assert result["source_arrowhead_count"] == 0
    assert result["source_marker_count"] == 1
    assert result["symbol_recognized"]
    assert result["paths"][0]["arrowhead"] is None
    assert result["paths"][0]["symbol_marker"]["arc_gaps_are_not_bridged"]
    assert result["paths"][0]["stem_attachment"]["diameter_handle"] == case["diameter"].dxf.handle


def test_arc_mid_sweep_radial_endpoint_is_native_attachment():
    case = _circular_case()
    case["continuation"].dxf.start = case["point"](0, 2)
    case["continuation"].dxf.end = case["point"](0, 20)
    case["obj"].set_points(
        [case["point"](-2, 10), case["point"](2, 10), case["point"](2, 15), case["point"](-2, 15)]
    )
    result = _trace(case)
    assert result["supported"], result
    assert result["paths"][0]["direction_constraint"] == "NATIVE_VISIBLE_ARC_RADIAL_STEM"
    assert result["paths"][0]["stem_attachment"]["arc_handles"] == [case["arcs"][0].dxf.handle]


def test_circle_gap_is_not_a_visible_attachment():
    case = _circular_case()
    radians = math.radians(220)
    case["continuation"].dxf.start = case["point"](2 * math.cos(radians), 2 * math.sin(radians))
    case["continuation"].dxf.end = case["point"](20 * math.cos(radians), 20 * math.sin(radians))
    result = _trace(case)
    assert result["symbol_recognized"]
    assert not result["supported"]
    assert not result["paths"]


def test_native_diameter_endpoint_can_leave_outward_and_turn():
    case = _circular_case()
    case["continuation"].dxf.end = case["point"](-5, 1)
    case["layout"].add_line(case["point"](-5, 1), case["point"](-20, 1))
    result = _trace(case)
    assert result["supported"], result
    assert len(result["paths"][0]["segments"]) == 2


@pytest.mark.parametrize(
    "variant", ["missing_diameter", "overlapping_arcs", "hidden_arc", "wrong_radius"]
)
def test_circular_family_requires_visible_native_structure(variant):
    case = _circular_case()
    if variant == "missing_diameter":
        case["block"].delete_entity(case["diameter"])
    elif variant == "overlapping_arcs":
        case["arcs"][1].dxf.start_angle = 90
    elif variant == "hidden_arc":
        case["arcs"][1].dxf.invisible = 1
    else:
        case["arcs"][1].dxf.radius = 3
    result = _trace(case)
    assert not result["supported"]
    assert not result["symbol_recognized"]
    assert result["source_symbol_evidence_present"]


def test_circle_has_multiple_native_outlets_is_ambiguous():
    case = _circular_case()
    case["layout"].add_line(case["point"](2, 0), case["point"](15, 0))
    result = _trace(case)
    assert not result["supported"]
    assert "MULTIPLE_DIRECTION_CONSTRAINED_STEMS" in result["reason_codes"]


def test_unknown_native_layer_never_means_hidden():
    case = _case()
    case["block"].add_line((-5, 0), (-5, 10), dxfattribs={"layer": "UNDECLARED_LAYER"})
    result = _trace(case)
    assert not result["supported"]
    assert not result["symbol_geometry_complete"]
    assert any(e["code"] == "NATIVE_LAYER_VISIBILITY_UNKNOWN" for e in result["limitations"])


def test_hidden_only_arrow_prohibits_absent_symbol_fallback():
    case = _case()
    case["arrow"].dxf.invisible = 1
    result = _trace(case)
    assert not result["symbol_recognized"]
    assert result["source_symbol_evidence_present"]
    assert not result["supported"]


def test_circle_radial_crossing_without_endpoint_is_not_attachment():
    case = _circular_case()
    case["continuation"].dxf.start = case["point"](0, 0)
    result = _trace(case)
    assert not result["paths"]


def test_nondiameter_arc_endpoint_requires_radial_outward_direction():
    case = _circular_case()
    case["continuation"].dxf.start = case["point"](0, 2)
    case["continuation"].dxf.end = case["point"](-20, 2)
    result = _trace(case)
    assert not result["paths"]


def test_triangle_attachment_external_branch_is_not_ignored():
    case = _case()
    case["layout"].add_line(case["point"](-2, 0), case["point"](-2, 20))
    result = _trace(case)
    assert not result["supported"]
    assert "SOURCE_ATTACHMENT_BRANCH_AMBIGUITY" in result["reason_codes"]


def test_polyline_width_never_becomes_unqualified_centerline():
    case = _case()
    case["arrow"].dxf.const_width = 1
    result = _trace(case)
    assert not result["supported"]
    assert not result["symbol_geometry_complete"]
    assert any(e["code"] == "POLYLINE_WIDTH_GEOMETRY_UNSUPPORTED" for e in result["limitations"])


def test_paper_path_uses_actual_viewport_scale_and_translation():
    case = _case(paper=True)
    case["viewport"].dxf.view_height = 200
    case["viewport"].dxf.view_center_point = (150, 150)
    inverse = case["viewport"].get_transformation_matrix().copy()
    inverse.inverse()
    vertices = [inverse.transform((x, y, 0)) for x, y, *_ in case["obj"].get_points()]
    case["obj"].set_points([(p.x, p.y) for p in vertices])
    result = _trace(case)
    assert result["supported"], result
    assert result["native_viewport_transform"]["model_to_paper_scale"] == 0.5


def test_legacy_polyline_native_stem_and_continuation():
    case = _case()
    case["block"].delete_entity(case["stem"])
    case["block"].add_polyline2d([(-2, 0), (-5, 0)])
    case["layout"].delete_entity(case["continuation"])
    case["layout"].add_polyline2d([case["point"](-5, 0), case["point"](-20, 0)])
    result = _trace(case)
    assert result["supported"], result


def test_recursive_native_insert_is_explicitly_incomplete():
    case = _case()
    case["block"].add_blockref("SECTION_SYMBOL", (0, 0))
    result = _trace(case)
    assert not result["supported"]
    assert not result["complete"]
    assert any(e["code"] == "RECURSIVE_BLOCK" for e in result["limitations"])


@pytest.mark.parametrize("family", ["triangle", "circular"])
@pytest.mark.parametrize("view_target_z,object_z", [(250, 0), (-120, 27)])
def test_parallel_viewport_constant_z_translation_preserves_xy_proof(
    family, view_target_z, object_z
):
    case = (_case if family == "triangle" else _circular_case)(paper=True)
    case["viewport"].dxf.view_target_point = (0, 0, view_target_z)
    case["obj"].dxf.elevation = object_z
    result = _trace(case)
    assert result["supported"], result
    planes = result["projection_planes"]
    assert planes["source_native_plane_z"] == 0
    assert planes["source_output_plane_z"] == pytest.approx(view_target_z)
    assert planes["objects"][0]["native_output_plane_z"] == pytest.approx(object_z)
    assert planes["objects"][0]["constant_plane_verified"]
    assert not planes["claims_three_dimensional_intersection"]
    assert all(
        segment["output_plane_z"] == pytest.approx(view_target_z)
        for segment in result["paths"][0]["segments"]
    )
    if family == "circular":
        marker = result["paths"][0]["symbol_marker"]
        assert marker["output_plane_z"] == pytest.approx(view_target_z)
        assert all(arc["output_plane_z"] == pytest.approx(view_target_z) for arc in marker["arcs"])
        assert result["paths"][0]["stem_attachment"]["native_point"][2] == pytest.approx(0)


@pytest.mark.parametrize(
    "variant", ["source_line_z_change", "object_line_z_change", "source_insert_tilt", "arc_tilt"]
)
def test_top_view_projection_never_accepts_unknown_nonplanar_geometry(variant):
    case = (_circular_case if variant == "arc_tilt" else _case)(paper=True)
    case["viewport"].dxf.view_target_point = (0, 0, 250)
    if variant == "source_line_z_change":
        end = case["continuation"].dxf.end
        case["continuation"].dxf.end = (end.x, end.y, 1)
    elif variant == "object_line_z_change":
        model = case["doc"].modelspace()
        model.delete_entity(case["obj"])
        obj = model.add_line((30, 38, 27), (30, 42, 28))
        case["objects"] = [{"id": "sloped-object", "handle": obj.dxf.handle}]
    elif variant == "source_insert_tilt":
        case["parent"].dxf.extrusion = (0, 1, 1)
    else:
        case["arcs"][0].dxf.extrusion = (0, 1, 1)
    result = _trace(case)
    assert not result["supported"], result
    assert not result["complete"]


def test_nonzero_object_plane_without_native_viewport_is_not_inferred_projection():
    case = _case()
    case["obj"].dxf.elevation = 27
    result = _trace(case)
    assert not result["supported"]
    assert not result["complete"]


@pytest.mark.parametrize("has_divider", [False, True])
def test_actual_source_segments_prevent_absent_symbol_fallback(has_divider):
    case = _case()
    case["block"].delete_entity(case["arrow"])
    case["block"].delete_entity(case["stem"])
    case["block"].add_circle((0, 0), 2)
    if has_divider:
        case["block"].add_line((-2, 0), (2, 0))
    result = _trace(case)
    assert not result["symbol_recognized"]
    assert result["source_symbol_evidence_present"] is has_divider
    assert result["source_symbol_segment_count"] == int(has_divider)


@pytest.mark.parametrize("near", [False, True])
def test_nonplanar_native_line_bounds_can_only_exclude_disjoint_geometry(near):
    case = _case()
    a, b = ((30, 39, 3), (30, 42, 5)) if near else ((90, 90, 3), (99, 99, 5))
    line = case["layout"].add_line(a, b)
    result = _trace(case)
    assert result["supported"] is not near, result
    assert not result["full_layout_scan_complete"]
    records = result["limitations"] if near else result["exclusions"]
    record = next(e for e in records if e["handle"] == line.dxf.handle)
    assert record["code"] == "NONPLANAR_OR_NONFINITE_GEOMETRY"
    assert record["matrix_to_output"]
    assert record["output_bbox"] == [a[0], a[1], b[0], b[1]]


def test_straight_native_leader_can_continue_at_actual_vertex_endpoint():
    case = _case()
    case["layout"].delete_entity(case["continuation"])
    leader = case["layout"].add_leader(
        [case["point"](-5, 0), case["point"](-20, 0)],
        dxfattribs={"has_arrowhead": 0, "has_hookline": 0, "annotation_type": 3},
    )
    result = _trace(case)
    assert result["supported"], result
    segment = result["paths"][0]["segments"][-1]
    assert segment["entity_type"] == "LEADER"
    assert segment["handle"] == leader.dxf.handle
    assert segment["leader_graphic_index"] == 0
    assert len(segment["leader_native_vertices"]) == 2
    assert segment["native_graphic_type"] == "LINE"


def test_straight_leader_crossing_is_not_an_endpoint_junction():
    case = _case()
    case["layout"].add_leader(
        [case["point"](-12, -10), case["point"](-12, 10), case["point"](-2, 10)],
        dxfattribs={"has_hookline": 0, "annotation_type": 3},
    )
    result = _trace(case)
    assert result["supported"], result
    assert len(result["paths"][0]["segments"]) == 2


def test_native_leader_closed_arrow_is_competing_terminal():
    case = _case()
    case["layout"].delete_entity(case["continuation"])
    case["layout"].add_leader(
        [case["point"](-20, 0), case["point"](-5, 0)],
        dxfattribs={"has_hookline": 0, "annotation_type": 3},
    )
    result = _trace(case)
    assert not result["supported"]
    assert "PATH_REACHES_OTHER_ARROWHEAD" in result["reason_codes"]


@pytest.mark.parametrize("near", [False, True])
def test_unsupported_curved_leader_has_native_bounds_and_scope(near):
    case = _case()
    vertices = [(24, 39), (28, 42), (34, 39)] if near else [(80, 80), (85, 95), (90, 80)]
    leader = case["layout"].add_leader(
        vertices, dxfattribs={"path_type": 1, "has_arrowhead": 0, "has_hookline": 0}
    )
    result = _trace(case)
    assert result["supported"] is not near, result
    records = result["limitations"] if near else result["exclusions"]
    record = next(e for e in records if e["handle"] == leader.dxf.handle)
    assert record["code"] == "CURVED_NATIVE_LEADER_UNSUPPORTED"
    assert record["output_bbox"]


def test_native_leader_unknown_style_cannot_release_geometry():
    case = _case()
    leader = case["layout"].add_leader([case["point"](-5, 0), case["point"](-20, 0)])
    leader.dxf.dimstyle = "UNDECLARED_NATIVE_STYLE"
    result = _trace(case)
    assert not result["supported"]
    assert not result["complete"]
    assert any(e["code"] == "NATIVE_LEADER_GRAPHICS_UNRESOLVED" for e in result["limitations"])


@pytest.mark.parametrize("connect", [False, True])
def test_native_line_arrow_graphics_are_traced_but_not_new_source_markers(connect):
    case = _case()
    arrow_block = case["doc"].blocks.new("NATIVE_LINE_ARROW")
    arrow_block.add_line((0, 0), (-1, -0.5))
    arrow_block.add_line((-1, -0.5), (-1, 0.5))
    arrow_block.add_line((-1, 0.5), (0, 0))
    if connect:
        case["layout"].delete_entity(case["continuation"])
        vertices = [case["point"](-20, 0), case["point"](-5, 0)]
    else:
        vertices = [case["point"](-12, -10), case["point"](-12, 10)]
    leader = case["layout"].add_leader(
        vertices, dxfattribs={"has_hookline": 0, "annotation_type": 3}
    )
    style = leader.override()
    style.set_arrows(ldrblk="NATIVE_LINE_ARROW", size=1)
    style.commit()
    result = _trace(case)
    assert result["supported"] is not connect, result
    assert result["complete"]
    assert result["source_marker_count"] == 1
    if connect:
        assert "PATH_REACHES_OTHER_ARROWHEAD" in result["reason_codes"]
        pointer = result["paths"][0]["competing_leader_pointer_terminals"][0]
        assert pointer["handle"] == leader.dxf.handle
        assert not pointer["usable_as_source_symbol"]
        assert not pointer["closed_graphic_arrowhead_recognized"]
