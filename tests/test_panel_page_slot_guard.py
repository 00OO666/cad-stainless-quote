"""Synthetic page-slot regressions; no customer drawings or target values."""

from copy import deepcopy

import pytest
from cadquote.models import CadEntity, Sheet
from cadquote.panels import _inherit_orphan_panel_page_codes, expand_viewport_panels


def title_block(key, code, point, *, extra_code=None):
    handles = [f"{key}-page", f"{key}-title"]
    if extra_code is not None:
        handles.append(f"{key}-extra")
    common = {"source_file_id": "file:synthetic-slot", "sheet_id": "paper", "space": "paper:Test"}
    entities = [
        CadEntity(
            id=key,
            handle=key,
            entity_type="INSERT",
            **common,
            geometry={"name": "TITLE_BLOCK", "attribute_handles": handles},
        ),
        CadEntity(
            id=f"{key}-page",
            handle=f"{key}-page",
            entity_type="ATTRIB",
            **common,
            text=code,
            insert=point,
            geometry={"parent_insert_handle": key, "tag": "DWG_NO"},
        ),
        CadEntity(
            id=f"{key}-title",
            handle=f"{key}-title",
            entity_type="ATTRIB",
            **common,
            text="ROOM ELEVATION",
            insert=(point[0], point[1] + 2),
            geometry={"parent_insert_handle": key, "tag": "SHEET_TITLE"},
        ),
    ]
    if extra_code is not None:
        entities.append(
            CadEntity(
                id=f"{key}-extra",
                handle=f"{key}-extra",
                entity_type="ATTRIB",
                **common,
                text=extra_code,
                insert=(point[0] + 3, point[1]),
                geometry={"parent_insert_handle": key, "tag": "SHEET_NO"},
            )
        )
    return entities


def expand(*titles, model_reference=None):
    source = "file:synthetic-slot"
    sheets = [
        Sheet(id="model", source_file_id=source, layout="Model"),
        Sheet(id="paper", source_file_id=source, layout="Test"),
    ]
    entities = [
        CadEntity(
            id="model-line",
            source_file_id=source,
            sheet_id="model",
            entity_type="LINE",
            space="model",
            bbox=(0, 0, 200, 200),
        ),
        CadEntity(
            id="viewport",
            handle="VIEW",
            source_file_id=source,
            sheet_id="paper",
            entity_type="VIEWPORT",
            space="paper:Test",
            bbox=(100, 200, 300, 400),
            geometry={"viewport_id": 2, "model_bbox": [0, 0, 200, 200]},
        ),
        *[e for block in titles for e in block],
    ]
    if model_reference:
        entities.append(
            CadEntity(
                id="model-reference",
                source_file_id=source,
                sheet_id="model",
                entity_type="TEXT",
                space="model",
                insert=(20, 20),
                text=model_reference,
            )
        )
    before = deepcopy([e.model_dump() for e in entities])
    result = expand_viewport_panels(sheets, entities, source_names={source: "room-elevations.dxf"})
    assert [e.model_dump() for e in entities] == before
    return result


def test_unparseable_own_page_slot_prevents_neighbour_borrow():
    result = expand(
        title_block("own", "FOO-E-42-AA", (315, 190)),
        title_block("neighbour", "FOO-E-77", (440, 100)),
        model_reference="FOO-E-77 ELEVATION",
    )
    assert result.sheets[0].drawing_number is None
    assert any("UNPARSEABLE_OR_AMBIGUOUS_PAGE_NUMBER" in e for e in result.sheets[0].evidence)


def test_blank_explicit_page_slot_is_not_silently_discarded():
    result = expand(
        title_block("own", "", (315, 190)), title_block("neighbour", "FOO-E-77", (440, 100))
    )
    assert result.sheets[0].drawing_number is None


def test_far_title_cannot_supply_classifier_fallback_page_number():
    result = expand(
        title_block("remote", "FOO-E-77", (850, -300)), model_reference="FOO-E-77 ELEVATION"
    )
    assert result.sheets[0].drawing_number is None
    assert any("NO_LOCAL_PAGE_SLOT" in value for value in result.sheets[0].evidence)


def test_same_title_block_conflicting_page_slots_are_unresolved():
    result = expand(title_block("own", "FOO-E-42", (315, 190), extra_code="FOO-E-43"))
    assert result.sheets[0].drawing_number is None
    assert any(
        "CONFLICTING_TITLE_BLOCK_PAGE_NUMBERS" in value for value in result.sheets[0].evidence
    )


def test_tied_page_slots_do_not_choose_lexical_code():
    result = expand(
        title_block("one", "FOO-E-42", (315, 190)), title_block("two", "FOO-E-43", (315, 190))
    )
    assert result.sheets[0].drawing_number is None
    assert any("AMBIGUOUS_LOCAL_PAGE_SLOTS" in value for value in result.sheets[0].evidence)


