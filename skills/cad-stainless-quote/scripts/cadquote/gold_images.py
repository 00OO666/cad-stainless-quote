"""Read-only export of image evidence from human-authored OOXML workbooks.

The gold importer already indexes floating workbook images and ``DISPIMG``
formula cells.  This module resolves those logical records back to the original
OOXML media parts, writes the untouched bytes under content-addressed names, and
produces a deterministic audit manifest.  It deliberately never saves or
rewrites the source workbook.
"""

from __future__ import annotations

import hashlib
import posixpath
import re
import zipfile
from collections import Counter
from pathlib import Path, PurePosixPath
from typing import Any, Literal
from urllib.parse import unquote
from xml.etree import ElementTree as ET

from pydantic import BaseModel, ConfigDict, Field

from .gold import GoldImportResult
from .io import sha256_file, write_json_atomic

MANIFEST_NAME = "manifest.json"
_REL_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
_DISPIMG_RE = re.compile(r"(?i)DISPIMG\s*\(\s*[\"']([^\"']+)[\"']")
_CELL_RE = re.compile(r"^([A-Za-z]{1,3})([1-9]\d*)$")
_RANGE_RE = re.compile(r"^([A-Za-z]{1,3})([1-9]\d*):([A-Za-z]{1,3})([1-9]\d*)$")
_SAFE_IMAGE_SUFFIXES = {
    ".bmp",
    ".emf",
    ".gif",
    ".jpeg",
    ".jpg",
    ".png",
    ".svg",
    ".tif",
    ".tiff",
    ".webp",
    ".wmf",
}


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class GoldImageAsset(_StrictModel):
    """One image use in a workbook, linked to its exported original bytes."""

    id: str
    sheet: str
    sheet_index: int = Field(ge=0)
    cell: str
    row: int = Field(ge=1)
    column: int = Field(ge=1)
    end_cell: str | None = None
    category: Literal["elevation", "detail", "drawing", "rendering", "image"]
    source_type: Literal["embedded", "dispimg"]
    formula_id: str | None = None
    sha256: str
    package_path: str
    export_relative_path: str


class GoldImageExportIssue(_StrictModel):
    """A non-guessed outcome encountered while resolving workbook images."""

    code: str
    severity: Literal["REVIEW", "BLOCK"]
    message: str
    sheet: str | None = None
    cell: str | None = None
    formula_id: str | None = None
    package_path: str | None = None


class GoldImageExportManifest(_StrictModel):
    """Portable manifest for exported workbook image evidence."""

    schema_version: str = "1.0"
    source_file: str
    source_sha256: str
    workbook_format: Literal["xls", "xlsx", "xlsm"]
    asset_count: int = Field(ge=0)
    unique_file_count: int = Field(ge=0)
    assets: list[GoldImageAsset] = Field(default_factory=list)
    issues: list[GoldImageExportIssue] = Field(default_factory=list)

    def to_json(self, *, indent: int | None = 2) -> str:
        import json

        return json.dumps(
            self.model_dump(mode="json"),
            ensure_ascii=False,
            indent=indent,
            sort_keys=True,
        )


class UnsafePackagePath(ValueError):
    """Raised when an OOXML member or relationship would escape the package root."""


class _Candidate:
    def __init__(
        self,
        *,
        sheet: str,
        sheet_index: int,
        row: int,
        column: int,
        end_row: int | None,
        end_column: int | None,
        source_type: Literal["embedded", "dispimg"],
        formula_id: str | None,
        package_path: str,
    ) -> None:
        self.sheet = sheet
        self.sheet_index = sheet_index
        self.row = row
        self.column = column
        self.end_row = end_row
        self.end_column = end_column
        self.source_type = source_type
        self.formula_id = formula_id
        self.package_path = package_path


class _SheetPart:
    def __init__(self, *, name: str, index: int, package_path: str) -> None:
        self.name = name
        self.index = index
        self.package_path = package_path


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _column_number(letters: str) -> int:
    value = 0
    for char in letters.upper():
        value = value * 26 + ord(char) - 64
    return value


def _column_letters(column: int) -> str:
    value = column
    result = ""
    while value:
        value, remainder = divmod(value - 1, 26)
        result = chr(65 + remainder) + result
    return result


