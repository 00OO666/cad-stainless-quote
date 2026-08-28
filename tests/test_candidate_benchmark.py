from cadquote.candidate_benchmark import (
    _is_physical_label,
    _page_code,
    build_candidate_benchmark,
)
from cadquote.models import MaterialMention, MtOccurrence


def test_candidate_benchmark_normalizes_gold_location_suffix() -> None:
    panel_payload = {
        "sheets": [
            {
                "id": "sheet:1",
                "source_file_id": "file:1",
                "drawing_number": "1F-EL-01",
                "title": "走廊立面",
                "kind": "elevation",
            }
        ],
        "entities": [
            {
                "id": "dimension:1",
                "source_file_id": "file:1",
                "sheet_id": "sheet:1",
                "entity_type": "DIMENSION",
                "space": "model",
                "geometry": {"display_measurement": 1_200.0},
            }
        ],
    }
    occurrence = MtOccurrence(
        id="occurrence:1",
        mt_code="MT-01",
        source_file_id="file:1",
        sheet_id="sheet:1",
        leader_target=(10.0, 20.0),
    )
    takeoff_payload = {
        "components": [
            {
                "id": "component:1",
                "mt_code": "MT-01",
                "elevation_occurrence_ids": ["occurrence:1"],
            }
        ],
        "measurements": [
            {
                "id": "measurement:1",
                "component_id": "component:1",
                "role": "length",
                "raw_value": "1200",
                "numeric_value": 1_200.0,
                "source_file_id": "file:1",
                "sheet_id": "sheet:1",
                "confidence": 0.9,
            }
        ],
        "items": [],
    }
    gold_payload = {
        "rows": [
            {
                "id": "gold:1",
                "row": 8,
                "source_material_code": "MT-01",
                "item": {
                    "sequence": 1,
                    "name": "线条",
                    "mt_code": "MT-01",
                    "plan_location": "1F-EL-01-走廊",
                    "width_mm": 1_200.0,
                    "length_mm": 1_200.0,
                    "quantity": 2.0,
                },
            }
        ]
    }

    result = build_candidate_benchmark(
        panel_payload,
        [occurrence],
        takeoff_payload,
        gold_payload,
    )

    row = result["comparison_rows"][0]
    assert row["page"] == "1F-EL-01"
    assert row["gold_location"] == "1F-EL-01-走廊"
    assert row["candidate_occurrence_count"] == 1
    assert row["width_probe"]["hit"] is True
    assert row["length_probe"]["hit"] is True
    assert row["quantity_probe"]["hit"] is False
    assert result["summary"]["page_code_candidate_coverage_count"] == 1


def test_auto_rows_do_not_copy_human_gold_values() -> None:
    panel_payload = {
        "sheets": [
            {
                "id": "sheet:1",
                "source_file_id": "file:1",
                "drawing_number": "EL-01",
                "kind": "elevation",
            }
        ],
        "entities": [],
    }
    occurrence = MtOccurrence(
        id="occurrence:1",
        mt_code="MT-01",
        source_file_id="file:1",
        sheet_id="sheet:1",
    )
    takeoff_payload = {
        "components": [
            {
                "id": "component:1",
                "mt_code": "MT-01",
                "elevation_occurrence_ids": ["occurrence:1"],
            }
        ],
        "measurements": [],
        "items": [],
    }
    gold_payload = {
        "rows": [
            {
                "id": "gold:1",
                "row": 7,
                "source_material_code": "MT-01",
                "item": {
                    "sequence": 1,
                    "name": "平板",
                    "mt_code": "MT-01",
                    "plan_location": "EL-01",
                    "width_mm": 888.0,
                    "length_mm": 9_999.0,
                    "quantity": 7.0,
                },
            }
        ]
    }

    result = build_candidate_benchmark(
        panel_payload,
        [occurrence],
        takeoff_payload,
        gold_payload,
    )

    auto = result["auto_rows"][0]
    assert auto["width_mm"] is None
    assert auto["length_mm"] is None
    assert auto["quantity"] is None
    assert auto["status"] == "REVIEW"


def test_page_code_normalizes_overpadded_numeric_suffix() -> None:
    assert _page_code("2F-EL-024-台盆区") == "2F-EL-24"
    assert _page_code("1F-EL-1-走廊") == "1F-EL-01"


