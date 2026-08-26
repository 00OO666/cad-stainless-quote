from pathlib import Path

import ezdxf
from cadquote.doctor import run_doctor
from cadquote.models import MtOccurrence, Sheet
from cadquote.pipeline import _render_occurrences
from cadquote.render import render_regions


def test_doctor_reports_required_runtime():
    report = run_doctor()
    checks = {check["name"]: check for check in report["checks"]}
    assert checks["python"]["status"] == "PASS"
    assert checks["python:ezdxf"]["status"] == "PASS"
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