def _coordinate(row: int, column: int) -> str:
    return f"{_column_letters(column)}{row}"


def _canonical_member_name(name: str) -> str:
    decoded = unquote(name)
    if not decoded or "\\" in decoded or decoded.startswith("/"):
        raise UnsafePackagePath(f"unsafe OOXML member path: {name!r}")
    path = PurePosixPath(decoded)
    if path.is_absolute() or ".." in path.parts or any(part in {"", "."} for part in path.parts):
        raise UnsafePackagePath(f"unsafe OOXML member path: {name!r}")
    if path.parts and ":" in path.parts[0]:
        raise UnsafePackagePath(f"unsafe OOXML member path: {name!r}")
    return path.as_posix()


def _resolve_package_path(source_part: str, target: str) -> str:
    """Resolve one internal relationship without allowing traversal above ZIP root."""

    decoded = unquote(target)
    if not decoded or "\\" in decoded:
        raise UnsafePackagePath(f"unsafe OOXML relationship target: {target!r}")
    if decoded.startswith("/"):
        candidate = decoded.lstrip("/")
    else:
        candidate = posixpath.join(posixpath.dirname(source_part), decoded)
    normalized = posixpath.normpath(candidate)
    if normalized in {"", ".", ".."} or normalized.startswith("../"):
        raise UnsafePackagePath(f"OOXML relationship escapes package root: {target!r}")
    return _canonical_member_name(normalized)


def _relationship_part(source_part: str) -> str:
    directory = posixpath.dirname(source_part)
    filename = posixpath.basename(source_part)
    return posixpath.join(directory, "_rels", f"{filename}.rels")


def _parse_xml(data: bytes, package_path: str) -> ET.Element:
    try:
        return ET.fromstring(data)
    except ET.ParseError as exc:
        raise ValueError(f"invalid OOXML XML part {package_path}: {exc}") from exc


def _member_map(
    archive: zipfile.ZipFile, issues: list[GoldImageExportIssue]
) -> dict[str, zipfile.ZipInfo]:
    grouped: dict[str, list[zipfile.ZipInfo]] = {}
    for info in archive.infolist():
        if info.is_dir():
            continue
        try:
            name = _canonical_member_name(info.filename)
        except UnsafePackagePath as exc:
            issues.append(
                GoldImageExportIssue(
                    code="UNSAFE_PACKAGE_MEMBER",
                    severity="BLOCK",
                    message=str(exc),
                    package_path=info.filename,
                )
            )
            continue
        grouped.setdefault(name, []).append(info)

    members: dict[str, zipfile.ZipInfo] = {}
    for name, values in grouped.items():
        if len(values) > 1:
            issues.append(
                GoldImageExportIssue(
                    code="DUPLICATE_PACKAGE_MEMBER",
                    severity="BLOCK",
                    message="OOXML 包内存在同名部件，无法确定应读取哪一个",
                    package_path=name,
                )
            )
            continue
        members[name] = values[0]
    return members


def _read_member(
    archive: zipfile.ZipFile,
    members: dict[str, zipfile.ZipInfo],
    package_path: str,
) -> bytes | None:
    info = members.get(package_path)
    return archive.read(info) if info is not None else None


def _relationships(
    archive: zipfile.ZipFile,
    members: dict[str, zipfile.ZipInfo],
    source_part: str,
    issues: list[GoldImageExportIssue],
) -> dict[str, tuple[str, str]]:
    rels_path = _relationship_part(source_part)
    data = _read_member(archive, members, rels_path)
    if data is None:
        return {}
    root = _parse_xml(data, rels_path)
    relationships: dict[str, tuple[str, str]] = {}
    for element in root:
        if _local_name(element.tag) != "Relationship":
            continue
        rel_id = element.attrib.get("Id")
        target = element.attrib.get("Target")
        rel_type = element.attrib.get("Type", "")
        if not rel_id or not target or element.attrib.get("TargetMode") == "External":
            continue
        try:
            resolved = _resolve_package_path(source_part, target)
        except UnsafePackagePath as exc:
            issues.append(
                GoldImageExportIssue(
                    code="UNSAFE_RELATIONSHIP_TARGET",
                    severity="BLOCK",
                    message=str(exc),
                    package_path=rels_path,
                )
            )
            continue
        relationships[rel_id] = (rel_type, resolved)
    return relationships


