"""Synthetic regression tests for read-only gold-workbook image export."""

from __future__ import annotations

import hashlib
import json
import shutil
import zipfile
from base64 import b64decode
from pathlib import Path

import xlsxwriter
from cad_quote import main as cad_quote_main
from cadquote.gold import import_gold_workbook
from cadquote.gold_images import MANIFEST_NAME, export_gold_image_assets

_PIXEL_PNG = b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk/x8AAusB9Y9Z4WQAAAAASUVORK5CYII="
)


def _rewrite_zip(
    source_path: Path,
    output_path: Path,
    replacements: dict[str, bytes],
    additions: dict[str, bytes],
) -> None:
    with (
        zipfile.ZipFile(source_path, "r") as source,
        zipfile.ZipFile(output_path, "w", compression=zipfile.ZIP_DEFLATED) as target,
    ):
        for info in source.infolist():
            data = replacements.get(info.filename, source.read(info))
            target.writestr(info, data)
        for name, data in additions.items():
            target.writestr(name, data)


def _inject_wps_cell_image(
    source_path: Path,
    output_path: Path,
    *,
    formula_id: str,
    image_bytes: bytes,
) -> None:
    with zipfile.ZipFile(source_path, "r") as archive:
        workbook_rels = archive.read("xl/_rels/workbook.xml.rels").decode("utf-8")
        content_types = archive.read("[Content_Types].xml").decode("utf-8")

    workbook_rels = workbook_rels.replace(
        "</Relationships>",
        (
            '<Relationship Id="rIdCellImages" '
            'Type="http://www.wps.cn/officeDocument/2020/cellImage" '
            'Target="cellimages.xml"/></Relationships>'
        ),
    )
    content_types = content_types.replace(
        "</Types>",
        (
            '<Override PartName="/xl/cellimages.xml" '
            'ContentType="application/vnd.wps-officedocument.cellimage+xml"/>'
            "</Types>"
        ),
    )
    cell_images = f'''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<etc:cellImages
 xmlns:etc="http://www.wps.cn/officeDocument/2017/etCustomData"
 xmlns:xdr="http://schemas.openxmlformats.org/drawingml/2006/spreadsheetDrawing"
 xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main"
 xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">
 <etc:cellImage><xdr:pic><xdr:nvPicPr>
  <xdr:cNvPr id="1" name="{formula_id}"/><xdr:cNvPicPr/>
 </xdr:nvPicPr><xdr:blipFill><a:blip r:embed="rId1"/>
 <a:stretch><a:fillRect/></a:stretch></xdr:blipFill>
 <xdr:spPr><a:prstGeom prst="rect"><a:avLst/></a:prstGeom></xdr:spPr>
 </xdr:pic></etc:cellImage>
</etc:cellImages>'''.encode()
    cell_image_rels = b"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
 <Relationship Id="rId1"
  Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/image"
  Target="media/cell-image.png"/>
