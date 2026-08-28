"""Generate auditable estimator-convention candidates without changing takeoff rows.

The convention profile is an external, versioned policy input.  This module does
not contain customer profiles or estimator-specific values.  A matched rule is a
suggestion by default.  Even a confirmed convention candidate is only an input to
a later reviewer-controlled takeoff step; it cannot release a quotation or amount.
"""

from __future__ import annotations

import ast
import hashlib
import math
import re
import unicodedata
from collections import defaultdict
from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .models import ComponentInstance, MeasurementCandidate, TakeoffItem

PROFILE_SCHEMA_VERSION = "estimator-convention-profile/v1"
OUTPUT_SCHEMA_VERSION = "estimator-convention-candidates/v1"

_ALLOWED_LIFECYCLE_STATES = {"DRAFT", "REVIEW", "APPROVABLE", "APPROVED", "RETIRED"}
_FORBIDDEN_COMMERCIAL_KEYS = {
    "amount",
    "price",
    "unit_price",
    "price_entry_id",
    "status",
    "item_status",
    "takeoff_status",
    "commercial_status",
    "quotation_status",
}
_CANDIDATE_FIELD_KEYS = {
    "quantity",
    "engineering_quantity",
    "unfolded_spec",
    "width_mm",
    "length_mm",
}
_MULTIPLICITY_RE = re.compile(r"(?:\d|\))\s*(?:\*|×|x|X)\s*(?:\d|\()")
_SAFE_EXPRESSION_RE = re.compile(r"^[\d\s.+\-*/()×xX]+$")


class _ProfileModel(BaseModel):
    """Profile models validate the public contract and retain descriptive metadata."""

    model_config = ConfigDict(extra="allow")


