"""Synthetic native frame geometry; no customer CAD, coordinates or text."""

import ezdxf
import pytest
from cadquote.paper_frames import extract_insert_frame_geometry


def _drawing():
    doc = ezdxf.new()
    block = doc.blocks.new("FRAME")
    return doc, block


def _poly(block):
    return block.add_lwpolyline([(0, 0), (12, 0), (12, 8), (0, 8)], close=True)


def test_closed_polyline_not_whole_insert_text_extents():
    doc, block = _drawing()
    poly = _poly(block)
    block.add_text("UNRELATED EXTENT", dxfattribs={"insert": (5000, 5000)})
    root = doc.modelspace().add_blockref("FRAME", (20, 30))
    result = extract_insert_frame_geometry(root)
    assert result["complete"] and result["issues"] == []
    assert result["rectangles"][0]["bbox"] == pytest.approx([20, 30, 32, 38])
    assert result["rectangles"][0]["source_handles"] == [poly.dxf.handle]
    assert result["rectangles"][0]["source_handle_chains"] == [[root.dxf.handle, poly.dxf.handle]]


def test_four_lines_can_form_closed_frame_among_title_grid_branches():
    doc, block = _drawing()
    for a, b in [
        ((0, 0), (12, 0)),
        ((12, 0), (12, 8)),
        ((12, 8), (0, 8)),
        ((0, 8), (0, 0)),
        ((0, 0), (3, 2)),
    ]:
        block.add_line(a, b)
    result = extract_insert_frame_geometry(doc.modelspace().add_blockref("FRAME", (0, 0)))
    assert len(result["rectangles"]) == 1
    assert result["rectangles"][0]["basis"] == "native_four_line_cycle"
    assert len(result["rectangles"][0]["source_handles"]) == 4


def test_open_three_sides_never_become_bbox_frame():
    doc, block = _drawing()
    for a, b in [((0, 0), (12, 0)), ((12, 0), (12, 8)), ((12, 8), (0, 8))]:
        block.add_line(a, b)
    result = extract_insert_frame_geometry(doc.modelspace().add_blockref("FRAME", (0, 0)))
    assert result["rectangles"] == []


def test_nested_nonuniform_mirror_orthogonal_transform():
    doc, block = _drawing()
    poly = _poly(block)
    wrapper = doc.blocks.new("WRAPPER")
    child = wrapper.add_blockref("FRAME", (5, 7), dxfattribs={"xscale": -2, "yscale": 3})
    root = doc.modelspace().add_blockref("WRAPPER", (100, 200), dxfattribs={"rotation": 90})
    result = extract_insert_frame_geometry(root)
    expected = [
        root.matrix44().transform(child.matrix44().transform(p)) for p in poly.vertices_in_wcs()
    ]
    box = [
        min(p.x for p in expected),
        min(p.y for p in expected),
        max(p.x for p in expected),
        max(p.y for p in expected),
    ]
    assert result["complete"]
    assert result["rectangles"][0]["bbox"] == pytest.approx(box)
    assert result["rectangles"][0]["source_handle_chains"] == [
        [root.dxf.handle, child.dxf.handle, poly.dxf.handle]
    ]


@pytest.mark.parametrize("rotation", [15, 45, 135])
def test_oblique_rectangle_is_not_replaced_with_axis_aligned_bbox(rotation):
    doc, block = _drawing()
    _poly(block)
    root = doc.modelspace().add_blockref("FRAME", (0, 0), dxfattribs={"rotation": rotation})
    result = extract_insert_frame_geometry(root)
    assert result["complete"]
    assert result["rectangles"] == []
    assert result["unsupported_geometry"]["ROTATED_NON_AXIS_ALIGNED_RECTANGLE"] == 1


@pytest.mark.parametrize("state", ["off", "frozen", "invisible"])
def test_hidden_native_frame_is_not_candidate(state):
    doc, block = _drawing()
    _poly(block)
    layer = doc.layers.new("HIDDEN_FRAME")
    root = doc.modelspace().add_blockref("FRAME", (0, 0), dxfattribs={"layer": layer.dxf.name})
    if state == "off":
        layer.off()
    elif state == "frozen":
        layer.freeze()
    else:
        root.dxf.invisible = 1
    result = extract_insert_frame_geometry(root)
    assert result["complete"] and result["rectangles"] == []


