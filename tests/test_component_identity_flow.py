"""A declared component identity must survive the framing/measurement path."""

import copy

import ezdxf
import pytest
from cadquote.component_closeups import render_component_frame_closeups
from cadquote.component_frames import suggest_component_frames
from cadquote.io import sha256_file
from cadquote.measurement_boards import build_measurement_boards


@pytest.mark.parametrize("component_id", ["component:A", "component:B", None, 42, ""])
def test_component_identity_survives_to_measurement_binding(tmp_path, component_id):
    doc = ezdxf.new("R2018")
    doc.units = 4
    model = doc.modelspace()
    model.add_lwpolyline([(100, 100), (220, 100), (220, 200), (100, 200)], close=True)
    dimension = model.add_linear_dim(base=(100, 90), p1=(100, 100), p2=(220, 100)).dimension
    path = tmp_path / "synthetic.dxf"
    doc.saveas(path)
    panel = {
        "id": "sheet",
        "source_file_id": "source",
        "bbox": [0, 0, 400, 300],
        "kind": "elevation",
        "layout": "Model",
    }
    entity = {
        "id": "dimension",
        "source_file_id": "source",
        "sheet_id": "sheet",
        "handle": dimension.dxf.handle,
        "entity_type": "DIMENSION",
        "value": 120,
        "bbox": [100, 90, 220, 100],
        "geometry": {
            "units": "millimeters",
            "defpoint2": [100, 100, 0],
            "defpoint3": [220, 100, 0],
        },
    }
    panels = {"sheets": [panel], "entities": [entity]}
    index = {
        "sources": [
            {
                "source_file_id": "source",
                "source_path": str(path),
                "source_sha256": sha256_file(path),
            }
        ]
    }
    candidate = {
        "occurrence_id": "occ",
        "leader_target": [160, 150],
        "annotation_anchor": [160, 220],
    }
    groups = {
        "groups": [
            {
                "group_id": "group",
                "sheet_id": "sheet",
                "source_file_id": "source",
                "panel_bbox": panel["bbox"],
                "candidates": [candidate],
            }
        ]
    }
    selection = {
        "component_id": component_id,
        "sequence": 9,
        "group_ids": ["group"],
        "selected_occurrence_ids": ["occ"],
    }
    before = copy.deepcopy((selection, groups, panels, index))
    frames = suggest_component_frames(panels, groups, [selection], tmp_path / "frames")
    expected = component_id if isinstance(component_id, str) and component_id else None
    assert frames["records"][0]["component_id"] == expected
    result = render_component_frame_closeups(
        index, panels, frames, tmp_path / "closeups", target_px=512
    )
    record = result["records"][0]
    assert record["component_id"] == expected
    assert record["state"] == "REVIEW"
    assert record["evidence"]
    assert all(e["component_id"] == expected and e["state"] == "REVIEW" for e in record["evidence"])
    takeoff = {
        "measurements": [
            {
                "id": "measurement:owned",
                "component_id": expected or "9",
                "entity_ids": ["dimension"],
                "role": "length",
                "numeric_value": 120,
            }
        ]
    }
    board = build_measurement_boards(panels, result, tmp_path / "boards", takeoff_payload=takeoff)
    boards = board["records"][0]["boards"]
    assert boards and all(b["component_id"] == expected for b in boards)
    candidates = [c for b in boards for c in b["candidates"] if c["entity_id"] == "dimension"]
    assert candidates
    assert all(len(c["measurement_candidates"]) == (1 if expected else 0) for c in candidates)
    assert (selection, groups, panels, index) == before
    assert sha256_file(path) == index["sources"][0]["source_sha256"]


def test_missing_frame_keeps_declared_identity_without_claiming_evidence(tmp_path):
    result = render_component_frame_closeups(
        {},
        {"sheets": []},
        {
            "records": [
                {"component_id": "component:missing", "selection_key": "view-key", "frames": []}
            ]
        },
        tmp_path / "out",
        target_px=512,
    )
    record = result["records"][0]
    assert record["component_id"] == "component:missing"
    assert record["selection_key"] == "view-key"
    assert record["state"] == "MISSING" and record["evidence"] == []