def test_uncoded_metal_mention_is_reported_without_fabricating_page_code_match() -> None:
    panel_payload = {
        "sheets": [
            {
                "id": "sheet:1",
                "source_file_id": "file:1",
                "drawing_number": "E-02",
                "kind": "elevation",
            }
        ],
        "entities": [],
    }
    mention = MaterialMention(
        id="mention:1",
        raw_text="金属栏板",
        source_file_id="file:1",
        sheet_id="sheet:1",
        entity_ids=["entity:1"],
    )
    gold_payload = {
        "rows": [
            {
                "id": "gold:1",
                "row": 5,
                "source_material_code": "",
                "item": {
                    "sequence": 1,
                    "name": "金属栏板",
                    "mt_code": "",
                    "plan_location": "E-02-休息区",
                },
            }
        ]
    }

    result = build_candidate_benchmark(
        panel_payload,
        [],
        {"components": [], "measurements": [], "items": []},
        gold_payload,
        material_mentions=[mention],
    )

    row = result["comparison_rows"][0]
    assert row["candidate_occurrence_count"] == 0
    assert row["uncoded_material_mention_count"] == 1
    assert row["uncoded_material_mention_ids"] == ["mention:1"]
    assert row["readiness"] == "UNCODED_MATERIAL_CANDIDATE"
    assert result["summary"]["page_code_candidate_coverage_count"] == 0
    assert result["summary"]["uncoded_material_candidate_coverage_count"] == 1


def test_vector_quantity_probe_is_candidate_only_and_does_not_fill_auto_row() -> None:
    panel_payload = {
        "sheets": [
            {
                "id": "sheet:1",
                "source_file_id": "file:1",
                "drawing_number": "E-03",
                "kind": "elevation",
            }
        ],
        "entities": [],
    }
    occurrence = MtOccurrence(
        id="occurrence:1",
        mt_code="MT-01",
        source_file_id="file:1",
        sheet_id="sheet:1",
        leader_target=(100.0, 200.0),
    )
    takeoff_payload = {
        "components": [
            {
                "id": "component:1",
                "mt_code": "MT-01",
                "elevation_occurrence_ids": ["occurrence:1"],
            }
        ],
        "measurements": [],
        "items": [],
    }
    gold_payload = {
        "rows": [
            {
                "id": "gold:1",
                "row": 6,
                "source_material_code": "MT-01",
                "item": {
                    "sequence": 1,
                    "name": "隔板",
                    "mt_code": "MT-01",
                    "plan_location": "E-03-休息区",
                    "quantity": 2.0,
                },
            }
        ]
    }
    vector_payload = {
        "probes": [
            {
                "occurrence_id": "occurrence:1",
                "recommended_quantity": 2,
                "status": "REVIEW",
                "confidence": 0.48,
            }
        ]
    }

    result = build_candidate_benchmark(
        panel_payload,
        [occurrence],
        takeoff_payload,
        gold_payload,
        vector_probe_payload=vector_payload,
    )

    row = result["comparison_rows"][0]
    assert row["vector_quantity_probe_count"] == 1
    assert row["vector_quantity_values"] == [2.0]
    assert row["quantity_probe"]["hit"] is True
    assert result["summary"]["vector_quantity_candidate_coverage_count"] == 1
    assert result["summary"]["quantity_candidate_hit_count"] == 1
    assert result["auto_rows"][0]["quantity"] is None


def test_vector_probe_structured_quantity_candidates_are_all_retrievable() -> None:
    panel_payload = {
        "sheets": [
            {
                "id": "sheet:1",
                "source_file_id": "file:1",
                "drawing_number": "E-03",
                "kind": "elevation",
            }
        ],
        "entities": [],
    }
    occurrence = MtOccurrence(
        id="occurrence:1",
        mt_code="MT-01",
        source_file_id="file:1",
        sheet_id="sheet:1",
        leader_target=(100.0, 200.0),
    )
    gold_payload = {
        "rows": [
            {
                "id": "gold:1",
                "row": 6,
                "source_material_code": "MT-01",
                "item": {
                    "sequence": 1,
                    "name": "隔板",
                    "mt_code": "MT-01",
                    "plan_location": "E-03",
                    "quantity": 3.0,
                },
            }
        ]
    }

    result = build_candidate_benchmark(
        panel_payload,
        [occurrence],
        {"components": [], "measurements": [], "items": []},
        gold_payload,
        vector_probe_payload={
            "probes": [
                {
                    "occurrence_id": "occurrence:1",
                    "recommended_quantity": None,
                    "quantity_candidates": [
                        {"value": 2, "method": "repeated_insert"},
                        {"value": 3, "method": "repeated_connected_graph"},
                    ],
                }
            ]
        },
    )

    row = result["comparison_rows"][0]
    assert row["vector_quantity_probe_count"] == 1
    assert row["vector_quantity_values"] == [2.0, 3.0]
    assert row["quantity_probe"]["hit"] is True


