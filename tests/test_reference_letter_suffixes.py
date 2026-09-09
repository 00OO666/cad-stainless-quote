"""Synthetic lexical and identity regressions for lettered drawing references."""

from __future__ import annotations

import pytest
from cadquote.classifier import extract_drawing_number
from cadquote.linking import (
    _expand_range,
    extract_reference_codes,
    extract_structured_reference_callouts,
    normalize_reference_code,
    normalize_view_number,
    rank_evidence_edges,
)
from cadquote.models import CadEntity, Sheet


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("foo-e-7a", "FOO-E-07A"),
        ("ＦＯＯ－Ｅ－７ａ", "FOO-E-07A"),
        ("FOO/E/7a", "FOO-E-07A"),
        ("FOO-E-007a", "FOO-E-007A"),
        ("FOO-E-1234b", "FOO-E-1234B"),
        ("FOO-E-7", "FOO-E-07"),
        ("AE7a", "A-E-07A"),
        ("AE-7a", "AE-07A"),
        ("A-E7a", "A-E-07A"),
        ("el7b", "EL-07B"),
        ("EL-007b", "EL-007B"),
        ("DT 8a", "DT-08A"),
        ("M9c", "M-09C"),
        ("SS-42a", "SS-42A"),
    ],
)
def test_normalize_preserves_attached_suffix_and_numeric_width(raw, expected):
    assert normalize_reference_code(raw) == expected


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("FOO-E-42A", {"FOO-E-42A"}),
        ("see (foo-e-7a)", {"FOO-E-07A"}),
        ("ＦＯＯ－Ｅ－７ａ", {"FOO-E-07A"}),
        ("FOO-E-42A-合成房间", {"FOO-E-42A"}),
        ("FOO-E-42A FOO-E-42B", {"FOO-E-42A", "FOO-E-42B"}),
        ("EL7a DT-8b AE9c", {"EL-07A", "DT-08B", "A-E-09C"}),
        ("FOO-EL-42A", {"FOO-EL-42A"}),
        ("FOO-DT-42A", {"FOO-DT-42A"}),
        ("FOO-E-42", {"FOO-E-42"}),
    ],
)
def test_extract_does_not_create_unsuffixed_or_short_aliases(raw, expected):
    assert extract_reference_codes(raw) == expected


@pytest.mark.parametrize(
    "raw",
    [
        "FOO-E-42-A",
        "A-E-42-A",
        "EL-42-A",
        "AE42-A",
        "FOO-E-42AB",
        "FOO-E-42A7",
        "FOO-E-12345A",
        "FOO-FOO-E-42A",
        "LONGFOO-E-42A",
        "2028-05-23",
        "2028-05-23A",
    ],
)
def test_malformed_segments_and_dates_remain_rejected(raw):
    assert normalize_reference_code(raw) is None
    assert extract_reference_codes(raw) == set()
    assert extract_drawing_number([raw]) is None


@pytest.mark.parametrize("prefix", ["GC-SS", "GC-WD", "GC-PF", "GC-AC", "GC-P", "MT"])
@pytest.mark.parametrize("suffix", ["", "A", "b"])
def test_material_namespace_cannot_gain_a_drawing_suffix_alias(prefix, suffix):
    code = f"{prefix}-42{suffix}"
    assert normalize_reference_code(code) is None
    assert extract_reference_codes(code) == set()
    assert extract_drawing_number([code]) is None
    assert extract_drawing_number([f"{code}; see FOO-E-42A"]) == "FOO-E-42A"


@pytest.mark.parametrize(
    "text,expected",
    [
        ("FOO-E-42A~FOO-E-44A", {"FOO-E-42A", "FOO-E-44A"}),
        ("FOO-E-42A~44A", {"FOO-E-42A", "FOO-E-44A"}),
        ("FOO-E-42~44A", {"FOO-E-42", "FOO-E-44A"}),
        ("FOO-E-42A~44", {"FOO-E-42A", "FOO-E-44"}),
        ("FOO-E-42A~42B", {"FOO-E-42A", "FOO-E-42B"}),
        ("FOO-E-42A至FOO-D-44B", {"FOO-E-42A", "FOO-D-44B"}),
        ("FOO-E-42~44", {"FOO-E-42", "FOO-E-43", "FOO-E-44"}),
        ("FOO-E-44～FOO-E-42", {"FOO-E-42", "FOO-E-43", "FOO-E-44"}),
        ("GC-SS-42A~44A", set()),
        ("GC-P-42~44A", set()),
    ],
)
def test_numeric_ranges_expand_but_lettered_endpoints_remain_exact(text, expected):
    assert extract_reference_codes(text) == expected


