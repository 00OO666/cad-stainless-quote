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

import hashlib
import mimetypes
import os
import re
import shutil
import stat
import subprocess
import tempfile
import unicodedata
import zipfile
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
    source_path: Path | None = None
    source_signature: tuple[int, int, int, int] | None = None


@dataclass(slots=True)
class _Preflight:
    members: list[_Member] = dataclass_field(default_factory=list)
    issues: list[RunIssue] = dataclass_field(default_factory=list)
    input_sha256: str | None = None


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
}

_SINGLE_FILE_SUFFIXES = {
    ".dwg",
    ".dxf",
    ".pdf",
    ".xls",
    ".xlsx",
    ".xlsm",
    ".png",
    ".jpg",
    ".jpeg",
    ".tif",
    ".tiff",
}


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

    for raw in raw_members:
        try:
            relative_path = _normalize_relative_path(raw.original_name, limits)
        except _IngestFailure as failure:
            result.issues.append(_issue(failure.code, failure.message, evidence=failure.evidence))
            continue

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
                source_path=raw.source_path,
                source_signature=raw.source_signature,
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
    alias_issues: list[RunIssue] = []
    try:
        with zipfile.ZipFile(path) as archive:
            for info in archive.infolist():
                # Python applies the Info-ZIP Unicode Path extra field to ``filename``
                # while retaining the legacy CP437-decoded name in ``orig_filename``.
                # The former is the archive lookup key.  Validate both so a malicious
                # alias cannot hide traversal, but extract by the canonical lookup key.
                original_name = info.filename
                legacy_name = getattr(info, "orig_filename", original_name)
                if legacy_name != original_name:
                    try:
                        _normalize_relative_path(legacy_name, limits)
                    except _IngestFailure as failure:
                        alias_issues.append(
                            _issue(failure.code, failure.message, evidence=failure.evidence)
                        )
                unix_mode = (info.external_attr >> 16) & 0xFFFF
                file_type = stat.S_IFMT(unix_mode)
                is_link = stat.S_ISLNK(unix_mode)
                is_dir = info.is_dir() or stat.S_ISDIR(unix_mode)
                allowed_type = file_type in {0, stat.S_IFREG, stat.S_IFDIR, stat.S_IFLNK}
                raw_members.append(
                    _RawMember(
                        original_name=original_name,
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
    preflight.issues = [*alias_issues, *preflight.issues]
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
        Path(os.environ.get("ProgramFiles(x86)", "C:/Program Files (x86)"))
        / "7-Zip"
        / "7z.exe",
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
        input_kind: Literal[
            "directory", "file", "zip", "rar", "7z", "unsupported"
        ] = "directory"
    elif source.is_file():
        suffix = source.suffix.lower()
        input_kind = {".zip": "zip", ".rar": "rar", ".7z": "7z"}.get(
            suffix,
            "file" if suffix in _SINGLE_FILE_SUFFIXES else "unsupported",
        )
    else:
        input_kind = "unsupported"

    result = _base_result(source, run_root, input_kind)
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

        os.replace(stage, final_ingest_root)
        result.files = records
        result.succeeded = True
        if input_kind != "directory":
            result.original_copy = str(
                (final_ingest_root / "original" / f"input{source.suffix.lower()}").resolve()
            )
        result.metadata = {
            "file_count": len(records),
            "total_uncompressed_bytes": actual_total,
            "normalized_paths": True,
            "originals_modified": False,
        }
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
