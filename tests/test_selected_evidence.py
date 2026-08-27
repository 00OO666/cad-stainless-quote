import json
from argparse import Namespace
from pathlib import Path

from cad_quote import command_selected_evidence
from cadquote.io import sha256_file
from cadquote.selected_evidence import render_selected_occurrence_evidence
from PIL import Image, ImageDraw


def _panel(path: Path) -> None:
    image = Image.new("RGB", (1_000, 500), "#111820")
    draw = ImageDraw.Draw(image)
    draw.rectangle((80, 80, 920, 420), outline="#22d3ee", width=5)
    draw.line((100, 360, 900, 360), fill="#32d583", width=4)
    image.save(path)


def test_selected_evidence_renders_only_explicit_occurrence(tmp_path: Path):
    panel = tmp_path / "panel.png"
    _panel(panel)
    candidate_manifest = {
        "groups": [
            {
                "group_id": "group:1",
                "sheet_id": "sheet:1",
                "mt_code": "MT-01",
                "drawing_number": "E-01",
                "panel_bbox": [0, 0, 1_000, 500],
                "candidates": [
                    {
                        "label": "C1",
                        "occurrence_id": "occurrence:1",
                        "leader_target": [200, 250],
                    },
                    {
                        "label": "C2",
                        "occurrence_id": "occurrence:2",
                        "leader_target": [800, 140],
                    },
                ],
            }
        ]
    }
    panel_index = {
        "panels": {
            "sheet:1": {
                "absolute_path": str(panel),
                "bbox": [0, 0, 1_000, 500],
            }
        }
    }
    selections = [
        {
            "sequence": 1,
            "gold_row_id": "row:1",
            "name": "脚线",
            "group_id": ["group:1"],
            "selected_occurrence_ids": ["occurrence:2"],
            "decision": "MATCH",
        }
    ]

    result = render_selected_occurrence_evidence(
        candidate_manifest,
        panel_index,
        selections,
        tmp_path / "selected",
    )

    assert result["rendered_selection_count"] == 1
    assert result["candidate_count"] == 0
    assert result["review_count"] == 1
    record = result["records"][0]
    assert record["state"] == "REVIEW"
    assert record["reason_codes"] == ["OBJECT_BBOX_MISSING"]
    evidence = record["evidence"][0]
    assert evidence["selected_occurrence_ids"] == ["occurrence:2"]
    assert evidence["selected_labels"] == ["C2"]
    assert evidence["framing_basis"] == "LEADER_POINT_FALLBACK"
    assert (tmp_path / "selected" / evidence["locator_image"]).is_file()
    assert (tmp_path / "selected" / evidence["closeup_image"]).is_file()
    assert evidence["crop_box_px"] != [0, 0, 1_000, 500]


def test_selected_evidence_blocks_cross_component_occurrence_reuse(tmp_path: Path):
    selections = [
        {
            "gold_row_id": "row:1",
            "name": "脚线",
            "selected_occurrence_ids": ["occurrence:shared"],
        },
        {
            "gold_row_id": "row:2",
            "name": "门套",
            "selected_occurrence_ids": ["occurrence:shared"],
        },
    ]

    result = render_selected_occurrence_evidence(
        {"groups": []},
        {"panels": {}},
        selections,
        tmp_path / "selected",
    )

    assert result["block_count"] == 2
    assert result["rendered_selection_count"] == 0
    assert result["conflicting_occurrences"] == {
        "occurrence:shared": [
            {
                "selection_key": "row:1",
                "component_name": "脚线",
                "room_or_location": "",
            },
            {
                "selection_key": "row:2",
                "component_name": "门套",
                "room_or_location": "",
            },
        ]
    }
    assert all(
        record["reason_codes"] == ["OCCURRENCE_ASSIGNED_TO_MULTIPLE_COMPONENTS"]
        for record in result["records"]
    )


def test_selected_evidence_blocks_same_component_id_with_different_names(tmp_path: Path):
    selections = [
        {
            "component_id": "component:1",
            "name": "脚线",
            "selected_occurrence_ids": ["occurrence:shared"],
        },
        {
            "component_id": "component:1",
            "name": "门套",
            "selected_occurrence_ids": ["occurrence:shared"],
        },
    ]

    result = render_selected_occurrence_evidence(
        {"groups": []},
        {"panels": {}},
        selections,
        tmp_path / "selected",
    )

    assert result["block_count"] == 2
    assert len(result["conflicting_occurrences"]["occurrence:shared"]) == 2


