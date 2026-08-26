from __future__ import annotations

import hashlib
import json
import zipfile
from pathlib import Path
from xml.sax.saxutils import escape

import ezdxf
import pytest
from cadquote.materials import load_docx_material_specs
from cadquote.models import MaterialMention, MaterialSpec, ReviewStatus
from cadquote.mt import link_docx_material_mentions
from cadquote.pipeline import run_pipeline


def _paragraph_xml(text: str) -> str:
    runs: list[str] = []
    for index, value in enumerate(text.split("\t")):
        if index:
            runs.append("<w:r><w:tab/></w:r>")
        runs.append(f'<w:r><w:t xml:space="preserve">{escape(value)}</w:t></w:r>')
    return f"<w:p>{''.join(runs)}</w:p>"


def _table_xml(rows: list[list[str]], *, span_description: bool = False) -> str:
    rendered_rows: list[str] = []
    for row_index, row in enumerate(rows):
        cells: list[str] = []
        for cell_index, value in enumerate(row):
            properties = (
                '<w:tcPr><w:gridSpan w:val="2"/></w:tcPr>'
                if span_description and row_index == 1 and cell_index == 1
                else ""
            )
            cells.append(f"<w:tc>{properties}{_paragraph_xml(value)}</w:tc>")
        rendered_rows.append(f"<w:tr>{''.join(cells)}</w:tr>")
    return f"<w:tbl>{''.join(rendered_rows)}</w:tbl>"


def _write_docx(
    path: Path,
    *,
    tables: list[list[list[str]]] | None = None,
    paragraphs: list[str] | None = None,
    unsafe_member: str | None = None,
) -> None:
    body = [
        _table_xml(table, span_description=index == 0) for index, table in enumerate(tables or [])
    ]
    body.extend(_paragraph_xml(value) for value in paragraphs or [])
    document_xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        f"<w:body>{''.join(body)}<w:sectPr/></w:body></w:document>"
    )
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("word/document.xml", document_xml.encode("utf-8"))
        archive.writestr("[Content_Types].xml", b"<Types/>")
        if unsafe_member:
            archive.writestr(unsafe_member, b"must never be extracted")


def test_docx_material_book_parses_tables_paragraphs_and_deduplicates(
    tmp_path: Path,
) -> None:
    source = tmp_path / "synthetic-material-book.docx"
    _write_docx(
        source,
        tables=[
            [
                ["材料编号", "材料描述", "使用部位"],
                ["MT-01", "虚构黑砂不锈钢", "虚构门套"],
                ["GC-SS-102", "虚构镜面不锈钢", "虚构窗套"],
                ["PO-88", "普通采购编号", "忽略"],
                ["MT-04", "999.00", "不是材料描述"],
            ]
        ],
        paragraphs=[
            "MT-01 | 虚构黑砂不锈钢 | 虚构门套",
            "MT-03    虚构拉丝不锈钢    虚构窗套",
            "合同编号 2026-7788",
        ],
    )
    source_sha256 = hashlib.sha256(source.read_bytes()).hexdigest()

    specs = load_docx_material_specs(source, source_file_id="file:synthetic")

    assert [value.mt_code for value in specs] == ["MT-01", "GC-SS-102", "MT-03"]
    assert all(value.status == ReviewStatus.REVIEW for value in specs)
    assert all(value.source_type == "docx_material_book" for value in specs)
    assert all(value.source_sha256 == source_sha256 for value in specs)
    mt01 = next(value for value in specs if value.mt_code == "MT-01")
    assert mt01.name == "虚构黑砂不锈钢"
    assert any(":table=1:row=2:cell=1" in value for value in mt01.source_evidence)
    assert any(":paragraph=1" in value for value in mt01.source_evidence)
    assert len([value for value in specs if value.mt_code == "MT-01"]) == 1


