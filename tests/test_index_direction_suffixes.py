"""Synthetic suffix parsing; all native ownership/geometry gates remain in force."""

import json
import math

import pytest
from test_index_directions import chevron_fixture, extract


def suffix_fixture(page="B4-E-17a", view="05b", **kwargs):
    doc, block, insert = chevron_fixture(**kwargs)
    insert.get_attrib("1.1-E01").dxf.text = page
    insert.get_attrib("E1").dxf.text = view
    return doc, block, insert


def assert_review_only(row):
    assert row["state"] == "REVIEW"
    assert row["physical_component_id"] is None
    assert row["physical_quantity"] is None
    assert row["view_binding_confirmed"] is False


@pytest.mark.parametrize("family", ["E", "EL", "QS", "DE", "DT", "D"])
def test_legacy_families_discover_one_ascii_suffix(family):
    doc, _, insert = suffix_fixture(page=f"B4-{family}-17a")
    result = extract(doc)
    row = result["records"][0]
    assert row["page_code"] == f"B4-{family}-17A"
    assert row["view_number"] == "05B"
    assert row["raw_page_values"] == [f"B4-{family}-17a"]
    assert row["raw_view_values"] == ["05b"]
    assert row["parent_insert_handle"] == insert.dxf.handle
    assert row["page_selection_basis"] == "LEGACY_REFERENCE_FAMILY"
    assert row["geometry_state"] == "GEOMETRY_RESOLVED"
    assert result["mutates_takeoff"] is False
    assert_review_only(row)


@pytest.mark.parametrize("view", ["7", "07", "007"])
def test_existing_numeric_view_output_spelling_is_unchanged(view):
    doc, _, _ = suffix_fixture(view=view)
    assert extract(doc)["records"][0]["view_number"] == view


def test_page_and_view_suffixes_are_independent_and_raw_values_survive():
    doc, _, _ = suffix_fixture(page="B4-E-17c", view="5b")
    row = extract(doc)["records"][0]
    assert (row["page_code"], row["view_number"]) == ("B4-E-17C", "05B")
    assert row["raw_view_values"] == ["5b"]
    assert_review_only(row)


def test_allowlist_suffix_is_exact_not_prefix_or_unsuffixed_match():
    doc, _, _ = suffix_fixture(page="B4-ZX-17a")
    assert extract(doc)["records"] == []  # No new default sheet families.
    result = extract(doc, page_codes=["b4-zx-17a", "B4-ZX-17A"])
    assert result["requested_page_codes"] == ["B4-ZX-17A"]
    row = result["records"][0]
    assert row["page_code"] == "B4-ZX-17A"
    assert row["page_selection_basis"] == "EXACT_NATIVE_PAGE_QUERY"
    assert row["geometry_state"] == "GEOMETRY_RESOLVED"
    assert_review_only(row)
    for other in ["B4-ZX-17", "B4-ZX-17B", "B4-ZX-18A", "B4-E-17A"]:
        assert extract(doc, page_codes=[other])["records"] == []


@pytest.mark.parametrize("bad", ["B4-E-17aa", "B4-E-17a1", "B4-E-17-a", "B4-E-17é"])
def test_malformed_page_suffix_is_not_silently_truncated(bad):
    doc, _, _ = suffix_fixture(page=bad)
    assert extract(doc)["records"] == []
    with pytest.raises(ValueError, match="page_codes"):
        extract(doc, page_codes=[bad])


@pytest.mark.parametrize("bad", ["GC-SS-17a", "GC-PL-17a", "MT-17a", "2400x800", "1:50"])
def test_allowlist_suffix_does_not_admit_material_or_dimension_codes(bad):
    doc, _, _ = suffix_fixture()
    with pytest.raises(ValueError, match="page_codes"):
        extract(doc, page_codes=[bad])


@pytest.mark.parametrize(
    "view", ["A", "AB", "5ab", "5b1", "5-b", "5 b", "5/1", "5.1", "0005", "5é", "5中"]
)
def test_suffix_does_not_bypass_strict_same_parent_view_grammar(view):
    doc, _, _ = suffix_fixture(view=view)
    row = extract(doc)["records"][0]
    assert row["view_number"] is None and row["arrow_vector"] is None
    assert row["raw_view_values"] == [view]
    assert "AMBIGUOUS_OR_MISSING_SAME_PARENT_ATTRIBUTES" in row["reason_codes"]
    assert_review_only(row)


