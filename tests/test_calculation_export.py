import json
import struct
import zlib
from argparse import Namespace
from pathlib import Path

import pytest
from cad_quote import command_quote
from cadquote.calculation import calculate_item, evaluate_numeric_expression
from cadquote.evaluation import evaluate_takeoff
from cadquote.exporter import QUOTE_HEADERS, build_quote_workbook
from cadquote.models import (
    EvidenceEdge,
    MaterialSpec,
    MeasurementCandidate,
    PriceBook,
    PriceEntry,
    ReviewStatus,
    RunIssue,
    Severity,
    TakeoffItem,
)
from cadquote.pricing import apply_price, load_price_book
from openpyxl import Workbook, load_workbook


def item(**updates):
    values = {
        "sequence": 1,
        "name": "不锈钢收口线",
        "mt_code": "MT-01",
        "unfolded_spec": "10+180+10",
        "length_mm": 5000,
        "quantity": 4,
        "unit": "㎡",
        "pricing_method": "按实际展开面积计算",
        "status": ReviewStatus.REVIEW,
    }
    values.update(updates)
    return TakeoffItem(**values)


def _write_test_png(path: Path, width: int = 600, height: int = 300) -> None:
    def chunk(kind: bytes, data: bytes) -> bytes:
        payload = kind + data
        return (
            struct.pack(">I", len(data))
            + payload
            + struct.pack(">I", zlib.crc32(payload) & 0xFFFFFFFF)
        )

    scanlines = b"".join(b"\x00" + b"\xE8\xEE\xF5" * width for _ in range(height))
    path.write_bytes(
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(scanlines))
        + chunk(b"IEND", b"")
    )


def test_xlsx_price_book_rejects_commercially_incomplete_columns(tmp_path: Path):
    path = tmp_path / "incomplete-price-book.xlsx"
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "价格库"
    sheet.append(["MT编号", "计价方式", "单位", "单价"])
    sheet.append(["MT-01", "按米", "m", 100])
    workbook.save(path)

    with pytest.raises(ValueError, match="price book missing columns"):
        load_price_book(path)


def test_safe_expression_and_quantity_calculation():
    assert evaluate_numeric_expression("5+10+120+10") == 145
    with pytest.raises(ValueError):
        evaluate_numeric_expression("__import__('os').system('whoami')")
    calculated = calculate_item(item())
    assert calculated.width_mm == 200
    assert calculated.engineering_quantity == 4.0
    assert calculated.status == ReviewStatus.REVIEW


def test_linear_and_piece_calculation():
    linear = calculate_item(
        item(unfolded_spec=None, width_mm=None, length_mm=2500, quantity=2, unit="m")
    )
    assert linear.engineering_quantity == 5
    piece = calculate_item(
        item(unfolded_spec=None, width_mm=None, length_mm=None, quantity=3, unit="件")
    )
    assert piece.engineering_quantity == 3


def test_exact_approved_price_match():
    material = MaterialSpec(
        id="m1",
        mt_code="MT-01",
        name="古铜色不锈钢",
        grade="304",
        thickness_mm=1.2,
        finish="PVD",
        process="折弯",
    )
    entry = PriceEntry(
        id="p1",
        version="v1",
        approved=True,
        mt_code="MT-01",
        material="古铜色不锈钢",
        grade="304",
        thickness_mm=1.2,
        finish="PVD",
        process="折弯",
        pricing_method="按实际展开面积计算",
        unit="㎡",
        unit_price=500,
        tax_included=True,
        source="approved.xlsx",
    )
    book = PriceBook(version="v1", approved=True, source="approved.xlsx", entries=[entry])
    priced, issues = apply_price(item(), material, book)
    assert not issues
    assert priced.unit_price == 500
    assert priced.price_entry_id == "p1"
    assert calculate_item(priced).amount == 2000


