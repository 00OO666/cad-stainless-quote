"""Create auditable quotation workbooks with the open-source XlsxWriter backend."""

from __future__ import annotations

import json
import math
import os
import tempfile
from datetime import UTC, date, datetime
from decimal import Decimal
from enum import Enum
from pathlib import Path
from typing import Any

import xlsxwriter
from openpyxl import load_workbook

from .calculation import calculate_item
from .io import write_json_atomic
from .models import EvidenceEdge, MeasurementCandidate, RunIssue, TakeoffItem

QUOTE_HEADERS = [
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

_SHEET_NAMES = ["报价表", "来源追踪", "待确认", "运行信息"]
_FORMULA_ERROR_VALUES = {"#REF!", "#DIV/0!", "#VALUE!", "#NAME?", "#N/A"}


def _enum_value(value: Any) -> Any:
    return value.value if isinstance(value, Enum) else value


def _safe_text(value: str) -> str:
    """Force untrusted CAD/workbook text to remain literal spreadsheet text."""

    if value.startswith(("=", "+", "-", "@", "\t", "\r", "\n")):
        return "'" + value
    return value


def _json_text(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)


def _cell_value(value: Any) -> Any:
    value = _enum_value(value)
    if value is None:
        return None
    if isinstance(value, str):
        return _safe_text(value)
    if isinstance(value, bool):
        return value
    if isinstance(value, Decimal):
        value = float(value)
    if isinstance(value, (int, float)):
        if isinstance(value, float) and not math.isfinite(value):
            return _safe_text(str(value))
        return value
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, (dict, list, tuple, set)):
        return _safe_text(_json_text(value))
    return _safe_text(str(value))


def _write_cell(
    worksheet: Any,
    row: int,
    column: int,
    value: Any,
    cell_format: Any | None = None,
) -> None:
    """Write a typed value without XlsxWriter's string formula/URL inference."""

    value = _cell_value(value)
    if value is None:
        worksheet.write_blank(row, column, None, cell_format)
    elif isinstance(value, bool):
        worksheet.write_boolean(row, column, value, cell_format)
    elif isinstance(value, (int, float)):
        worksheet.write_number(row, column, value, cell_format)
    else:
        worksheet.write_string(row, column, value, cell_format)


def _write_row(
    worksheet: Any,
    row: int,
    values: list[Any],
    cell_format: Any | None = None,
) -> None:
    for column, value in enumerate(values):
        _write_cell(worksheet, row, column, value, cell_format)


def _status_text(item: TakeoffItem) -> str:
    return str(_enum_value(item.status))


def _quantity_and_amount_cache(item: TakeoffItem) -> tuple[float | None, float | None]:
    """Return cached formula results consistent with deterministic takeoff JSON."""

    calculated = calculate_item(item)
    quantity = (
        item.engineering_quantity
        if item.engineering_quantity is not None
        else calculated.engineering_quantity
    )
    amount = item.amount if item.amount is not None else calculated.amount
    if _status_text(item) != "PASS":
        amount = None
    return quantity, amount


def _quote_note(item: TakeoffItem) -> str:
    notes = [f"[{_status_text(item)}]"]
    reasons = list(dict.fromkeys(value for value in (item.note, item.block_reason) if value))
    if reasons:
        reason = "；".join(reasons)
        if len(reason) > 260:
            reason = f"{reason[:260]}…详见“待确认”表"
        notes.append(reason)
    return "；".join(notes)


