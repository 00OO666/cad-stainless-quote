"""Independent synthetic regressions for page-slot ownership and fallback guards."""

from copy import deepcopy

import pytest
from cadquote.models import CadEntity, Sheet
from cadquote.panels import (
    PanelExpansion,
    _inherit_orphan_panel_page_codes,
    _LocalViewAnchor,
    _prepend_parent_page_anchor,
    expand_viewport_panels,
    split_local_drawing_panels,
)


def _block(key, value, point, *, source="file:synthetic", layout="Main", title="ELEVATION"):
    common = {
        "source_file_id": source,
        "sheet_id": f"{source}:{layout}:paper",
        "space": f"paper:{layout}",
    }
    return [
        CadEntity(
            id=f"{source}:{key}",
            handle=key,
            entity_type="INSERT",
            **common,
            geometry={"name": "TITLE_BLOCK", "attribute_handles": [key + "-n", key + "-t"]},
        ),
        CadEntity(
            id=f"{source}:{key}-n",
            handle=key + "-n",
            entity_type="ATTRIB",
            **common,
            text=value,
            insert=point,
            geometry={"tag": "SHEET_NO", "parent_insert_handle": key},
        ),
        CadEntity(
            id=f"{source}:{key}-t",
            handle=key + "-t",
            entity_type="ATTRIB",
            **common,
            text=title,
            insert=(point[0], point[1] + 2),
            geometry={"tag": "SHEET_TITLE", "parent_insert_handle": key},
        ),
    ]


def _source(source="file:synthetic", layout="Main", bbox=(0, 0, 100, 100)):
    sheets = [
        Sheet(id=source + ":model", source_file_id=source, layout="Model"),
        Sheet(id=f"{source}:{layout}:paper", source_file_id=source, layout=layout),
    ]
    entities = [
        CadEntity(
            id=source + ":geometry",
            source_file_id=source,
            sheet_id=sheets[0].id,
            entity_type="LINE",
            space="model",
            bbox=(0, 0, 100, 100),
        ),
        CadEntity(
            id=source + ":viewport",
            handle="VIEW",
            source_file_id=source,
            sheet_id=sheets[1].id,
            entity_type="VIEWPORT",
            space=f"paper:{layout}",
            bbox=bbox,
            geometry={"viewport_id": 2, "model_bbox": [0, 0, 100, 100]},
        ),
    ]
    return sheets, entities


def _expand(blocks, *, bbox=(0, 0, 100, 100), model_code=None):
    sheets, entities = _source(bbox=bbox)
    entities += [entity for block in blocks for entity in block]
    if model_code:
        entities.append(
            CadEntity(
                id="model-reference",
                source_file_id="file:synthetic",
                sheet_id=sheets[0].id,
                entity_type="TEXT",
                space="model",
                insert=(20, 40),
                text=model_code,
            )
        )
    snapshot = deepcopy([entity.model_dump() for entity in entities])
    result = expand_viewport_panels(
        sheets,
        entities,
        source_names={"file:synthetic": "synthetic-elevations.dxf"},
    )
    assert snapshot == [entity.model_dump() for entity in entities]
    assert result.sheets
    assert any(
        entity.geometry.get("original_entity_id") == "file:synthetic:geometry"
        for entity in result.entities
    )
    return result


@pytest.mark.parametrize("slot", [None, "", "UNASSIGNED", "L2-E-31 / L2-E-32"])
def test_local_invalid_slot_does_not_expose_classifier_model_reference(slot):
    result = _expand(
        [_block("own", slot, (105, -5)), _block("other", "L2-E-90", (180, -40))],
        model_code="L2-E-90 ELEVATION",
    )
    assert result.sheets[0].drawing_number is None
    assert any(
        value.startswith("paper_page_reference_unresolved:") for value in result.sheets[0].evidence
    )