@pytest.mark.parametrize("explicit_scope", [False, True])
@pytest.mark.parametrize(
    "mode",
    [
        "duplicate_page",
        "duplicate_view",
        "other_parent",
        "hidden_insert",
        "hidden_view",
        "wrong_page_tag",
        "wrong_view_tag",
        "two_arrows",
        "hidden_arrow",
        "asymmetric",
        "nested",
    ],
)
def test_suffix_keeps_identity_visibility_uniqueness_and_geometry_gates(mode, explicit_scope):
    doc, block, insert = suffix_fixture()
    if mode == "duplicate_page":
        insert.add_attrib("DWG_NO", "B4-E-17b", (40, 38))
    elif mode == "duplicate_view":
        insert.add_attrib("NO", "05b", (40, 40))
    elif mode == "other_parent":
        insert.delete_attrib("E1")
        other = doc.layouts.get("Layout1").add_blockref(insert.dxf.name, (40, 40))
        other.add_attrib("E1", "05b", (40, 40))
    elif mode == "hidden_insert":
        insert.dxf.invisible = 1
    elif mode == "hidden_view":
        insert.get_attrib("E1").dxf.flags = 1
    elif mode == "wrong_page_tag":
        insert.get_attrib("1.1-E01").dxf.tag = "MATERIAL"
    elif mode == "wrong_view_tag":
        insert.get_attrib("E1").dxf.tag = "WIDTH"
    elif mode == "two_arrows":
        block.add_entity(next(iter(block.query("LWPOLYLINE"))).copy())
    elif mode == "hidden_arrow":
        next(iter(block.query("LWPOLYLINE"))).dxf.invisible = 1
    elif mode == "asymmetric":
        next(iter(block.query("LWPOLYLINE"))).set_points([(4, 0), (8, 0), (0, 9), (-7, 0), (-4, 0)])
    else:
        doc.blocks.new("nested-suffix-symbol")
        block.add_blockref("nested-suffix-symbol", (0, 0))
    kwargs = {"page_codes": ["B4-E-17A"]} if explicit_scope else {}
    rows = extract(doc, **kwargs)["records"]
    assert all(row["arrow_vector"] is None for row in rows)
    for row in rows:
        assert_review_only(row)


@pytest.mark.parametrize("angle", [0, 33, 90, 180])
def test_suffix_uses_original_native_arrow_transform(angle):
    doc, block, _ = suffix_fixture(rotation=angle, xscale=-1)
    row = extract(doc)["records"][0]
    assert row["arrow_vector"] == pytest.approx(
        [-math.sin(math.radians(angle)), math.cos(math.radians(angle))]
    )
    assert (
        row["geometry_evidence"]["arrow_entity_handle"]
        == next(iter(block.query("LWPOLYLINE"))).dxf.handle
    )
    assert_review_only(row)


def test_same_definition_numeric_and_suffixed_indices_remain_distinct_parents():
    doc, _, first = suffix_fixture(page="B4-E-17", view="05")
    second = doc.layouts.get("Layout1").add_blockref(first.dxf.name, (70, 40))
    second.add_attrib("E1", "05a", (70, 40))
    second.add_attrib("1.1-E01", "B4-E-17a", (70, 38))
    rows = extract(doc)["records"]
    assert [r["page_code"] for r in rows] == ["B4-E-17", "B4-E-17A"]
    assert [r["parent_insert_handle"] for r in rows] == [first.dxf.handle, second.dxf.handle]
    assert (
        rows[0]["geometry_evidence"]["arrow_entity_handle"]
        == rows[1]["geometry_evidence"]["arrow_entity_handle"]
    )
    for row in rows:
        assert_review_only(row)


def test_cli_exact_suffix_scope_keeps_native_file_bytes(tmp_path):
    from cad_quote import build_parser

    doc, _, _ = suffix_fixture(page="B4-ZX-17a", view="5b")
    source, output = tmp_path / "synthetic-suffix.dxf", tmp_path / "directions.json"
    doc.saveas(source)
    before = source.read_bytes()
    args = build_parser().parse_args(
        [
            "index-directions",
            str(source),
            "--source-file-id",
            "synthetic-suffix",
            "--layout",
            "Layout1",
            "--page-code",
            "b4-zx-17a",
            "--out",
            str(output),
        ]
    )
    assert args.handler(args) == 0
    result = json.loads(output.read_text(encoding="utf-8"))
    assert result["records"][0]["view_number"] == "05B"
    assert result["records"][0]["raw_view_values"] == ["5b"]
    assert_review_only(result["records"][0])
    assert source.read_bytes() == before
