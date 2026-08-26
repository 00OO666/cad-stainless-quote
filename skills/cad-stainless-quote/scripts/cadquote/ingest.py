"""Secure, deterministic project input ingestion.

The caller supplies an existing directory or a ZIP/RAR/7z archive and a dedicated
run directory.  Inputs are treated as untrusted and read-only.  This module first
preflights the complete input, then copies/extracts into a private staging directory
inside the run directory, verifies sizes and hashes, and finally publishes the
snapshot atomically as ``<run_dir>/ingest``.

No archive library is allowed to choose final filesystem paths.  ZIP and RAR members
are streamed directly to validated destinations.  7z members are likewise streamed
one by one through the installed 7-Zip backend, so declared and runtime byte limits
are enforced before a member can fill the staging disk.
"""

from __future__ import annotations

import errno
import hashlib
import mimetypes
import os
import re
import shutil
import stat
import struct
import subprocess
import tempfile
import time
import unicodedata
import zipfile
import zlib
from dataclasses import dataclass
from dataclasses import field as dataclass_field
from pathlib import Path, PurePosixPath
from typing import BinaryIO, Literal

from pydantic import Field

from .io import sha256_file
from .models import ReviewStatus, RunIssue, Severity, SourceFile, StrictModel


class IngestLimits(StrictModel):
    """Resource and path limits applied before any extraction starts."""

    max_members: int = Field(default=20_000, gt=0)
    max_file_bytes: int = Field(default=2 * 1024**3, gt=0)
    max_total_bytes: int = Field(default=20 * 1024**3, gt=0)
    max_archive_bytes: int = Field(default=5 * 1024**3, gt=0)
    max_compression_ratio: float = Field(default=200.0, gt=0)
    max_path_chars: int = Field(default=1_024, gt=0)
    max_path_depth: int = Field(default=64, gt=0)
    copy_chunk_bytes: int = Field(default=1024 * 1024, gt=0)


class IngestResult(StrictModel):
    """Serializable result returned for both accepted and rejected inputs."""

    input_path: str
    input_kind: Literal["directory", "file", "zip", "rar", "7z", "unsupported"]
    ingest_dir: str
    extracted_dir: str
    original_copy: str | None = None
    input_sha256: str | None = None
    succeeded: bool = False
    files: list[SourceFile] = Field(default_factory=list)
    issues: list[RunIssue] = Field(default_factory=list)
    metadata: dict[str, object] = Field(default_factory=dict)


@dataclass(slots=True)
class _RawMember:
    original_name: str
    size: int
    compressed_size: int | None
    output_name: str | None = None
    path_aliases: tuple[str, ...] = ()
    name_repaired: bool = False
    repaired_encoding: str | None = None
    is_dir: bool = False
    is_link: bool = False
    is_special: bool = False
    encrypted: bool = False
    source_path: Path | None = None
    source_signature: tuple[int, int, int, int] | None = None


@dataclass(slots=True)
class _Member:
    original_name: str
    relative_path: str
    size: int
    compressed_size: int | None
    name_repaired: bool = False
    repaired_encoding: str | None = None
    source_path: Path | None = None
    source_signature: tuple[int, int, int, int] | None = None


@dataclass(slots=True)
class _Preflight:
    members: list[_Member] = dataclass_field(default_factory=list)
    issues: list[RunIssue] = dataclass_field(default_factory=list)
    input_sha256: str | None = None
    metadata: dict[str, object] = dataclass_field(default_factory=dict)


