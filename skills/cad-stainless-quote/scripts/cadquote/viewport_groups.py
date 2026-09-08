"""Paper-space broken-view candidates, never physical or measurable unions.

Close paper gaps alone are insufficient: fragments must also share a native
page, scale, orientation and transverse model coordinates. Titles compete
explicitly; neither the closest caption nor the first viewport wins.
"""

from __future__ import annotations

import math
from collections import Counter, defaultdict
from itertools import combinations

from .detail_routes import _native_page_frames, _view_title_kind, _visible
from .linking import _stable_id, extract_structured_reference_callouts


def _union(boxes):
    return [
        min(b[0] for b in boxes),
        min(b[1] for b in boxes),
        max(b[2] for b in boxes),
        max(b[3] for b in boxes),
    ]


def _valid_box(box):
    return (
        box is not None
        and len(box) == 4
        and all(math.isfinite(v) for v in box)
        and box[0] < box[2]
        and box[1] < box[3]
    )


def _projection(vp):
    g = vp.geometry
    direction = g.get("view_direction_vector", [])
    h, mh = g.get("height"), g.get("view_height")
    model = g.get("model_bbox")
    if (
        len(direction) != 3
        or not all(math.isfinite(x) for x in direction)
        or direction[2] <= 0
        or abs(direction[0]) > 1e-7
        or abs(direction[1]) > 1e-7
        or abs(g.get("view_twist_angle") or 0) > 1e-7
        or not h
        or not mh
        or not math.isfinite(h / mh)
        or h / mh <= 0
        or not _valid_box(model)
    ):
        return None
    return h / mh, model


def _fragment_edge(a, b, max_gap_ratio):
    pa, pb = _projection(a), _projection(b)
    if pa is None or pb is None or not math.isclose(pa[0], pb[0], rel_tol=1e-7):
        return None
    for axis in (0, 1):
        cross = 1 - axis
        first, second = sorted((a, b), key=lambda v: v.bbox[axis])
        x, y = first.bbox, second.bbox
        transverse = min(x[cross + 2] - x[cross], y[cross + 2] - y[cross])
        eps = max(1e-6, transverse * 1e-7)
        gap = y[axis] - x[axis + 2]
        if not (eps < gap <= max_gap_ratio * transverse):
            continue
        if any(abs(x[k] - y[k]) > eps for k in (cross, cross + 2)):
            continue
        mx, my = first.geometry["model_bbox"], second.geometry["model_bbox"]
        if any(abs(mx[k] - my[k]) > eps / pa[0] for k in (cross, cross + 2)):
            continue
        # Repeated, reversed or overlapping model windows are not a broken chain.
        model_gap = my[axis] - mx[axis + 2]
        if model_gap <= eps / pa[0]:
            continue
        return {
            "members": [first.handle, second.handle],
            "axis": "xy"[axis],
            "paper_gap": gap,
            "paper_gap_ratio": gap / transverse,
            "model_gap_raw": model_gap,
            "basis": "aligned_paper_and_model_cross_axis_same_scale_disjoint_windows",
        }
    return None


def _title_relation(group, title):
    x0, y0, x1, y1 = group["paper_viewport_bbox"]
    w, h = x1 - x0, y1 - y0
    points = title["paper_title_points"]
    # The lower text insertion of a divided title bubble can slightly protrude
    # left of a viewport. Wide/shallow sections can have a larger caption gap.
    pad = 0.05 * min(w, h)
    if all(x0 - pad <= x <= x1 + pad and y0 - max(h, 0.6 * w) <= y <= y0 for x, y in points):
        return "below"
    if all(x1 < x <= x1 + 0.65 * w and y0 - 0.15 * h <= y <= y0 + 0.20 * h for x, y in points):
        return "bottom_right"
    return None