def test_clear_directional_local_page_remains_available():
    result = expand(
        title_block("own", "FOO-E-42", (315, 190)), title_block("above", "FOO-E-77", (150, 410))
    )
    assert result.sheets[0].drawing_number == "FOO-E-42"
    assert any("BOUNDED_DIRECTIONAL_PAGE_CANDIDATE" in value for value in result.sheets[0].evidence)


def test_unresolved_page_cannot_be_repopulated_by_orphan_inheritance():
    shared = {
        "source_file_id": "file:synthetic-slot",
        "kind": "elevation",
        "bbox": (0, 0, 100, 100),
        "layout": "Test#viewport:VIEW",
    }
    unknown = Sheet(
        id="unknown",
        **shared,
        evidence=["paper_page_reference_unresolved:MISSING_PAGE_NUMBER@raw-slot"],
    )
    known = Sheet(
        id="known", **shared, drawing_number="FOO-E-77", evidence=["local_subview_parent:parent"]
    )
    assert _inherit_orphan_panel_page_codes([unknown, known])[0].drawing_number is None


def framed_title(key, code, point, box, *, complete=True, issues=None):
    block = title_block(key, code, point)
    block[0].geometry["paper_frame_scan"] = {
        "schema": "native-paper-frame/1",
        "complete": complete,
        "issues": issues or [],
        "rectangles": [{"bbox": box, "source_handles": [key + "-closed-outline"]}],
    }
    return block


def test_native_frame_owns_small_viewport_despite_distant_page_text():
    result = expand(
        framed_title("sheet", "FOO-D-42", (850, 20), (80, 0, 900, 440)),
        title_block("backref", "FOO-E-77", (315, 190)),
    )
    assert result.sheets[0].drawing_number == "FOO-D-42"
    assert any("NATIVE_CLOSED_FRAME_CONTAINS_VIEWPORT" in e for e in result.sheets[0].evidence)


def test_missing_own_native_frame_page_is_not_replaced_with_view_backref():
    result = expand(
        framed_title("sheet", None, (850, 20), (80, 0, 900, 440)),
        title_block("backref", "FOO-E-77", (315, 190)),
    )
    assert result.sheets[0].drawing_number is None
    assert any("UNPARSEABLE_NATIVE_PAGE_FRAMES" in e for e in result.sheets[0].evidence)


def test_conflicting_containing_frames_never_choose_by_size_or_order():
    one = framed_title("one", "FOO-D-42", (850, 20), (80, 0, 900, 440))
    two = framed_title("two", "FOO-D-43", (950, 20), (80, 0, 1000, 500))
    for titles in [(one, two), (two, one)]:
        result = expand(*titles)
        assert result.sheets[0].drawing_number is None


def test_partial_or_failed_frame_scan_cannot_supply_page():
    for complete, issues in [(False, []), (True, ["SCAN_LIMIT"])]:
        result = expand(
            framed_title(
                "sheet",
                "FOO-D-42",
                (850, 20),
                (80, 0, 900, 440),
                complete=complete,
                issues=issues,
            )
        )
        assert result.sheets[0].drawing_number is None


def test_partial_viewport_overlap_is_not_native_ownership():
    result = expand(framed_title("sheet", "FOO-D-42", (850, 20), (150, 0, 900, 440)))
    assert result.sheets[0].drawing_number is None


def test_plain_insert_bbox_is_never_promoted_to_native_frame():
    block = title_block("sheet", "FOO-D-42", (850, 20))
    block[0].bbox = (80, 0, 900, 440)
    assert expand(block).sheets[0].drawing_number is None


def test_frame_selection_respects_source_identity():
    from cadquote.panels import _paper_page_references, _select_page_reference

    references = _paper_page_references(
        framed_title("sheet", "FOO-D-42", (850, 20), (80, 0, 900, 440))
    )
    viewport = CadEntity(
        id="other",
        source_file_id="different-source",
        entity_type="VIEWPORT",
        space="paper:Test",
        bbox=(100, 200, 300, 400),
    )
    assert _select_page_reference(viewport, references).reference is None


@pytest.mark.parametrize("tag", ["DWG_NO", "CUSTOM_PAGE_CODE"])
@pytest.mark.parametrize("value", ["FOO-D-42 FOO-D-42A", "D-42 FOO-D-42"])
def test_one_page_attribute_must_not_drop_shorter_distinct_code(tag, value):
    block = framed_title("sheet", value, (850, 20), (80, 0, 900, 440))
    block[1].geometry["tag"] = tag
    result = expand(block, title_block("nearby", "FOO-E-77", (315, 190)))
    assert result.sheets[0].drawing_number is None
    assert any("UNPARSEABLE_NATIVE_PAGE_FRAMES" in e for e in result.sheets[0].evidence)
