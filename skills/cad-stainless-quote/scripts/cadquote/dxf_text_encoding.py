"""Conservative recovery for ASCII DXF text written with undeclared UTF-8 bytes.

Some DWG converters keep ``$DWGCODEPAGE`` at a legacy Chinese code page while
writing entity text as UTF-8.  A normal DXF reader then returns mojibake even
though the original bytes are intact.  This module scans only text-bearing DXF
groups, requires strong file-level UTF-8 evidence, and returns handle-addressed
repairs.  It never rewrites the source drawing.
"""

from __future__ import annotations

import hashlib
import math
from dataclasses import dataclass
from pathlib import Path

from ezdxf.tools.text import plain_mtext

_TEXT_GROUPS = {
    "TEXT": frozenset({1}),
    "ATTRIB": frozenset({1}),
    "ATTDEF": frozenset({1}),
    "MTEXT": frozenset({1, 3}),
}
_UTF8_CODEPAGES = frozenset({"UTF-8", "UTF8", "ANSI_65001", "CP65001"})


@dataclass(frozen=True, slots=True)
class DxfTextRepair:
    """One source-backed text replacement keyed by an immutable DXF handle."""

    handle: str
    entity_type: str
    text: str
    raw_sha256: str


@dataclass(frozen=True, slots=True)
class DxfTextRepairPlan:
    """File-level decision and the safe replacements admitted by that decision."""

    active: bool
    reason: str
    dxf_version: str | None
    declared_codepage: str | None
    non_ascii_text_count: int
    utf8_cjk_text_count: int
    repairs: dict[str, DxfTextRepair]


@dataclass(slots=True)
class _RawTextEntity:
    entity_type: str
    handle: str | None = None
    text_groups: list[tuple[int, bytes]] | None = None

    def __post_init__(self) -> None:
        if self.text_groups is None:
            self.text_groups = []


def _is_cjk(character: str) -> bool:
    codepoint = ord(character)
    return (
        0x3400 <= codepoint <= 0x4DBF
        or 0x4E00 <= codepoint <= 0x9FFF
        or 0xF900 <= codepoint <= 0xFAFF
        or 0x20000 <= codepoint <= 0x3134F
    )


def _normalize_candidate(entity_type: str, value: str) -> str | None:
    cleaned = value.replace("\x00", "")
    if entity_type == "MTEXT":
        try:
            cleaned = plain_mtext(cleaned)
        except Exception:
            pass
    cleaned = cleaned.strip()
    if not cleaned or "\ufffd" in cleaned:
        return None
    if any(ord(character) < 32 and character not in "\t\r\n" for character in cleaned):
        return None
    return cleaned


def _decode_ascii(value: bytes) -> str:
    return value.decode("ascii", errors="replace").strip()


def _flush_entity(
    current: _RawTextEntity | None,
    output: list[tuple[str, str, bytes]],
) -> None:
    if current is None or not current.handle or not current.text_groups:
        return
    if current.entity_type == "MTEXT":
        # DXF semantics are all group-3 chunks followed by the group-1 tail.
        # Physical group ordering is not guaranteed by the DXF specification.
        chunks = [value for code, value in current.text_groups if code == 3]
        tails = [value for code, value in current.text_groups if code == 1]
        raw_value = b"".join([*chunks, *(tails[-1:] or [])])
    else:
        text_values = [value for code, value in current.text_groups if code == 1]
        raw_value = text_values[-1] if text_values else b""
    if any(byte >= 128 for byte in raw_value):
        output.append((current.handle.upper(), current.entity_type, raw_value))


