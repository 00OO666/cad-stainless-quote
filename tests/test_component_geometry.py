from pathlib import Path

import ezdxf
from cadquote.component_geometry import probe_component_geometry


def _payload(source: Path, evidence: list[dict]) -> tuple[dict, dict]:
    return (
        {
            "sources": [
                {
                    "source_file_id": "file:1",
                    "source_path": str(source),
                    "source_sha256": "a" * 64,
                    "units": "millimeters",
                }
            ]
        },
        {
            "records": [
                {
                    "selection_key": "component:1",
                    "sequence": 1,
                    "evidence": evidence,
                }
            ]
        },
    )


def test_component_geometry_expands_nested_blocks_with_world_provenance(
    tmp_path: Path,
):
    source = tmp_path / "nested.dxf"
    document = ezdxf.new()
    nested = document.blocks.new("NEST")
    nested_line = nested.add_line(
        (0, 0),
        (10, 0),
        dxfattribs={"layer": "OBJECT"},
    )
    root = document.blocks.new("ROOT")
    root.add_blockref("NEST", (5, 0))
    root_insert = document.modelspace().add_blockref(
        "ROOT",
        (100, 100),
        dxfattribs={"xscale": 2, "yscale": 2, "rotation": 90},
    )
    document.saveas(source)

    index, closeups = _payload(
        source,
        [
            {
                "source_file_id": "file:1",
                "source_sha256": "a" * 64,
                "sheet_id": "panel:1",
                "render_bbox": [95, 105, 105, 135],
            }
        ],
    )
    result = probe_component_geometry(index, closeups, tmp_path / "out")

    assert result["summary"]["region_count"] == 1
    assert result["summary"]["primitive_count"] == 1
    region = result["regions"][0]
    primitive = region["primitives"][0]
    assert primitive["state"] == "REVIEW"
    assert primitive["measurement_role"] is None
    assert primitive["root_insert_handle"] == root_insert.dxf.handle
    assert primitive["source_block_entity_handle"] == nested_line.dxf.handle
    assert primitive["provenance_state"] == "BLOCK_ENTITY_HANDLE"
    assert primitive["block_path"] == ["ROOT", "NEST"]
    assert primitive["length_method"] == "EXACT"
    assert primitive["length_drawing_units"] == 20.0
    assert primitive["endpoints"] == [[100.0, 110.0], [100.0, 130.0]]
    assert region["path_candidates"][0]["measurement_role"] is None
    assert region["path_candidates"][0]["path_length_candidate_drawing_units"] == 20.0
    assert (tmp_path / "out" / "component_geometry.json").is_file()


def test_component_geometry_supports_curves_and_same_layer_connected_paths(
    tmp_path: Path,
):
    source = tmp_path / "curves.dxf"
    document = ezdxf.new()
    model = document.modelspace()
    first = model.add_line((0, 0), (10, 0), dxfattribs={"layer": "METAL"})
    second = model.add_line((10.2, 0), (20, 0), dxfattribs={"layer": "METAL"})
    model.add_arc((35, 0), 5, 0, 90, dxfattribs={"layer": "CURVE"})
    model.add_circle((50, 0), 4, dxfattribs={"layer": "CURVE"})
    model.add_lwpolyline(
        [(60, 0, 1), (70, 0, 0)],
        format="xyb",
        dxfattribs={"layer": "CURVE"},
    )
    model.add_ellipse((85, 0), major_axis=(8, 0), ratio=0.5, dxfattribs={"layer": "CURVE"})
    model.add_spline([(100, 0), (105, 8), (110, 0)], dxfattribs={"layer": "CURVE"})
    document.saveas(source)

    index, closeups = _payload(
        source,
        [{"source_file_id": "file:1", "sheet_id": "panel:1", "render_bbox": [-5, -15, 120, 15]}],
    )
    result = probe_component_geometry(
        index,
        closeups,
        tmp_path / "out",
        endpoint_tolerance=0.5,
        flattening_tolerance=0.1,
    )

    region = result["regions"][0]
    assert {value["entity_type"] for value in region["primitives"]} == {
        "LINE",
        "ARC",
        "CIRCLE",
        "LWPOLYLINE",
        "ELLIPSE",
        "SPLINE",
    }
    curved = {
        value["entity_type"]: value
        for value in region["primitives"]
        if value["entity_type"] in {"ELLIPSE", "SPLINE"}
    }
    assert all(value["approximation"] for value in curved.values())
    assert all(
        value["approximation_tolerance_drawing_units"] == 0.1
        for value in curved.values()
    )
    first_id = next(
        item["id"]
        for item in region["primitives"]
        if item["top_level_entity_handle"] == first.dxf.handle
    )
    second_id = next(
        item["id"]
        for item in region["primitives"]
        if item["top_level_entity_handle"] == second.dxf.handle
    )
    connected = next(
        value
        for value in region["path_candidates"]
        if set(value["primitive_ids"]) == {first_id, second_id}
    )
    assert connected["primitive_count"] == 2
    assert connected["bbox_width_candidate_drawing_units"] == 20.0
    assert connected["path_length_candidate_drawing_units"] == 19.8
    assert connected["state"] == "REVIEW"


