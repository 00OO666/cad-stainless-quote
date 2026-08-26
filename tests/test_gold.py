"""Gold-workbook import and audit regression tests."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from cadquote.gold import import_gold_workbook
from openpyxl import Workbook


def _write_canonical_xlsx(path: Path) -> None:
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "报价表"
    sheet.append(
        [
            "序号",
            "名称",
            "MT编号",
            "材料",
            "平面图位置",
            "对应立面",
            "对应节点",
            "展开规格",
            "宽",
            "长度",
            "数量",
            "工程量",
            "单位",
            "计价方式",
            "单价",
            "金额",
            "备注",
        ]
    )
    sheet.append(
        [
            1,
            "墙面收口",
            "mt1",
            "304不锈钢",
            "门厅",
            "EL-01",
            "DT-01",
            "50+150",
            200,
            1000,
            2,
            0.4,
            "㎡",
            "按展开面积",
            100,
            "=L2*O2",
            "人工待审",
        ]
    )
    sheet.append(
        [
            2,
            "踢脚线",
            "MT-02",
            "304不锈钢",
            "走廊",
            "EL-02",
            "DT-02",
            None,
            80,
            2000,
            2,
            2,
            "m",
            "按米",
            None,
            None,
            None,
        ]
    )
    sheet["L2"].number_format = sheet["L3"].number_format = "0.000"
    workbook.save(path)


def test_import_canonical_xlsx_preserves_evidence_and_never_passes(tmp_path: Path) -> None:
    source = tmp_path / "gold.xlsx"
    _write_canonical_xlsx(source)

    result = import_gold_workbook(source)

    assert result.workbook_format == "xlsx"
    assert result.summary.row_count == 2
    assert result.summary.mt_distribution == {"MT-01": 1, "MT-02": 1}
    assert result.summary.pass_count == 0
    assert result.summary.review_count == 1
    assert result.summary.block_count == 1
    assert result.summary.quantity_mismatch_count == 1
    assert result.rows[0].quantity_audit == "MATCH"
    assert result.rows[0].item.status.value == "REVIEW"
    assert result.rows[0].field_cells["mt_code"] == ["C2"]
    assert len(result.rows[0].raw_cells) == 17
    assert result.rows[0].raw_cells[15].coordinate == "P2"
    assert result.rows[0].raw_cells[15].formula == "=L2*O2"
    assert result.rows[1].recalculated_engineering_quantity == 4
    assert result.rows[1].reported_engineering_quantity == 2
    assert result.issues[0].code == "QUANTITY_FORMULA_MISMATCH"

    payload = json.loads(result.to_json())
    assert payload["source_sha256"] == result.source_sha256
    assert payload["rows"][0]["raw_cells"][2]["raw_value"] == "mt1"
    output = result.write_json(tmp_path / "gold.json")
    assert output.is_file()


def test_rejects_non_excel_file(tmp_path: Path) -> None:
    source = tmp_path / "gold.csv"
    source.write_text("序号,名称", encoding="utf-8")
    with pytest.raises(ValueError, match="unsupported gold workbook format"):
        import_gold_workbook(source)
