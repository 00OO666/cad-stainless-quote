"""Read-only native viewport identity, without expected page/view answers.

VERIFIED means page and local-title identity only. It never proves a source
callout, physical object, material, dimension, count or commercial release.
Unsupported layouts keep partial field evidence and an overall REVIEW state.
"""

from __future__ import annotations

import hashlib
import math
import re
from pathlib import Path

import ezdxf
from ezdxf import bbox as ezbbox
from ezdxf.math import Vec3

from .cad_index import _clean_text, _entity_bbox, _record_entity
from .linking import extract_reference_codes
from .native_paper_render import native_viewport_region
from .native_reference_scope import NativeReferenceScope
from .panels import _STRONG_VIEW_TITLE_TAG_RE

_NUMBER = re.compile(r"\d{1,4}[A-Za-z]?\Z")
_ROLES = {
    "section": re.compile(r"\bSECTION\b|剖面|剖切", re.I),
    "elevation": re.compile(r"\bELEVATION\b|立面", re.I),
    "plan": re.compile(r"\bPLAN\b|\bRCP\b|平面|天花|顶面", re.I),
}
_DETAIL = re.compile(r"\bDETAIL\b|节点|大样|详图", re.I)
_TOLERANCE = 1e-7  # Paper-coordinate floating-point tolerance, not a search radius.
_LAYOUT_CAP = 100_000


def _role(texts):
    text = " ".join(texts)
    found = [role for role, pattern in _ROLES.items() if pattern.search(text)]
    if len(found) == 1:
        return found[0]
    if len(found) > 1:
        return "ambiguous"
    return "detail" if _DETAIL.search(text) else None


def _attribute(raw, scope, parent):
    point = raw.dxf.insert
    if (raw.dxf.get("halign", 0) or raw.dxf.get("valign", 0)) and raw.dxf.hasattr("align_point"):
        point = raw.dxf.align_point
    return {
        "handle": raw.dxf.handle,
        "parent_insert_handle": parent.dxf.handle,
        "text": _clean_text(raw) or "",
        "tag": raw.dxf.get("tag", ""),
        "insert": list(raw.dxf.insert),
        "anchor": list(point),
        "bbox": _entity_bbox(raw, scope.cache),
        "visibility_issue": scope._visible(raw, parent.dxf.layer),
    }


def _contains(outer, inner):
    return bool(
        outer
        and inner
        and outer[0] + _TOLERANCE < inner[0]
        and outer[1] + _TOLERANCE < inner[1]
        and inner[2] < outer[2] - _TOLERANCE
        and inner[3] < outer[3] - _TOLERANCE
    )


def _intersects_or_touches(outer, inner):
    return bool(
        outer
        and inner
        and min(outer[2], inner[2]) >= max(outer[0], inner[0]) - _TOLERANCE
        and min(outer[3], inner[3]) >= max(outer[1], inner[1]) - _TOLERANCE
    )


