import pytest
from cadquote.linking import (
    extract_reference_codes,
    extract_structured_reference_callouts,
    normalize_reference_code,
    rank_evidence_edges,
)
from cadquote.models import CadEntity, MtOccurrence, ReviewStatus, Sheet


@pytest.mark.parametrize(
    "code",
    [
        "GC-SS-987",
        "GC-WD-765",
        "GC-GL-567",
        "GC-PF-456",
        "GC-AC-345",
        "GC-P-123",
        "MT-87",
    ],
)
def test_material_codes_are_not_explicit_drawing_references(code):
    assert extract_reference_codes(f"材质 {code}，详见 9F-QS-77") == {"9F-QS-77"}


def fixture(code="9F-QS-77", text="GC-SS-987"):
    a = Sheet(id="e", source_file_id="a", kind="elevation", drawing_number="8FZ-EL-97")
    b = Sheet(id="d", source_file_id="b", kind="detail", drawing_number=code, title="GC-SS-987")
    entities = [
        CadEntity(
            id="t", source_file_id="a", sheet_id="e", entity_type="TEXT", space="model", text=text
        )
    ]
    occurrences = [
        MtOccurrence(
            id=s.id + "m", source_file_id=s.source_file_id, sheet_id=s.id, mt_code="GC-SS-987"
        )
        for s in (a, b)
    ]
    return [a, b], entities, occurrences


def test_material_title_cannot_be_promoted_to_explicit_route():
    sheets, entities, occurrences = fixture("8F-QS-77")
    edges = rank_evidence_edges(sheets, occurrences, entities, promote_explicit=True)
    assert len(edges) == 1
    assert edges[0].status == ReviewStatus.REVIEW
    assert not any(b.startswith("explicit_reference:") for b in edges[0].basis)
    assert any(b.startswith("same_mt:") for b in edges[0].basis)


def test_same_material_does_not_link_different_known_floors():
    sheets, entities, occurrences = fixture()
    assert rank_evidence_edges(sheets, occurrences, entities) == []


def test_explicit_cross_floor_reference_remains_review_candidate():
    sheets, entities, occurrences = fixture(text="详见 9F-QS-77")
    edges = rank_evidence_edges(sheets, occurrences, entities)
    assert len(edges) == 1
    assert edges[0].status == ReviewStatus.REVIEW
    assert any(b.startswith("explicit_reference:9F-QS-77@") for b in edges[0].basis)


def test_material_namespace_does_not_swallow_drawing_floor_prefix():
    assert extract_reference_codes("8F-ST-01 9F-PL-02 GC-PT-987") == {"8F-ST-01", "9F-PL-02"}


def test_unknown_floor_is_not_silently_discarded():
    sheets, entities, occurrences = fixture("QS-77")
    assert len(rank_evidence_edges(sheets, occurrences, entities)) == 1


def callout(code):
    return [
        CadEntity(
            id="ref",
            source_file_id="a",
            sheet_id="e",
            entity_type="ATTRIB",
            space="model",
            text=code,
            geometry={"parent_insert_handle": "100"},
        ),
        CadEntity(
            id="view",
            source_file_id="a",
            sheet_id="e",
            entity_type="ATTRIB",
            space="model",
            text="03",
            geometry={"parent_insert_handle": "100"},
        ),
    ]


@pytest.mark.parametrize("code", ["PL-01", "ST-01", "PT-01", "SS-01"])
def test_bare_prefixes_retain_real_structured_drawing_references(code):
    assert normalize_reference_code(code) == code
    assert extract_reference_codes(code) == {code}
    refs = extract_structured_reference_callouts(callout(code))
    assert len(refs) == 1
    assert refs[0].code == code and refs[0].view_number == "03"