</Relationships>"""
    _rewrite_zip(
        source_path,
        output_path,
        {
            "xl/_rels/workbook.xml.rels": workbook_rels.encode(),
            "[Content_Types].xml": content_types.encode(),
        },
        {
            "xl/cellimages.xml": cell_images,
            "xl/_rels/cellimages.xml.rels": cell_image_rels,
            "xl/media/cell-image.png": image_bytes,
        },
    )


def _write_mixed_image_workbook(path: Path) -> None:
    pixel = path.with_name("synthetic-pixel.png")
    base = path.with_name(f"{path.stem}-base.xlsx")
    pixel.write_bytes(_PIXEL_PNG)
    formula_id = "ID_SYNTHETIC_DETAIL"
    workbook = xlsxwriter.Workbook(base)
    sheet = workbook.add_worksheet("候选金标")
    sheet.write_row(
        "A1",
        [
            "序号",
            "名称",
            "MT编号",
            "立面示意图",
            "节点示意图",
            "宽",
            "长度",
            "数量",
            "工程量",
            "单位",
        ],
    )
    sheet.write_row("A2", [1, "合成收口", "MT-01", None, None, 100, 1000, 1, 0.1, "㎡"])
    sheet.insert_image("D2", pixel, {"x_scale": 8, "y_scale": 8})
    sheet.write_formula("E2", f'=_xlfn.DISPIMG("{formula_id}",1)', None, 0)
    workbook.close()
    _inject_wps_cell_image(
        base,
        path,
        formula_id=formula_id,
        image_bytes=_PIXEL_PNG,
    )


def _replace_drawing_target(source_path: Path, output_path: Path, target_value: str) -> None:
    rels_path = "xl/drawings/_rels/drawing1.xml.rels"
    with zipfile.ZipFile(source_path, "r") as archive:
        rels = archive.read(rels_path).decode("utf-8")
    rels = rels.replace("../media/image1.png", target_value)
    _rewrite_zip(source_path, output_path, {rels_path: rels.encode()}, {})


def test_exports_embedded_and_dispimg_original_bytes_deterministically(
    tmp_path: Path,
) -> None:
    source = tmp_path / "synthetic.xlsx"
    output = tmp_path / "image-assets"
    _write_mixed_image_workbook(source)
    source_before = source.read_bytes()
    gold = import_gold_workbook(source)

    first = export_gold_image_assets(source, output, gold_result=gold)
    manifest_bytes = (output / MANIFEST_NAME).read_bytes()
    second = export_gold_image_assets(source, output, gold_result=gold)

    assert source.read_bytes() == source_before
    assert first == second
    assert (output / MANIFEST_NAME).read_bytes() == manifest_bytes
    assert first.asset_count == 2
    assert first.unique_file_count == 1
    assert first.issues == []
    assert [(asset.sheet, asset.cell, asset.row) for asset in first.assets] == [
        ("候选金标", "D2", 2),
        ("候选金标", "E2", 2),
    ]
    assert [asset.category for asset in first.assets] == ["elevation", "detail"]
    assert [asset.source_type for asset in first.assets] == ["embedded", "dispimg"]
    assert first.assets[0].formula_id is None
    assert first.assets[1].formula_id == "ID_SYNTHETIC_DETAIL"
    assert {asset.package_path for asset in first.assets} == {
        "xl/media/image1.png",
        "xl/media/cell-image.png",
    }
    assert len({asset.export_relative_path for asset in first.assets}) == 1
    expected_hash = hashlib.sha256(_PIXEL_PNG).hexdigest()
    assert {asset.sha256 for asset in first.assets} == {expected_hash}
    exported = output / first.assets[0].export_relative_path
    assert exported.read_bytes() == _PIXEL_PNG
    payload = json.loads(manifest_bytes)
    assert payload["source_file"] == "synthetic.xlsx"
    assert "source_path" not in payload


def test_xlsm_uses_the_same_read_only_ooxml_export(tmp_path: Path) -> None:
    xlsx = tmp_path / "synthetic.xlsx"
    xlsm = tmp_path / "synthetic.xlsm"
    _write_mixed_image_workbook(xlsx)
    shutil.copyfile(xlsx, xlsm)

    result = export_gold_image_assets(xlsm, tmp_path / "xlsm-assets")

    assert result.workbook_format == "xlsm"
    assert result.asset_count == 2
    assert result.issues == []


def test_gold_import_cli_option_exports_manifest(tmp_path: Path) -> None:
    source = tmp_path / "synthetic.xlsx"
    result_path = tmp_path / "gold.json"
    assets = tmp_path / "assets"
    _write_mixed_image_workbook(source)

    exit_code = cad_quote_main(
        [
            "gold-import",
            str(source),
            "--out",
            str(result_path),
            "--image-assets-dir",
            str(assets),
        ]
    )

    assert exit_code == 0
    assert result_path.is_file()
    assert json.loads((assets / MANIFEST_NAME).read_text(encoding="utf-8"))["asset_count"] == 2


def test_unsafe_relationship_is_blocked_without_path_traversal(tmp_path: Path) -> None:
    source = tmp_path / "safe.xlsx"
    unsafe = tmp_path / "unsafe.xlsx"
    _write_mixed_image_workbook(source)
    _replace_drawing_target(source, unsafe, "../../../../escaped.png")
    outside = tmp_path / "escaped.png"

    result = export_gold_image_assets(unsafe, tmp_path / "unsafe-assets")

    assert outside.exists() is False
    assert result.asset_count == 1
    assert {issue.code for issue in result.issues} >= {
        "UNSAFE_RELATIONSHIP_TARGET",
        "MISSING_IMAGE_RELATIONSHIP",
    }
    assert all(".." not in asset.export_relative_path for asset in result.assets)


def test_legacy_xls_writes_explicit_unsupported_issue(tmp_path: Path) -> None:
    source = tmp_path / "legacy.xls"
    source.write_bytes(b"synthetic legacy placeholder")
    source_before = source.read_bytes()

    result = export_gold_image_assets(source, tmp_path / "legacy-assets")

    assert source.read_bytes() == source_before
    assert result.asset_count == 0
    assert result.unique_file_count == 0
    assert [(issue.code, issue.severity) for issue in result.issues] == [
        ("LEGACY_XLS_IMAGE_EXPORT_UNSUPPORTED", "BLOCK")
    ]
    manifest = json.loads((tmp_path / "legacy-assets" / MANIFEST_NAME).read_text(encoding="utf-8"))
    assert manifest["issues"][0]["code"] == "LEGACY_XLS_IMAGE_EXPORT_UNSUPPORTED"
