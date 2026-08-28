"""Deterministic, policy-driven comparison of predicted and gold takeoff rows."""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from collections import Counter, defaultdict
from collections.abc import Iterable
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from statistics import mean
from typing import Any

from .calculation import evaluate_numeric_expression
from .models import (
    EvaluationAmountPolicy,
    EvaluationNumericFieldPolicy,
    EvaluationPolicy,
    EvaluationTextFieldPolicy,
    EvaluationUnfoldedSpecPolicy,
    NumericZeroHandling,
    TakeoffItem,
    TextNormalizationMode,
    UnfoldedSpecComparisonMode,
)

_TEXT_FIELDS = ("mt_code", "name", "material", "plan_location", "elevation", "detail")
_NUMERIC_FIELDS = ("width_mm", "length_mm", "quantity", "engineering_quantity")
_DIAGNOSTIC_ANCHORS = ("name", "plan_location", "elevation", "detail")


@dataclass(frozen=True)
class _Record:
    index: int
    item: TakeoffItem
    row_id: str | None


@dataclass(frozen=True)
class _Match:
    predicted: _Record
    gold: _Record
    method: str


def item_key(item: TakeoffItem) -> str:
    """Legacy deterministic row key retained for integrations and old reports."""

    return "|".join(
        str(value or "").strip().casefold()
        for value in (item.mt_code, item.name, item.plan_location, item.elevation, item.detail)
    )


def load_evaluation_policy(
    value: EvaluationPolicy | dict[str, Any] | str | None = None,
) -> EvaluationPolicy:
    """Validate a policy object, JSON string, or the conservative default."""

    if value is None:
        return EvaluationPolicy()
    if isinstance(value, EvaluationPolicy):
        return value
    if isinstance(value, str):
        return EvaluationPolicy.model_validate(json.loads(value))
    return EvaluationPolicy.model_validate(value)