def test_hidden_unused_slot_does_not_veto_a_visible_local_title():
    hidden = _block("hidden", None, (104, -4))
    for entity in hidden:
        entity.geometry["semantic_hidden"] = True
    result = _expand([hidden, _block("own", "L2-E-31A", (105, -5))])
    assert result.sheets[0].drawing_number == "L2-E-31A"


def test_other_layout_invalid_slot_does_not_block_current_layout():
    result = _expand(
        [
            _block("own", "L2-E-31A", (105, -5)),
            _block("foreign", None, (105, -5), layout="Other"),
        ]
    )
    assert result.sheets[0].drawing_number == "L2-E-31A"


def test_other_layout_strong_title_does_not_replace_current_title_or_kind():
    result = _expand(
        [
            _block("own", "L2-E-31A", (105, -10), title="ROOM ELEVATION"),
            _block("foreign", "L2-P-99", (105, -2), layout="Other", title="FLOOR PLAN"),
        ]
    )
    panel = result.sheets[0]
    assert panel.drawing_number == "L2-E-31A"
    assert panel.title == "ROOM ELEVATION"
    assert panel.kind == "elevation"


def test_source_scope_separates_identical_native_handles_and_paper_locations():
    sheets, entities = [], []
    expected = {"file:left": "L2-E-31A", "file:right": "L2-E-44B"}
    for source, code in expected.items():
        source_sheets, source_entities = _source(source=source)
        sheets += source_sheets
        entities += source_entities + _block("TB", code, (105, -5), source=source)
    result = expand_viewport_panels(sheets, entities)
    assert {panel.source_file_id: panel.drawing_number for panel in result.sheets} == expected


def test_ordinary_viewport_does_not_take_leftmost_page_band_shortcut():
    result = _expand(
        [
            _block("left", "L2-E-30", (10, -15)),
            _block("right", "L2-E-31", (105, -5)),
        ]
    )
    assert result.sheets[0].drawing_number == "L2-E-31"


def test_wide_viewport_with_unparsed_first_slot_does_not_skip_to_later_page():
    result = _expand(
        [
            _block("first", None, (50, 10)),
            _block("second", "L2-E-32", (450, 10)),
            _block("third", "L2-E-33", (850, 10)),
        ],
        bbox=(0, 20, 1000, 100),
    )
    assert result.sheets[0].drawing_number is None


def test_wide_viewport_valid_first_page_survives_unparsed_later_slot():
    # The association labels the beginning of this wide panel, not all later pages.
    result = _expand(
        [
            _block("first", "L2-E-31A", (50, 10)),
            _block("second", None, (450, 10)),
            _block("third", "L2-E-33", (850, 10)),
        ],
        bbox=(0, 20, 1000, 100),
    )
    assert result.sheets[0].drawing_number == "L2-E-31A"


def test_wide_missing_first_slot_is_not_skipped_when_only_one_valid_code_remains():
    result = _expand(
        [_block("first", None, (50, 10)), _block("second", "L2-E-32", (450, 10))],
        bbox=(0, 20, 1000, 100),
    )
    assert result.sheets[0].drawing_number is None


def test_guarded_orphan_does_not_inherit_while_truly_unnumbered_sibling_can():
    common = dict(source_file_id="file:synthetic", kind="elevation", bbox=(0, 0, 100, 100))
    page = Sheet(
        id="page",
        **common,
        drawing_number="L2-E-31A",
        layout="Main#viewport:WIDE",
        confidence=0.9,
        evidence=["local_subview_parent:wide"],
    )
    guarded = Sheet(
        id="guarded",
        **common,
        layout="Main#viewport:SMALL",
        confidence=0.8,
        evidence=["paper_page_reference_unresolved:NO_LOCAL_PAGE_SLOT@candidate"],
    )
    unnumbered = guarded.model_copy(update={"id": "unnumbered", "evidence": []})
    result = _inherit_orphan_panel_page_codes([guarded, unnumbered, page])
    assert result[0].drawing_number is None
    assert result[1].drawing_number == "L2-E-31A"


