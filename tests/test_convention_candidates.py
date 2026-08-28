from __future__ import annotations

import json
from copy import deepcopy

import pytest
from cad_quote import main
from cadquote.convention_candidates import (
    build_convention_candidates,
    load_convention_profile,
)
from pydantic import ValidationError


def _profile(
    rules: list[dict[str, object]],
    *,
    lifecycle_status: str = "REVIEW",
    approved: bool = False,
) -> dict[str, object]:
    payload: dict[str, object] = {
        "schema_version": "estimator-convention-profile/v1",
        "profile_id": "synthetic-estimating-policy",
        "profile_version": "1.0.0",
        "lifecycle_status": lifecycle_status,
        "normalization": {
            "component_families": [{"id": "SYNTHETIC_TRIM", "name_terms_any": ["demo trim"]}],
            "material_families": [
                {"id": "SYNTHETIC_METAL", "material_terms_all": ["sample metal"]}
            ],
            "pricing_bases": [
                {
                    "id": "AREA",
                    "unit_aliases": ["m2", "㎡"],
                    "method_terms_any": ["area basis"],
                },
                {
                    "id": "LINEAR",
                    "unit_aliases": ["m"],
                    "method_terms_any": ["linear basis"],
                },
                {
                    "id": "SET",
                    "unit_aliases": ["set", "套"],
                    "method_terms_any": ["set basis"],
                },
            ],
        },
        "rules": rules,
    }
    if approved:
        payload["approval"] = {
            "reviewer": "reviewer-a",
            "reviewed_at": "2026-08-28T11:30:00+08:00",
            "reason": "Synthetic policy independently approved for this test.",
        }
    return payload


def _takeoff(
    *,
    unit: str = "㎡",
    pricing_method: str = "area basis",
    measurements: list[dict[str, object]] | None = None,
    unfolded_spec: str | None = None,
    existing_engineering_quantity: float | None = None,
) -> dict[str, object]:
    return {
        "components": [
            {
                "id": "component:synthetic",
                "mt_code": "MT-DEMO",
                "name": "demo trim",
                "room": "sample room",
                "status": "PASS",
            }
        ],
        "items": [
            {
                "sequence": 1,
                "name": "demo trim",
                "mt_code": "MT-DEMO",
                "material": "sample metal",
                "plan_location": "sample room",
                "component_id": "component:synthetic",
                "unit": unit,
                "pricing_method": pricing_method,
                "unfolded_spec": unfolded_spec,
                "engineering_quantity": existing_engineering_quantity,
                "status": "REVIEW",
            }
        ],
        "measurements": measurements or [],
    }


def _measurement(
    candidate_id: str,
    role: str,
    value: float,
    *,
    status: str = "PASS",
    expression: str | None = None,
    entity_id: str | None = None,
) -> dict[str, object]:
    return {
        "id": candidate_id,
        "component_id": "component:synthetic",
        "role": role,
        "raw_value": str(value),
        "numeric_value": value,
        "unit": "mm" if role != "quantity" else None,
        "source_file_id": "file:synthetic",
        "sheet_id": "sheet:synthetic",
        "entity_ids": [entity_id or f"entity:{candidate_id}"],
        "derived_expression": expression,
        "status": status,
        "confidence": 1.0,
    }


def test_profile_version_and_approval_audit_are_validated() -> None:
    rule = {
        "id": "SYNTHETIC-001",
        "category": "formula",
        "title": "Synthetic rule",
        "status": "REVIEW",
        "match": {"component_family": "SYNTHETIC_TRIM"},
        "action": {"quantity_role": "top_level"},
    }
    wrong_version = _profile([rule])
    wrong_version["schema_version"] = "estimator-convention-profile/v2"
    with pytest.raises(ValidationError):
        load_convention_profile(wrong_version)

    missing_audit = _profile([rule], lifecycle_status="APPROVED")
    with pytest.raises(ValidationError, match="requires approval"):
        load_convention_profile(missing_audit)

    naive_audit = _profile([rule], lifecycle_status="APPROVED", approved=True)
    naive_audit["approval"]["reviewed_at"] = "2026-08-28T11:30:00"  # type: ignore[index]
    with pytest.raises(ValidationError, match="timezone"):
        load_convention_profile(naive_audit)


def test_profile_rejects_commercial_write_actions_and_duplicate_rule_ids() -> None:
    rule = {
        "id": "SYNTHETIC-001",
        "category": "formula",
        "title": "Synthetic rule",
        "status": "REVIEW",
        "match": {"component_family": "SYNTHETIC_TRIM"},
        "action": {"amount": 99},
    }
    with pytest.raises(ValidationError, match="commercial fields"):
        load_convention_profile(_profile([rule]))

    safe_rule = {**rule, "action": {"quantity_role": "top_level"}}
    with pytest.raises(ValidationError, match="duplicate"):
        load_convention_profile(_profile([safe_rule, safe_rule]))