def test_range_guard_handles_direct_compact_and_invalid_endpoint_calls():
    assert _expand_range("AE7a", "9b") == {"A-E-07A", "A-E-09B"}
    assert _expand_range("FOO-E-42A", "invalid") == {"FOO-E-42A"}
    assert _expand_range("MT-42A", "44A") == set()
    assert _expand_range("FOO-E-001", "003") == {"FOO-E-01", "FOO-E-02", "FOO-E-03"}


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("7", "07"),
        ("07", "07"),
        ("007", "007"),
        ("7a", "07A"),
        ("007b", "007B"),
        (" ７ａ ", "07A"),
        ("999z", "999Z"),
        ("000", "000"),
        ("0007", None),
        ("42ab", None),
        ("A", None),
        ("42-A", None),
        ("42 A", None),
        ("4 2A", None),
        ("-42", None),
        ("4.2", None),
        ("٤٢A", None),
        (None, None),
    ],
)
def test_strict_native_view_number_helper(raw, expected):
    assert normalize_view_number(raw) == expected


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("FOO-E-42a", "FOO-E-42A"),
        ("FOO-E-7", "FOO-E-7"),
        ("FOO-E-007", "FOO-E-007"),
        ("FOO-E-42~44", "FOO-E-42~44"),
        ("FOO-E-42A~44B", "FOO-E-42A~44B"),
        ("Drawing No: FOO/E/42a", "FOO/E/42A"),
        ("Drawing No: AE42a", "AE42A"),
        ("FOO-E-42A-合成房间", "FOO-E-42A"),
    ],
)
def test_classifier_retains_full_suffix_and_historical_numeric_spelling(raw, expected):
    assert extract_drawing_number([raw]) == expected


def _attribute(identity, text, **overrides):
    values = {
        "id": identity,
        "source_file_id": "file:synthetic",
        "sheet_id": "sheet:synthetic-plan",
        "entity_type": "ATTRIB",
        "space": "model",
        "text": text,
        "geometry": {"parent_insert_handle": "SYNTHETIC-PARENT"},
    }
    values.update(overrides)
    return CadEntity(**values)


def test_structured_suffix_callout_retains_page_view_and_entity_identity():
    members = [_attribute("page", "FOO-E-42a"), _attribute("view", "7b")]
    results = extract_structured_reference_callouts(members)
    assert len(results) == 1
    assert results[0].code == "FOO-E-42A"
    assert results[0].view_number == "07B"
    assert set(results[0].entity_ids) == {"page", "view"}
    assert results[0].parent_insert_handle == "SYNTHETIC-PARENT"


@pytest.mark.parametrize(
    "override",
    [
        {"source_file_id": "file:other-synthetic"},
        {"sheet_id": "sheet:other-synthetic"},
        {"space": "paper:synthetic"},
        {"geometry": {"parent_insert_handle": "OTHER-SYNTHETIC-PARENT"}},
        {"geometry": {}},
        {"entity_type": "TEXT"},
    ],
)
def test_suffix_does_not_relax_structured_same_parent_scope(override):
    members = [_attribute("page", "FOO-E-42a"), _attribute("view", "7b", **override)]
    assert extract_structured_reference_callouts(members) == []


@pytest.mark.parametrize(
    "extra",
    [_attribute("other-page", "FOO-E-42B"), _attribute("other-view", "7c")],
)
def test_competing_suffix_identities_are_not_collapsed(extra):
    members = [_attribute("page", "FOO-E-42a"), _attribute("view", "7b"), extra]
    assert extract_structured_reference_callouts(members) == []


def test_lettered_target_cannot_promote_an_unsuffixed_sheet_alias():
    plan = Sheet(id="p", source_file_id="f", kind="plan")
    base = Sheet(id="e", source_file_id="f", kind="elevation", drawing_number="FOO-E-42")
    entity = CadEntity(
        id="ref",
        source_file_id="f",
        sheet_id="p",
        entity_type="TEXT",
        space="model",
        text="FOO-E-42A",
    )
    assert rank_evidence_edges([plan, base], entities=[entity], promote_explicit=True) == []
    target = base.model_copy(update={"drawing_number": "FOO-E-42A"})
    edges = rank_evidence_edges([plan, target], entities=[entity])
    assert len(edges) == 1
    assert any("FOO-E-42A@" in evidence for evidence in edges[0].basis)


@pytest.mark.parametrize("separator", ["-", "_", "/", ".", ":", "\\", "＿", "／", "：", "./"])
@pytest.mark.parametrize("tail", ["7", "ABC"])
@pytest.mark.parametrize("suffix", ["", "A"])
def test_connected_ascii_tail_is_not_a_real_drawing_substring(separator, tail, suffix):
    text = f"FOO-E-42{suffix}{separator}{tail}"
    assert normalize_reference_code(text) is None
    assert extract_reference_codes(text) == set()
    assert extract_drawing_number([text]) is None


