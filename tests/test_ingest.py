from __future__ import annotations

import errno
import hashlib
import json
import os
import stat
import struct
import subprocess
import sys
import zipfile
import zlib
from pathlib import Path

import pytest

SCRIPT_ROOT = Path(__file__).parents[1] / "skills" / "cad-stainless-quote" / "scripts"
sys.path.insert(0, str(SCRIPT_ROOT))

import cadquote.ingest as ingest_module  # noqa: E402
from cadquote.ingest import IngestLimits, ingest_input  # noqa: E402
from cadquote.models import ReviewStatus  # noqa: E402


def _issue_codes(result: object) -> set[str]:
    return {issue.code for issue in result.issues}


def _write_zip(path: Path, members: list[tuple[str, bytes]]) -> None:
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, payload in members:
            archive.writestr(name, payload)


def _write_misflagged_utf8_zip(path: Path, name: str, payload: bytes) -> None:
    """Write UTF-8 filename bytes, then emulate tools that forgot ZIP bit 11."""

    _write_zip(path, [(name, payload)])
    with zipfile.ZipFile(path) as archive:
        local_offsets = [info.header_offset for info in archive.infolist()]
        central_offset = archive.start_dir

    raw = bytearray(path.read_bytes())
    for offset in local_offsets:
        assert raw[offset : offset + 4] == b"PK\x03\x04"
        flags = struct.unpack_from("<H", raw, offset + 6)[0] & ~0x800
        struct.pack_into("<H", raw, offset + 6, flags)

    offset = central_offset
    while raw[offset : offset + 4] == b"PK\x01\x02":
        flags = struct.unpack_from("<H", raw, offset + 8)[0] & ~0x800
        struct.pack_into("<H", raw, offset + 8, flags)
        name_size, extra_size, comment_size = struct.unpack_from("<HHH", raw, offset + 28)
        offset += 46 + name_size + extra_size + comment_size
    path.write_bytes(raw)


def _unicode_path_extra(raw_name: str, unicode_name: str) -> bytes:
    raw_name_bytes = raw_name.encode("cp437")
    payload = (
        b"\x01"
        + struct.pack("<I", zlib.crc32(raw_name_bytes) & 0xFFFFFFFF)
        + unicode_name.encode("utf-8")
    )
    return struct.pack("<HH", 0x7075, len(payload)) + payload


def _write_raw_name_zip(path: Path, raw_name: bytes, payload: bytes) -> None:
    """Create a single-member stored ZIP with exact legacy filename bytes."""

    _write_raw_names_zip(path, [(raw_name, payload)])


def _write_raw_names_zip(path: Path, members: list[tuple[bytes, bytes]]) -> None:
    """Create a stored ZIP with exact no-flag legacy filename bytes."""

    local_records: list[bytes] = []
    central_records: list[bytes] = []
    local_offset = 0
    for raw_name, payload in members:
        crc = zlib.crc32(payload) & 0xFFFFFFFF
        local_header = struct.pack(
            "<4s5H3I2H",
            b"PK\x03\x04",
            20,
            0,
            0,
            0,
            0,
            crc,
            len(payload),
            len(payload),
            len(raw_name),
            0,
        )
        local_record = local_header + raw_name + payload
        local_records.append(local_record)
        central_header = struct.pack(
            "<4s6H3I5H2I",
            b"PK\x01\x02",
            20,
            20,
            0,
            0,
            0,
            0,
            crc,
            len(payload),
            len(payload),
            len(raw_name),
            0,
            0,
            0,
            0,
            0,
            local_offset,
        )
        central_records.append(central_header + raw_name)
        local_offset += len(local_record)

    local_data = b"".join(local_records)
    central_data = b"".join(central_records)
    end_record = struct.pack(
        "<4s4H2IH",
        b"PK\x05\x06",
        0,
        0,
        len(members),
        len(members),
        len(central_data),
        len(local_data),
        0,
    )
    path.write_bytes(local_data + central_data + end_record)