def _rule_geometry(parent, scope):
    """One actual visible circle/diameter extended into a horizontal caption rule."""
    issues, excluded, primitives = [], [], []
    if scope._visible(parent):
        issues.append(scope._visible(parent))
    if parent.mcount != 1 or parent.block() is None:
        return None, ["TITLE_MULTIINSERT_OR_BLOCK_MISSING"], excluded
    if parent.block().block.is_xref:
        return None, ["TITLE_EXTERNAL_REFERENCE"], excluded
    if parent.has_extension_dict and "ACAD_FILTER" in parent.extension_dict:
        issues.append("TITLE_CLIPPING_UNSUPPORTED")
    children = list(parent.block())
    if len(children) > 1000:
        return None, ["TITLE_ENTITY_CAP"], excluded
    for child in children:
        problem = scope._visible(child, parent.dxf.layer)
        if problem == "HIDDEN_NATIVE_ENTITY_OR_PARENT":
            excluded.append({"handle": child.dxf.handle, "reason": problem})
            continue
        if problem:
            issues.append(problem)
        if child.dxftype() not in {"ATTDEF", "TEXT", "MTEXT"}:
            primitives.append(child)
    circles = [p for p in primitives if p.dxftype() == "CIRCLE"]
    lines = [p for p in primitives if p.dxftype() == "LINE"]
    if len(primitives) != 2 or len(circles) != 1 or len(lines) != 1:
        return None, [*issues, "TITLE_NATIVE_CIRCLE_RULE_NOT_UNIQUE"], excluded
    circle, line = circles[0], lines[0]
    matrix = parent.matrix44()
    center = matrix.transform(circle.ocs().to_wcs(circle.dxf.center))
    xaxis = matrix.transform_direction(circle.ocs().to_wcs(Vec3(1, 0, 0)))
    yaxis = matrix.transform_direction(circle.ocs().to_wcs(Vec3(0, 1, 0)))
    start, end = matrix.transform(line.dxf.start), matrix.transform(line.dxf.end)
    radius = float(circle.dxf.radius) * xaxis.magnitude
    values = (*center, *xaxis, *yaxis, *start, *end, radius)
    if not all(math.isfinite(v) for v in values) or radius <= 0:
        return None, [*issues, "TITLE_NONFINITE_OR_SINGULAR_GEOMETRY"], excluded
    tol = max(radius, 1.0) * 1e-8
    if (
        abs(xaxis.magnitude - yaxis.magnitude) > tol
        or abs(xaxis.dot(yaxis)) > tol
        or abs(xaxis.z) > tol
        or abs(yaxis.z) > tol
        or any(abs(p.z - center.z) > tol or abs(p.y - center.y) > tol for p in (start, end))
    ):
        return None, [*issues, "TITLE_NONCIRCULAR_OR_NONHORIZONTAL_RULE"], excluded
    left, right = sorted((start.x - center.x, end.x - center.x))
    if (
        left > -radius + tol
        or right < radius - tol
        or not (abs(left + radius) <= tol or abs(right - radius) <= tol)
        or right - left <= 3 * radius
    ):
        return None, [*issues, "TITLE_RULE_NOT_EXTENDED_DIAMETER"], excluded
    return (
        {
            "basis": "visible_native_circle_and_extended_diameter_caption_rule",
            "circle_handle": circle.dxf.handle,
            "circle_center_wcs": list(center),
            "circle_radius_wcs": radius,
            "rule_handle": line.dxf.handle,
            "rule_start_wcs": list(start),
            "rule_end_wcs": list(end),
            "parent_insert_matrix": list(matrix),
            "circle_ocs_extrusion": list(circle.dxf.extrusion),
        },
        issues,
        excluded,
    )


def _title(parent, scope):
    attrs = [_attribute(a, scope, parent) for a in parent.attribs]
    captions = [a for a in attrs if _role([a["text"]])]
    numbers = [a for a in attrs if _NUMBER.fullmatch(a["text"].strip())]
    # A numeric reference marker alone is not a local drawing title.
    if not captions or not numbers:
        return None
    geometry, issues, excluded = _rule_geometry(parent, scope)
    bounds, bound_issues = scope._parent_bounds(parent.dxf.handle)
    issues.extend(bound_issues)
    issues.extend(a["visibility_issue"] for a in attrs if a["visibility_issue"])
    role = _role([a["text"] for a in captions])
    if role in {None, "ambiguous"}:
        issues.append("TITLE_ROLE_AMBIGUOUS_OR_MISSING")
    if len(numbers) != 1:
        issues.append("TITLE_LOCAL_NUMBER_AMBIGUOUS")
    backrefs = [
        a for a in attrs if extract_reference_codes(a["text"]) or a["text"].strip() in {"--", "-"}
    ]
    if len(backrefs) != 1 or len(extract_reference_codes(backrefs[0]["text"])) > 1:
        issues.append("TITLE_BACKREFERENCE_SLOT_AMBIGUOUS_OR_MISSING")
    if geometry:
        c, radius = geometry["circle_center_wcs"], geometry["circle_radius_wcs"]
        for a in numbers:
            p = a["anchor"]
            if not (abs(p[0] - c[0]) < radius and 0 < p[1] - c[1] < radius):
                issues.append("LOCAL_NUMBER_NOT_IN_NATIVE_UPPER_CIRCLE")
        for a in backrefs:
            p = a["anchor"]
            if not (abs(p[0] - c[0]) < radius and -radius < p[1] - c[1] < 0):
                issues.append("BACKREFERENCE_NOT_IN_NATIVE_LOWER_CIRCLE")
        left, right = sorted((geometry["rule_start_wcs"][0], geometry["rule_end_wcs"][0]))
        if not all(
            left - _TOLERANCE <= a["anchor"][0] <= right + _TOLERANCE
            and abs(a["anchor"][1] - c[1]) <= 2 * radius
            for a in captions
        ):
            issues.append("CAPTION_NOT_ON_SAME_NATIVE_TITLE_RULE")
    return {
        "parent_handle": parent.dxf.handle,
        "attributes": attrs,
        "native_bbox": bounds,
        "native_geometry": geometry,
        "excluded_hidden_geometry": excluded,
        "local_view_number": numbers[0]["text"] if len(numbers) == 1 else None,
        "backreference_page": (
            next(iter(extract_reference_codes(backrefs[0]["text"])), None)
            if len(backrefs) == 1
            else None
        ),
        "backreference_raw_text": backrefs[0]["text"] if len(backrefs) == 1 else None,
        "title_text": " | ".join(a["text"] for a in captions),
        "role": role,
        "reason_codes": sorted(set(issues)),
        "candidate_viewports": [],
        "blocked_viewports": [],
    }