def _unique_caption_assignment(groups, *, max_search=10000):
    """Accept only unique complete matchings in each title/group component.

    No minimum-distance optimization. A missing title, duplicate title, tie or
    bounded-search exhaustion leaves the whole connected component unresolved.
    """
    eligible = {
        g["group_id"]: {t["parent_handle"] for t in g["title_candidates"]}
        for g in groups
        if g["title_candidates"]
    }
    remaining, assignments, diagnostics = set(eligible), {}, []
    while remaining:
        gids, parents = {min(remaining)}, set()
        while True:
            p = set().union(*(eligible[g] for g in gids))
            expanded = {g for g in eligible if eligible[g] & p} | gids
            if expanded == gids and p == parents:
                break
            gids, parents = expanded, p
        remaining -= gids
        budget, solutions = [0], []

        def search(todo, selected, budget=budget, solutions=solutions):
            budget[0] += 1
            if budget[0] > max_search or len(solutions) > 1:
                return
            if not todo:
                solutions.append(dict(selected))
                return
            used = set(selected.values())
            gid = min(todo, key=lambda x: (len(eligible[x] - used), x))
            for parent in sorted(eligible[gid] - used):
                search(todo - {gid}, {**selected, gid: parent})
                if budget[0] > max_search or len(solutions) > 1:
                    break

        if len(gids) == len(parents) and all(
            not g["overlapping_group_ids"] for g in groups if g["group_id"] in gids
        ):
            search(gids, {})
        unique = len(solutions) == 1 and budget[0] <= max_search
        if unique:
            assignments.update(solutions[0])
        diagnostics.append(
            {
                "group_ids": sorted(gids),
                "title_parent_handles": sorted(parents),
                "unique_complete_matching": unique,
                "search_steps": budget[0],
                "search_truncated": budget[0] > max_search,
            }
        )
    return assignments, diagnostics


def _blocked(group, title, others, relation):
    # A caption cannot jump over an intervening viewport in its corridor.
    points = title["paper_title_points"]
    b = group["paper_viewport_bbox"]
    if relation == "below":
        corridor = [
            min(p[0] for p in points),
            max(p[1] for p in points),
            max(p[0] for p in points),
            b[1],
        ]
    else:
        corridor = [
            b[2],
            min(p[1] for p in points),
            min(p[0] for p in points),
            max(p[1] for p in points),
        ]
    return any(
        v[0] < corridor[2] and corridor[0] < v[2] and v[1] < corridor[3] and corridor[1] < v[3]
        for other in others
        if other["group_id"] != group["group_id"]
        for v in (m["paper_bbox"] for m in other["members"])
    )


