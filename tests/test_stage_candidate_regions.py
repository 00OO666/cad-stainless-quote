from pathlib import Path

import pytest
from cadquote.stage_candidate_regions import build_stage_candidate_regions


def test_stage_candidate_regions_deduplicate_and_bound_ranked_panels(tmp_path: Path):
    image_a = tmp_path / "a.png"
    image_b = tmp_path / "b.png"
    image_a.write_bytes(b"a")
    image_b.write_bytes(b"b")
    panels = {
        "sheets": [
            {
                "id": "detail:a",
                "source_file_id": "file:a",
                "drawing_number": "D-01",
                "kind": "detail",
                "bbox": [0, 0, 10, 10],
            },
            {
                "id": "detail:b",
                "source_file_id": "file:b",
                "drawing_number": "D-02",
                "kind": "detail",
                "bbox": [20, 20, 40, 40],
            },
        ]
    }
    stages = {
        "records": [
            {
                "selection_key": "row:1",
                "sequence": 1,
                "name": "门套",
                "stages": {
                    "detail": {
                        "candidates": [
                            {
                                "candidate_id": "candidate:a:first",
                                "sheet_id": "detail:a",
                                "source": "relation_candidate",
                                "retrieval_rank_score": 0.9,
                            },
                            {
                                "candidate_id": "candidate:a:duplicate",
                                "sheet_id": "detail:a",
                                "source": "exact_material_code_candidate",
                                "retrieval_rank_score": 0.8,
                            },
                            {
                                "candidate_id": "candidate:b",
                                "sheet_id": "detail:b",
                                "source": "relation_candidate",
                                "retrieval_rank_score": 0.7,
                            },
                        ]
                    }
                },
            }
        ]
    }
    catalog = {
        "panels": {
            "detail:a": {"absolute_path": str(image_a), "image_sha256": "a" * 64},
            "detail:b": {"absolute_path": str(image_b), "image_sha256": "b" * 64},
        }
    }

    result = build_stage_candidate_regions(
        panels,
        stages,
        catalog,
        tmp_path / "regions.json",
        maximum_per_selection=1,
    )

    assert result["selection_count"] == 1
    assert result["evidence_count"] == 1
    assert result["truncated_selection_count"] == 1
    evidence = result["records"][0]["evidence"][0]
    assert evidence["stage_candidate_id"] == "candidate:a:first"
    assert evidence["sheet_id"] == "detail:a"
    assert evidence["render_bbox"] == [0.0, 0.0, 10.0, 10.0]
    assert evidence["state"] == "REVIEW"


def test_stage_candidate_regions_reject_invalid_limits(tmp_path: Path):
    with pytest.raises(ValueError, match="at least 1"):
        build_stage_candidate_regions(
            {},
            {},
            {"panels": {}},
            tmp_path / "x.json",
            maximum_per_selection=0,
        )
