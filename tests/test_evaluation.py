from __future__ import annotations

import json
from pathlib import Path

import pytest
from cad_quote import main
from cadquote.evaluation import evaluate_takeoff, evaluation_policy_hash
from cadquote.models import EvaluationPolicy, ReviewStatus, TakeoffItem


def item(sequence: int = 1, **updates: object) -> TakeoffItem:
    values: dict[str, object] = {
        "sequence": sequence,
        "name": f"门套-{sequence}",
        "mt_code": "MT-01",
        "material": "304不锈钢",
        "plan_location": f"1F/客厅-{sequence}",
        "elevation": f"EL-{sequence:02d}",
        "detail": f"DT-{sequence:02d}",
        "unfolded_spec": "10+20+30",
        "width_mm": 60,
        "length_mm": 1000,
        "quantity": 10,
        "engineering_quantity": 0.6,
        "unit": "㎡",
        "pricing_method": "按展开面积",
        "unit_price": 100,
        "amount": 60,
        "component_id": f"component-{sequence}",
        "evidence_ids": [f"entity-{sequence}"],
        "status": ReviewStatus.PASS,
    }
    values.update(updates)
    return TakeoffItem.model_validate(values)


def confirmed_policy(**updates: object) -> EvaluationPolicy:
    values = EvaluationPolicy().model_dump(mode="json")
    values["policy_version"] = "test-confirmed-v1"
    values["length_mm"]["relative_tolerance"] = 0.05
    values["quantity"]["relative_tolerance"] = 0.05
    for field, value in updates.items():
        values[field] = value
    return EvaluationPolicy.model_validate(values)


def row(report: dict[str, object], index: int = 0) -> dict[str, object]:
    return report["row_results"][index]  # type: ignore[index,return-value]


def test_safe_default_is_indeterminate_until_length_and_quantity_are_confirmed():
    report = evaluate_takeoff([item()], [item()])

    assert report["overall_gate"] == "INDETERMINATE"
    assert report["meets_target"] is None
    assert report["correct_rows"] == 0
    assert report["policy_pending_fields"] == [
        "length_mm.relative_tolerance",
        "quantity.relative_tolerance",
    ]
    assert row(report)["field_results"]["engineering_quantity"]["status"] == "PASS"  # type: ignore[index]
    assert row(report)["field_results"]["length_mm"]["status"] == "UNRESOLVED"  # type: ignore[index]


def test_all_enabled_fields_correct_passes_project_gate():
    policy = confirmed_policy()
    report = evaluate_takeoff([item()], [item()], policy=policy, project_id="synthetic-a")

    assert report["overall_gate"] == "PASS"
    assert report["eligible_gold_rows"] == 1
    assert report["correct_rows"] == 1
    assert report["replication_recall"] == 1
    assert report["output_precision"] == 1
    assert report["projects"][0]["project_id"] == "synthetic-a"  # type: ignore[index]
    assert report["policy_version"] == "test-confirmed-v1"
    assert report["policy_hash"] == evaluation_policy_hash(policy)
    assert len(report["policy_hash"]) == 64  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("field", "wrong_value"),
    [
        ("mt_code", "MT-99"),
        ("name", "错误构件"),
        ("plan_location", "2F/错误房间"),
        ("elevation", "EL-99"),
        ("detail", "DT-99"),
        ("unfolded_spec", "10+20+31"),
        ("length_mm", 1051),
        ("quantity", 10.51),
        ("engineering_quantity", 0.631),
    ],
)
def test_any_required_field_failure_makes_the_row_incorrect(field: str, wrong_value: object):
    predicted = item(**{field: wrong_value})
    report = evaluate_takeoff([predicted], [item()], policy=confirmed_policy())

    assert report["correct_rows"] == 0
    assert report["overall_gate"] == "FAIL"
    assert row(report)["field_results"][field]["status"] == "FAIL"  # type: ignore[index]


def test_relative_tolerance_boundary_is_inclusive_and_10_vs_996_passes():
    gold = item(length_mm=1000, quantity=10, engineering_quantity=10)
    boundary = item(length_mm=1050, quantity=10.5, engineering_quantity=10.5)
    report = evaluate_takeoff([boundary], [gold], policy=confirmed_policy())
    assert report["overall_gate"] == "PASS"
    for field in ("length_mm", "quantity", "engineering_quantity"):
        assert row(report)["field_results"][field]["status"] == "PASS"  # type: ignore[index]

    example = item(engineering_quantity=9.96)
    example_gold = item(engineering_quantity=10.00)
    example_report = evaluate_takeoff([example], [example_gold], policy=confirmed_policy())
    engineering = row(example_report)["field_results"]["engineering_quantity"]  # type: ignore[index]
    assert engineering["status"] == "PASS"
    assert float(engineering["relative_error"]) == pytest.approx(0.004)


def test_relative_tolerance_just_over_boundary_fails():
    predicted = item(length_mm=1050.01, quantity=10.5001, engineering_quantity=10.5001)
    gold = item(length_mm=1000, quantity=10, engineering_quantity=10)
    report = evaluate_takeoff([predicted], [gold], policy=confirmed_policy())

    assert report["overall_gate"] == "FAIL"
    for field in ("length_mm", "quantity", "engineering_quantity"):
        assert row(report)["field_results"][field]["status"] == "FAIL"  # type: ignore[index]