class _IngestFailure(Exception):
    def __init__(self, code: str, message: str, evidence: list[str] | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.evidence = evidence or []


_DRIVE_PREFIX = re.compile(r"^[A-Za-z]:")
_WINDOWS_INVALID = frozenset('<>:"/\\|?*')
_WINDOWS_RESERVED = {
    "CON",
    "PRN",
    "AUX",
    "NUL",
    "CLOCK$",
    *(f"COM{index}" for index in range(1, 10)),
    *(f"LPT{index}" for index in range(1, 10)),
}

_MEDIA_TYPES = {
    ".dwg": "image/vnd.dwg",
    ".dxf": "image/vnd.dxf",
    ".xls": "application/vnd.ms-excel",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
}

_SINGLE_FILE_SUFFIXES = {
    ".dwg",
    ".dxf",
    ".pdf",
    ".xls",
    ".xlsx",
    ".xlsm",
    ".docx",
    ".png",
    ".jpg",
    ".jpeg",
    ".tif",
    ".tiff",
}

_ARCHIVE_SUFFIXES: dict[str, Literal["zip", "rar", "7z"]] = {
    ".zip": "zip",
    ".rar": "rar",
    ".7z": "7z",
}
_ZIP_SIGNATURES = (b"PK\x03\x04", b"PK\x05\x06", b"PK\x07\x08")
_RAR_SIGNATURES = (b"Rar!\x1a\x07\x00", b"Rar!\x1a\x07\x01\x00")
_SEVEN_Z_SIGNATURE = b"7z\xbc\xaf\x27\x1c"
_PUBLISH_RETRY_DELAYS_SECONDS = (0.1, 0.25, 0.5, 1.0, 2.0, 4.0, 6.0)
_TRANSIENT_PUBLISH_WINERRORS = {5, 32, 33}


def _issue(
    code: str,
    message: str,
    *,
    severity: Severity = Severity.BLOCK,
    evidence: list[str] | None = None,
    suggested_action: str | None = None,
) -> RunIssue:
    return RunIssue(
        stage="ingest",
        severity=severity,
        code=code,
        message=message,
        evidence=evidence or [],
        suggested_action=suggested_action,
    )


def _has_blocking_issue(issues: list[RunIssue]) -> bool:
    return any(issue.severity in {Severity.ERROR, Severity.BLOCK} for issue in issues)


def _archive_kind_from_prefix(prefix: bytes) -> Literal["zip", "rar", "7z"] | None:
    if prefix.startswith(_ZIP_SIGNATURES):
        return "zip"
    if prefix.startswith(_RAR_SIGNATURES):
        return "rar"
    if prefix.startswith(_SEVEN_Z_SIGNATURE):
        return "7z"
    return None


def _is_macos_metadata(relative_path: str) -> bool:
    """Return true for Finder/AppleDouble metadata, never for a project drawing."""

    parts = PurePosixPath(relative_path).parts
    folded = tuple(part.casefold() for part in parts)
    return any(part == "__macosx" or part.startswith("._") for part in folded) or (
        bool(folded) and folded[-1] == ".ds_store"
    )


def _zip_extra_fields(extra: bytes) -> list[tuple[int, bytes]]:
    fields: list[tuple[int, bytes]] = []
    offset = 0
    while offset + 4 <= len(extra):
        header_id, size = struct.unpack_from("<HH", extra, offset)
        offset += 4
        end = offset + size
        if end > len(extra):
            break
        fields.append((header_id, extra[offset:end]))
        offset = end
    return fields


def _zip_unicode_path(extra: bytes, raw_name: bytes) -> str | None:
    """Read a valid Info-ZIP Unicode Path alias (0x7075), if present."""

    for header_id, payload in _zip_extra_fields(extra):
        if header_id != 0x7075 or len(payload) < 5 or payload[0] != 1:
            continue
        expected_crc = struct.unpack_from("<I", payload, 1)[0]
        if expected_crc != zlib.crc32(raw_name) & 0xFFFFFFFF:
            continue
        try:
            return payload[5:].decode("utf-8", errors="strict")
        except UnicodeDecodeError:
            continue
    return None


def _is_cjk(character: str) -> bool:
    codepoint = ord(character)
    return (
        0x3400 <= codepoint <= 0x4DBF
        or 0x4E00 <= codepoint <= 0x9FFF
        or 0xF900 <= codepoint <= 0xFAFF
        or 0x20000 <= codepoint <= 0x3134F
    )


def _cp437_mojibake_group(character: str) -> str | None:
    codepoint = ord(character)
    if 0x2500 <= codepoint <= 0x257F:
        return "box"
    if 0x2580 <= codepoint <= 0x259F:
        return "block"
    if 0x0370 <= codepoint <= 0x03FF:
        return "greek"
    if 0x2200 <= codepoint <= 0x22FF:
        return "math"
    if 0x2300 <= codepoint <= 0x23FF:
        return "technical"
    if character in "¬µªº±÷×·¤¢£¥ƒ":
        return "legacy-symbol"
    return None


def _name_quality(value: str) -> tuple[float, int, int, set[str]]:
    visible = [character for character in value if character not in "/\\"]
    if not visible:
        return float("-inf"), 0, 0, set()
    printable = sum(character.isprintable() for character in visible)
    cjk_count = sum(_is_cjk(character) for character in visible)
    mojibake_groups = {
        group for character in visible if (group := _cp437_mojibake_group(character))
    }
    mojibake_count = sum(_cp437_mojibake_group(character) is not None for character in visible)
    score = (
        2.0 * printable / len(visible)
        + 4.0 * cjk_count / len(visible)
        - 5.0 * mojibake_count / len(visible)
    )
    return score, cjk_count, mojibake_count, mojibake_groups


def _clear_legacy_filename_repair(lookup_name: str, candidate: str) -> bool:
    """Require strong mojibake-to-CJK improvement before changing CP437 output.

    A no-flag ZIP byte sequence is intrinsically ambiguous.  This deliberately
    declines weak repairs (including ordinary accented CP437 names) and accepts
    only candidates with multiple CJK characters plus multiple characteristic
    CP437 mojibake classes.  The untouched lookup name remains available for reads.
    """

    if candidate == lookup_name:
        return False
    lookup_score, _, mojibake_count, mojibake_groups = _name_quality(lookup_name)
    candidate_score, cjk_count, candidate_mojibake_count, _ = _name_quality(candidate)
    if cjk_count < 2 or candidate_mojibake_count:
        return False
    if mojibake_count < 3 or len(mojibake_groups) < 2:
        return False
    return candidate_score - lookup_score >= 2.0


def _reasonable_utf8_filename_repair(lookup_name: str, candidate: str) -> bool:
    """Prefer strict UTF-8 when it clearly turns CP437 mojibake into CJK text."""

    if candidate == lookup_name:
        return False
    lookup_score, _, lookup_mojibake_count, _ = _name_quality(lookup_name)
    candidate_score, cjk_count, candidate_mojibake_count, _ = _name_quality(candidate)
    return (
        cjk_count >= 1
        and candidate_mojibake_count == 0
        and lookup_mojibake_count >= 2
        and candidate_score - lookup_score >= 1.0
    )


def _zip_member_names(
    info: zipfile.ZipInfo,
) -> tuple[str, str, tuple[str, ...], str | None, tuple[tuple[bytes, str], ...]]:
    """Return validated-later lookup, output, aliases, and repaired encoding.

    ZIP archives produced by some older tools store UTF-8 or GBK/GB18030 filename
    bytes without setting bit 11.  Python must initially decode those bytes as
    CP437.  The byte round-trip and conservative quality score repair clear Chinese
    mojibake while keeping the exact lookup key for ``ZipFile.open``.  Every returned
    alias is safety-checked before extraction.
    """

    lookup_name = info.filename
    legacy_name = getattr(info, "orig_filename", lookup_name)
    aliases: list[str] = []
    if legacy_name != lookup_name:
        aliases.append(legacy_name)

    utf8_flagged = bool(info.flag_bits & 0x800)
    try:
        raw_name = legacy_name.encode("utf-8" if utf8_flagged else "cp437")
    except UnicodeEncodeError:
        raw_name = b""

    output_name = lookup_name
    repaired_encoding: str | None = None
    unicode_alias = _zip_unicode_path(info.extra, raw_name) if raw_name else None
    if unicode_alias is not None:
        output_name = unicode_alias
        repaired_encoding = "infozip_unicode_path" if unicode_alias != lookup_name else None
        if unicode_alias != lookup_name:
            aliases.append(unicode_alias)
    elif not utf8_flagged and raw_name:
        utf8_candidate: str | None = None
        try:
            utf8_candidate = raw_name.decode("utf-8", errors="strict")
        except UnicodeDecodeError:
            pass
        if utf8_candidate is not None and _reasonable_utf8_filename_repair(
            lookup_name, utf8_candidate
        ):
            repaired_encoding, output_name = "utf-8", utf8_candidate
            aliases.append(output_name)
        else:
            try:
                gb18030_candidate = raw_name.decode("gb18030", errors="strict")
            except UnicodeDecodeError:
                gb18030_candidate = None
            if gb18030_candidate is not None and _clear_legacy_filename_repair(
                lookup_name, gb18030_candidate
            ):
                utf8_is_clearly_worse = utf8_candidate is None or (
                    _name_quality(gb18030_candidate)[0] - _name_quality(utf8_candidate)[0] >= 2.0
                )
                if utf8_is_clearly_worse:
                    repaired_encoding, output_name = "gb18030", gb18030_candidate
                    aliases.append(output_name)

    unique_aliases = tuple(dict.fromkeys(aliases))
    raw_segments = [part for part in re.split(rb"[/\\]", raw_name) if part] if raw_name else []
    output_segments = [part for part in re.split(r"[/\\]", output_name) if part]
    segment_aliases = (
        tuple(zip(raw_segments, output_segments, strict=True))
        if len(raw_segments) == len(output_segments)
        else ()
    )
    return lookup_name, output_name, unique_aliases, repaired_encoding, segment_aliases


def _reuse_zip_segment_aliases(
    output_name: str,
    segment_aliases: tuple[tuple[bytes, str], ...],
    segment_map: dict[bytes, tuple[str, str | None]],
    repaired_encoding: str | None,
) -> tuple[str, str | None, int, list[tuple[str, str]]]:
    """Make each repeated raw ZIP path segment resolve to one Unicode alias."""

    parts = re.split(r"([/\\])", output_name)
    segment_positions = [
        index for index, part in enumerate(parts) if part and part not in {"/", "\\"}
    ]
    if len(segment_positions) != len(segment_aliases):
        return output_name, repaired_encoding, 0, []

    overrides = 0
    authoritative_conflicts: list[tuple[str, str]] = []
    inherited_encodings: set[str] = set()
    for position, (raw_segment, proposed_segment) in zip(
        segment_positions, segment_aliases, strict=True
    ):
        prior = segment_map.get(raw_segment)
        if prior is None:
            segment_map[raw_segment] = (proposed_segment, repaired_encoding)
            continue
        prior_segment, prior_encoding = prior
        if prior_segment == proposed_segment:
            if prior_encoding:
                inherited_encodings.add(prior_encoding)
            continue
        if "infozip_unicode_path" in {prior_encoding, repaired_encoding}:
            authoritative_conflicts.append((prior_segment, proposed_segment))
            continue
        parts[position] = prior_segment
        overrides += 1
        if prior_encoding:
            inherited_encodings.add(prior_encoding)

    effective_encoding = repaired_encoding
    if effective_encoding is None and len(inherited_encodings) == 1:
        effective_encoding = next(iter(inherited_encodings))
    return "".join(parts), effective_encoding, overrides, authoritative_conflicts


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def _is_reparse_point(file_stat: os.stat_result) -> bool:
    attributes = getattr(file_stat, "st_file_attributes", 0)
    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    return bool(attributes & reparse_flag)


def _normalize_relative_path(raw_name: str, limits: IngestLimits) -> str:
    """Normalize an untrusted member path or raise a precise security failure."""

    if not isinstance(raw_name, str) or not raw_name:
        raise _IngestFailure("PATH_EMPTY", "发现空文件名。")
    if "\x00" in raw_name:
        raise _IngestFailure("PATH_NUL", "文件名包含 NUL 字符。", [repr(raw_name)])

    slash_name = raw_name.replace("\\", "/")
    if slash_name.startswith("/") or _DRIVE_PREFIX.match(slash_name):
        raise _IngestFailure("PATH_ABSOLUTE", "拒绝绝对路径成员。", [raw_name])

    normalized_parts: list[str] = []
    for raw_part in slash_name.split("/"):
        if raw_part in {"", "."}:
            continue
        if raw_part == "..":
            raise _IngestFailure("PATH_TRAVERSAL", "拒绝包含 '..' 的路径。", [raw_name])

        part = unicodedata.normalize("NFC", raw_part)
        if part in {"", ".", ".."}:
            raise _IngestFailure("PATH_INVALID", "路径规范化后无效。", [raw_name])
        if part.endswith((" ", ".")):
            raise _IngestFailure(
                "PATH_WINDOWS_UNSAFE",
                "文件名以空格或句点结尾，Windows 下会发生路径别名。",
                [raw_name],
            )
        if any(character in _WINDOWS_INVALID for character in part):
            raise _IngestFailure(
                "PATH_WINDOWS_UNSAFE",
                "文件名包含 Windows 禁止字符。",
                [raw_name],
            )
        if any(ord(character) < 32 or ord(character) == 127 for character in part):
            raise _IngestFailure("PATH_CONTROL_CHAR", "文件名包含控制字符。", [raw_name])
        if part.split(".", 1)[0].upper() in _WINDOWS_RESERVED:
            raise _IngestFailure(
                "PATH_WINDOWS_RESERVED",
                "文件名使用 Windows 保留设备名。",
                [raw_name],
            )
        normalized_parts.append(part)

    if not normalized_parts:
        raise _IngestFailure("PATH_EMPTY", "路径规范化后为空。", [raw_name])
    if len(normalized_parts) > limits.max_path_depth:
        raise _IngestFailure(
            "PATH_DEPTH_LIMIT",
            f"路径层级超过限制 {limits.max_path_depth}。",
            [raw_name],
        )

    normalized = PurePosixPath(*normalized_parts).as_posix()
    if len(normalized) > limits.max_path_chars:
        raise _IngestFailure(
            "PATH_LENGTH_LIMIT",
            f"规范化路径长度超过限制 {limits.max_path_chars}。",
            [raw_name],
        )
    return normalized


def _safe_target(root: Path, relative_path: str) -> Path:
    target = root.joinpath(*PurePosixPath(relative_path).parts)
    resolved_root = root.resolve(strict=False)
    resolved_target = target.resolve(strict=False)
    if not _is_within(resolved_target, resolved_root):
        raise _IngestFailure(
            "PATH_ESCAPE",
            "规范化后的目标路径仍越出运行目录。",
            [relative_path],
        )
    return target


def _validate_members(
    raw_members: list[_RawMember],
    limits: IngestLimits,
    *,
    archive_bytes: int | None = None,
) -> _Preflight:
    result = _Preflight()
    if len(raw_members) > limits.max_members:
        result.issues.append(
            _issue(
                "MEMBER_COUNT_LIMIT",
                f"成员数 {len(raw_members)} 超过限制 {limits.max_members}。",
            )
        )

    seen_paths: dict[str, tuple[str, bool]] = {}
    file_keys: set[str] = set()
    directory_keys: set[str] = set()
    total_size = 0
    skipped_macos_count = 0
    skipped_macos_bytes = 0
    repaired_name_count = 0
    repaired_name_encodings: dict[str, int] = {}

    for raw in raw_members:
        output_name = raw.output_name or raw.original_name
        names_to_validate = tuple(
            dict.fromkeys((raw.original_name, *raw.path_aliases, output_name))
        )
        normalized_aliases: dict[str, str] = {}
        alias_invalid = False
        for alias in names_to_validate:
            try:
                normalized_aliases[alias] = _normalize_relative_path(alias, limits)
            except _IngestFailure as failure:
                result.issues.append(
                    _issue(failure.code, failure.message, evidence=failure.evidence)
                )
                alias_invalid = True
        if alias_invalid:
            continue
        relative_path = normalized_aliases[output_name]

        if _is_macos_metadata(relative_path):
            skipped_macos_count += 1
            skipped_macos_bytes += max(raw.size, 0)
            continue

        if raw.name_repaired:
            repaired_name_count += 1
            encoding = raw.repaired_encoding or "unknown"
            repaired_name_encodings[encoding] = repaired_name_encodings.get(encoding, 0) + 1

        collision_key = relative_path.casefold()
        prior = seen_paths.get(collision_key)
        if prior is not None:
            prior_path, _ = prior
            result.issues.append(
                _issue(
                    "PATH_COLLISION",
                    "多个成员规范化为同一路径，拒绝可能的覆盖。",
                    evidence=[prior_path, raw.original_name, relative_path],
                )
            )
            continue
        seen_paths[collision_key] = (relative_path, raw.is_dir)

        if raw.is_link:
            result.issues.append(
                _issue(
                    "LINK_MEMBER_REJECTED",
                    "拒绝符号链接、硬链接或 Windows junction 成员。",
                    evidence=[raw.original_name],
                )
            )
            continue
        if raw.is_special:
            result.issues.append(
                _issue(
                    "SPECIAL_FILE_REJECTED",
                    "拒绝设备、管道、套接字等特殊文件。",
                    evidence=[raw.original_name],
                )
            )
            continue
        if raw.encrypted:
            result.issues.append(
                _issue(
                    "ENCRYPTED_MEMBER_REJECTED",
                    "归档成员已加密，无法在无人值守流程中验证。",
                    evidence=[raw.original_name],
                    suggested_action="请提供未加密归档。",
                )
            )
            continue

        if raw.is_dir:
            directory_keys.add(collision_key)
            continue
        if raw.size < 0:
            result.issues.append(
                _issue(
                    "MEMBER_SIZE_INVALID",
                    "归档声明了负数文件大小。",
                    evidence=[raw.original_name, str(raw.size)],
                )
            )
            continue
        if raw.size > limits.max_file_bytes:
            result.issues.append(
                _issue(
                    "FILE_SIZE_LIMIT",
                    f"文件大小 {raw.size} 超过单文件限制 {limits.max_file_bytes}。",
                    evidence=[raw.original_name],
                )
            )

        total_size += raw.size
        if raw.size > 0 and raw.compressed_size is not None:
            ratio = float("inf") if raw.compressed_size <= 0 else raw.size / raw.compressed_size
            if ratio > limits.max_compression_ratio:
                result.issues.append(
                    _issue(
                        "COMPRESSION_RATIO_LIMIT",
                        (f"成员压缩比 {ratio:.2f} 超过限制 {limits.max_compression_ratio:.2f}。"),
                        evidence=[raw.original_name],
                    )
                )

        file_keys.add(collision_key)
        result.members.append(
            _Member(
                original_name=raw.original_name,
                relative_path=relative_path,
                size=raw.size,
                compressed_size=raw.compressed_size,
                name_repaired=raw.name_repaired,
                repaired_encoding=raw.repaired_encoding,
                source_path=raw.source_path,
                source_signature=raw.source_signature,
            )
        )

    result.metadata["skipped_macos_metadata_count"] = skipped_macos_count
    result.metadata["skipped_macos_metadata_bytes"] = skipped_macos_bytes
    result.metadata["repaired_member_name_count"] = repaired_name_count
    result.metadata["repaired_member_name_encodings"] = dict(
        sorted(repaired_name_encodings.items())
    )
    if skipped_macos_count:
        result.issues.append(
            _issue(
                "MACOS_METADATA_SKIPPED",
                f"已忽略 {skipped_macos_count} 个 macOS 元数据成员。",
                severity=Severity.WARNING,
                evidence=[str(skipped_macos_count), str(skipped_macos_bytes)],
            )
        )
    if repaired_name_count:
        result.issues.append(
            _issue(
                "ZIP_MEMBER_NAME_REPAIRED",
                f"已恢复 {repaired_name_count} 个 ZIP 成员的文件名编码。",
                severity=Severity.WARNING,
                evidence=[
                    str(repaired_name_count),
                    repr(dict(sorted(repaired_name_encodings.items()))),
                ],
            )
        )

    if total_size > limits.max_total_bytes:
        result.issues.append(
            _issue(
                "TOTAL_SIZE_LIMIT",
                f"展开总大小 {total_size} 超过限制 {limits.max_total_bytes}。",
            )
        )
    if archive_bytes and total_size:
        aggregate_ratio = total_size / archive_bytes
        if aggregate_ratio > limits.max_compression_ratio:
            result.issues.append(
                _issue(
                    "COMPRESSION_RATIO_LIMIT",
                    (
                        f"归档总压缩比 {aggregate_ratio:.2f} 超过限制 "
                        f"{limits.max_compression_ratio:.2f}。"
                    ),
                )
            )

    for key in file_keys:
        parts = key.split("/")
        for end in range(1, len(parts)):
            prefix = "/".join(parts[:end])
            if prefix in file_keys:
                result.issues.append(
                    _issue(
                        "PATH_PREFIX_CONFLICT",
                        "一个文件同时被另一个文件当作父目录。",
                        evidence=[key, prefix],
                    )
                )
                break
        if key in directory_keys:
            result.issues.append(
                _issue(
                    "PATH_PREFIX_CONFLICT",
                    "同一路径同时声明为文件和目录。",
                    evidence=[key],
                )
            )

    result.members.sort(key=lambda member: (member.relative_path.casefold(), member.relative_path))
    return result


def _signature(file_stat: os.stat_result) -> tuple[int, int, int, int]:
    return (
        int(file_stat.st_dev),
        int(file_stat.st_ino),
        int(file_stat.st_size),
        int(file_stat.st_mtime_ns),
    )


def _scan_directory(input_root: Path, run_root: Path, limits: IngestLimits) -> _Preflight:
    raw_members: list[_RawMember] = []
    resolved_input = input_root.resolve(strict=True)
    resolved_run = run_root.resolve(strict=False)
    if _is_within(resolved_run, resolved_input):
        return _Preflight(
            issues=[
                _issue(
                    "RUN_DIR_INSIDE_INPUT",
                    "运行目录位于输入目录内部，会导致递归摄入。",
                    evidence=[str(input_root), str(run_root)],
                )
            ]
        )

    def visit(directory: Path) -> None:
        try:
            entries = sorted(
                os.scandir(directory),
                key=lambda entry: (entry.name.casefold(), entry.name),
            )
        except OSError as error:
            raise _IngestFailure(
                "DIRECTORY_READ_FAILED",
                f"无法读取输入目录：{error}",
                [str(directory)],
            ) from error

        for entry in entries:
            entry_path = Path(entry.path)
            relative = entry_path.relative_to(input_root).as_posix()
            try:
                # On Windows, DirEntry.stat() can expose zero device/inode values while
                # Path.lstat() and fstat() expose the stable file identity.  Use the
                # latter so the preflight-to-open race check compares like with like.
                entry_stat = entry_path.lstat()
            except OSError as error:
                raise _IngestFailure(
                    "DIRECTORY_STAT_FAILED",
                    f"无法读取文件属性：{error}",
                    [relative],
                ) from error

            is_link = entry.is_symlink() or _is_reparse_point(entry_stat)
            if is_link:
                raw_members.append(
                    _RawMember(
                        original_name=relative,
                        size=entry_stat.st_size,
                        compressed_size=None,
                        is_link=True,
                    )
                )
                continue
            if stat.S_ISDIR(entry_stat.st_mode):
                raw_members.append(
                    _RawMember(
                        original_name=relative,
                        size=0,
                        compressed_size=None,
                        is_dir=True,
                    )
                )
                visit(entry_path)
                continue
            if stat.S_ISREG(entry_stat.st_mode):
                raw_members.append(
                    _RawMember(
                        original_name=relative,
                        size=entry_stat.st_size,
                        compressed_size=None,
                        source_path=entry_path,
                        source_signature=_signature(entry_stat),
                    )
                )
                continue
            raw_members.append(
                _RawMember(
                    original_name=relative,
                    size=entry_stat.st_size,
                    compressed_size=None,
                    is_special=True,
                )
            )

    try:
        visit(input_root)
    except _IngestFailure as failure:
        return _Preflight(issues=[_issue(failure.code, failure.message, evidence=failure.evidence)])
    return _validate_members(raw_members, limits)


def _preflight_zip(path: Path, limits: IngestLimits) -> _Preflight:
    raw_members: list[_RawMember] = []
    segment_map: dict[bytes, tuple[str, str | None]] = {}
    segment_override_count = 0
    segment_issues: list[RunIssue] = []
    try:
        with zipfile.ZipFile(path) as archive:
            for info in archive.infolist():
                (
                    lookup_name,
                    output_name,
                    path_aliases,
                    repaired_encoding,
                    segment_aliases,
                ) = _zip_member_names(info)
                proposed_output_name = output_name
                (
                    output_name,
                    repaired_encoding,
                    overrides,
                    authoritative_conflicts,
                ) = _reuse_zip_segment_aliases(
                    output_name,
                    segment_aliases,
                    segment_map,
                    repaired_encoding,
                )
                segment_override_count += overrides
                if output_name != proposed_output_name:
                    path_aliases = tuple(dict.fromkeys((*path_aliases, proposed_output_name)))
                for prior_segment, proposed_segment in authoritative_conflicts:
                    segment_issues.append(
                        _issue(
                            "ZIP_SEGMENT_ALIAS_CONFLICT",
                            "同一 ZIP 原始路径段对应多个 Unicode 别名。",
                            evidence=[prior_segment, proposed_segment],
                        )
                    )
                unix_mode = (info.external_attr >> 16) & 0xFFFF
                file_type = stat.S_IFMT(unix_mode)
                is_link = stat.S_ISLNK(unix_mode)
                is_dir = info.is_dir() or stat.S_ISDIR(unix_mode)
                allowed_type = file_type in {0, stat.S_IFREG, stat.S_IFDIR, stat.S_IFLNK}
                raw_members.append(
                    _RawMember(
                        original_name=lookup_name,
                        output_name=output_name,
                        path_aliases=path_aliases,
                        name_repaired=repaired_encoding is not None,
                        repaired_encoding=repaired_encoding,
                        size=info.file_size,
                        compressed_size=info.compress_size,
                        is_dir=is_dir,
                        is_link=is_link,
                        is_special=not allowed_type,
                        encrypted=bool(info.flag_bits & 0x1),
                    )
                )
    except (OSError, zipfile.BadZipFile, NotImplementedError) as error:
        return _Preflight(
            issues=[
                _issue(
                    "ARCHIVE_INVALID",
                    f"ZIP 无法读取或格式无效：{error}",
                    evidence=[str(path)],
                )
            ]
        )
    preflight = _validate_members(raw_members, limits, archive_bytes=path.stat().st_size)
    preflight.metadata["zip_segment_alias_count"] = len(segment_map)
    preflight.metadata["zip_segment_consistency_override_count"] = segment_override_count
    preflight.issues = [*segment_issues, *preflight.issues]
    if segment_override_count:
        preflight.issues.append(
            _issue(
                "ZIP_SEGMENT_ALIAS_REUSED",
                f"已复用 {segment_override_count} 个重复 ZIP 路径段的既定编码别名。",
                severity=Severity.WARNING,
                evidence=[str(segment_override_count)],
            )
        )
    return preflight


def _preflight_rar(path: Path, limits: IngestLimits) -> _Preflight:
    try:
        import rarfile
    except ImportError as error:
        return _Preflight(
            issues=[
                _issue(
                    "DEPENDENCY_MISSING_RARFILE",
                    "缺少 rarfile，无法读取 RAR。",
                    evidence=[str(error)],
                )
            ]
        )

    raw_members: list[_RawMember] = []
    try:
        _configure_rar_backend(rarfile)
        with rarfile.RarFile(path) as archive:
            for info in archive.infolist():
                is_dir = bool(info.is_dir())
                is_file = bool(info.is_file())
                is_link = bool(info.is_symlink()) or bool(getattr(info, "file_redir", None))
                raw_members.append(
                    _RawMember(
                        original_name=info.filename,
                        size=info.file_size,
                        compressed_size=info.compress_size,
                        is_dir=is_dir,
                        is_link=is_link,
                        is_special=not (is_dir or is_file or is_link),
                        encrypted=bool(info.needs_password()),
                    )
                )
    except rarfile.RarCannotExec as error:
        return _Preflight(
            issues=[
                _issue(
                    "DEPENDENCY_MISSING_RAR_TOOL",
                    f"找不到可用的 UnRAR/7-Zip/bsdtar 后端：{error}",
                    suggested_action="请安装 UnRAR 或 7-Zip，并确保可执行文件可被发现。",
                )
            ]
        )
    except (OSError, rarfile.Error) as error:
        return _Preflight(
            issues=[
                _issue(
                    "ARCHIVE_INVALID",
                    f"RAR 无法读取或格式无效：{error}",
                    evidence=[str(path)],
                )
            ]
        )
    return _validate_members(raw_members, limits, archive_bytes=path.stat().st_size)


def _configure_rar_backend(rarfile_module: object) -> None:
    """Let rarfile discover tools installed in standard Windows locations."""

    candidates = {
        "UNRAR_TOOL": [
            shutil.which("unrar"),
            Path(os.environ.get("ProgramFiles", "C:/Program Files")) / "WinRAR" / "UnRAR.exe",
            Path(os.environ.get("ProgramFiles(x86)", "C:/Program Files (x86)"))
            / "WinRAR"
            / "UnRAR.exe",
        ],
        "SEVENZIP_TOOL": [
            shutil.which("7z"),
            Path(os.environ.get("ProgramFiles", "C:/Program Files")) / "7-Zip" / "7z.exe",
            Path(os.environ.get("ProgramFiles(x86)", "C:/Program Files (x86)"))
            / "7-Zip"
            / "7z.exe",
        ],
    }
    for attribute, paths in candidates.items():
        for candidate in paths:
            if candidate and Path(candidate).is_file():
                setattr(rarfile_module, attribute, str(candidate))
                break
    rarfile_module.tool_setup(force=True)


def _preflight_7z(path: Path, limits: IngestLimits) -> _Preflight:
    try:
        import py7zr
    except ImportError as error:
        return _Preflight(
            issues=[
                _issue(
                    "DEPENDENCY_MISSING_PY7ZR",
                    "缺少 py7zr，无法读取 7z。",
                    evidence=[str(error)],
                )
            ]
        )

    raw_members: list[_RawMember] = []
    try:
        with py7zr.SevenZipFile(path, mode="r") as archive:
            encrypted = bool(archive.needs_password())
            for info in archive.files:
                is_dir = bool(info.is_directory)
                is_link = bool(info.is_symlink or info.is_junction)
                is_special = bool(info.is_socket) or not (
                    is_dir or is_link or bool(info.archivable)
                )
                raw_members.append(
                    _RawMember(
                        original_name=info.filename,
                        size=int(info.uncompressed or 0),
                        compressed_size=(
                            int(info.compressed) if info.compressed is not None else None
                        ),
                        is_dir=is_dir,
                        is_link=is_link,
                        is_special=is_special,
                        encrypted=encrypted,
                    )
                )
    except (OSError, py7zr.Bad7zFile, ValueError) as error:
        return _Preflight(
            issues=[
                _issue(
                    "ARCHIVE_INVALID",
                    f"7z 无法读取或格式无效：{error}",
                    evidence=[str(path)],
                )
            ]
        )
    return _validate_members(raw_members, limits, archive_bytes=path.stat().st_size)


def _open_regular_file(path: Path, expected: tuple[int, int, int, int] | None) -> BinaryIO:
    try:
        before = path.lstat()
    except OSError as error:
        raise _IngestFailure(
            "SOURCE_CHANGED", f"复制前无法读取源文件：{error}", [str(path)]
        ) from error
    if path.is_symlink() or _is_reparse_point(before) or not stat.S_ISREG(before.st_mode):
        raise _IngestFailure(
            "LINK_SOURCE_REJECTED",
            "复制时源文件已变为链接或特殊文件。",
            [str(path)],
        )
    if expected is not None and _signature(before) != expected:
        raise _IngestFailure("SOURCE_CHANGED", "源文件在预检后发生变化。", [str(path)])

    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as error:
        raise _IngestFailure(
            "SOURCE_OPEN_FAILED", f"无法安全打开源文件：{error}", [str(path)]
        ) from error
    handle = os.fdopen(descriptor, "rb")
    opened = os.fstat(handle.fileno())
    source_changed = expected is not None and _signature(opened) != expected
    if not stat.S_ISREG(opened.st_mode) or source_changed:
        handle.close()
        raise _IngestFailure("SOURCE_CHANGED", "打开的源文件与预检对象不一致。", [str(path)])
    return handle


def _detect_archive_kind(
    path: Path,
    expected: tuple[int, int, int, int],
) -> Literal["zip", "rar", "7z"] | None:
    with _open_regular_file(path, expected) as source:
        return _archive_kind_from_prefix(source.read(8))


def _copy_stream(
    source: BinaryIO,
    destination: Path,
    *,
    expected_size: int,
    limits: IngestLimits,
) -> tuple[str, int]:
    destination.parent.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256()
    written = 0
    temporary = destination.with_name(f".{destination.name}.part")
    try:
        with temporary.open("xb") as output:
            while True:
                chunk = source.read(limits.copy_chunk_bytes)
                if not chunk:
                    break
                written += len(chunk)
                if written > expected_size or written > limits.max_file_bytes:
                    raise _IngestFailure(
                        "EXTRACTED_SIZE_MISMATCH",
                        "实际读取大小超过归档声明或单文件限制。",
                        [str(destination), str(expected_size), str(written)],
                    )
                output.write(chunk)
                digest.update(chunk)
        if written != expected_size:
            raise _IngestFailure(
                "EXTRACTED_SIZE_MISMATCH",
                "实际读取大小与预检大小不一致。",
                [str(destination), str(expected_size), str(written)],
            )
        os.replace(temporary, destination)
        try:
            os.utime(destination, ns=(0, 0))
        except OSError:
            pass
    except Exception:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
        raise
    return digest.hexdigest(), written


def _copy_path(
    source_path: Path,
    destination: Path,
    *,
    expected_size: int,
    limits: IngestLimits,
    expected_signature: tuple[int, int, int, int] | None = None,
) -> tuple[str, int]:
    with _open_regular_file(source_path, expected_signature) as source:
        return _copy_stream(
            source,
            destination,
            expected_size=expected_size,
            limits=limits,
        )


def _extract_zip(
    archive_path: Path,
    files_root: Path,
    members: list[_Member],
    limits: IngestLimits,
) -> list[tuple[_Member, str, int]]:
    extracted: list[tuple[_Member, str, int]] = []
    with zipfile.ZipFile(archive_path) as archive:
        for member in members:
            target = _safe_target(files_root, member.relative_path)
            with archive.open(member.original_name, mode="r") as source:
                digest, size = _copy_stream(
                    source,
                    target,
                    expected_size=member.size,
                    limits=limits,
                )
            extracted.append((member, digest, size))
    return extracted


def _extract_rar(
    archive_path: Path,
    files_root: Path,
    members: list[_Member],
    limits: IngestLimits,
) -> list[tuple[_Member, str, int]]:
    import rarfile

    _configure_rar_backend(rarfile)
    extracted: list[tuple[_Member, str, int]] = []
    with rarfile.RarFile(archive_path) as archive:
        for member in members:
            target = _safe_target(files_root, member.relative_path)
            with archive.open(member.original_name, mode="r") as source:
                digest, size = _copy_stream(
                    source,
                    target,
                    expected_size=member.size,
                    limits=limits,
                )
            extracted.append((member, digest, size))
    return extracted


def _seven_zip_executable() -> Path | None:
    candidates = [
        shutil.which("7z"),
        Path(os.environ.get("ProgramFiles", "C:/Program Files")) / "7-Zip" / "7z.exe",
        Path(os.environ.get("ProgramFiles(x86)", "C:/Program Files (x86)")) / "7-Zip" / "7z.exe",
    ]
    for candidate in candidates:
        if candidate and Path(candidate).is_file():
            return Path(candidate).resolve()
    return None


def _extract_7z(
    archive_path: Path,
    files_root: Path,
    members: list[_Member],
    limits: IngestLimits,
) -> list[tuple[_Member, str, int]]:
    executable = _seven_zip_executable()
    if executable is None:
        raise _IngestFailure(
            "DEPENDENCY_MISSING_7ZIP",
            "7z 必须通过可流式限制的 7-Zip 后端解包；未找到 7z.exe。",
            [str(archive_path)],
        )

    extracted: list[tuple[_Member, str, int]] = []
    creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
    for member in members:
        target = _safe_target(files_root, member.relative_path)
        with tempfile.TemporaryFile() as error_output:
            process = subprocess.Popen(
                [
                    str(executable),
                    "x",
                    "-so",
                    "-bd",
                    "-y",
                    "-spd",
                    str(archive_path),
                    member.original_name,
                ],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=error_output,
                creationflags=creationflags,
            )
            assert process.stdout is not None
            try:
                digest, size = _copy_stream(
                    process.stdout,
                    target,
                    expected_size=member.size,
                    limits=limits,
                )
            except Exception:
                process.kill()
                process.wait(timeout=30)
                raise
            finally:
                process.stdout.close()
            return_code = process.wait(timeout=120)
            if return_code != 0:
                target.unlink(missing_ok=True)
                error_output.seek(0)
                detail = error_output.read(8_000).decode("utf-8", errors="replace")
                raise _IngestFailure(
                    "ARCHIVE_EXTRACT_FAILED",
                    f"7z 成员流式解包失败，退出码{return_code}。",
                    [member.original_name, detail],
                )
            extracted.append((member, digest, size))
    return extracted


def _media_type(relative_path: str) -> str | None:
    suffix = PurePosixPath(relative_path).suffix.lower()
    return _MEDIA_TYPES.get(suffix) or mimetypes.guess_type(relative_path)[0]


def _source_record(
    member: _Member,
    digest: str,
    size: int,
    final_files_root: Path,
    *,
    input_kind: str,
    container_sha256: str | None,
) -> SourceFile:
    metadata: dict[str, object] = {"source_kind": f"{input_kind}_member"}
    archive_member: str | None = None
    if input_kind == "directory":
        metadata["source_kind"] = "directory_file"
    elif input_kind == "file":
        metadata["source_kind"] = "single_file"
    else:
        archive_member = member.original_name
        metadata["container_sha256"] = container_sha256
        metadata["archive_member_normalized"] = member.relative_path
        metadata["archive_member_name_repaired"] = member.name_repaired
        metadata["archive_member_repaired_encoding"] = member.repaired_encoding
    path_digest = hashlib.sha256(member.relative_path.encode("utf-8")).hexdigest()[:16]
    return SourceFile(
        # Content hashes remain separately queryable, while the path suffix
        # prevents two byte-identical drawings in different folders/revisions
        # from overwriting each other's sheets and entities.
        id=f"file:{digest}:{path_digest}",
        relative_path=member.relative_path,
        absolute_path=str(_safe_target(final_files_root, member.relative_path).resolve()),
        sha256=digest,
        bytes=size,
        suffix=PurePosixPath(member.relative_path).suffix.lower(),
        media_type=_media_type(member.relative_path),
        archive_member=archive_member,
        status=ReviewStatus.PASS,
        metadata=metadata,
    )


def _tree_digest(records: list[SourceFile]) -> str:
    digest = hashlib.sha256(b"cadquote-directory-v1\x00")
    for record in records:
        digest.update(record.relative_path.encode("utf-8"))
        digest.update(b"\x00")
        digest.update(record.sha256.encode("ascii"))
        digest.update(b"\n")
    return digest.hexdigest()


def _base_result(
    input_path: Path,
    run_root: Path,
    input_kind: Literal["directory", "file", "zip", "rar", "7z", "unsupported"],
) -> IngestResult:
    ingest_root = run_root / "ingest"
    return IngestResult(
        input_path=str(input_path.resolve(strict=False)),
        input_kind=input_kind,
        ingest_dir=str(ingest_root.resolve(strict=False)),
        extracted_dir=str((ingest_root / "files").resolve(strict=False)),
    )


def _publish_ingest_directory(stage: Path, destination: Path) -> int:
    """Atomically publish a staged tree, tolerating short Windows scanner locks.

    Antivirus/indexing products can briefly open a newly extracted DWG without
    ``FILE_SHARE_DELETE``.  Windows then rejects the parent-directory rename with
    WinError 5/32/33.  Retrying the same atomic rename is safe: the destination is
    checked before every attempt, and no partial final directory is ever exposed.
    """

    last_error: OSError | None = None
    for attempt in range(len(_PUBLISH_RETRY_DELAYS_SECONDS) + 1):
        if destination.exists() or destination.is_symlink():
            raise _IngestFailure(
                "INGEST_DESTINATION_EXISTS",
                "发布结果时发现 ingest 已存在；拒绝覆盖并发或既有结果。",
                [str(destination)],
            )
        try:
            os.replace(stage, destination)
            return attempt
        except OSError as error:
            last_error = error
            winerror = getattr(error, "winerror", None)
            transient = (
                isinstance(error, PermissionError)
                or winerror in _TRANSIENT_PUBLISH_WINERRORS
                or error.errno in {errno.EACCES, errno.EPERM, errno.EBUSY}
            )
            if not transient or attempt >= len(_PUBLISH_RETRY_DELAYS_SECONDS):
                break
            time.sleep(_PUBLISH_RETRY_DELAYS_SECONDS[attempt])

    assert last_error is not None
    raise _IngestFailure(
        "INGEST_PUBLISH_FAILED",
        (
            "暂存目录已完整生成，但原子发布持续被系统拒绝；"
            "可能有杀毒、索引或同步程序短暂占用新文件。"
        ),
        [
            str(stage),
            str(destination),
            f"winerror={getattr(last_error, 'winerror', None)}",
            f"errno={last_error.errno}",
            str(last_error),
        ],
    ) from last_error


def ingest_input(
    input_path: str | Path,
    run_dir: str | Path,
    limits: IngestLimits | None = None,
) -> IngestResult:
    """Create a verified, immutable-by-convention snapshot inside ``run_dir``.

    Security rejections and ordinary input failures are returned as structured
    ``RunIssue`` records.  The function raises only for invalid Python arguments or
    catastrophic process errors.  If ``succeeded`` is false, the final ingest
    directory is never published and no partial member files remain.
    """

    limits = limits or IngestLimits()
    source = Path(input_path).expanduser()
    run_root = Path(run_dir).expanduser()

    if not source.exists():
        result = _base_result(source, run_root, "unsupported")
        result.issues.append(_issue("INPUT_NOT_FOUND", "输入路径不存在。", evidence=[str(source)]))
        return result

    try:
        source_stat = source.lstat()
    except OSError as error:
        result = _base_result(source, run_root, "unsupported")
        result.issues.append(
            _issue("INPUT_STAT_FAILED", f"无法读取输入属性：{error}", evidence=[str(source)])
        )
        return result

    if source.is_symlink() or _is_reparse_point(source_stat):
        result = _base_result(source, run_root, "unsupported")
        result.issues.append(
            _issue(
                "LINK_SOURCE_REJECTED",
                "输入根路径不能是链接或 junction。",
                evidence=[str(source)],
            )
        )
        return result

    if source.is_dir():
        input_kind: Literal["directory", "file", "zip", "rar", "7z", "unsupported"] = "directory"
        format_metadata: dict[str, object] = {
            "input_extension": source.suffix.lower(),
            "detected_format": "directory",
            "format_detection": "filesystem",
            "extension_mismatch": False,
        }
    elif source.is_file():
        suffix = source.suffix.lower()
        declared_archive_kind = _ARCHIVE_SUFFIXES.get(suffix)
        if suffix in _SINGLE_FILE_SUFFIXES:
            input_kind = "file"
            detected_archive_kind = None
            detection_method = "single_file_suffix"
        else:
            try:
                detected_archive_kind = _detect_archive_kind(source, _signature(source_stat))
            except _IngestFailure as failure:
                result = _base_result(source, run_root, "unsupported")
                result.issues.append(
                    _issue(failure.code, failure.message, evidence=failure.evidence)
                )
                return result
            input_kind = detected_archive_kind or declared_archive_kind or "unsupported"
            detection_method = "signature" if detected_archive_kind else "extension_fallback"
        extension_mismatch = detected_archive_kind is not None and (
            declared_archive_kind != detected_archive_kind
        )
        format_metadata = {
            "input_extension": suffix,
            "declared_archive_format": declared_archive_kind,
            "detected_format": detected_archive_kind or input_kind,
            "format_detection": detection_method,
            "extension_mismatch": extension_mismatch,
        }
    else:
        input_kind = "unsupported"
        format_metadata = {
            "input_extension": source.suffix.lower(),
            "detected_format": "unsupported",
            "format_detection": "filesystem",
            "extension_mismatch": False,
        }

    result = _base_result(source, run_root, input_kind)
    result.metadata.update(format_metadata)
    if bool(format_metadata.get("extension_mismatch")):
        result.issues.append(
            _issue(
                "ARCHIVE_EXTENSION_MISMATCH",
                (f"文件扩展名与归档签名不一致；已按实际 {input_kind.upper()} 格式安全处理。"),
                severity=Severity.WARNING,
                evidence=[source.name, source.suffix.lower() or "(none)", input_kind],
            )
        )
    if input_kind == "unsupported":
        result.issues.append(
            _issue(
                "INPUT_FORMAT_UNSUPPORTED",
                "仅支持目录、ZIP、RAR、7z及DWG/DXF/PDF/表格/图片文件输入。",
                evidence=[str(source)],
            )
        )
        return result

    final_ingest_root = run_root / "ingest"
    if final_ingest_root.exists() or final_ingest_root.is_symlink():
        result.issues.append(
            _issue(
                "INGEST_DESTINATION_EXISTS",
                "运行目录中的 ingest 已存在；拒绝覆盖既有结果。",
                evidence=[str(final_ingest_root)],
                suggested_action="请为本次处理创建新的 run 目录。",
            )
        )
        return result

    archive_sha256: str | None = None
    archive_size: int | None = None
    if input_kind == "directory":
        preflight = _scan_directory(source, run_root, limits)
    elif input_kind == "file":
        archive_size = source_stat.st_size
        if archive_size > limits.max_file_bytes:
            result.issues.append(
                _issue(
                    "FILE_SIZE_LIMIT",
                    f"文件大小 {archive_size} 超过限制 {limits.max_file_bytes}。",
                    evidence=[str(source)],
                )
            )
            return result
        archive_sha256 = sha256_file(source)
        preflight = _validate_members(
            [
                _RawMember(
                    original_name=source.name,
                    size=archive_size,
                    compressed_size=archive_size,
                    source_path=source,
                    source_signature=_signature(source_stat),
                )
            ],
            limits,
        )
        preflight.input_sha256 = archive_sha256
    else:
        archive_size = source_stat.st_size
        if archive_size > limits.max_archive_bytes:
            result.issues.append(
                _issue(
                    "ARCHIVE_SIZE_LIMIT",
                    f"归档大小 {archive_size} 超过限制 {limits.max_archive_bytes}。",
                    evidence=[str(source)],
                )
            )
            return result
        archive_sha256 = sha256_file(source)
        if input_kind == "zip":
            preflight = _preflight_zip(source, limits)
        elif input_kind == "rar":
            preflight = _preflight_rar(source, limits)
        else:
            preflight = _preflight_7z(source, limits)
        preflight.input_sha256 = archive_sha256

    result.issues.extend(preflight.issues)
    result.input_sha256 = preflight.input_sha256
    result.metadata.update(preflight.metadata)
    if _has_blocking_issue(result.issues):
        return result

    try:
        run_root.mkdir(parents=True, exist_ok=True)
        resolved_run = run_root.resolve(strict=True)
        stage = Path(tempfile.mkdtemp(prefix=".ingest-", dir=resolved_run))
        if not _is_within(stage.resolve(strict=True), resolved_run):
            raise _IngestFailure("PATH_ESCAPE", "暂存目录未创建在运行目录中。")
    except (OSError, _IngestFailure) as error:
        if isinstance(error, _IngestFailure):
            code, message, evidence = error.code, error.message, error.evidence
        else:
            code, message, evidence = "RUN_DIR_CREATE_FAILED", str(error), [str(run_root)]
        result.issues.append(_issue(code, message, evidence=evidence))
        return result

    stage_files = stage / "files"
    stage_original = stage / "original"
    stage_files.mkdir()
    copied_original: Path | None = None
    try:
        if input_kind != "directory":
            assert archive_size is not None and archive_sha256 is not None
            stage_original.mkdir()
            copied_original = stage_original / f"input{source.suffix.lower()}"
            copied_hash, copied_size = _copy_path(
                source,
                copied_original,
                expected_size=archive_size,
                limits=IngestLimits(
                    **{
                        **limits.model_dump(),
                        "max_file_bytes": max(limits.max_file_bytes, limits.max_archive_bytes),
                    }
                ),
                expected_signature=_signature(source_stat),
            )
            if copied_hash != archive_sha256 or copied_size != archive_size:
                raise _IngestFailure(
                    "SOURCE_CHANGED",
                    "归档在预检与复制之间发生变化。",
                    [str(source)],
                )

        if input_kind in {"directory", "file"}:
            extracted: list[tuple[_Member, str, int]] = []
            for member in preflight.members:
                if member.source_path is None:
                    raise _IngestFailure(
                        "INTERNAL_INGEST_ERROR", "目录成员缺少源路径。", [member.relative_path]
                    )
                target = _safe_target(stage_files, member.relative_path)
                digest, size = _copy_path(
                    member.source_path,
                    target,
                    expected_size=member.size,
                    limits=limits,
                    expected_signature=member.source_signature,
                )
                extracted.append((member, digest, size))
        elif input_kind == "zip":
            assert copied_original is not None
            extracted = _extract_zip(copied_original, stage_files, preflight.members, limits)
        elif input_kind == "rar":
            assert copied_original is not None
            extracted = _extract_rar(copied_original, stage_files, preflight.members, limits)
        else:
            assert copied_original is not None
            extracted = _extract_7z(copied_original, stage_files, preflight.members, limits)

        actual_total = sum(size for _, _, size in extracted)
        if actual_total > limits.max_total_bytes:
            raise _IngestFailure(
                "TOTAL_SIZE_LIMIT",
                f"实际展开大小 {actual_total} 超过限制 {limits.max_total_bytes}。",
            )

        final_files_root = final_ingest_root / "files"
        records = [
            _source_record(
                member,
                digest,
                size,
                final_files_root,
                input_kind=input_kind,
                container_sha256=archive_sha256,
            )
            for member, digest, size in extracted
        ]
        records.sort(key=lambda record: (record.relative_path.casefold(), record.relative_path))
        if input_kind == "directory":
            result.input_sha256 = _tree_digest(records)

        publish_retry_count = _publish_ingest_directory(stage, final_ingest_root)
        result.files = records
        result.succeeded = True
        if input_kind != "directory":
            result.original_copy = str(
                (final_ingest_root / "original" / f"input{source.suffix.lower()}").resolve()
            )
        result.metadata = {
            **result.metadata,
            "file_count": len(records),
            "total_uncompressed_bytes": actual_total,
            "normalized_paths": True,
            "originals_modified": False,
            "publish_method": "atomic_directory_rename",
            "publish_retry_count": publish_retry_count,
        }
        if publish_retry_count:
            result.issues.append(
                _issue(
                    "INGEST_PUBLISH_RETRIED",
                    f"原子发布曾被占用，重试 {publish_retry_count} 次后成功。",
                    severity=Severity.WARNING,
                    evidence=[str(publish_retry_count)],
                )
            )
        if not records:
            result.issues.append(
                _issue(
                    "INPUT_EMPTY",
                    "输入中没有可处理的普通文件。",
                    severity=Severity.WARNING,
                )
            )
        return result
    except _IngestFailure as failure:
        result.issues.append(_issue(failure.code, failure.message, evidence=failure.evidence))
    except (OSError, zipfile.BadZipFile, EOFError, ValueError) as error:
        result.issues.append(
            _issue(
                "INGEST_EXTRACTION_FAILED",
                f"复制或解包失败：{error}",
                evidence=[str(source)],
            )
        )
    except Exception as error:  # Archive backends expose several optional exception types.
        result.issues.append(
            _issue(
                "INGEST_EXTRACTION_FAILED",
                f"复制或解包失败：{type(error).__name__}: {error}",
                evidence=[str(source)],
            )
        )
    finally:
        if stage.exists():
            shutil.rmtree(stage, ignore_errors=True)
    return result


__all__ = ["IngestLimits", "IngestResult", "ingest_input"]
