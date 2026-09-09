"""Synthetic evaluation contract tests; no customer artifacts or target-driven takeoff."""

from __future__ import annotations

from copy import deepcopy

import pytest
from cadquote.evaluation import evaluate_takeoff
from cadquote.models import EvaluationPolicy, TakeoffItem
from cadquote.view_dispositions import prepare_view_context, resolve_view_applicability
from pydantic import ValidationError


def policy():
    result = EvaluationPolicy().model_dump(mode="json")
    result["policy_version"] = "synthetic-view-contract-v1"
    result["length_mm"]["relative_tolerance"] = 0.05
    result["quantity"]["relative_tolerance"] = 0
    return result


def ordinary_item(sequence=1, **changes):
    values = {
        "sequence": sequence,
        "component_id": f"component-{sequence}",
        "name": f"synthetic trim {sequence}",
        "mt_code": "MT-01",
        "plan_location": f"PLAN-{sequence}",
        "elevation": f"ELEV-{sequence}",
        "detail": f"DETAIL-{sequence}",
        "unfolded_spec": "20+30",
        "length_mm": 1000,
        "quantity": 2,
        "engineering_quantity": 0.1,
        "evidence_ids": [f"entity-{sequence}"],
    }
    values.update(changes)
    return TakeoffItem.model_validate(values)


def case(field="elevation", side="predicted"):
    """The context is built as separate CAD facts, not from either evaluation row."""
    role = "section" if field == "elevation" else "elevation"
    context = {
        "schema_version": "view-disposition-context/1",
        "side": side,
        "index_sha256": "a" * 64,
        "source_sha256": {"source-1": "b" * 64},
        "views": [
            {
                "id": "view-plan",
                "component_id": "component-1",
                "role": "plan",
                "sheet_id": "sheet-plan",
                "source_id": "source-1",
                "state": "CONFIRMED",
                "evidence_ids": ["entity-plan"],
            },
            {
                "id": "view-governing",
                "component_id": "component-1",
                "role": role,
                "sheet_id": "sheet-governing",
                "source_id": "source-1",
                "state": "CONFIRMED",
                "evidence_ids": ["entity-dimension"],
            },
        ],
        "entities": [
            {
                "id": "entity-plan",
                "component_id": "component-1",
                "entity_type": "MTEXT",
                "sheet_id": "sheet-plan",
                "source_id": "source-1",
            },
            {
                "id": "entity-dimension",
                "component_id": "component-1",
                "entity_type": "DIMENSION",
                "sheet_id": "sheet-governing",
                "source_id": "source-1",
            },
        ],
        "connections": [
            {
                "id": "connection-1",
                "component_id": "component-1",
                "state": "CONFIRMED",
                "source_view_id": "view-plan",
                "target_view_id": "view-governing",
                "evidence_ids": ["entity-plan"],
            }
        ],
        "searches": [
            {
                "id": "search-1",
                "component_id": "component-1",
                "excluded_stage": field,
                "searched_sheet_ids": ["sheet-plan", "sheet-governing"],
                "complete": True,
                "unresolved_reference_ids": [],
                "conflicting_view_ids": [],
            }
        ],
    }
    receipt = {
        "state": "NOT_APPLICABLE",
        "component_id": "component-1",
        "basis_kind": "plan_section" if field == "elevation" else "plan_elevation_without_detail",
        "basis": "The selected component views give the required governing geometry.",
        "index_sha256": "a" * 64,
        "source_sha256": {"source-1": "b" * 64},
        "support_view_ids": ["view-plan", "view-governing"],
        "connection_id": "connection-1",
        "search_id": "search-1",
        "evidence_ids": ["entity-plan", "entity-dimension"],
        "review": {
            "reviewer": "synthetic-reviewer",
            "reviewed_at": "2026-09-10T10:00:00+08:00",
            "reason": "Reviewed component-bound views and the complete reference search.",
        },
    }
    item = ordinary_item(
        **{
            field: None,
            "view_dispositions": {field: receipt},
            "evidence_ids": ["entity-plan", "entity-dimension"],
        }
    )
    return item, context