class ConventionApproval(_ProfileModel):
    reviewer: str = Field(min_length=1)
    reviewed_at: datetime
    reason: str = Field(min_length=1)

    @field_validator("reviewer", "reason")
    @classmethod
    def _strip_required_text(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("must not be blank")
        return stripped

    @field_validator("reviewed_at")
    @classmethod
    def _require_timezone(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("must include a timezone offset")
        return value


class ConventionRule(_ProfileModel):
    id: str = Field(min_length=1)
    category: str = Field(min_length=1)
    title: str = Field(min_length=1)
    status: Literal["DRAFT", "REVIEW", "APPROVABLE", "APPROVED", "RETIRED"] = "REVIEW"
    enabled_for_auto_apply: bool = False
    match: dict[str, Any]
    action: dict[str, Any]
    conditions: list[str] = Field(default_factory=list)
    guardrails: list[str] = Field(default_factory=list)
    required_measurement_roles: list[
        Literal["length", "height", "width", "unfolded_spec", "quantity"]
    ] = Field(default_factory=list)

    @field_validator("id", "category", "title")
    @classmethod
    def _strip_rule_text(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("must not be blank")
        return stripped

    @model_validator(mode="after")
    def _validate_action_scope(self) -> ConventionRule:
        if not self.match:
            raise ValueError("match must not be empty")
        if not self.action:
            raise ValueError("action must not be empty")
        forbidden = _find_forbidden_keys(self.action)
        if forbidden:
            raise ValueError(
                "convention actions cannot write commercial fields: " + ", ".join(forbidden)
            )
        return self


class ConventionProfile(_ProfileModel):
    schema_version: Literal["estimator-convention-profile/v1"]
    profile_id: str = Field(min_length=1)
    profile_version: str = Field(min_length=1)
    lifecycle_status: Literal["DRAFT", "REVIEW", "APPROVABLE", "APPROVED", "RETIRED"]
    approval: ConventionApproval | None = None
    normalization: dict[str, Any] = Field(default_factory=dict)
    rules: list[ConventionRule] = Field(min_length=1)

    @field_validator("profile_id", "profile_version")
    @classmethod
    def _strip_profile_text(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("must not be blank")
        return stripped

    @model_validator(mode="after")
    def _validate_release_state(self) -> ConventionProfile:
        rule_ids = [rule.id for rule in self.rules]
        duplicate_ids = sorted({value for value in rule_ids if rule_ids.count(value) > 1})
        if duplicate_ids:
            raise ValueError("duplicate convention rule ids: " + ", ".join(duplicate_ids))
        if self.lifecycle_status == "APPROVED" and self.approval is None:
            raise ValueError("an APPROVED profile requires approval reviewer/reviewed_at/reason")
        return self


class ConventionInputBinding(BaseModel):
    model_config = ConfigDict(extra="forbid")

    symbol: str
    value: float | str | None = None
    source: Literal["measurement", "takeoff_item", "derived_context"]
    measurement_candidate_ids: list[str] = Field(default_factory=list)
    entity_ids: list[str] = Field(default_factory=list)
    source_sheet_ids: list[str] = Field(default_factory=list)
    auditable: bool = False
    conflict: bool = False


class ConventionCandidate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    profile_id: str
    profile_version: str
    rule_id: str
    category: str
    component_id: str | None = None
    sequence: int | None = None
    state: Literal["REVIEW", "CONFIRMED"]
    matched_context: dict[str, Any] = Field(default_factory=dict)
    candidate_fields: dict[str, float | str] = Field(default_factory=dict)
    calculation_basis: dict[str, Any] = Field(default_factory=dict)
    profile_suggestions: dict[str, Any] = Field(default_factory=dict)
    inputs: list[ConventionInputBinding] = Field(default_factory=list)
    measurement_candidate_ids: list[str] = Field(default_factory=list)
    entity_ids: list[str] = Field(default_factory=list)
    source_sheet_ids: list[str] = Field(default_factory=list)
    confirmation_basis: list[str] = Field(default_factory=list)
    review_reasons: list[str] = Field(default_factory=list)
    commercial_effect: Literal["NONE"] = "NONE"
    mutates_takeoff: Literal[False] = False


def _find_forbidden_keys(value: Any, prefix: str = "") -> list[str]:
    found: list[str] = []
    if isinstance(value, Mapping):
        for raw_key, child in value.items():
            key = str(raw_key)
            path = f"{prefix}.{key}" if prefix else key
            if key in _FORBIDDEN_COMMERCIAL_KEYS:
                found.append(path)
            found.extend(_find_forbidden_keys(child, path))
    elif isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        for index, child in enumerate(value):
            found.extend(_find_forbidden_keys(child, f"{prefix}[{index}]"))
    return sorted(found)


def load_convention_profile(payload: Mapping[str, Any]) -> ConventionProfile:
    """Validate a versioned convention profile.

    Passing a mapping rather than a path keeps file I/O at the CLI boundary and
    makes the deterministic engine easy to embed and test.
    """

    return ConventionProfile.model_validate(payload)


def _text(value: Any) -> str:
    normalized = unicodedata.normalize("NFKC", "" if value is None else str(value))
    return " ".join(normalized.casefold().split())


def _normalized_unit(value: Any) -> str | None:
    normalized = _text(value).replace(" ", "")
    aliases = {
        "㎡": "m2",
        "m²": "m2",
        "m2": "m2",
        "平方": "m2",
        "m": "m",
        "米": "m",
        "套": "set",
        "set": "set",
        "件": "piece",
        "piece": "piece",
    }
    return aliases.get(normalized) if normalized else None


def _terms_match(text_value: str, definition: Mapping[str, Any], prefix: str) -> bool:
    any_terms = [_text(value) for value in definition.get(f"{prefix}_terms_any", [])]
    all_terms = [_text(value) for value in definition.get(f"{prefix}_terms_all", [])]
    none_terms = [_text(value) for value in definition.get(f"{prefix}_terms_none", [])]
    if any_terms and not any(term and term in text_value for term in any_terms):
        return False
    if all_terms and not all(term and term in text_value for term in all_terms):
        return False
    return not any(term and term in text_value for term in none_terms)


def _family_ids(
    text_value: str,
    definitions: Any,
    prefix: str,
) -> list[str]:
    if not isinstance(definitions, list):
        return []
    matches: list[str] = []
    for definition in definitions:
        if not isinstance(definition, Mapping) or not definition.get("id"):
            continue
        if _terms_match(text_value, definition, prefix):
            has_terms = any(
                definition.get(f"{prefix}_terms_{suffix}") for suffix in ("any", "all", "none")
            )
            if has_terms:
                matches.append(str(definition["id"]))
    return matches


def _pricing_basis(item: TakeoffItem | None, normalization: Mapping[str, Any]) -> list[str]:
    if item is None:
        return []
    unit = _normalized_unit(item.unit)
    method = _text(item.pricing_method)
    matches: list[str] = []
    definitions = normalization.get("pricing_bases", [])
    if not isinstance(definitions, list):
        return matches
    for definition in definitions:
        if not isinstance(definition, Mapping) or not definition.get("id"):
            continue
        unit_aliases = {_normalized_unit(value) for value in definition.get("unit_aliases", [])}
        method_terms = [_text(value) for value in definition.get("method_terms_any", [])]
        unit_match = bool(unit and unit in unit_aliases)
        method_match = bool(method_terms and any(value in method for value in method_terms))
        if unit_match or method_match:
            matches.append(str(definition["id"]))
    return matches


def _measurement_sheets(measurement: MeasurementCandidate) -> list[str]:
    values = list(measurement.source_sheet_ids)
    if measurement.sheet_id:
        values.append(measurement.sheet_id)
    return list(dict.fromkeys(value for value in values if value))


def _measurement_auditable(measurement: MeasurementCandidate) -> bool:
    return bool(
        measurement.status.value == "PASS"
        and measurement.source_file_id
        and _measurement_sheets(measurement)
        and measurement.entity_ids
    )


def _expression_values(
    measurements: Sequence[MeasurementCandidate], item: TakeoffItem | None
) -> list[str]:
    values: list[str] = []
    for measurement in measurements:
        for value in (
            measurement.derived_expression,
            measurement.value_expression,
            measurement.raw_value if measurement.role == "unfolded_spec" else None,
        ):
            if value:
                values.append(str(value))
    if item and item.unfolded_spec:
        values.append(item.unfolded_spec)
    return list(dict.fromkeys(values))


def _contains_explicit_multiplicity(expressions: Sequence[str]) -> bool:
    return any(_MULTIPLICITY_RE.search(_text(value)) for value in expressions)


def _safe_numeric_expression(value: str) -> bool:
    expression = value.replace("×", "*").replace("X", "*").replace("x", "*")
    if not expression.strip() or not _SAFE_EXPRESSION_RE.fullmatch(expression):
        return False
    try:
        parsed = ast.parse(expression, mode="eval")
    except SyntaxError:
        return False
    allowed_nodes = (
        ast.Expression,
        ast.BinOp,
        ast.UnaryOp,
        ast.Constant,
        ast.Add,
        ast.Sub,
        ast.Mult,
        ast.Div,
        ast.UAdd,
        ast.USub,
        ast.Load,
    )
    return all(isinstance(node, allowed_nodes) for node in ast.walk(parsed))


def _spec_shape(expressions: Sequence[str]) -> str | None:
    for expression in expressions:
        normalized = expression.replace("×", "*").replace("X", "*").replace("x", "*")
        terms = [part.strip() for part in normalized.split("*")]
        if len(terms) == 3 and all(re.fullmatch(r"\d+(?:\.\d+)?", part) for part in terms):
            return "a*b*c"
    return None


def _role_tokens(measurements: Sequence[MeasurementCandidate]) -> list[str]:
    tokens: list[str] = []
    for measurement in measurements:
        tokens.append(measurement.role)
        tokens.extend(str(value) for value in measurement.basis)
        expression = measurement.derived_expression or measurement.value_expression
        if expression and "+" in expression:
            tokens.append("multi_segment_path")
        if expression and _contains_explicit_multiplicity([expression]):
            tokens.append("aggregated_path_expression")
            if _measurement_auditable(measurement):
                tokens.append("verified_aggregated_path_expression")
    return list(dict.fromkeys(tokens))


def _context_for(
    component: ComponentInstance | None,
    item: TakeoffItem | None,
    measurements: Sequence[MeasurementCandidate],
    profile: ConventionProfile,
) -> dict[str, Any]:
    name = _text((component.name if component else None) or (item.name if item else None))
    room = _text((component.room if component else None) or (item.plan_location if item else None))
    material = _text(item.material if item else None)
    expressions = _expression_values(measurements, item)
    measurement_tokens = _role_tokens(measurements)
    normalization = profile.normalization
    pricing_bases = _pricing_basis(item, normalization)
    unit = _normalized_unit(item.unit if item else None)
    existing_fields = {
        field
        for field in (
            "unfolded_spec",
            "width_mm",
            "length_mm",
            "quantity",
            "engineering_quantity",
        )
        if item is not None and getattr(item, field) is not None
    }
    return {
        "name": name,
        "room": room,
        "material": material,
        "mt_code": _text(
            (component.mt_code if component else None) or (item.mt_code if item else None)
        ),
        "component_family": _family_ids(name, normalization.get("component_families"), "name"),
        "material_family": _family_ids(
            material, normalization.get("material_families"), "material"
        ),
        "pricing_basis": pricing_bases,
        "unit": unit,
        "measurement_role": measurement_tokens,
        "candidate_sources": measurement_tokens,
        "expression": expressions,
        "expression_contains_multiplicity": _contains_explicit_multiplicity(expressions),
        "unfolded_spec": (
            "safe_numeric_expression"
            if any(_safe_numeric_expression(value) for value in expressions)
            else None
        ),
        "spec_shape": _spec_shape(expressions),
        "existing_fields": sorted(existing_fields),
        "component_status": component.status.value if component else None,
    }


def _as_list(value: Any) -> list[Any]:
    if isinstance(value, list):
        return value
    if isinstance(value, tuple | set):
        return list(value)
    return [value]


def _context_values(context: Mapping[str, Any], key: str) -> list[str]:
    value = context.get(key)
    if isinstance(value, list | tuple | set):
        return [_text(item) for item in value]
    return [_text(value)] if value is not None else []


def _match_terms(context: Mapping[str, Any], field: str, expected: Any, mode: str) -> bool:
    haystack = " ".join(_context_values(context, field))
    needles = [_text(value) for value in _as_list(expected)]
    if mode == "any":
        return any(value and value in haystack for value in needles)
    if mode == "all":
        return all(value and value in haystack for value in needles)
    return not any(value and value in haystack for value in needles)


def _rule_matches(rule: ConventionRule, context: Mapping[str, Any]) -> tuple[bool, dict[str, Any]]:
    matched: dict[str, Any] = {}
    for key, expected in rule.match.items():
        field = key
        if key.endswith("_terms_any"):
            field = key.removesuffix("_terms_any")
            ok = _match_terms(context, field, expected, "any")
        elif key.endswith("_terms_all"):
            field = key.removesuffix("_terms_all")
            ok = _match_terms(context, field, expected, "all")
        elif key.endswith("_terms_none"):
            field = key.removesuffix("_terms_none")
            ok = _match_terms(context, field, expected, "none")
        elif key == "expression_contains_any":
            field = "expression"
            ok = _match_terms(context, "expression", expected, "any")
        elif key == "expression_contains_all":
            field = "expression"
            ok = _match_terms(context, "expression", expected, "all")
        elif key.endswith("_any"):
            field = key.removesuffix("_any")
            actual = set(_context_values(context, field))
            wanted = {_text(value) for value in _as_list(expected)}
            ok = bool(actual & wanted)
        elif key.endswith("_all"):
            field = key.removesuffix("_all")
            actual = set(_context_values(context, field))
            wanted = {_text(value) for value in _as_list(expected)}
            ok = wanted.issubset(actual)
        elif key.endswith("_none"):
            field = key.removesuffix("_none")
            actual = set(_context_values(context, field))
            wanted = {_text(value) for value in _as_list(expected)}
            ok = not bool(actual & wanted)
        else:
            actual = set(_context_values(context, key))
            wanted = {_text(value) for value in _as_list(expected)}
            ok = bool(actual & wanted)
        if not ok:
            return False, {}
        matched[key] = {"expected": expected, "actual": context.get(field)}
    return True, matched


def _distinct_numeric(values: Sequence[float]) -> list[float]:
    unique: list[float] = []
    for value in values:
        if not any(
            math.isclose(value, existing, rel_tol=1e-9, abs_tol=1e-6) for existing in unique
        ):
            unique.append(value)
    return unique


def _binding_for_role(
    symbol: str,
    role: str,
    measurements: Sequence[MeasurementCandidate],
    item: TakeoffItem | None,
) -> ConventionInputBinding:
    candidates = [
        value for value in measurements if value.role == role and value.numeric_value is not None
    ]
    numeric_values = _distinct_numeric([float(value.numeric_value) for value in candidates])
    conflict = len(numeric_values) > 1
    selected = candidates[0] if len(numeric_values) == 1 and candidates else None
    if selected is not None:
        same_value = [
            value
            for value in candidates
            if value.numeric_value is not None
            and math.isclose(
                float(value.numeric_value), numeric_values[0], rel_tol=1e-9, abs_tol=1e-6
            )
        ]
        return ConventionInputBinding(
            symbol=symbol,
            value=numeric_values[0],
            source="measurement",
            measurement_candidate_ids=[value.id for value in same_value],
            entity_ids=list(dict.fromkeys(eid for value in same_value for eid in value.entity_ids)),
            source_sheet_ids=list(
                dict.fromkeys(sid for value in same_value for sid in _measurement_sheets(value))
            ),
            auditable=all(_measurement_auditable(value) for value in same_value),
            conflict=conflict,
        )
    item_field = {
        "width": "width_mm",
        "length": "length_mm",
        "height": "length_mm",
        "quantity": "quantity",
    }.get(role)
    item_value = getattr(item, item_field, None) if item is not None and item_field else None
    return ConventionInputBinding(
        symbol=symbol,
        value=float(item_value) if isinstance(item_value, int | float) else None,
        source="takeoff_item" if item_value is not None else "derived_context",
        auditable=False,
        conflict=conflict,
    )


def _formula_bindings(
    formula: str,
    measurements: Sequence[MeasurementCandidate],
    item: TakeoffItem | None,
) -> list[ConventionInputBinding]:
    symbols = set(re.findall(r"\b[A-Za-z_][A-Za-z0-9_]*\b", formula))
    role_for_symbol = {
        "width_mm": "width",
        "role_labelled_width_mm": "width",
        "length_mm": "length",
        "role_labelled_length_or_height_mm": "length",
        "governing_path_total_mm": "length",
        "physical_quantity": "quantity",
        "aggregation_quantity": "quantity",
    }
    return [
        _binding_for_role(symbol, role_for_symbol[symbol], measurements, item)
        for symbol in sorted(symbols)
        if symbol in role_for_symbol
    ]


def _multiplicity_expression_binding(
    measurements: Sequence[MeasurementCandidate],
) -> ConventionInputBinding:
    candidates = [
        measurement
        for measurement in measurements
        if (measurement.derived_expression or measurement.value_expression)
        and _contains_explicit_multiplicity(
            [measurement.derived_expression or measurement.value_expression or ""]
        )
    ]
    expressions = list(
        dict.fromkeys(
            measurement.derived_expression or measurement.value_expression or ""
            for measurement in candidates
        )
    )
    return ConventionInputBinding(
        symbol="embedded_multiplicity_expression",
        value=expressions[0] if len(expressions) == 1 else None,
        source="measurement" if candidates else "derived_context",
        measurement_candidate_ids=[measurement.id for measurement in candidates],
        entity_ids=list(
            dict.fromkeys(
                entity_id for measurement in candidates for entity_id in measurement.entity_ids
            )
        ),
        source_sheet_ids=list(
            dict.fromkeys(
                sheet_id
                for measurement in candidates
                for sheet_id in _measurement_sheets(measurement)
            )
        ),
        auditable=bool(candidates)
        and all(_measurement_auditable(measurement) for measurement in candidates),
        conflict=len(expressions) > 1,
    )


def _evaluate_formula(formula: str, bindings: Sequence[ConventionInputBinding]) -> float | None:
    values = {binding.symbol: binding.value for binding in bindings}
    if any(value is None or not isinstance(value, int | float) for value in values.values()):
        return None
    try:
        parsed = ast.parse(formula, mode="eval")
    except SyntaxError:
        return None
    allowed = (
        ast.Expression,
        ast.BinOp,
        ast.UnaryOp,
        ast.Constant,
        ast.Name,
        ast.Add,
        ast.Sub,
        ast.Mult,
        ast.Div,
        ast.UAdd,
        ast.USub,
        ast.Load,
    )
    if not all(isinstance(node, allowed) for node in ast.walk(parsed)):
        return None
    try:
        result = eval(compile(parsed, "<convention-formula>", "eval"), {"__builtins__": {}}, values)
    except (ArithmeticError, NameError, TypeError):
        return None
    return float(result) if isinstance(result, int | float) and math.isfinite(result) else None


def _candidate_payload(
    rule: ConventionRule,
    context: Mapping[str, Any],
    measurements: Sequence[MeasurementCandidate],
    item: TakeoffItem | None,
) -> tuple[
    dict[str, float | str],
    dict[str, Any],
    dict[str, Any],
    list[ConventionInputBinding],
    list[str],
]:
    action = rule.action
    candidate_fields: dict[str, float | str] = {}
    calculation_basis: dict[str, Any] = {}
    suggestions = dict(action)
    bindings: list[ConventionInputBinding] = []
    review_reasons: list[str] = []

    if "candidate_unfolded_spec" in action and action["candidate_unfolded_spec"] is not None:
        candidate_fields["unfolded_spec"] = str(action["candidate_unfolded_spec"])
    if isinstance(action.get("candidate_width_mm"), int | float):
        candidate_fields["width_mm"] = float(action["candidate_width_mm"])
    if isinstance(action.get("candidate_quantity"), int | float):
        if action.get("write_field") is True:
            candidate_fields["quantity"] = float(action["candidate_quantity"])
        else:
            review_reasons.append("quantity prior is ranking-only; write_field is not true")

    if "outer_quantity" in action:
        if context.get("expression_contains_multiplicity") is True:
            if isinstance(action["outer_quantity"], int | float):
                candidate_fields["quantity"] = float(action["outer_quantity"])
            calculation_basis["multiplicity_policy"] = "expression_contains_internal_multiplier"
            bindings.append(_multiplicity_expression_binding(measurements))
        else:
            review_reasons.append(
                "outer quantity was not proposed because no explicit internal multiplier was found"
            )

    formula = action.get("formula") or action.get("preferred_formula")
    if isinstance(formula, str) and formula.strip():
        calculation_basis["formula"] = formula
        bindings.extend(_formula_bindings(formula, measurements, item))
        result = _evaluate_formula(formula, bindings)
        if result is not None:
            candidate_fields["engineering_quantity"] = result
            calculation_basis["evaluated_result"] = result
            if action.get("result_unit") is not None:
                calculation_basis["result_unit"] = action["result_unit"]
        else:
            review_reasons.append(
                "formula inputs are missing, conflicting, or not safely evaluable"
            )

    for key in (
        "quantity_role",
        "subentity_count_role",
        "candidate_path_role",
        "preferred_column_role",
        "quantity_dimensions",
        "unused_dimension_role",
        "required_segment_fields",
    ):
        if key in action:
            calculation_basis[key] = action[key]

    candidate_fields = {
        key: value for key, value in candidate_fields.items() if key in _CANDIDATE_FIELD_KEYS
    }
    return candidate_fields, calculation_basis, suggestions, bindings, review_reasons


def _role_requirement_bindings(
    rule: ConventionRule,
    measurements: Sequence[MeasurementCandidate],
    item: TakeoffItem | None,
    existing: Sequence[ConventionInputBinding],
) -> list[ConventionInputBinding]:
    bindings = list(existing)
    existing_roles = {binding.symbol for binding in bindings}
    for role in rule.required_measurement_roles:
        symbol = f"required_{role}"
        if symbol not in existing_roles:
            bindings.append(_binding_for_role(symbol, role, measurements, item))
    return bindings


def _existing_field_conflicts(
    item: TakeoffItem | None,
    candidate_fields: Mapping[str, float | str],
) -> list[str]:
    if item is None:
        return []
    conflicts: list[str] = []
    for field, candidate in candidate_fields.items():
        existing = getattr(item, field, None)
        if existing is None:
            continue
        if isinstance(existing, int | float) and isinstance(candidate, int | float):
            equal = math.isclose(float(existing), float(candidate), rel_tol=1e-9, abs_tol=1e-6)
        else:
            equal = _text(existing) == _text(candidate)
        if not equal:
            conflicts.append(f"candidate {field} conflicts with existing takeoff value")
    return conflicts


def _context_conflicts(context: Mapping[str, Any]) -> list[str]:
    conflicts: list[str] = []
    for field in ("component_family", "material_family", "pricing_basis"):
        values = context.get(field)
        if isinstance(values, list) and len(values) > 1:
            conflicts.append(f"multiple {field} classifications matched")
    return conflicts


def _confirmation_state(
    profile: ConventionProfile,
    rule: ConventionRule,
    component: ComponentInstance | None,
    bindings: Sequence[ConventionInputBinding],
    candidate_fields: Mapping[str, float | str],
    conflicts: Sequence[str],
) -> tuple[Literal["REVIEW", "CONFIRMED"], list[str], list[str]]:
    reasons: list[str] = []
    basis: list[str] = []
    if profile.lifecycle_status != "APPROVED" or profile.approval is None:
        reasons.append("profile is not explicitly APPROVED with reviewer audit")
    else:
        basis.extend(
            [
                f"profile_approved_by:{profile.approval.reviewer}",
                f"profile_reviewed_at:{profile.approval.reviewed_at.isoformat()}",
                f"profile_approval_reason:{profile.approval.reason}",
            ]
        )
    if rule.status != "APPROVED" or not rule.enabled_for_auto_apply:
        reasons.append("rule is not APPROVED and enabled")
    if component is None or component.status.value != "PASS":
        reasons.append("physical component is not PASS")
    if not candidate_fields and not bindings:
        reasons.append("rule produced no calculable candidate or audited input")
    elif not bindings:
        reasons.append("candidate has no bound upstream measurement input")
    if bindings and any(not binding.auditable for binding in bindings):
        reasons.append("one or more upstream measurement inputs are not auditable PASS entities")
    if bindings and any(binding.conflict for binding in bindings):
        reasons.append("conflicting upstream measurement candidates")
    reasons.extend(conflicts)
    if reasons:
        return "REVIEW", basis, list(dict.fromkeys(reasons))
    return "CONFIRMED", basis, []


def _stable_candidate_id(
    profile: ConventionProfile,
    rule: ConventionRule,
    component_id: str | None,
    sequence: int | None,
) -> str:
    raw = "|".join(
        [
            profile.profile_id,
            profile.profile_version,
            rule.id,
            component_id or "",
            str(sequence or ""),
        ]
    )
    return "convention:" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]


def build_convention_candidates(
    takeoff_payload: Mapping[str, Any],
    profile: ConventionProfile | Mapping[str, Any],
) -> dict[str, Any]:
    """Match a profile against a takeoff and return non-mutating convention candidates."""

    validated_profile = (
        profile if isinstance(profile, ConventionProfile) else load_convention_profile(profile)
    )
    raw_components = takeoff_payload.get("components", [])
    raw_items = takeoff_payload.get("items", [])
    raw_measurements = takeoff_payload.get("measurements", [])
    if not isinstance(raw_components, list) or not isinstance(raw_items, list):
        raise ValueError("takeoff components and items must be arrays")
    if not isinstance(raw_measurements, list):
        raise ValueError("takeoff measurements must be an array")

    components = [ComponentInstance.model_validate(value) for value in raw_components]
    items = [TakeoffItem.model_validate(value) for value in raw_items]
    measurements = [MeasurementCandidate.model_validate(value) for value in raw_measurements]
    component_by_id = {value.id: value for value in components}
    measurements_by_component: dict[str, list[MeasurementCandidate]] = defaultdict(list)
    for measurement in measurements:
        measurements_by_component[measurement.component_id].append(measurement)

    contexts: list[
        tuple[ComponentInstance | None, TakeoffItem | None, list[MeasurementCandidate]]
    ] = []
    represented_components: set[str] = set()
    for item in items:
        component = component_by_id.get(item.component_id) if item.component_id else None
        if component is not None:
            represented_components.add(component.id)
        contexts.append(
            (
                component,
                item,
                list(measurements_by_component.get(item.component_id or "", [])),
            )
        )
    for component in components:
        if component.id not in represented_components:
            contexts.append(
                (component, None, list(measurements_by_component.get(component.id, [])))
            )

    candidates: list[ConventionCandidate] = []
    matched_rule_counts: dict[str, int] = defaultdict(int)
    for component, item, component_measurements in contexts:
        context = _context_for(component, item, component_measurements, validated_profile)
        for rule in validated_profile.rules:
            if rule.status == "RETIRED":
                continue
            matches, matched_context = _rule_matches(rule, context)
            if not matches:
                continue
            matched_rule_counts[rule.id] += 1
            candidate_fields, calculation_basis, suggestions, bindings, reasons = (
                _candidate_payload(rule, context, component_measurements, item)
            )
            bindings = _role_requirement_bindings(rule, component_measurements, item, bindings)
            conflicts = [
                *_context_conflicts(context),
                *_existing_field_conflicts(item, candidate_fields),
            ]
            state, confirmation_basis, state_reasons = _confirmation_state(
                validated_profile,
                rule,
                component,
                bindings,
                candidate_fields,
                conflicts,
            )
            reasons.extend(state_reasons)
            measurement_ids = list(
                dict.fromkeys(
                    value for binding in bindings for value in binding.measurement_candidate_ids
                )
            )
            entity_ids = list(
                dict.fromkeys(value for binding in bindings for value in binding.entity_ids)
            )
            source_sheet_ids = list(
                dict.fromkeys(value for binding in bindings for value in binding.source_sheet_ids)
            )
            candidates.append(
                ConventionCandidate(
                    id=_stable_candidate_id(
                        validated_profile,
                        rule,
                        component.id if component else item.component_id if item else None,
                        item.sequence if item else None,
                    ),
                    profile_id=validated_profile.profile_id,
                    profile_version=validated_profile.profile_version,
                    rule_id=rule.id,
                    category=rule.category,
                    component_id=(
                        component.id if component else item.component_id if item else None
                    ),
                    sequence=item.sequence if item else None,
                    state=state,
                    matched_context=matched_context,
                    candidate_fields=candidate_fields,
                    calculation_basis=calculation_basis,
                    profile_suggestions=suggestions,
                    inputs=bindings,
                    measurement_candidate_ids=measurement_ids,
                    entity_ids=entity_ids,
                    source_sheet_ids=source_sheet_ids,
                    confirmation_basis=confirmation_basis,
                    review_reasons=list(dict.fromkeys(reasons)),
                )
            )

    state_counts = {
        state: sum(candidate.state == state for candidate in candidates)
        for state in ("REVIEW", "CONFIRMED")
    }
    return {
        "schema_version": OUTPUT_SCHEMA_VERSION,
        "profile": {
            "schema_version": validated_profile.schema_version,
            "profile_id": validated_profile.profile_id,
            "profile_version": validated_profile.profile_version,
            "lifecycle_status": validated_profile.lifecycle_status,
        },
        "policy": {
            "mutates_takeoff": False,
            "commercial_effect": "NONE",
            "quantity_default_allowed": False,
            "confirmation_requires": [
                "APPROVED profile with reviewer/reviewed_at/reason",
                "APPROVED enabled rule",
                "PASS physical component",
                "auditable PASS measurement entities",
                "no conflicting inputs or existing values",
            ],
        },
        "summary": {
            "context_count": len(contexts),
            "candidate_count": len(candidates),
            "state_counts": state_counts,
            "matched_rule_counts": dict(sorted(matched_rule_counts.items())),
        },
        "candidates": [value.model_dump(mode="json") for value in candidates],
    }


__all__ = [
    "ConventionApproval",
    "ConventionCandidate",
    "ConventionProfile",
    "ConventionRule",
    "build_convention_candidates",
    "load_convention_profile",
]