def test_quantity_one_prior_is_review_only_and_never_becomes_a_default() -> None:
    profile = _profile(
        [
            {
                "id": "QTY-PRIOR-SYNTHETIC",
                "category": "default_quantity",
                "title": "Synthetic prior",
                "status": "REVIEW",
                "enabled_for_auto_apply": False,
                "match": {"pricing_basis_any": ["AREA", "LINEAR", "SET"]},
                "action": {
                    "kind": "rank_candidate",
                    "candidate_quantity": 1,
                    "write_field": False,
                },
            }
        ]
    )

    result = build_convention_candidates(_takeoff(), profile)

    assert result["summary"]["candidate_count"] == 1
    candidate = result["candidates"][0]
    assert candidate["state"] == "REVIEW"
    assert candidate["candidate_fields"] == {}
    assert candidate["profile_suggestions"]["candidate_quantity"] == 1
    assert result["policy"]["quantity_default_allowed"] is False


def test_embedded_multiplier_allows_outer_quantity_one_but_does_not_mutate_takeoff() -> None:
    profile = _profile(
        [
            {
                "id": "QTY-AGG-SYNTHETIC",
                "category": "default_quantity",
                "title": "Synthetic aggregation rule",
                "status": "REVIEW",
                "match": {
                    "measurement_role": "verified_aggregated_path_expression",
                    "expression_contains_any": ["*"],
                },
                "action": {
                    "outer_quantity": 1,
                    "preserve_internal_multipliers": True,
                    "forbid_double_multiplication": True,
                },
            }
        ]
    )
    payload = _takeoff(
        measurements=[
            _measurement(
                "measurement:path",
                "length",
                2_400,
                expression="600 * 4",
            )
        ]
    )
    before = deepcopy(payload)

    result = build_convention_candidates(payload, profile)

    candidate = result["candidates"][0]
    assert candidate["state"] == "REVIEW"
    assert candidate["candidate_fields"]["quantity"] == 1.0
    assert candidate["calculation_basis"]["multiplicity_policy"] == (
        "expression_contains_internal_multiplier"
    )
    assert candidate["mutates_takeoff"] is False
    assert payload == before


def test_outer_quantity_one_is_not_suggested_for_addition_only_expression() -> None:
    profile = _profile(
        [
            {
                "id": "QTY-AGG-SYNTHETIC",
                "category": "default_quantity",
                "title": "Synthetic aggregation rule",
                "status": "REVIEW",
                "match": {
                    "measurement_role": "multi_segment_path",
                    "expression_contains_any": ["+"],
                },
                "action": {"outer_quantity": 1},
            }
        ]
    )
    payload = _takeoff(
        measurements=[_measurement("measurement:path", "length", 1_200, expression="500 + 700")]
    )

    candidate = build_convention_candidates(payload, profile)["candidates"][0]

    assert "quantity" not in candidate["candidate_fields"]
    assert any(
        "no explicit internal multiplier" in reason for reason in candidate["review_reasons"]
    )


def test_approved_area_formula_can_confirm_only_with_auditable_nonconflicting_inputs() -> None:
    profile = _profile(
        [
            {
                "id": "AREA-SYNTHETIC",
                "category": "engineering_quantity_formula",
                "title": "Synthetic area formula",
                "status": "APPROVED",
                "enabled_for_auto_apply": True,
                "match": {
                    "component_family": "SYNTHETIC_TRIM",
                    "material_family": "SYNTHETIC_METAL",
                    "pricing_basis": "AREA",
                    "unit": "m2",
                },
                "action": {
                    "formula": "width_mm * length_mm * physical_quantity / 1000000",
                    "result_unit": "m2",
                },
            }
        ],
        lifecycle_status="APPROVED",
        approved=True,
    )
    payload = _takeoff(
        measurements=[
            _measurement("measurement:width", "width", 70),
            _measurement("measurement:length", "length", 1_000),
            _measurement("measurement:quantity", "quantity", 2),
        ]
    )

    result = build_convention_candidates(payload, profile)

    candidate = result["candidates"][0]
    assert candidate["state"] == "CONFIRMED"
    assert candidate["candidate_fields"]["engineering_quantity"] == pytest.approx(0.14)
    assert set(candidate["measurement_candidate_ids"]) == {
        "measurement:width",
        "measurement:length",
        "measurement:quantity",
    }
    assert len(candidate["entity_ids"]) == 3
    assert candidate["commercial_effect"] == "NONE"


