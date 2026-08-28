from __future__ import annotations

from pathlib import Path

from cadquote.geometry_boards import build_geometry_boards
from PIL import Image


def _closeups(image_path: Path) -> dict:
    return {
        "records": [
            {
                "selection_key": "component:test",
                "component_id": "component:test",
                "sequence": 7,
                "evidence": [
                    {
                        "source_file_id": "file:1",
                        "sheet_id": "panel:1",
                        "render_bbox": [0, 0, 100, 50],
                        "absolute_path": str(image_path),
                    }
                ],
            }
        ]
    }


def _geometry(*, truncated: bool = False) -> dict:
    primitives = [
        {
            "id": "geometry:p1",
            "measurement_role": "length",
            "entity_type": "LINE",
            "layer": "METAL",
            "bbox": [10, 10, 20, 10],
            "length_drawing_units": 10,
            "length_method": "EXACT",
            "provenance_state": "TOP_LEVEL_HANDLE",
            "top_level_entity_handle": "A1",
            "top_level_entity_ordinal": 1,
            "block_path": [],
        },
        {
            "id": "geometry:p2",
            "measurement_role": None,
            "entity_type": "LINE",
            "layer": "METAL",
            "bbox": [20, 10, 30, 10],
            "length_drawing_units": 10,
            "length_method": "EXACT",
            "provenance_state": "BLOCK_ENTITY_HANDLE",
            "root_insert_handle": "B1",
            "source_block_entity_handle": "B2",
            "top_level_entity_ordinal": 2,
            "block_path": ["FRAME"],
        },
        {
            "id": "geometry:p3",
            "measurement_role": None,
            "entity_type": "LWPOLYLINE",
            "layer": "TRIM",
            "bbox": [60, 10, 80, 30],
            "length_drawing_units": 60,
            "length_method": "EXACT",
            "provenance_state": "BLOCK_ENTITY_ORDINAL_ONLY",
            "root_insert_handle": "C1",
            "source_block_entity_ordinal": [3],
            "top_level_entity_ordinal": 3,
            "block_path": ["*U1"],
        },
    ]
    return {
        "summary": {"global_output_truncated": False},
        "regions": [
            {
                "region_id": "component-region:1",
                "selection_key": "component:test",
                "sequence": 7,
                "evidence_index": 1,
                "source_file_id": "file:1",
                "sheet_id": "panel:1",
                "render_bbox": [0, 0, 100, 50],
                "usable": True,
                "truncation": {"any": truncated, "flags": ["LIMIT"] if truncated else []},
                "primitives": primitives,
                "path_candidates": [
                    {
                        "id": "path:1",
                        "measurement_role": "width",
                        "layer": "METAL",
                        "bbox": [10, 10, 30, 10],
                        "path_length_candidate_drawing_units": 20,
                        "length_method": "EXACT",
                        "primitive_ids": ["geometry:p1", "geometry:p2"],
                        "primitive_count": 2,
                    }
                ],
            }
        ],
    }


def test_geometry_board_labels_paths_and_primitives_with_provenance(tmp_path: Path) -> None:
    image_path = tmp_path / "closeup.png"
    Image.new("RGB", (800, 400), "#111827").save(image_path)

    result = build_geometry_boards(
        _closeups(image_path),
        _geometry(),
        tmp_path / "out",
    )

    assert result["board_count"] == 1
    assert result["missing_count"] == 0
    assert result["input_role_ignored_count"] == 1
    record = result["records"][0]
    assert "INPUT_MEASUREMENT_ROLE_IGNORED" in record["reason_codes"]
    board = record["board"]
    assert Path(board["board_absolute_path"]).is_file()
    candidates = board["candidates"]
    assert [candidate["candidate_label"] for candidate in candidates] == [
        "G1",
        "G2",
        "G3",
        "G4",
    ]
    assert candidates[0]["candidate_kind"] == "path"
    assert candidates[0]["path_id"] == "path:1"
    assert candidates[0]["bbox_width_drawing_units"] == 20
    assert candidates[0]["bbox_height_drawing_units"] == 0
    assert candidates[0]["path_length_drawing_units"] == 20
    assert candidates[0]["layer"] == "METAL"
    assert candidates[0]["provenance"]["primitive_ids"] == ["geometry:p1", "geometry:p2"]
    assert candidates[1]["primitive_id"] == "geometry:p1"
    assert candidates[1]["provenance"]["top_level_entity_handle"] == "A1"
    assert candidates[1]["selected_by_category"] == "top_level_handle_primitive"
    assert candidates[2]["selected_by_category"] == "block_handle_primitive"
    assert all(candidate["measurement_role"] is None for candidate in candidates)
    assert result["policy"]["auto_assign_measurement_role"] is False
    assert result["policy"]["selection_strategy"] == "deterministic_balanced_round_robin"


def test_geometry_board_marks_candidate_and_source_truncation(tmp_path: Path) -> None:
    image_path = tmp_path / "closeup.png"
    Image.new("RGB", (400, 200), "#111827").save(image_path)

    result = build_geometry_boards(
        _closeups(image_path),
        _geometry(truncated=True),
        tmp_path / "out",
        maximum_per_image=2,
    )

    assert result["truncated_count"] == 1
    record = result["records"][0]
    assert record["candidate_count"] == 2
    assert record["total_candidate_count"] == 4
    assert record["candidate_truncated"] is True
    assert record["source_geometry_truncated"] is True
    assert "GEOMETRY_BOARD_CANDIDATES_TRUNCATED" in record["reason_codes"]
    assert "SOURCE_GEOMETRY_TRUNCATED" in record["reason_codes"]


def test_geometry_board_small_cap_preserves_closed_primitive_and_path(tmp_path: Path) -> None:
    image_path = tmp_path / "closeup.png"
    Image.new("RGB", (400, 200), "#111827").save(image_path)
    geometry = _geometry()
    geometry["regions"][0]["primitives"][2]["closed"] = True
    original_path = geometry["regions"][0]["path_candidates"][0]
    geometry["regions"][0]["path_candidates"].extend(
        {**original_path, "id": f"path:extra:{index:02d}"} for index in range(20)
    )

    result = build_geometry_boards(
        _closeups(image_path),
        geometry,
        tmp_path / "out",
        maximum_per_image=2,
    )

    candidates = result["records"][0]["board"]["candidates"]
    assert [(value["candidate_kind"], value["geometry_id"]) for value in candidates] == [
        ("primitive", "geometry:p3"),
        ("path", "path:1"),
    ]
    assert candidates[0]["selected_by_category"] == "significant_closed_primitive"
    assert candidates[1]["selected_by_category"] == "multi_primitive_path"
    assert result["records"][0]["total_candidate_count"] == 24
    assert result["records"][0]["candidate_truncated"] is True


def test_geometry_board_fails_closed_when_closeup_is_missing(tmp_path: Path) -> None:
    result = build_geometry_boards(
        _closeups(tmp_path / "missing.png"),
        _geometry(),
        tmp_path / "out",
    )

    assert result["board_count"] == 0
    assert result["missing_count"] == 1
    assert result["records"][0]["state"] == "MISSING"
    assert "CLOSEUP_IMAGE_MISSING" in result["records"][0]["reason_codes"]


def test_geometry_boards_cli_parser() -> None:
    from cad_quote import build_parser

    args = build_parser().parse_args(
        [
            "geometry-boards",
            "component_closeups.json",
            "component_geometry.json",
            "--out",
            "boards",
            "--maximum-per-image",
            "42",
            "--maximum-boards",
            "9",
        ]
    )

    assert args.maximum_per_image == 42
    assert args.maximum_boards == 9
