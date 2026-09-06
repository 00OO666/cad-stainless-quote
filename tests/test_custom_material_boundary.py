"""Synthetic custom material attributes and mirrored OCS boundary regression."""

import copy

import ezdxf
import pytest
from cadquote.cad_index import _insert_geometry
from cadquote.material_branches import discover_material_leader_branches
from cadquote.models import CadEntity, MtOccurrence
from cadquote.panels import _PaperToModelTransform


def fixture(code="GC-SS-207", extrusion=(0, 0, 1), rotation=0):
    doc = ezdxf.new()
    block = doc.blocks.new("SYNTHETIC_LABEL")
    border = block.add_lwpolyline([(0, 0), (24, 0), (24, 7), (0, 7)], close=True)
    insert = doc.modelspace().add_blockref(
        "SYNTHETIC_LABEL", (110, 220), dxfattribs={"extrusion": extrusion, "rotation": rotation}
    )
    child = next(insert.virtual_entities())
    vertices = list(child.vertices_in_wcs())
    center = sum(vertices, ezdxf.math.Vec3()) / 4
    insert.add_attrib("CUSTOM_MATERIAL", code, center, dxfattribs={"height": 0.25})
    return doc, block, border, insert, vertices


@pytest.mark.parametrize("extrusion", [(0, 0, 1), (0, 0, -1)])
@pytest.mark.parametrize("rotation", [0, 90, 27])
def test_custom_code_border_is_wcs(extrusion, rotation):
    _, _, _, insert, vertices = fixture(extrusion=extrusion, rotation=rotation)
    actual = _insert_geometry(insert)["annotation_boundary"]
    assert len(actual) == 4
    for left, right in zip(actual, vertices, strict=True):
        assert left == pytest.approx(list(right))


@pytest.mark.parametrize("code", ["hello", "EL-99", "GC-GL-207", ""])
def test_non_material_attribute_does_not_seed_a_border(code):
    _, _, _, insert, _ = fixture(code=code)
    assert "annotation_boundary" not in _insert_geometry(insert)


def test_code_outside_rectangle_is_not_sufficient():
    _, _, _, insert, _ = fixture()
    insert.attribs[0].dxf.insert = (800, 900, 0)
    assert "annotation_boundary" not in _insert_geometry(insert)


def test_hidden_material_value_is_not_sufficient():
    _, _, _, insert, _ = fixture()
    insert.attribs[0].dxf.invisible = 1
    assert "annotation_boundary" not in _insert_geometry(insert)


def test_two_eligible_borders_remain_ambiguous():
    _, block, _, insert, _ = fixture()
    block.add_lwpolyline([(0, 0), (24, 0), (24, 7), (0, 7)], close=True)
    assert "annotation_boundary" not in _insert_geometry(insert)


@pytest.mark.parametrize("shape", ["open", "curved", "trapezoid"])
def test_non_rectangular_boundary_is_rejected(shape):
    _, _, border, insert, _ = fixture()
    if shape == "open":
        border.closed = False
    elif shape == "curved":
        border.set_points([(0, 0, 0.3), (24, 0, 0), (24, 7, 0), (0, 7, 0)], format="xyb")
    else:
        border.set_points([(0, 0), (24, 0), (20, 7), (0, 7)])
    assert "annotation_boundary" not in _insert_geometry(insert)


def test_mirrored_border_connects_to_native_leader_without_quantity():
    _, _, _, insert, vertices = fixture(extrusion=(0, 0, -1))
    geom = _insert_geometry(insert)
    frame = CadEntity(
        id="frame",
        source_file_id="source",
        sheet_id="sheet",
        entity_type="INSERT",
        handle="P",
        space="model",
        geometry=geom,
    )
    attr = CadEntity(
        id="attr",
        source_file_id="source",
        sheet_id="sheet",
        entity_type="ATTRIB",
        handle="A",
        space="model",
        text="GC-SS-207",
        geometry={"parent_insert_handle": "P"},
    )
    landing = (vertices[0] + vertices[1]) / 2
    tip = landing + ezdxf.math.Vec3(30, 35, 0)
    leader = CadEntity(
        id="leader",
        source_file_id="source",
        sheet_id="sheet",
        entity_type="LEADER",
        handle="L",
        space="model",
        geometry={"vertices": [list(tip), list(landing)], "leader_targets": [list(tip)]},
    )
    occurrence = MtOccurrence(
        id="occ",
        mt_code="GC-SS-207",
        source_file_id="source",
        sheet_id="sheet",
        entity_ids=["attr"],
    )
    before = copy.deepcopy(geom)
    result = discover_material_leader_branches([frame, attr, leader], [occurrence])
    assert result["summary"]["geometry_routing_eligible_branches"] == 1
    assert result["physical_quantity"] is None and occurrence.leader_target is None
    assert geom == before


def test_glyph_box_can_belong_to_border_when_insertion_precedes_left_edge():
    _, _, _, insert, _ = fixture()
    # Baseline insertion is outside, but the visible short label is inside.
    insert.attribs[0].dxf.insert = (109.9, 223, 0)
    assert len(_insert_geometry(insert)["annotation_boundary"]) == 4


@pytest.mark.parametrize("rotation", [0, 90, 27])
def test_split_legacy_material_label_is_still_supported(rotation):
    _, _, _, insert, _ = fixture(rotation=rotation)
    insert.attribs[0].dxf.tag = "ITEM"
    insert.attribs[0].dxf.text = "MT"
    insert.add_attrib("NUM", "07", (110, 220), dxfattribs={"height": 0.25})
    assert len(_insert_geometry(insert)["annotation_boundary"]) == 4


def test_projection_maps_every_redundant_leader_representation_without_mutation():
    raw = {
        "vertices": [[2, 3], [4, 5]],
        "leader_targets": [[2, 3]],
        "landing_points": [[4, 5]],
        "dogleg_end_points": [[4, 5]],
        "leader_target": [2, 3],
        "label_point": [4, 5],
        "text_location": [4, 5],
        "annotation_boundary": [[4, 4], [6, 4], [6, 6], [4, 6]],
        "leader_paths": [
            {
                "leader_index": 4,
                "line_index": 7,
                "vertices": [[2, 3], [4, 5]],
                "landing_point": [4, 5],
            }
        ],
    }
    original = copy.deepcopy(raw)
    transform = _PaperToModelTransform((0, 0, 10, 10), (100, 200, 200, 300), 10, 10)
    mapped = transform.geometry(raw)
    assert mapped["vertices"][0] == mapped["leader_targets"][0] == [120, 230]
    assert mapped["leader_target"] == [120, 230]
    assert mapped["label_point"] == mapped["text_location"] == [140, 250]
    assert mapped["landing_points"] == mapped["dogleg_end_points"] == [[140, 250]]
    assert mapped["annotation_boundary"][0] == [140, 240]
    assert mapped["leader_paths"][0] == {
        "leader_index": 4,
        "line_index": 7,
        "vertices": [[120, 230], [140, 250]],
        "landing_point": [140, 250],
    }
    assert raw == original


def test_projection_of_mirrored_native_border_preserves_contact():
    _, _, _, insert, vertices = fixture(extrusion=(0, 0, -1))
    transform = _PaperToModelTransform((-200, 200, 0, 400), (1000, 2000, 3000, 4000), 10, 10)
    mapped = transform.geometry(_insert_geometry(insert))
    for actual, original in zip(mapped["annotation_boundary"], vertices, strict=True):
        assert actual == pytest.approx(transform.point(list(original)))
