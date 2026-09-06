"""Synthetic regressions: an identifier is not a physical instance count."""

import pytest
from cadquote.models import CadEntity, MtOccurrence, Sheet
from cadquote.takeoff import build_takeoff


def _draft(tag, text, *, unit=None):
    sheet = Sheet(id="e", source_file_id="f", kind="elevation", bbox=(0, 0, 200, 200))
    attr = CadEntity(
        id="number",
        source_file_id="f",
        sheet_id="e",
        entity_type="ATTRIB",
        text=text,
        space="model",
        insert=(50, 50),
        bbox=(45, 45, 55, 55),
        geometry={"tag": tag, "parent_insert_id": "label"},
    )
    family = CadEntity(
        id="family",
        source_file_id="f",
        sheet_id="e",
        entity_type="ATTRIB",
        text="MT",
        space="model",
        insert=(40, 50),
        geometry={"tag": "TYPE", "parent_insert_id": "label"},
    )
    occurrence = MtOccurrence(
        id="mt",
        mt_code="MT-07",
        source_file_id="f",
        sheet_id="e",
        anchor=(50, 50),
        confidence=0.9,
        leader_entity_id="pointer",
        leader_target=(50, 50),
    )
    entities = [attr, family]
    before = [e.model_dump() for e in entities]
    result = build_takeoff([sheet], entities, [occurrence], [])
    if unit:
        result = build_takeoff(
            [sheet],
            entities,
            [occurrence],
            [],
            confirmations={result.components[0].id: {"unit": unit, "pricing_method": "按套计算"}},
        )
    assert [e.model_dump() for e in entities] == before
    return result


@pytest.mark.parametrize("tag", ["NUM", "NUMBER", " num ", "number"])
@pytest.mark.parametrize("value", ["07", "7", "7.0"])
def test_bare_identifier_attributes_never_become_quantity(tag, value):
    result = _draft(tag, value, unit="套")
    assert not [m for m in result.measurements if m.role == "quantity"]
    assert result.items[0].quantity is None
    assert result.items[0].engineering_quantity is None
    assert result.items[0].status.value == "BLOCK"


@pytest.mark.parametrize("tag", ["QTY", "QUANTITY", "COUNT", "数量", "件数", " qty "])
def test_explicit_count_tags_remain_review_candidates(tag):
    result = _draft(tag, "7")
    matches = [m for m in result.measurements if m.role == "quantity"]
    assert len(matches) == 1
    assert matches[0].numeric_value == 7
    assert matches[0].unit == "count"
    assert matches[0].status.value == "REVIEW"
    assert result.items[0].quantity is None


@pytest.mark.parametrize("text", ["7mm", "7 MM", "7毫米", "7.5", "0"])
def test_dimension_units_or_invalid_counts_do_not_become_quantity(text):
    result = _draft("QTY", text)
    assert not [m for m in result.measurements if m.role == "quantity"]


def test_explicit_quantity_text_is_not_lost_in_ambiguous_attribute():
    result = _draft("NUM", "QTY:7")
    matches = [m for m in result.measurements if m.role == "quantity"]
    assert len(matches) == 1
    assert matches[0].numeric_value == 7
    assert "explicit_count_text" in matches[0].basis
