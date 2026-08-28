import json
from pathlib import Path

from cadquote.component_frames import suggest_component_frames


def test_component_frames_are_review_only_and_augment_selection(tmp_path: Path):
    panels = {
        "sheets": [
            {
                "id": "panel:1",
                "kind": "elevation",
                "bbox": [0, 0, 1_000, 500],
            }
        ],
        "entities": [
            {
                "id": "line:1",
                "sheet_id": "panel:1",
                "entity_type": "LINE",
                "bbox": [180, 120, 420, 120],
            },
            {
                "id": "line:2",
                "sheet_id": "panel:1",
                "entity_type": "LINE",
                "bbox": [420, 120, 420, 360],
            },
            {
                "id": "dimension:1",
                "sheet_id": "panel:1",
                "entity_type": "DIMENSION",
                "bbox": [180, -20, 420, 100],
            },
            {
                "id": "text:ignored",
                "sheet_id": "panel:1",
                "entity_type": "TEXT",
                "bbox": [200, 200, 260, 230],
            },
            {
                "id": "floor:runaway",
                "sheet_id": "panel:1",
                "entity_type": "LINE",
                "bbox": [0, 360, 1_000, 360],
            },
        ],
    }
    boards = {
        "groups": [
            {
                "group_id": "group:1",
                "sheet_id": "panel:1",
                "candidates": [
                    {
                        "occurrence_id": "occurrence:1",
                        "leader_target": [400, 200],
                    }
                ],
            }
        ]
    }
    selections = [
        {
            "sequence": 1,
            "component_id": "component:1",
            "group_id": "group:1",
            "selected_occurrence_ids": ["occurrence:1"],
        }
    ]

    result = suggest_component_frames(
        panels,
        boards,
        selections,
        tmp_path / "frames",
    )

    assert result["suggested_count"] == 1
    frame = result["records"][0]["frames"][0]
    assert frame["state"] == "REVIEW"
    assert frame["object_bbox"] != [0, 0, 1_000, 500]
    assert "floor:runaway" not in frame["entity_ids"]
    assert frame["dimension_bboxes"] == [[180.0, 0.0, 420.0, 100.0]]
    augmented = json.loads(
        (tmp_path / "frames" / "selections_with_frame_candidates.json").read_text(encoding="utf-8")
    )
    assert augmented["rows"][0]["object_bbox_state"] == {"group:1": "REVIEW"}
    assert augmented["rows"][0]["stage"] == {"group:1": "elevation"}


def test_component_frames_keep_local_review_proposals_for_multiple_leaders(tmp_path: Path):
    panels = {
        "sheets": [{"id": "panel:1", "kind": "elevation", "bbox": [0, 0, 1_000, 500]}],
        "entities": [
            {
                "id": "left",
                "sheet_id": "panel:1",
                "entity_type": "LWPOLYLINE",
                "bbox": [80, 100, 260, 360],
            },
            {
                "id": "right",
                "sheet_id": "panel:1",
                "entity_type": "LWPOLYLINE",
                "bbox": [740, 100, 920, 360],
            },
        ],
    }
    boards = {
        "groups": [
            {
                "group_id": "group:1",
                "sheet_id": "panel:1",
                "candidates": [
                    {"occurrence_id": "occurrence:left", "leader_target": [160, 220]},
                    {"occurrence_id": "occurrence:right", "leader_target": [840, 220]},
                ],
            }
        ]
    }
    selections = [
        {
            "component_id": "component:1",
            "group_id": "group:1",
            "selected_occurrence_ids": ["occurrence:left", "occurrence:right"],
        }
    ]

    result = suggest_component_frames(panels, boards, selections, tmp_path / "frames")

    frames = result["records"][0]["frames"]
    assert [frame["frame_role"] for frame in frames] == [
        "selection_aggregate",
        "occurrence_local",
        "occurrence_local",
    ]
    assert frames[1]["selected_occurrence_ids"] == ["occurrence:left"]
    assert frames[2]["selected_occurrence_ids"] == ["occurrence:right"]
    augmented = json.loads(
        (tmp_path / "frames" / "selections_with_frame_candidates.json").read_text(
            encoding="utf-8"
        )
    )
    instances = augmented["rows"][0]["instance_object_bboxes"]["group:1"]
    assert len(instances) == 2
    assert all(value["object_bbox_state"] == "REVIEW" for value in instances)
