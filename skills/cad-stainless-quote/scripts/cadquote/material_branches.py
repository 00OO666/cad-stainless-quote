"""Preserve every exact native LEADER contact of a material annotation.

Branches are geometry-routing candidates, never new occurrences or quantities.
The old single-target occurrence remains unchanged, including its ambiguity.
"""

from __future__ import annotations

import hashlib
import json
import math
from collections import Counter, defaultdict
from collections.abc import Sequence

from shapely.geometry import LineString, Point, Polygon


def _point(value):
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)) or len(value) < 2:
        return None
    try:
        p = tuple(float(v) for v in value)
    except (ValueError, TypeError):
        return None
    if len(p) not in {2, 3} or not all(math.isfinite(v) for v in p):
        return None
    if len(p) == 3 and abs(p[2]) > 1e-6:
        return None
    return p[:2]


def _scope(entity):
    return entity.source_file_id, entity.sheet_id, entity.space


def _id(*parts):
    payload = json.dumps(parts, sort_keys=True, ensure_ascii=False).encode("utf8")
    return "material-branch:" + hashlib.sha256(payload).hexdigest()[:24]


def _terminal_entry(vertices, polygon, tolerance):
    """An explicit last segment crosses the border once and ends inside it.

    Do not extend a line, snap an endpoint or infer connection from proximity.
    Earlier vertices/segments must not enter this label. This is connectivity
    evidence only, not proof of a physical component or material surface.
    """
    landing = Point(vertices[-1])
    previous = Point(vertices[-2])
    if not polygon.contains(landing) or polygon.boundary.distance(landing) <= tolerance:
        return None
    if polygon.distance(previous) <= tolerance:
        return None
    if len(vertices) > 2 and LineString(vertices[:-1]).intersects(polygon):
        return None
    crossing = LineString(vertices[-2:]).intersection(polygon.boundary)
    if crossing.geom_type != "Point":
        return None
    return list(crossing.coords[0])