def test_directory_ingest_is_deterministic_and_preserves_source(tmp_path: Path) -> None:
    source = tmp_path / "source"
    (source / "立面").mkdir(parents=True)
    (source / "B.dwg").write_bytes(b"second")
    (source / "立面" / "A.dxf").write_bytes(b"first")
    before = {
        path.relative_to(source).as_posix(): path.read_bytes()
        for path in source.rglob("*")
        if path.is_file()
    }

    first = ingest_input(source, tmp_path / "run-one")
    second = ingest_input(source, tmp_path / "run-two")

    assert first.succeeded is True
    assert second.succeeded is True
    assert first.input_sha256 == second.input_sha256
    assert [record.relative_path for record in first.files] == ["B.dwg", "立面/A.dxf"]
    assert all(record.status == ReviewStatus.PASS for record in first.files)
    assert all(record.id.startswith(f"file:{record.sha256}:") for record in first.files)
    assert all(
        Path(record.absolute_path).is_relative_to(Path(first.extracted_dir))
        for record in first.files
    )
    assert {
        path.relative_to(source).as_posix(): path.read_bytes()
        for path in source.rglob("*")
        if path.is_file()
    } == before
    assert first.model_dump(mode="json") == json.loads(first.model_dump_json())


def test_identical_content_at_different_paths_has_distinct_source_ids(tmp_path: Path) -> None:
    source = tmp_path / "source"
    (source / "revision-a").mkdir(parents=True)
    (source / "revision-b").mkdir(parents=True)
    payload = b"same-drawing-bytes"
    (source / "revision-a" / "plan.dwg").write_bytes(payload)
    (source / "revision-b" / "plan.dwg").write_bytes(payload)

    result = ingest_input(source, tmp_path / "run")

    assert result.succeeded is True
    assert len({record.sha256 for record in result.files}) == 1
    assert len({record.id for record in result.files}) == 2


def test_single_cad_file_is_snapshotted(tmp_path: Path) -> None:
    source = tmp_path / "单张图.dxf"
    source.write_bytes(b"0\nSECTION\n2\nHEADER\n0\nENDSEC\n0\nEOF\n")

    result = ingest_input(source, tmp_path / "single-run")

    assert result.succeeded is True
    assert result.input_kind == "file"
    assert [record.relative_path for record in result.files] == [source.name]
    assert Path(result.files[0].absolute_path).read_bytes() == source.read_bytes()
    assert result.files[0].metadata["source_kind"] == "single_file"
    assert result.original_copy is not None