def score(predicted, gold, predicted_context=None, gold_context=None):
    return evaluate_takeoff(
        [predicted],
        [gold],
        policy=policy(),
        predicted_view_context=predicted_context,
        gold_view_context=gold_context,
    )


@pytest.mark.parametrize("field", ["elevation", "detail"])
def test_independent_audited_views_can_supply_not_applicable_field(field):
    predicted, predicted_context = case(field)
    gold, gold_context = case(field, "gold")
    before = predicted.model_dump(mode="json")
    report = score(predicted, gold, predicted_context, gold_context)
    assert report["overall_gate"] == "PASS"
    assert report["correct_rows"] == report["eligible_gold_rows"] == 1
    assert report["replication_recall"] == report["output_precision"] == 1
    result = report["row_results"][0]["field_results"][field]
    assert result["reason"] == "audited_not_applicable_on_both_sides"
    assert predicted.model_dump(mode="json") == before
    assert predicted.amount is None
    if field == "elevation":
        assert predicted.elevation is None
        assert {view["role"] for view in predicted_context["views"]} == {"plan", "section"}


def test_detail_negative_search_preserves_selected_elevation_scope():
    predicted, context = case("detail")
    context["searches"][0]["searched_sheet_ids"] = ["sheet-governing"]
    assert (
        resolve_view_applicability(
            predicted, "detail", prepare_view_context(context, "predicted")
        ).state
        == "NOT_APPLICABLE"
    )


@pytest.mark.parametrize(
    "value",
    [
        None,
        "",
        "  ",
        "N/A",
        "NOT_APPLICABLE",
        "无",
        "不适用",
        "N/A—ceiling item",
        "Not applicable (plan and section)",
        "无（仅有平面和剖面）",
    ],
)
def test_blank_or_matching_unaudited_sentinel_cannot_be_correct(value):
    predicted = ordinary_item(elevation=value)
    report = score(predicted, predicted)
    assert report["overall_gate"] == "BLOCKED"
    assert report["eligible_gold_rows"] == report["correct_rows"] == 0


@pytest.mark.parametrize("ids", [[], [""], ["  "], ["entity-1", ""]])
def test_empty_source_evidence_cannot_pass(ids):
    report = score(ordinary_item(evidence_ids=ids), ordinary_item())
    assert report["correct_rows"] == 0
    assert report["overall_gate"] == "FAIL"
    assert report["row_results"][0]["field_results"]["source_evidence"]["status"] == "FAIL"
    gold_report = score(ordinary_item(), ordinary_item(evidence_ids=ids))
    assert gold_report["overall_gate"] == "BLOCKED"


def test_gold_applicability_and_context_are_never_prediction_defaults():
    gold, gold_context = case(side="gold")
    predicted = ordinary_item(elevation=None)
    report = score(predicted, gold, gold_context=gold_context)
    assert report["eligible_gold_rows"] == 1
    assert report["overall_gate"] == "FAIL"
    assert (
        report["row_results"][0]["field_results"]["elevation"]["reason"]
        == "predicted_value_missing"
    )


def test_a_typed_receipt_without_its_own_context_still_fails():
    predicted, _ = case()
    gold, gold_context = case(side="gold")
    report = score(predicted, gold, gold_context=gold_context)
    assert report["overall_gate"] == "FAIL"
    assert (
        report["row_results"][0]["field_results"]["elevation"]["reason"]
        == "predicted_view_context_missing"
    )
    assert score(predicted, gold)["overall_gate"] == "BLOCKED"
    wrong_side = score(predicted, gold, gold_context, gold_context)
    assert wrong_side["overall_gate"] == "FAIL"
    assert (
        wrong_side["row_results"][0]["field_results"]["elevation"]["reason"]
        == "predicted_view_context_side_mismatch"
    )