def _write_quote_sheet(workbook: Any, items: list[TakeoffItem]) -> dict[str, Any]:
    sheet = workbook.add_worksheet("报价表")
    sheet.hide_gridlines(2)
    sheet.freeze_panes(1, 0)

    navy = "#1F4E78"
    pale_blue = "#DCE6F1"
    light_border = "#D9E2F3"
    header = workbook.add_format(
        {
            "bg_color": navy,
            "font_color": "#FFFFFF",
            "bold": True,
            "align": "center",
            "valign": "vcenter",
            "text_wrap": True,
            "border": 1,
            "border_color": navy,
        }
    )
    body = workbook.add_format(
        {
            "valign": "vcenter",
            "text_wrap": True,
            "bottom": 1,
            "bottom_color": light_border,
        }
    )
    numeric = workbook.add_format(
        {
            "valign": "vcenter",
            "align": "right",
            "num_format": "#,##0.00",
            "bottom": 1,
            "bottom_color": light_border,
        }
    )
    centered = workbook.add_format(
        {
            "valign": "vcenter",
            "align": "center",
            "text_wrap": True,
            "bottom": 1,
            "bottom_color": light_border,
        }
    )
    status_formats = {
        "PASS": workbook.add_format(
            {
                "bg_color": "#E2F0D9",
                "align": "center",
                "valign": "vcenter",
                "bottom": 1,
                "bottom_color": light_border,
            }
        ),
        "BLOCK": workbook.add_format(
            {
                "bg_color": "#F4CCCC",
                "align": "center",
                "valign": "vcenter",
                "bottom": 1,
                "bottom_color": light_border,
            }
        ),
        "REVIEW": workbook.add_format(
            {
                "bg_color": "#FFF2CC",
                "align": "center",
                "valign": "vcenter",
                "bottom": 1,
                "bottom_color": light_border,
            }
        ),
    }
    total_label = workbook.add_format(
        {
            "bg_color": pale_blue,
            "bold": True,
            "bottom": 6,
            "bottom_color": navy,
        }
    )
    total_number = workbook.add_format(
        {
            "bg_color": pale_blue,
            "bold": True,
            "num_format": "#,##0.00",
            "bottom": 6,
            "bottom_color": navy,
        }
    )

    _write_row(sheet, 0, QUOTE_HEADERS, header)
    sheet.set_row(0, 30)
    caches: list[tuple[float | None, float | None]] = []
    for index, item in enumerate(items):
        excel_row = index + 2
        row = index + 1
        status = _status_text(item)
        engineering_cache, amount_cache = _quantity_and_amount_cache(item)
        caches.append((engineering_cache, amount_cache))
        values = [
            item.sequence,
            item.name,
            item.mt_code,
            item.material,
            item.plan_location,
            item.elevation,
            item.detail,
            item.unfolded_spec,
            item.width_mm,
            item.length_mm,
            item.quantity,
            None,
            item.unit,
            item.pricing_method,
            item.unit_price,
            None,
            _quote_note(item),
        ]
        for column, value in enumerate(values):
            if column == 0:
                cell_format = status_formats.get(status, status_formats["REVIEW"])
            elif column == 2:
                cell_format = centered
            elif 8 <= column <= 15:
                cell_format = numeric
            else:
                cell_format = body
            _write_cell(sheet, row, column, value, cell_format)

        quantity_formula = (
            f'=IF(M{excel_row}="㎡",I{excel_row}*J{excel_row}*K{excel_row}/1000000,'
            f'IF(M{excel_row}="m",J{excel_row}*K{excel_row}/1000,'
            f'IF(OR(M{excel_row}="件",M{excel_row}="套"),K{excel_row},"")))'
        )
        amount_formula = (
            f'=IF(LEFT(Q{excel_row},6)<>"[PASS]","",'
            f'IF(OR(L{excel_row}="",O{excel_row}=""),"",'
            f'ROUND(L{excel_row}*O{excel_row},2)))'
        )
        sheet.write_formula(
            row,
            11,
            quantity_formula,
            numeric,
            "" if engineering_cache is None else engineering_cache,
        )
        sheet.write_formula(
            row,
            15,
            amount_formula,
            numeric,
            "" if amount_cache is None else amount_cache,
        )
        formula_text = (
            f"{item.width_mm if item.width_mm is not None else '?'}×"
            f"{item.length_mm if item.length_mm is not None else '?'}×"
            f"{item.quantity if item.quantity is not None else '?'}÷1,000,000"
            if item.unit == "㎡"
            else f"{item.length_mm if item.length_mm is not None else '?'}×"
            f"{item.quantity if item.quantity is not None else '?'}"
        )
        sheet.write_comment(
            row,
            11,
            f"构件ID：{item.component_id or '—'}\n"
            f"计算：{formula_text}\n"
            f"证据：{'，'.join(item.evidence_ids) or '—'}",
            {"author": "User", "width": 420, "height": 150},
        )

    if items:
        sheet.add_table(
            0,
            0,
            len(items),
            len(QUOTE_HEADERS) - 1,
            {
                "name": "QuoteItemsTable",
                "style": "Table Style Medium 2",
                "columns": [{"header": value} for value in QUOTE_HEADERS],
            },
        )
    else:
        sheet.autofilter(0, 0, 0, len(QUOTE_HEADERS) - 1)

    total_row = len(items) + 1
    _write_cell(sheet, total_row, 14, "已确认合计", total_label)
    total_cache = sum(
        Decimal(str(amount))
        for item, (_, amount) in zip(items, caches, strict=True)
        if _status_text(item) == "PASS" and amount is not None
    )
    total_formula = f"=SUM(P2:P{len(items) + 1})" if items else "=0"
    sheet.write_formula(total_row, 15, total_formula, total_number, float(total_cache))

    widths = [7, 20, 12, 20, 24, 15, 15, 22, 10, 12, 10, 12, 9, 18, 12, 14, 38]
    for column, width in enumerate(widths):
        sheet.set_column(column, column, width)
    sheet.set_landscape()
    sheet.fit_to_pages(1, 0)
    sheet.repeat_rows(0)
    sheet.set_margins(0.3, 0.3, 0.5, 0.5)

    return {
        "total_excel_row": total_row + 1,
        "engineering_cache": [value[0] for value in caches],
        "amount_cache": [value[1] for value in caches],
        "total_cache": float(total_cache),
    }