def _title_owners(title, viewports, frame):
    """Unique column/caption-band ownership, with actual viewport occlusion.

    No nearest score is computed. An intervening visible viewport geometrically
    occludes an upper viewport's claim to a caption below the lower viewport.
    Every remaining eligible owner is retained; two owners never win by distance.
    """
    bounds = title["native_bbox"]
    if not bounds or not any(_contains(r["bbox"], bounds) for r in frame["rectangles"]):
        title["reason_codes"].append("TITLE_PARENT_OUTSIDE_PAGE_FRAME")
        return
    geometry = title["native_geometry"]
    if geometry:
        anchors = [geometry["rule_start_wcs"], geometry["rule_end_wcs"]]
        y = geometry["circle_center_wcs"][1]
    else:
        anchors = [a["anchor"] for a in title["attributes"]]
        y = sum(p[1] for p in anchors) / len(anchors)
    xmin, xmax = min(p[0] for p in anchors), max(p[0] for p in anchors)
    for vp in viewports:
        b = vp["paper_bbox"]
        height = b[3] - b[1]
        if not (
            b[0] - _TOLERANCE <= xmin <= xmax <= b[2] + _TOLERANCE
            and b[1] - height <= y <= b[1] + height * 0.12
        ):
            continue
        blockers = [
            other["handle"]
            for other in viewports
            if other["handle"] != vp["handle"]
            and min(xmax, other["paper_bbox"][2]) - max(xmin, other["paper_bbox"][0]) > _TOLERANCE
            and min(b[1], other["paper_bbox"][3]) - max(y, other["paper_bbox"][1]) > _TOLERANCE
        ]
        if blockers:
            title["blocked_viewports"].append(
                {"viewport": vp["handle"], "occluding_viewports": blockers}
            )
        else:
            title["candidate_viewports"].append(vp["handle"])