def build_node_view_groups(sheets, native_entities, *, max_gap_ratio=0.06):
    """Build indexed native-frame groups; all uncertain associations stay REVIEW."""
    if not math.isfinite(max_gap_ratio) or not 0 < max_gap_ratio <= 0.1:
        raise ValueError("gap ratio must be positive and at most 0.1")
    native = sorted((e for e in native_entities if _visible(e)), key=lambda e: e.id)
    if len({e.id for e in native}) != len(native):
        raise ValueError("duplicate native entity identity")
    attrs, viewports, sheet_ids = defaultdict(list), {}, defaultdict(list)
    for s in sheets:
        sheet_ids[
            (
                s.source_file_id,
                "paper:" + (s.layout or "").split("#viewport:")[0],
                s.viewport_handle,
            )
        ].append(s.id)
    for e in native:
        parent = e.geometry.get("parent_insert_handle")
        if parent:
            attrs[(e.source_file_id, e.sheet_id, e.space, str(parent))].append(e)
        if e.entity_type == "VIEWPORT" and e.handle and _valid_box(e.bbox):
            if e.geometry.get("viewport_id", 2) <= 1 or e.geometry.get("status", 1) <= 0:
                continue
            key = e.source_file_id, e.space, e.handle
            if key in viewports:
                raise ValueError("duplicate viewport identity")
            viewports[key] = e
    frames = _native_page_frames(native, attrs, viewports)
    owners = defaultdict(list)
    for f in frames:
        e = f["entity"]
        for h in f["viewport_handles"]:
            owners[(e.source_file_id, e.space, h)].append(e.id)
    by_id = {e.id: e for e in native}
    titles = []
    for ref in extract_structured_reference_callouts(native):
        anchor = by_id[ref.entity_ids[0]]
        members = attrs[
            (anchor.source_file_id, anchor.sheet_id, anchor.space, ref.parent_insert_handle)
        ]
        if _view_title_kind(members) != "detail":
            continue
        pts = [by_id[i].insert for i in ref.entity_ids]
        if not all(p and len(p) == 2 and all(math.isfinite(x) for x in p) for p in pts):
            continue
        boxes = [e.bbox for e in members if _valid_box(e.bbox)]
        boxes.extend([[p[0], p[1], p[0], p[1]] for p in pts])
        titles.append(
            {
                "source_file_id": anchor.source_file_id,
                "space": anchor.space,
                "parent_handle": ref.parent_insert_handle,
                "view_number": ref.view_number,
                "back_reference": ref.code,
                "native_entity_ids": sorted(ref.entity_ids),
                "paper_title_points": [list(p) for p in pts],
                "paper_title_bbox": _union(boxes),
            }
        )
    groups, issues = [], []
    for f in sorted(frames, key=lambda f: f["entity"].id):
        frame = f["entity"]
        members = []
        for h in sorted(f["viewport_handles"]):
            key = frame.source_file_id, frame.space, h
            if len(owners[key]) != 1:
                issues.append(
                    {
                        "viewport_handle": h,
                        "source_file_id": frame.source_file_id,
                        "space": frame.space,
                        "reason": "MULTIPLE_PAGE_FRAMES",
                    }
                )
            else:
                members.append(viewports[key])
        edges = [
            edge
            for a, b in combinations(members, 2)
            if (edge := _fragment_edge(a, b, max_gap_ratio))
        ]
        neighbours = defaultdict(set)
        for edge in edges:
            a, b = edge["members"]
            neighbours[a].add(b)
            neighbours[b].add(a)
        remaining = {v.handle for v in members}
        frame_groups = []
        while remaining:
            pending, component = [min(remaining)], set()
            while pending:
                h = pending.pop()
                if h not in component:
                    component.add(h)
                    pending.extend(neighbours[h] - component)
            remaining -= component
            selected = [v for v in members if v.handle in component]
            identity = {
                "source_file_id": frame.source_file_id,
                "space": frame.space,
                "frame_handle": frame.handle,
                "viewport_handles": sorted(component),
            }
            group = {
                "group_id": _stable_id("node-view-group", identity),
                **identity,
                "page_code": f["code"],
                "paper_frame_bbox": list(frame.bbox),
                "paper_viewport_bbox": _union([v.bbox for v in selected]),
                "adjacency_edges": [e for e in edges if set(e["members"]) <= component],
                "members": [
                    {
                        "viewport_handle": v.handle,
                        "native_entity_id": v.id,
                        "paper_bbox": list(v.bbox),
                        "model_bbox_raw": list(v.geometry["model_bbox"])
                        if v.geometry.get("model_bbox") is not None
                        else None,
                        "scale": _projection(v)[0] if _projection(v) else None,
                        "sheet_ids": sorted(sheet_ids[(v.source_file_id, v.space, v.handle)]),
                    }
                    for v in selected
                ],
                "title_candidates": [],
                "selected_title": None,
                "state": "REVIEW",
                "physical_component_confirmed": False,
                "physical_quantity": None,
                "measurement_role": None,
            }
            frame_groups.append(group)
        frame_titles = [
            t
            for t in titles
            if (t["source_file_id"], t["space"]) == (frame.source_file_id, frame.space)
            and all(
                frame.bbox[0] <= p[0] <= frame.bbox[2] and frame.bbox[1] <= p[1] <= frame.bbox[3]
                for p in t["paper_title_points"]
            )
        ]
        competing = defaultdict(list)
        for g in frame_groups:
            for t in frame_titles:
                relation = _title_relation(g, t)
                if relation and not _blocked(g, t, frame_groups, relation):
                    candidate = {**t, "relation": relation}
                    g["title_candidates"].append(candidate)
                    competing[t["parent_handle"]].append(g["group_id"])
        for g in frame_groups:
            g["overlapping_group_ids"] = sorted(
                other["group_id"]
                for other in frame_groups
                if other is not g
                and any(
                    a[0] < b[2] and b[0] < a[2] and a[1] < b[3] and b[1] < a[3]
                    for a in (m["paper_bbox"] for m in g["members"])
                    for b in (m["paper_bbox"] for m in other["members"])
                )
            )
            for t in g["title_candidates"]:
                t["competing_group_ids"] = sorted(competing[t["parent_handle"]])
        assigned, assignment_diagnostics = _unique_caption_assignment(frame_groups)
        for g in frame_groups:
            g["caption_assignment"] = next(
                (d for d in assignment_diagnostics if g["group_id"] in d["group_ids"]), None
            )
            # A chain must be collinear, not a grid with accidental bridges.
            chain = len({e["axis"] for e in g["adjacency_edges"]}) <= 1
            unique = chain and g["group_id"] in assigned
            g["group_state"] = (
                "UNIQUE_TITLE_GROUP_CANDIDATE"
                if unique
                else "AMBIGUOUS_TITLE_GROUP"
                if g["title_candidates"]
                else "UNTITLED"
            )
            if unique:
                g["selected_title"] = next(
                    t
                    for t in g["title_candidates"]
                    if t["parent_handle"] == assigned[g["group_id"]]
                )
            # Crop is context in PAPER coordinates only, not measurement geometry.
            crop_titles = [g["selected_title"]] if unique else g["title_candidates"]
            boxes = [g["paper_viewport_bbox"]] + [t["paper_title_bbox"] for t in crop_titles]
            envelope = _union(boxes)
            g["paper_context_bbox"] = [
                max(envelope[0], frame.bbox[0]),
                max(envelope[1], frame.bbox[1]),
                min(envelope[2], frame.bbox[2]),
                min(envelope[3], frame.bbox[3]),
            ]
        groups.extend(frame_groups)
    groups.sort(key=lambda g: g["group_id"])
    return {
        "schema_version": "node-view-groups/1.0",
        "state": "REVIEW",
        "human_answers_used": False,
        "mutates_takeoff": False,
        "max_gap_ratio": max_gap_ratio,
        "groups": groups,
        "issues": issues,
        "summary": dict(Counter(g["group_state"] for g in groups)),
        "limitations": [
            "Aligned fragmented viewports are view candidates, not physical items.",
            "Every viewport retains independent projection and raw model bounds.",
            "Paper gaps and model gaps must not be added as unfolded dimensions.",
            "Only framed top-view, untwisted parallel fragments can be joined.",
            "Untitled, competing captions and unsupported layouts remain REVIEW.",
        ],
    }


