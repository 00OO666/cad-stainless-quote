"""Resolve numbered elevation/detail references without choosing a billable object.

The local subview number is not the sheet number. Reciprocal title attributes
may sit outside the viewport and therefore be absent from projected panels.
Keep every alternative and explicit geometric title-association limitations.
"""

from __future__ import annotations

import re
from collections import Counter, defaultdict

from .linking import (
    _stable_id,
    extract_structured_reference_callouts,
    normalize_reference_code,
)
from .materials import find_material_codes


def _visible(entity):
    return not entity.geometry.get("semantic_hidden")


def _title_members(ref, by_id, groups):
    entity = by_id[ref.entity_ids[0]]
    return groups[(entity.source_file_id, entity.sheet_id, entity.space, ref.parent_insert_handle)]


def _is_detail_title(members):
    # A generic code bubble is not the numbered title of a detail subview.
    return any(
        e.text
        and (
            "DETAIL" in e.text.upper()
            or "节点图" in e.text
            or "大样图" in e.text
            or "详图" in e.text
        )
        for e in members
    )


def _contains(outer, inner):
    return (
        outer[0] <= inner[0] <= inner[2] <= outer[2]
        and outer[1] <= inner[1] <= inner[3] <= outer[3]
    )


def _native_page_frames(native, groups, viewports):
    frames = []
    for entity in native:
        if entity.entity_type != "INSERT" or not entity.bbox or not entity.handle:
            continue
        members = groups.get(
            (entity.source_file_id, entity.sheet_id, entity.space, entity.handle), []
        )
        codes = {normalize_reference_code(e.text) for e in members}
        codes = {
            c
            for c in codes
            if c and re.fullmatch(r"(?:[A-Z0-9]+-)*(?:QS|GS|DL|DE|DT|DZ|DS|TD|CD)-\d+", c)
        }
        if len(codes) != 1:
            continue
        contained = [
            v
            for v in viewports.values()
            if (v.source_file_id, v.space) == (entity.source_file_id, entity.space)
            and _contains(entity.bbox, v.bbox)
        ]
        if contained:
            frames.append(
                {
                    "entity": entity,
                    "code": next(iter(codes)),
                    "viewport_handles": {v.handle for v in contained},
                    "code_entity_ids": sorted(
                        e.id for e in members if normalize_reference_code(e.text) in codes
                    ),
                }
            )
    return frames