def _scan_ascii_dxf(
    path: Path,
) -> tuple[str | None, str | None, list[tuple[str, str, bytes]]]:
    dxf_version: str | None = None
    declared_codepage: str | None = None
    values: list[tuple[str, str, bytes]] = []
    current: _RawTextEntity | None = None
    awaiting_header_variable: str | None = None

    with path.open("rb") as stream:
        first_line = stream.readline()
        if first_line.startswith(b"AutoCAD Binary DXF"):
            return None, None, []
        stream.seek(0)
        while True:
            code_line = stream.readline()
            if not code_line:
                break
            value_line = stream.readline()
            if not value_line:
                break
            try:
                code = int(code_line.lstrip(b"\xef\xbb\xbf").strip())
            except ValueError:
                continue
            value = value_line.rstrip(b"\r\n")

            if code == 9:
                awaiting_header_variable = _decode_ascii(value).upper()
            elif awaiting_header_variable:
                if awaiting_header_variable == "$DWGCODEPAGE" and code in {1, 3}:
                    declared_codepage = _decode_ascii(value) or None
                elif awaiting_header_variable == "$ACADVER" and code == 1:
                    dxf_version = _decode_ascii(value).upper() or None
                awaiting_header_variable = None

            if code == 0:
                _flush_entity(current, values)
                entity_type = _decode_ascii(value).upper()
                current = (
                    _RawTextEntity(entity_type=entity_type)
                    if entity_type in _TEXT_GROUPS
                    else None
                )
                continue
            if current is None:
                continue
            if code == 5 and current.handle is None:
                current.handle = _decode_ascii(value)
            elif code in _TEXT_GROUPS[current.entity_type]:
                current.text_groups.append((code, value))

    _flush_entity(current, values)
    return dxf_version, declared_codepage, values


def _is_2007_or_newer(dxf_version: str | None) -> bool:
    if not dxf_version or not dxf_version.startswith("AC"):
        return False
    try:
        return int(dxf_version[2:]) >= 1021
    except ValueError:
        return False


def plan_utf8_text_repairs(path: Path | str) -> DxfTextRepairPlan:
    """Return repairs only when the whole file strongly contradicts its code page.

    Activation requires at least three non-ASCII text entities and at least 70%
    of them to be strict UTF-8 strings containing CJK.  This deliberately leaves
    small or mixed-encoding drawings in review instead of guessing.
    """

    source = Path(path).expanduser().resolve()
    try:
        dxf_version, declared_codepage, raw_values = _scan_ascii_dxf(source)
    except OSError as exc:
        return DxfTextRepairPlan(
            active=False,
            reason=f"source_read_failed:{type(exc).__name__}",
            dxf_version=None,
            declared_codepage=None,
            non_ascii_text_count=0,
            utf8_cjk_text_count=0,
            repairs={},
        )

    if _is_2007_or_newer(dxf_version):
        return DxfTextRepairPlan(
            active=False,
            reason="dxf_2007_or_newer_uses_utf8",
            dxf_version=dxf_version,
            declared_codepage=declared_codepage,
            non_ascii_text_count=len(raw_values),
            utf8_cjk_text_count=0,
            repairs={},
        )

    if declared_codepage and declared_codepage.upper() in _UTF8_CODEPAGES:
        return DxfTextRepairPlan(
            active=False,
            reason="declared_utf8_no_repair_needed",
            dxf_version=dxf_version,
            declared_codepage=declared_codepage,
            non_ascii_text_count=len(raw_values),
            utf8_cjk_text_count=0,
            repairs={},
        )

    decoded: dict[str, DxfTextRepair] = {}
    for handle, entity_type, raw_value in raw_values:
        try:
            candidate = raw_value.decode("utf-8", errors="strict")
        except UnicodeDecodeError:
            continue
        normalized = _normalize_candidate(entity_type, candidate)
        if normalized is None or not any(_is_cjk(character) for character in normalized):
            continue
        decoded[handle] = DxfTextRepair(
            handle=handle,
            entity_type=entity_type,
            text=normalized,
            raw_sha256=hashlib.sha256(raw_value).hexdigest(),
        )

    non_ascii_count = len(raw_values)
    utf8_cjk_count = len(decoded)
    required_count = max(3, math.ceil(non_ascii_count * 0.70))
    if non_ascii_count < 3 or utf8_cjk_count < required_count:
        return DxfTextRepairPlan(
            active=False,
            reason="insufficient_file_level_utf8_evidence",
            dxf_version=dxf_version,
            declared_codepage=declared_codepage,
            non_ascii_text_count=non_ascii_count,
            utf8_cjk_text_count=utf8_cjk_count,
            repairs={},
        )

    return DxfTextRepairPlan(
        active=True,
        reason="legacy_codepage_conflicts_with_utf8_entity_bytes",
        dxf_version=dxf_version,
        declared_codepage=declared_codepage,
        non_ascii_text_count=non_ascii_count,
        utf8_cjk_text_count=utf8_cjk_count,
        repairs=decoded,
    )
