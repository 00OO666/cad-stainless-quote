from copy import deepcopy

import ezdxf
import pytest
from cadquote.cad_index import _record_entity
from cadquote.io import sha256_file
from cadquote.route_context import refresh_native_route_frames
from ezdxf import bbox


def fixture(tmp_path):
    doc = ezdxf.new("R2018")
    layout = doc.layouts.get("Layout1")
    block = doc.blocks.new("offset-frame")
    block.add_lwpolyline([(900, 900), (1200, 900), (1200, 1200), (900, 1200)], close=True)
    ins = layout.add_blockref(block.name, (-1000, -1000))
    att = ins.add_attrib("PAGE", "B3-QS-09", (-75, -75))
    vp = layout.add_viewport(
        center=(50, 50), size=(100, 100), view_center_point=(0, 0), view_height=100
    )
    path = tmp_path / "frame.dxf"
    doc.saveas(path)
    cache = bbox.Cache()
    ent = _record_entity(ins, "cad", "paper", "paper:Layout1", cache)
    ent.bbox = (-1000, -1000, -1000, -1000)
    child = _record_entity(
        att, "cad", "paper", "paper:Layout1", cache, parent_insert_handle=ins.dxf.handle
    )
    index = {
        "sources": [
            {"source_file_id": "cad", "source_path": str(path), "source_sha256": sha256_file(path)}
        ]
    }
    return index, [ent, child], path, vp


def test_moved_block_frame_not_insertion_point_and_input_unchanged(tmp_path):
    index, entities, path, vp = fixture(tmp_path)
    before, file_before = deepcopy(entities), path.read_bytes()
    refreshed, report = refresh_native_route_frames(index, entities)
    assert report["refreshed_frames"] == 1 and not report["incomplete"]
    assert refreshed[0].bbox == pytest.approx((-100, -100, 200, 200))
    assert report["records"][0]["viewport_handles"] == [vp.dxf.handle]
    assert entities == before and path.read_bytes() == file_before
    assert not report["mutates_index"]


def test_wrong_hash_never_refreshes(tmp_path):
    index, entities, _, _ = fixture(tmp_path)
    index["sources"][0]["source_sha256"] = "wrong"
    refreshed, report = refresh_native_route_frames(index, entities)
    assert refreshed == entities and report["refreshed_frames"] == 0
    assert report["incomplete"] and "hash mismatch" in report["issues"][0]["reason"]


def test_hidden_attribute_is_not_page_evidence(tmp_path):
    index, entities, _, _ = fixture(tmp_path)
    entities[1].geometry["semantic_hidden"] = True
    assert refresh_native_route_frames(index, entities)[1]["candidate_sources"] == 0


def test_different_source_same_handle_cannot_supply_page_attribute(tmp_path):
    index, entities, _, _ = fixture(tmp_path)
    entities[1].source_file_id = "another-source"
    assert refresh_native_route_frames(index, entities)[1]["refreshed_frames"] == 0


def test_cap_reports_incomplete_not_negative_evidence(tmp_path):
    index, entities, _, _ = fixture(tmp_path)
    entities.extend(
        [
            e.model_copy(update={"id": e.id + "other", "source_file_id": "other"})
            for e in list(entities)
        ]
    )
    _, report = refresh_native_route_frames(index, entities, max_sources=1)
    assert report["incomplete"] and any(i["reason"] == "SOURCE_CAP" for i in report["issues"])


def test_viewport_not_inside_insert_is_not_frame(tmp_path):
    index, entities, path, _ = fixture(tmp_path)
    doc = ezdxf.readfile(path)
    doc.layouts.get("Layout1").query("VIEWPORT")[-1].dxf.center = (10000, 10000)
    doc.saveas(path)
    index["sources"][0]["source_sha256"] = sha256_file(path)
    assert refresh_native_route_frames(index, entities)[1]["refreshed_frames"] == 0


def test_changed_during_read_discards_all_source_updates(tmp_path, monkeypatch):
    index, entities, _, _ = fixture(tmp_path)
    values = iter([index["sources"][0]["source_sha256"], "changed"])
    monkeypatch.setattr("cadquote.route_context.sha256_file", lambda _: next(values))
    out, report = refresh_native_route_frames(index, entities)
    assert out == entities and not report["records"] and report["incomplete"]