def _subheader_format(workbook: Any) -> Any:
    return workbook.add_format(
        {
            "bg_color": "#DCE6F1",
            "font_color": "#1F1F1F",
            "bold": True,
            "valign": "vcenter",
            "text_wrap": True,
            "border": 1,
            "border_color": "#D9E2F3",
        }
    )


def _write_trace_sheet(
    workbook: Any,
    edges: list[EvidenceEdge],
    measurements: list[MeasurementCandidate],
) -> None:
    sheet = workbook.add_worksheet("来源追踪")
    sheet.hide_gridlines(2)
    sheet.freeze_panes(1, 0)
    headers = [
        "关系ID",
        "关系",
        "源ID",
        "目标ID",
        "候选角色",
        "原始值",
        "数值",
        "单位",
        "来源文件ID",
        "图纸ID",
        "实体ID",
        "依据",
        "置信度",
        "状态",
    ]
    _write_row(sheet, 0, headers, _subheader_format(workbook))
    body = workbook.add_format({"valign": "vcenter", "text_wrap": True})
    numeric = workbook.add_format(
        {"valign": "vcenter", "align": "right", "num_format": "#,##0.00"}
    )
    confidence = workbook.add_format(
        {"valign": "vcenter", "align": "right", "num_format": "0.000"}
    )
    measurement_by_id = {value.id: value for value in measurements}
    for row, edge in enumerate(edges, start=1):
        measurement = measurement_by_id.get(edge.target_id)
        values = [
            edge.id,
            edge.relation,
            edge.source_id,
            edge.target_id,
            measurement.role if measurement else None,
            measurement.raw_value if measurement else None,
            measurement.numeric_value if measurement else None,
            measurement.unit if measurement else None,
            measurement.source_file_id if measurement else None,
            measurement.sheet_id if measurement else None,
            "；".join(measurement.entity_ids) if measurement else None,
            "；".join(edge.basis),
            edge.confidence,
            edge.status,
        ]
        for column, value in enumerate(values):
            cell_format = numeric if column == 6 else confidence if column == 12 else body
            _write_cell(sheet, row, column, value, cell_format)
    widths = [30, 24, 30, 30, 14, 24, 12, 10, 28, 28, 38, 48, 12, 12]
    for column, width in enumerate(widths):
        sheet.set_column(column, column, width)
    if edges:
        sheet.autofilter(0, 0, len(edges), len(headers) - 1)


def _write_pending_sheet(
    workbook: Any,
    items: list[TakeoffItem],
    issues: list[RunIssue],
) -> int:
    sheet = workbook.add_worksheet("待确认")
    sheet.hide_gridlines(2)
    sheet.freeze_panes(1, 0)
    headers = ["类型", "序号/来源", "严重度/状态", "原因", "建议动作"]
    _write_row(sheet, 0, headers, _subheader_format(workbook))
    body = workbook.add_format({"valign": "vcenter", "text_wrap": True})
    rows: list[list[Any]] = []
    for item in items:
        if _status_text(item) != "PASS":
            rows.append(
                [
                    "算量项",
                    f"{item.sequence} / {item.component_id or '无构件ID'}",
                    item.status,
                    item.block_reason or item.note,
                    "回图复核并选择证据候选",
                ]
            )
    for issue in issues:
        rows.append(
            [
                "运行问题",
                issue.source_id,
                issue.severity,
                issue.message,
                issue.suggested_action,
            ]
        )
    for row, values in enumerate(rows, start=1):
        _write_row(sheet, row, values, body)
    for column, width in enumerate([14, 26, 14, 62, 42]):
        sheet.set_column(column, column, width)
    if rows:
        sheet.autofilter(0, 0, len(rows), len(headers) - 1)
    return len(rows)


