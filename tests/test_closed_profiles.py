import copy
import math

import ezdxf
import pytest
from cadquote.closed_profiles import probe_closed_group_profiles
from cadquote.group_materials import native_group_dimension_record, probe_node_view_group
from cadquote.strip_profiles import analyze_strip_geometry, analyze_strip_outline
from ezdxf import bbox
from shapely.geometry import LineString
from test_detail_materials import fixture as material_fixture


def shape():
    return list(
        LineString([(0, 20), (0, 0), (30, 0), (30, 15), (55, 15)])
        .buffer(1, cap_style=2, join_style=2)
        .exterior.coords
    )[:-1]


def fixture():
    doc = ezdxf.new()
    doc.units = 0
    points = shape()
    poly = doc.modelspace().add_lwpolyline(points, close=True)
    layout = doc.layouts.get("Layout1")
    vp = layout.add_viewport(
        center=(50, 50), size=(200, 200), view_center_point=(25, 10), view_height=200
    )
    group = {
        "group_id": "synthetic-group",
        "source_file_id": "synthetic-source",
        "space": "paper:Layout1",
        "truncated": False,
        "dimension_candidates": [],
        "probes": [
            {
                "leader_handle": "synthetic-leader",
                "annotation_handle": "synthetic-label",
                "material_code": "GC-SS-TEST",
                "viewport_handle": vp.dxf.handle,
                "profile_candidates": [
                    {
                        "handle": poly.dxf.handle,
                        "closed": True,
                        "vertices": [list(p) for p in points + [points[0]]],
                    }
                ],
            }
        ],
    }
    raw = analyze_strip_geometry(
        points, source_file_id="synthetic-source", entity_handle=poly.dxf.handle
    )
    skin = raw["candidates"][0]["skin_a_points"]

    def dim(p1, p2, text="<>"):
        angle = math.degrees(math.atan2(p2[1] - p1[1], p2[0] - p1[0]))
        d = doc.modelspace().add_linear_dim(base=(50, 50), p1=p1, p2=p2, angle=angle, text=text)
        d.render()
        record = native_group_dimension_record(
            d.dimension, group["source_file_id"], group["group_id"], "model", bbox.Cache()
        )
        group["dimension_candidates"].append(
            {
                "entity": record.model_dump(mode="json"),
                "coordinate_space": "model",
                "visible_in": [{"viewport_handle": vp.dxf.handle}],
            }
        )

    dim(skin[0], skin[1])
    dim(skin[1], skin[2])
    return doc, group, poly, vp, dim


def test_raw_core_does_not_label_unknown_geometry_as_millimetres():
    raw = analyze_strip_geometry(shape(), source_file_id="synthetic", entity_handle="P")
    assert raw["profile_state"] == "UNIQUE_GEOMETRIC_STRIP"
    assert raw["candidates"][0]["geometric_middle_path_length_raw"] == pytest.approx(90)
    assert raw["outline_perimeter_raw"] == pytest.approx(184)
    assert raw["unfolded_width_raw"] is None
    assert '"geometric_middle_path_length_mm"' not in str(raw)
    assert analyze_strip_outline(
        shape(), source_file_id="synthetic", entity_handle="P", units="unknown"
    )["issues"] == ["MILLIMETRE_UNITS_NOT_VERIFIED"]


def test_native_closed_skin_dimension_support_and_input_immutability():
    doc, group, poly, _, _ = fixture()
    before, native = copy.deepcopy(group), list(poly.get_points())
    out = probe_closed_group_profiles(doc, group)
    r = out["records"][0]
    assert r["binding_state"] == "DIMENSION_SUPPORTED_CLOSED_SKIN_CANDIDATE"
    s = r["selected_boundary"]
    assert s["outline_handle"] == poly.dxf.handle
    assert len(s["bindings"]) == 2 and len(s["undimensioned_segment_indices"]) == 2
    assert out["source_insunits"] == 0 and out["unfolded_width_mm"] is None
    assert out["physical_quantity"] is None and not out["mutates_takeoff"]
    assert group == before and list(poly.get_points()) == native


