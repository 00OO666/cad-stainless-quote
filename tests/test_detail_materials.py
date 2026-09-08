import ezdxf
import pytest
from cadquote.detail_materials import probe_detail_materials, probe_routed_details


def fixture():
    doc = ezdxf.new("R2018")
    paper = doc.layouts.get("Layout1")
    vp = paper.add_viewport(
        center=(50, 50), size=(100, 100), view_center_point=(50, 50), view_height=100
    )
    block = doc.blocks.new("synthetic-material-label")
    block.add_lwpolyline([(0, 0), (40, 0), (40, 12), (0, 12)], close=True)
    ins = paper.add_blockref(block.name, (0, -20))
    ins.add_attrib("CUSTOM", "GC-SS-907", (10, -15), dxfattribs={"height": 1})
    leader = paper.add_leader([(20, 50), (-10, -14), (0, -14)])
    poly = doc.modelspace().add_lwpolyline([(20, 45), (20, 55), (25, 55)])
    args = {
        "source_file_id": "synthetic",
        "sheet_id": "node",
        "layout_name": "Layout1",
        "viewport_handle": vp.dxf.handle,
        "paper_bbox": [-20, -30, 100, 100],
    }
    return doc, args, leader, poly


def test_native_code_border_arrow_and_profile_chain_without_quantity():
    doc, args, leader, poly = fixture()
    out = probe_detail_materials(doc, **args)
    assert len(out["probes"]) == 1
    p = out["probes"][0]
    assert p["material_code"] == "GC-SS-907" and p["leader_handle"] == leader.dxf.handle
    assert p["model_tip"] == pytest.approx([20, 50])
    assert p["profile_candidates"][0]["handle"] == poly.dxf.handle
    assert p["profile_candidates"][0]["length_raw"] == pytest.approx(15)
    assert p["measurement_role"] is None and p["state"] == "REVIEW"
    assert not out["mutates_takeoff"] and not out["truncated"]


def test_nearby_label_without_contact_is_not_selected():
    doc, args, leader, _ = fixture()
    leader.set_vertices([(20, 50), (-10, -14), (-1, -14)])
    assert probe_detail_materials(doc, **args)["probes"] == []


def test_nested_profile_not_claimed_as_complete():
    doc, args, _, poly = fixture()
    doc.modelspace().delete_entity(poly)
    b = doc.blocks.new("nested-profile")
    b.add_lwpolyline([(20, 45), (20, 55)])
    doc.modelspace().add_blockref(b.name, (0, 0))
    out = probe_detail_materials(doc, **args)
    assert out["probes"][0]["profile_candidates"] == []
    assert any("nested" in t for t in out["limitations"])


def test_scan_cap_is_explicit_and_wrong_viewport_fails():
    doc, args, _, _ = fixture()
    doc.modelspace().add_line((0, 0), (1, 1))
    assert probe_detail_materials(doc, **args, max_model_entities=1)["truncated"]
    with pytest.raises(ValueError, match="viewport"):
        probe_detail_materials(doc, **{**args, "viewport_handle": "MISSING"})


def test_source_hash_mismatch_cannot_open_as_valid_candidate(tmp_path):
    doc, args, _, _ = fixture()
    path = tmp_path / "synthetic.dxf"
    doc.saveas(path)
    index = {
        "sources": [
            {"source_file_id": "synthetic", "source_path": str(path), "source_sha256": "invalid"}
        ]
    }
    node = {"sheet_id": "node", "source_file_id": "synthetic"}
    routes = {"records": [{"suggested_detail_sheet_id": "node", "candidates": [node]}]}
    result = probe_routed_details(index, routes)
    assert result["completed_nodes"] == 0
    assert "hash mismatch" in result["issues"][0]["reason"]


def test_hidden_paper_leader_does_not_supply_material_evidence():
    doc, args, leader, _ = fixture()
    layer = doc.layers.new("hidden-leader")
    layer.off()
    leader.dxf.layer = layer.dxf.name
    assert probe_detail_materials(doc, **args)["probes"] == []