def _workbook_sheets(
    archive: zipfile.ZipFile,
    members: dict[str, zipfile.ZipInfo],
    issues: list[GoldImageExportIssue],
) -> tuple[list[_SheetPart], dict[str, tuple[str, str]]]:
    workbook_part = "xl/workbook.xml"
    data = _read_member(archive, members, workbook_part)
    if data is None:
        raise ValueError("OOXML workbook is missing xl/workbook.xml")
    rels = _relationships(archive, members, workbook_part, issues)
    root = _parse_xml(data, workbook_part)
    sheets: list[_SheetPart] = []
    for element in root.iter():
        if _local_name(element.tag) != "sheet":
            continue
        rel_id = element.attrib.get(f"{{{_REL_NS}}}id")
        relation = rels.get(rel_id or "")
        if relation is None or not relation[0].endswith("/worksheet"):
            issues.append(
                GoldImageExportIssue(
                    code="MISSING_WORKSHEET_RELATIONSHIP",
                    severity="BLOCK",
                    message="工作表关系缺失或不是 worksheet 类型",
                    sheet=element.attrib.get("name"),
                )
            )
            continue
        sheets.append(
            _SheetPart(
                name=element.attrib.get("name", f"Sheet{len(sheets) + 1}"),
                index=len(sheets),
                package_path=relation[1],
            )
        )
    return sheets, rels


def _shared_strings(archive: zipfile.ZipFile, members: dict[str, zipfile.ZipInfo]) -> list[str]:
    data = _read_member(archive, members, "xl/sharedStrings.xml")
    if data is None:
        return []
    root = _parse_xml(data, "xl/sharedStrings.xml")
    values: list[str] = []
    for item in root:
        if _local_name(item.tag) != "si":
            continue
        values.append(
            "".join(node.text or "" for node in item.iter() if _local_name(node.tag) == "t")
        )
    return values


def _sheet_cells(
    root: ET.Element, shared_strings: list[str]
) -> tuple[
    dict[tuple[int, int], str],
    list[tuple[str, int, int, str]],
    list[tuple[int, int, int, int]],
]:
    texts: dict[tuple[int, int], str] = {}
    formulas: list[tuple[str, int, int, str]] = []
    merges: list[tuple[int, int, int, int]] = []
    for cell in root.iter():
        if _local_name(cell.tag) != "c":
            continue
        address = cell.attrib.get("r", "")
        match = _CELL_RE.fullmatch(address)
        if match is None:
            continue
        column = _column_number(match.group(1))
        row = int(match.group(2))
        formula = next((node.text or "" for node in cell if _local_name(node.tag) == "f"), None)
        if formula:
            formulas.append((address.upper(), row, column, formula))
        cell_type = cell.attrib.get("t")
        value_node = next((node for node in cell if _local_name(node.tag) == "v"), None)
        if cell_type == "inlineStr":
            text = "".join(node.text or "" for node in cell.iter() if _local_name(node.tag) == "t")
        elif value_node is not None and value_node.text is not None:
            if cell_type == "s":
                try:
                    text = shared_strings[int(value_node.text)]
                except (IndexError, ValueError):
                    text = ""
            else:
                text = value_node.text
        else:
            text = ""
        if text:
            texts[(row, column)] = text

    for element in root.iter():
        if _local_name(element.tag) != "mergeCell":
            continue
        match = _RANGE_RE.fullmatch(element.attrib.get("ref", ""))
        if match:
            merges.append(
                (
                    int(match.group(2)),
                    _column_number(match.group(1)),
                    int(match.group(4)),
                    _column_number(match.group(3)),
                )
            )
    return texts, formulas, merges


def _normalise_header(value: str) -> str:
    return re.sub(r"[\s\n\r\t/／()（）:_：·-]+", "", value.strip().lower())


def _category_from_text(
    text: str,
) -> Literal["elevation", "detail", "drawing", "rendering", "image"] | None:
    normalized = _normalise_header(text)
    if any(token in normalized for token in ("节点", "大样")):
        return "detail"
    if "立面" in normalized:
        return "elevation"
    if any(token in normalized for token in ("效果", "图样效果")):
        return "rendering"
    if any(token in normalized for token in ("图纸", "图样", "附图")):
        return "drawing"
    return None