def attach_node_group_routes(routes, catalogue):
    """Add a separate group ledger; preserve original reference/viewport results."""
    records = []
    for r in routes["records"]:
        candidates = []
        if r.get("reference_role") == "OUTGOING_CALLOUT":
            for g in catalogue["groups"]:
                if g["page_code"] != r["target_page"]:
                    continue
                for t in g["title_candidates"]:
                    if (
                        g["selected_title"]
                        and t["parent_handle"] != g["selected_title"]["parent_handle"]
                    ):
                        continue
                    if t["view_number"] == r["target_view"]:
                        candidates.append(
                            {
                                "group_id": g["group_id"],
                                "source_file_id": g["source_file_id"],
                                "frame_handle": g["frame_handle"],
                                "viewport_handles": g["viewport_handles"],
                                "title_parent_handle": t["parent_handle"],
                                "back_reference": t["back_reference"],
                                "reciprocal": t["back_reference"] == r.get("resolved_source_page"),
                                "group_state": g["group_state"],
                            }
                        )
        unique = (
            len(candidates) == 1
            and candidates[0]["reciprocal"]
            and candidates[0]["group_state"] == "UNIQUE_TITLE_GROUP_CANDIDATE"
        )
        state = (
            "UNIQUE_RECIPROCAL_GROUP_CANDIDATE"
            if unique
            else "GROUP_BACK_REFERENCE_CONFLICT"
            if candidates and not any(c["reciprocal"] for c in candidates)
            else "AMBIGUOUS_GROUP"
            if candidates
            else "NO_NUMBERED_GROUP"
        )
        records.append(
            {
                **r,
                "group_navigation_state": state,
                "suggested_detail_group_id": candidates[0]["group_id"] if unique else None,
                "detail_group_candidates": candidates,
            }
        )
    return {
        **routes,
        "records": records,
        "node_view_groups": catalogue,
        "group_navigation_summary": dict(Counter(r["group_navigation_state"] for r in records)),
    }


def other_group_title_handles(group, catalogue):
    """Explicit native annotation exclusions for isolated group context images."""
    own = group.get("selected_title")
    if own is None:
        raise ValueError("unambiguous selected title required for isolated group image")
    return sorted(
        {
            t["parent_handle"]
            for g in catalogue["groups"]
            if (g["source_file_id"], g["space"], g["frame_handle"])
            == (group["source_file_id"], group["space"], group["frame_handle"])
            for t in g["title_candidates"]
            if t["parent_handle"] != own["parent_handle"]
        }
    )
