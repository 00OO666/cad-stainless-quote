from __future__ import annotations

from pathlib import Path

from cadquote.cad_index import _apply_text_repairs
from cadquote.dxf_text_encoding import plan_utf8_text_repairs
from cadquote.models import CadEntity


def _write_ascii_dxf(
    path: Path,
    values: list[tuple[str, str, list[tuple[int, bytes]]]],
    *,
    codepage: bytes = b"ANSI_936",
    dxf_version: bytes | None = None,
) -> None:
    groups: list[tuple[int, bytes]] = [
        (0, b"SECTION"),
        (2, b"HEADER"),
        *(
            [(9, b"$ACADVER"), (1, dxf_version)]
            if dxf_version is not None
            else []
        ),
        (9, b"$DWGCODEPAGE"),
        (3, codepage),
        (0, b"ENDSEC"),
        (0, b"SECTION"),
        (2, b"ENTITIES"),
    ]
    for entity_type, handle, entity_groups in values:
        groups.extend([(0, entity_type.encode("ascii")), (5, handle.encode("ascii"))])
        groups.extend(entity_groups)
    groups.extend([(0, b"ENDSEC"), (0, b"EOF")])
    payload = b"".join(
        f"{code:>3}\r\n".encode("ascii") + value + b"\r\n"
        for code, value in groups
    )
    path.write_bytes(payload)


def test_repairs_utf8_entity_bytes_when_legacy_header_conflicts(tmp_path: Path) -> None:
    source = tmp_path / "conflicting-codepage.dxf"
    _write_ascii_dxf(
        source,
        [
            ("TEXT", "A1", [(1, "虚构前台".encode())]),
            ("ATTRIB", "A2", [(1, "虚构门套".encode())]),
            (
                "MTEXT",
                "A3",
                [(3, "虚构节点".encode()), (1, "\\P尺寸说明".encode())],
            ),
            ("TEXT", "A4", [(1, b"ASCII ONLY")]),
        ],
    )

    plan = plan_utf8_text_repairs(source)

    assert plan.active is True
    assert plan.declared_codepage == "ANSI_936"
    assert plan.non_ascii_text_count == 3
    assert plan.utf8_cjk_text_count == 3
    assert plan.repairs["A1"].text == "虚构前台"
    assert plan.repairs["A2"].text == "虚构门套"
    assert plan.repairs["A3"].text == "虚构节点\n尺寸说明"


def test_does_not_guess_when_only_one_utf8_label_exists(tmp_path: Path) -> None:
    source = tmp_path / "small-mixed.dxf"
    _write_ascii_dxf(source, [("TEXT", "B1", [(1, "虚构标签".encode())])])

    plan = plan_utf8_text_repairs(source)

    assert plan.active is False
    assert plan.reason == "insufficient_file_level_utf8_evidence"
    assert plan.repairs == {}


def test_does_not_reinterpret_gbk_text_as_utf8(tmp_path: Path) -> None:
    source = tmp_path / "legacy-gbk.dxf"
    _write_ascii_dxf(
        source,
        [
            ("TEXT", "C1", [(1, "虚构甲".encode("gb18030"))]),
            ("TEXT", "C2", [(1, "虚构乙".encode("gb18030"))]),
            ("TEXT", "C3", [(1, "虚构丙".encode("gb18030"))]),
        ],
    )

    plan = plan_utf8_text_repairs(source)

    assert plan.active is False
    assert plan.repairs == {}


def test_declared_utf8_is_left_to_the_normal_dxf_reader(tmp_path: Path) -> None:
    source = tmp_path / "declared-utf8.dxf"
    _write_ascii_dxf(
        source,
        [
            ("TEXT", "D1", [(1, "虚构甲".encode())]),
            ("TEXT", "D2", [(1, "虚构乙".encode())]),
            ("TEXT", "D3", [(1, "虚构丙".encode())]),
        ],
        codepage=b"UTF-8",
    )

    plan = plan_utf8_text_repairs(source)

    assert plan.active is False
    assert plan.reason == "declared_utf8_no_repair_needed"