def test_lettered_local_page_is_not_silently_absorbed_by_numeric_neighbors():
    panel = Sheet(
        id="wide",
        source_file_id="file:synthetic",
        drawing_number="L2-E-30",
        title="LEVEL ELEVATIONS",
        kind="elevation",
        layout="Main#viewport:WIDE",
        bbox=(0, 0, 600, 100),
    )
    entities = []
    for code, x in [("L2-E-30", 100), ("L2-E-30A", 300), ("L2-E-31", 500)]:
        for suffix, text, point in [
            ("title", "LEVEL ELEVATIONS", (x, 8)),
            ("code", code, (x, 4)),
            ("item", "MT-01", (x, 55)),
        ]:
            entities.append(
                CadEntity(
                    id=code + ":" + suffix,
                    source_file_id=panel.source_file_id,
                    sheet_id=panel.id,
                    entity_type="TEXT",
                    space="model@Main#WIDE",
                    text=text,
                    insert=point,
                )
            )
    result = PanelExpansion(sheets=[panel], entities=entities)
    split_local_drawing_panels(result)
    assert len(result.entities) == len(entities)
    if len(result.sheets) > 1:
        assert "L2-E-30A" in {sheet.drawing_number for sheet in result.sheets}
        by_id = {sheet.id: sheet for sheet in result.sheets}
        marker = next(
            entity
            for entity in result.entities
            if entity.geometry.get("parent_panel_entity_id") == "L2-E-30A:item"
        )
        assert by_id[marker.sheet_id].drawing_number == "L2-E-30A"
    else:
        # Safe no-split is acceptable; it must preserve the original panel.
        assert result.sheets == [panel]


@pytest.mark.parametrize("parent,first", [("L2-E-30A", "L2-E-31"), ("L2-E-30", "L2-E-31A")])
def test_letter_suffix_is_not_used_to_infer_an_ordinal_predecessor(parent, first):
    panel = Sheet(
        id="wide",
        source_file_id="file:synthetic",
        drawing_number=parent,
        title="LEVEL ELEVATIONS",
        kind="elevation",
        bbox=(0, 0, 600, 100),
        evidence=[f"paper_page_reference:{parent}@original-title-slot"],
    )
    anchors = [
        _LocalViewAnchor(code=first, x=300, title="LEVEL ELEVATIONS", entity_ids=("one",)),
        _LocalViewAnchor(code="L2-E-32", x=500, title="LEVEL ELEVATIONS", entity_ids=("two",)),
    ]
    assert _prepend_parent_page_anchor(panel, anchors) == anchors


def test_later_letter_anchor_also_disables_numeric_predecessor_inference():
    panel = Sheet(
        id="wide",
        source_file_id="file:synthetic",
        drawing_number="L2-E-30",
        kind="elevation",
        bbox=(0, 0, 600, 100),
        evidence=["paper_page_reference:L2-E-30@original-title-slot"],
    )
    anchors = [
        _LocalViewAnchor(code="L2-E-31", x=300, title="ELEVATIONS", entity_ids=("one",)),
        _LocalViewAnchor(code="L2-E-32A", x=500, title="ELEVATIONS", entity_ids=("two",)),
    ]
    assert _prepend_parent_page_anchor(panel, anchors) == anchors


@pytest.mark.parametrize("source,layout", [("file:other", "Main"), ("file:synthetic", "Other")])
def test_orphan_does_not_inherit_from_other_source_or_layout(source, layout):
    unknown = Sheet(
        id="unknown",
        source_file_id="file:synthetic",
        kind="elevation",
        layout="Main#viewport:SMALL",
        bbox=(0, 0, 100, 100),
    )
    foreign = Sheet(
        id="foreign",
        source_file_id=source,
        kind="elevation",
        drawing_number="L2-E-32A",
        layout=layout + "#viewport:WIDE",
        bbox=(0, 0, 100, 100),
        evidence=["local_subview_parent:wide"],
    )
    assert _inherit_orphan_panel_page_codes([unknown, foreign])[0].drawing_number is None