def test_bulged_closed_polyline_is_not_rectangular_frame():
    doc, block = _drawing()
    block.add_lwpolyline([(0, 0, 1), (12, 0, 0), (12, 8, 0), (0, 8, 0)], format="xyb", close=True)
    result = extract_insert_frame_geometry(doc.modelspace().add_blockref("FRAME", (0, 0)))
    assert result["rectangles"] == []


def test_cap_is_incomplete_and_never_hidden_as_success():
    doc, block = _drawing()
    _poly(block)
    block.add_line((0, 0), (1, 1))
    result = extract_insert_frame_geometry(
        doc.modelspace().add_blockref("FRAME", (0, 0)), max_entities=2
    )
    assert not result["complete"]
    assert "ENTITY_CAP_EXCEEDED" in result["issues"]


def test_recursive_block_is_incomplete():
    doc, block = _drawing()
    block.add_blockref("FRAME", (0, 0))
    result = extract_insert_frame_geometry(doc.modelspace().add_blockref("FRAME", (0, 0)))
    assert not result["complete"]
    assert any("CYCLIC_BLOCK_REFERENCE" in issue for issue in result["issues"])


def test_depth_limit_is_incomplete():
    doc, block = _drawing()
    _poly(block)
    wrapper = doc.blocks.new("WRAPPER")
    wrapper.add_blockref("FRAME", (0, 0))
    result = extract_insert_frame_geometry(
        doc.modelspace().add_blockref("WRAPPER", (0, 0)), max_depth=1
    )
    assert not result["complete"]
    assert "DEPTH_CAP_EXCEEDED" in result["issues"]


def test_exact_entity_budget_can_still_be_complete():
    doc, block = _drawing()
    _poly(block)
    result = extract_insert_frame_geometry(
        doc.modelspace().add_blockref("FRAME", (0, 0)), max_entities=2
    )
    assert result["complete"] and len(result["rectangles"]) == 1


def test_classic_polyline_and_ignored_primitives():
    doc, block = _drawing()
    block.add_polyline2d([(0, 0), (12, 0), (12, 8), (0, 8)], close=True)
    block.add_circle((1000, 1000), 500)
    result = extract_insert_frame_geometry(doc.modelspace().add_blockref("FRAME", (0, 0)))
    assert result["complete"] and result["issues"] == []
    assert result["ignored_types"] == {"CIRCLE": 1}
    assert result["rectangles"][0]["bbox"] == pytest.approx([0, 0, 12, 8])


def test_nested_oblique_then_nonuniform_transform_is_not_a_frame():
    doc, block = _drawing()
    _poly(block)
    wrapper = doc.blocks.new("WRAPPER")
    wrapper.add_blockref("FRAME", (0, 0), dxfattribs={"rotation": 45})
    root = doc.modelspace().add_blockref("WRAPPER", (0, 0), dxfattribs={"xscale": 2})
    result = extract_insert_frame_geometry(root)
    assert result["rectangles"] == []
    assert result["unsupported_geometry"]["NON_RECTANGULAR_QUADRILATERAL"] == 1


def test_native_frame_on_hidden_child_layer_is_excluded():
    doc, block = _drawing()
    layer = doc.layers.new("CHILD_HIDDEN")
    layer.off()
    _poly(block).dxf.layer = layer.dxf.name
    result = extract_insert_frame_geometry(doc.modelspace().add_blockref("FRAME", (0, 0)))
    assert result["complete"] and result["rectangles"] == []


def test_nonplanar_polyline_is_not_projected_to_frame():
    doc, block = _drawing()
    block.add_polyline3d([(0, 0, 0), (12, 0, 0), (12, 8, 1), (0, 8, 1)], close=True)
    result = extract_insert_frame_geometry(doc.modelspace().add_blockref("FRAME", (0, 0)))
    assert result["rectangles"] == []
    assert result["unsupported_geometry"]["NON_PAPER_PLANE_RECTANGLE"] == 1


def test_title_cells_are_kept_as_geometry_not_labeled_as_page_ownership():
    doc, block = _drawing()
    _poly(block)
    block.add_lwpolyline([(1, 1), (3, 1), (3, 2), (1, 2)], close=True)
    result = extract_insert_frame_geometry(doc.modelspace().add_blockref("FRAME", (0, 0)))
    assert len(result["rectangles"]) == 2
    assert all("page_code" not in rectangle for rectangle in result["rectangles"])
