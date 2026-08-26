from __future__ import annotations

import hashlib
import json
import os
import stat
import sys
import zipfile
from pathlib import Path

import pytest

SCRIPT_ROOT = Path(__file__).parents[1] / "skills" / "cad-stainless-quote" / "scripts"
sys.path.insert(0, str(SCRIPT_ROOT))

from cadquote.ingest import IngestLimits, ingest_input  # noqa: E402
from cadquote.models import ReviewStatus  # noqa: E402


def _issue_codes(result: object) -> set[str]:
    return {issue.code for issue in result.issues}


def _write_zip(path: Path, members: list[tuple[str, bytes]]) -> None:
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, payload in members:
            archive.writestr(name, payload)


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