def build_detail_routes(sheets, entities, native_entities, *, max_title_gap_ratio=1.0):
    """Indexed CAD only; source package scope is the supplied sheet collection.

    ``UNIQUE_RECIPROCAL_CANDIDATE`` is navigation, not a confirmed material,
    section, measurement or commercial PASS. Same-code materials are listed,
    never merged. This stage reports numbered references only.
    """
    if not 0 < max_title_gap_ratio <= 2:
        raise ValueError("title gap ratio must be positive and at most two")
    sheets, entities, native_entities = list(sheets), list(entities), list(native_entities)
    if len({s.id for s in sheets}) != len(sheets):
        raise ValueError("duplicate sheet identity")
    if len({e.id for e in native_entities}) != len(native_entities):
        raise ValueError("duplicate native entity identity")
    sheet_map = {s.id: s for s in sheets}
    by_id = {e.id: e for e in native_entities}
    projected = [e for e in entities if _visible(e)]
    native = [e for e in native_entities if _visible(e)]
    groups, panel_entities, viewports = defaultdict(list), defaultdict(list), {}
    for e in native:
        parent = e.geometry.get("parent_insert_handle")
        if parent:
            groups[(e.source_file_id, e.sheet_id, e.space, str(parent))].append(e)
        if e.entity_type == "VIEWPORT" and e.handle and e.bbox:
            key = (e.source_file_id, e.space, e.handle)
            if key in viewports:
                raise ValueError("duplicate viewport identity")
            viewports[key] = e
    for e in projected:
        panel_entities[e.sheet_id].append(e)
    native_titles = []
    for ref in extract_structured_reference_callouts(native):
        members = _title_members(ref, by_id, groups)
        if _is_detail_title(members):
            anchor = by_id[ref.entity_ids[0]]
            points = [by_id[i].insert for i in ref.entity_ids]
            if all(p is not None for p in points):
                native_titles.append((ref, anchor, points))

    details_by_code = defaultdict(list)
    page_recovery = {}
    frames = _native_page_frames(native, groups, viewports)
    for s in sheets:
        layout = (s.layout or "").split("#viewport:")[0]
        frame_matches = [
            f
            for f in frames
            if f["entity"].source_file_id == s.source_file_id
            and f["entity"].space == "paper:" + layout
            and s.viewport_handle in f["viewport_handles"]
        ]
        if frame_matches:
            codes = {f["code"] for f in frame_matches}
            page_recovery[s.id] = {
                "original_drawing_number": s.drawing_number,
                "original_kind": s.kind,
                "candidate_page_codes": sorted(codes),
                "frame_handles": sorted(f["entity"].handle for f in frame_matches),
                "code_entity_ids": sorted({i for f in frame_matches for i in f["code_entity_ids"]}),
                "state": "CANDIDATE",
                "conflict": len(codes) != 1,
                "basis": "native_title_block_contains_entire_viewport",
            }
            for code in sorted(codes):
                details_by_code[code].append(s)
        elif s.kind == "detail" and s.drawing_number:
            code = normalize_reference_code(s.drawing_number)
            if code:
                details_by_code[code].append(s)

    # Reattach title candidates omitted by viewport clipping. An immediate
    # vertical-column neighbour is framing evidence only, not physical proof.
    titles_by_panel = defaultdict(list)
    for code, panels in details_by_code.items():
        for ref, anchor, points in native_titles:
            eligible = []
            for p in panels:
                if p.source_file_id != anchor.source_file_id or not p.viewport_handle:
                    continue
                layout = (p.layout or "").split("#viewport:")[0]
                vp = viewports.get((p.source_file_id, "paper:" + layout, p.viewport_handle))
                if vp is None or anchor.space != vp.space:
                    continue
                x0, y0, x1, y1 = vp.bbox
                if all(
                    x0 <= pt[0] <= x1 and y0 - max_title_gap_ratio * (y1 - y0) <= pt[1] <= y0
                    for pt in points
                ):
                    eligible.append((p, vp, y0 - max(pt[1] for pt in points)))
            if not eligible:
                continue
            gap = min(item[2] for item in eligible)
            # Equal/overlapping candidates are retained; lexical order is not selection.
            for p, vp, distance in eligible:
                if abs(distance - gap) <= 1e-6:
                    recovery = page_recovery.get(p.id)
                    if recovery and recovery["conflict"]:
                        continue
                    titles_by_panel[p.id].append(
                        {
                            "page_code": code,
                            "view_number": ref.view_number,
                            "back_reference": ref.code,
                            "native_title_entity_ids": list(ref.entity_ids),
                            "title_parent_handle": ref.parent_insert_handle,
                            "viewport_handle": vp.handle,
                            "paper_viewport_bbox": list(vp.bbox),
                            "paper_title_points": [list(pt) for pt in points],
                            "basis": "same_native_parent_title_immediate_vertical_column",
                            "state": "CANDIDATE",
                        }
                    )

    records = []
    for ref in extract_structured_reference_callouts(projected):
        source = sheet_map.get(ref.sheet_id)
        if source is None or source.kind != "elevation":
            continue
        # Include outgoing references even if the target page was not recovered.
        source_code = normalize_reference_code(source.drawing_number)
        if ref.code == source_code:
            continue
        alternatives = []
        for target in sorted(details_by_code.get(ref.code, []), key=lambda s: s.id):
            titles = titles_by_panel.get(target.id, [])
            matching = [
                t
                for t in titles
                if t["view_number"] == ref.view_number and t["back_reference"] == source_code
            ]
            conflicts = [
                t
                for t in titles
                if t["view_number"] == ref.view_number and t["back_reference"] != source_code
            ]
            materials = []
            for e in sorted(panel_entities[target.id], key=lambda e: e.id):
                codes = {m.normalized_code for m in find_material_codes(e.text or "")}
                # Preserve adjacent non-steel code labels for exclusion review;
                # this does not register these families as stainless materials.
                codes.update(
                    re.findall(r"\b[A-Z]{1,8}-[A-Z]{1,8}-\d{1,4}\b", (e.text or "").upper())
                )
                if codes:
                    materials.append(
                        {
                            "entity_id": e.id,
                            "handle": e.handle,
                            "parent_insert_handle": e.geometry.get("parent_insert_handle"),
                            "codes": sorted(codes),
                            "text": e.text,
                            "state": "CANDIDATE",
                            "steel_surface_confirmed": False,
                        }
                    )
            alternatives.append(
                {
                    "sheet_id": target.id,
                    "source_file_id": target.source_file_id,
                    "drawing_number": target.drawing_number,
                    "resolved_page_code": ref.code,
                    "page_recovery": page_recovery.get(target.id),
                    "layout": target.layout,
                    "model_bbox": list(target.bbox) if target.bbox else None,
                    "viewport_handle": target.viewport_handle,
                    "matching_titles": matching,
                    "conflicting_titles": conflicts,
                    "all_title_candidates": titles,
                    "material_candidates": materials,
                    "reciprocal_candidate": len(matching) == 1 and not conflicts,
                }
            )
        matches = [p for p in alternatives if p["reciprocal_candidate"]]
        state = (
            "UNIQUE_RECIPROCAL_CANDIDATE"
            if len(matches) == 1
            else "AMBIGUOUS_RECIPROCAL"
            if matches
            else "PAGE_ONLY"
            if alternatives
            else "TARGET_NOT_FOUND"
        )
        identity = {
            "source_sheet_id": source.id,
            "target_page": ref.code,
            "target_view": ref.view_number,
            "source_callout_parent": ref.parent_insert_handle,
        }
        records.append(
            {
                "route_id": _stable_id("detail-route", identity),
                **identity,
                "source_file_id": source.source_file_id,
                "source_drawing_number": source.drawing_number,
                "source_reference_entity_ids": list(ref.entity_ids),
                "navigation_state": state,
                "suggested_detail_sheet_id": matches[0]["sheet_id"] if len(matches) == 1 else None,
                "page_candidate_count": len(alternatives),
                "reciprocal_candidate_count": len(matches),
                "candidates": alternatives,
                "state": "REVIEW",
                "physical_component_confirmed": False,
                "measurement_role": None,
                "physical_quantity": None,
            }
        )
    return {
        "schema_version": "numbered-detail-routes/1.0",
        "path_scope": "local_run_diagnostics",
        "state": "REVIEW",
        "mutates_takeoff": False,
        "human_answers_used": False,
        "input_counts": {
            "sheets": len(sheets),
            "entities": len(entities),
            "native_entities": len(native_entities),
        },
        "summary": dict(Counter(r["navigation_state"] for r in records)),
        "records": records,
        "limitations": [
            "Only visible same-parent numbered references are supported; no negative search claim.",
            "Native title/viewport column association needs visual checking; not component proof.",
            "Material candidates remain separate; matching codes do not identify a steel surface.",
            "Conflicting titles, duplicate revisions and page-only matches are not auto-selected.",
        ],
    }


def annotate_detail_route_edges(edges, routes):
    """Annotate existing graph candidates, never create or promote a PASS edge."""
    markers = defaultdict(list)
    for r in routes["records"]:
        if r["suggested_detail_sheet_id"]:
            markers[(r["source_sheet_id"], r["suggested_detail_sheet_id"])].append(
                "numbered_detail_navigation_candidate:" + r["route_id"]
            )
    result = []
    for edge in edges:
        extra = markers.get((edge.source_id, edge.target_id), [])
        if edge.relation == "elevation_to_detail" and extra:
            basis = sorted(set(edge.basis + extra))
            identity = {
                "relation": edge.relation,
                "source_id": edge.source_id,
                "target_id": edge.target_id,
                "confidence": edge.confidence,
                "basis": basis,
            }
            result.append(
                edge.model_copy(update={"basis": basis, "id": _stable_id("edge", identity)})
            )
        else:
            result.append(edge)
    return result
