import pytest
from cadquote.group_materials import probe_node_view_group, probe_routed_groups
from test_detail_materials import fixture as material_fixture


def fixture():
    doc, args, leader, poly = material_fixture()
    layout = doc.layouts.get("Layout1")
    second = layout.add_viewport(
        center=(154, 50), size=(100, 100), view_center_point=(2050, 50), view_height=100
    )
    other_leader = layout.add_leader([(124, 50), (30, -14), (40, -14)])
    other_poly = doc.modelspace().add_lwpolyline([(2020, 45), (2020, 55), (2025, 55)])
    group = {
        "group_id": "g",
        "source_file_id": "synthetic",
        "space": "paper:Layout1",
        "group_state": "UNIQUE_TITLE_GROUP_CANDIDATE",
        "paper_context_bbox": [0, 0, 204, 100],
        "paper_frame_bbox": [-30, -40, 250, 150],
        "members": [
            {"viewport_handle": args["viewport_handle"], "paper_bbox": [0, 0, 100, 100]},
            {"viewport_handle": second.dxf.handle, "paper_bbox": [104, 0, 204, 100]},
        ],
    }
    return doc, group, leader, poly, other_leader, other_poly


def test_leader_outside_view_and_independent_member_projection():
    doc, group, first, first_poly, second, second_poly = fixture()
    out = probe_node_view_group(doc, group)
    probes = {p["leader_handle"]: p for p in out["probes"]}
    assert probes[first.dxf.handle]["model_tip"] == pytest.approx([20, 50])
    assert probes[second.dxf.handle]["model_tip"] == pytest.approx([2020, 50])
    assert probes[first.dxf.handle]["profile_candidates"][0]["handle"] == first_poly.dxf.handle
    assert probes[second.dxf.handle]["profile_candidates"][0]["handle"] == second_poly.dxf.handle
    assert out["paper_evidence_bbox"][1] <= -20
    assert out["physical_quantity"] is None and out["measurement_role"] is None
    assert all(len(m["native_paper_to_model_matrix"]) == 16 for m in out["member_results"])


def test_paper_dimension_crossing_fragments_remains_raw_candidate():
    doc, group, *_ = fixture()
    dim = doc.layouts.get("Layout1").add_linear_dim(
        base=(0, 80), p1=(20, 50), p2=(124, 50), override={"dimlfac": 9}
    )
    dim.render()
    out = probe_node_view_group(doc, group)
    d = out["dimension_candidates"][0]
    assert d["crosses_broken_viewports"] and d["dimension_role"] is None
    assert d["entity"]["geometry"]["measurement_factor"] == 9
    assert d["entity"]["value"] == pytest.approx(936)
    assert out["measurement_role"] is None  # not recomputed as 2000 across model windows


def test_model_dimensions_preserve_native_coordinates_and_unit_uncertainty():
    doc, group, *_ = fixture()
    doc.header["$INSUNITS"] = 0
    dim = doc.modelspace().add_linear_dim(base=(2000, 60), p1=(2010, 50), p2=(2060, 50))
    dim.render()
    out = probe_node_view_group(doc, group)
    d = next(d for d in out["dimension_candidates"] if d["coordinate_space"] == "model")
    assert d["entity"]["value"] == pytest.approx(50)
    assert d["visible_in"][0]["viewport_handle"] == group["members"][1]["viewport_handle"]
    assert d["visible_in"][0]["paper_extension_points"][0] == pytest.approx([114, 50])
    assert out["source_insunits"] == 0 and d["dimension_role"] is None


def test_overlapping_member_tip_is_not_selected():
    doc, group, *_ = fixture()
    second = doc.entitydb[group["members"][1]["viewport_handle"]]
    second.dxf.center = (50, 50)
    group["members"][1]["paper_bbox"] = [0, 0, 100, 100]
    out = probe_node_view_group(doc, group)
    assert out["probes"] == []
    assert {i["reason"] for i in out["issues"]} == {"LEADER_TIP_VIEWPORT_AMBIGUOUS"}


def test_changed_footprint_and_ambiguous_group_fail_closed():
    doc, group, *_ = fixture()
    group["members"][0]["paper_bbox"][0] = -1
    with pytest.raises(ValueError, match="footprint"):
        probe_node_view_group(doc, group)
    group["group_state"] = "AMBIGUOUS_TITLE_GROUP"
    with pytest.raises(ValueError, match="unambiguous"):
        probe_node_view_group(doc, group)


def test_group_probe_cap_and_hash_mismatch_are_visible(tmp_path):
    doc, group, *_ = fixture()
    path = tmp_path / "native.dxf"
    doc.saveas(path)
    source = {"source_file_id": "synthetic", "source_path": str(path), "source_sha256": "wrong"}
    routes = {
        "node_view_groups": {"groups": [group]},
        "records": [{"suggested_detail_group_id": "g"}, {"suggested_detail_group_id": "z"}],
    }
    out = probe_routed_groups({"sources": [source]}, routes, max_groups=1)
    assert out["completed_groups"] == 0 and out["truncated"]
    assert any("hash mismatch" in i["reason"] for i in out["issues"])
    assert any(i["reason"] == "GROUP_PROBE_CAP" for i in out["issues"])
