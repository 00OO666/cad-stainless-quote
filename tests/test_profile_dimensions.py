import copy

import ezdxf
import pytest
from cadquote.group_materials import native_group_dimension_record
from cadquote.profile_dimensions import bind_boundary_dimensions, resolve_group_profile_dimensions
from ezdxf import bbox


def fixture():
    doc = ezdxf.new()
    doc.header["$INSUNITS"] = 0
    a = [(0, 0), (0, -8), (9, -8), (9, 12), (89, 12), (89, -8), (98, -8), (98, 0)]
    b = [(2, 0), (2, -6), (7, -6), (7, 14), (91, 14), (91, -6), (96, -6), (96, 0)]
    pa = doc.modelspace().add_lwpolyline(a)
    pb = doc.modelspace().add_lwpolyline(b)
    vp = doc.layouts.get("Layout1").add_viewport(
        center=(50, 50), size=(200, 200), view_center_point=(50, 0), view_height=200
    )
    group = {
        "group_id": "synthetic-group",
        "source_file_id": "synthetic-source",
        "probes": [
            {
                "leader_handle": "synthetic-leader",
                "annotation_handle": "synthetic-label",
                "material_code": "GC-SS-TEST",
                "viewport_handle": vp.dxf.handle,
                "profile_candidates": [{"handle": pb.dxf.handle}],
            }
        ],
        "dimension_candidates": [],
        "truncated": False,
    }

    def dim(p1, p2, angle=0, text="<>", factor=1):
        d = doc.modelspace().add_linear_dim(
            base=(30, 30), p1=p1, p2=p2, angle=angle, text=text, override={"dimlfac": factor}
        )
        d.render()
        e = native_group_dimension_record(
            d.dimension, group["source_file_id"], group["group_id"], "model", bbox.Cache()
        )
        group["dimension_candidates"].append(
            {
                "entity": e.model_dump(mode="json"),
                "coordinate_space": "model",
                "visible_in": [{"viewport_handle": vp.dxf.handle}],
            }
        )
        return d.dimension

    dim(a[0], a[1], 90)
    dim(a[1], a[2])
    return doc, group, pa, pb, dim


def test_contact_skin_is_not_nominal_dimension_skin_and_unknown_units_stay_unknown():
    doc, group, a, b, _ = fixture()
    frozen = copy.deepcopy(group)
    out = resolve_group_profile_dimensions(doc, group)
    r = out["records"][0]
    assert r["contact_handles"] == [b.dxf.handle]
    assert r["selected_boundary_handle"] == a.dxf.handle
    assert r["nominal_segments_raw"] == pytest.approx([8, 9, 20, 80, 20, 9, 8])
    assert r["nominal_boundary_sum_raw"] == pytest.approx(154)
    assert r["candidates"][0]["normal_separation_raw"] == pytest.approx(2)
    assert out["source_insunits"] == 0
    assert r["unfolded_width_mm"] is None and r["physical_quantity"] is None
    assert out["manufacturing_blank_width_mm"] is None and group == frozen


def test_normal_projection_requires_both_vertices_on_profile():
    doc, group, a, _, dim = fixture()
    dim((9, -8), (89, -8))
    dim((9, -50), (89, -50))  # same value, foreign anchors
    xy = [list(p[:2]) for p in a.get_points()]
    out = bind_boundary_dimensions(xy, group["dimension_candidates"])
    projected = [b for b in out["bindings"] if b["binding"] == "PROFILE_VERTEX_NORMAL_PROJECTION"]
    assert len(projected) == 1 and projected[0]["segment_index"] == 3


@pytest.mark.parametrize(
    "kind",
    [
        "override",
        "scale",
        "other_skin",
        "duplicate_skin",
        "foreign_source",
        "foreign_group",
        "foreign_view",
        "one_axis",
        "other_hit",
        "truncated",
        "no_pair",
        "hidden",
        "curved",
        "wide",
        "clipped",
    ],
)
def test_conflict_or_incomplete_scope_cannot_select(kind):
    doc, group, a, b, dim = fixture()
    if kind == "override":
        dim((0, 0), (0, -8), 90, text="17")
    elif kind == "scale":
        dim((0, 0), (0, -8), 90, factor=3)
    elif kind == "other_skin":
        dim((2, 0), (2, -6), 90)
        dim((2, -6), (7, -6))
    elif kind == "duplicate_skin":
        doc.modelspace().add_lwpolyline(list(a.get_points("xy")))
    elif kind in {"foreign_source", "foreign_group", "foreign_view"}:
        for d in group["dimension_candidates"]:
            if kind == "foreign_view":
                d["visible_in"] = [{"viewport_handle": "unknown"}]
            else:
                d["entity"]["source_file_id" if kind == "foreign_source" else "sheet_id"] = "wrong"
    elif kind == "one_axis":
        group["dimension_candidates"].pop()
    elif kind == "other_hit":
        group["probes"][0]["profile_candidates"].append({"handle": a.dxf.handle})
    elif kind == "truncated":
        group["truncated"] = True
    elif kind == "no_pair":
        a.translate(0, 30, 0)
    elif kind == "hidden":
        a.dxf.invisible = 1
    elif kind == "curved":
        points = list(a.get_points("xyb"))
        points[0] = (points[0][0], points[0][1], 0.5)
        a.set_points(points, format="xyb")
    elif kind == "wide":
        a.dxf.const_width = 2
    elif kind == "clipped":
        doc.entitydb[group["probes"][0]["viewport_handle"]].dxf.width = 50
    out = resolve_group_profile_dimensions(doc, group)
    assert all(r["selected_boundary_handle"] is None for r in out["records"])


def test_model_scan_cap_returns_no_partial_selection():
    doc, group, *_ = fixture()
    out = resolve_group_profile_dimensions(doc, group, max_model_entities=1)
    assert out["truncated"] and out["records"] == []


def test_reverse_vertex_order_is_not_a_different_strip():
    doc, group, a, _, _ = fixture()
    a.set_points(list(reversed(list(a.get_points("xy")))), format="xy")
    out = resolve_group_profile_dimensions(doc, group)["records"][0]
    assert out["selected_boundary_handle"] == a.dxf.handle
    assert out["nominal_boundary_sum_raw"] == pytest.approx(154)


@pytest.mark.parametrize("mode", ["missing_rounding", "rounding", "angle_dimension"])
def test_legacy_or_non_linear_dimension_metadata_cannot_supply_proof(mode):
    doc, group, *_ = fixture()
    for d in group["dimension_candidates"]:
        g = d["entity"]["geometry"]
        if mode == "missing_rounding":
            g.pop("rounding_increment")
        elif mode == "rounding":
            g["rounding_increment"] = 5
        else:
            g["dimtype"] = 34
    out = resolve_group_profile_dimensions(doc, group)
    assert all(r["selected_boundary_handle"] is None for r in out["records"])