def evaluation_policy_hash(policy: EvaluationPolicy) -> str:
    """Return a content hash of the canonical validated policy."""

    canonical = json.dumps(
        policy.model_dump(mode="json"),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def _normalise_text(value: Any, mode: TextNormalizationMode) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    if mode == TextNormalizationMode.STRICT:
        return text
    text = unicodedata.normalize("NFKC", text).casefold()
    text = text.translate(str.maketrans({"—": "-", "–": "-", "_": "-", "＋": "+"}))
    text = re.sub(r"\s+", " ", text).strip()
    text = re.sub(r"\s*([-+/:(),])\s*", r"\1", text)
    return text


def _identity(value: str | None) -> str | None:
    return _normalise_text(value, TextNormalizationMode.STRICT)


def _diagnostic_value(item: TakeoffItem, field: str) -> str | None:
    return _normalise_text(getattr(item, field), TextNormalizationMode.CANONICAL)


def _diagnostic_signature(item: TakeoffItem) -> tuple[str, ...] | None:
    values = tuple(
        _diagnostic_value(item, field) or "" for field in ("mt_code", *_DIAGNOSTIC_ANCHORS)
    )
    # A material code alone is not enough to identify a physical component.
    return values if values[0] and sum(bool(value) for value in values[1:]) >= 1 else None


def _record_sort_key(record: _Record) -> tuple[int, int]:
    return record.item.sequence, record.index


def _record_reference(record: _Record, side: str) -> str:
    identity = record.row_id or record.item.component_id
    suffix = f":{identity}" if identity else ""
    return f"{side}:{record.index + 1}:seq-{record.item.sequence}{suffix}"


def _row_ids(values: Iterable[str | None] | None, count: int, label: str) -> list[str | None]:
    if values is None:
        return [None] * count
    result = [
        str(value).strip() if value is not None and str(value).strip() else None for value in values
    ]
    if len(result) != count:
        raise ValueError(f"{label} row-id count does not match row count")
    return result


def _group_records(records: Iterable[_Record], accessor: Any) -> dict[Any, list[_Record]]:
    groups: dict[Any, list[_Record]] = defaultdict(list)
    for record in records:
        value = accessor(record)
        if value:
            groups[value].append(record)
    return groups


def _duplicate_report(
    category: str,
    predicted_groups: dict[Any, list[_Record]],
    gold_groups: dict[Any, list[_Record]],
    duplicate_predicted: set[int],
    duplicate_gold: set[int],
) -> list[dict[str, Any]]:
    report: list[dict[str, Any]] = []
    for value in sorted(set(predicted_groups) | set(gold_groups), key=str):
        predicted_values = sorted(predicted_groups.get(value, []), key=_record_sort_key)
        gold_values = sorted(gold_groups.get(value, []), key=_record_sort_key)
        if len(predicted_values) <= 1 and len(gold_values) <= 1:
            continue
        duplicate_predicted.update(record.index for record in predicted_values[1:])
        duplicate_gold.update(record.index for record in gold_values[1:])
        report.append(
            {
                "category": category,
                "value": str(value),
                "predicted_count": len(predicted_values),
                "gold_count": len(gold_values),
                "predicted_rows": [
                    _record_reference(record, "predicted") for record in predicted_values
                ],
                "gold_rows": [_record_reference(record, "gold") for record in gold_values],
            }
        )
    return report


def _match_rows(
    predicted: list[_Record], gold: list[_Record]
) -> tuple[list[_Match], list[_Record], list[_Record], dict[str, Any]]:
    unmatched_predicted = {record.index: record for record in predicted}
    unmatched_gold = {record.index: record for record in gold}
    matches: list[_Match] = []
    duplicate_predicted: set[int] = set()
    duplicate_gold: set[int] = set()
    duplicate_groups: list[dict[str, Any]] = []
    blocking_gold_duplicate_groups: list[dict[str, Any]] = []

    def unique_identity_stage(label: str, accessor: Any) -> None:
        predicted_groups = _group_records(unmatched_predicted.values(), accessor)
        gold_groups = _group_records(unmatched_gold.values(), accessor)
        duplicate_values = _duplicate_report(
            label,
            predicted_groups,
            gold_groups,
            duplicate_predicted,
            duplicate_gold,
        )
        duplicate_groups.extend(duplicate_values)
        blocking_gold_duplicate_groups.extend(
            value for value in duplicate_values if value["gold_count"] > 1
        )
        for value in sorted(set(predicted_groups) & set(gold_groups), key=str):
            predicted_values = predicted_groups[value]
            gold_values = gold_groups[value]
            if len(predicted_values) != 1 or len(gold_values) != 1:
                continue
            predicted_record = predicted_values[0]
            gold_record = gold_values[0]
            matches.append(_Match(predicted_record, gold_record, label))
            unmatched_predicted.pop(predicted_record.index, None)
            unmatched_gold.pop(gold_record.index, None)

    unique_identity_stage("component_id", lambda record: _identity(record.item.component_id))
    unique_identity_stage("stable_row_id", lambda record: _identity(record.row_id))

    predicted_signatures = _group_records(
        unmatched_predicted.values(), lambda record: _diagnostic_signature(record.item)
    )
    gold_signatures = _group_records(
        unmatched_gold.values(), lambda record: _diagnostic_signature(record.item)
    )
    duplicate_groups.extend(
        _duplicate_report(
            "diagnostic_signature",
            predicted_signatures,
            gold_signatures,
            duplicate_predicted,
            duplicate_gold,
        )
    )
    for signature in sorted(set(predicted_signatures) & set(gold_signatures), key=str):
        predicted_values = sorted(predicted_signatures[signature], key=_record_sort_key)
        gold_values = sorted(gold_signatures[signature], key=_record_sort_key)
        # A categorical signature is an identity only when it is unique on both
        # sides.  Pairing duplicate groups by sequence/order would leak workbook
        # row order into the score and could manufacture correct rows when two
        # physical components share the same labels.
        if len(predicted_values) != 1 or len(gold_values) != 1:
            continue
        predicted_record = predicted_values[0]
        gold_record = gold_values[0]
        matches.append(_Match(predicted_record, gold_record, "diagnostic_signature"))
        unmatched_predicted.pop(predicted_record.index, None)
        unmatched_gold.pop(gold_record.index, None)

    # Conservative fallback for one wrong categorical field. A match must share a
    # non-empty material code, agree on at least two other categorical anchors, and
    # be the unique best candidate in both directions. Numeric proximity is never
    # considered, so a close engineering quantity cannot manufacture a correct row.
    while unmatched_predicted and unmatched_gold:
        scores: dict[tuple[int, int], int] = {}
        for predicted_record in unmatched_predicted.values():
            predicted_mt = _diagnostic_value(predicted_record.item, "mt_code")
            if not predicted_mt:
                continue
            for gold_record in unmatched_gold.values():
                if predicted_mt != _diagnostic_value(gold_record.item, "mt_code"):
                    continue
                anchor_agreements = 0
                for field in _DIAGNOSTIC_ANCHORS:
                    predicted_value = _diagnostic_value(predicted_record.item, field)
                    gold_value = _diagnostic_value(gold_record.item, field)
                    if predicted_value and predicted_value == gold_value:
                        anchor_agreements += 1
                if anchor_agreements >= 2:
                    scores[(predicted_record.index, gold_record.index)] = anchor_agreements
        if not scores:
            break

        predicted_best: dict[int, tuple[int, int] | None] = {}
        for predicted_index in unmatched_predicted:
            values = [
                (score, gold_index)
                for (candidate_index, gold_index), score in scores.items()
                if candidate_index == predicted_index
            ]
            if not values:
                continue
            best_score = max(score for score, _ in values)
            best_gold = [gold_index for score, gold_index in values if score == best_score]
            predicted_best[predicted_index] = (
                (best_score, best_gold[0]) if len(best_gold) == 1 else None
            )

        gold_best: dict[int, tuple[int, int] | None] = {}
        for gold_index in unmatched_gold:
            values = [
                (score, predicted_index)
                for (predicted_index, candidate_index), score in scores.items()
                if candidate_index == gold_index
            ]
            if not values:
                continue
            best_score = max(score for score, _ in values)
            best_predicted = [
                predicted_index for score, predicted_index in values if score == best_score
            ]
            gold_best[gold_index] = (
                (best_score, best_predicted[0]) if len(best_predicted) == 1 else None
            )

        mutual: list[tuple[int, int]] = []
        for predicted_index, predicted_choice in predicted_best.items():
            if predicted_choice is None:
                continue
            score, gold_index = predicted_choice
            gold_choice = gold_best.get(gold_index)
            if gold_choice == (score, predicted_index):
                mutual.append((predicted_index, gold_index))
        if not mutual:
            break
        for predicted_index, gold_index in sorted(mutual):
            predicted_record = unmatched_predicted.pop(predicted_index)
            gold_record = unmatched_gold.pop(gold_index)
            matches.append(_Match(predicted_record, gold_record, "diagnostic_mutual_best"))

    matches.sort(key=lambda value: _record_sort_key(value.gold))
    diagnostics = {
        "duplicate_groups": duplicate_groups,
        "duplicate_predicted_count": len(duplicate_predicted),
        "duplicate_gold_count": len(duplicate_gold),
        "blocking_gold_duplicate_groups": blocking_gold_duplicate_groups,
    }
    return (
        matches,
        sorted(unmatched_predicted.values(), key=_record_sort_key),
        sorted(unmatched_gold.values(), key=_record_sort_key),
        diagnostics,
    )


def _enabled_rules(policy: EvaluationPolicy) -> list[tuple[str, Any]]:
    rules: list[tuple[str, Any]] = []
    for field in _TEXT_FIELDS:
        rule = getattr(policy, field)
        if rule.enabled:
            rules.append((field, rule))
    if policy.unfolded_spec.enabled:
        rules.append(("unfolded_spec", policy.unfolded_spec))
    for field in _NUMERIC_FIELDS:
        rule = getattr(policy, field)
        if rule.enabled:
            rules.append((field, rule))
    if policy.amount.enabled:
        rules.append(("amount", policy.amount))
    return rules


def _pending_policy_fields(policy: EvaluationPolicy) -> list[str]:
    pending: list[str] = []
    for field in _NUMERIC_FIELDS:
        rule: EvaluationNumericFieldPolicy = getattr(policy, field)
        if not rule.enabled:
            continue
        if rule.relative_tolerance is None:
            pending.append(f"{field}.relative_tolerance")
        if (
            rule.zero_handling == NumericZeroHandling.ABSOLUTE
            and rule.zero_absolute_tolerance is None
        ):
            pending.append(f"{field}.zero_absolute_tolerance")
    if policy.amount.enabled and not policy.amount.exact:
        pending.append("amount.exact")
    return pending


def _present(value: Any) -> bool:
    if value is None:
        return False
    return bool(str(value).strip()) if isinstance(value, str) else True


def _required_gold_missing(item: TakeoffItem, policy: EvaluationPolicy) -> list[str]:
    missing: list[str] = []
    for field, rule in _enabled_rules(policy):
        if rule.required_in_gold and not _present(getattr(item, field)):
            missing.append(field)
    if not item.evidence_ids:
        missing.append("source_evidence")
    return missing


def _base_result(
    field: str,
    predicted: Any,
    gold: Any,
    status: str,
    reason: str,
    **details: Any,
) -> dict[str, Any]:
    return {
        "field": field,
        "status": status,
        "reason": reason,
        "predicted": predicted,
        "gold": gold,
        **details,
    }


def _compare_text(
    field: str,
    predicted: Any,
    gold: Any,
    rule: EvaluationTextFieldPolicy,
) -> dict[str, Any]:
    gold_normalised = _normalise_text(gold, rule.normalization)
    predicted_normalised = _normalise_text(predicted, rule.normalization)
    if gold_normalised is None:
        return _base_result(
            field,
            predicted,
            gold,
            "UNRESOLVED",
            "gold_value_missing",
            normalization=rule.normalization.value,
        )
    if predicted_normalised is None:
        return _base_result(
            field,
            predicted,
            gold,
            "FAIL",
            "predicted_value_missing",
            normalization=rule.normalization.value,
            normalized_gold=gold_normalised,
        )
    status = "PASS" if predicted_normalised == gold_normalised else "FAIL"
    return _base_result(
        field,
        predicted,
        gold,
        status,
        "equal_after_normalization" if status == "PASS" else "text_mismatch",
        normalization=rule.normalization.value,
        normalized_predicted=predicted_normalised,
        normalized_gold=gold_normalised,
    )


def _unfolded_total(value: Any) -> Decimal:
    text = unicodedata.normalize("NFKC", str(value)).strip()
    text = re.sub(r"(?i)\s*(?:mm|毫米)\s*$", "", text)
    if "=" not in text:
        return evaluate_numeric_expression(text)
    parts = [part.strip() for part in text.split("=") if part.strip()]
    if len(parts) < 2:
        raise ValueError("invalid unfolded expression")
    values = [evaluate_numeric_expression(part) for part in parts]
    if any(value != values[-1] for value in values[:-1]):
        raise ValueError("unfolded expression equality is inconsistent")
    return values[-1]


def _compare_unfolded(
    predicted: Any,
    gold: Any,
    rule: EvaluationUnfoldedSpecPolicy,
) -> dict[str, Any]:
    if rule.mode == UnfoldedSpecComparisonMode.STRICT:
        text_rule = EvaluationTextFieldPolicy(
            enabled=True,
            required_in_gold=rule.required_in_gold,
            normalization=rule.normalization,
        )
        result = _compare_text("unfolded_spec", predicted, gold, text_rule)
        result["comparison_mode"] = rule.mode.value
        return result
    if not _present(gold):
        return _base_result(
            "unfolded_spec",
            predicted,
            gold,
            "UNRESOLVED",
            "gold_value_missing",
            comparison_mode=rule.mode.value,
        )
    if not _present(predicted):
        return _base_result(
            "unfolded_spec",
            predicted,
            gold,
            "FAIL",
            "predicted_value_missing",
            comparison_mode=rule.mode.value,
        )
    try:
        gold_total = _unfolded_total(gold)
    except (ValueError, InvalidOperation):
        return _base_result(
            "unfolded_spec",
            predicted,
            gold,
            "UNRESOLVED",
            "gold_expression_not_evaluable",
            comparison_mode=rule.mode.value,
        )
    try:
        predicted_total = _unfolded_total(predicted)
    except (ValueError, InvalidOperation):
        return _base_result(
            "unfolded_spec",
            predicted,
            gold,
            "FAIL",
            "predicted_expression_not_evaluable",
            comparison_mode=rule.mode.value,
            evaluated_gold=str(gold_total),
        )
    status = "PASS" if predicted_total == gold_total else "FAIL"
    return _base_result(
        "unfolded_spec",
        predicted,
        gold,
        status,
        "evaluated_totals_equal" if status == "PASS" else "evaluated_totals_differ",
        comparison_mode=rule.mode.value,
        evaluated_predicted=str(predicted_total),
        evaluated_gold=str(gold_total),
    )


def _decimal(value: Any) -> Decimal:
    return Decimal(str(value))


def _compare_numeric(
    field: str,
    predicted: Any,
    gold: Any,
    rule: EvaluationNumericFieldPolicy,
) -> dict[str, Any]:
    if gold is None:
        return _base_result(field, predicted, gold, "UNRESOLVED", "gold_value_missing")
    if predicted is None:
        return _base_result(field, predicted, gold, "FAIL", "predicted_value_missing")
    gold_value = _decimal(gold)
    predicted_value = _decimal(predicted)
    error = abs(predicted_value - gold_value)
    if gold_value == 0:
        if rule.zero_handling == NumericZeroHandling.UNRESOLVED:
            return _base_result(
                field,
                predicted,
                gold,
                "UNRESOLVED",
                "gold_zero_policy_unresolved",
                absolute_error=str(error),
                zero_handling=rule.zero_handling.value,
            )
        if rule.zero_handling == NumericZeroHandling.EXACT:
            passed = error == 0
            threshold = Decimal("0")
        else:
            if rule.zero_absolute_tolerance is None:
                return _base_result(
                    field,
                    predicted,
                    gold,
                    "UNRESOLVED",
                    "zero_absolute_tolerance_pending",
                    absolute_error=str(error),
                    zero_handling=rule.zero_handling.value,
                )
            threshold = _decimal(rule.zero_absolute_tolerance)
            passed = error <= threshold
        return _base_result(
            field,
            predicted,
            gold,
            "PASS" if passed else "FAIL",
            "within_zero_rule" if passed else "outside_zero_rule",
            absolute_error=str(error),
            zero_handling=rule.zero_handling.value,
            zero_absolute_tolerance=str(threshold),
        )
    if rule.relative_tolerance is None:
        return _base_result(
            field,
            predicted,
            gold,
            "UNRESOLVED",
            "relative_tolerance_pending",
            absolute_error=str(error),
        )
    relative_error = error / abs(gold_value)
    threshold = _decimal(rule.relative_tolerance)
    passed = relative_error <= threshold
    return _base_result(
        field,
        predicted,
        gold,
        "PASS" if passed else "FAIL",
        "within_relative_tolerance" if passed else "outside_relative_tolerance",
        absolute_error=str(error),
        relative_error=str(relative_error),
        relative_tolerance=str(threshold),
        boundary_inclusive=True,
    )


def _compare_amount(
    predicted: Any,
    gold: Any,
    rule: EvaluationAmountPolicy,
) -> dict[str, Any]:
    if gold is None or predicted is None:
        return _base_result(
            "amount",
            predicted,
            gold,
            "UNRESOLVED",
            "amount_requires_values_on_both_sides",
            exact=rule.exact,
        )
    if not rule.exact:
        return _base_result(
            "amount",
            predicted,
            gold,
            "UNRESOLVED",
            "amount_exact_rule_pending",
            exact=False,
        )
    passed = _decimal(predicted) == _decimal(gold)
    return _base_result(
        "amount",
        predicted,
        gold,
        "PASS" if passed else "FAIL",
        "exact_amount_equal" if passed else "exact_amount_mismatch",
        exact=True,
        relative_tolerance="0",
    )


def _compare_source_evidence(
    predicted: TakeoffItem,
    gold: TakeoffItem,
) -> dict[str, Any]:
    if not gold.evidence_ids:
        return _base_result(
            "source_evidence",
            predicted.evidence_ids,
            gold.evidence_ids,
            "UNRESOLVED",
            "gold_source_evidence_missing",
        )
    if not predicted.evidence_ids:
        return _base_result(
            "source_evidence",
            predicted.evidence_ids,
            gold.evidence_ids,
            "FAIL",
            "predicted_source_evidence_missing",
        )
    return _base_result(
        "source_evidence",
        predicted.evidence_ids,
        gold.evidence_ids,
        "PASS",
        "source_evidence_present_on_both_sides",
    )


def _compare_fields(
    predicted: TakeoffItem,
    gold: TakeoffItem,
    policy: EvaluationPolicy,
) -> dict[str, dict[str, Any]]:
    results: dict[str, dict[str, Any]] = {}
    for field, rule in _enabled_rules(policy):
        predicted_value = getattr(predicted, field)
        gold_value = getattr(gold, field)
        if isinstance(rule, EvaluationTextFieldPolicy):
            result = _compare_text(field, predicted_value, gold_value, rule)
        elif isinstance(rule, EvaluationUnfoldedSpecPolicy):
            result = _compare_unfolded(predicted_value, gold_value, rule)
        elif isinstance(rule, EvaluationNumericFieldPolicy):
            result = _compare_numeric(field, predicted_value, gold_value, rule)
        elif isinstance(rule, EvaluationAmountPolicy):
            result = _compare_amount(predicted_value, gold_value, rule)
        else:  # pragma: no cover - strict model types make this unreachable
            raise TypeError(f"unsupported evaluation rule for {field}")
        results[field] = result
    results["source_evidence"] = _compare_source_evidence(predicted, gold)
    return results


def _missing_prediction_fields(
    gold: TakeoffItem, policy: EvaluationPolicy
) -> dict[str, dict[str, Any]]:
    results: dict[str, dict[str, Any]] = {}
    for field, _ in _enabled_rules(policy):
        gold_value = getattr(gold, field)
        status = "FAIL" if _present(gold_value) else "UNRESOLVED"
        results[field] = _base_result(
            field,
            None,
            gold_value,
            status,
            "predicted_row_missing" if status == "FAIL" else "gold_value_missing",
        )
    evidence_status = "FAIL" if gold.evidence_ids else "UNRESOLVED"
    results["source_evidence"] = _base_result(
        "source_evidence",
        None,
        gold.evidence_ids,
        evidence_status,
        ("predicted_row_missing" if evidence_status == "FAIL" else "gold_source_evidence_missing"),
    )
    return results


def _legacy_metrics(
    predicted: list[TakeoffItem],
    gold: list[TakeoffItem],
    matches: list[_Match],
    missing: list[_Record],
    unexpected: list[_Record],
    tolerance: float,
) -> dict[str, Any]:
    absolute_errors: list[float] = []
    relative_errors: list[float] = []
    exact = 0
    comparable = 0
    matched_exact_indices: set[int] = set()
    for match in matches:
        predicted_quantity = match.predicted.item.engineering_quantity
        gold_quantity = match.gold.item.engineering_quantity
        if predicted_quantity is None or gold_quantity is None:
            continue
        comparable += 1
        error = abs(predicted_quantity - gold_quantity)
        absolute_errors.append(error)
        if abs(gold_quantity) > tolerance:
            relative_errors.append(error / abs(gold_quantity))
        if error <= tolerance:
            exact += 1
            matched_exact_indices.add(match.predicted.index)
    pass_indices = {index for index, item in enumerate(predicted) if item.status.value == "PASS"}
    pass_correct = len(pass_indices & matched_exact_indices)
    complete_chain = sum(
        bool(item.plan_location and item.elevation and item.detail and item.evidence_ids)
        for item in predicted
    )
    complete_measurements = sum(
        bool(item.unfolded_spec and item.length_mm is not None and item.quantity is not None)
        for item in predicted
    )
    priced = sum(item.unit_price is not None for item in predicted)
    status_counts = {
        status: sum(item.status.value == status for item in predicted)
        for status in ("PASS", "REVIEW", "BLOCK")
    }
    return {
        "predicted_count": len(predicted),
        "gold_count": len(gold),
        "matched_count": len(matches),
        "missing_count": len(missing),
        "unexpected_count": len(unexpected),
        "missing_keys": [_record_reference(record, "gold") for record in missing],
        "unexpected_keys": [_record_reference(record, "predicted") for record in unexpected],
        "strict_row_recall": len(matches) / len(gold) if gold else None,
        "strict_row_precision": len(matches) / len(predicted) if predicted else None,
        "comparable_quantity_count": comparable,
        "quantity_exact_count": exact,
        "quantity_exact_rate": exact / comparable if comparable else None,
        "quantity_mae": mean(absolute_errors) if absolute_errors else None,
        "quantity_mape": mean(relative_errors) if relative_errors else None,
        "pass_count": len(pass_indices),
        "pass_precision": pass_correct / len(pass_indices) if pass_indices else None,
        "automation_rate": len(pass_indices) / len(predicted) if predicted else None,
        "pass_recall": pass_correct / len(gold) if gold else None,
        "evidence_chain_coverage": complete_chain / len(predicted) if predicted else None,
        "measurement_coverage": complete_measurements / len(predicted) if predicted else None,
        "price_coverage": priced / len(predicted) if predicted else None,
        "status_counts": status_counts,
        "legacy_engineering_absolute_tolerance": tolerance,
    }


def evaluate_takeoff(
    predicted: list[TakeoffItem],
    gold: list[TakeoffItem],
    *,
    tolerance: float = 1e-3,
    policy: EvaluationPolicy | dict[str, Any] | str | None = None,
    predicted_row_ids: Iterable[str | None] | None = None,
    gold_row_ids: Iterable[str | None] | None = None,
    project_id: str = "project",
) -> dict[str, Any]:
    """Evaluate one project under a versioned, auditable acceptance policy.

    Identity matching uses common component IDs and stable external row IDs first.
    The fallback is a conservative categorical diagnostic match; it never uses
    engineering-quantity proximity.  The legacy metric names remain available for
    existing integrations, while ``overall_gate`` is the authoritative 95% result.
    """

    validated_policy = load_evaluation_policy(policy)
    predicted_ids = _row_ids(predicted_row_ids, len(predicted), "predicted")
    gold_ids = _row_ids(gold_row_ids, len(gold), "gold")
    predicted_records = [
        _Record(index, item, predicted_ids[index]) for index, item in enumerate(predicted)
    ]
    gold_records = [_Record(index, item, gold_ids[index]) for index, item in enumerate(gold)]
    matches, unexpected, missing, matching_diagnostics = _match_rows(
        predicted_records, gold_records
    )
    match_by_gold = {match.gold.index: match for match in matches}

    invalid_gold_rows: list[dict[str, Any]] = []
    eligible_gold_indices: set[int] = set()
    for record in gold_records:
        missing_fields = _required_gold_missing(record.item, validated_policy)
        if missing_fields:
            invalid_gold_rows.append(
                {
                    "gold_row": _record_reference(record, "gold"),
                    "missing_required_fields": missing_fields,
                }
            )
        else:
            eligible_gold_indices.add(record.index)

    field_summary = {
        field: {"PASS": 0, "FAIL": 0, "UNRESOLVED": 0}
        for field, _ in _enabled_rules(validated_policy)
    }
    field_summary["source_evidence"] = {"PASS": 0, "FAIL": 0, "UNRESOLVED": 0}
    row_results: list[dict[str, Any]] = []
    correct_gold_indices: set[int] = set()
    unresolved_reasons: set[str] = set()
    for gold_record in sorted(gold_records, key=_record_sort_key):
        match = match_by_gold.get(gold_record.index)
        if match is None:
            field_results = _missing_prediction_fields(gold_record.item, validated_policy)
        else:
            field_results = _compare_fields(
                match.predicted.item,
                gold_record.item,
                validated_policy,
            )
        for field, result in field_results.items():
            field_summary[field][result["status"]] += 1
            if result["status"] == "UNRESOLVED":
                unresolved_reasons.add(f"{field}:{result['reason']}")
        row_correct = (
            gold_record.index in eligible_gold_indices
            and match is not None
            and all(result["status"] == "PASS" for result in field_results.values())
        )
        if row_correct:
            correct_gold_indices.add(gold_record.index)
        row_results.append(
            {
                "gold_row": _record_reference(gold_record, "gold"),
                "predicted_row": (
                    _record_reference(match.predicted, "predicted") if match else None
                ),
                "gold_sequence": gold_record.item.sequence,
                "predicted_sequence": match.predicted.item.sequence if match else None,
                "match_method": match.method if match else None,
                "match_status": "MATCHED" if match else "MISSING_PREDICTION",
                "eligible_gold": gold_record.index in eligible_gold_indices,
                "row_correct": row_correct,
                "field_results": field_results,
            }
        )

    eligible_gold_rows = len(eligible_gold_indices)
    correct_rows = len(correct_gold_indices)
    replication_recall = correct_rows / eligible_gold_rows if eligible_gold_rows else None
    output_precision = correct_rows / len(predicted) if predicted else 0.0
    pending_fields = _pending_policy_fields(validated_policy)
    blocking_gold_duplicates = matching_diagnostics["blocking_gold_duplicate_groups"]
    has_unresolved = bool(unresolved_reasons)
    if invalid_gold_rows or blocking_gold_duplicates or not gold:
        overall_gate = "BLOCKED"
        meets_target: bool | None = None
    elif pending_fields or has_unresolved or not eligible_gold_rows:
        overall_gate = "INDETERMINATE"
        meets_target = None
    else:
        meets_target = bool(
            replication_recall is not None
            and replication_recall >= validated_policy.target_accuracy
            and output_precision >= validated_policy.target_accuracy
        )
        overall_gate = "PASS" if meets_target else "FAIL"

    project_report = {
        "project_id": project_id,
        "gold_rows": len(gold),
        "predicted_rows": len(predicted),
        "eligible_gold_rows": eligible_gold_rows,
        "correct_rows": correct_rows,
        "incorrect_eligible_gold_rows": eligible_gold_rows - correct_rows,
        "replication_recall": replication_recall,
        "output_precision": output_precision,
        "target_accuracy": validated_policy.target_accuracy,
        "meets_target": meets_target,
        "overall_gate": overall_gate,
        "missing_rows": [_record_reference(record, "gold") for record in missing],
        "unexpected_rows": [_record_reference(record, "predicted") for record in unexpected],
        "duplicate_predicted_count": matching_diagnostics["duplicate_predicted_count"],
        "duplicate_gold_count": matching_diagnostics["duplicate_gold_count"],
        "duplicate_groups": matching_diagnostics["duplicate_groups"],
        "invalid_gold_rows": invalid_gold_rows,
        "policy_pending_fields": pending_fields,
        "unresolved_reasons": sorted(unresolved_reasons),
        "field_summary": field_summary,
        "row_results": row_results,
    }
    legacy = _legacy_metrics(
        predicted,
        gold,
        matches,
        missing,
        unexpected,
        tolerance,
    )
    return {
        "evaluation_schema_version": "2.0",
        "policy_version": validated_policy.policy_version,
        "policy_hash": evaluation_policy_hash(validated_policy),
        "policy": validated_policy.model_dump(mode="json"),
        "project_id": project_id,
        "projects": [project_report],
        "aggregate": {
            key: project_report[key]
            for key in (
                "gold_rows",
                "predicted_rows",
                "eligible_gold_rows",
                "correct_rows",
                "replication_recall",
                "output_precision",
                "target_accuracy",
                "meets_target",
                "overall_gate",
            )
        },
        "eligible_gold_rows": eligible_gold_rows,
        "correct_rows": correct_rows,
        "replication_recall": replication_recall,
        "output_precision": output_precision,
        "target_accuracy": validated_policy.target_accuracy,
        "meets_target": meets_target,
        "overall_gate": overall_gate,
        "policy_pending_fields": pending_fields,
        "invalid_gold_rows": invalid_gold_rows,
        "duplicate_groups": matching_diagnostics["duplicate_groups"],
        "duplicate_predicted_count": matching_diagnostics["duplicate_predicted_count"],
        "duplicate_gold_count": matching_diagnostics["duplicate_gold_count"],
        "field_summary": field_summary,
        "row_results": row_results,
        **legacy,
    }


def summarize_evaluation_batch(
    projects: list[dict[str, Any]],
    *,
    batch_id: str,
    manifest_sha256: str | None = None,
) -> dict[str, Any]:
    """Build conservative macro/micro metrics without masking a project failure.

    ``projects`` contains compact per-project summaries produced from independent
    :func:`evaluate_takeoff` calls. Aggregate percentages are diagnostic only: the
    batch gate is PASS exclusively when every project gate is PASS.
    """

    gate_order = ("BLOCKED", "INDETERMINATE", "FAIL", "PASS")
    gate_counts = {
        gate: sum(project.get("overall_gate") == gate for project in projects)
        for gate in gate_order
    }
    unknown_gates = sorted(
        {
            str(project.get("overall_gate"))
            for project in projects
            if project.get("overall_gate") not in gate_order
        }
    )
    project_ids = [str(project.get("project_id") or "").strip() for project in projects]
    project_id_counts = Counter(project_ids)
    invalid_project_ids = sorted(
        {
            project_id
            for project_id in project_ids
            if not project_id or project_id_counts[project_id] > 1
        }
    )
    if not projects or unknown_gates or invalid_project_ids:
        overall_gate = "BLOCKED"
    else:
        overall_gate = next(
            (gate for gate in gate_order if gate_counts[gate]),
            "BLOCKED",
        )

    def total(field: str) -> int:
        return sum(int(value) for project in projects if (value := project.get(field)) is not None)

    def defined_mean(field: str) -> float | None:
        values = [float(value) for project in projects if (value := project.get(field)) is not None]
        return mean(values) if values else None

    gold_rows = total("gold_rows")
    predicted_rows = total("predicted_rows")
    eligible_gold_rows = total("eligible_gold_rows")
    correct_rows = total("correct_rows")
    matched_rows = total("matched_rows")
    missing_rows = total("missing_rows")
    unexpected_rows = total("unexpected_rows")
    micro_recall = correct_rows / eligible_gold_rows if eligible_gold_rows else None
    micro_precision = correct_rows / predicted_rows if predicted_rows else 0.0
    all_projects_pass = (
        bool(projects) and not invalid_project_ids and gate_counts["PASS"] == len(projects)
    )
    return {
        "batch_evaluation_schema_version": "1.0",
        "batch_id": batch_id,
        "manifest_sha256": manifest_sha256,
        "overall_gate": overall_gate,
        "all_projects_pass": all_projects_pass,
        "unknown_gates": unknown_gates,
        "invalid_project_ids": invalid_project_ids,
        "project_count": len(projects),
        "gate_counts": gate_counts,
        "aggregate": {
            "gold_rows": gold_rows,
            "predicted_rows": predicted_rows,
            "eligible_gold_rows": eligible_gold_rows,
            "correct_rows": correct_rows,
            "matched_rows": matched_rows,
            "missing_rows": missing_rows,
            "unexpected_rows": unexpected_rows,
            "micro_replication_recall": micro_recall,
            "micro_output_precision": micro_precision,
            "macro_replication_recall": defined_mean("replication_recall"),
            "macro_output_precision": defined_mean("output_precision"),
            "project_pass_rate": (gate_counts["PASS"] / len(projects) if projects else None),
        },
        "projects": projects,
        "gate_rule": (
            "PASS only when every project PASS; aggregate rates never override "
            "BLOCKED, INDETERMINATE, or FAIL projects"
        ),
    }


def evaluation_batch_markdown(summary: dict[str, Any]) -> str:
    """Render a compact, deterministic Markdown companion to a batch report."""

    def escaped(value: Any) -> str:
        return (
            str(value if value is not None else "—")
            .replace("\\", "\\\\")
            .replace("|", "\\|")
            .replace("`", "\\`")
            .replace("\r", " ")
            .replace("\n", " ")
        )

    def percentage(value: Any) -> str:
        return "—" if value is None else f"{float(value):.2%}"

    aggregate = summary.get("aggregate", {})
    lines = [
        "# CAD takeoff batch evaluation",
        "",
        f"- Batch: `{escaped(summary.get('batch_id'))}`",
        f"- Overall gate: **{escaped(summary.get('overall_gate'))}**",
        f"- Projects: {int(summary.get('project_count', 0))}",
        (
            "- Micro recall / precision: "
            f"{percentage(aggregate.get('micro_replication_recall'))} / "
            f"{percentage(aggregate.get('micro_output_precision'))}"
        ),
        "",
        "> Aggregate percentages are diagnostic. Every project must PASS individually; "
        "missing evidence never becomes PASS.",
        "",
        "| Project | Gate | Gold | Predicted | Eligible | Correct | Matched | "
        "Recall | Precision | Missing | Extra | Report |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---|",
    ]
    for project in summary.get("projects", []):
        report_path = project.get("report_path")
        report_cell = f"[{escaped(report_path)}]({escaped(report_path)})" if report_path else "—"
        lines.append(
            "| "
            + " | ".join(
                (
                    escaped(project.get("project_id")),
                    escaped(project.get("overall_gate")),
                    escaped(project.get("gold_rows")),
                    escaped(project.get("predicted_rows")),
                    escaped(project.get("eligible_gold_rows")),
                    escaped(project.get("correct_rows")),
                    escaped(project.get("matched_rows")),
                    percentage(project.get("replication_recall")),
                    percentage(project.get("output_precision")),
                    escaped(project.get("missing_rows")),
                    escaped(project.get("unexpected_rows")),
                    report_cell,
                )
            )
            + " |"
        )
    lines.extend(
        [
            "",
            "## Totals",
            "",
            f"- Gold / predicted: {int(aggregate.get('gold_rows', 0))} / "
            f"{int(aggregate.get('predicted_rows', 0))}",
            f"- Eligible / correct: {int(aggregate.get('eligible_gold_rows', 0))} / "
            f"{int(aggregate.get('correct_rows', 0))}",
            f"- Missing / extra: {int(aggregate.get('missing_rows', 0))} / "
            f"{int(aggregate.get('unexpected_rows', 0))}",
            "",
        ]
    )
    errors = [project for project in summary.get("projects", []) if project.get("error")]
    if errors:
        lines.extend(["## Project errors", ""])
        for project in errors:
            error = project["error"]
            lines.append(
                f"- `{escaped(project.get('project_id'))}`: "
                f"{escaped(error.get('type'))}: {escaped(error.get('message'))}"
            )
        lines.append("")
    return "\n".join(lines)
