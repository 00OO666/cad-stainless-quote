"""Prove native paper-page containment for unresolved reference annotations.

This is a negative-search scope aid, never component or target-view ownership.
Only a unique, independently replayed closed paper frame with its actual parent
page ATTRIB may exclude a reference from a different selected native page.
Same-page titles remain in scope; no nearest-title or insertion-point shortcut
is used to infer local-view ownership.
"""

from __future__ import annotations

import math

from ezdxf import bbox as ezbbox
from ezdxf.math import Vec2, Vec3, bulge_to_arc

from .cad_index import _clean_text, _entity_bbox
from .linking import extract_reference_codes
from .models import CadEntity
from .panels import _PAGE_NUMBER_TAG_RE, _paper_page_references
from .paper_frames import extract_insert_frame_geometry


def _dict(value):
    return value.model_dump(mode="python") if hasattr(value, "model_dump") else dict(value)


def _finite_box(box):
    return bool(
        box
        and len(box) == 4
        and all(math.isfinite(v) for v in box)
        and box[2] > box[0]
        and box[3] > box[1]
    )


def _inside(outer, inner, tolerance):
    return bool(
        _finite_box(outer)
        and _finite_box(inner)
        and outer[0] + tolerance < inner[0]
        and outer[1] + tolerance < inner[1]
        and inner[2] < outer[2] - tolerance
        and inner[3] < outer[3] - tolerance
    )


def _point_inside(box, point, tolerance):
    return bool(
        point
        and box[0] + tolerance < point[0] < box[2] - tolerance
        and box[1] + tolerance < point[1] < box[3] - tolerance
    )


def _overlap(a, b, tolerance):
    return bool(
        a
        and b
        and a[0] <= b[2] + tolerance
        and b[0] <= a[2] + tolerance
        and a[1] <= b[3] + tolerance
        and b[1] <= a[3] + tolerance
    )


def _same(left, right):
    return bool(
        left is not None
        and right is not None
        and len(left) == len(right)
        and all(
            math.isclose(a, b, rel_tol=1e-12, abs_tol=1e-7)
            for a, b in zip(left, right, strict=True)
        )
    )


