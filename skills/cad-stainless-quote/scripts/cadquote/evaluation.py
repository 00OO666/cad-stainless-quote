"""Gold-set comparison for takeoff rows."""

from __future__ import annotations

from collections import defaultdict
from statistics import mean
from typing import Any

from .models import TakeoffItem


def item_key(item: TakeoffItem) -> str:
    return "|".join(
        str(value or "").strip().casefold()
        for value in (item.mt_code, item.name, item.plan_location, item.elevation, item.detail)
    )


def evaluate_takeoff(
    predicted: list[TakeoffItem], gold: list[TakeoffItem], *, tolerance: float = 1e-3
) -> dict[str, Any]:
    """Compare row multisets without collapsing duplicate human line items."""

    predicted_groups: dict[str, list[TakeoffItem]] = defaultdict(list)
    gold_groups: dict[str, list[TakeoffItem]] = defaultdict(list)
    for item in predicted:
        predicted_groups[item_key(item)].append(item)
    for item in gold:
        gold_groups[item_key(item)].append(item)
    matched_pairs: list[tuple[str, TakeoffItem, TakeoffItem]] = []
    missing: list[str] = []
    unexpected: list[str] = []
    for key in sorted(set(predicted_groups) | set(gold_groups)):
        predicted_values = sorted(predicted_groups.get(key, []), key=lambda item: item.sequence)
        gold_values = sorted(gold_groups.get(key, []), key=lambda item: item.sequence)
        available = list(predicted_values)
        for gold_item in gold_values:
            if not available:
                missing.append(f"{key}#gold:{gold_item.sequence}")
                continue
            if gold_item.engineering_quantity is None:
                selected_index = 0
            else:
                selected_index = min(
                    range(len(available)),
                    key=lambda index: (
                        abs(
                            (available[index].engineering_quantity or 0)
                            - gold_item.engineering_quantity
                        )
                        if available[index].engineering_quantity is not None
                        else float("inf"),
                        available[index].sequence,
                    ),
                )
            predicted_item = available.pop(selected_index)
            matched_pairs.append((key, predicted_item, gold_item))
        unexpected.extend(f"{key}#pred:{item.sequence}" for item in available)

    absolute_errors: list[float] = []
    relative_errors: list[float] = []
    exact = 0
    comparable = 0
    matched_exact_ids: set[int] = set()
    for _, predicted_item, gold_item in matched_pairs:
        predicted_quantity = predicted_item.engineering_quantity
        gold_quantity = gold_item.engineering_quantity
        if predicted_quantity is None or gold_quantity is None:
            continue
        comparable += 1
        error = abs(predicted_quantity - gold_quantity)
        absolute_errors.append(error)
        if abs(gold_quantity) > tolerance:
            relative_errors.append(error / abs(gold_quantity))
        if error <= tolerance:
            exact += 1
            matched_exact_ids.add(id(predicted_item))
    pass_items = [item for item in predicted if item.status.value == "PASS"]
    pass_correct = sum(id(item) in matched_exact_ids for item in pass_items)
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
        "matched_count": len(matched_pairs),
        "missing_count": len(missing),
        "unexpected_count": len(unexpected),
        "missing_keys": missing,
        "unexpected_keys": unexpected,
        "strict_row_recall": len(matched_pairs) / len(gold) if gold else None,
        "strict_row_precision": len(matched_pairs) / len(predicted) if predicted else None,
        "comparable_quantity_count": comparable,
        "quantity_exact_count": exact,
        "quantity_exact_rate": exact / comparable if comparable else None,
        "quantity_mae": mean(absolute_errors) if absolute_errors else None,
        "quantity_mape": mean(relative_errors) if relative_errors else None,
        "pass_count": len(pass_items),
        "pass_precision": pass_correct / len(pass_items) if pass_items else None,
        "automation_rate": len(pass_items) / len(predicted) if predicted else None,
        "pass_recall": pass_correct / len(gold) if gold else None,
        "evidence_chain_coverage": complete_chain / len(predicted) if predicted else None,
        "measurement_coverage": complete_measurements / len(predicted) if predicted else None,
        "price_coverage": priced / len(predicted) if predicted else None,
        "status_counts": status_counts,
    }
