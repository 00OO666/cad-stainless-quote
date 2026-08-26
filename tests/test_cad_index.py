from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import ezdxf
import pytest
from cadquote import converter
from cadquote.cad_index import build_cad_index, index_dxf
from cadquote.classifier import classify_sheet


def _make_fixture(path: Path) -> Path:
    document = ezdxf.new("R2018")
    document.header["$INSUNITS"] = 4
    modelspace = document.modelspace()
    modelspace.add_text("MT-01", dxfattribs={"height": 3}).set_placement((10, 20))
    modelspace.add_mtext("镜面不锈钢", dxfattribs={"char_height": 3}).set_location((30, 20))

    inner = document.blocks.new("INNER_MT")
    inner.add_text("MT", dxfattribs={"height": 2}).set_placement((0, 0))
    block = document.blocks.new("MT_TAG")
    block.add_attdef("CODE", insert=(0, 0), height=2)
    block.add_blockref("INNER_MT", (5, 0))
    insert = modelspace.add_blockref("MT_TAG", (40, 20))
    insert.add_auto_attribs({"CODE": "01"})

    dimension = modelspace.add_linear_dim(base=(0, 10), p1=(0, 0), p2=(100, 0))
    dimension.render()
    modelspace.add_leader([(0, 0), (5, 5), (10, 5)])

    sheet = document.layouts.new("一层平面")
    sheet.add_text("P-01 一层平面图", dxfattribs={"height": 4}).set_placement((5, 5))
    sheet.add_viewport(
        center=(100, 70),
        size=(180, 120),
        view_center_point=(50, 50),
        view_height=100,
    )
    document.saveas(path)
    return path


def test_classifier_uses_semantics_and_stays_conservative() -> None:
    plan = classify_sheet("01 平面系统图0728.dwg")
    assert plan.kind == "plan"
    assert plan.confidence >= 0.6

    elevation_index = classify_sheet("drawing.dwg", ["EL-01 首层立面索引图"])
    assert elevation_index.kind == "elevation_index"
    assert elevation_index.drawing_number == "EL-01"

    code_only = classify_sheet("A-E-01.dwg")
    assert code_only.kind == "unknown"
    assert code_only.confidence < 0.5

    mixed = classify_sheet("combined.dwg", ["首层平面图", "首层立面图"])
    assert mixed.kind == "unknown"

    assert classify_sheet("地花大样图-FD-01.dwg").kind == "floor"
    assert classify_sheet("天花大样图-CD-01.dwg").kind == "ceiling"
    assert classify_sheet("售楼部门大样图-M-01.dwg").kind == "door"


def test_index_all_layouts_and_semantic_entities(tmp_path: Path) -> None:
    drawing = _make_fixture(tmp_path / "fixture.dxf")
    result = index_dxf(drawing, source_file_id="file:test-source")

    assert result.source_file_id == "file:test-source"
    assert result.units == "millimeters"
    assert len(result.sheets) >= 2
    assert {"Model", "一层平面"} <= {sheet.layout for sheet in result.sheets}

    types = {entity.entity_type for entity in result.entities}
    assert {
        "TEXT",
        "MTEXT",
        "ATTRIB",
        "DIMENSION",
        "LEADER",
        "INSERT",
        "VIEWPORT",
    } <= types
    assert all(entity.source_file_id == "file:test-source" for entity in result.entities)
    assert all(entity.sheet_id for entity in result.entities)

    attribute = next(entity for entity in result.entities if entity.entity_type == "ATTRIB")
    assert attribute.text == "01"
    assert attribute.geometry["parent_insert_handle"]

    virtual_text = next(
        entity
        for entity in result.entities
        if entity.entity_type == "TEXT"
        and entity.text == "MT"
        and entity.geometry.get("virtual") is True
    )
    assert virtual_text.insert == pytest.approx((45.0, 20.0))
    assert virtual_text.handle is None
    assert virtual_text.geometry["block_path"] == ["MT_TAG", "INNER_MT"]
    assert virtual_text.geometry["source_block_entity_handle"]
    assert len(virtual_text.geometry["parent_insert_chain"]) == 2
    assert result.virtual_entity_count >= 2
    assert not result.block_expansion_truncated
    assert not any(
        entity.entity_type == "ATTDEF" and entity.geometry.get("virtual") is True
        for entity in result.entities
    )

    measured = next(entity for entity in result.entities if entity.entity_type == "DIMENSION")
    assert measured.value == pytest.approx(100.0)
    assert measured.handle
    assert measured.geometry["units_code"] == 4
    assert measured.geometry["units"] == "millimeters"
    assert measured.geometry["display_measurement"] == pytest.approx(100.0)

    viewport = next(entity for entity in result.entities if entity.entity_type == "VIEWPORT")
    assert viewport.bbox
    assert len(viewport.geometry["model_bbox"]) == 4


