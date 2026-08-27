from cadquote.candidate_benchmark import _page_code, build_candidate_benchmark
from cadquote.models import MtOccurrence


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