@pytest.mark.parametrize(
    "mode",
    [
        "no_dimensions",
        "one_axis",
        "other_skin",
        "override",
        "foreign_source",
        "foreign_group",
        "foreign_view",
        "changed",
        "open",
        "curved",
        "wide",
        "hidden",
        "frozen",
        "clipped",
        "wrong_layout",
        "competing_hit",
        "truncated",
    ],
)
def test_unsupported_or_ambiguous_evidence_cannot_select(mode):
    doc, group, poly, vp, dim = fixture()
    if mode == "no_dimensions":
        group["dimension_candidates"] = []
    elif mode == "one_axis":
        group["dimension_candidates"].pop()
    elif mode in {"other_skin", "override"}:
        raw = analyze_strip_geometry(
            shape(), source_file_id="synthetic", entity_handle=poly.dxf.handle
        )
        side = "skin_b_points" if mode == "other_skin" else "skin_a_points"
        points = raw["candidates"][0][side]
        dim(points[0], points[1], text="<>" if mode == "other_skin" else "123")
        if mode == "other_skin":
            dim(points[1], points[2])
    elif mode.startswith("foreign_"):
        for d in group["dimension_candidates"]:
            if mode == "foreign_view":
                d["visible_in"] = [{"viewport_handle": "not-this-view"}]
            else:
                d["entity"]["source_file_id" if mode == "foreign_source" else "sheet_id"] = "other"
    elif mode == "changed":
        poly.translate(1, 0, 0)
    elif mode == "open":
        poly.closed = False
    elif mode == "curved":
        points = list(poly.get_points())
        points[0] = (*points[0][:4], 0.5)
        poly.set_points(points)
    elif mode == "wide":
        poly.dxf.const_width = 2
    elif mode == "hidden":
        poly.dxf.invisible = 1
    elif mode == "frozen":
        doc.layers.get(poly.dxf.layer).freeze()
    elif mode == "clipped":
        vp.dxf.width = 10
    elif mode == "wrong_layout":
        group["space"] = "paper:Other"
    elif mode == "competing_hit":
        group["probes"][0]["profile_candidates"].append(
            copy.deepcopy(group["probes"][0]["profile_candidates"][0])
        )
    elif mode == "truncated":
        group["truncated"] = True
    out = probe_closed_group_profiles(doc, group)
    assert all(r["selected_boundary"] is None for r in out["records"])


def test_geometry_still_available_without_dimension_support():
    doc, group, *_ = fixture()
    group["dimension_candidates"] = []
    r = probe_closed_group_profiles(doc, group)["records"][0]
    assert r["selected_boundary"] is None
    assert r["profiles"][0]["profile_state"] == "UNIQUE_GEOMETRIC_STRIP"


def test_caps_are_explicit_and_never_release_a_partial_result():
    doc, group, *_ = fixture()
    out = probe_closed_group_profiles(doc, group, max_vertices=3)
    assert out["truncated"] and out["records"][0]["selected_boundary"] is None


def test_existing_native_group_entry_includes_closed_probe():
    doc, args, _, poly = material_fixture()
    doc.modelspace().delete_entity(poly)
    points = [(x + 20, y + 30) for x, y in shape()]
    closed = doc.modelspace().add_lwpolyline(points, close=True)
    group = {
        "group_id": "g",
        "source_file_id": "synthetic",
        "space": "paper:Layout1",
        "group_state": "UNIQUE_TITLE_GROUP_CANDIDATE",
        "paper_context_bbox": [0, -30, 100, 100],
        "paper_frame_bbox": [-30, -40, 150, 150],
        "members": [{"viewport_handle": args["viewport_handle"], "paper_bbox": [0, 0, 100, 100]}],
    }
    out = probe_node_view_group(doc, group)
    closed_results = out["closed_strip_profiles"]["records"]
    assert len(closed_results) == 1
    assert closed_results[0]["contact_handles"] == [closed.dxf.handle]
    assert closed_results[0]["profiles"][0]["profile_state"] == "UNIQUE_GEOMETRIC_STRIP"