def test_quantity_candidate_uses_five_percent_not_one_whole_count_tolerance() -> None:
    panel_payload = {
        "sheets": [
            {
                "id": "sheet:1",
                "source_file_id": "file:1",
                "drawing_number": "E-04",
                "kind": "elevation",
            }
        ],
        "entities": [],
    }
    occurrence = MtOccurrence(
        id="occurrence:1",
        mt_code="MT-01",
        source_file_id="file:1",
        sheet_id="sheet:1",
        leader_target=(0.0, 0.0),
    )
    result = build_candidate_benchmark(
        panel_payload,
        [occurrence],
        {"components": [], "measurements": [], "items": []},
        {
            "rows": [
                {
                    "id": "gold:1",
                    "row": 7,
                    "source_material_code": "MT-01",
                    "item": {
                        "sequence": 1,
                        "name": "面板",
                        "mt_code": "MT-01",
                        "plan_location": "E-04",
                        "quantity": 1.0,
                    },
                }
            ]
        },
        vector_probe_payload={
            "probes": [
                {
                    "occurrence_id": "occurrence:1",
                    "recommended_quantity": 2,
                }
            ]
        },
    )

    probe = result["comparison_rows"][0]["quantity_probe"]
    assert probe["closest"] == 2.0
    assert probe["relative_error"] == 1.0
    assert probe["hit"] is False


def test_detail_material_tag_is_not_counted_as_a_second_line_item_occurrence() -> None:
    panel_payload = {
        "sheets": [
            {
                "id": "sheet:elevation",
                "source_file_id": "file:1",
                "drawing_number": "E-05",
                "kind": "elevation",
            },
            {
                "id": "sheet:detail",
                "source_file_id": "file:1",
                "drawing_number": "E-05",
                "kind": "detail",
            },
        ],
        "entities": [],
    }
    elevation = MtOccurrence(
        id="occurrence:elevation",
        mt_code="MT-01",
        source_file_id="file:1",
        sheet_id="sheet:elevation",
        leader_target=(10.0, 20.0),
    )
    detail = elevation.model_copy(
        update={"id": "occurrence:detail", "sheet_id": "sheet:detail"}
    )
    result = build_candidate_benchmark(
        panel_payload,
        [elevation, detail],
        {
            "components": [
                {
                    "id": "component:1",
                    "mt_code": "MT-01",
                    "elevation_occurrence_ids": ["occurrence:elevation"],
                }
            ],
            "measurements": [],
            "items": [],
        },
        {
            "rows": [
                {
                    "id": "gold:1",
                    "row": 5,
                    "source_material_code": "MT-01",
                    "item": {
                        "sequence": 1,
                        "name": "门套",
                        "mt_code": "MT-01",
                        "plan_location": "E-05",
                    },
                }
            ]
        },
    )

    row = result["comparison_rows"][0]
    assert row["candidate_occurrence_ids"] == ["occurrence:elevation"]
    assert row["candidate_occurrence_count"] == 1


