"""Synthetic, customer-free tests of the research-only ID selection boundary."""

from copy import deepcopy

import pytest
from cadquote.research_choices import occurrence_coverage, resolve_choices


def evidence(identity="native-a", value=850.0, case="case-a", units="millimeters"):
    return {
        "candidate_id": identity, "entity_id": identity, "source_file_id": "source-a",
        "source_sha256": "a" * 64, "sheet_id": "sheet-a", "board_sha256": "b" * 64,
        "artifact_sha256": "c" * 64, "origin": "cad_numbered_board",
        "allowed_case_ids": [case], "raw_value": value, "units": units,
        "value_mm": value if units == "millimeters" else None,
    }


def choice(ids=None, role="width_mm", op="single", case="case-a"):
    return {"case_id": case, "roles": {role: {
        "candidate_ids": ids or ["native-a"], "op": op,
    }}}


def run(entries=None, choices=None, cases=None):
    return resolve_choices(
        cases or [{"case_id": "case-a"}],
        entries if entries is not None else [evidence()],
        choices if choices is not None else [choice()],
    )


def test_selection_reads_cad_value_and_never_confirms():
    result = run()
    row = result["rows"][0]
    assert row["fields"]["width_mm"] == 850
    assert row["state"] == "REVIEW"
    assert result["commercial_use"] is False and result["formal_accuracy"] is None
    assert row["quantity"] is None and row["engineering_quantity"] is None
    assert row["amount"] is None


def test_missing_cases_remain_in_denominator():
    result = run(choices=[], cases=[{"case_id": "case-a"}, {"case_id": "case-b"}])
    assert len(result["rows"]) == 2
    assert all(row["state"] == "ABSTAIN" for row in result["rows"])


@pytest.mark.parametrize("key,value", [("value", 123), ("status", "PASS"),
                                      ("quantity", 1), ("unit", "mm")])
def test_rejects_chooser_numeric_values_units_and_approval(key, value):
    proposal = choice()
    proposal[key] = value
    with pytest.raises(ValueError):
        run(choices=[proposal])


@pytest.mark.parametrize("role", ["quantity", "amount", "status", "engineering_quantity"])
def test_rejects_non_dimension_roles(role):
    with pytest.raises(ValueError):
        run(choices=[choice(role=role)])


def test_rejects_numeric_value_inside_selection():
    proposal = choice()
    proposal["roles"]["width_mm"]["value"] = 123
    with pytest.raises(ValueError):
        run(choices=[proposal])


def test_scope_and_unknown_id_cannot_be_bypassed():
    with pytest.raises(ValueError):
        run(choices=[choice(ids=["not-in-cad"])])
    with pytest.raises(ValueError):
        run(choices=[choice(case="case-b")],
            cases=[{"case_id": "case-a"}, {"case_id": "case-b"}])


def test_unitless_number_is_retained_but_not_silently_mm():
    row = run(entries=[evidence(units="unitless")])["rows"][0]
    assert row["fields"]["width_mm"] is None
    binding = row["bindings"]["width_mm"]
    assert binding["native_values"] == [850]
    assert binding["reason_codes"] == ["SOURCE_UNITS_UNRESOLVED"]


def test_unproved_conversion_fails():
    entry = evidence(units="unitless")
    entry["value_mm"] = entry["raw_value"]
    with pytest.raises(ValueError):
        run(entries=[entry])


@pytest.mark.parametrize("key", ["source_sha256", "source_file_id", "entity_id",
                                 "sheet_id", "board_sha256", "artifact_sha256"])
def test_missing_lineage_fails(key):
    entry = evidence()
    del entry[key]
    with pytest.raises(ValueError):
        run(entries=[entry])


@pytest.mark.parametrize("value", [float("nan"), float("inf"), 0, -7, True])
def test_invalid_values_fail(value):
    with pytest.raises(ValueError):
        run(entries=[evidence(value=value)])


def test_answer_sheet_is_not_valid_evidence_origin():
    entry = evidence()
    entry["origin"] = "human_workbook"
    with pytest.raises(ValueError):
        run(entries=[entry])


def test_duplicate_ids_and_reused_dimensions_fail():
    with pytest.raises(ValueError):
        run(entries=[evidence(), evidence()])
    with pytest.raises(ValueError):
        run(choices=[choice(ids=["native-a", "native-a"], op="sum")])
    proposal = choice()
    proposal["roles"]["length_mm"] = deepcopy(proposal["roles"]["width_mm"])
    with pytest.raises(ValueError):
        run(choices=[proposal])


def span_entry(identity, start, end):
    entry = evidence(identity, end - start)
    entry["span"] = {"coordinate_space": "model", "axis": "y",
                     "interval_mm": [start, end]}
    return entry


@pytest.mark.parametrize("second,expected", [
    ((680, 740), "CHAIN_OVERLAP_OR_NESTING"),
    ((850, 940), "CHAIN_GAP"),
])
def test_chains_reject_nested_offsets_and_gaps(second, expected):
    entries = [span_entry("native-a", 0, 800), span_entry("native-b", *second)]
    row = run(entries, [choice(["native-a", "native-b"], op="sum")])["rows"][0]
    assert row["fields"]["width_mm"] is None
    assert row["bindings"]["width_mm"]["reason_codes"] == [expected]


def test_contiguous_chain_sums_only_native_lengths():
    entries = [span_entry("native-a", 0, 800), span_entry("native-b", 800, 860)]
    row = run(entries, [choice(["native-a", "native-b"], op="sum")])["rows"][0]
    assert row["fields"]["width_mm"] == 860
    assert row["engineering_quantity"] is None


@pytest.mark.parametrize("key,value", [("axis", "x"), ("coordinate_space", "paper")])
def test_chain_coordinate_frame_conflict(key, value):
    entries = [span_entry("native-a", 0, 800), span_entry("native-b", 800, 860)]
    entries[1]["span"][key] = value
    row = run(entries, [choice(["native-a", "native-b"], op="sum")])["rows"][0]
    assert row["bindings"]["width_mm"]["reason_codes"] == ["CHAIN_CROSSES_COORDINATE_FRAMES"]


def test_rectangle_is_not_billed_and_input_is_not_mutated():
    entries = [evidence(), evidence("native-b", 2100)]
    proposal = choice()
    proposal["roles"]["length_mm"] = {"candidate_ids": ["native-b"], "op": "single"}
    before = deepcopy((entries, proposal))
    row = run(entries, [proposal])["rows"][0]
    assert row["rectangular_area_per_instance_m2"] == 1.785
    assert row["engineering_quantity"] is None and row["quantity"] is None
    assert (entries, proposal) == before


def test_occurrence_coverage_is_not_a_count_of_physical_objects():
    result = occurrence_coverage(
        [{"id": "tag-a"}, {"id": "tag-b"}],
        [{"plan_occurrence_ids": ["tag-a"], "elevation_occurrence_ids": ["stale-tag"]}],
    )
    assert result["consumed_occurrence_count"] == 1
    assert result["unconsumed_occurrence_ids"] == ["tag-b"]
    assert result["orphaned_component_occurrence_ids"] == ["stale-tag"]
    assert "quantity" not in result


def test_different_view_aliases_are_still_one_native_dimension():
    entries = [evidence(), evidence("native-b")]
    for entry in entries:
        entry["original_entity_id"] = "original-shared"
    with pytest.raises(ValueError, match="aliases"):
        run(entries, [choice(["native-a", "native-b"], op="sum")])


def test_metric_value_cannot_be_replaced_in_pack():
    entry = evidence()
    entry["value_mm"] += 10
    with pytest.raises(ValueError, match="differs"):
        run(entries=[entry])