def test_component_geometry_marks_unmappable_nonuniform_block_curve_by_ordinal(
    tmp_path: Path,
):
    source = tmp_path / "anonymous-nonuniform.dxf"
    document = ezdxf.new()
    block = document.blocks.new("*U1")
    block.add_arc((0, 0), 10, 0, 180, dxfattribs={"layer": "PROFILE"})
    root_insert = document.modelspace().add_blockref(
        "*U1",
        (50, 50),
        dxfattribs={"xscale": 2, "yscale": 1},
    )
    document.saveas(source)
    index, closeups = _payload(
        source,
        [{"source_file_id": "file:1", "sheet_id": "panel:1", "render_bbox": [25, 35, 75, 65]}],
    )

    result = probe_component_geometry(index, closeups, tmp_path / "out")

    primitive = result["regions"][0]["primitives"][0]
    assert primitive["entity_type"] == "ELLIPSE"
    assert primitive["root_insert_handle"] == root_insert.dxf.handle
    assert primitive["block_path"] == ["*U1"]
    assert primitive["source_block_entity_handle"] is None
    assert primitive["source_block_entity_ordinal"] == [1]
    assert primitive["provenance_state"] == "BLOCK_ENTITY_ORDINAL_ONLY"
    assert primitive["length_method"] == "APPROXIMATE_FLATTENING"
    assert primitive["approximation_tolerance_drawing_units"] == 0.5


def test_component_geometry_reports_per_region_truncation(tmp_path: Path):
    source = tmp_path / "limited.dxf"
    document = ezdxf.new()
    model = document.modelspace()
    model.add_line((0, 0), (1, 0))
    model.add_line((2, 0), (3, 0))
    model.add_line((4, 0), (5, 0))
    document.saveas(source)
    index, closeups = _payload(
        source,
        [{"source_file_id": "file:1", "sheet_id": "panel:1", "render_bbox": [-1, -1, 6, 1]}],
    )

    result = probe_component_geometry(
        index,
        closeups,
        tmp_path / "out",
        max_primitives_per_region=1,
    )

    region = result["regions"][0]
    assert region["primitive_count"] == 1
    assert region["truncation"]["any"] is True
    assert region["truncation"]["dropped_primitive_count"] == 2
    assert region["truncation"]["flags"] == ["PER_REGION_PRIMITIVE_LIMIT_REACHED"]
    assert result["summary"]["truncated_region_count"] == 1


def test_component_geometry_reports_global_output_truncation(tmp_path: Path):
    source = tmp_path / "global-limit.dxf"
    document = ezdxf.new()
    model = document.modelspace()
    model.add_line((0, 0), (1, 0))
    model.add_line((2, 0), (3, 0))
    document.saveas(source)
    index, closeups = _payload(
        source,
        [{"source_file_id": "file:1", "sheet_id": "panel:1", "render_bbox": [-1, -1, 4, 1]}],
    )

    result = probe_component_geometry(
        index,
        closeups,
        tmp_path / "out",
        max_total_primitives=1,
    )

    region = result["regions"][0]
    assert region["primitive_count"] == 1
    assert region["truncation"]["flags"] == ["GLOBAL_PRIMITIVE_LIMIT_REACHED"]
    assert result["summary"]["global_output_truncated"] is True
    assert result["summary"]["truncated_region_count"] == 1


def test_component_geometry_keeps_missing_sources_review_only(tmp_path: Path):
    index, closeups = _payload(
        tmp_path / "missing.dxf",
        [{"source_file_id": "file:1", "sheet_id": "panel:1", "render_bbox": [0, 0, 10, 10]}],
    )

    result = probe_component_geometry(index, closeups, tmp_path / "out")

    region = result["regions"][0]
    assert region["state"] == "REVIEW"
    assert region["usable"] is False
    assert "SOURCE_DXF_MISSING" in region["reason_codes"]
    assert result["summary"]["primitive_count"] == 0
    assert result["summary"]["issue_count"] == 1