@pytest.mark.parametrize("separator", ["-", "_", "/", ".", ":", "\\", "＿", "／", "：", "./"])
@pytest.mark.parametrize("code", ["FOO-E-42A", "FOO-E-42", "A-E42A", "A-E42"])
def test_connected_ascii_prefix_does_not_create_a_short_reference(separator, code):
    text = f"BAD{separator}{code}"
    assert normalize_reference_code(text) is None
    assert extract_reference_codes(text) == set()
    assert extract_drawing_number([text]) is None


@pytest.mark.parametrize("label", ["Drawing No: ", "Drawing Number: ", "图号："])
@pytest.mark.parametrize("separator", ["_", "/", ".", ":", "\\"])
@pytest.mark.parametrize("suffix", ["", "A"])
def test_classifier_label_fallback_cannot_bypass_ascii_continuation(label, separator, suffix):
    text = f"{label}FOO-E-42{suffix}{separator}7"
    assert extract_reference_codes(text) == set()
    assert extract_drawing_number([text]) is None


@pytest.mark.parametrize("label", ["Drawing No:", "Drawing No.:", "Drawing Number:", "图号："])
@pytest.mark.parametrize("code", ["FOO-E-42A", "FOO-E-42", "AE42A", "A-E42A"])
def test_explicit_classifier_label_remains_a_delimiter_without_a_space(label, code):
    assert extract_drawing_number([f"{label}{code}"]) == code
    for tail in (":7", "\\7", "_ABC", ".7"):
        assert extract_drawing_number([f"{label}{code}{tail}"]) != code


@pytest.mark.parametrize("separator", ["-", "_", "/", ".", ":", "\\", "＿", "／", "："])
@pytest.mark.parametrize("code", ["FOO-E-42A", "FOO-E-42", "A-E42A"])
def test_chinese_description_separators_remain_real_reference_delimiters(separator, code):
    expected = normalize_reference_code(code)
    assert expected is not None
    for text in (f"{code}{separator}合成说明", f"合成说明{separator}{code}"):
        assert extract_reference_codes(text) == {expected}
        assert normalize_reference_code(extract_drawing_number([text])) == expected


@pytest.mark.parametrize("separator", ["-", "_", "/", ":", "\\", "＿", "／", "："])
@pytest.mark.parametrize("suffix", ["", "a"])
def test_complete_separator_spellings_have_consistent_normalization_and_extraction(
    separator, suffix
):
    text = separator.join(("FOO", "E", f"42{suffix}"))
    expected = f"FOO-E-42{suffix.upper()}"
    assert normalize_reference_code(text) == expected
    assert extract_reference_codes(text) == {expected}
    assert normalize_reference_code(extract_drawing_number([text])) == expected


@pytest.mark.parametrize("section,kind", [("A", "E"), ("A", "D"), ("B", "E"), ("B", "D")])
@pytest.mark.parametrize("separator", ["-", "_", "/", ":", "\\"])
@pytest.mark.parametrize("suffix", ["", "a"])
def test_known_split_compact_family_matches_its_normalizer(section, kind, separator, suffix):
    text = f"{section}{separator}{kind}42{suffix}"
    expected = f"{section}-{kind}-42{suffix.upper()}"
    assert normalize_reference_code(text) == expected
    assert extract_reference_codes(text) == {expected}
    assert normalize_reference_code(extract_drawing_number([text])) == expected


def test_delimiters_preserve_independent_references_and_numeric_ranges():
    assert extract_reference_codes("see FOO-E-42A and FOO-E-44B") == {"FOO-E-42A", "FOO-E-44B"}
    assert extract_reference_codes("FOO-E-42A, FOO-E-44B.") == {"FOO-E-42A", "FOO-E-44B"}
    assert extract_reference_codes("FOO-E-42A. Next drawing: FOO-E-44B.") == {
        "FOO-E-42A",
        "FOO-E-44B",
    }
    assert extract_reference_codes("FOO-E-42~44_合成说明") == {"FOO-E-42", "FOO-E-43", "FOO-E-44"}
    assert extract_reference_codes("FOO-E-42A~44B/合成说明") == {"FOO-E-42A", "FOO-E-44B"}
    assert extract_reference_codes("FOO-E-42A~FOO-E-44B") == {"FOO-E-42A", "FOO-E-44B"}
    assert extract_reference_codes("FOO-E-42~44_7") == {"FOO-E-42"}
    assert extract_drawing_number(["FOO-E-42A and FOO-E-44~46"]) == "FOO-E-42A"
    assert extract_drawing_number(["FOO-E-44~46 and FOO-E-42A"]) == "FOO-E-44~46"


@pytest.mark.parametrize("code", ["FOO-E42A", "FOO-E42", "Q-E42A", "Q-E42"])
def test_split_compact_fix_does_not_add_arbitrary_families(code):
    assert normalize_reference_code(code) is None
    assert extract_reference_codes(code) == set()
    assert extract_drawing_number([code]) is None