def test_selected_evidence_blocks_same_component_id_in_different_locations(tmp_path: Path):
    selections = [
        {
            "component_id": "component:1",
            "name": "脚线",
            "room": "room-a",
            "selected_occurrence_ids": ["occurrence:shared"],
        },
        {
            "component_id": "component:1",
            "name": "脚线",
            "room": "room-b",
            "selected_occurrence_ids": ["occurrence:shared"],
        },
    ]

    result = render_selected_occurrence_evidence(
        {"groups": []},
        {"panels": {}},
        selections,
        tmp_path / "selected",
    )

    assert result["block_count"] == 2


def test_selected_evidence_paths_are_unique_after_label_normalization(tmp_path: Path):
    panel = tmp_path / "panel.png"
    _panel(panel)
    candidate_manifest = {
        "groups": [
            {
                "group_id": "group:1",
                "sheet_id": "sheet:1",
                "mt_code": "MT-01",
                "drawing_number": "E-01",
                "panel_bbox": [0, 0, 1_000, 500],
                "candidates": [
                    {"label": "C1", "occurrence_id": "occurrence:1", "leader_target": [200, 250]},
                    {"label": "C2", "occurrence_id": "occurrence:2", "leader_target": [800, 250]},
                ],
            }
        ]
    }
    panel_index = {"panels": {"sheet:1": {"absolute_path": str(panel)}}}
    selections = [
        {
            "sequence": 1,
            "row_id": "row:1",
            "name": "left",
            "group_id": "group:1",
            "selected_occurrence_ids": ["occurrence:1"],
        },
        {
            "sequence": 1,
            "row_id": "row/1",
            "name": "right",
            "group_id": "group:1",
            "selected_occurrence_ids": ["occurrence:2"],
        },
    ]

    result = render_selected_occurrence_evidence(
        candidate_manifest,
        panel_index,
        selections,
        tmp_path / "selected",
    )

    locator_paths = [record["evidence"][0]["locator_image"] for record in result["records"]]
    assert len(locator_paths) == len(set(locator_paths)) == 2
    for record in result["records"]:
        evidence = record["evidence"][0]
        assert (
            sha256_file(tmp_path / "selected" / evidence["locator_image"])
            == evidence["locator_sha256"]
        )


def test_selected_evidence_rejects_target_outside_panel_bbox(tmp_path: Path):
    panel = tmp_path / "panel.png"
    _panel(panel)
    result = render_selected_occurrence_evidence(
        {
            "groups": [
                {
                    "group_id": "group:1",
                    "sheet_id": "sheet:1",
                    "panel_bbox": [0, 0, 1_000, 500],
                    "candidates": [
                        {
                            "occurrence_id": "occurrence:outside",
                            "leader_target": [10_000, 10_000],
                        }
                    ],
                }
            ]
        },
        {"panels": {"sheet:1": {"absolute_path": str(panel)}}},
        [
            {
                "row_id": "row:outside",
                "group_id": "group:1",
                "selected_occurrence_ids": ["occurrence:outside"],
            }
        ],
        tmp_path / "selected",
    )

    assert result["candidate_count"] == 0
    assert result["missing_count"] == 1
    assert "SELECTED_OCCURRENCE_OUTSIDE_PANEL_BBOX" in result["records"][0]["reason_codes"]


def test_selected_evidence_uses_reviewed_object_bbox_for_crop(tmp_path: Path):
    panel = tmp_path / "panel.png"
    _panel(panel)
    candidate_manifest = {
        "groups": [
            {
                "group_id": "group:1",
                "sheet_id": "sheet:1",
                "panel_bbox": [0, 0, 1_000, 500],
                "candidates": [
                    {
                        "label": "C1",
                        "occurrence_id": "occurrence:1",
                        "leader_target": [200, 250],
                    }
                ],
            }
        ]
    }
    panel_index = {"panels": {"sheet:1": {"absolute_path": str(panel)}}}
    base_selection = {
        "row_id": "row:1",
        "group_id": "group:1",
        "selected_occurrence_ids": ["occurrence:1"],
    }
    small = render_selected_occurrence_evidence(
        candidate_manifest,
        panel_index,
        [
            {
                **base_selection,
                "object_bbox": [100, 100, 300, 300],
                "object_bbox_state": "CONFIRMED",
            }
        ],
        tmp_path / "small",
    )
    large = render_selected_occurrence_evidence(
        candidate_manifest,
        panel_index,
        [
            {
                **base_selection,
                "object_bbox": [100, 50, 900, 450],
                "object_bbox_state": "CONFIRMED",
            }
        ],
        tmp_path / "large",
    )

    small_evidence = small["records"][0]["evidence"][0]
    large_evidence = large["records"][0]["evidence"][0]
    assert small_evidence["framing_basis"] == "OBJECT_BBOX_PLUS_LEADER"
    assert large_evidence["framing_basis"] == "OBJECT_BBOX_PLUS_LEADER"
    assert small_evidence["crop_box_px"] != large_evidence["crop_box_px"]
    assert small["records"][0]["state"] == "CANDIDATE"