def _infer_category(
    row: int,
    column: int,
    texts: dict[tuple[int, int], str],
    merges: list[tuple[int, int, int, int]],
) -> Literal["elevation", "detail", "drawing", "rendering", "image"]:
    for header_row in range(row - 1, 0, -1):
        candidates = [texts.get((header_row, column), "")]
        for min_row, min_col, max_row, max_col in merges:
            if min_row <= header_row <= max_row and min_col <= column <= max_col:
                candidates.append(texts.get((min_row, min_col), ""))
        for text in candidates:
            category = _category_from_text(text)
            if category is not None:
                return category
    return "image"


def _category_lookup(
    gold_result: GoldImportResult | None,
) -> dict[tuple[int, str, str, str | None], str]:
    lookup: dict[tuple[int, str, str, str | None], str] = {}
    if gold_result is None:
        return lookup
    for sheet in gold_result.sheets:
        for image in sheet.images:
            source_type = "dispimg" if image.source_type == "cell_formula" else "embedded"
            key = (
                image.sheet_index,
                image.anchor_coordinate.upper(),
                source_type,
                image.reference_id,
            )
            lookup[key] = image.category
    return lookup


def _node_int(parent: ET.Element, name: str) -> int | None:
    for node in parent:
        if _local_name(node.tag) == name and node.text is not None:
            try:
                return int(node.text)
            except ValueError:
                return None
    return None


def _drawing_candidates(
    archive: zipfile.ZipFile,
    members: dict[str, zipfile.ZipInfo],
    sheet: _SheetPart,
    worksheet_rels: dict[str, tuple[str, str]],
    issues: list[GoldImageExportIssue],
) -> list[_Candidate]:
    candidates: list[_Candidate] = []
    drawing_parts = sorted(
        path for rel_type, path in worksheet_rels.values() if rel_type.endswith("/drawing")
    )
    for drawing_part in drawing_parts:
        drawing_data = _read_member(archive, members, drawing_part)
        if drawing_data is None:
            issues.append(
                GoldImageExportIssue(
                    code="MISSING_DRAWING_PART",
                    severity="BLOCK",
                    message="工作表引用的 drawing 部件不存在",
                    sheet=sheet.name,
                    package_path=drawing_part,
                )
            )
            continue
        drawing_root = _parse_xml(drawing_data, drawing_part)
        drawing_rels = _relationships(archive, members, drawing_part, issues)
        for anchor in drawing_root:
            anchor_name = _local_name(anchor.tag)
            if anchor_name not in {"oneCellAnchor", "twoCellAnchor"}:
                if anchor_name == "absoluteAnchor":
                    issues.append(
                        GoldImageExportIssue(
                            code="IMAGE_WITHOUT_CELL_ANCHOR",
                            severity="REVIEW",
                            message="绝对定位图片没有可审计的工作表单元格，未导出",
                            sheet=sheet.name,
                            package_path=drawing_part,
                        )
                    )
                continue
            start = next((node for node in anchor if _local_name(node.tag) == "from"), None)
            if start is None:
                continue
            start_row = _node_int(start, "row")
            start_column = _node_int(start, "col")
            if start_row is None or start_column is None:
                continue
            end = next((node for node in anchor if _local_name(node.tag) == "to"), None)
            end_row = _node_int(end, "row") if end is not None else None
            end_column = _node_int(end, "col") if end is not None else None
            for blip in (node for node in anchor.iter() if _local_name(node.tag) == "blip"):
                rel_id = blip.attrib.get(f"{{{_REL_NS}}}embed")
                relation = drawing_rels.get(rel_id or "")
                if relation is None or not relation[0].endswith("/image"):
                    issues.append(
                        GoldImageExportIssue(
                            code="MISSING_IMAGE_RELATIONSHIP",
                            severity="BLOCK",
                            message="drawing 图片关系缺失或不是 image 类型",
                            sheet=sheet.name,
                            cell=_coordinate(start_row + 1, start_column + 1),
                            package_path=drawing_part,
                        )
                    )
                    continue
                candidates.append(
                    _Candidate(
                        sheet=sheet.name,
                        sheet_index=sheet.index,
                        row=start_row + 1,
                        column=start_column + 1,
                        end_row=end_row + 1 if end_row is not None else None,
                        end_column=end_column + 1 if end_column is not None else None,
                        source_type="embedded",
                        formula_id=None,
                        package_path=relation[1],
                    )
                )
    return candidates


