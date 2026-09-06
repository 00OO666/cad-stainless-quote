"""Non-commercial, ID-only dimension choices for development experiments.

The evidence pack is produced by a trusted CAD adapter, not by the chooser.
Choices cannot supply numbers, units, counts or approval. This module deliberately
does not call the takeoff confirmation API and cannot produce a PASS quote.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from typing import Any

ROLES = frozenset({"width_mm", "length_mm", "height_mm", "depth_mm", "unfolded_width_mm"})


def _positive(value: Any) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(value)
        and value > 0
    )


def _index(records: Sequence[Mapping[str, Any]], key: str) -> dict[str, Mapping[str, Any]]:
    result = {}
    for record in records:
        identity = record.get(key)
        if not isinstance(identity, str) or not identity or identity in result:
            raise ValueError(f"Missing or duplicate {key}")
        result[identity] = record
    return result


def _sum_value(entries: list[Mapping[str, Any]]) -> tuple[float | None, str | None]:
    """Only contiguous collinear native spans can be added; nesting is not a chain."""
    spans = [entry.get("span") for entry in entries]
    if any(not isinstance(span, Mapping) for span in spans):
        return None, "CHAIN_HAS_NO_NATIVE_SPANS"
    frames = {
        (entry["source_file_id"], span.get("coordinate_space"), span.get("axis"))
        for entry, span in zip(entries, spans, strict=True)
    }
    if len(frames) != 1 or any(not part for part in next(iter(frames))):
        return None, "CHAIN_CROSSES_COORDINATE_FRAMES"
    intervals = []
    for entry, span in zip(entries, spans, strict=True):
        endpoints = span.get("interval_mm")
        if not isinstance(endpoints, list) or len(endpoints) != 2:
            return None, "CHAIN_HAS_NO_NATIVE_SPANS"
        if any(not isinstance(v, (int, float)) or not math.isfinite(v) for v in endpoints):
            return None, "CHAIN_HAS_INVALID_SPAN"
        start, end = sorted(endpoints)
        value = entry.get("value_mm")
        if not _positive(value) or not math.isclose(end - start, value, abs_tol=0.05):
            return None, "CHAIN_SPAN_VALUE_CONFLICT"
        intervals.append((start, end))
    intervals.sort()
    for (_, previous_end), (start, _) in zip(intervals, intervals[1:], strict=False):
        if start < previous_end - 0.05:
            return None, "CHAIN_OVERLAP_OR_NESTING"
        if start > previous_end + 0.05:
            return None, "CHAIN_GAP"
    return sum(entry["value_mm"] for entry in entries), None


def resolve_choices(
    cases: Sequence[Mapping[str, Any]],
    evidence: Sequence[Mapping[str, Any]],
    choices: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """Bind a choice to CAD evidence; absent choices remain explicit abstentions.

    Evidence lineage and allowed_case_ids are adapter-controlled scope constraints,
    not proof that the selected dimension measures the requested physical object.
    The return value is deliberately a research artifact, never a QuoteItem.
    """
    case_by_id = _index(cases, "case_id")
    evidence_by_id = _index(evidence, "candidate_id")
    choice_by_id = _index(choices, "case_id")
    if set(choice_by_id) - set(case_by_id):
        raise ValueError("Choice references an unknown case")
    for entry in evidence:
        for key in ("source_file_id", "source_sha256", "entity_id", "sheet_id",
                    "board_sha256", "artifact_sha256"):
            if not entry.get(key):
                raise ValueError(f"Evidence lacks {key}")
        if entry.get("origin") != "cad_numbered_board":
            raise ValueError("Evidence is not from the trusted numbered CAD board adapter")
        if not _positive(entry.get("raw_value")):
            raise ValueError("Evidence has invalid native dimension")
        if entry.get("value_mm") is not None:
            if entry.get("units") != "millimeters" or not _positive(entry["value_mm"]):
                raise ValueError("Unproved unit conversion")
            if entry["value_mm"] != entry["raw_value"]:
                raise ValueError("Metric value differs from its native millimeter evidence")
        allowed = entry.get("allowed_case_ids")
        if not isinstance(allowed, list) or not allowed or set(allowed) - set(case_by_id):
            raise ValueError("Invalid evidence scope")

    rows = []
    for case_id in case_by_id:
        choice = choice_by_id.get(case_id, {"case_id": case_id, "roles": {}})
        if set(choice) - {"case_id", "roles", "reason"}:
            raise ValueError("Chooser may only submit IDs, operations and a reason")
        roles = choice.get("roles", {})
        if not isinstance(roles, Mapping) or set(roles) - ROLES:
            raise ValueError("Unsupported role; quantity and approval cannot be supplied")
        bindings = {}
        values = dict.fromkeys(sorted(ROLES))
        used: set[str] = set()
        used_native: set[tuple[str, str]] = set()
        for role, selection in roles.items():
            if not isinstance(selection, Mapping) or set(selection) != {"candidate_ids", "op"}:
                raise ValueError("Selection must contain candidate_ids and op only")
            ids = selection["candidate_ids"]
            if not isinstance(ids, list) or not ids or any(not isinstance(i, str) for i in ids):
                raise ValueError("Selection must contain nonempty candidate IDs")
            if len(set(ids)) != len(ids) or used.intersection(ids):
                raise ValueError("A native dimension cannot be reused in multiple roles")
            used.update(ids)
            if selection["op"] not in {"single", "sum"}:
                raise ValueError("Unsupported dimension operation")
            if selection["op"] == "single" and len(ids) != 1:
                raise ValueError("Single requires exactly one candidate")
            if selection["op"] == "sum" and len(ids) < 2:
                raise ValueError("Sum requires at least two candidates")
            if any(i not in evidence_by_id for i in ids):
                raise ValueError("Unknown CAD candidate ID")
            entries = [evidence_by_id[i] for i in ids]
            if any(case_id not in e["allowed_case_ids"] for e in entries):
                raise ValueError("Candidate is outside this case's evidence scope")
            native_ids = [
                (e["source_file_id"], e.get("original_entity_id") or e["entity_id"])
                for e in entries
            ]
            if len(set(native_ids)) != len(native_ids) or used_native.intersection(native_ids):
                raise ValueError("Native dimension aliases cannot be reused")
            used_native.update(native_ids)
            error = None
            value = None
            if any(e.get("value_mm") is None for e in entries):
                error = "SOURCE_UNITS_UNRESOLVED"
            elif selection["op"] == "sum":
                value, error = _sum_value(entries)
            else:
                value = entries[0]["value_mm"]
            values[role] = value
            bindings[role] = {
                "candidate_ids": ids, "operation": selection["op"],
                "native_values": [e["raw_value"] for e in entries],
                "native_units": [e.get("units") for e in entries],
                "value_mm": value, "state": "REVIEW",
                "reason_codes": [error] if error else ["PHYSICAL_ROLE_NOT_CONFIRMED"],
            }
        area = None
        if values["width_mm"] is not None and values["length_mm"] is not None:
            area = values["width_mm"] * values["length_mm"] / 1_000_000
        rows.append({
            "case_id": case_id, "state": "REVIEW" if bindings else "ABSTAIN",
            "fields": values, "bindings": bindings,
            "rectangular_area_per_instance_m2": area,
            "quantity": None, "engineering_quantity": None, "amount": None,
            "warning": "Role hypothesis only; per-instance rectangle is not billable takeoff",
            "reason": choice.get("reason", "No ID selection made in this experiment"),
        })
    return {
        "schema_version": "research-id-choices/1", "commercial_use": False,
        "formal_accuracy": None, "rows": rows,
    }


def occurrence_coverage(
    occurrences: Sequence[Mapping[str, Any]],
    components: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """Detect unconsumed current input, without interpreting annotation count as objects."""
    available = {record["id"] for record in occurrences}
    consumed = {
        identity for component in components
        for key in ("plan_occurrence_ids", "elevation_occurrence_ids")
        for identity in component.get(key, [])
    }
    return {
        "input_occurrence_count": len(available),
        "consumed_occurrence_count": len(available & consumed),
        "unconsumed_occurrence_ids": sorted(available - consumed),
        "orphaned_component_occurrence_ids": sorted(consumed - available),
        "warning": "Coverage diagnostic only; detail/excluded occurrences may be unconsumed",
    }