def _verify(document, layout, handle, result):
    try:
        paper = document.layouts.get(layout)
    except KeyError:
        result["reason_codes"].append("NATIVE_LAYOUT_MISSING")
        return
    viewport = document.entitydb.get(handle)
    if (
        not paper.is_any_paperspace
        or viewport is None
        or viewport.dxftype() != "VIEWPORT"
        or viewport.get_layout() != paper
    ):
        result["reason_codes"].append("NATIVE_VIEWPORT_OR_LAYOUT_MISMATCH")
        return
    if len(paper) > _LAYOUT_CAP:
        result["reason_codes"].append("NATIVE_LAYOUT_ENTITY_CAP")
        return
    native, cache = [], ezbbox.Cache()
    fid, space = "file:" + result["source_sha256"], "paper:" + layout
    parents = list(paper.query("INSERT"))
    for parent in parents:
        if not parent.attribs:
            continue
        native.append(_record_entity(parent, fid, space, space, cache))
        for attr in parent.attribs:
            native.append(
                _record_entity(
                    attr, fid, space, space, cache, parent_insert_handle=parent.dxf.handle
                )
            )
        if len(native) > _LAYOUT_CAP:
            result["reason_codes"].append("NATIVE_ATTRIBUTE_CAP")
            return
    scope = NativeReferenceScope(document, source_file_id=fid, native_entities=native)
    result["proof"]["frame_scan"] = scope.frames
    if scope._visible(viewport):
        result["reason_codes"].append(scope._visible(viewport))
        return
    try:
        projection = native_viewport_region(viewport, viewport.get_modelspace_limits())
    except (ValueError, TypeError, ArithmeticError) as exc:
        result["reason_codes"].append("NATIVE_VIEWPORT_UNSUPPORTED:" + str(exc))
        return
    result["proof"]["viewport"] = projection
    box = projection["paper_bbox"]
    owners, contacts = scope._owners(box, ((box[0] + box[2]) / 2, (box[1] + box[3]) / 2), space)
    result["proof"].update({"page_owners": owners, "page_contacts": contacts})
    if len(owners) != 1 or contacts:
        result["reason_codes"].append("NATIVE_PAGE_OWNER_MISSING_AMBIGUOUS_OR_BOUNDARY")
        return
    owner = owners[0]
    frame = next(
        f for f in scope.frames if f["frame_parent_handle"] == owner["frame_parent_handle"]
    )
    result["page_code"] = owner["page_code"]
    result["page_code_native_texts"] = [
        _clean_text(document.entitydb[e["handle"]]) for e in owner["page_number_evidence"]
    ]
    result["field_states"]["page_code"] = "VERIFIED"
    result["native_handles"]["page_parent"] = owner["frame_parent_handle"]
    result["native_handles"]["page_attributes"] = [
        e["handle"] for e in owner["page_number_evidence"]
    ]
    page_parent = document.entitydb[owner["frame_parent_handle"]]
    page_titles = [
        _attribute(a, scope, page_parent)
        for a in page_parent.attribs
        if _STRONG_VIEW_TITLE_TAG_RE.search(a.dxf.get("tag", "")) and _clean_text(a)
    ]
    result["proof"]["page_title_attributes"] = page_titles
    vps = []
    for other in paper.query("VIEWPORT"):
        if (
            int(other.dxf.id) <= 1
            or not other.is_visible
            or scope._visible(other) == "HIDDEN_NATIVE_ENTITY_OR_PARENT"
        ):
            continue
        c, w, h = other.dxf.center, other.dxf.width, other.dxf.height
        b = [c.x - w / 2, c.y - h / 2, c.x + w / 2, c.y + h / 2]
        if not all(math.isfinite(v) for v in b) or w <= 0 or h <= 0:
            # Its position cannot be excluded from this page. Do not turn an
            # unlocatable visible/unknown competitor into an empty inventory.
            result["reason_codes"].append("UNLOCATABLE_VISIBLE_VIEWPORT")
            continue
        contained = any(_contains(rect["bbox"], b) for rect in frame["rectangles"])
        if any(_intersects_or_touches(rect["bbox"], b) for rect in frame["rectangles"]):
            vps.append(
                {
                    "handle": other.dxf.handle,
                    "paper_bbox": b,
                    "visibility_issue": scope._visible(other),
                    # Crossing/contact is not proved page ownership. It must
                    # nevertheless remain in caption competition/occlusion;
                    # dropping it can give its caption to an upper viewport.
                    "frame_relation": "CONTAINED" if contained else "TOUCHING_OR_CROSSING",
                }
            )
    result["proof"]["same_page_viewports"] = vps
    titles = []
    for parent in parents:
        if parent.dxf.handle == page_parent.dxf.handle:
            continue
        title = _title(parent, scope)
        if title:
            _title_owners(title, vps, frame)
            # Keep raw candidates, including invalid/hidden candidates, in audit.
            titles.append(title)
    result["proof"]["local_title_candidates"] = titles
    eligible = [t for t in titles if handle in t["candidate_viewports"]]
    if len(eligible) != 1:
        result["reason_codes"].append("LOCAL_TITLE_MISSING_OR_MULTIPLE")
    elif eligible[0]["reason_codes"] or eligible[0]["candidate_viewports"] != [handle]:
        result["reason_codes"].extend(eligible[0]["reason_codes"])
        result["reason_codes"].append("LOCAL_TITLE_UNVERIFIED_OR_VIEWPORT_AMBIGUOUS")
    else:
        title = eligible[0]
        for name in ("local_view_number", "title_text", "role", "backreference_page"):
            result[name] = title[name]
            result["field_states"][name] = "VERIFIED"
        result["backreference_raw_text"] = title["backreference_raw_text"]
        result["native_handles"]["title_parent"] = title["parent_handle"]
        result["native_handles"]["title_attributes"] = [a["handle"] for a in title["attributes"]]
        result["proof"]["selected_title"] = title
    # Whole-page evidence can verify page title/role, never invent a local number
    # by copying the page suffix or a source direction callout from another file.
    if not eligible and len(vps) == 1 and vps[0]["handle"] == handle and page_titles:
        if all(not a["visibility_issue"] for a in page_titles):
            role = _role([a["text"] for a in page_titles])
            if role not in {None, "ambiguous"}:
                result["title_text"] = " | ".join(a["text"] for a in page_titles)
                result["role"] = role
                result["field_states"].update({"title_text": "VERIFIED", "role": "VERIFIED"})
                result["proof"]["title_basis"] = "same_parent_page_title_and_single_native_viewport"
    if result["expected_role"] and result["role"] != result["expected_role"]:
        result["reason_codes"].append("EXPECTED_ROLE_MISMATCH_OR_UNRESOLVED")