def _write_run_sheet(
    workbook: Any,
    items: list[TakeoffItem],
    metadata: dict[str, Any],
    generated_at: str,
) -> None:
    sheet = workbook.add_worksheet("运行信息")
    sheet.hide_gridlines(2)
    sheet.freeze_panes(1, 0)
    _write_row(sheet, 0, ["键", "值"], _subheader_format(workbook))
    body = workbook.add_format({"valign": "vcenter", "text_wrap": True})
    statuses = [_status_text(item) for item in items]
    rows: list[list[Any]] = [
        ["generated_at", generated_at],
        ["item_count", len(items)],
        ["pass_count", statuses.count("PASS")],
        ["review_count", statuses.count("REVIEW")],
        ["block_count", statuses.count("BLOCK")],
    ]
    for key, value in sorted(metadata.items(), key=lambda pair: str(pair[0])):
        rows.append([str(key), value if isinstance(value, str) else _json_text(value)])
    for row, values in enumerate(rows, start=1):
        _write_row(sheet, row, values, body)
    sheet.set_column(0, 0, 28)
    sheet.set_column(1, 1, 78)


def _build_xlsx(
    destination: Path,
    items: list[TakeoffItem],
    edges: list[EvidenceEdge],
    measurements: list[MeasurementCandidate],
    issues: list[RunIssue],
    metadata: dict[str, Any],
    generated_at: str,
) -> dict[str, Any]:
    workbook = xlsxwriter.Workbook(
        str(destination),
        {
            "strings_to_formulas": False,
            "strings_to_urls": False,
            "nan_inf_to_errors": False,
        },
    )
    try:
        quote_state = _write_quote_sheet(workbook, items)
        _write_trace_sheet(workbook, edges, measurements)
        pending_count = _write_pending_sheet(workbook, items, issues)
        _write_run_sheet(workbook, items, metadata, generated_at)
    finally:
        workbook.close()
    return {**quote_state, "pending_count": pending_count}


def _same_cached_value(actual: Any, expected: float | None) -> bool:
    if expected is None:
        return actual in (None, "")
    if not isinstance(actual, (int, float)):
        return False
    return math.isclose(float(actual), float(expected), rel_tol=1e-9, abs_tol=1e-9)