def _cell_image_map(
    archive: zipfile.ZipFile,
    members: dict[str, zipfile.ZipInfo],
    workbook_rels: dict[str, tuple[str, str]],
    issues: list[GoldImageExportIssue],
) -> dict[str, str]:
    parts = sorted(
        path
        for rel_type, path in workbook_rels.values()
        if rel_type.endswith("/cellImage") or rel_type.endswith("/cellImages")
    )
    if not parts and "xl/cellimages.xml" in members:
        parts = ["xl/cellimages.xml"]
    mappings: dict[str, set[str]] = {}
    for part in parts:
        data = _read_member(archive, members, part)
        if data is None:
            continue
        root = _parse_xml(data, part)
        rels = _relationships(archive, members, part, issues)
        for cell_image in (node for node in root.iter() if _local_name(node.tag) == "cellImage"):
            name = next(
                (
                    node.attrib.get("name")
                    for node in cell_image.iter()
                    if _local_name(node.tag) == "cNvPr" and node.attrib.get("name")
                ),
                None,
            )
            blip = next(
                (node for node in cell_image.iter() if _local_name(node.tag) == "blip"), None
            )
            rel_id = blip.attrib.get(f"{{{_REL_NS}}}embed") if blip is not None else None
            relation = rels.get(rel_id or "")
            if name and relation is not None and relation[0].endswith("/image"):
                mappings.setdefault(name, set()).add(relation[1])

    resolved: dict[str, str] = {}
    for formula_id, paths in sorted(mappings.items()):
        if len(paths) == 1:
            resolved[formula_id] = next(iter(paths))
        else:
            issues.append(
                GoldImageExportIssue(
                    code="AMBIGUOUS_DISPIMG_FORMULA_ID",
                    severity="BLOCK",
                    message="同一 DISPIMG 公式 ID 指向多个媒体部件，已拒绝猜测",
                    formula_id=formula_id,
                )
            )
    return resolved


def _safe_suffix(package_path: str) -> str:
    suffix = PurePosixPath(package_path).suffix.lower()
    return suffix if suffix in _SAFE_IMAGE_SUFFIXES else ".bin"


def _safe_destination(root: Path, relative_path: str) -> Path:
    relative = PurePosixPath(relative_path)
    if relative.is_absolute() or ".." in relative.parts:
        raise UnsafePackagePath(f"unsafe export relative path: {relative_path!r}")
    destination = root.joinpath(*relative.parts)
    resolved = destination.resolve(strict=False)
    if not resolved.is_relative_to(root):
        raise UnsafePackagePath(f"export path escapes destination: {relative_path!r}")
    return destination


def _asset_id(
    source_sha256: str,
    candidate: _Candidate,
    media_sha256: str,
    occurrence_index: int,
) -> str:
    payload = "|".join(
        (
            source_sha256,
            str(candidate.sheet_index),
            candidate.sheet,
            _coordinate(candidate.row, candidate.column),
            candidate.source_type,
            candidate.formula_id or "",
            candidate.package_path,
            media_sha256,
            str(occurrence_index),
        )
    ).encode("utf-8")
    return f"gold-image-asset:{hashlib.sha256(payload).hexdigest()[:20]}"


def _issue_sort_key(issue: GoldImageExportIssue) -> tuple[Any, ...]:
    return (
        issue.severity,
        issue.code,
        issue.sheet or "",
        issue.cell or "",
        issue.formula_id or "",
        issue.package_path or "",
        issue.message,
    )


def _write_manifest(root: Path, manifest: GoldImageExportManifest) -> Path:
    path = _safe_destination(root, MANIFEST_NAME)
    if path.exists() and path.is_symlink():
        raise UnsafePackagePath("refusing to overwrite a symlinked manifest")
    write_json_atomic(path, manifest.model_dump(mode="json"))
    return path


