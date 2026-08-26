"""Import and audit human-authored quote workbooks as reviewable gold data.

The importer deliberately treats a workbook as *candidate* gold.  Imported rows
remain REVIEW (or BLOCK when an audit finds a material problem); this module never
turns an unreviewed spreadsheet row into PASS.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from collections import Counter
from datetime import date, datetime, time
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Literal

import xlrd
from openpyxl import load_workbook
from openpyxl.utils import get_column_letter
from pydantic import BaseModel, ConfigDict, Field

from .calculation import evaluate_numeric_expression, infer_unfolded_width, infer_unit
from .models import ReviewStatus, TakeoffItem

CANONICAL_FIELDS = (
    "sequence",
    "name",
    "mt_code",
    "material",
    "plan_location",
    "elevation",
    "detail",
    "unfolded_spec",
    "width_mm",
    "length_mm",
    "quantity",
    "engineering_quantity",
    "unit",
    "pricing_method",
    "unit_price",
    "amount",
    "note",
)

_CANONICAL_HEADERS = {
    "序号": "sequence",
    "名称": "name",
    "mt编号": "mt_code",
    "材料": "material",
    "平面图位置": "plan_location",
    "对应立面": "elevation",
    "对应节点": "detail",
    "展开规格": "unfolded_spec",
    "宽": "width_mm",
    "长度": "length_mm",
    "数量": "quantity",
    "工程量": "engineering_quantity",
    "单位": "unit",
    "计价方式": "pricing_method",
    "单价": "unit_price",
    "金额": "amount",
    "备注": "note",
}

_HEADER_ALIASES = {
    **_CANONICAL_HEADERS,
    "编号": "sequence",
    "项次": "sequence",
    "项目名称": "name",
    "部位": "name",
    "mt号": "mt_code",
    "mt编码": "mt_code",
    "材质": "material",
    "位置": "plan_location",
    "立面": "elevation",
    "立面编号": "elevation",
    "节点": "detail",
    "节点编号": "detail",
    "大样": "detail",
    "大样编号": "detail",
    "计算式附图": "unfolded_spec",
    "计算式": "unfolded_spec",
    "规格尺寸": "unfolded_spec",
    "展开宽": "width_mm",
    "展开宽度": "width_mm",
    "长": "length_mm",
    "件数": "quantity",
    "计量单位": "unit",
    "含税单价": "unit_price",
    "含税总价": "amount",
    "总价": "amount",
}

_MT_RE = re.compile(r"\bMT\s*[-_]?\s*(\d{1,3})(?:\s*[-_]\s*(\d{1,3}))?\b", re.I)
_NUMBER_FORMAT_DECIMALS = re.compile(r"\.([0#?]+)")
_XLRD_CELL_TYPES = {
    xlrd.XL_CELL_EMPTY: "empty",
    xlrd.XL_CELL_TEXT: "text",
    xlrd.XL_CELL_NUMBER: "number",
    xlrd.XL_CELL_DATE: "date",
    xlrd.XL_CELL_BOOLEAN: "boolean",
    xlrd.XL_CELL_ERROR: "error",
    xlrd.XL_CELL_BLANK: "blank",
}


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class GoldCellEvidence(_StrictModel):
    """Loss-minimised evidence for one source workbook cell."""

    sheet_name: str
    sheet_index: int = Field(ge=0)
    row: int = Field(ge=1)
    column: int = Field(ge=1)
    coordinate: str
    raw_value: Any = None
    value_repr: str
    formula: str | None = None
    data_type: str
    number_format: str | None = None


class GoldSheetEvidence(_StrictModel):
    sheet_name: str
    sheet_index: int = Field(ge=0)
    max_row: int = Field(ge=0)
    max_column: int = Field(ge=0)
    header_rows: list[int] = Field(default_factory=list)
    header_cells: list[GoldCellEvidence] = Field(default_factory=list)
    field_columns: dict[str, int] = Field(default_factory=dict)
    imported_row_count: int = Field(default=0, ge=0)


class GoldAuditIssue(_StrictModel):
    id: str
    code: Literal[
        "QUANTITY_FORMULA_MISMATCH",
        "QUANTITY_ROUNDING_DEVIATION",
        "QUANTITY_NOT_CALCULABLE",
        "AMOUNT_FORMULA_MISMATCH",
        "MISSING_REQUIRED_FIELD",
    ]
    severity: Literal["REVIEW", "BLOCK"]
    message: str
    sheet_name: str
    row: int = Field(ge=1)
    field: str
    actual: str | None = None
    expected: str | None = None
    difference: str | None = None
    tolerance: str | None = None
    evidence_cells: list[str] = Field(default_factory=list)


class GoldRow(_StrictModel):
    id: str
    sheet_name: str
    sheet_index: int = Field(ge=0)
    row: int = Field(ge=1)
    item: TakeoffItem
    raw_cells: list[GoldCellEvidence]
    field_cells: dict[str, list[str]] = Field(default_factory=dict)
    recalculated_engineering_quantity: float | None = None
    reported_engineering_quantity: float | None = None
    quantity_difference: float | None = None
    quantity_tolerance: float | None = None
    quantity_audit: Literal["MATCH", "MISMATCH", "NOT_CALCULABLE"]
    audit_issue_ids: list[str] = Field(default_factory=list)


class GoldImportSummary(_StrictModel):
    sheet_count: int = Field(ge=0)
    row_count: int = Field(ge=0)
    mt_distribution: dict[str, int]
    unit_distribution: dict[str, int]
    audit_issue_count: int = Field(ge=0)
    quantity_mismatch_count: int = Field(ge=0)
    quantity_material_mismatch_count: int = Field(ge=0)
    quantity_rounding_deviation_count: int = Field(ge=0)
    quantity_not_calculable_count: int = Field(ge=0)
    amount_mismatch_count: int = Field(ge=0)
    pass_count: int = Field(ge=0)
    review_count: int = Field(ge=0)
    block_count: int = Field(ge=0)


class GoldImportResult(_StrictModel):
    """Serializable import result, including row-level source evidence."""

    schema_version: str = "1.0"
    source_path: str
    source_sha256: str
    workbook_format: Literal["xls", "xlsx", "xlsm"]
    sheets: list[GoldSheetEvidence]
    rows: list[GoldRow]
    issues: list[GoldAuditIssue]
    summary: GoldImportSummary

    def to_json(self, *, indent: int | None = 2) -> str:
        return json.dumps(self.model_dump(mode="json"), ensure_ascii=False, indent=indent)

    def write_json(self, output: str | Path, *, indent: int | None = 2) -> Path:
        destination = Path(output)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(self.to_json(indent=indent), encoding="utf-8")
        return destination


class _Cell:
    def __init__(
        self,
        *,
        sheet_name: str,
        sheet_index: int,
        row: int,
        column: int,
        value: Any,
        formula: str | None,
        data_type: str,
        number_format: str | None,
    ) -> None:
        self.sheet_name = sheet_name
        self.sheet_index = sheet_index
        self.row = row
        self.column = column
        self.value = _json_scalar(value)
        self.formula = formula
        self.data_type = data_type
        self.number_format = number_format

    @property
    def coordinate(self) -> str:
        return f"{get_column_letter(self.column)}{self.row}"

    def evidence(self) -> GoldCellEvidence:
        return GoldCellEvidence(
            sheet_name=self.sheet_name,
            sheet_index=self.sheet_index,
            row=self.row,
            column=self.column,
            coordinate=self.coordinate,
            raw_value=self.value,
            value_repr=_value_repr(self.value),
            formula=self.formula,
            data_type=self.data_type,
            number_format=self.number_format,
        )


class _Sheet:
    def __init__(self, name: str, index: int, rows: list[list[_Cell]]) -> None:
        self.name = name
        self.index = index
        self.rows = rows

    @property
    def max_row(self) -> int:
        return len(self.rows)

    @property
    def max_column(self) -> int:
        return max((len(row) for row in self.rows), default=0)

    def cell(self, row: int, column: int) -> _Cell | None:
        if row < 1 or column < 1 or row > len(self.rows):
            return None
        cells = self.rows[row - 1]
        return cells[column - 1] if column <= len(cells) else None


def _json_scalar(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, (datetime, date, time)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return str(value)
    return str(value)


def _value_repr(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _read_xlsx(path: Path) -> list[_Sheet]:
    values_book = load_workbook(path, data_only=True, read_only=False)
    formulas_book = load_workbook(path, data_only=False, read_only=False)
    sheets: list[_Sheet] = []
    for index, value_ws in enumerate(values_book.worksheets):
        formula_ws = formulas_book[value_ws.title]
        rows: list[list[_Cell]] = []
        for row_index in range(1, value_ws.max_row + 1):
            row: list[_Cell] = []
            for column in range(1, value_ws.max_column + 1):
                value_cell = value_ws.cell(row_index, column)
                formula_cell = formula_ws.cell(row_index, column)
                formula = (
                    str(formula_cell.value)
                    if formula_cell.data_type == "f" and formula_cell.value is not None
                    else None
                )
                value = value_cell.value
                if value is None and formula is not None:
                    value = formula
                row.append(
                    _Cell(
                        sheet_name=value_ws.title,
                        sheet_index=index,
                        row=row_index,
                        column=column,
                        value=value,
                        formula=formula,
                        data_type=str(formula_cell.data_type or value_cell.data_type or "unknown"),
                        number_format=str(formula_cell.number_format or "General"),
                    )
                )
            rows.append(row)
        sheets.append(_Sheet(value_ws.title, index, rows))
    values_book.close()
    formulas_book.close()
    return sheets


def _read_xls(path: Path) -> list[_Sheet]:
    book = xlrd.open_workbook(path, formatting_info=True)
    sheets: list[_Sheet] = []
    for index, worksheet in enumerate(book.sheets()):
        rows: list[list[_Cell]] = []
        for row_index in range(worksheet.nrows):
            row: list[_Cell] = []
            for column in range(worksheet.ncols):
                source = worksheet.cell(row_index, column)
                number_format = None
                try:
                    xf = book.xf_list[source.xf_index]
                    number_format = book.format_map[xf.format_key].format_str
                except (AttributeError, IndexError, KeyError):
                    pass
                row.append(
                    _Cell(
                        sheet_name=worksheet.name,
                        sheet_index=index,
                        row=row_index + 1,
                        column=column + 1,
                        value=source.value if source.ctype != xlrd.XL_CELL_EMPTY else None,
                        formula=None,
                        data_type=_XLRD_CELL_TYPES.get(source.ctype, "unknown"),
                        number_format=number_format,
                    )
                )
            rows.append(row)
        sheets.append(_Sheet(worksheet.name, index, rows))
    return sheets


def _normalise_header(value: Any) -> str:
    text = _value_repr(value).strip().lower()
    return re.sub(r"[\s\n\r\t/／()（）:_：·-]+", "", text)


def _normalise_mt(value: Any) -> str | None:
    text = _value_repr(value).strip().upper().replace("_", "-")
    match = _MT_RE.search(text)
    if not match:
        return text or None
    suffix = f"-{int(match.group(2)):02d}" if match.group(2) else ""
    return f"MT-{int(match.group(1)):02d}{suffix}"


def _is_empty(value: Any) -> bool:
    return value is None or (isinstance(value, str) and not value.strip())


def _to_decimal(value: Any) -> Decimal | None:
    if _is_empty(value):
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float, Decimal)):
        try:
            return Decimal(str(value))
        except InvalidOperation:
            return None
    text = str(value).strip().replace(",", "").replace("，", "")
    text = re.sub(r"(?i)\s*(?:mm|m|㎡|m2|件|套)\s*$", "", text)
    try:
        return evaluate_numeric_expression(text)
    except ValueError:
        return None


def _to_float(value: Any) -> float | None:
    number = _to_decimal(value)
    return float(number) if number is not None else None


def _to_sequence(value: Any, fallback: int) -> int:
    number = _to_decimal(value)
    if number is None or number < 1:
        return fallback
    return int(number)


def _normalise_unit(value: Any, pricing_method: Any = None) -> str | None:
    text = _value_repr(value).strip().lower().replace("²", "2")
    aliases = {
        "m": "m",
        "米": "m",
        "延米": "m",
        "lm": "m",
        "㎡": "㎡",
        "m2": "㎡",
        "m^2": "㎡",
        "平方米": "㎡",
        "平米": "㎡",
        "件": "件",
        "套": "套",
    }
    return aliases.get(text) or infer_unit(_value_repr(pricing_method))


def _default_pricing_method(unit: str | None) -> str | None:
    return {"m": "按米", "㎡": "按展开面积", "件": "按件", "套": "按套"}.get(unit)


def _format_quantum(number_format: str | None) -> Decimal:
    """Return half of the visible cell quantum for comparison tolerance."""
    if not number_format or number_format.lower() == "general":
        return Decimal("0.0000005")
    section = number_format.split(";", 1)[0]
    match = _NUMBER_FORMAT_DECIMALS.search(section)
    decimals = len(match.group(1)) if match else 0
    return Decimal(1).scaleb(-decimals) / Decimal(2)


def _formula_quantity(
    *,
    unit: str | None,
    width: Decimal | None,
    length: Decimal | None,
    quantity: Decimal | None,
    unfolded_spec: Any,
) -> tuple[Decimal | None, list[str]]:
    missing: list[str] = []
    if quantity is None:
        missing.append("数量")
    if unit == "m":
        if length is None:
            missing.append("长度")
        if missing:
            return None, missing
        return length * quantity / Decimal("1000"), []  # type: ignore[operator]
    if unit == "㎡":
        if width is None:
            width = infer_unfolded_width(_value_repr(unfolded_spec))
        if width is None:
            missing.append("宽/展开规格")
        if length is None:
            missing.append("长度")
        if missing:
            return None, missing
        return width * length * quantity / Decimal("1000000"), []  # type: ignore[operator]
    if unit in ("件", "套"):
        return (quantity, []) if quantity is not None else (None, missing)
    return None, ["单位"]


def _header_mapping(sheet: _Sheet) -> tuple[list[int], dict[str, int]] | None:
    best: tuple[int, int, int, dict[str, int]] | None = None
    for row_number in range(1, min(sheet.max_row, 30) + 1):
        row = sheet.rows[row_number - 1]
        normalised = [_normalise_header(cell.value) for cell in row]
        color_quantity_layout = "颜色" in normalised and (
            "规格" in normalised
            or any(
                _normalise_header(cell.value) in {"展开宽", "展开宽度", "件数"}
                for cell in (sheet.rows[row_number] if row_number < sheet.max_row else [])
            )
        )
        mapping: dict[str, int] = {}
        recognised = 0
        for column, header in enumerate(normalised, start=1):
            if not header:
                continue
            if color_quantity_layout and header == "颜色":
                field = "mt_code"
            elif color_quantity_layout and header == "数量":
                field = "engineering_quantity"
            else:
                field = _HEADER_ALIASES.get(header)
            if field and field not in mapping:
                mapping[field] = column
                recognised += 1

        depth = 1
        if row_number < sheet.max_row:
            sub_mapping: dict[str, int] = {}
            for column, cell in enumerate(sheet.rows[row_number], start=1):
                header = _normalise_header(cell.value)
                field = _HEADER_ALIASES.get(header)
                if field and header in {"展开宽", "展开宽度", "长", "长度", "件数"}:
                    sub_mapping[field] = column
            if len(sub_mapping) >= 2:
                mapping.update(sub_mapping)
                recognised += len(sub_mapping)
                depth = 2

        required_score = sum(field in mapping for field in ("name", "mt_code", "sequence"))
        score = recognised + required_score * 3
        candidate = (score, row_number, depth, mapping)
        if best is None or candidate[0] > best[0]:
            best = candidate
    if best is None or best[0] < 8:
        return None
    _, row_number, depth, mapping = best
    return list(range(row_number, row_number + depth)), mapping


def _looks_like_data(values: dict[str, Any]) -> bool:
    sequence = _to_decimal(values.get("sequence"))
    mt_code = _normalise_mt(values.get("mt_code"))
    name = _value_repr(values.get("name")).strip()
    if sequence is not None and sequence >= 1 and (mt_code or name):
        return True
    if mt_code and _MT_RE.search(mt_code) and name:
        return True
    return False


def _issue_id(source_sha256: str, sheet: str, row: int, code: str) -> str:
    payload = f"{source_sha256}|{sheet}|{row}|{code}".encode()
    return f"gold-issue:{hashlib.sha256(payload).hexdigest()[:20]}"


def _row_id(source_sha256: str, sheet: str, row: int) -> str:
    payload = f"{source_sha256}|{sheet}|{row}".encode()
    return f"gold-row:{hashlib.sha256(payload).hexdigest()[:20]}"


def _field_value(sheet: _Sheet, row: int, mapping: dict[str, int], field: str) -> Any:
    column = mapping.get(field)
    cell = sheet.cell(row, column) if column is not None else None
    return cell.value if cell else None


def import_gold_workbook(path: str | Path) -> GoldImportResult:
    """Import an ``.xls``/``.xlsx`` quote and audit every detected data row."""
    source = Path(path).expanduser().resolve()
    if not source.is_file():
        raise FileNotFoundError(source)
    suffix = source.suffix.lower()
    if suffix == ".xls":
        workbook_format: Literal["xls", "xlsx", "xlsm"] = "xls"
        source_sheets = _read_xls(source)
    elif suffix in {".xlsx", ".xlsm"}:
        workbook_format = "xlsm" if suffix == ".xlsm" else "xlsx"
        source_sheets = _read_xlsx(source)
    else:
        raise ValueError(f"unsupported gold workbook format: {source.suffix}")

    source_hash = _sha256(source)
    sheets: list[GoldSheetEvidence] = []
    rows: list[GoldRow] = []
    issues: list[GoldAuditIssue] = []
    fallback_sequence = 1

    for sheet in source_sheets:
        detected = _header_mapping(sheet)
        if detected is None:
            sheets.append(
                GoldSheetEvidence(
                    sheet_name=sheet.name,
                    sheet_index=sheet.index,
                    max_row=sheet.max_row,
                    max_column=sheet.max_column,
                )
            )
            continue
        header_rows, mapping = detected
        header_cells = [
            cell.evidence()
            for row_number in header_rows
            for cell in sheet.rows[row_number - 1]
        ]
        imported_before = len(rows)
        for row_number in range(header_rows[-1] + 1, sheet.max_row + 1):
            values = {
                field: _field_value(sheet, row_number, mapping, field)
                for field in CANONICAL_FIELDS
            }
            if not _looks_like_data(values):
                continue

            raw_cells = [cell.evidence() for cell in sheet.rows[row_number - 1]]
            field_cells = {
                field: [sheet.cell(row_number, column).coordinate]  # type: ignore[union-attr]
                for field, column in mapping.items()
                if sheet.cell(row_number, column) is not None
            }
            mt_code = _normalise_mt(values["mt_code"]) or ""
            name = _value_repr(values["name"]).strip()
            sequence = _to_sequence(values["sequence"], fallback_sequence)
            fallback_sequence = max(fallback_sequence + 1, sequence + 1)
            unit = _normalise_unit(values["unit"], values["pricing_method"])
            pricing_method = (
                _value_repr(values["pricing_method"]).strip()
                or _default_pricing_method(unit)
            )
            width = _to_decimal(values["width_mm"])
            length = _to_decimal(values["length_mm"])
            quantity = _to_decimal(values["quantity"])
            reported = _to_decimal(values["engineering_quantity"])
            recalculated, missing = _formula_quantity(
                unit=unit,
                width=width,
                length=length,
                quantity=quantity,
                unfolded_spec=values["unfolded_spec"],
            )
            issue_ids: list[str] = []
            row_status = ReviewStatus.REVIEW
            quantity_audit: Literal["MATCH", "MISMATCH", "NOT_CALCULABLE"] = "MATCH"
            difference: Decimal | None = None
            tolerance: Decimal | None = None

            missing_required = [
                label
                for label, value in (("名称", name), ("MT编号", mt_code))
                if not value
            ]
            if missing_required:
                code = "MISSING_REQUIRED_FIELD"
                issue = GoldAuditIssue(
                    id=_issue_id(source_hash, sheet.name, row_number, code),
                    code=code,
                    severity="BLOCK",
                    message=f"缺少必填字段：{'、'.join(missing_required)}",
                    sheet_name=sheet.name,
                    row=row_number,
                    field="/".join(missing_required),
                    evidence_cells=sum(
                        (field_cells.get(field, []) for field in ("name", "mt_code")), []
                    ),
                )
                issues.append(issue)
                issue_ids.append(issue.id)
                row_status = ReviewStatus.BLOCK

            if recalculated is None:
                quantity_audit = "NOT_CALCULABLE"
                code = "QUANTITY_NOT_CALCULABLE"
                issue = GoldAuditIssue(
                    id=_issue_id(source_hash, sheet.name, row_number, code),
                    code=code,
                    severity="BLOCK",
                    message=f"无法复算工程量，缺少或不支持：{'、'.join(missing)}",
                    sheet_name=sheet.name,
                    row=row_number,
                    field="engineering_quantity",
                    actual=str(reported) if reported is not None else None,
                    evidence_cells=sum(
                        (
                            field_cells.get(field, [])
                            for field in ("width_mm", "length_mm", "quantity", "unit")
                        ),
                        [],
                    ),
                )
                issues.append(issue)
                issue_ids.append(issue.id)
                row_status = ReviewStatus.BLOCK
            elif reported is None:
                quantity_audit = "NOT_CALCULABLE"
                code = "QUANTITY_NOT_CALCULABLE"
                issue = GoldAuditIssue(
                    id=_issue_id(source_hash, sheet.name, row_number, code),
                    code=code,
                    severity="BLOCK",
                    message="原表缺少工程量，无法与复算值核对",
                    sheet_name=sheet.name,
                    row=row_number,
                    field="engineering_quantity",
                    expected=str(recalculated),
                    evidence_cells=field_cells.get("engineering_quantity", []),
                )
                issues.append(issue)
                issue_ids.append(issue.id)
                row_status = ReviewStatus.BLOCK
            else:
                engineering_cell = sheet.cell(
                    row_number, mapping.get("engineering_quantity", 0)
                )
                tolerance = _format_quantum(
                    engineering_cell.number_format if engineering_cell else None
                )
                difference = reported - recalculated
                if abs(difference) > tolerance:
                    quantity_audit = "MISMATCH"
                    material_threshold = max(
                        tolerance * Decimal(2), abs(recalculated) * Decimal("0.01")
                    )
                    material = abs(difference) > material_threshold
                    code = (
                        "QUANTITY_FORMULA_MISMATCH"
                        if material
                        else "QUANTITY_ROUNDING_DEVIATION"
                    )
                    issue = GoldAuditIssue(
                        id=_issue_id(source_hash, sheet.name, row_number, code),
                        code=code,
                        severity="BLOCK" if material else "REVIEW",
                        message=(
                            "原表工程量与计价公式复算值存在实质偏差"
                            if material
                            else "原表工程量超出单元格显示精度允许的半单位舍入差"
                        ),
                        sheet_name=sheet.name,
                        row=row_number,
                        field="engineering_quantity",
                        actual=str(reported),
                        expected=str(recalculated),
                        difference=str(difference),
                        tolerance=str(tolerance),
                        evidence_cells=sum(
                            (
                                field_cells.get(field, [])
                                for field in (
                                    "width_mm",
                                    "length_mm",
                                    "quantity",
                                    "engineering_quantity",
                                    "unit",
                                )
                            ),
                            [],
                        ),
                    )
                    issues.append(issue)
                    issue_ids.append(issue.id)
                    if material:
                        row_status = ReviewStatus.BLOCK

            unit_price = _to_decimal(values["unit_price"])
            amount = _to_decimal(values["amount"])
            if unit_price is not None and reported is not None and amount is not None:
                expected_amount = unit_price * reported
                amount_difference = amount - expected_amount
                if abs(amount_difference) > Decimal("0.005"):
                    code = "AMOUNT_FORMULA_MISMATCH"
                    issue = GoldAuditIssue(
                        id=_issue_id(source_hash, sheet.name, row_number, code),
                        code=code,
                        severity="BLOCK",
                        message="原表金额与工程量×单价不一致",
                        sheet_name=sheet.name,
                        row=row_number,
                        field="amount",
                        actual=str(amount),
                        expected=str(expected_amount),
                        difference=str(amount_difference),
                        tolerance="0.005",
                        evidence_cells=sum(
                            (field_cells.get(field, []) for field in (
                                "engineering_quantity", "unit_price", "amount"
                            )),
                            [],
                        ),
                    )
                    issues.append(issue)
                    issue_ids.append(issue.id)
                    row_status = ReviewStatus.BLOCK

            item = TakeoffItem(
                sequence=sequence,
                name=name or "待确认名称",
                mt_code=mt_code or "待确认MT",
                material=_value_repr(values["material"]).strip() or None,
                plan_location=_value_repr(values["plan_location"]).strip() or None,
                elevation=_value_repr(values["elevation"]).strip() or None,
                detail=_value_repr(values["detail"]).strip() or None,
                unfolded_spec=_value_repr(values["unfolded_spec"]).strip() or None,
                width_mm=float(width) if width is not None else None,
                length_mm=float(length) if length is not None else None,
                quantity=float(quantity) if quantity is not None else None,
                engineering_quantity=float(reported) if reported is not None else None,
                unit=unit,  # type: ignore[arg-type]
                pricing_method=pricing_method or None,
                unit_price=float(unit_price) if unit_price is not None else None,
                amount=float(amount) if amount is not None else None,
                note=_value_repr(values["note"]).strip() or None,
                evidence_ids=[
                    f"{sheet.name}!{cell.coordinate}"
                    for cell in raw_cells
                    if not _is_empty(cell.raw_value)
                ],
                status=row_status,
                block_reason=(
                    "人工金标准导入审计发现阻断项"
                    if row_status == ReviewStatus.BLOCK
                    else None
                ),
            )
            rows.append(
                GoldRow(
                    id=_row_id(source_hash, sheet.name, row_number),
                    sheet_name=sheet.name,
                    sheet_index=sheet.index,
                    row=row_number,
                    item=item,
                    raw_cells=raw_cells,
                    field_cells=field_cells,
                    recalculated_engineering_quantity=(
                        float(recalculated) if recalculated is not None else None
                    ),
                    reported_engineering_quantity=(
                        float(reported) if reported is not None else None
                    ),
                    quantity_difference=float(difference) if difference is not None else None,
                    quantity_tolerance=float(tolerance) if tolerance is not None else None,
                    quantity_audit=quantity_audit,
                    audit_issue_ids=issue_ids,
                )
            )

        sheets.append(
            GoldSheetEvidence(
                sheet_name=sheet.name,
                sheet_index=sheet.index,
                max_row=sheet.max_row,
                max_column=sheet.max_column,
                header_rows=header_rows,
                header_cells=header_cells,
                field_columns=mapping,
                imported_row_count=len(rows) - imported_before,
            )
        )

    issue_counts = Counter(issue.code for issue in issues)
    status_counts = Counter(row.item.status.value for row in rows)
    summary = GoldImportSummary(
        sheet_count=sum(sheet.imported_row_count > 0 for sheet in sheets),
        row_count=len(rows),
        mt_distribution=dict(sorted(Counter(row.item.mt_code for row in rows).items())),
        unit_distribution=dict(
            sorted(Counter(row.item.unit or "未识别" for row in rows).items())
        ),
        audit_issue_count=len(issues),
        quantity_mismatch_count=(
            issue_counts["QUANTITY_FORMULA_MISMATCH"]
            + issue_counts["QUANTITY_ROUNDING_DEVIATION"]
        ),
        quantity_material_mismatch_count=issue_counts["QUANTITY_FORMULA_MISMATCH"],
        quantity_rounding_deviation_count=issue_counts["QUANTITY_ROUNDING_DEVIATION"],
        quantity_not_calculable_count=issue_counts["QUANTITY_NOT_CALCULABLE"],
        amount_mismatch_count=issue_counts["AMOUNT_FORMULA_MISMATCH"],
        pass_count=status_counts[ReviewStatus.PASS.value],
        review_count=status_counts[ReviewStatus.REVIEW.value],
        block_count=status_counts[ReviewStatus.BLOCK.value],
    )
    return GoldImportResult(
        source_path=str(source),
        source_sha256=source_hash,
        workbook_format=workbook_format,
        sheets=sheets,
        rows=rows,
        issues=issues,
        summary=summary,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="导入并审计人工报价清单金标准")
    parser.add_argument("workbook", type=Path)
    parser.add_argument("--output", "-o", type=Path)
    args = parser.parse_args(argv)
    result = import_gold_workbook(args.workbook)
    if args.output:
        result.write_json(args.output)
    else:
        print(result.to_json())
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