class NativeReferenceScope:
    """Cache original native frames for one already source-replayed DXF.

    ``native_entities`` must be the original source index, not projected panel
    clones. Source-byte authentication remains the caller's source-replay job;
    this helper independently verifies selected handles, layouts, attached
    attributes, source text codes, positions and boxes against the document.
    """

    def __init__(self, document, *, source_file_id, native_entities, sheets=(), tolerance=1e-7):
        if not math.isfinite(tolerance) or tolerance <= 0:
            raise ValueError("tolerance must be finite and positive")
        self.document = document
        self.source_file_id = source_file_id
        self.tolerance = tolerance
        self.entities = [_dict(e) for e in native_entities]
        self.by_id = {
            e["id"]: e for e in self.entities if e.get("source_file_id") == source_file_id
        }
        self.by_handle = {e["handle"]: e for e in self.by_id.values() if e.get("handle")}
        self.sheets = [_dict(s) for s in sheets]
        self.cache = ezbbox.Cache()
        self.frames = []
        self.parent_bounds_cache = {}
        self.parent_bound_proofs = {}
        self.reference_cache = {}
        self.initial_issues = []
        if document is None or not source_file_id:
            self.initial_issues.append("SOURCE_DOCUMENT_OR_IDENTITY_MISSING")
        elif not self.by_id:
            self.initial_issues.append("SOURCE_NATIVE_INDEX_MISSING")
        else:
            self._build_frames()

    def _visible(self, raw, inherited=None):
        name = raw.dxf.get("layer", "0")
        if name == "0" and inherited:
            name = inherited
        try:
            layer = self.document.layers.get(name)
        except Exception:
            return "NATIVE_LAYER_VISIBILITY_UNKNOWN"
        if (
            raw.dxf.get("invisible", 0)
            or raw.dxftype() in {"ATTRIB", "ATTDEF"}
            and raw.dxf.get("flags", 0) & 1
            or layer.is_off()
            or layer.is_frozen()
        ):
            return "HIDDEN_NATIVE_ENTITY_OR_PARENT"
        return None

    def _validate_reference(self, entity):
        identifier = entity.get("id")
        indexed = self.by_id.get(identifier)
        if (
            indexed is None
            or any(
                entity.get(k) != indexed.get(k)
                for k in ("handle", "entity_type", "space", "source_file_id", "text")
            )
            or not _same(entity.get("bbox"), indexed.get("bbox"))
            or not _same(entity.get("insert"), indexed.get("insert"))
            or entity.get("geometry", {}).get("parent_insert_handle")
            != indexed.get("geometry", {}).get("parent_insert_handle")
        ):
            return ["REFERENCE_NOT_IDENTICAL_TO_SOURCE_NATIVE_INDEX"], {}
        if identifier in self.reference_cache:
            return self.reference_cache[identifier]
        issues, evidence = [], {}
        if entity.get("source_file_id") != self.source_file_id:
            return ["NATIVE_REFERENCE_SOURCE_MISMATCH"], evidence
        raw = self.document.entitydb.get(entity.get("handle"))
        if raw is None or not raw.is_alive or raw.dxftype() != entity.get("entity_type"):
            return ["NATIVE_REFERENCE_HANDLE_OR_TYPE_MISMATCH"], evidence
        parent = None
        if raw.dxftype() == "ATTRIB":
            handle = entity.get("geometry", {}).get("parent_insert_handle")
            parent = self.document.entitydb.get(handle)
            if (
                parent is None
                or parent.dxftype() != "INSERT"
                or not any(a is raw for a in parent.attribs)
            ):
                return ["REFERENCE_NOT_ACTUALLY_ATTACHED_TO_INDEXED_PARENT"], evidence
            layout = parent.get_layout()
        else:
            layout = raw.get_layout()
        if (
            layout is None
            or not layout.is_any_paperspace
            or entity.get("space") != "paper:" + layout.name
        ):
            issues.append("NATIVE_REFERENCE_LAYOUT_MISMATCH")
        for item, inherited in ((parent, None), (raw, parent.dxf.layer if parent else None)):
            if item is not None:
                problem = self._visible(item, inherited)
                if problem:
                    issues.append(problem)
        native_box = _entity_bbox(raw, self.cache)
        native_point = tuple(raw.dxf.insert)[:2] if raw.dxf.hasattr("insert") else None
        if not _finite_box(native_box) or not _same(native_box, entity.get("bbox")):
            issues.append("NATIVE_REFERENCE_BBOX_UNVERIFIED")
        if not _same(native_point, entity.get("insert")):
            issues.append("NATIVE_REFERENCE_POSITION_MISMATCH")
        if extract_reference_codes(_clean_text(raw)) != extract_reference_codes(entity.get("text")):
            issues.append("NATIVE_REFERENCE_TEXT_CODES_MISMATCH")
        evidence.update(
            {
                "entity_id": identifier,
                "handle": raw.dxf.handle,
                "entity_type": raw.dxftype(),
                "source_file_id": self.source_file_id,
                "native_space": entity.get("space"),
                "native_bbox": list(native_box) if native_box else None,
                "native_insert": list(native_point) if native_point else None,
                "parent_insert_handle": parent.dxf.handle if parent else None,
                "native_codes": sorted(extract_reference_codes(_clean_text(raw))),
            }
        )
        self.reference_cache[identifier] = (issues, evidence)
        return issues, evidence

    def _parent_bounds(self, handle):
        if handle in self.parent_bounds_cache:
            return self.parent_bounds_cache[handle]
        parent = self.document.entitydb.get(handle)
        issues, count, hatch_enclosures = [], 0, []
        supported = {
            "LINE",
            "ARC",
            "CIRCLE",
            "LWPOLYLINE",
            "POLYLINE",
            "SOLID",
            "TRACE",
            "TEXT",
            "MTEXT",
            "ATTRIB",
            "ATTDEF",
            "INSERT",
            "POINT",
            "HATCH",
        }

        def check(raw, stack=(), inherited=None, matrices=()):
            nonlocal count
            count += 1
            if count > 20_000 or len(stack) > 8:
                issues.append("REFERENCE_PARENT_GEOMETRY_CAP")
                return
            visibility = self._visible(raw, inherited)
            if visibility == "HIDDEN_NATIVE_ENTITY_OR_PARENT":
                return
            if visibility:
                issues.append(visibility)
                return
            kind = raw.dxftype()
            if kind not in supported:
                issues.append("REFERENCE_PARENT_UNSUPPORTED_GEOMETRY:" + kind)
            if kind == "HATCH":
                # A closed solid polyline hatch stays inside its true boundary.
                # Full-circle enclosures for bulged edges are conservative under
                # every affine INSERT/OCS transform; no tessellation underbounds.
                try:
                    points = []
                    if not raw.dxf.solid_fill or not raw.paths:
                        raise ValueError("unsupported hatch fill")
                    for path in raw.paths:
                        if (
                            not hasattr(path, "vertices")
                            or not path.is_closed
                            or not 3 <= len(path.vertices) <= 20_000
                        ):
                            raise ValueError("unsupported hatch boundary")
                        vertices = list(path.vertices)
                        if not all(math.isfinite(v) for p in vertices for v in p):
                            raise ValueError("nonfinite hatch boundary")
                        points.extend((p[0], p[1]) for p in vertices)
                        for start, end in zip(vertices, vertices[1:] + vertices[:1], strict=True):
                            if start[2]:
                                center, _, _, radius = bulge_to_arc(
                                    Vec2(start[:2]), Vec2(end[:2]), start[2]
                                )
                                if not math.isfinite(radius) or radius <= 0:
                                    raise ValueError("invalid hatch arc")
                                points.extend(
                                    (center.x + x * radius, center.y + y * radius)
                                    for x in (-1, 1)
                                    for y in (-1, 1)
                                )
                    world = []
                    for x, y in points:
                        point = raw.ocs().to_wcs(Vec3(x, y, raw.dxf.elevation.z))
                        for matrix in reversed(matrices):
                            point = matrix.transform(point)
                        if not all(math.isfinite(v) for v in point):
                            raise ValueError("nonfinite hatch transform")
                        world.append(point)
                    hatch_enclosures.append(
                        {
                            "handle": raw.dxf.handle,
                            "basis": (
                                "native_closed_solid_hatch_polyline_and_full_bulge_circle_enclosure"
                            ),
                            "native_paths": [list(p.vertices) for p in raw.paths],
                            "insert_matrices_outer_to_inner": [list(m) for m in matrices],
                            "ocs_extrusion": list(raw.dxf.extrusion),
                            "ocs_elevation": list(raw.dxf.elevation),
                            "bbox": [
                                min(p.x for p in world),
                                min(p.y for p in world),
                                max(p.x for p in world),
                                max(p.y for p in world),
                            ],
                        }
                    )
                except Exception:
                    issues.append("REFERENCE_PARENT_UNSUPPORTED_HATCH_BOUNDARY")
            if kind == "INSERT":
                if raw.dxf.name in stack or raw.mcount != 1:
                    issues.append("REFERENCE_PARENT_RECURSION_OR_MULTIINSERT")
                    return
                if raw.has_extension_dict and "ACAD_FILTER" in raw.extension_dict:
                    issues.append("REFERENCE_PARENT_CLIPPING_UNSUPPORTED")
                    return
                block = raw.block()
                if block is None or block.block.is_xref:
                    issues.append("REFERENCE_PARENT_XREF_OR_BLOCK_MISSING")
                    return
                for child in block:
                    check(
                        child,
                        (*stack, raw.dxf.name),
                        raw.dxf.layer if raw.dxf.layer != "0" else inherited,
                        (*matrices, raw.matrix44()),
                    )
                    if count > 20_000:
                        break

        if parent is None:
            result = None, ["REFERENCE_PARENT_MISSING"]
        else:
            check(parent)
            try:
                extents = ezbbox.extents([parent], fast=False)
                bounds = (
                    [extents.extmin.x, extents.extmin.y, extents.extmax.x, extents.extmax.y]
                    if extents.has_data
                    else None
                )
            except Exception:
                bounds = None
            if bounds is not None:
                for enclosure in hatch_enclosures:
                    box = enclosure["bbox"]
                    bounds = [
                        min(bounds[0], box[0]),
                        min(bounds[1], box[1]),
                        max(bounds[2], box[2]),
                        max(bounds[3], box[3]),
                    ]
            if not _finite_box(bounds):
                issues.append("REFERENCE_PARENT_NATIVE_BOUNDS_UNAVAILABLE")
            result = bounds, sorted(set(issues))
        self.parent_bounds_cache[handle] = result
        self.parent_bound_proofs[handle] = {"native_hatch_enclosures": hatch_enclosures}
        return result

    def _build_frames(self):
        native = [
            CadEntity.model_validate(e)
            for e in self.by_id.values()
            if e.get("space", "").startswith("paper:")
        ]
        groups = {}
        for page in _paper_page_references(native):
            groups.setdefault(page.parent_handle, set()).add(page.entity_id)
        # Keep hidden/empty explicit page slots visible in the frame audit.
        for entity in native:
            if entity.entity_type == "ATTRIB" and _PAGE_NUMBER_TAG_RE.fullmatch(
                str(entity.geometry.get("tag") or "")
            ):
                groups.setdefault(entity.geometry.get("parent_insert_handle"), set()).add(entity.id)
        for handle, identifiers in sorted(groups.items(), key=lambda item: str(item[0])):
            raw = self.document.entitydb.get(handle)
            if raw is None or raw.dxftype() != "INSERT" or raw.get_layout() is None:
                continue
            scan = extract_insert_frame_geometry(raw)
            # Fully traversed symbols without a native rectangle are not pages.
            if not scan["rectangles"] and scan["complete"] and not scan["unsupported_geometry"]:
                continue
            problems = list(scan["issues"])
            if self._visible(raw):
                problems.append(self._visible(raw))
            if raw.has_extension_dict and "ACAD_FILTER" in raw.extension_dict:
                problems.append("PAGE_FRAME_CLIPPING_UNSUPPORTED")
            records, codes = [], set()
            for identifier in sorted(identifiers):
                entity = self.by_id[identifier]
                failures, evidence = self._validate_reference(entity)
                problems.extend(failures)
                records.append(evidence)
                codes.update(evidence.get("native_codes", []))
                if not evidence.get("native_codes"):
                    problems.append("EMPTY_OR_UNPARSEABLE_NATIVE_PAGE_NUMBER")
            if len(codes) != 1:
                problems.append("CONFLICTING_OR_MISSING_NATIVE_PAGE_NUMBER")
            if any(
                not any(
                    _inside(r["bbox"], e.get("native_bbox"), self.tolerance)
                    and _point_inside(r["bbox"], e.get("native_insert"), self.tolerance)
                    for r in scan["rectangles"]
                )
                for e in records
            ):
                problems.append("PAGE_NUMBER_OUTSIDE_NATIVE_PARENT_FRAME")
            for rectangle in scan["rectangles"]:
                for chain in rectangle.get("source_handle_chains", []):
                    for ancestor_handle in chain:
                        ancestor = self.document.entitydb.get(ancestor_handle)
                        if (
                            ancestor is not None
                            and ancestor.dxftype() == "INSERT"
                            and ancestor.has_extension_dict
                            and "ACAD_FILTER" in ancestor.extension_dict
                        ):
                            problems.append("PAGE_FRAME_CLIPPING_UNSUPPORTED")
            try:
                extents = ezbbox.extents([raw], fast=False)
                native_extent = (
                    [extents.extmin.x, extents.extmin.y, extents.extmax.x, extents.extmax.y]
                    if extents.has_data
                    else None
                )
            except Exception:
                native_extent = None
            self.frames.append(
                {
                    "frame_parent_handle": handle,
                    "native_space": "paper:" + raw.get_layout().name,
                    "source_file_id": self.source_file_id,
                    "page_code": next(iter(codes)) if len(codes) == 1 else None,
                    "page_number_evidence": records,
                    "frame_matrix": list(raw.matrix44()),
                    "frame_scan": scan,
                    "rectangles": scan["rectangles"],
                    "native_frame_extent": native_extent,
                    "reason_codes": sorted(set(problems)),
                    "usable": bool(scan["complete"] and scan["rectangles"] and not problems),
                }
            )

    def _owners(self, box, point, space, parent_handle=None):
        owners, contacts = [], []
        for frame in self.frames:
            if frame["native_space"] != space:
                continue
            matched = [
                r
                for r in frame["rectangles"]
                if _inside(r["bbox"], box, self.tolerance)
                and _point_inside(r["bbox"], point, self.tolerance)
            ]
            touched = [r for r in frame["rectangles"] if _overlap(r["bbox"], box, self.tolerance)]
            if not frame["usable"] and (
                not frame["native_frame_extent"]
                or _overlap(frame["native_frame_extent"], box, self.tolerance)
            ):
                contacts.append(
                    {
                        "frame_parent_handle": frame["frame_parent_handle"],
                        "reason_codes": frame["reason_codes"] or ["NATIVE_FRAME_UNSUPPORTED"],
                        "native_frame_extent": frame["native_frame_extent"],
                    }
                )
                continue
            if not touched:
                continue
            if not frame["usable"]:
                contacts.append(
                    {
                        "frame_parent_handle": frame["frame_parent_handle"],
                        "reason_codes": frame["reason_codes"] or ["NATIVE_FRAME_UNSUPPORTED"],
                    }
                )
                continue
            if not matched:
                contacts.append(
                    {
                        "frame_parent_handle": frame["frame_parent_handle"],
                        "reason_codes": ["REFERENCE_TOUCHES_OR_CROSSES_NATIVE_FRAME_BOUNDARY"],
                    }
                )
                continue
            parent_box = None
            if parent_handle and parent_handle != frame["frame_parent_handle"]:
                parent_box, problems = self._parent_bounds(parent_handle)
                if problems or not any(
                    _inside(r["bbox"], parent_box, self.tolerance) for r in matched
                ):
                    contacts.append(
                        {
                            "frame_parent_handle": frame["frame_parent_handle"],
                            "reason_codes": problems or ["REFERENCE_PARENT_CROSSES_NATIVE_FRAME"],
                            "native_parent_bbox": parent_box,
                        }
                    )
                    continue
            owners.append(
                {
                    "frame_parent_handle": frame["frame_parent_handle"],
                    "page_code": frame["page_code"],
                    "source_file_id": self.source_file_id,
                    "native_space": space,
                    "page_number_evidence": frame["page_number_evidence"],
                    "containing_rectangles": matched,
                    "frame_matrix": frame["frame_matrix"],
                    "native_parent_bbox": parent_box,
                    "native_parent_bound_evidence": self.parent_bound_proofs.get(parent_handle),
                }
            )
        return owners, contacts

    def classify(self, reference, selected_sheet):
        """Return a retained audit row; only ``exclude_from_selected_scope`` releases scope."""
        entity, selected = _dict(reference), _dict(selected_sheet)
        out = {
            "schema_version": "native-reference-scope/1",
            "state": "REVIEW",
            "classification": "UNRESOLVED_NATIVE_REFERENCE_SCOPE",
            "exclude_from_selected_scope": False,
            "source_file_id": self.source_file_id,
            "entity_id": entity.get("id"),
            "handle": entity.get("handle"),
            "selected_sheet_id": selected.get("id"),
            "reference_evidence": {},
            "reference_page_owners": [],
            "selected_page_owners": [],
            "contacts": [],
            "reason_codes": list(self.initial_issues),
            "proves_component_or_target_view_ownership": False,
        }
        if out["reason_codes"]:
            return out
        if (
            selected.get("source_file_id") != self.source_file_id
            or entity.get("source_file_id") != self.source_file_id
        ):
            out["reason_codes"].append("SOURCE_SCOPE_MISMATCH")
            return out
        problems, evidence = self._validate_reference(entity)
        out["reference_evidence"] = evidence
        out["reason_codes"].extend(problems)
        if problems:
            return out
        viewport = self.document.entitydb.get(selected.get("viewport_handle"))
        if (
            viewport is None
            or viewport.dxftype() != "VIEWPORT"
            or viewport.get_layout() is None
            or not viewport.is_visible
            or int(viewport.dxf.id) <= 1
        ):
            out["reason_codes"].append("SELECTED_NATIVE_VIEWPORT_MISSING_OR_INACTIVE")
            return out
        native_space = "paper:" + viewport.get_layout().name
        if native_space != evidence["native_space"]:
            out["reason_codes"].append("REFERENCE_AND_SELECTED_VIEWPORT_LAYOUT_MISMATCH")
            return out
        center = viewport.dxf.center
        viewport_box = [
            center.x - viewport.dxf.width / 2,
            center.y - viewport.dxf.height / 2,
            center.x + viewport.dxf.width / 2,
            center.y + viewport.dxf.height / 2,
        ]
        selected_owners, selected_contacts = self._owners(
            viewport_box, (center.x, center.y), native_space
        )
        owners, contacts = self._owners(
            evidence["native_bbox"],
            evidence["native_insert"],
            native_space,
            evidence["parent_insert_handle"],
        )
        out["reference_page_owners"], out["selected_page_owners"] = owners, selected_owners
        out["contacts"] = [
            *contacts,
            *[{**c, "scope": "selected_viewport"} for c in selected_contacts],
        ]
        if len(selected_owners) != 1 or selected_contacts:
            out["reason_codes"].append("SELECTED_VIEWPORT_PAGE_FRAME_AMBIGUOUS_OR_UNVERIFIED")
        if len(owners) != 1 or contacts:
            out["reason_codes"].append("REFERENCE_PAGE_FRAME_AMBIGUOUS_BOUNDARY_OR_UNVERIFIED")
        if out["reason_codes"]:
            return out
        expected_pages = {
            item.split(":", 1)[1].split("@", 1)[0]
            for item in selected.get("evidence", [])
            if item.startswith("paper_page_reference:")
        }
        expected_ids = {
            item.rsplit("@", 1)[-1]
            for item in selected.get("evidence", [])
            if item.startswith("paper_page_reference:")
        }
        actual_selected = selected_owners[0]
        actual_ids = {e["entity_id"] for e in actual_selected["page_number_evidence"]}
        if (
            expected_pages != {actual_selected["page_code"]}
            or not expected_ids
            or not expected_ids.issubset(actual_ids)
        ):
            out["reason_codes"].append("SELECTED_REPLAYED_PAGE_IDENTITY_MISMATCH")
            return out
        owner = owners[0]
        if owner["frame_parent_handle"] == actual_selected["frame_parent_handle"]:
            out["classification"] = "CURRENT_NATIVE_PAGE_RETAINED"
            out["reason_codes"].append("PAGE_CONTAINMENT_DOES_NOT_ESTABLISH_LOCAL_VIEW_OWNERSHIP")
        elif owner["page_code"] == actual_selected["page_code"]:
            out["reason_codes"].append("DUPLICATE_NATIVE_PAGE_NUMBER_ACROSS_FRAMES")
        else:
            out["classification"] = "OTHER_NATIVE_PAGE"
            out["exclude_from_selected_scope"] = True
        return out


__all__ = ["NativeReferenceScope"]