def test_json_and_sqlite_are_repeatable(tmp_path: Path) -> None:
    drawing = _make_fixture(tmp_path / "fixture.dxf")
    database = tmp_path / "cad-index.sqlite"
    export = tmp_path / "cad-index.json"

    first = build_cad_index(drawing, sqlite_path=database, json_path=export)
    first_ids = [entity.id for entity in first.entities]
    second = build_cad_index(drawing, sqlite_path=database)

    assert first_ids == [entity.id for entity in second.entities]
    payload = json.loads(export.read_text(encoding="utf-8"))
    assert payload["source_count"] == 1
    assert payload["entity_count"] == len(first.entities)

    with sqlite3.connect(database) as connection:
        source_count = connection.execute("SELECT COUNT(*) FROM cad_sources").fetchone()[0]
        sheet_count = connection.execute("SELECT COUNT(*) FROM sheets").fetchone()[0]
        entity_count = connection.execute("SELECT COUNT(*) FROM cad_entities").fetchone()[0]
    assert source_count == 1
    assert sheet_count == len(first.sheets)
    assert entity_count == len(first.entities)


def test_block_expansion_depth_and_entity_limits_are_explicit(tmp_path: Path) -> None:
    drawing = _make_fixture(tmp_path / "fixture.dxf")

    shallow = index_dxf(drawing, max_block_depth=1)
    assert not any(
        entity.text == "MT" and entity.geometry.get("virtual") is True
        for entity in shallow.entities
    )
    assert any("depth 1 reached" in warning for warning in shallow.warnings)

    limited = index_dxf(drawing, max_virtual_entities=1)
    assert limited.virtual_entity_count == 1
    assert limited.block_expansion_truncated
    assert any("virtual entity limit 1 reached" in warning for warning in limited.warnings)


def test_cyclic_block_reference_is_stopped(tmp_path: Path) -> None:
    drawing = tmp_path / "cycle.dxf"
    document = ezdxf.new("R2018")
    block = document.blocks.new("LOOP")
    block.add_text("MT").set_placement((0, 0))
    block.add_blockref("LOOP", (10, 0))
    document.modelspace().add_blockref("LOOP", (100, 100))
    document.saveas(drawing)

    result = index_dxf(drawing)

    assert not result.block_expansion_truncated
    assert any("cyclic block reference" in warning for warning in result.warnings)
    assert result.virtual_entity_count == 2


def test_converter_audits_missing_tool_and_uses_unique_names(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    first_dir = tmp_path / "first"
    second_dir = tmp_path / "second"
    first_dir.mkdir()
    second_dir.mkdir()
    (first_dir / "same.dwg").write_bytes(b"AC1032-identical")
    (second_dir / "same.dwg").write_bytes(b"AC1032-identical")
    monkeypatch.setattr(converter, "discover_converters", lambda explicit=None: [])

    audit = converter.convert_dwgs([first_dir, second_dir], tmp_path / "out")

    assert audit.expected_count == 2
    assert audit.attempted_count == 0
    assert audit.succeeded_count == 0
    assert audit.failed_count == 2
    assert len({record.destination for record in audit.records}) == 2
    assert all(record.source_sha256 for record in audit.records)
    assert all("No DWG converter found" in (record.error or "") for record in audit.records)