def test_formula_stays_review_when_an_input_is_not_auditable_or_existing_value_conflicts() -> None:
    rule = {
        "id": "AREA-SYNTHETIC",
        "category": "engineering_quantity_formula",
        "title": "Synthetic area formula",
        "status": "APPROVED",
        "enabled_for_auto_apply": True,
        "match": {"pricing_basis": "AREA"},
        "action": {
            "formula": "width_mm * length_mm * physical_quantity / 1000000",
            "result_unit": "m2",
        },
    }
    profile = _profile([rule], lifecycle_status="APPROVED", approved=True)
    payload = _takeoff(
        existing_engineering_quantity=9.9,
        measurements=[
            _measurement("measurement:width", "width", 70, status="REVIEW"),
            _measurement("measurement:length", "length", 1_000),
            _measurement("measurement:quantity", "quantity", 2),
        ],
    )

    candidate = build_convention_candidates(payload, profile)["candidates"][0]

    assert candidate["state"] == "REVIEW"
    assert any("not auditable" in reason for reason in candidate["review_reasons"])
    assert any("conflicts with existing" in reason for reason in candidate["review_reasons"])


def test_set_formula_uses_proved_physical_count_not_quantity_one() -> None:
    profile = _profile(
        [
            {
                "id": "SET-SYNTHETIC",
                "category": "engineering_quantity_formula",
                "title": "Synthetic set formula",
                "status": "APPROVED",
                "enabled_for_auto_apply": True,
                "match": {"pricing_basis": "SET", "unit": "set"},
                "action": {"formula": "physical_quantity", "result_unit": "set"},
            }
        ],
        lifecycle_status="APPROVED",
        approved=True,
    )
    payload = _takeoff(
        unit="套",
        pricing_method="set basis",
        measurements=[_measurement("measurement:quantity", "quantity", 3)],
    )

    candidate = build_convention_candidates(payload, profile)["candidates"][0]

    assert candidate["state"] == "CONFIRMED"
    assert candidate["candidate_fields"]["engineering_quantity"] == 3.0
    assert "quantity" not in candidate["candidate_fields"]


def test_conflicting_measurement_facts_block_confirmation() -> None:
    profile = _profile(
        [
            {
                "id": "LINEAR-SYNTHETIC",
                "category": "engineering_quantity_formula",
                "title": "Synthetic linear formula",
                "status": "APPROVED",
                "enabled_for_auto_apply": True,
                "match": {"pricing_basis": "LINEAR"},
                "action": {
                    "formula": "governing_path_total_mm * aggregation_quantity / 1000",
                    "result_unit": "m",
                },
            }
        ],
        lifecycle_status="APPROVED",
        approved=True,
    )
    payload = _takeoff(
        unit="m",
        pricing_method="linear basis",
        measurements=[
            _measurement("measurement:length-a", "length", 1_000),
            _measurement("measurement:length-b", "length", 1_200),
            _measurement("measurement:quantity", "quantity", 2),
        ],
    )

    candidate = build_convention_candidates(payload, profile)["candidates"][0]

    assert candidate["state"] == "REVIEW"
    assert "engineering_quantity" not in candidate["candidate_fields"]
    assert any("conflicting" in reason for reason in candidate["review_reasons"])


def test_convention_candidates_cli_writes_versioned_nonmutating_output(tmp_path) -> None:
    profile = _profile(
        [
            {
                "id": "QTY-PRIOR-SYNTHETIC",
                "category": "default_quantity",
                "title": "Synthetic prior",
                "status": "REVIEW",
                "match": {"pricing_basis": "AREA"},
                "action": {"candidate_quantity": 1, "write_field": False},
            }
        ]
    )
    takeoff_path = tmp_path / "takeoff.json"
    profile_path = tmp_path / "profile.json"
    output_path = tmp_path / "candidates.json"
    takeoff_path.write_text(json.dumps(_takeoff()), encoding="utf-8")
    profile_path.write_text(json.dumps(profile), encoding="utf-8")

    exit_code = main(
        [
            "convention-candidates",
            str(takeoff_path),
            str(profile_path),
            "--out",
            str(output_path),
        ]
    )

    assert exit_code == 0
    result = json.loads(output_path.read_text(encoding="utf-8"))
    assert result["schema_version"] == "estimator-convention-candidates/v1"
    assert result["summary"]["candidate_count"] == 1
    assert result["policy"]["mutates_takeoff"] is False
