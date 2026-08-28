from __future__ import annotations

import json
from typing import Any

import pytest
from cad_quote import main
from cadquote.variant_bindings import (
    build_variant_binding_row,
    build_variant_bindings,
    parse_panel_title,
    parse_row_variant,
)


def _panel(panel_id: str, title: str, kind: str) -> dict[str, Any]:
    return {
        "id": panel_id,
        "title": title,
        "kind": kind,
        "drawing_number": panel_id,
        "viewport_handle": f"viewport-{panel_id}",
        "source_file_id": "file:synthetic",
    }


def _dimension(panel_id: str, handle: str, value: float, axis: str) -> dict[str, Any]:
    point3 = [value, 0.0, 0.0] if axis == "horizontal" else [0.0, value, 0.0]
    return {
        "id": f"entity:{handle}",
        "sheet_id": panel_id,
        "handle": handle,
        "entity_type": "DIMENSION",
        "space": f"model@layout#{panel_id}",
        "value": value,
        "geometry": {
            "defpoint2": [0.0, 0.0, 0.0],
            "defpoint3": point3,
            "display_measurement": value,
            "geometric_measurement": value,
            "original_entity_id": f"original:{handle}",
            "panel_viewport_handle": f"viewport-{panel_id}",
            "units": "millimeters",
        },
    }


def _material(panel_id: str, code: str) -> dict[str, Any]:
    return {
        "id": f"material:{panel_id}",
        "sheet_id": panel_id,
        "entity_type": "ATTRIB",
        "text": code,
    }


def _fixture(variant: str, width: float) -> tuple[dict[str, Any], dict[str, Any]]:
    plan_id = f"plan-{variant}"
    front_id = f"front-{variant}"
    back_id = f"back-{variant}"
    panels = {
        plan_id: _panel(plan_id, f"接待台{variant}平面图 SCALE:1/10", "plan"),
        front_id: _panel(front_id, f"接待台{variant}正立面图 SCALE:1/10", "elevation"),
        back_id: _panel(back_id, f"接待台{variant}背立面图 SCALE:1/10", "elevation"),
        "opposite": _panel("opposite", "接待台Z平面图 SCALE:1/10", "plan"),
    }
    entities = {
        plan_id: [
            _material(plan_id, "MT-07"),
            _dimension(plan_id, "pw-1", width, "horizontal"),
            _dimension(plan_id, "pw-2", width + 0.2, "horizontal"),
            _dimension(plan_id, "pd-1", 900.0, "vertical"),
            _dimension(plan_id, "pd-2", 900.1, "vertical"),
        ],
        front_id: [
            _material(front_id, "MT-07"),
            _dimension(front_id, "fw", width, "horizontal"),
            _dimension(front_id, "height-1", 1050.0, "vertical"),
        ],
        back_id: [
            _material(back_id, "MT-07"),
            _dimension(back_id, "bw", width, "horizontal"),
            _dimension(back_id, "height-2", 1050.1, "vertical"),
        ],
        "opposite": [
            _material("opposite", "MT-07"),
            _dimension("opposite", "wrong-1", 9999.0, "horizontal"),
            _dimension("opposite", "wrong-2", 9999.0, "horizontal"),
        ],
    }
    task = {
        "sequence": 1,
        "name": f"前厅接待台-{variant}",
        "material_code": "MT-07",
        "candidate_occurrences": [
            {"sheet_id": plan_id},
            {"sheet_id": front_id},
            {"sheet_id": back_id},
            {"sheet_id": "opposite"},
        ],
    }
    flat_entities = [entity for group in entities.values() for entity in group]
    return task, {"sheets": list(panels.values()), "entities": flat_entities}


def test_variant_parsers() -> None:
    assert parse_row_variant("前厅接待台-A") == ("前厅接待台", "A")
    assert parse_panel_title("接待台A背立面图 SCALE:1/10") == (
        "接待台",
        "A",
        "背立面图",
    )
    assert parse_row_variant("普通墙面") is None


def test_variants_do_not_share_the_first_candidate() -> None:
    task_a, catalog_a = _fixture("A", 1750.0)
    task_b, catalog_b = _fixture("B", 2650.0)
    result_a = build_variant_bindings({"tasks": [task_a]}, catalog_a)["rows"][0]
    result_b = build_variant_bindings({"tasks": [task_b]}, catalog_b)["rows"][0]
    assert result_a["prediction"]["unfolded_spec"] == "1750*1050*900"
    assert result_b["prediction"]["unfolded_spec"] == "2650*1050*900"
    assert result_a["binding_state"] == result_b["binding_state"]
    assert "opposite" not in {
        item["panel_id"] for item in result_a["audit"]["matched_panels"]
    }


def test_material_mismatch_blocks_the_variant() -> None:
    task, catalog = _fixture("C", 3100.0)
    task["material_code"] = "MT-99"
    panels = {panel["id"]: panel for panel in catalog["sheets"]}
    grouped: dict[str, list[dict[str, Any]]] = {}
    for entity in catalog["entities"]:
        grouped.setdefault(entity["sheet_id"], []).append(entity)
    result = build_variant_binding_row(task, panels, grouped)
    assert result["prediction"] is None
    assert result["reason_codes"] == ["PLAN_VARIANT_PANEL_NOT_UNIQUE"]
    assert {
        item["reason"] for item in result["audit"]["rejected_panels"]
    } == {"ROW_MATERIAL_CODE_NOT_PRINTED_IN_PANEL", "VARIANT_OR_COMPONENT_BASE_MISMATCH"}


def test_numeric_target_contamination_is_rejected() -> None:
    task, catalog = _fixture("D", 2400.0)
    task["length_mm"] = 1234
    with pytest.raises(ValueError, match="forbidden numeric target"):
        build_variant_bindings({"tasks": [task]}, catalog)


def test_variant_bindings_cli_writes_target_free_audit(tmp_path) -> None:
    task, catalog = _fixture("E", 2250.0)
    tasks_path = tmp_path / "tasks.json"
    panels_path = tmp_path / "panels.json"
    output_path = tmp_path / "bindings.json"
    tasks_path.write_text(
        json.dumps({"tasks": [task]}, ensure_ascii=False), encoding="utf-8"
    )
    panels_path.write_text(json.dumps(catalog, ensure_ascii=False), encoding="utf-8")
    assert (
        main(
            [
                "variant-bindings",
                str(tasks_path),
                str(panels_path),
                "--out",
                str(output_path),
            ]
        )
        == 0
    )
    payload = json.loads(output_path.read_text(encoding="utf-8"))
    assert payload["summary"]["bound_count"] == 1
    assert payload["rows"][0]["prediction"]["unfolded_spec"] == "2250*1050*900"
    assert payload["production_eligible"] is False
    assert payload["input_provenance"]["tasks_sha256"]