def test_gold_zero_uses_explicit_exact_rule():
    zero = item(length_mm=0, quantity=0, engineering_quantity=0)
    passed = evaluate_takeoff([zero], [zero], policy=confirmed_policy())
    assert passed["overall_gate"] == "PASS"

    nonzero = item(length_mm=0.001, quantity=0.001, engineering_quantity=0.001)
    failed = evaluate_takeoff([nonzero], [zero], policy=confirmed_policy())
    assert failed["overall_gate"] == "FAIL"
    engineering = row(failed)["field_results"]["engineering_quantity"]  # type: ignore[index]
    assert engineering["reason"] == "outside_zero_rule"
    assert engineering["zero_handling"] == "exact"


def test_unfolded_expression_mode_can_be_strict_or_evaluated_total():
    predicted = item(unfolded_spec="60")
    gold = item(unfolded_spec="10+20+30")

    strict = evaluate_takeoff([predicted], [gold], policy=confirmed_policy())
    assert row(strict)["field_results"]["unfolded_spec"]["status"] == "FAIL"  # type: ignore[index]

    values = confirmed_policy().model_dump(mode="json")
    values["unfolded_spec"]["mode"] = "evaluated-total"
    evaluated = evaluate_takeoff(
        [predicted],
        [gold],
        policy=EvaluationPolicy.model_validate(values),
    )
    assert evaluated["overall_gate"] == "PASS"
    assert row(evaluated)["field_results"]["unfolded_spec"]["status"] == "PASS"  # type: ignore[index]


def test_missing_and_unexpected_rows_reduce_recall_and_precision():
    gold = [item(1), item(2)]
    missing_report = evaluate_takeoff([item(1)], gold, policy=confirmed_policy())
    assert missing_report["missing_count"] == 1
    assert missing_report["replication_recall"] == 0.5
    assert missing_report["output_precision"] == 1
    assert missing_report["overall_gate"] == "FAIL"

    extra = item(2, component_id="extra-2", plan_location="1F/额外", name="额外构件")
    extra_report = evaluate_takeoff([item(1), extra], [item(1)], policy=confirmed_policy())
    assert extra_report["unexpected_count"] == 1
    assert extra_report["replication_recall"] == 1
    assert extra_report["output_precision"] == 0.5
    assert extra_report["overall_gate"] == "FAIL"


def test_duplicate_prediction_is_reported_and_cannot_inflate_precision():
    duplicate = item(
        2,
        component_id="component-1",
        name="门套-1",
        plan_location="1F/客厅-1",
        elevation="EL-01",
        detail="DT-01",
    )
    report = evaluate_takeoff(
        [item(1), duplicate],
        [item(1)],
        policy=confirmed_policy(),
    )

    assert report["duplicate_predicted_count"] >= 1
    assert report["matched_count"] == 0
    assert report["missing_count"] == 1
    assert report["unexpected_count"] == 2
    assert report["output_precision"] == 0
    assert report["overall_gate"] == "FAIL"


def test_duplicate_gold_stable_identity_blocks_the_gate():
    report = evaluate_takeoff(
        [item(1), item(2, component_id="component-1")],
        [item(1), item(2, component_id="component-1")],
        policy=confirmed_policy(),
    )

    assert report["duplicate_gold_count"] >= 1
    assert report["overall_gate"] == "BLOCKED"
    assert report["meets_target"] is None


def test_amount_is_ignored_by_default_and_exact_when_explicitly_enabled():
    different_amount = item(amount=60.01)
    gold = item(amount=60)
    disabled = evaluate_takeoff([different_amount], [gold], policy=confirmed_policy())
    assert disabled["overall_gate"] == "PASS"
    assert "amount" not in row(disabled)["field_results"]  # type: ignore[operator]

    values = confirmed_policy().model_dump(mode="json")
    values["amount"]["enabled"] = True
    amount_policy = EvaluationPolicy.model_validate(values)
    enabled = evaluate_takeoff([different_amount], [gold], policy=amount_policy)
    assert enabled["overall_gate"] == "FAIL"
    assert row(enabled)["field_results"]["amount"]["status"] == "FAIL"  # type: ignore[index]

    missing = evaluate_takeoff([item(amount=None)], [gold], policy=amount_policy)
    assert missing["overall_gate"] == "INDETERMINATE"
    assert row(missing)["field_results"]["amount"]["status"] == "UNRESOLVED"  # type: ignore[index]


def test_missing_required_gold_field_blocks_the_gate():
    report = evaluate_takeoff(
        [item(detail=None)],
        [item(detail=None)],
        policy=confirmed_policy(),
    )

    assert report["eligible_gold_rows"] == 0
    assert report["overall_gate"] == "BLOCKED"
    assert report["invalid_gold_rows"][0]["missing_required_fields"] == ["detail"]  # type: ignore[index]


