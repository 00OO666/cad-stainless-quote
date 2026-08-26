"""Material-table parsing and MT code normalization.

The source workbooks used by interior-design teams are rarely tidy databases.  A
material may occupy one row in a conventional table, or a small key/value block
(``材料编号`` on one row, ``材料名称`` on the next).  This module accepts either
shape and keeps every parsed value traceable to its source row.

Parsing is deliberately conservative.  A missing value stays missing and a
conflict is recorded; neither condition is silently repaired from another
project.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import unicodedata
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path
from typing import Any

from .models import CadEntity, MaterialSpec, ReviewStatus

_MT_RE = re.compile(r"(?<![A-Z0-9])M\s*T\s*[-—–－_:/／\\]?\s*(\d{1,3})(?!\d)", re.I)
_MT_CELL_RE = re.compile(r"^\s*M\s*T\s*[-—–－_:/／\\]?\s*\d{1,3}\s*$", re.I)
_MT_ONLY_RE = re.compile(r"^\s*M\s*T\s*[-—–－_:/／\\]?\s*$", re.I)
_NUMBER_ONLY_RE = re.compile(r"^\s*0*(\d{1,3})\s*$")
_THICKNESS_RE = re.compile(
    r"(?<!\d)(\d+(?:\.\d+)?)\s*"
    r"(?:(?:mm|毫米)\s*(?:厚|thk|t)?|(?:厚|thk))(?![A-Z0-9])",
    re.I,
)
_GRADE_RE = re.compile(r"(?<!\d)(201|304L?|316L?|430)(?!\d)", re.I)
_FINISH_TERMS = (
    "黑色镜面",
    "镜面黑色",
    "青古铜",
    "红古铜",
    "古铜色",
    "苹果砂",
    "玫瑰金",
    "镜面",
    "拉丝",
    "喷砂",
    "烤漆",
    "镀色",
)
_PROCESS_TERMS = ("折弯", "刨槽", "雕花", "蚀刻", "满焊", "点焊", "焊接", "激光")
_CAD_MATERIAL_RE = re.compile(
    r"不锈钢|钢板|金属|铝板|铝合金|铜板|铁板|钛金|玫瑰金|镜面|拉丝|喷砂|"
    r"苹果砂|古铜|烤漆|镀色|雕花|蚀刻"
)

_FIELD_ALIASES: dict[str, tuple[str, ...]] = {
    "mt_code": ("MT编号", "材料编号", "物料编号", "色号", "编号", "NUMBER", "MATERIALNO"),
    "name": ("材料名称", "物料名称", "名称", "品名", "NAME", "DESCRIPTION"),
    "grade": ("材质", "牌号", "GRADE", "MATERIAL"),
    "thickness_mm": ("厚度", "材料规格", "物料规格", "规格", "SIZE", "THICKNESS", "THK"),
    "finish": ("表面处理", "饰面", "表面", "颜色", "FINISH", "COLOR", "COLOUR"),
    "process": ("加工方式", "加工工艺", "工艺", "PROCESS", "CRAFT"),
    "brand": ("材料品牌", "物料品牌", "品牌", "BRAND"),
    "model": ("材料型号", "物料型号", "型号", "MODEL"),
}


def normalize_text(value: Any) -> str:
    """Return stable human text for heterogeneous spreadsheet cells."""

    if value is None:
        return ""
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    if isinstance(value, float):
        if not math.isfinite(value):
            return ""
        if value.is_integer():
            value = int(value)
    text = unicodedata.normalize("NFKC", str(value))
    text = text.replace("\u00a0", " ").replace("\u3000", " ")
    return re.sub(r"\s+", " ", text).strip()


def normalize_mt_code(value: Any) -> str | None:
    """Normalize ``MT01``/``ＭＴ－０１``/``MT 1`` to ``MT-01``.

    Only an explicit MT prefix is accepted here.  Detached ``MT`` + number is
    handled by :func:`find_mt_codes_in_cells`, where cell proximity is known.
    """

    match = _MT_RE.search(normalize_text(value).upper())
    if not match:
        return None
    number = int(match.group(1))
    width = max(2, len(match.group(1)))
    return f"MT-{number:0{width}d}"


def find_mt_codes(value: Any) -> list[str]:
    """Find all explicit MT codes in a string, preserving first-seen order."""

    text = normalize_text(value).upper()
    result: list[str] = []
    for match in _MT_RE.finditer(text):
        width = max(2, len(match.group(1)))
        code = f"MT-{int(match.group(1)):0{width}d}"
        if code not in result:
            result.append(code)
    return result


def find_mt_codes_in_cells(cells: Sequence[Any]) -> list[tuple[str, tuple[int, ...]]]:
    """Find explicit and adjacent-cell detached MT codes.

    The returned tuple contains the code and contributing cell indexes.  A
    detached pair is intentionally limited to immediately adjacent non-empty
    cells, preventing an unrelated quantity elsewhere in the row becoming a
    material code.
    """

    texts = [normalize_text(value) for value in cells]
    found: list[tuple[str, tuple[int, ...]]] = []
    for index, text in enumerate(texts):
        for code in find_mt_codes(text):
            found.append((code, (index,)))
        if not _MT_ONLY_RE.fullmatch(text):
            continue
        for neighbour in (index + 1, index - 1):
            if neighbour < 0 or neighbour >= len(texts):
                continue
            match = _NUMBER_ONLY_RE.fullmatch(texts[neighbour])
            if match:
                width = max(2, len(match.group(1)))
                found.append((f"MT-{int(match.group(1)):0{width}d}", (index, neighbour)))
                break

    unique: list[tuple[str, tuple[int, ...]]] = []
    seen: set[tuple[str, tuple[int, ...]]] = set()
    for item in found:
        if item not in seen:
            seen.add(item)
            unique.append(item)
    return unique


def _compact_label(value: Any) -> str:
    text = normalize_text(value).upper()
    text = re.sub(r"[\s:：()（）\[\]【】._/\\-]+", "", text)
    return text


def _field_for_label(value: Any) -> str | None:
    label = _compact_label(value)
    if not label:
        return None
    for field, aliases in _FIELD_ALIASES.items():
        for alias in aliases:
            compact = _compact_label(alias)
            ascii_alias = compact.isascii()
            if (
                label == compact
                or label.startswith(compact)
                or (ascii_alias and label.endswith(compact))
                or (ascii_alias and len(compact) >= 4 and compact in label)
            ):
                return field
    return None


def _clean_value(value: Any) -> str | None:
    text = normalize_text(value)
    # Some legacy BIFF files advertise UTF-16 while containing text that xlrd
    # can only decode as U+FFFD.  Treat replacement-heavy output as missing
    # evidence instead of exporting mojibake as a material name.
    if text.count("\ufffd") >= 2 and text.count("\ufffd") / max(1, len(text)) >= 0.15:
        return None
    return text or None


def _parse_thickness(*values: Any) -> float | None:
    for value in values:
        text = normalize_text(value)
        match = _THICKNESS_RE.search(text)
        if match:
            thickness = float(match.group(1))
            if 0 < thickness <= 20:
                return thickness
        # In a column explicitly named 厚度, bare numeric cells are common.
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            thickness = float(value)
            if math.isfinite(thickness) and 0 < thickness <= 20:
                return thickness
    return None


def _parse_grade(*values: Any) -> str | None:
    for value in values:
        match = _GRADE_RE.search(normalize_text(value).upper())
        if match:
            return match.group(1).upper()
    return None


def _first_term(values: Sequence[Any], terms: Sequence[str]) -> str | None:
    for value in values:
        text = normalize_text(value)
        for term in terms:
            if term in text:
                return term
    return None


def _stable_id(prefix: str, payload: Mapping[str, Any]) -> str:
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    digest = hashlib.sha256(encoded.encode("utf-8")).hexdigest()[:24]
    return f"{prefix}:{digest}"


def _row_values(row: Mapping[str, Any] | Sequence[Any]) -> list[Any]:
    if isinstance(row, Mapping):
        return list(row.values())
    if isinstance(row, (str, bytes)):
        return [row]
    return list(row)


def _mapping_record(row: Mapping[str, Any]) -> dict[str, Any]:
    record: dict[str, Any] = {}
    for key, value in row.items():
        field = _field_for_label(key)
        if field and _clean_value(value) is not None:
            record[field] = value
    return record


def _header_map(cells: Sequence[Any]) -> dict[int, str]:
    result: dict[int, str] = {}
    for index, value in enumerate(cells):
        field = _field_for_label(value)
        if field:
            result[index] = field
    return result


def _extract_pairs(rows: Sequence[Sequence[Any]]) -> dict[str, Any]:
    """Extract repeated label/value pairs from a material-card row block."""

    record: dict[str, Any] = {}
    for row in rows:
        cells = list(row)
        for index, cell in enumerate(cells):
            field = _field_for_label(cell)
            if not field:
                continue
            for candidate in cells[index + 1 :]:
                if _field_for_label(candidate):
                    break
                if _clean_value(candidate) is not None:
                    record.setdefault(field, candidate)
                    break
    return record


def _make_spec(
    raw: Mapping[str, Any],
    *,
    code: str,
    source_file_id: str | None,
    source_location: str,
) -> MaterialSpec:
    name = _clean_value(raw.get("name"))
    raw_grade = _clean_value(raw.get("grade"))
    grade = _parse_grade(raw_grade, raw.get("name"), raw.get("thickness_mm")) or raw_grade
    thickness = _parse_thickness(raw.get("thickness_mm"), raw.get("name"))
    finish = _clean_value(raw.get("finish")) or _first_term(
        (raw.get("name"),), _FINISH_TERMS
    )
    process = _clean_value(raw.get("process")) or _first_term(
        (raw.get("name"),), _PROCESS_TERMS
    )
    brand = _clean_value(raw.get("brand"))
    model = _clean_value(raw.get("model"))
    payload = {
        "mt_code": code,
        "name": name,
        "grade": grade,
        "thickness_mm": thickness,
        "finish": finish,
        "process": process,
        "brand": brand,
        "model": model,
        "source_file_id": source_file_id,
        "source_location": source_location,
    }
    return MaterialSpec(
        id=_stable_id("material", payload),
        **payload,
        status=ReviewStatus.REVIEW,
    )


def _choose_primary_code(cells: Sequence[Any], codes: Sequence[tuple[str, tuple[int, ...]]]) -> str:
    """Prefer the code adjacent to a NUMBER/编号 label over cross references."""

    for code, indexes in codes:
        first = min(indexes)
        for label_index in range(max(0, first - 2), first):
            if _field_for_label(cells[label_index]) == "mt_code":
                return code
    return codes[0][0]


def _annotate_conflicts(specs: list[MaterialSpec]) -> list[MaterialSpec]:
    by_code: dict[str, list[MaterialSpec]] = {}
    for spec in specs:
        by_code.setdefault(spec.mt_code, []).append(spec)

    fields = ("name", "grade", "thickness_mm", "finish", "process", "brand", "model")
    output: list[MaterialSpec] = []
    for spec in specs:
        conflicts: list[str] = list(spec.conflicts)
        peers = by_code[spec.mt_code]
        for field in fields:
            values = {
                normalize_text(getattr(peer, field)).casefold()
                for peer in peers
                if getattr(peer, field) is not None
            }
            if len(values) > 1:
                rendered = sorted(
                    {
                        normalize_text(getattr(peer, field))
                        for peer in peers
                        if getattr(peer, field) is not None
                    }
                )
                conflicts.append(f"{field}: {' | '.join(rendered)}")
        output.append(spec.model_copy(update={"conflicts": sorted(set(conflicts))}))
    return output


def annotate_material_conflicts(specs: Iterable[MaterialSpec]) -> list[MaterialSpec]:
    """Annotate conflicts across workbook and CAD-derived material records."""

    unique = {spec.id: spec for spec in specs}
    return _annotate_conflicts(list(unique.values()))


def _entity_center(entity: CadEntity) -> tuple[float, float] | None:
    if entity.insert is not None:
        return float(entity.insert[0]), float(entity.insert[1])
    if entity.bbox is not None:
        return (
            float(entity.bbox[0] + entity.bbox[2]) / 2.0,
            float(entity.bbox[1] + entity.bbox[3]) / 2.0,
        )
    return None


def parse_cad_material_specs(entities: Iterable[CadEntity]) -> list[MaterialSpec]:
    """Recover conservative MT/material rows from native CAD text tables.

    A material table usually places an MT code and its description on the same
    horizontal baseline. This parser deliberately requires that row evidence;
    nearest-text distance alone is not enough because adjacent table rows often
    contain different MT definitions.
    """

    grouped: dict[tuple[str, str | None, str], list[CadEntity]] = {}
    for entity in entities:
        if not entity.text or _entity_center(entity) is None:
            continue
        key = (entity.source_file_id, entity.sheet_id, entity.space)
        grouped.setdefault(key, []).append(entity)

    specs: list[MaterialSpec] = []
    seen_original_seeds: set[tuple[str, str]] = set()
    for (source_id, sheet_id, _space), values in sorted(grouped.items()):
        heights = [
            abs(entity.bbox[3] - entity.bbox[1])
            for entity in values
            if entity.bbox and abs(entity.bbox[3] - entity.bbox[1]) > 0
        ]
        typical_height = sorted(heights)[len(heights) // 2] if heights else 1.0
        for seed in sorted(values, key=lambda value: value.id):
            codes = find_mt_codes(seed.text)
            if len(codes) != 1:
                continue
            seed_identity = str(seed.geometry.get("original_entity_id") or seed.id)
            identity = (source_id, seed_identity)
            if identity in seen_original_seeds:
                continue
            seed_text = normalize_text(seed.text)
            seed_point = _entity_center(seed)
            assert seed_point is not None
            seed_height = (
                abs(seed.bbox[3] - seed.bbox[1])
                if seed.bbox and abs(seed.bbox[3] - seed.bbox[1]) > 0
                else typical_height
            )
            row_tolerance = max(0.5, min(typical_height * 2.0, seed_height * 2.5))
            row_values: list[tuple[float, float, str, CadEntity]] = []
            for candidate in values:
                if candidate.id == seed.id or not candidate.text:
                    continue
                text = normalize_text(candidate.text)
                if not text or find_mt_codes(text):
                    continue
                point = _entity_center(candidate)
                if point is None:
                    continue
                dy = abs(point[1] - seed_point[1])
                if dy > row_tolerance:
                    continue
                if not _CAD_MATERIAL_RE.search(text):
                    continue
                dx = point[0] - seed_point[0]
                # Table descriptions normally sit to the right. A left-side
                # value is accepted only as a weaker fallback.
                direction_penalty = 0.0 if dx >= 0 else abs(dx) * 0.35
                row_values.append((abs(dx) + direction_penalty, dy, candidate.id, candidate))

            direct_description = (
                seed_text if _CAD_MATERIAL_RE.search(seed_text) else None
            )
            if not row_values and direct_description is None:
                continue
            descriptor = min(row_values)[3] if row_values else seed
            name = direct_description or normalize_text(descriptor.text)
            # Do not borrow finish/process terms from another material phrase
            # merely because two independent tables happen to share a baseline.
            combined = " | ".join(dict.fromkeys([seed_text, name]))
            raw = {
                "name": name,
                "grade": _parse_grade(combined),
                "thickness_mm": _parse_thickness(combined),
                "finish": _first_term((combined,), _FINISH_TERMS),
                "process": _first_term((combined,), _PROCESS_TERMS),
            }
            entity_ids = [seed.id]
            if descriptor.id != seed.id:
                entity_ids.append(descriptor.id)
            specs.append(
                _make_spec(
                    raw,
                    code=codes[0],
                    source_file_id=source_id,
                    source_location=(
                        f"cad:{sheet_id or '<unassigned>'}:entities="
                        + ",".join(sorted(entity_ids))
                    ),
                )
            )
            seen_original_seeds.add(identity)
    return annotate_material_conflicts(specs)


def parse_material_rows(
    rows: Iterable[Mapping[str, Any] | Sequence[Any]],
    *,
    source_file_id: str | None = None,
    sheet_name: str = "Sheet1",
    start_row: int = 1,
) -> list[MaterialSpec]:
    """Parse a conventional table or material-card blocks into records.

    ``rows`` may be dictionaries, tuples from ``openpyxl``/``xlrd``, or text
    cells copied from a PDF table.  Row numbers in ``source_location`` remain
    one-based and deterministic.
    """

    materialized = list(rows)
    if not materialized:
        return []

    specs: list[MaterialSpec] = []

    # Dictionary rows already carry their own headers.
    if all(isinstance(row, Mapping) for row in materialized):
        for offset, row in enumerate(materialized):
            assert isinstance(row, Mapping)
            raw = _mapping_record(row)
            codes = find_mt_codes_in_cells(list(row.values()))
            explicit = normalize_mt_code(raw.get("mt_code"))
            code = explicit or (codes[0][0] if codes else None)
            if code:
                location = f"{sheet_name}!row={start_row + offset}"
                specs.append(
                    _make_spec(
                        raw,
                        code=code,
                        source_file_id=source_file_id,
                        source_location=location,
                    )
                )
        return _annotate_conflicts(specs)

    table = [_row_values(row) for row in materialized]

    # Parse conventional tabular sections.  A header is credible only when it
    # contains an MT/code column and at least one descriptive field.
    consumed_rows: set[int] = set()
    active_header: dict[int, str] | None = None
    for offset, cells in enumerate(table):
        candidate = _header_map(cells)
        nonempty_count = sum(bool(normalize_text(cell)) for cell in cells)
        is_header_row = (
            "mt_code" in candidate.values()
            and len(set(candidate.values())) >= 2
            and len(candidate) / max(1, nonempty_count) >= 0.75
        )
        if is_header_row:
            active_header = candidate
            consumed_rows.add(offset)
            continue
        if not active_header:
            continue
        raw = {
            field: cells[index]
            for index, field in active_header.items()
            if index < len(cells) and _clean_value(cells[index]) is not None
        }
        code = normalize_mt_code(raw.get("mt_code"))
        if code:
            location = f"{sheet_name}!row={start_row + offset}"
            specs.append(
                _make_spec(raw, code=code, source_file_id=source_file_id, source_location=location)
            )
            consumed_rows.add(offset)
        elif any(_field_for_label(cell) for cell in cells):
            active_header = None

    # Parse key/value material cards.  Each primary code row starts a block
    # ending immediately before the next primary code row.
    starts: list[tuple[int, str]] = []
    for offset, cells in enumerate(table):
        if offset in consumed_rows:
            continue
        codes = find_mt_codes_in_cells(cells)
        if not codes:
            continue
        primary = _choose_primary_code(cells, codes)
        # Cross references inside a free-text description must not start a card.
        has_number_label = any(_field_for_label(cell) == "mt_code" for cell in cells)
        has_code_cell = any(_MT_CELL_RE.fullmatch(normalize_text(cell)) for cell in cells)
        if has_number_label or has_code_cell:
            starts.append((offset, primary))

    for index, (offset, code) in enumerate(starts):
        end = starts[index + 1][0] if index + 1 < len(starts) else len(table)
        block = table[offset:end]
        raw = _extract_pairs(block)
        # A compact row can contain code + free description without labels.
        if not raw.get("name"):
            for cell in block[0]:
                text = normalize_text(cell)
                if text and not find_mt_codes(text) and _field_for_label(text) is None:
                    if re.search(r"不锈钢|金属|铝板|钢板|铜|铁", text):
                        raw["name"] = cell
                        break
        location = f"{sheet_name}!rows={start_row + offset}:{start_row + end - 1}"
        specs.append(
            _make_spec(raw, code=code, source_file_id=source_file_id, source_location=location)
        )

    # Remove only exact duplicate records.  Separate conflicting source rows are
    # retained so that conflict annotation remains auditable.
    unique: dict[str, MaterialSpec] = {}
    for spec in specs:
        unique.setdefault(spec.id, spec)
    return _annotate_conflicts(list(unique.values()))


def load_material_specs(
    path: str | Path,
    *,
    source_file_id: str | None = None,
    sheet_names: Iterable[str] | None = None,
) -> list[MaterialSpec]:
    """Read ``.xlsx`` or BIFF ``.xls`` and parse all selected worksheets."""

    workbook_path = Path(path)
    selected = set(sheet_names or [])
    specs: list[MaterialSpec] = []
    suffix = workbook_path.suffix.lower()

    if suffix in {".xlsx", ".xlsm"}:
        from openpyxl import load_workbook

        workbook = load_workbook(workbook_path, read_only=True, data_only=True)
        try:
            for worksheet in workbook.worksheets:
                if selected and worksheet.title not in selected:
                    continue
                specs.extend(
                    parse_material_rows(
                        worksheet.iter_rows(values_only=True),
                        source_file_id=source_file_id,
                        sheet_name=worksheet.title,
                    )
                )
        finally:
            workbook.close()
    elif suffix == ".xls":
        import xlrd

        workbook = xlrd.open_workbook(workbook_path, on_demand=True)
        try:
            for worksheet in workbook.sheets():
                if selected and worksheet.name not in selected:
                    continue
                rows = (
                    [worksheet.cell_value(row, column) for column in range(worksheet.ncols)]
                    for row in range(worksheet.nrows)
                )
                specs.extend(
                    parse_material_rows(
                        rows,
                        source_file_id=source_file_id,
                        sheet_name=worksheet.name,
                    )
                )
        finally:
            workbook.release_resources()
    else:
        raise ValueError(f"unsupported material workbook: {workbook_path.suffix or '<none>'}")

    return _annotate_conflicts(specs)


__all__ = [
    "annotate_material_conflicts",
    "find_mt_codes",
    "find_mt_codes_in_cells",
    "load_material_specs",
    "normalize_mt_code",
    "normalize_text",
    "parse_cad_material_specs",
    "parse_material_rows",
]