@pytest.mark.parametrize(
    "change,expected",
    [
        (("index_sha256",), "disposition_index_hash_mismatch"),
        (("source_sha256", "source-1"), "disposition_source_hash_mismatch"),
        (("views", 1, "component_id"), "support_view_component_mismatch"),
        (("views", 1, "state"), "support_view_not_confirmed"),
        (("views", 1, "role"), "support_view_roles_invalid"),
        (("connections", 0, "target_view_id"), "support_views_disconnected"),
        (("searches", 0, "complete"), "negative_search_invalid_or_incomplete"),
        (("searches", 0, "unresolved_reference_ids"), "negative_search_invalid_or_incomplete"),
        (("searches", 0, "conflicting_view_ids"), "negative_search_invalid_or_incomplete"),
        (("searches", 0, "searched_sheet_ids"), "negative_search_invalid_or_incomplete"),
        (("entities", 1, "component_id"), "disposition_evidence_outside_component_scope"),
        (("entities", 1, "sheet_id"), "disposition_evidence_outside_component_scope"),
        (("entities", 1, "entity_type"), "governing_view_dimension_missing"),
    ],
)
def test_current_independent_context_rejects_stale_or_unbound_receipts(change, expected):
    predicted, context = case()
    gold, gold_context = case(side="gold")
    replacement = {
        "index_sha256": "c" * 64,
        "source-1": "c" * 64,
        "state": "REVIEW",
        "role": "detail",
        "complete": False,
        "unresolved_reference_ids": ["reference-unresolved"],
        "conflicting_view_ids": ["view-conflict"],
        "searched_sheet_ids": ["sheet-governing"],
        "entity_type": "MTEXT",
    }.get(change[-1], "wrong-identity")
    target = context
    for key in change[:-1]:
        target = target[key]
    target[change[-1]] = replacement
    report = score(predicted, gold, context, gold_context)
    assert report["overall_gate"] == "FAIL"
    result = report["row_results"][0]["field_results"]["elevation"]
    assert result["reason"] == f"predicted_{expected}"


@pytest.mark.parametrize("collection", ["views", "entities", "connections", "searches"])
def test_duplicate_context_identity_cannot_select_first(collection):
    predicted, context = case()
    context[collection].append(deepcopy(context[collection][0]))
    outcome = resolve_view_applicability(
        predicted, "elevation", prepare_view_context(context, "predicted")
    )
    assert outcome.reason == "view_context_duplicate_identity"


def test_changed_current_selection_cannot_reuse_old_receipt():
    predicted, context = case()
    additional = deepcopy(context["views"][1])
    additional["id"] = "view-new-selection"
    context["views"].append(additional)
    outcome = resolve_view_applicability(
        predicted, "elevation", prepare_view_context(context, "predicted")
    )
    assert outcome.reason == "support_views_differ_from_current_selection"


def test_two_absent_stages_cannot_complete_a_chain():
    predicted, context = case()
    detail_item, _ = case("detail")
    values = predicted.model_dump(mode="json")
    values["detail"] = None
    values["view_dispositions"]["detail"] = detail_item.view_dispositions["detail"].model_dump(
        mode="json"
    )
    predicted = TakeoffItem.model_validate(values)
    outcome = resolve_view_applicability(
        predicted, "elevation", prepare_view_context(context, "predicted")
    )
    assert outcome.reason == "multiple_not_applicable_stages"


@pytest.mark.parametrize(
    "review",
    [
        {"reviewer": " ", "reviewed_at": "2026-09-10T10:00:00+08:00", "reason": "reviewed"},
        {"reviewer": "reviewer", "reviewed_at": "2026-09-10T10:00:00", "reason": "reviewed"},
        {"reviewer": "reviewer", "reviewed_at": "2026-09-10T10:00:00+08:00", "reason": " "},
    ],
)
def test_missing_review_metadata_is_a_validation_error(review):
    predicted, _ = case()
    values = predicted.model_dump(mode="json")
    values["view_dispositions"]["elevation"]["review"] = review
    with pytest.raises(ValidationError):
        TakeoffItem.model_validate(values)