@pytest.mark.parametrize("code", ["GC-PF-101", "GC-AC-101", "GC-P-101"])
def test_other_gc_material_families_cannot_bypass_floor_gate_or_form_callout(code):
    assert extract_structured_reference_callouts(callout(code)) == []
    sheets, _, occurrences = fixture(text=code)
    sheets[1] = sheets[1].model_copy(update={"title": code})
    assert rank_evidence_edges(sheets, occurrences, callout(code), promote_explicit=True) == []


@pytest.mark.parametrize("code", ["9F-EL-77", "9F-DT-77", "B1-DS-77"])
def test_full_drawing_token_does_not_create_short_suffix_alias(code):
    assert extract_reference_codes(code) == {code}
    assert extract_reference_codes(f"详见（{code}）") == {code}


def test_wrong_floor_target_cannot_match_embedded_short_alias_even_with_promotion():
    source = Sheet(id="p", source_file_id="a", kind="plan", drawing_number="7F-PL-01")
    wrong = Sheet(id="e", source_file_id="b", kind="elevation", drawing_number="8F-EL-77")
    ref = CadEntity(
        id="ref",
        source_file_id="a",
        sheet_id="p",
        entity_type="TEXT",
        space="model",
        text="详见 9F-EL-77",
    )
    assert rank_evidence_edges([source, wrong], [], [ref], promote_explicit=True) == []
    correct = wrong.model_copy(update={"drawing_number": "9F-EL-77"})
    edges = rank_evidence_edges([source, correct], [], [ref], promote_explicit=True)
    assert len(edges) == 1
    assert any(b.startswith("drawing_sheet_reference:9F-EL-77@") for b in edges[0].basis)


def test_short_target_title_alias_cannot_supply_cross_floor_exception():
    source = Sheet(id="p", source_file_id="a", kind="plan", drawing_number="7F-PL-01")
    target = Sheet(
        id="e", source_file_id="b", kind="elevation", drawing_number="8F-EL-77", title="EL-77"
    )
    ref = CadEntity(
        id="ref",
        source_file_id="a",
        sheet_id="p",
        entity_type="TEXT",
        space="model",
        text="详见 EL-77",
    )
    assert rank_evidence_edges([source, target], [], [ref], promote_explicit=True) == []


def test_ranges_and_standalone_short_codes_survive_without_partial_long_tokens():
    assert extract_reference_codes("9F-EL-01~03") == {"9F-EL-01", "9F-EL-02", "9F-EL-03"}
    assert extract_reference_codes("EL77 DT-04 AE03") == {"EL-77", "DT-04", "A-E-03"}
    assert extract_reference_codes("XX-9F-EL-01~03") == set()
    assert extract_reference_codes("GC-PF-101~103 GC-P-001~002") == set()


@pytest.mark.parametrize(
    "source_code,target_code",
    [
        ("01F-EL-01", "1F-QS-01"),
        ("B01-EL-01", "B1-QS-01"),
        ("B1F-EL-01", "B1-QS-01"),
    ],
)
def test_floor_spelling_variants_do_not_drop_same_floor_weak_candidates(source_code, target_code):
    sheets, entities, occurrences = fixture(target_code)
    sheets[0] = sheets[0].model_copy(update={"drawing_number": source_code})
    assert len(rank_evidence_edges(sheets, occurrences, entities)) == 1


@pytest.mark.parametrize(
    "text,expected",
    [
        ("1F-EL-01-大厅", {"1F-EL-01"}),
        ("2F-EL-024-台盆柜", {"2F-EL-024"}),
        ("E-02-休息区", {"E-02"}),
        ("EL-01-大厅", {"EL-01"}),
        ("1F-EL-01—大厅", {"1F-EL-01"}),
        ("9F-EL-01~03-大厅", {"9F-EL-01", "9F-EL-02", "9F-EL-03"}),
        ("GC-PF-101-材料 GC-P-101-材料 MT-01-不锈钢", set()),
        ("9F-EL-77-A", set()),
        ("XX-9F-EL-77-大厅", set()),
    ],
)
def test_description_separator_does_not_restore_ascii_suffix_aliases(text, expected):
    assert extract_reference_codes(text) == expected