def test_expired_or_incomplete_price_never_matches():
    material = MaterialSpec(
        id="m1",
        mt_code="MT-01",
        name="古铜色不锈钢",
        grade="304",
        thickness_mm=1.2,
        finish="PVD",
        process="折弯",
    )
    expired = PriceEntry(
        id="expired",
        version="v1",
        approved=True,
        mt_code="MT-01",
        material="古铜色不锈钢",
        grade="304",
        thickness_mm=1.2,
        finish="PVD",
        process="折弯",
        pricing_method="按实际展开面积计算",
        unit="㎡",
        unit_price=500,
        tax_included=True,
        valid_to="2000-01-01",
        source="expired.xlsx",
    )
    book = PriceBook(version="v1", approved=True, source="expired.xlsx", entries=[expired])
    priced, issues = apply_price(item(), material, book, quote_date="2026-08-26")
    assert priced.unit_price is None
    assert any("过期" in issue for issue in issues)

    incomplete_material = material.model_copy(update={"process": None})
    priced, issues = apply_price(item(), incomplete_material, book, quote_date="1999-01-01")
    assert priced.unit_price is None
    assert any("材料证据缺少" in issue for issue in issues)


def test_portable_quote_workbook(tmp_path: Path):
    calculated = calculate_item(item(unit_price=500, status=ReviewStatus.PASS))
    output = build_quote_workbook([calculated], tmp_path / "quote.xlsx")
    workbook = load_workbook(output, data_only=False)
    assert workbook.sheetnames == ["报价表", "来源追踪", "待确认", "运行信息"]
    assert [cell.value for cell in workbook["报价表"][1]] == QUOTE_HEADERS
    assert workbook["报价表"]["L2"].value.startswith("=IF(")
    assert workbook["报价表"]["P2"].value.startswith("=IF(")
    calculated_values = load_workbook(output, data_only=True)
    assert calculated_values["报价表"]["L2"].value == 4
    assert calculated_values["报价表"]["P2"].value == 2000


def test_optional_screenshot_evidence_sheet_embeds_images_and_marks_missing(
    tmp_path: Path,
):
    image_path = tmp_path / "evidence.png"
    _write_test_png(image_path)

    class DumpableEvidence:
        def model_dump(self, *, mode: str):
            assert mode == "json"
            return {
                "mt_code": "MT-02",
                "component_id": "component:2",
                "stage": "节点",
                "status": "REVIEW",
                "render_state": "FAILED",
                "drawing_number": "DT-02",
                "source_file_id": "file:cad",
                "focus_bbox": [10, 20, 30, 40],
                "entity_ids": ["entity:2"],
                "entity_handles": ["2B"],
                "context_image": str(image_path),
                "detail_image": str(tmp_path / "missing.png"),
                "render_reason": "渲染阶段未产出放大图",
            }

    report_path = tmp_path / "verification.json"
    output = build_quote_workbook(
        [calculate_item(item(unit_price=500, status=ReviewStatus.PASS))],
        tmp_path / "with-evidence.xlsx",
        evidence_records=[
            {
                "sequence": 7,
                "mt_code": "MT-01",
                "component_id": "component:1",
                "stage": "立面",
                "status": "REVIEW",
                "drawing_number": "EL-01",
                "source_file": "sample.dxf",
                "cad_bbox": [1, 2, 3, 4],
                "entity_ids": ["entity:1", "entity:1b"],
                "dxf_handles": ["1A", "1B"],
                "location_image": image_path,
                "zoom_image": image_path,
            },
            DumpableEvidence(),
        ],
        verification_report=report_path,
    )

    workbook = load_workbook(output, data_only=False)
    try:
        assert workbook.sheetnames == [
            "报价表",
            "来源追踪",
            "待确认",
            "运行信息",
            "截图证据",
        ]
        sheet = workbook["截图证据"]
        assert [cell.value for cell in sheet[1]] == [
            "序号",
            "MT",
            "构件ID",
            "阶段",
            "状态",
            "图号",
            "来源文件",
            "CAD bbox",
            "实体ID",
            "DXF Handle",
            "定位图",
            "放大图",
            "缺图原因",
        ]
        assert len(sheet._images) == 3
        assert all(image.width <= 600 and image.height <= 300 for image in sheet._images)
        assert all(image.width / image.height == pytest.approx(2.0) for image in sheet._images)
        assert sheet.row_dimensions[2].height >= 180
        assert sheet["L3"].value.startswith("缺图：")
        assert "渲染阶段未产出放大图" in sheet["M3"].value
        assert sheet["E3"].value == "FAILED"
        assert sheet["G3"].value == "file:cad"
        assert sheet["H3"].value == "[10, 20, 30, 40]"
        assert sheet["J3"].value == "2B"
        assert sheet["L3"].font.color.rgb.endswith("C00000")
        assert sheet["M3"].font.color.rgb.endswith("C00000")
    finally:
        workbook.close()

    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert report["evidence"] == {
        "record_count": 2,
        "image_count": 3,
        "missing_rows": [3],
    }
    assert all(
        report["checks"][name]
        for name in (
            "sheet_order",
            "evidence_headers",
            "evidence_image_count",
            "evidence_missing_rows",
            "evidence_missing_red",
            "evidence_image_scale",
        )
    )