def _validate_xlsx(path: Path, items: list[TakeoffItem], state: dict[str, Any]) -> dict[str, Any]:
    formula_book = load_workbook(path, read_only=True, data_only=False)
    try:
        actual_sheets = formula_book.sheetnames
        quote = formula_book["报价表"] if "报价表" in actual_sheets else None
        actual_headers = (
            [cell.value for cell in next(quote.iter_rows(min_row=1, max_row=1))]
            if quote
            else []
        )
        quantity_formulas = (
            [quote.cell(row=index + 2, column=12).value for index in range(len(items))]
            if quote
            else []
        )
        amount_formulas = (
            [quote.cell(row=index + 2, column=16).value for index in range(len(items))]
            if quote
            else []
        )
        total_formula = (
            quote.cell(row=state["total_excel_row"], column=16).value if quote else None
        )
        key_rows = []
        if quote:
            for cells in quote.iter_rows(
                min_row=1,
                max_row=min(state["total_excel_row"], 22),
                min_col=1,
                max_col=len(QUOTE_HEADERS),
            ):
                key_rows.append([cell.value for cell in cells])
    finally:
        formula_book.close()

    value_book = load_workbook(path, read_only=True, data_only=True)
    formula_errors: list[dict[str, Any]] = []
    try:
        quote_values = (
            value_book["报价表"] if "报价表" in value_book.sheetnames else None
        )
        quantity_values = (
            [
                quote_values.cell(row=index + 2, column=12).value
                for index in range(len(items))
            ]
            if quote_values
            else []
        )
        amount_values = (
            [
                quote_values.cell(row=index + 2, column=16).value
                for index in range(len(items))
            ]
            if quote_values
            else []
        )
        total_value = (
            quote_values.cell(row=state["total_excel_row"], column=16).value
            if quote_values
            else None
        )
        for sheet in value_book.worksheets:
            for cells in sheet.iter_rows():
                for cell in cells:
                    if cell.value in _FORMULA_ERROR_VALUES:
                        formula_errors.append(
                            {"sheet": sheet.title, "cell": cell.coordinate, "value": cell.value}
                        )
    finally:
        value_book.close()

    quantity_matches = all(
        _same_cached_value(actual, expected)
        for actual, expected in zip(
            quantity_values, state["engineering_cache"], strict=True
        )
    )
    amount_matches = all(
        _same_cached_value(actual, expected)
        for actual, expected in zip(amount_values, state["amount_cache"], strict=True)
    )
    checks = {
        "sheet_order": actual_sheets == _SHEET_NAMES,
        "quote_headers": actual_headers == QUOTE_HEADERS,
        "quantity_formulas": all(
            isinstance(value, str) and value.startswith("=IF(") for value in quantity_formulas
        ),
        "amount_formulas": all(
            isinstance(value, str) and value.startswith("=IF(") for value in amount_formulas
        ),
        "total_formula": isinstance(total_formula, str) and total_formula.startswith("="),
        "quantity_cache": quantity_matches,
        "amount_cache": amount_matches,
        "total_cache": _same_cached_value(total_value, state["total_cache"]),
        "formula_errors": not formula_errors,
    }
    report = {
        "backend": "XlsxWriter",
        "openpyxl_readable": True,
        "checks": checks,
        "sheet_names": actual_sheets,
        "quote_headers": actual_headers,
        "item_count": len(items),
        "formula_count": len(quantity_formulas) + len(amount_formulas) + 1,
        "formula_errors": formula_errors,
        "key_range": key_rows,
    }
    failed = [name for name, passed in checks.items() if not passed]
    if failed:
        raise RuntimeError(f"XlsxWriter workbook validation failed: {', '.join(failed)}")
    return report


def _record_preview_limitation(preview_dir: str | Path) -> dict[str, Any]:
    directory = Path(preview_dir).expanduser().resolve()
    directory.mkdir(parents=True, exist_ok=True)
    note = directory / "PREVIEW_NOT_GENERATED.txt"
    note.write_text(
        "未生成工作簿图片预览：开源 XlsxWriter/openpyxl 导出后端不包含渲染器。\n"
        "报价工作簿本身已通过 openpyxl 结构、公式和缓存值校验。\n",
        encoding="utf-8",
    )
    return {
        "requested": True,
        "generated": False,
        "directory": str(directory),
        "reason": "XlsxWriter/openpyxl backend does not include an image renderer",
        "note": str(note),
    }


def build_quote_workbook(
    items: list[TakeoffItem],
    output: str | Path,
    *,
    edges: list[EvidenceEdge] | None = None,
    measurements: list[MeasurementCandidate] | None = None,
    issues: list[RunIssue] | None = None,
    metadata: dict[str, Any] | None = None,
    preview_dir: str | Path | None = None,
    verification_report: str | Path | None = None,
) -> Path:
    """Create and validate a portable formula-driven quotation workbook."""

    destination = Path(output).expanduser().resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)
    generated_at = datetime.now(UTC).isoformat()
    edge_rows = edges or []
    measurement_rows = measurements or []
    issue_rows = issues or []
    run_metadata = metadata or {}

    with tempfile.TemporaryDirectory(
        prefix=".cadquote-xlsxwriter-",
        dir=destination.parent,
    ) as temp:
        temporary_output = Path(temp) / destination.name
        state = _build_xlsx(
            temporary_output,
            items,
            edge_rows,
            measurement_rows,
            issue_rows,
            run_metadata,
            generated_at,
        )
        report = _validate_xlsx(temporary_output, items, state)
        if not temporary_output.is_file() or temporary_output.stat().st_size == 0:
            raise RuntimeError("XlsxWriter reported success but produced no workbook")
        os.replace(temporary_output, destination)

    preview = (
        _record_preview_limitation(preview_dir)
        if preview_dir is not None
        else {"requested": False, "generated": False}
    )
    report.update(
        {
            "output": str(destination),
            "generated_at": generated_at,
            "edge_count": len(edge_rows),
            "measurement_count": len(measurement_rows),
            "pending_count": state["pending_count"],
            "preview": preview,
        }
    )
    if verification_report is not None:
        write_json_atomic(Path(verification_report).expanduser().resolve(), report)
    return destination