def test_selected_evidence_keeps_dimension_bboxes_and_stage(tmp_path: Path):
    panel = tmp_path / "panel.png"
    _panel(panel)
    result = render_selected_occurrence_evidence(
        {
            "groups": [
                {
                    "group_id": "group:1",
                    "sheet_id": "sheet:1",
                    "panel_bbox": [0, 0, 1_000, 500],
                    "candidates": [
                        {
                            "label": "C1",
                            "occurrence_id": "occurrence:1",
                            "leader_target": [200, 250],
                        }
                    ],
                }
            ]
        },
        {"panels": {"sheet:1": {"absolute_path": str(panel)}}},
        [
            {
                "row_id": "row:1",
                "group_id": "group:1",
                "selected_occurrence_ids": ["occurrence:1"],
                "object_bbox": [100, 100, 300, 300],
                "object_bbox_state": "CONFIRMED",
                "dimension_bboxes": [[310, 90, 600, 140]],
                "stage": "elevation",
            }
        ],
        tmp_path / "selected",
    )

    evidence = result["records"][0]["evidence"][0]
    assert evidence["stage"] == "elevation"
    assert evidence["dimension_bboxes"] == [[310.0, 90.0, 600.0, 140.0]]


def test_selected_evidence_blocks_declared_label_mismatch(tmp_path: Path):
    panel = tmp_path / "panel.png"
    _panel(panel)
    result = render_selected_occurrence_evidence(
        {
            "groups": [
                {
                    "group_id": "group:1",
                    "sheet_id": "sheet:1",
                    "mt_code": "MT-01",
                    "drawing_number": "E-01",
                    "panel_bbox": [0, 0, 1_000, 500],
                    "candidates": [
                        {
                            "label": "C1",
                            "occurrence_id": "occurrence:1",
                            "leader_target": [200, 250],
                        }
                    ],
                }
            ]
        },
        {"panels": {"sheet:1": {"absolute_path": str(panel)}}},
        [
            {
                "row_id": "row:1",
                "group_id": "group:1",
                "selected_occurrence_ids": ["occurrence:1"],
                "selected_labels": ["C2"],
            }
        ],
        tmp_path / "selected",
    )

    assert result["block_count"] == 1
    assert result["records"][0]["reason_codes"] == ["SELECTED_LABELS_MISMATCH"]


def test_selected_evidence_blocks_mt_or_drawing_mismatch(tmp_path: Path):
    panel = tmp_path / "panel.png"
    _panel(panel)
    candidate_manifest = {
        "groups": [
            {
                "group_id": "group:1",
                "sheet_id": "sheet:1",
                "mt_code": "MT-01",
                "drawing_number": "E-01",
                "panel_bbox": [0, 0, 1_000, 500],
                "candidates": [
                    {
                        "label": "C1",
                        "occurrence_id": "occurrence:1",
                        "leader_target": [200, 250],
                    }
                ],
            }
        ]
    }
    panel_index = {"panels": {"sheet:1": {"absolute_path": str(panel)}}}
    base_selection = {
        "row_id": "row:1",
        "group_id": "group:1",
        "selected_occurrence_ids": ["occurrence:1"],
    }
    wrong_mt = render_selected_occurrence_evidence(
        candidate_manifest,
        panel_index,
        [{**base_selection, "mt_code": "MT-99"}],
        tmp_path / "wrong-mt",
    )
    wrong_page = render_selected_occurrence_evidence(
        candidate_manifest,
        panel_index,
        [{**base_selection, "page": "E-99"}],
        tmp_path / "wrong-page",
    )

    assert wrong_mt["records"][0]["reason_codes"] == ["MT_CODE_MISMATCH"]
    assert wrong_page["records"][0]["reason_codes"] == ["DRAWING_NUMBER_MISMATCH"]


def test_selected_evidence_cli_returns_failure_when_evidence_is_missing(tmp_path: Path):
    candidate_boards = tmp_path / "candidate_boards.json"
    panel_index = tmp_path / "panel_index.json"
    selections = tmp_path / "selections.json"
    candidate_boards.write_text(json.dumps({"groups": []}), encoding="utf-8")
    panel_index.write_text(json.dumps({"panels": {}}), encoding="utf-8")
    selections.write_text(
        json.dumps(
            [
                {
                    "row_id": "row:missing",
                    "group_id": "group:missing",
                    "selected_occurrence_ids": ["occurrence:missing"],
                }
            ]
        ),
        encoding="utf-8",
    )

    assert (
        command_selected_evidence(
            Namespace(
                candidate_boards=candidate_boards,
                panel_index=panel_index,
                selections=selections,
                out=tmp_path / "selected",
            )
        )
        == 2
    )
