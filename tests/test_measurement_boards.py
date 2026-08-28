from __future__ import annotations

from pathlib import Path

from cadquote.measurement_boards import build_measurement_boards
from PIL import Image


def _closeups(image_path: Path) -> dict:
    return {
        "records": [
            {
                "selection_key": "component:test",
                "component_id": "component:test",
                "sequence": 1,
                "evidence": [
                    {
                        "sheet_id": "sheet:1",
                        "source_file_id": "file:1",
                        "drawing_number": "E-01",
                        "kind": "elevation",
                        "render_bbox": [0, 0, 1_000, 500],
                        "absolute_path": str(image_path),
                    }
                ],
            }
        ]
    }


def test_measurement_board_labels_exact_cad_entities(tmp_path: Path) -> None:
    image_path = tmp_path / "closeup.png"
    Image.new("RGB", (1_000, 500), "#111827").save(image_path)
    panels = {
        "entities": [
            {
                "id": "entity:dimension",
                "handle": "A1",
                "sheet_id": "sheet:1",
                "entity_type": "DIMENSION",
                "value": 3_000,
                "bbox": [100, 100, 400, 140],
                "geometry": {
                    "display_measurement": 3_000,
                    "units": "mm",
                    "defpoint2": [100, 100, 0],
                    "defpoint3": [400, 100, 0],
                },
            },
            {
                "id": "entity:qty",
                "handle": "A2",
                "sheet_id": "sheet:1",
                "entity_type": "ATTRIB",
                "text": "2",
                "insert": [500, 250],
                "geometry": {"tag": "QTY"},
            },
            {
                "id": "entity:ordinary-text",
                "handle": "A3",
                "sheet_id": "sheet:1",
                "entity_type": "TEXT",
                "text": "room 101",
                "insert": [600, 250],
                "geometry": {},
            },
            {
                "id": "entity:outside",
                "handle": "A4",
                "sheet_id": "sheet:1",
                "entity_type": "DIMENSION",
                "value": 9_999,
                "bbox": [2_000, 2_000, 2_100, 2_050],
                "geometry": {"display_measurement": 9_999},
            },
        ]
    }

    takeoff = {
        "measurements": [
            {
                "id": "measurement:3000",
                "component_id": "component:test",
                "role": "length",
                "raw_value": "3000",
                "numeric_value": 3_000,
                "unit": "mm",
                "entity_ids": ["entity:dimension"],
                "status": "REVIEW",
            }
        ]
    }
    result = build_measurement_boards(
        panels,
        _closeups(image_path),
        tmp_path / "out",
        takeoff_payload=takeoff,
    )

    assert result["board_count"] == 1
    assert result["missing_count"] == 0
    board = result["records"][0]["boards"][0]
    assert Path(board["board_absolute_path"]).is_file()
    assert board["state"] == "REVIEW"
    assert [value["entity_id"] for value in board["candidates"]] == [
        "entity:qty",
        "entity:dimension",
    ]
    assert board["candidates"][0]["candidate_label"] == "D1"
    assert board["candidates"][0]["role_hint"] == "quantity"
    assert board["candidates"][1]["orientation"] == "horizontal"
    assert board["candidates"][1]["role_hint"] == "horizontal_length_or_width"
    assert board["candidates"][1]["measurement_candidates"] == [
        {
            "candidate_id": "measurement:3000",
            "role": "length",
            "raw_value": "3000",
            "numeric_value": 3_000,
            "unit": "mm",
            "status": "REVIEW",
        }
    ]


def test_measurement_board_fails_closed_when_closeup_is_missing(tmp_path: Path) -> None:
    result = build_measurement_boards(
        {"entities": []},
        _closeups(tmp_path / "missing.png"),
        tmp_path / "out",
    )

    assert result["board_count"] == 0
    assert result["missing_count"] == 1
    assert result["records"][0]["state"] == "MISSING"
    assert "CLOSEUP_OR_RENDER_BBOX_MISSING" in result["records"][0]["reason_codes"]


def test_measurement_boards_cli_parser() -> None:
    from cad_quote import build_parser

    args = build_parser().parse_args(
        [
            "measurement-boards",
            "panels.json",
            "component_closeups.json",
            "--out",
            "boards",
            "--maximum-per-image",
            "42",
        ]
    )

    assert args.maximum_per_image == 42