def test_directory_symlink_is_rejected_without_copying_target(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    outside = tmp_path / "outside-secret.txt"
    outside.write_text("secret", encoding="utf-8")
    source = tmp_path / "source"
    source.mkdir()
    link = source / "linked-secret.txt"
    try:
        link.symlink_to(outside)
    except OSError:
        # Windows without Developer Mode cannot create a test symlink.  Simulate
        # DirEntry's no-follow classification while retaining a real lstat target,
        # so the rejection path remains covered on every test host.
        link.write_text("placeholder", encoding="utf-8")

        class _LinkEntry:
            name = link.name
            path = str(link)

            @staticmethod
            def is_symlink() -> bool:
                return True

        monkeypatch.setattr(os, "scandir", lambda _path: [_LinkEntry()])

    run = tmp_path / "run"
    result = ingest_input(source, run)

    assert result.succeeded is False
    assert "LINK_MEMBER_REJECTED" in _issue_codes(result)
    assert not (run / "ingest").exists()
    assert outside.read_text(encoding="utf-8") == "secret"


def test_run_directory_inside_source_is_rejected(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "drawing.dwg").write_bytes(b"dwg")

    result = ingest_input(source, source / "runs" / "one")

    assert result.succeeded is False
    assert "RUN_DIR_INSIDE_INPUT" in _issue_codes(result)
    assert not (source / "runs").exists()


def test_zip_ingest_normalizes_order_hashes_and_preserves_archive(tmp_path: Path) -> None:
    source = tmp_path / "project.zip"
    _write_zip(source, [("图纸/B.dwg", b"B"), ("图纸/A.dwg", b"A")])
    source_hash = hashlib.sha256(source.read_bytes()).hexdigest()

    result = ingest_input(source, tmp_path / "run")

    assert result.succeeded is True
    assert result.input_sha256 == source_hash
    assert [record.relative_path for record in result.files] == ["图纸/A.dwg", "图纸/B.dwg"]
    assert [Path(record.absolute_path).read_bytes() for record in result.files] == [b"A", b"B"]
    assert all(record.archive_member for record in result.files)
    assert Path(result.original_copy).read_bytes() == source.read_bytes()
    assert source.read_bytes() == Path(result.original_copy).read_bytes()


def test_archive_signature_overrides_wrong_rar_extension(tmp_path: Path) -> None:
    source = tmp_path / "renamed-project.rar"
    _write_zip(source, [("drawings/plan.dwg", b"zip-content")])

    result = ingest_input(source, tmp_path / "run")

    assert result.succeeded is True, result.model_dump(mode="json")
    assert result.input_kind == "zip"
    assert [record.relative_path for record in result.files] == ["drawings/plan.dwg"]
    assert Path(result.files[0].absolute_path).read_bytes() == b"zip-content"
    assert result.metadata["detected_format"] == "zip"
    assert result.metadata["declared_archive_format"] == "rar"
    assert result.metadata["extension_mismatch"] is True
    assert "ARCHIVE_EXTENSION_MISMATCH" in _issue_codes(result)
    assert all(issue.severity.value != "BLOCK" for issue in result.issues)


def test_zip_repairs_utf8_name_decoded_as_cp437(tmp_path: Path) -> None:
    source = tmp_path / "legacy.zip"
    _write_misflagged_utf8_zip(source, "图纸/平面图.dwg", b"drawing")

    result = ingest_input(source, tmp_path / "run")

    assert result.succeeded is True, result.model_dump(mode="json")
    assert [record.relative_path for record in result.files] == ["图纸/平面图.dwg"]
    assert result.files[0].archive_member != result.files[0].relative_path
    assert result.files[0].metadata["archive_member_normalized"] == "图纸/平面图.dwg"
    assert result.files[0].metadata["archive_member_name_repaired"] is True
    assert result.files[0].metadata["archive_member_repaired_encoding"] == "utf-8"
    assert result.metadata["repaired_member_name_count"] == 1
    assert result.metadata["repaired_member_name_encodings"] == {"utf-8": 1}
    assert "ZIP_MEMBER_NAME_REPAIRED" in _issue_codes(result)


def test_zip_repairs_gbk_name_but_reads_with_original_lookup_key(tmp_path: Path) -> None:
    source = tmp_path / "legacy-gbk.zip"
    expected_name = "项目图纸/平面施工图.dwg"
    _write_raw_name_zip(source, expected_name.encode("gbk"), b"gbk-drawing")
    with zipfile.ZipFile(source) as archive:
        lookup_name = archive.infolist()[0].filename
    assert lookup_name != expected_name

    result = ingest_input(source, tmp_path / "run")

    assert result.succeeded is True, result.model_dump(mode="json")
    assert [record.relative_path for record in result.files] == [expected_name]
    assert result.files[0].archive_member == lookup_name
    assert Path(result.files[0].absolute_path).read_bytes() == b"gbk-drawing"
    assert Path(result.files[0].absolute_path).parent.name == "项目图纸"
    assert result.files[0].metadata["archive_member_normalized"] == expected_name
    assert result.files[0].metadata["archive_member_repaired_encoding"] == "gb18030"
    assert result.metadata["repaired_member_name_encodings"] == {"gb18030": 1}
    assert "ZIP_MEMBER_NAME_REPAIRED" in _issue_codes(result)


def test_zip_prefers_utf8_and_keeps_shared_segments_in_one_directory(tmp_path: Path) -> None:
    source = tmp_path / "mixed-members.zip"
    top = "01公共施工图纸"
    names = [
        f"{top}/1F/001 总平面图.dwg",
        f"{top}/1F/002 立面图.dwg",
        f"{top}/1F/003 服务台.dwg",
        f"{top}/8F/004 节点图.dwg",
    ]
    _write_raw_names_zip(
        source,
        [(name.encode("utf-8"), f"drawing-{index}".encode()) for index, name in enumerate(names)],
    )

    result = ingest_input(source, tmp_path / "run")

    assert result.succeeded is True, result.model_dump(mode="json")
    assert {record.relative_path.split("/", 1)[0] for record in result.files} == {top}
    assert [record.relative_path for record in result.files] == sorted(names)
    assert result.metadata["repaired_member_name_encodings"] == {"utf-8": 4}
    assert result.metadata["zip_segment_consistency_override_count"] == 0
    assert result.metadata["zip_segment_alias_count"] >= 7
    assert all(Path(record.absolute_path).is_file() for record in result.files)


def test_repeated_raw_zip_segment_reuses_first_safe_alias() -> None:
    raw_top = "统一目录".encode()
    segment_map: dict[bytes, tuple[str, str | None]] = {}
    first = ingest_module._reuse_zip_segment_aliases(
        "统一目录/一层.dwg",
        ((raw_top, "统一目录"), (b"one", "一层.dwg")),
        segment_map,
        "utf-8",
    )
    second = ingest_module._reuse_zip_segment_aliases(
        "错误目录/二层.dwg",
        ((raw_top, "错误目录"), (b"two", "二层.dwg")),
        segment_map,
        "gb18030",
    )

    assert first[0] == "统一目录/一层.dwg"
    assert second[0] == "统一目录/二层.dwg"
    assert second[2] == 1


def test_ingest_cli_escapes_cp437_lookup_for_gbk_console(tmp_path: Path) -> None:
    source = tmp_path / "legacy-gbk.zip"
    expected_name = "项目图纸/平面施工图.dwg"
    _write_raw_name_zip(source, expected_name.encode("gbk"), b"gbk-drawing")
    run = tmp_path / "cli-run"
    environment = os.environ.copy()
    environment["PYTHONIOENCODING"] = "gbk"

    completed = subprocess.run(
        [
            sys.executable,
            str(SCRIPT_ROOT / "cad_quote.py"),
            "ingest",
            str(source),
            "--out",
            str(run),
        ],
        check=False,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        encoding="ascii",
        env=environment,
        timeout=30,
    )

    assert completed.returncode == 0, completed.stderr
    payload = json.loads(completed.stdout)
    assert payload["succeeded"] is True
    assert payload["files"][0]["relative_path"] == expected_name
    assert payload["metadata"]["repaired_member_name_encodings"] == {"gb18030": 1}


@pytest.mark.parametrize("legacy_name", ["éé.dwg", "╔═╗╚.dwg", "café.dwg"])
def test_zip_does_not_reinterpret_legitimate_cp437_name(tmp_path: Path, legacy_name: str) -> None:
    source = tmp_path / "legitimate-cp437.zip"
    _write_raw_name_zip(source, legacy_name.encode("cp437"), b"cp437-drawing")

    result = ingest_input(source, tmp_path / "run")

    assert result.succeeded is True, result.model_dump(mode="json")
    assert [record.relative_path for record in result.files] == [legacy_name]
    assert result.files[0].metadata["archive_member_name_repaired"] is False
    assert result.files[0].metadata["archive_member_repaired_encoding"] is None
    assert result.metadata["repaired_member_name_count"] == 0


@pytest.mark.parametrize(
    ("lookup_name", "output_name"),
    [
        ("safe.dwg", "../escaped.dwg"),
        ("../escaped.dwg", "safe.dwg"),
    ],
)
def test_zip_validates_lookup_and_unicode_output_aliases(
    tmp_path: Path, lookup_name: str, output_name: str
) -> None:
    source = tmp_path / "aliases.zip"
    info = zipfile.ZipInfo(lookup_name)
    info.extra = _unicode_path_extra(lookup_name, output_name)
    with zipfile.ZipFile(source, "w") as archive:
        archive.writestr(info, b"unsafe-alias")

    result = ingest_input(source, tmp_path / "run")

    assert result.succeeded is False
    assert "PATH_TRAVERSAL" in _issue_codes(result)
    assert not (tmp_path / "run" / "ingest").exists()
    assert not (tmp_path / "escaped.dwg").exists()


def test_macos_metadata_members_are_skipped_and_counted(tmp_path: Path) -> None:
    source = tmp_path / "finder-export.zip"
    _write_zip(
        source,
        [
            ("__MACOSX/._sheet.dwg", b"x" * 176),
            ("project/._plan.dwg", b"y" * 232),
            ("project/.DS_Store", b"finder"),
            ("project/plan.dwg", b"real-dwg"),
        ],
    )

    result = ingest_input(source, tmp_path / "run")

    assert result.succeeded is True, result.model_dump(mode="json")
    assert [record.relative_path for record in result.files] == ["project/plan.dwg"]
    assert result.metadata["file_count"] == 1
    assert result.metadata["skipped_macos_metadata_count"] == 3
    assert result.metadata["skipped_macos_metadata_bytes"] == 176 + 232 + len(b"finder")
    assert "MACOS_METADATA_SKIPPED" in _issue_codes(result)
    assert not any(
        record.suffix == ".dwg" and record.bytes in {176, 232} for record in result.files
    )


@pytest.mark.parametrize(
    ("unsafe_name", "expected_code"),
    [
        ("../escaped.txt", "PATH_TRAVERSAL"),
        ("safe/../../escaped.txt", "PATH_TRAVERSAL"),
        ("/absolute.txt", "PATH_ABSOLUTE"),
        (r"C:\absolute.txt", "PATH_ABSOLUTE"),
        (r"\\server\share\file.txt", "PATH_ABSOLUTE"),
    ],
)
def test_zip_unsafe_paths_write_nothing_outside_run(
    tmp_path: Path, unsafe_name: str, expected_code: str
) -> None:
    source = tmp_path / "malicious.zip"
    _write_zip(source, [(unsafe_name, b"owned"), ("safe.dwg", b"safe")])
    run = tmp_path / "run"

    result = ingest_input(source, run)

    assert result.succeeded is False
    assert expected_code in _issue_codes(result)
    assert not (run / "ingest").exists()
    assert not (tmp_path / "escaped.txt").exists()
    assert not (tmp_path / "absolute.txt").exists()


def test_zip_symlink_member_is_rejected(tmp_path: Path) -> None:
    source = tmp_path / "symlink.zip"
    with zipfile.ZipFile(source, "w") as archive:
        info = zipfile.ZipInfo("link-to-outside")
        info.create_system = 3
        info.external_attr = (stat.S_IFLNK | 0o777) << 16
        archive.writestr(info, "../../outside")

    result = ingest_input(source, tmp_path / "run")

    assert result.succeeded is False
    assert "LINK_MEMBER_REJECTED" in _issue_codes(result)
    assert not (tmp_path / "run" / "ingest").exists()


def test_case_insensitive_normalized_collision_is_rejected(tmp_path: Path) -> None:
    source = tmp_path / "collision.zip"
    _write_zip(source, [("A.dwg", b"one"), ("a.DWG", b"two")])

    result = ingest_input(source, tmp_path / "run")

    assert result.succeeded is False
    assert "PATH_COLLISION" in _issue_codes(result)


def test_file_directory_prefix_conflict_is_rejected(tmp_path: Path) -> None:
    source = tmp_path / "prefix.zip"
    _write_zip(source, [("parent", b"file"), ("parent/child.dwg", b"child")])

    result = ingest_input(source, tmp_path / "run")

    assert result.succeeded is False
    assert "PATH_PREFIX_CONFLICT" in _issue_codes(result)


@pytest.mark.parametrize(
    ("limits", "members", "expected_code"),
    [
        (IngestLimits(max_members=1), [("a", b"1"), ("b", b"2")], "MEMBER_COUNT_LIMIT"),
        (IngestLimits(max_file_bytes=2), [("large", b"123")], "FILE_SIZE_LIMIT"),
        (
            IngestLimits(max_file_bytes=10, max_total_bytes=3),
            [("a", b"12"), ("b", b"34")],
            "TOTAL_SIZE_LIMIT",
        ),
        (
            IngestLimits(max_compression_ratio=2),
            [("bomb", b"0" * 10_000)],
            "COMPRESSION_RATIO_LIMIT",
        ),
    ],
)
def test_zip_resource_limits_reject_before_publishing(
    tmp_path: Path,
    limits: IngestLimits,
    members: list[tuple[str, bytes]],
    expected_code: str,
) -> None:
    source = tmp_path / "limited.zip"
    _write_zip(source, members)
    run = tmp_path / "run"

    result = ingest_input(source, run, limits)

    assert result.succeeded is False
    assert expected_code in _issue_codes(result)
    assert not (run / "ingest").exists()


def test_existing_ingest_snapshot_is_never_overwritten(tmp_path: Path) -> None:
    source = tmp_path / "project.zip"
    _write_zip(source, [("drawing.dwg", b"new")])
    ingest_root = tmp_path / "run" / "ingest"
    ingest_root.mkdir(parents=True)
    sentinel = ingest_root / "sentinel.txt"
    sentinel.write_text("keep", encoding="utf-8")

    result = ingest_input(source, tmp_path / "run")

    assert result.succeeded is False
    assert "INGEST_DESTINATION_EXISTS" in _issue_codes(result)
    assert sentinel.read_text(encoding="utf-8") == "keep"


def test_transient_windows_publish_lock_is_retried_atomically(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "project.zip"
    _write_zip(source, [("drawing.dwg", b"dwg")])
    real_replace = ingest_module.os.replace
    publish_attempts = 0

    def flaky_replace(source_path: object, destination_path: object) -> None:
        nonlocal publish_attempts
        is_publish = Path(source_path).name.startswith(".ingest-") and (
            Path(destination_path).name == "ingest"
        )
        if is_publish:
            publish_attempts += 1
            if publish_attempts <= 2:
                raise PermissionError(errno.EACCES, "synthetic scanner lock")
        real_replace(source_path, destination_path)

    monkeypatch.setattr(ingest_module.os, "replace", flaky_replace)
    monkeypatch.setattr(ingest_module.time, "sleep", lambda _seconds: None)

    result = ingest_input(source, tmp_path / "run")

    assert result.succeeded is True, result.model_dump(mode="json")
    assert publish_attempts == 3
    assert result.metadata["publish_method"] == "atomic_directory_rename"
    assert result.metadata["publish_retry_count"] == 2
    assert "INGEST_PUBLISH_RETRIED" in _issue_codes(result)
    assert Path(result.files[0].absolute_path).read_bytes() == b"dwg"


def test_persistent_publish_lock_remains_a_safe_block(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "project.zip"
    _write_zip(source, [("drawing.dwg", b"dwg")])
    real_replace = ingest_module.os.replace
    publish_attempts = 0

    def locked_replace(source_path: object, destination_path: object) -> None:
        nonlocal publish_attempts
        is_publish = Path(source_path).name.startswith(".ingest-") and (
            Path(destination_path).name == "ingest"
        )
        if is_publish:
            publish_attempts += 1
            raise PermissionError(errno.EACCES, "synthetic persistent lock")
        real_replace(source_path, destination_path)

    monkeypatch.setattr(ingest_module.os, "replace", locked_replace)
    monkeypatch.setattr(ingest_module.time, "sleep", lambda _seconds: None)

    result = ingest_input(source, tmp_path / "run")

    assert result.succeeded is False
    assert publish_attempts == len(ingest_module._PUBLISH_RETRY_DELAYS_SECONDS) + 1
    assert "INGEST_PUBLISH_FAILED" in _issue_codes(result)
    assert not (tmp_path / "run" / "ingest").exists()
    assert not any(path.name.startswith(".ingest-") for path in (tmp_path / "run").iterdir())


def test_7z_ingest_uses_same_manifest_contract(tmp_path: Path) -> None:
    py7zr = pytest.importorskip("py7zr")
    payload = tmp_path / "drawing.dwg"
    payload.write_bytes(b"seven-zip-dwg")
    source = tmp_path / "project.7z"
    with py7zr.SevenZipFile(source, "w") as archive:
        archive.write(payload, arcname="nested/drawing.dwg")

    result = ingest_input(source, tmp_path / "run")

    assert result.succeeded is True, result.model_dump(mode="json")
    assert len(result.files) == 1
    assert result.files[0].relative_path == "nested/drawing.dwg"
    assert Path(result.files[0].absolute_path).read_bytes() == b"seven-zip-dwg"
    assert Path(result.original_copy).read_bytes() == source.read_bytes()


def test_unsupported_regular_file_gets_clear_issue(tmp_path: Path) -> None:
    source = tmp_path / "program.exe"
    source.write_bytes(b"binary")

    result = ingest_input(source, tmp_path / "run")

    assert result.succeeded is False
    assert result.input_kind == "unsupported"
    assert "INPUT_FORMAT_UNSUPPORTED" in _issue_codes(result)
    assert not os.path.exists(result.ingest_dir)