def export_gold_image_assets(
    workbook: str | Path,
    output_dir: str | Path,
    *,
    gold_result: GoldImportResult | None = None,
) -> GoldImageExportManifest:
    """Export original XLSX/XLSM image bytes and write ``manifest.json``.

    The source file is opened read-only.  Legacy binary ``.xls`` workbooks get a
    deterministic BLOCK issue because this exporter does not pretend that OOXML
    media extraction applies to BIFF/OLE containers.
    """

    source = Path(workbook).expanduser().resolve()
    if not source.is_file():
        raise FileNotFoundError(source)
    suffix = source.suffix.lower()
    if suffix not in {".xls", ".xlsx", ".xlsm"}:
        raise ValueError(f"unsupported gold workbook format: {source.suffix}")
    workbook_format: Literal["xls", "xlsx", "xlsm"] = suffix.lstrip(".")  # type: ignore[assignment]
    source_hash = sha256_file(source)
    destination_root = Path(output_dir).expanduser()
    destination_root.mkdir(parents=True, exist_ok=True)
    destination_root = destination_root.resolve()

    if suffix == ".xls":
        manifest = GoldImageExportManifest(
            source_file=source.name,
            source_sha256=source_hash,
            workbook_format="xls",
            asset_count=0,
            unique_file_count=0,
            issues=[
                GoldImageExportIssue(
                    code="LEGACY_XLS_IMAGE_EXPORT_UNSUPPORTED",
                    severity="BLOCK",
                    message=(
                        "旧版 .xls 是 BIFF/OLE 二进制容器；当前只读导出器不解析其中图片，"
                        "不能把未导出的图片当作无图片"
                    ),
                )
            ],
        )
        _write_manifest(destination_root, manifest)
        return manifest

    issues: list[GoldImageExportIssue] = []
    if gold_result is not None and gold_result.source_sha256 != source_hash:
        issues.append(
            GoldImageExportIssue(
                code="GOLD_IMAGE_INDEX_SOURCE_MISMATCH",
                severity="REVIEW",
                message="传入的 gold-import 索引不属于当前工作簿，已忽略并从源表头重新分类",
            )
        )
        gold_result = None
    candidates: list[_Candidate] = []
    category_lookup = _category_lookup(gold_result)
    sheet_context: dict[
        int, tuple[dict[tuple[int, int], str], list[tuple[int, int, int, int]]]
    ] = {}

    try:
        with zipfile.ZipFile(source, "r") as archive:
            members = _member_map(archive, issues)
            sheets, workbook_rels = _workbook_sheets(archive, members, issues)
            shared_strings = _shared_strings(archive, members)
            formula_media = _cell_image_map(archive, members, workbook_rels, issues)

            for sheet in sheets:
                worksheet_data = _read_member(archive, members, sheet.package_path)
                if worksheet_data is None:
                    issues.append(
                        GoldImageExportIssue(
                            code="MISSING_WORKSHEET_PART",
                            severity="BLOCK",
                            message="workbook 引用的工作表部件不存在",
                            sheet=sheet.name,
                            package_path=sheet.package_path,
                        )
                    )
                    continue
                worksheet_root = _parse_xml(worksheet_data, sheet.package_path)
                texts, formulas, merges = _sheet_cells(worksheet_root, shared_strings)
                sheet_context[sheet.index] = (texts, merges)
                worksheet_rels = _relationships(archive, members, sheet.package_path, issues)
                candidates.extend(
                    _drawing_candidates(
                        archive,
                        members,
                        sheet,
                        worksheet_rels,
                        issues,
                    )
                )
                for cell, row, column, formula in formulas:
                    match = _DISPIMG_RE.search(formula)
                    if match is None:
                        continue
                    formula_id = match.group(1)
                    package_path = formula_media.get(formula_id)
                    if package_path is None:
                        issues.append(
                            GoldImageExportIssue(
                                code="DISPIMG_MEDIA_NOT_FOUND",
                                severity="BLOCK",
                                message="DISPIMG 公式 ID 没有可解析的 cellImages 媒体关系",
                                sheet=sheet.name,
                                cell=cell,
                                formula_id=formula_id,
                            )
                        )
                        continue
                    candidates.append(
                        _Candidate(
                            sheet=sheet.name,
                            sheet_index=sheet.index,
                            row=row,
                            column=column,
                            end_row=None,
                            end_column=None,
                            source_type="dispimg",
                            formula_id=formula_id,
                            package_path=package_path,
                        )
                    )

            candidates.sort(
                key=lambda value: (
                    value.sheet_index,
                    value.row,
                    value.column,
                    value.source_type,
                    value.formula_id or "",
                    value.package_path,
                )
            )
            assets: list[GoldImageAsset] = []
            written_paths: set[str] = set()
            for occurrence_index, candidate in enumerate(candidates, start=1):
                image_bytes = _read_member(archive, members, candidate.package_path)
                cell = _coordinate(candidate.row, candidate.column)
                if image_bytes is None:
                    issues.append(
                        GoldImageExportIssue(
                            code="MISSING_IMAGE_PART",
                            severity="BLOCK",
                            message="图片关系指向的媒体部件不存在",
                            sheet=candidate.sheet,
                            cell=cell,
                            formula_id=candidate.formula_id,
                            package_path=candidate.package_path,
                        )
                    )
                    continue
                media_sha256 = hashlib.sha256(image_bytes).hexdigest()
                relative_path = f"assets/{media_sha256}{_safe_suffix(candidate.package_path)}"
                output_path = _safe_destination(destination_root, relative_path)
                if output_path.exists():
                    existing_hash = (
                        None
                        if output_path.is_symlink()
                        else hashlib.sha256(output_path.read_bytes()).hexdigest()
                    )
                    if existing_hash != media_sha256:
                        issues.append(
                            GoldImageExportIssue(
                                code="IMAGE_EXPORT_COLLISION",
                                severity="BLOCK",
                                message="内容寻址的导出路径已存在不同内容或符号链接，拒绝覆盖",
                                sheet=candidate.sheet,
                                cell=cell,
                                package_path=candidate.package_path,
                            )
                        )
                        continue
                else:
                    output_path.parent.mkdir(parents=True, exist_ok=True)
                    output_path.write_bytes(image_bytes)
                written_paths.add(relative_path)
                lookup_key = (
                    candidate.sheet_index,
                    cell,
                    candidate.source_type,
                    candidate.formula_id,
                )
                context = sheet_context.get(candidate.sheet_index, ({}, []))
                category = category_lookup.get(lookup_key) or _infer_category(
                    candidate.row,
                    candidate.column,
                    context[0],
                    context[1],
                )
                end_cell = (
                    _coordinate(candidate.end_row, candidate.end_column)
                    if candidate.end_row is not None and candidate.end_column is not None
                    else None
                )
                assets.append(
                    GoldImageAsset(
                        id=_asset_id(
                            source_hash,
                            candidate,
                            media_sha256,
                            occurrence_index,
                        ),
                        sheet=candidate.sheet,
                        sheet_index=candidate.sheet_index,
                        cell=cell,
                        row=candidate.row,
                        column=candidate.column,
                        end_cell=end_cell,
                        category=category,  # type: ignore[arg-type]
                        source_type=candidate.source_type,
                        formula_id=candidate.formula_id,
                        sha256=media_sha256,
                        package_path=candidate.package_path,
                        export_relative_path=relative_path,
                    )
                )
    except zipfile.BadZipFile as exc:
        issues.append(
            GoldImageExportIssue(
                code="INVALID_OOXML_PACKAGE",
                severity="BLOCK",
                message=f"无法读取 XLSX/XLSM ZIP 包: {exc}",
            )
        )
        assets = []
        written_paths = set()

    duplicate_ids = [
        asset_id for asset_id, count in Counter(asset.id for asset in assets).items() if count > 1
    ]
    if duplicate_ids:
        raise RuntimeError(f"non-unique deterministic image IDs: {duplicate_ids}")
    issues.sort(key=_issue_sort_key)
    manifest = GoldImageExportManifest(
        source_file=source.name,
        source_sha256=source_hash,
        workbook_format=workbook_format,
        asset_count=len(assets),
        unique_file_count=len(written_paths),
        assets=assets,
        issues=issues,
    )
    _write_manifest(destination_root, manifest)
    return manifest


__all__ = [
    "GoldImageAsset",
    "GoldImageExportIssue",
    "GoldImageExportManifest",
    "MANIFEST_NAME",
    "UnsafePackagePath",
    "export_gold_image_assets",
]