def test_numeric_nearness_is_never_used_to_swap_ambiguous_duplicate_rows():
    gold = [
        item(1, component_id=None, engineering_quantity=1),
        item(
            2,
            component_id=None,
            name="门套-1",
            plan_location="1F/客厅-1",
            elevation="EL-01",
            detail="DT-01",
            engineering_quantity=100,
        ),
    ]
    predicted = [
        item(1, component_id=None, engineering_quantity=100),
        item(
            2,
            component_id=None,
            name="门套-1",
            plan_location="1F/客厅-1",
            elevation="EL-01",
            detail="DT-01",
            engineering_quantity=1,
        ),
    ]
    report = evaluate_takeoff(predicted, gold, policy=confirmed_policy())

    assert report["matched_count"] == 0
    assert report["missing_count"] == 2
    assert report["unexpected_count"] == 2
    assert report["correct_rows"] == 0
    assert report["overall_gate"] == "FAIL"
    assert all(
        value["field_results"]["engineering_quantity"]["reason"]
        == "predicted_row_missing"
        for value in report["row_results"]
    )


def test_cli_uses_primary_source_material_code_for_non_mt_gold(tmp_path: Path):
    policy_values = confirmed_policy().model_dump(mode="json")
    policy_values["unfolded_spec"]["mode"] = "evaluated-total"
    policy = EvaluationPolicy.model_validate(policy_values)
    predicted_path = tmp_path / "predicted.json"
    gold_path = tmp_path / "gold.json"
    policy_path = tmp_path / "policy.json"
    output_path = tmp_path / "report.json"
    predicted_path.write_text(
        json.dumps(
            [
                {
                    "gold_id": "gold-row-1",
                    **item(
                        mt_code="GC-SS-201",
                        component_id=None,
                        unfolded_spec="60",
                        length_mm=1050,
                        quantity=10.5,
                        engineering_quantity=0.63,
                        unit="m",
                        unit_price=999,
                        amount=999,
                    ).model_dump(mode="json"),
                }
            ]
        ),
        encoding="utf-8",
    )
    gold_item = item(mt_code="", component_id=None).model_dump(mode="json")
    gold_path.write_text(
        json.dumps(
            {
                "rows": [
                    {
                        "id": "gold-row-1",
                        "source_material_code": "GC-SS-201\nGC-GL-104",
                        "item": gold_item,
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    policy_path.write_text(policy.model_dump_json(indent=2), encoding="utf-8")

    assert (
        main(
            [
                "evaluate",
                str(predicted_path),
                str(gold_path),
                "--policy",
                str(policy_path),
                "--out",
                str(output_path),
            ]
        )
        == 0
    )
    report = json.loads(output_path.read_text(encoding="utf-8"))

    assert report["overall_gate"] == "PASS"
    material_code = report["row_results"][0]["field_results"]["mt_code"]
    assert material_code["status"] == "PASS"
    assert material_code["gold"] == "GC-SS-201"
    fields = report["row_results"][0]["field_results"]
    assert fields["unfolded_spec"]["status"] == "PASS"
    for field in ("length_mm", "quantity", "engineering_quantity"):
        assert fields[field]["status"] == "PASS"
    assert "unit" not in fields
    assert "unit_price" not in fields


def test_ten_of_eleven_rows_does_not_meet_a_95_percent_gate():
    gold = [item(sequence) for sequence in range(1, 12)]
    predicted = [item(sequence) for sequence in range(1, 11)]
    report = evaluate_takeoff(predicted, gold, policy=confirmed_policy())

    assert report["correct_rows"] == 10
    assert report["eligible_gold_rows"] == 11
    assert report["replication_recall"] == pytest.approx(10 / 11)
    assert report["replication_recall"] < 0.95  # type: ignore[operator]
    assert report["overall_gate"] == "FAIL"


def test_cli_policy_records_version_hash_ids_and_project(tmp_path: Path):
    policy = confirmed_policy()
    predicted_path = tmp_path / "predicted.json"
    gold_path = tmp_path / "gold.json"
    policy_path = tmp_path / "policy.json"
    output_path = tmp_path / "report.json"
    predicted_path.write_text(
        json.dumps(
            [
                {
                    "gold_id": "gold-row-1",
                    **item(component_id=None).model_dump(mode="json"),
                }
            ]
        ),
        encoding="utf-8",
    )
    gold_path.write_text(
        json.dumps(
            {
                "project_id": "project-from-gold",
                "rows": [
                    {
                        "id": "gold-row-1",
                        "item": item(component_id=None).model_dump(mode="json"),
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    policy_path.write_text(policy.model_dump_json(indent=2), encoding="utf-8")

    exit_code = main(
        [
            "evaluate",
            str(predicted_path),
            str(gold_path),
            "--policy",
            str(policy_path),
            "--out",
            str(output_path),
        ]
    )
    report = json.loads(output_path.read_text(encoding="utf-8"))

    assert exit_code == 0
    assert report["project_id"] == "project-from-gold"
    assert report["policy_version"] == policy.policy_version
    assert report["policy_hash"] == evaluation_policy_hash(policy)
    assert report["row_results"][0]["match_method"] == "stable_row_id"
    assert report["overall_gate"] == "PASS"