def test_docx_material_book_accepts_only_explicit_configured_families(
    tmp_path: Path,
) -> None:
    source = tmp_path / "configured-family.docx"
    _write_docx(
        source,
        tables=[[["编号", "描述"], ["XX-SS-07", "虚构压纹不锈钢"]]],
    )

    assert load_docx_material_specs(source) == []
    configured = load_docx_material_specs(
        source,
        stainless_families={"MT", "GC-SS", "XX-SS"},
        review_families=(),
    )

    assert [value.mt_code for value in configured] == ["XX-SS-07"]
    assert configured[0].material_code_family == "XX-SS"


def test_docx_parser_rejects_unsafe_member_paths_without_extracting(
    tmp_path: Path,
) -> None:
    source = tmp_path / "unsafe.docx"
    _write_docx(
        source,
        paragraphs=["MT-01 | 虚构不锈钢"],
        unsafe_member="../escaped.xml",
    )

    with pytest.raises(ValueError, match="unsafe DOCX member path"):
        load_docx_material_specs(source)

    assert not (tmp_path / "escaped.xml").exists()


def test_docx_mention_link_is_review_when_unique_and_block_when_ambiguous() -> None:
    mention = MaterialMention(
        id="mention:one",
        raw_text="虚构黑砂不锈钢门套",
        source_file_id="file:cad",
        sheet_id="sheet:plan",
        entity_ids=["entity:description"],
        anchor=(10, 20),
    )
    unique_material = MaterialSpec(
        id="material:one",
        mt_code="MT-01",
        raw_material_code="MT-01",
        material_code_family="MT",
        name="虚构黑砂不锈钢",
        source_file_id="file:docx",
        source_type="docx_material_book",
        source_sha256="a" * 64,
        source_location="docx:paragraph=1",
    )

    edges, occurrences = link_docx_material_mentions([mention], [unique_material])

    assert len(edges) == 1
    assert edges[0].relation == "material_mention_to_material"
    assert edges[0].status == ReviewStatus.REVIEW
    assert len(occurrences) == 1
    assert occurrences[0].mt_code == "MT-01"
    assert occurrences[0].status == ReviewStatus.REVIEW
    assert occurrences[0].confidence < 0.5

    ambiguous_material = unique_material.model_copy(
        update={"id": "material:two", "mt_code": "MT-02", "raw_material_code": "MT-02"}
    )
    blocked_edges, blocked_occurrences = link_docx_material_mentions(
        [mention],
        [unique_material, ambiguous_material],
    )

    assert len(blocked_edges) == 2
    assert all(value.status == ReviewStatus.BLOCK for value in blocked_edges)
    assert blocked_occurrences == []


def test_pipeline_surfaces_docx_material_and_unnumbered_match_counts(
    tmp_path: Path,
) -> None:
    source = tmp_path / "input"
    source.mkdir()
    _write_docx(
        source / "project-material-book.docx",
        tables=[[["编号", "描述"], ["MT-01", "虚构黑砂不锈钢"]]],
    )
    drawing = ezdxf.new("R2018")
    modelspace = drawing.modelspace()
    modelspace.add_text("平面布置图", dxfattribs={"insert": (0, 100), "height": 5})
    modelspace.add_text(
        "虚构黑砂不锈钢门套",
        dxfattribs={"insert": (20, 20), "height": 5},
    )
    drawing.saveas(source / "plan.dxf")

    result = run_pipeline(source, tmp_path / "run", render_evidence=False)

    assert result.counts["docx_materials"] == 1
    assert result.counts["material_mentions"] == 1
    assert result.counts["material_mention_match_candidates"] == 1
    assert result.counts["material_mention_unique_matches"] == 1
    assert result.counts["mt_occurrences"] == 1
    occurrences = json.loads(Path(result.paths["mt_occurrences"]).read_text(encoding="utf-8"))
    assert occurrences[0]["mt_code"] == "MT-01"
    assert occurrences[0]["status"] == "REVIEW"
    review_pack = json.loads(Path(result.paths["review_pack"]).read_text(encoding="utf-8"))
    assert review_pack["summary"]["docx_material_count"] == 1
    assert review_pack["summary"]["material_mention_match_candidate_count"] == 1
    assert (
        review_pack["material_evidence"]["mention_to_material_candidates"][0]["status"] == "REVIEW"
    )
