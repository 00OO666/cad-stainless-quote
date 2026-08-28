from __future__ import annotations

import copy

from cadquote.detail_subviews import propose_detail_subviews


def _panel() -> dict[str, object]:
    return {
        "id": "panel:detail",
        "drawing_number": "D-01",
        "title": "虚构收口条节点大样图",
        "bbox": [0.0, 0.0, 1000.0, 600.0],
    }


def _entities() -> list[dict[str, object]]:
    return [
        {
            "id": "text:a-code",
            "sheet_id": "panel:detail",
            "handle": "A1",
            "entity_type": "ATTRIB",
            "text": "MT-91",
            "bbox": [80.0, 80.0, 180.0, 110.0],
            "geometry": {"parent_insert_handle": "ANN-A"},
        },
        {
            "id": "text:a-material",
            "sheet_id": "panel:detail",
            "handle": "A2",
            "entity_type": "ATTRIB",
            "text": "虚构蓝色不锈钢",
            "bbox": [80.0, 112.0, 190.0, 140.0],
            "geometry": {"parent_insert_handle": "ANN-A"},
        },
        {
            "id": "leader:a",
            "sheet_id": "panel:detail",
            "handle": "LA",
            "entity_type": "LEADER",
            "bbox": [150.0, 120.0, 280.0, 330.0],
            "geometry": {
                "vertices": [[270.0, 315.0, 0.0], [270.0, 125.0, 0.0], [175.0, 125.0, 0.0]]
            },
        },
        {
            "id": "dimension:a",
            "sheet_id": "panel:detail",
            "handle": "DA",
            "entity_type": "DIMENSION",
            "bbox": [205.0, 255.0, 330.0, 355.0],
            "value": 37.0,
            "geometry": {"raw_measurement": 37.0, "display_measurement": 37.0},
        },
        {
            "id": "text:b-code",
            "sheet_id": "panel:detail",
            "handle": "B1",
            "entity_type": "ATTRIB",
            "text": "MT-92",
            "bbox": [790.0, 80.0, 900.0, 110.0],
            "geometry": {"parent_insert_handle": "ANN-B"},
        },
        {
            "id": "leader:b",
            "sheet_id": "panel:detail",
            "handle": "LB",
            "entity_type": "LEADER",
            "bbox": [720.0, 120.0, 850.0, 330.0],
            "geometry": {
                "vertices": [[735.0, 315.0, 0.0], [735.0, 125.0, 0.0], [820.0, 125.0, 0.0]]
            },
        },
        {
            "id": "dimension:b",
            "sheet_id": "panel:detail",
            "handle": "DB",
            "entity_type": "DIMENSION",
            "bbox": [690.0, 255.0, 790.0, 355.0],
            "value": 83.0,
            "geometry": {"raw_measurement": 83.0, "display_measurement": 83.0},
        },
    ]


def test_exact_material_leader_ranks_the_bounded_subview_first() -> None:
    candidates = propose_detail_subviews(
        _panel(),
        _entities(),
        query={
            "component_name": "虚构收口条",
            "material": "虚构蓝色不锈钢",
            "material_codes": ["MT-91"],
        },
    )

    assert candidates
    first = candidates[0]
    assert first["rank"] == 1
    assert first["material_codes"] == ["MT-91"]
    assert first["state"] in {"MATCH", "REVIEW"}
    assert first["bbox"][2] < 600.0
    assert "leader:a" in first["leader_entity_ids"]
    assert "LA" in first["leader_handles"]
    assert {value["handle"] for value in first["dimensions"]} == {"DA"}


def test_dimension_values_are_output_only_and_do_not_change_ranking() -> None:
    original = _entities()
    changed = copy.deepcopy(original)
    for entity in changed:
        if entity["entity_type"] != "DIMENSION":
            continue
        entity["value"] = 999999.0
        entity["geometry"] = {
            "raw_measurement": 999999.0,
            "display_measurement": 999999.0,
        }

    query = {"component_name": "虚构收口条", "material_codes": ["MT-91"]}
    before = propose_detail_subviews(_panel(), original, query=query)
    after = propose_detail_subviews(_panel(), changed, query=query)

    assert [value["candidate_id"] for value in before] == [
        value["candidate_id"] for value in after
    ]
    assert [value["score"] for value in before] == [value["score"] for value in after]
    assert before[0]["dimensions"][0]["value"] == 37.0
    assert after[0]["dimensions"][0]["value"] == 999999.0


def test_same_code_leaders_keep_individual_and_union_candidates() -> None:
    entities = _entities()
    entities.extend(
        [
            {
                "id": "text:a2-code",
                "sheet_id": "panel:detail",
                "handle": "A3",
                "entity_type": "ATTRIB",
                "text": "MT-91",
                "bbox": [390.0, 80.0, 490.0, 110.0],
                "geometry": {"parent_insert_handle": "ANN-A2"},
            },
            {
                "id": "leader:a2",
                "sheet_id": "panel:detail",
                "handle": "LA2",
                "entity_type": "LEADER",
                "bbox": [420.0, 120.0, 510.0, 340.0],
                "geometry": {
                    "vertices": [
                        [500.0, 320.0, 0.0],
                        [500.0, 125.0, 0.0],
                        [455.0, 125.0, 0.0],
                    ]
                },
            },
        ]
    )

    candidates = propose_detail_subviews(
        _panel(), entities, query={"material_codes": ["MT-91"]}, top_k=10
    )

    same_code = [
        value for value in candidates if value["seed_kind"] == "same_material_leader_cluster"
    ]
    individual = [value for value in candidates if value["seed_kind"] == "material_leader"]
    assert same_code
    assert len(same_code[0]["leader_entity_ids"]) == 2
    assert len(individual) >= 2


def test_closed_frame_is_emitted_as_bounded_review_candidate() -> None:
    entities = [
        {
            "id": "frame:1",
            "sheet_id": "panel:detail",
            "handle": "F1",
            "entity_type": "LWPOLYLINE",
            "bbox": [100.0, 100.0, 450.0, 400.0],
            "geometry": {"closed": True},
        }
    ]

    candidates = propose_detail_subviews(_panel(), entities)

    assert len(candidates) == 1
    assert candidates[0]["seed_kind"] == "closed_frame"
    assert candidates[0]["bbox"] == [100.0, 100.0, 450.0, 400.0]
    assert candidates[0]["state"] == "UNRESOLVED"
