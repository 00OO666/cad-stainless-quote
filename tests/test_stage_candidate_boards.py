from pathlib import Path

from cadquote.stage_candidate_boards import render_stage_candidate_boards
from PIL import Image


def test_stage_candidate_boards_render_review_only_contact_sheet(tmp_path: Path):
    first = tmp_path / "first.png"
    second = tmp_path / "second.png"
    Image.new("RGB", (240, 120), "navy").save(first)
    Image.new("RGB", (120, 240), "maroon").save(second)
    payload = {
        "stage": "detail",
        "records": [
            {
                "selection_key": "component:1",
                "sequence": 1,
                "name": "frame",
                "stage": "detail",
                "evidence": [
                    {
                        "stage_candidate_rank": 1,
                        "stage_candidate_id": "candidate:1",
                        "sheet_id": "sheet:1",
                        "drawing_number": "D-01",
                        "candidate_source": "relation_candidate",
                        "retrieval_rank_score": 0.8,
                        "absolute_path": str(first),
                    },
                    {
                        "stage_candidate_rank": 2,
                        "stage_candidate_id": "candidate:2",
                        "sheet_id": "sheet:2",
                        "drawing_number": "D-02",
                        "candidate_source": "exact_material_code_candidate",
                        "retrieval_rank_score": 0.3,
                        "absolute_path": str(second),
                    },
                ],
            }
        ],
    }

    result = render_stage_candidate_boards(
        payload,
        tmp_path / "out",
        columns=2,
        tile_width=200,
        tile_height=160,
    )

    assert result["board_count"] == 1
    assert result["candidate_count"] == 2
    row = result["records"][0]
    assert row["state"] == "REVIEW"
    assert [value["board_label"] for value in row["candidates"]] == ["C01", "C02"]
    assert Path(row["board_absolute_path"]).is_file()
    assert row["board_pixel_size"] == [400, 210]
    assert "EXPLICIT_CANDIDATE_SELECTION" in row["reason_codes"][0]


def test_stage_candidate_boards_report_missing_images(tmp_path: Path):
    result = render_stage_candidate_boards(
        {
            "stage": "detail",
            "records": [
                {
                    "selection_key": "component:missing",
                    "sequence": 2,
                    "evidence": [
                        {
                            "stage_candidate_id": "candidate:missing",
                            "sheet_id": "sheet:missing",
                            "absolute_path": str(tmp_path / "missing.png"),
                        }
                    ],
                }
            ],
        },
        tmp_path / "out",
    )

    assert result["board_count"] == 0
    assert result["missing_image_count"] == 1
    assert result["records"][0]["state"] == "MISSING"