def discover_material_leader_branches(
    entities,
    occurrences,
    *,
    contact_tolerance=1e-6,
    allow_terminal_entry=False,
    max_entities=200_000,
    max_occurrences=10_000,
    max_contact_checks=500_000,
    max_branches=20_000,
):
    """Return all same-source/sheet/space contacts on native annotation borders.

    Opt-in terminal entry also accepts one native final-segment border crossing.
    Supports indexed native LEADER vertices, not proximity, inferred text centers,
    or arbitrary MULTILEADER path/landing reconstruction. Coincident competing
    label borders remain explicitly ambiguous. Input limits fail closed.
    """
    if not math.isfinite(contact_tolerance) or contact_tolerance <= 0:
        raise ValueError("positive finite contact tolerance required")
    if not isinstance(allow_terminal_entry, bool):
        raise ValueError("allow_terminal_entry must be a boolean")
    limits = {
        "max_entities": max_entities,
        "max_occurrences": max_occurrences,
        "max_contact_checks": max_contact_checks,
        "max_branches": max_branches,
    }
    if any(not isinstance(v, int) or isinstance(v, bool) or v < 1 for v in limits.values()):
        raise ValueError("positive integer limits required")
    result = {
        "schema_version": "material-leader-branches/1.0",
        "state": "REVIEW",
        "mutates_takeoff": False,
        "creates_occurrences": False,
        "physical_quantity": None,
        "measurement_role": None,
        "limits": {**limits, "contact_tolerance_drawing_units": contact_tolerance},
        "allow_terminal_entry": allow_terminal_entry,
        "records": [],
        "issues": [],
        "truncated": False,
        "summary": {},
        "limitations": [
            "Branch count is not a physical instance, face or billable quantity.",
            "Border contact proves only annotation connectivity, not physical ownership.",
            "Supported native LEADER only; missing/unsupported paths are not negative evidence.",
            "Indexed visibility and coordinates must already reflect native layout/viewport state.",
        ],
    }
    if len(entities) > max_entities or len(occurrences) > max_occurrences:
        result["truncated"] = True
        result["issues"].append({"reason": "INPUT_CAP"})
        return result
    by_scope = defaultdict(list)
    by_id = defaultdict(list)
    for e in entities:
        by_id[e.id].append(e)
        if not e.geometry.get("semantic_hidden"):
            by_scope[_scope(e)].append(e)
    duplicate_ids = {key for key, values in by_id.items() if len(values) != 1}
    for key in sorted(duplicate_ids):
        result["issues"].append({"entity_id": key, "reason": "DUPLICATE_ENTITY_ID"})
    frames = {}
    contacts = defaultdict(list)
    checks = 0
    for scope, group in sorted(by_scope.items(), key=lambda item: str(item[0])):
        group = [e for e in group if e.id not in duplicate_ids]
        local_frames = []
        for e in group:
            if (
                e.entity_type != "INSERT"
                or not e.handle
                or not e.geometry.get("annotation_boundary")
            ):
                continue
            points = [_point(p) for p in e.geometry["annotation_boundary"]]
            if len(points) != 4 or any(p is None for p in points):
                result["issues"].append({"entity_id": e.id, "reason": "INVALID_ANNOTATION_BORDER"})
                continue
            poly = Polygon(points)
            if not poly.is_valid or poly.area <= contact_tolerance**2:
                result["issues"].append({"entity_id": e.id, "reason": "INVALID_ANNOTATION_BORDER"})
                continue
            local_frames.append((e, poly))
        frames[scope] = [e for e, _ in local_frames]
        for leader in sorted(group, key=lambda e: e.id):
            if leader.entity_type not in {"LEADER", "MLEADER", "MULTILEADER"}:
                continue
            if leader.entity_type != "LEADER":
                result["issues"].append(
                    {"entity_id": leader.id, "reason": "UNSUPPORTED_LEADER_TYPE"}
                )
                continue
            vertices = [_point(p) for p in leader.geometry.get("vertices", [])]
            if len(vertices) < 2 or any(p is None for p in vertices):
                result["issues"].append(
                    {"entity_id": leader.id, "reason": "INVALID_LEADER_VERTICES"}
                )
                continue
            landing, tip = vertices[-1], vertices[0]
            targets = [_point(p) for p in leader.geometry.get("leader_targets", [])]
            explicit = _point(leader.geometry.get("leader_target"))
            if explicit is not None:
                targets.append(explicit)
            target_conflict = any(
                p is None or math.dist(p, tip) > contact_tolerance for p in targets
            )
            owners = []
            crossed_frames = set()
            terminal = LineString(vertices[-2:]) if allow_terminal_entry else None
            for frame, polygon in local_frames:
                checks += 1
                if checks > max_contact_checks:
                    result["truncated"] = True
                    result["issues"].append({"reason": "CONTACT_CHECK_CAP"})
                    break
                if allow_terminal_entry and terminal.intersects(polygon):
                    crossed_frames.add(frame.id)
                if polygon.boundary.distance(Point(landing)) <= contact_tolerance:
                    owners.append((frame, "NATIVE_LEADER_LANDING_ON_ANNOTATION_BORDER", None))
                elif allow_terminal_entry:
                    crossing = _terminal_entry(vertices, polygon, contact_tolerance)
                    if crossing is not None:
                        owners.append(
                            (frame, "NATIVE_TERMINAL_SEGMENT_ENTERS_ANNOTATION", crossing)
                        )
            if result["truncated"]:
                break
            attached = leader.geometry.get("annotation_handle")
            for frame, connection_basis, crossing in owners:
                reasons = []
                if len(owners) != 1:
                    reasons.append("AMBIGUOUS_ANNOTATION_OWNER")
                if attached and attached != frame.handle:
                    reasons.append("NATIVE_ATTACHMENT_CONFLICT")
                if target_conflict:
                    reasons.append("CONFLICTING_ARROW_TARGETS")
                if math.dist(landing, tip) <= contact_tolerance:
                    reasons.append("ZERO_SPAN_LEADER")
                if crossing is not None and crossed_frames - {frame.id}:
                    reasons.append("TERMINAL_SEGMENT_INTERSECTS_OTHER_ANNOTATION")
                contacts[(scope, frame.id)].append(
                    {
                        "leader_entity_id": leader.id,
                        "leader_handle": leader.handle,
                        "original_leader_entity_id": leader.geometry.get("original_entity_id"),
                        "landing_point": list(landing),
                        "leader_target": None if target_conflict else list(tip),
                        "native_vertices": [list(p) for p in vertices],
                        "competing_annotation_entity_ids": sorted(e.id for e, _, _ in owners),
                        "connection_basis": connection_basis,
                        "terminal_border_intersection": crossing,
                        "reason_codes": reasons,
                        "geometry_routing_eligible": not reasons,
                        "state": "REVIEW",
                        "physical_quantity": None,
                        "measurement_role": None,
                    }
                )
        if result["truncated"]:
            break
    branch_count = 0
    for o in sorted(occurrences, key=lambda o: o.id):
        evidence = [by_id[key][0] for key in o.entity_ids if len(by_id[key]) == 1]
        evidence = [
            e
            for e in evidence
            if e.source_file_id == o.source_file_id
            and e.sheet_id == o.sheet_id
            and not e.geometry.get("semantic_hidden")
        ]
        parents = {
            (e.geometry.get("parent_insert_handle"), _scope(e))
            for e in evidence
            if e.entity_type == "ATTRIB" and e.geometry.get("parent_insert_handle")
        }
        identity = getattr(o, "annotation_identity", None)
        if identity and any(parent == identity for parent, _ in parents):
            parents = {(parent, scope) for parent, scope in parents if parent == identity}
        record = {
            "occurrence_id": o.id,
            "mt_code": o.mt_code,
            "source_file_id": o.source_file_id,
            "sheet_id": o.sheet_id,
            "state": "REVIEW",
            "physical_quantity": None,
            "measurement_role": None,
            "branches": [],
            "reason_codes": [],
            "original_single_target": o.leader_target,
        }
        if len(parents) != 1:
            record["reason_codes"].append("ANNOTATION_PARENT_MISSING_OR_AMBIGUOUS")
        else:
            parent, scope = next(iter(parents))
            matches = [e for e in frames.get(scope, []) if e.handle == parent]
            if len(matches) != 1:
                record["reason_codes"].append("ANNOTATION_BORDER_MISSING_OR_AMBIGUOUS")
            else:
                frame = matches[0]
                record.update(
                    {
                        "annotation_entity_id": frame.id,
                        "annotation_handle": frame.handle,
                        "coordinate_space": frame.space,
                        "viewport_handle": frame.geometry.get("panel_viewport_handle"),
                        "paper_to_model_transform": frame.geometry.get("paper_to_model_transform"),
                    }
                )
                for contact in contacts.get((scope, frame.id), []):
                    branch_count += 1
                    if branch_count > max_branches:
                        result["truncated"] = True
                        record["reason_codes"].append("BRANCH_CAP")
                        break
                    record["branches"].append(
                        {
                            **contact,
                            "branch_id": _id(
                                o.source_file_id,
                                o.sheet_id,
                                scope[2],
                                frame.id,
                                contact["leader_entity_id"],
                                contact["native_vertices"],
                            ),
                            "occurrence_id": o.id,
                            "source_file_id": o.source_file_id,
                            "sheet_id": o.sheet_id,
                            "annotation_entity_id": frame.id,
                            "mt_code": o.mt_code,
                            "evidence_entity_ids": sorted(
                                {frame.id, contact["leader_entity_id"], *(e.id for e in evidence)}
                            ),
                        }
                    )
                if not record["branches"]:
                    record["reason_codes"].append("NO_SUPPORTED_BORDER_CONTACT")
        result["records"].append(record)
        if branch_count > max_branches:
            break
    if result["truncated"]:
        for record in result["records"]:
            record["reason_codes"].append("INCOMPLETE_BRANCH_SCAN")
            for branch in record["branches"]:
                branch["geometry_routing_eligible"] = False
                branch["reason_codes"].append("INCOMPLETE_BRANCH_SCAN")
    result["summary"] = {
        "occurrence_records": len(result["records"]),
        "occurrences_with_branches": sum(bool(r["branches"]) for r in result["records"]),
        "multi_branch_annotations": sum(len(r["branches"]) > 1 for r in result["records"]),
        "branches": sum(len(r["branches"]) for r in result["records"]),
        "geometry_routing_eligible_branches": sum(
            b["geometry_routing_eligible"] for r in result["records"] for b in r["branches"]
        ),
        "branch_reasons": dict(
            Counter(
                reason
                for r in result["records"]
                for b in r["branches"]
                for reason in b["reason_codes"]
            )
        ),
        "contact_checks": checks,
    }
    return result