def test_partial_quote_preserves_full_takeoff_evidence(tmp_path: Path):
    candidate = MeasurementCandidate(
        id="measurement:1",
        component_id="component:1",
        role="length",
        raw_value="5000",
        numeric_value=5000,
        unit="mm",
        source_file_id="file:1",
        sheet_id="sheet:1",
        entity_ids=["entity:1"],
    )
    edge = EvidenceEdge(
        id="edge:1",
        relation="component_to_dimension",
        source_id="component:1",
        target_id=candidate.id,
    )
    issue = RunIssue(
        stage="takeoff",
        severity=Severity.WARNING,
        code="REVIEW",
        message="需要复核",
    )
    payload_path = tmp_path / "takeoff.json"
    payload_path.write_text(
        json.dumps(
            {
                "items": [item(component_id="component:1").model_dump(mode="json")],
                "evidence_edges": [edge.model_dump(mode="json")],
                "measurements": [candidate.model_dump(mode="json")],
                "issues": [issue.model_dump(mode="json")],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    output = tmp_path / "partial.xlsx"
    assert command_quote(Namespace(takeoff=payload_path, out=output)) == 0
    workbook = load_workbook(output)
    assert workbook["来源追踪"]["A2"].value == "edge:1"
    assert workbook["来源追踪"]["I2"].value == "file:1"
    assert workbook["待确认"]["D3"].value == "需要复核"


def test_quote_total_excludes_unresolved_rows(tmp_path: Path):
    passed = calculate_item(item(sequence=1, unit_price=10, status=ReviewStatus.PASS))
    blocked = calculate_item(item(sequence=2, unit_price=10, status=ReviewStatus.BLOCK))
    output = build_quote_workbook([passed, blocked], tmp_path / "safe-total.xlsx")
    values = load_workbook(output, data_only=True)["报价表"]
    assert values["P2"].value == 40
    assert values["P3"].value is None
    assert values["P4"].value == 40


def test_quote_escapes_untrusted_formula_text(tmp_path: Path):
    malicious = calculate_item(
        item(
            name="=WEBSERVICE(\"https://example.invalid\")",
            plan_location="+cmd|' /C calc'!A0",
            note="@SUM(1,1)",
            status=ReviewStatus.BLOCK,
        )
    )
    output = build_quote_workbook([malicious], tmp_path / "literal-text.xlsx")
    sheet = load_workbook(output, data_only=False)["报价表"]
    for coordinate in ("B2", "E2", "Q2"):
        assert sheet[coordinate].data_type != "f"


def test_quote_amount_rounding_matches_deterministic_json(tmp_path: Path):
    rows = [
        calculate_item(
            item(
                sequence=sequence,
                unfolded_spec=None,
                width_mm=1,
                length_mm=5000,
                quantity=1,
                unit_price=1,
                status=ReviewStatus.PASS,
            )
        )
        for sequence in (1, 2)
    ]
    output = build_quote_workbook(rows, tmp_path / "rounding.xlsx")
    values = load_workbook(output, data_only=True)["报价表"]
    assert [row.amount for row in rows] == [0.01, 0.01]
    assert values["P2"].value == 0.01
    assert values["P3"].value == 0.01
    assert values["P4"].value == 0.02


def test_evaluation_separates_precision_and_automation():
    gold = [calculate_item(item(status=ReviewStatus.PASS))]
    predicted = [calculate_item(item(status=ReviewStatus.PASS))]
    report = evaluate_takeoff(predicted, gold)
    assert report["pass_precision"] == 1
    assert report["automation_rate"] == 1
