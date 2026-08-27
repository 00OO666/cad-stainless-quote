from pathlib import Path

import ezdxf
from cadquote.doctor import run_doctor
from cadquote.models import CadEntity, MtOccurrence, Sheet
from cadquote.pipeline import _render_occurrences
from cadquote.render import (
    render_indexed_occurrences,
    render_panel_occurrence_crops,
    render_regions,
)


def test_doctor_reports_required_runtime():
    report = run_doctor()
    checks = {check["name"]: check for check in report["checks"]}
    assert checks["python"]["status"] == "PASS"
    assert checks["python:ezdxf"]["status"] == "PASS"
    assert checks["python:opencv-python-headless"]["status"] == "PASS"
    assert "dwg_converter" in checks


def test_region_render_writes_coordinate_index(tmp_path: Path):
    drawing = ezdxf.new("R2018")
    modelspace = drawing.modelspace()
    modelspace.add_line((0, 0), (100, 100))
    modelspace.add_text("MT-01", dxfattribs={"insert": (10, 50), "height": 5})
    source = tmp_path / "sample.dxf"
    drawing.saveas(source)

    output = tmp_path / "rendered"
    result = render_regions(source, {"MT-01 evidence": (0, 0, 120, 120)}, output)

    assert result["rendered_count"] == 1
    record = result["regions"]["MT-01 evidence"]
    assert record["bbox"] == [0.0, 0.0, 120.0, 120.0]
    assert (output / record["file"]).is_file()
    assert (output / "index.json").is_file()


def test_region_render_skips_fill_entities_and_records_the_reason(tmp_path: Path):
    drawing = ezdxf.new("R2018")
    modelspace = drawing.modelspace()
    modelspace.add_line((0, 0), (100, 100))
    hatch = modelspace.add_hatch(color=3)
    hatch.paths.add_polyline_path([(0, 0), (100, 0), (100, 100), (0, 100)], is_closed=True)
    source = tmp_path / "filled.dxf"
    drawing.saveas(source)

    result = render_regions(
        source,
        {"line evidence": (0, 0, 120, 120)},
        tmp_path / "rendered-fill",
    )

    assert result["rendered_count"] == 1
    assert result["skipped_entity_count"] == 1
    assert result["skipped_entity_type_counts"] == {"HATCH": 1}


def test_occurrence_render_uses_real_paper_layout_and_model_for_virtual_panel(
    tmp_path: Path,
):
    drawing = ezdxf.new("R2018")
    drawing.modelspace().add_text("MT-01", dxfattribs={"insert": (10, 10), "height": 2})
    paper = drawing.layouts.new("图纸A")
    paper.add_text("MT-02", dxfattribs={"insert": (20, 20), "height": 2})
    source = tmp_path / "layouts.dxf"
    drawing.saveas(source)
    source_id = "file:test"
    sheets = [
        Sheet(
            id="sheet:paper",
            source_file_id=source_id,
            title="纸空间",
            layout="图纸A",
        ),
        Sheet(
            id="panel:virtual",
            source_file_id=source_id,
            title="虚拟面板",
            layout="图纸A#viewport:AB",
        ),
    ]
    occurrences = [
        MtOccurrence(
            id="occurrence:paper",
            mt_code="MT-02",
            source_file_id=source_id,
            sheet_id="sheet:paper",
            anchor=(20, 20),
        ),
        MtOccurrence(
            id="occurrence:model",
            mt_code="MT-01",
            source_file_id=source_id,
            sheet_id="panel:virtual",
            anchor=(10, 10),
        ),
    ]
    result = _render_occurrences(
        occurrences,
        sheets,
        {source_id: source},
        tmp_path / "evidence",
        [],
    )
    groups = {group["layout"]: group for group in result["groups"]}
    assert set(groups) == {"Model", "图纸A"}
    assert groups["图纸A"]["regions"]["occurrence:paper"]["bbox"]
    assert groups["Model"]["regions"]["occurrence:model"]["occurrence_id"] == (
        "occurrence:model"
    )


def test_indexed_occurrence_render_includes_projected_annotation(tmp_path: Path):
    sheet = Sheet(
        id="panel:1",
        source_file_id="file:1",
        drawing_number="1F-E-01",
        kind="elevation",
        layout="布局1#viewport:AB",
        bbox=(0, 0, 1_000, 1_000),
    )
    entities = [
        CadEntity(
            id="line",
            source_file_id="file:1",
            sheet_id=sheet.id,
            entity_type="LINE",
            space="model@布局1#AB",
            bbox=(100, 100, 900, 900),
            geometry={"start": [100, 100], "end": [900, 900]},
        ),
        CadEntity(
            id="mt-label",
            source_file_id="file:1",
            sheet_id=sheet.id,
            entity_type="ATTRIB",
            space="model@布局1#AB",
            text="MT-01",
            insert=(500, 520),
            bbox=(480, 510, 550, 530),
        ),
    ]
    occurrence = MtOccurrence(
        id="occurrence:1",
        mt_code="MT-01",
        source_file_id="file:1",
        sheet_id=sheet.id,
        entity_ids=["mt-label"],
        leader_target=(500, 500),
    )

    result = render_indexed_occurrences(
        [sheet],
        entities,
        [occurrence],
        tmp_path / "indexed",
        target_px=400,
    )

    assert result["rendered_count"] == 1
    record = result["regions"][occurrence.id]
    assert record["drawing_number"] == "1F-E-01"
    assert record["backend"] == "indexed-matplotlib-agg"
    assert (tmp_path / "indexed" / record["file"]).is_file()


def test_panel_render_is_reused_for_occurrence_crop(tmp_path: Path):
    drawing = ezdxf.new("R2018")
    drawing.modelspace().add_line((0, 0), (1_000, 1_000))
    source = tmp_path / "panel-source.dxf"
    drawing.saveas(source)
    sheet = Sheet(
        id="panel:crop",
        source_file_id="file:crop",
        drawing_number="1F-E-01",
        kind="elevation",
        layout="布局1#viewport:AB",
        bbox=(0, 0, 1_000, 1_000),
    )
    occurrences = [
        MtOccurrence(
            id="occurrence:a",
            mt_code="MT-01",
            source_file_id="file:crop",
            sheet_id=sheet.id,
            leader_target=(300, 300),
        ),
        MtOccurrence(
            id="occurrence:b",
            mt_code="MT-02",
            source_file_id="file:crop",
            sheet_id=sheet.id,
            leader_target=(700, 700),
        ),
    ]

    result = render_panel_occurrence_crops(
        [sheet],
        occurrences,
        {"file:crop": source},
        tmp_path / "panel-crops",
        target_px=500,
    )

    assert result["panel_count"] == 1
    assert result["rendered_count"] == 2
    for record in result["occurrences"].values():
        assert Path(record["absolute_path"]).is_file()
        assert record["backend"] == "raw-panel-then-crop"