def test_categorical_assignment_is_one_to_one_and_never_uses_row_order() -> None:
    panel_payload = {
        "sheets": [
            {
                "id": "sheet:1",
                "source_file_id": "file:1",
                "drawing_number": "E-06",
                "title": "立面图",
                "kind": "elevation",
            }
        ],
        "entities": [],
    }
    occurrences = [
        MtOccurrence(
            id=f"occurrence:{index}",
            mt_code="MT-01",
            source_file_id="file:1",
            sheet_id="sheet:1",
            leader_target=(float(index), 0.0),
        )
        for index in (1, 2)
    ]
    takeoff_payload = {
        "components": [
            {
                "id": f"component:{index}",
                "mt_code": "MT-01",
                "elevation_occurrence_ids": [f"occurrence:{index}"],
            }
            for index in (1, 2)
        ],
        "measurements": [],
        "items": [],
    }
    gold_payload = {
        "rows": [
            {
                "id": f"gold:{index}",
                "row": index + 4,
                "source_material_code": "MT-01",
                "item": {
                    "sequence": index,
                    "name": name,
                    "mt_code": "MT-01",
                    "plan_location": "E-06",
                },
            }
            for index, name in ((1, "门套"), (2, "踢脚线"))
        ]
    }

    result = build_candidate_benchmark(
        panel_payload,
        occurrences,
        takeoff_payload,
        gold_payload,
    )

    assert [
        row["component_assignment"]["status"] for row in result["comparison_rows"]
    ] == ["AMBIGUOUS", "AMBIGUOUS"]
    summary = result["summary"]["categorical_one_to_one_assignment"]
    assert summary["matched_count"] == 0
    assert summary["duplicate_component_assignment_count"] == 0
    assert summary["auto_components_in_gold_buckets"] == 2
    assert summary["auto_components_outside_gold_buckets"] == 0
    assert summary["overcomplete_bucket_component_count"] == 0


def test_exact_physical_names_enable_unique_categorical_assignment() -> None:
    panel_payload = {
        "sheets": [
            {
                "id": "sheet:1",
                "source_file_id": "file:1",
                "drawing_number": "E-07",
                "title": "立面图",
                "kind": "elevation",
            }
        ],
        "entities": [],
    }
    occurrences = [
        MtOccurrence(
            id=f"occurrence:{index}",
            mt_code="MT-01",
            source_file_id="file:1",
            sheet_id="sheet:1",
            leader_target=(float(index), 0.0),
        )
        for index in (1, 2)
    ]
    takeoff_payload = {
        "components": [
            {
                "id": "component:door",
                "mt_code": "MT-01",
                "name": "门套",
                "elevation_occurrence_ids": ["occurrence:1"],
            },
            {
                "id": "component:skirting",
                "mt_code": "MT-01",
                "name": "踢脚线",
                "elevation_occurrence_ids": ["occurrence:2"],
            },
        ],
        "measurements": [],
        "items": [],
    }
    gold_payload = {
        "rows": [
            {
                "id": f"gold:{index}",
                "row": index + 4,
                "source_material_code": "MT-01",
                "item": {
                    "sequence": index,
                    "name": name,
                    "mt_code": "MT-01",
                    "plan_location": "E-07",
                },
            }
            for index, name in ((1, "踢脚线"), (2, "门套"))
        ]
    }

    result = build_candidate_benchmark(
        panel_payload,
        occurrences,
        takeoff_payload,
        gold_payload,
    )

    matched = [
        row["component_assignment"]["matched_component_id"]
        for row in result["comparison_rows"]
    ]
    assert matched == ["component:skirting", "component:door"]
    summary = result["summary"]["categorical_one_to_one_assignment"]
    assert summary["matched_count"] == 2
    assert summary["duplicate_component_assignment_count"] == 0


def test_material_description_is_not_reported_as_a_physical_component_name() -> None:
    occurrence = MtOccurrence(
        id="occurrence:1",
        mt_code="MT-01",
        source_file_id="file:1",
        sheet_id="sheet:1",
    )
    result = build_candidate_benchmark(
        {
            "sheets": [
                {
                    "id": "sheet:1",
                    "source_file_id": "file:1",
                    "drawing_number": "E-08",
                    "title": "立面图",
                    "kind": "elevation",
                }
            ],
            "entities": [],
        },
        [occurrence],
        {
            "components": [
                {
                    "id": "component:1",
                    "mt_code": "MT-01",
                    "name": "灰色不锈钢",
                    "elevation_occurrence_ids": ["occurrence:1"],
                }
            ],
            "measurements": [],
            "items": [],
        },
        {"rows": []},
    )

    assert result["auto_rows"][0]["name"] is None
    assert result["auto_rows"][0]["name_candidates"] == []
    assert _is_physical_label("全部版权归设计单位所有") is False
    assert _is_physical_label("某市某路A座2505室") is False
    assert _is_physical_label("健身室门套") is True