def verify_native_view_identity(source_path, *, layout, viewport_handle, expected_role=None):
    """Discover source-native identity; caller cannot supply expected page/local ID.

    ``expected_role`` validates the discovered role; it never selects candidates.
    Null local numbers remain REVIEW even for an otherwise verified single-page
    elevation. Consumers may use individually VERIFIED fields, not fill nulls.
    Source and all handles/coordinates remain private run diagnostics.
    """
    if expected_role not in {None, "plan", "elevation", "section", "detail"}:
        raise ValueError("expected_role must be plan, elevation, section, detail or None")
    path = Path(source_path).resolve()
    result = {
        "schema_version": "native-view-identity/1",
        "state": "REVIEW",
        "source_path": str(path),
        "source_sha256": None,
        "layout": layout,
        "viewport_handle": str(viewport_handle),
        "expected_role": expected_role,
        "page_code": None,
        "page_code_native_texts": [],
        "local_view_number": None,
        "title_text": None,
        "role": None,
        "backreference_page": None,
        "backreference_raw_text": None,
        "field_states": {
            name: "REVIEW"
            for name in (
                "page_code",
                "local_view_number",
                "title_text",
                "role",
                "backreference_page",
            )
        },
        "native_handles": {"viewport": str(viewport_handle)},
        "proof": {},
        "reason_codes": [],
        "limitations": [
            "Identity only; no component, callout, material, dimension or quantity proof.",
            "Supported native axis-aligned page rectangles and circle/rule local-title family.",
            "Frame primitive scans do not certify arbitrary proxy/OLE/Xref content.",
            "Caption ownership uses unique column/band and viewport occlusion; no nearest winner.",
        ],
    }
    try:
        result["source_sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
        document = ezdxf.readfile(path)
        _verify(document, layout, str(viewport_handle), result)
        if hashlib.sha256(path.read_bytes()).hexdigest() != result["source_sha256"]:
            result["reason_codes"].append("SOURCE_BYTES_CHANGED_DURING_REPLAY")
            result["field_states"] = dict.fromkeys(result["field_states"], "REVIEW")
    except (OSError, ValueError, TypeError, KeyError, ArithmeticError, ezdxf.DXFError) as exc:
        result["reason_codes"].append("SOURCE_REPLAY_FAILED:" + type(exc).__name__)
        result["field_states"] = dict.fromkeys(result["field_states"], "REVIEW")
    result["reason_codes"] = sorted(set(result["reason_codes"]))
    if not result["reason_codes"] and all(v == "VERIFIED" for v in result["field_states"].values()):
        result["state"] = "VERIFIED"
    return result
