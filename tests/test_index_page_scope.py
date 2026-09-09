"""Synthetic custom sheet families; no customer drawing or geometry fixtures."""

import json

import pytest
from test_index_directions import chevron_fixture, extract


@pytest.mark.parametrize("family", ["ZE", "WE", "SE", "XY", "E", "EL", "QS"])
def test_exact_page_query_supports_custom_family_and_preserves_review(family):
    doc, _, ins = chevron_fixture()
    ins.get_attrib("1.1-E01").dxf.text = f"B7-{family}-82"
    result = extract(doc, page_codes=[f"B7-{family}-82"])
    row = result["records"][0]
    assert row["page_code"] == f"B7-{family}-82"
    assert row["geometry_state"] == "GEOMETRY_RESOLVED"
    assert row["page_selection_basis"] == "EXACT_NATIVE_PAGE_QUERY"
    assert row["state"] == "REVIEW"
    assert row["view_binding_confirmed"] is False
    assert row["physical_quantity"] is None


def test_no_implicit_broadening_of_legacy_discovery():
    doc, _, ins = chevron_fixture()
    ins.get_attrib("1.1-E01").dxf.text = "B7-ZE-82"
    assert extract(doc)["records"] == []
    assert extract(doc, page_codes=["B7-WE-82"])["records"] == []
    assert extract(doc, page_codes=["B7-E-82"])["records"] == []


def test_exact_query_is_canonical_but_not_a_prefix_or_view_number_match():
    doc, _, ins = chevron_fixture()
    ins.get_attrib("1.1-E01").dxf.text = "Ｂ７－ＺＥ－８"
    result = extract(doc, page_codes=["b7-ze-8", "B7-ZE-08"])
    assert result["requested_page_codes"] == ["B7-ZE-08"]
    assert result["records"][0]["page_code"] == "B7-ZE-08"
    assert extract(doc, page_codes=["B7-ZE-80"])["records"] == []


@pytest.mark.parametrize(
    "codes",
    [[], "B7-ZE-82", [None], ["1:20"], ["3600"], ["2026-05-20"], ["GC-SS-23"], ["MT-12"],
     ["GC-WD-23"], ["GC-ST-23"], ["GC-PL-23"], ["GC-MR-23"]],
)
def test_invalid_or_material_allowlist_is_rejected(codes):
    doc, _, _ = chevron_fixture()
    with pytest.raises(ValueError, match="page_codes"):
        extract(doc, page_codes=codes)


@pytest.mark.parametrize("mode", ["duplicate_page", "other_parent_view", "hidden", "wrong_tag"])
def test_allowlist_does_not_bypass_native_identity_or_visibility(mode):
    doc, _, ins = chevron_fixture()
    ins.get_attrib("1.1-E01").dxf.text = "B7-ZE-82"
    if mode == "duplicate_page":
        ins.add_attrib("DWG_NO", "B7-E-83", (40, 38))
    elif mode == "other_parent_view":
        ins.delete_attrib("E1")
        other = doc.layouts.get("Layout1").add_blockref("synthetic-chevron", (45, 45))
        other.add_attrib("E1", "07", (45, 45))
    elif mode == "hidden":
        ins.dxf.invisible = 1
    else:
        ins.get_attrib("1.1-E01").dxf.tag = "MATERIAL"
    result = extract(doc, page_codes=["B7-ZE-82"])
    assert all(r["arrow_vector"] is None for r in result["records"])
    assert not any(r["view_binding_confirmed"] for r in result["records"])


def test_cli_accepts_repeated_exact_page_scope_and_keeps_source(tmp_path):
    from cad_quote import build_parser

    doc, _, ins = chevron_fixture()
    ins.get_attrib("1.1-E01").dxf.text = "B7-ZE-82"
    source, output = tmp_path / "synthetic.dxf", tmp_path / "result.json"
    doc.saveas(source)
    before = source.read_bytes()
    args = build_parser().parse_args(
        ["index-directions", str(source), "--source-file-id", "synthetic-source",
         "--layout", "Layout1", "--out", str(output),
         "--page-code", "B7-ZE-82", "--page-code", "B7-WE-83"]
    )
    assert args.handler(args) == 0
    result = json.loads(output.read_text(encoding="utf-8"))
    assert result["requested_page_codes"] == ["B7-WE-83", "B7-ZE-82"]
    assert result["records"][0]["physical_quantity"] is None
    assert source.read_bytes() == before