def test_positive_and_not_applicable_disagreement_remains_a_field_failure():
    predicted, context = case()
    report = score(predicted, ordinary_item(), context)
    assert report["overall_gate"] == "FAIL"
    assert (
        report["row_results"][0]["field_results"]["elevation"]["reason"]
        == "view_applicability_mismatch"
    )


def test_full_row_recall_and_precision_keep_missing_and_extra_rows():
    predicted, predicted_context = case()
    gold, gold_context = case(side="gold")
    missed = evaluate_takeoff(
        [],
        [gold],
        policy=policy(),
        gold_view_context=gold_context,
    )
    assert missed["eligible_gold_rows"] == 1
    assert missed["missing_count"] == 1
    assert missed["row_results"][0]["field_results"]["elevation"]["status"] == "FAIL"
    assert missed["overall_gate"] == "FAIL"
    report = evaluate_takeoff(
        [predicted, ordinary_item(3)],
        [gold, ordinary_item(2)],
        policy=policy(),
        predicted_view_context=predicted_context,
        gold_view_context=gold_context,
    )
    assert report["eligible_gold_rows"] == report["predicted_count"] == 2
    assert report["correct_rows"] == 1
    assert report["missing_count"] == report["unexpected_count"] == 1
    assert report["replication_recall"] == report["output_precision"] == 0.5


def test_na_values_do_not_supply_diagnostic_matching_anchors():
    predicted = ordinary_item(
        component_id=None,
        name="different name",
        plan_location="wrong plan",
        elevation="N/A",
        detail="N/A",
    )
    gold = ordinary_item(component_id=None, elevation="N/A", detail="N/A")
    report = score(predicted, gold)
    assert report["matched_count"] == 0
    assert report["missing_count"] == report["unexpected_count"] == 1


def test_one_entity_cannot_be_relabelled_as_two_supporting_views():
    predicted, context = case()
    context["views"][0]["evidence_ids"] = ["entity-dimension"]
    outcome = resolve_view_applicability(
        predicted, "elevation", prepare_view_context(context, "predicted")
    )
    assert outcome.reason == "same_evidence_reused_across_support_views"


@pytest.mark.parametrize(
    "mutation",
    [
        "missing_id",
        "wrong_component",
        "positive_display",
        "wrong_topology",
        "duplicate_support",
        "missing_review",
    ],
)
def test_receipt_edits_cannot_override_current_cad_facts(mutation):
    predicted, context = case()
    values = predicted.model_dump(mode="json")
    receipt = values["view_dispositions"]["elevation"]
    if mutation == "missing_id":
        receipt["evidence_ids"].append("entity-nonexistent")
        values["evidence_ids"].append("entity-nonexistent")
    elif mutation == "wrong_component":
        receipt["component_id"] = "component-other"
    elif mutation == "positive_display":
        values["elevation"] = "EL-01"
    elif mutation == "wrong_topology":
        receipt["basis_kind"] = "plan_elevation_without_detail"
    elif mutation == "duplicate_support":
        receipt["support_view_ids"] = ["view-plan", "view-plan"]
    else:
        # Even a Python caller bypassing Pydantic construction must fail closed.
        predicted.view_dispositions["elevation"].review.reviewer = ""
    if mutation != "missing_review":
        predicted = TakeoffItem.model_validate(values)
    outcome = resolve_view_applicability(
        predicted, "elevation", prepare_view_context(context, "predicted")
    )
    assert outcome.state == "INVALID"


def test_text_true_does_not_imply_completed_negative_search():
    predicted, context = case()
    context["searches"][0]["complete"] = "true"
    outcome = resolve_view_applicability(
        predicted, "elevation", prepare_view_context(context, "predicted")
    )
    assert outcome.reason == "view_context_invalid"