def test_r2007_or_newer_ignores_stale_legacy_codepage_header(tmp_path: Path) -> None:
    source = tmp_path / "r2007-stale-header.dxf"
    _write_ascii_dxf(
        source,
        [
            ("TEXT", "D7", [(1, "虚构甲".encode())]),
            ("TEXT", "D8", [(1, "虚构乙".encode())]),
            ("TEXT", "D9", [(1, "虚构丙".encode())]),
        ],
        dxf_version=b"AC1021",
    )

    plan = plan_utf8_text_repairs(source)

    assert plan.active is False
    assert plan.reason == "dxf_2007_or_newer_uses_utf8"
    assert plan.dxf_version == "AC1021"


def test_mtext_uses_group3_chunks_then_group1_tail_regardless_of_file_order(
    tmp_path: Path,
) -> None:
    source = tmp_path / "mtext-group-order.dxf"
    _write_ascii_dxf(
        source,
        [
            ("MTEXT", "M1", [(1, "尾".encode()), (3, "虚构甲".encode())]),
            ("TEXT", "M2", [(1, "虚构乙".encode())]),
            ("TEXT", "M3", [(1, "虚构丙".encode())]),
        ],
    )

    plan = plan_utf8_text_repairs(source)

    assert plan.active is True
    assert plan.repairs["M1"].text == "虚构甲尾"


def test_admitted_repairs_update_root_and_virtual_records_with_provenance(
    tmp_path: Path,
) -> None:
    source = tmp_path / "conflicting-codepage.dxf"
    _write_ascii_dxf(
        source,
        [
            ("TEXT", "E1", [(1, "虚构根文本".encode())]),
            ("TEXT", "E2", [(1, "虚构块文本".encode())]),
            ("TEXT", "E3", [(1, "虚构旁文本".encode())]),
        ],
    )
    plan = plan_utf8_text_repairs(source)
    records = [
        CadEntity(
            id="root",
            source_file_id="synthetic",
            entity_type="TEXT",
            space="model",
            handle="E1",
            text="\ufffd\ufffd-root",
        ),
        CadEntity(
            id="virtual",
            source_file_id="synthetic",
            entity_type="TEXT",
            space="model",
            text="\ue000-virtual",
            geometry={"source_block_entity_handle": "E2"},
        ),
        CadEntity(
            id="unchanged",
            source_file_id="synthetic",
            entity_type="TEXT",
            space="model",
            handle="E3",
            text="虚构旁文本",
        ),
    ]

    repaired, changed = _apply_text_repairs(records, plan)

    assert changed == 2
    assert [record.text for record in repaired] == [
        "虚构根文本",
        "虚构块文本",
        "虚构旁文本",
    ]
    provenance = repaired[0].geometry["text_encoding_repair"]
    assert provenance["method"] == "raw_ascii_dxf_handle_utf8"
    assert provenance["source_handle"] == "E1"
    assert len(provenance["raw_sha256"]) == 64
    assert "text_encoding_repair" not in repaired[2].geometry


def test_plausible_legacy_chinese_is_not_silently_overwritten(tmp_path: Path) -> None:
    source = tmp_path / "ambiguous-valid-gbk-and-utf8.dxf"
    ambiguous = bytes.fromhex("E6 B5 8B E8 AF 95")
    _write_ascii_dxf(
        source,
        [
            ("TEXT", "F1", [(1, ambiguous)]),
            ("TEXT", "F2", [(1, ambiguous)]),
            ("TEXT", "F3", [(1, ambiguous)]),
        ],
    )
    plan = plan_utf8_text_repairs(source)
    records = [
        CadEntity(
            id=f"legacy-{index}",
            source_file_id="synthetic",
            entity_type="TEXT",
            space="model",
            handle=f"F{index}",
            text="娴嬭瘯",
        )
        for index in range(1, 4)
    ]

    assert plan.active is True
    assert plan.repairs["F1"].text == "测试"
    repaired, changed = _apply_text_repairs(records, plan)
    assert changed == 0
    assert [record.text for record in repaired] == ["娴嬭瘯"] * 3
