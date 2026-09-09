"""Reproduce evaluation view facts from CAD and target-free semantic selections.

This is a bounded, explicitly reviewed producer, not an automatic takeoff or an
authentication service. Source bytes are reindexed and selected-source panels
are replayed before any positive fact is emitted. A receipt is reproducible but
is not a signature: consumers should rerun ``verify_view_context_receipt``.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
from collections import defaultdict
from pathlib import Path
from typing import Literal

import ezdxf
from ezdxf import bbox as ezbbox
from pydantic import Field, field_validator

from . import native_cut_paths
from .cad_index import index_dxf
from .io import sha256_file, write_json_atomic
from .linking import extract_reference_codes, extract_structured_reference_callouts
from .models import CadEntity, NonEmptyString, Sheet, StrictModel, ViewDispositionReview
from .panels import expand_viewport_panels
from .view_dispositions import ViewDispositionContext

PRODUCER = "cad-view-context-builder/1"


class ViewContextBuildError(ValueError):
    """A source/snapshot/selection validation failure; no context was issued."""

    def __init__(self, message, *, diagnostic=None):
        super().__init__(message)
        self.diagnostic = diagnostic


class SelectedView(StrictModel):
    id: NonEmptyString
    role: Literal["plan", "section", "elevation"]
    sheet_id: NonEmptyString
    object_bbox: tuple[float, float, float, float]
    evidence_ids: list[NonEmptyString] = Field(min_length=1)
    binding_entity_ids: list[NonEmptyString] = Field(default_factory=list)
    title_reference_ids: list[NonEmptyString] = Field(default_factory=list)
    native_object_handles: list[NonEmptyString] = Field(default_factory=list)

    @field_validator("object_bbox")
    @classmethod
    def valid_box(cls, value):
        if not all(math.isfinite(v) for v in value) or value[2] <= value[0] or value[3] <= value[1]:
            raise ValueError("object_bbox must be finite and nondegenerate")
        return value


class NegativeSearchSelection(StrictModel):
    # Deliberately no caller-authored complete/unresolved/conflict flags.
    searched_sheet_ids: list[NonEmptyString] = Field(min_length=1)


class ComponentSelection(StrictModel):
    component_id: NonEmptyString
    basis_kind: Literal["plan_section", "plan_elevation_without_detail"]
    basis: NonEmptyString
    review: ViewDispositionReview
    views: list[SelectedView] = Field(min_length=2, max_length=2)
    source_reference_ids: list[NonEmptyString] = Field(min_length=2)
    target_reference_ids: list[NonEmptyString] = Field(min_length=2)
    negative_search: NegativeSearchSelection


class ContextSelections(StrictModel):
    schema_version: Literal["cad-view-context-selections/1"]
    side: Literal["predicted", "gold"]
    components: list[ComponentSelection] = Field(min_length=1)


def _canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _hash(value):
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def _id(kind, value):
    return f"{kind}:{_hash(value)[:24]}"


def _read(path):
    return json.loads(Path(path).read_text(encoding="utf-8-sig"))


def _require(condition, message):
    if not condition:
        raise ViewContextBuildError(message)


def _unique(records, label):
    result = {}
    for record in records:
        identifier = record.id if hasattr(record, "id") else record["id"]
        _require(identifier not in result, f"duplicate {label} identity: {identifier}")
        result[identifier] = record
    return result


def _same(left, right):
    """Numeric tolerances only absorb float serialization, never geometric drift."""
    if isinstance(left, (float, int)) and isinstance(right, (float, int)):
        return math.isclose(left, right, rel_tol=1e-12, abs_tol=1e-8)
    if isinstance(left, (tuple, list)) and isinstance(right, (tuple, list)):
        return len(left) == len(right) and all(
            _same(a, b) for a, b in zip(left, right, strict=True)
        )
    if isinstance(left, dict) and isinstance(right, dict):
        return left.keys() == right.keys() and all(_same(left[k], right[k]) for k in left)
    return left == right


def _contains(box, point):
    # Match _same's serialization tolerance, never a CAD-distance allowance.
    epsilon = 1e-8
    return bool(
        box is not None
        and point is not None
        and box[0] - epsilon <= point[0] <= box[2] + epsilon
        and box[1] - epsilon <= point[1] <= box[3] + epsilon
    )


def _within(outer, inner):
    return bool(inner and _contains(outer, inner[:2]) and _contains(outer, inner[2:]))


def _native_id(entity):
    return str(entity.geometry.get("original_entity_id") or entity.id)


def _core(entity):
    return {
        key: getattr(entity, key)
        for key in (
            "id",
            "source_file_id",
            "sheet_id",
            "handle",
            "entity_type",
            "space",
            "text",
            "text_override",
            "value",
            "insert",
            "bbox",
            "layer",
        )
    }


def _source_replay(index, panels, selected_sources):
    """Reindex immutable bytes; compare native facts and replay panel membership."""
    source_records = index.get("sources")
    _require(isinstance(source_records, list) and source_records, "missing index sources")
    sources = {}
    all_native = []
    for source in source_records:
        fid = source.get("source_file_id")
        _require(fid and fid not in sources, "duplicate or missing source identity")
        sources[fid] = source
        entities = [CadEntity.model_validate(e) for e in source.get("entities", [])]
        _require(
            all(e.source_file_id == fid for e in entities), "index entity source-scope mismatch"
        )
        all_native.extend(entities)
    _unique(all_native, "native entity")
    _require(selected_sources.issubset(sources), "selected source absent from index")
    native, sheets, source_receipts, scan_issues = [], [], [], []
    for fid in sorted(selected_sources):
        source = sources[fid]
        path = Path(source["source_path"]).resolve()
        _require(path.is_file() and path.suffix.lower() == ".dxf", "indexed DXF unavailable")
        actual_hash = sha256_file(path)
        _require(actual_hash == source.get("source_sha256"), "source hash mismatch")
        # The deterministic source identity embeds the original content digest.
        _require(fid.startswith(f"file:{actual_hash}"), "source identity/hash mismatch")
        fresh = index_dxf(path, source_file_id=fid)
        old_entities = _unique([CadEntity.model_validate(e) for e in source["entities"]], "entity")
        fresh_entities = _unique(fresh.entities, "reindexed entity")
        _require(
            old_entities.keys() == fresh_entities.keys(), "index entity inventory differs from CAD"
        )
        for eid, old in old_entities.items():
            current = fresh_entities[eid]
            _require(_same(_core(old), _core(current)), f"index entity differs from CAD: {eid}")
            # Enrichers may ADD diagnostics, but cannot rewrite native geometry.
            _require(
                all(
                    key in old.geometry and _same(value, old.geometry[key])
                    for key, value in current.geometry.items()
                ),
                f"index native geometry differs from CAD: {eid}",
            )
            for key in ("semantic_hidden", "original_entity_id", "original_space"):
                _require(
                    old.geometry.get(key) == current.geometry.get(key),
                    f"index changed native visibility/identity: {eid}",
                )
        old_sheets = _unique([Sheet.model_validate(s) for s in source["sheets"]], "native sheet")
        _require(
            set(old_sheets) == {s.id for s in fresh.sheets},
            "index sheet inventory differs from CAD",
        )
        for sheet in fresh.sheets:
            old = old_sheets[sheet.id]
            _require(
                old.source_file_id == fid
                and old.layout == sheet.layout
                and _same(old.bbox, sheet.bbox),
                "index sheet source/layout/bbox mismatch",
            )
        # Enrichment may add metadata, but it must never become executable
        # parent/leader/visibility/viewport evidence. All downstream facts and
        # panel replay use the fresh CAD records, including absent geometry keys.
        native.extend(fresh_entities.values())
        sheets.extend(fresh.sheets)
        for label, value in (("source", source), ("fresh", fresh.to_dict())):
            if value.get("block_expansion_truncated") or value.get("recovered"):
                scan_issues.append(f"{fid}:{label}:truncated_or_recovered")
            if value.get("audit_error_count", 0):
                scan_issues.append(f"{fid}:{label}:audit_errors")
            # Encoding repair is fully audited; every other indexing warning
            # conservatively makes a negative search incomplete.
            scan_issues.extend(
                f"{fid}:{label}:{w}"
                for w in value.get("warnings", [])
                if not str(w).startswith("raw UTF-8 text recovery applied")
            )
        source_receipts.append(
            {
                "source_file_id": fid,
                "source_path": str(path),
                "source_sha256": actual_hash,
                "native_entity_count": len(old_entities),
            }
        )
    expansion = expand_viewport_panels(
        sheets,
        native,
        source_names={fid: Path(sources[fid]["source_path"]).name for fid in selected_sources},
    )
    expected_entities = _unique(expansion.entities, "replayed panel entity")
    expected_sheets = _unique(expansion.sheets, "replayed panel sheet")
    provided_entities = _unique(
        [CadEntity.model_validate(e) for e in panels["entities"]], "panel entity"
    )
    provided_sheets = _unique([Sheet.model_validate(s) for s in panels["sheets"]], "panel sheet")
    actual_entities = {
        k: e for k, e in provided_entities.items() if e.source_file_id in selected_sources
    }
    actual_sheets = {
        k: s for k, s in provided_sheets.items() if s.source_file_id in selected_sources
    }
    _require(
        actual_entities.keys() == expected_entities.keys(),
        "panels entity inventory differs from replay",
    )
    _require(
        actual_sheets.keys() == expected_sheets.keys(), "panels sheet inventory differs from replay"
    )
    for eid, entity in actual_entities.items():
        _require(
            _same(entity.model_dump(mode="json"), expected_entities[eid].model_dump(mode="json")),
            f"panel entity differs from source replay: {eid}",
        )
    for sid, sheet in actual_sheets.items():
        _require(
            _same(sheet.model_dump(mode="json"), expected_sheets[sid].model_dump(mode="json")),
            f"panel sheet differs from source replay: {sid}",
        )
    # Keep replay diagnostics in the receipt. Unassigned annotations on another
    # viewport do not alone invalidate a scoped search; source scan failures do.
    return native, actual_sheets, actual_entities, source_receipts, scan_issues, expansion.warnings


def _native_object_visibility(
    document, original, viewport_frozen, *, max_entities=10000, max_depth=16
):
    """Reject a selected block whose full bbox includes hidden/unknown children.

    This deliberately does not silently substitute a visible subassembly for
    the selected INSERT. A later selection may explicitly identify that part.
    """
    visited = 0
    leaves = 0

    def walk(entity, inherited_layer=None, stack=()):
        nonlocal visited, leaves
        visited += 1
        _require(visited <= max_entities and len(stack) <= max_depth,
                 "native object visibility scan incomplete: limit")
        layer_name = str(entity.dxf.get("layer", "0"))
        if layer_name == "0" and inherited_layer is not None:
            layer_name = inherited_layer
        try:
            layer = document.layers.get(layer_name)
        except Exception as exc:
            raise ViewContextBuildError("native object visibility scan: missing layer") from exc
        _require(
            not entity.dxf.get("invisible", 0)
            and not layer.is_off() and not layer.is_frozen()
            and layer_name.casefold() not in viewport_frozen,
            "hidden native object descendant cannot supply full-block geometry",
        )
        if entity.dxftype() == "INSERT":
            name = str(entity.dxf.name)
            _require(name.casefold() not in stack, "native object visibility scan: block cycle")
            try:
                block = document.blocks.get(name)
            except Exception as exc:
                raise ViewContextBuildError("native object visibility scan: missing block") from exc
            _require(block is not None, "native object visibility scan: missing block")
            _require(not block.block.dxf.get("flags", 0) & 12,
                     "native object visibility scan: external block")
            for child in block:
                if child.dxftype() != "ATTDEF":
                    walk(child, layer_name, (*stack, name.casefold()))
        else:
            leaves += 1

    walk(original)
    _require(leaves > 0, "native object visibility scan: no visible content")
    return {"complete": True, "visited_entities": visited, "visible_leaf_entities": leaves,
            "basis": "native_instance_layer_inheritance_and_selected_viewport_visibility"}


class _Scope:
    def __init__(self, view, sheets, entities, native, source_documents):
        self.view = view
        self.sheet = sheets[view.sheet_id]
        self.native = native
        self.document = source_documents[self.sheet.source_file_id]
        self.viewport = self.document.entitydb.get(self.sheet.viewport_handle)
        _require(
            self.viewport is not None and self.viewport.dxftype() == "VIEWPORT",
            "selected native viewport missing",
        )
        self.viewport_frozen_layers = {
            str(name).casefold() for name in self.viewport.frozen_layers
        }
        self.visibility_cache = {}
        self.label_link_cache = {}
        self.all_panel_entities = entities
        self.native_by_handle = {
            (e.space, e.handle): e
            for e in native.values()
            if e.source_file_id == self.sheet.source_file_id and e.handle
        }
        _require(
            _within(self.sheet.bbox, view.object_bbox), "component bbox outside selected sheet"
        )
        _require(
            not _same(self.sheet.bbox, view.object_bbox), "whole-sheet bbox is not a component bbox"
        )
        self.entities = [e for e in entities.values() if e.sheet_id == view.sheet_id]
        self.originals = defaultdict(list)
        for entity in self.entities:
            self.originals[_native_id(entity)].append(entity)
        self.by_id = {e.id: e for e in self.entities}
        self.sheets = sheets
        self.title_ids = set(view.title_reference_ids)
        self.anchors = [self.resolve(e) for e in view.binding_entity_ids]
        self.binding_audit = {}
        self.cut_path_cache = {}
        self.reference_pairs_by_parent = defaultdict(list)
        source_entities = [
            e for e in native.values() if e.source_file_id == self.sheet.source_file_id
        ]
        for pair in extract_structured_reference_callouts(source_entities):
            self.reference_pairs_by_parent[pair.parent_insert_handle].append(pair)
        self.native_objects = []
        if view.native_object_handles:
            _require(
                len(set(view.native_object_handles)) == len(view.native_object_handles),
                "duplicate native object handle",
            )
            doc = self.document
            for handle in view.native_object_handles:
                original = doc.entitydb.get(handle)
                _require(
                    original is not None and original.is_alive,
                    "native object handle absent from source",
                )
                layout = original.get_layout()
                _require(
                    layout is not None and layout.name.lower() == "model",
                    "native object must be a top-level model-space entity",
                )
                _require(
                    original.dxftype()
                    in {"LINE", "LWPOLYLINE", "POLYLINE", "ARC", "CIRCLE", "INSERT"},
                    "unsupported native object geometry",
                )
                layer = doc.layers.get(original.dxf.layer)
                _require(
                    not original.dxf.get("invisible", 0)
                    and not layer.is_off()
                    and not layer.is_frozen()
                    and str(original.dxf.layer).casefold() not in self.viewport_frozen_layers,
                    "hidden native object cannot bind a component",
                )
                visibility_proof = _native_object_visibility(
                    doc, original, self.viewport_frozen_layers
                )
                extents = ezbbox.extents([original], fast=False)
                _require(extents.has_data, "native object has no geometric extent")
                box = (extents.extmin.x, extents.extmin.y, extents.extmax.x, extents.extmax.y)
                _require(_within(view.object_bbox, box), "native object outside component bbox")
                identity = _id(
                    "native-object",
                    {"source": self.sheet.source_file_id, "space": "model", "handle": handle},
                )
                self.native_objects.append(
                    {
                        "id": identity,
                        "source_id": self.sheet.source_file_id,
                        "sheet_id": self.sheet.id,
                        "entity_type": original.dxftype(),
                        "handle": handle,
                        "bbox": box,
                        "binding_basis": "original_DXF_model_geometry_in_reviewed_bbox",
                        "native_instance_visibility": visibility_proof,
                    }
                )

    def resolve(self, identifier):
        if identifier in self.by_id:
            return self.by_id[identifier]
        matches = self.originals.get(identifier, [])
        if not matches and identifier in self.title_ids:
            original = self.native.get(identifier)
            _require(
                original is not None and original.source_file_id == self.sheet.source_file_id,
                "native title source mismatch",
            )
            _require(
                original.space.startswith("paper:") and original.insert is not None,
                "native title must be positioned paper annotation",
            )
            eligible = []
            for sheet in self.sheets.values():
                if sheet.source_file_id != original.source_file_id:
                    continue
                # A vertically near title on a different native page is not
                # proof of this viewport. The page parent's separate title
                # strip is permitted as well as its drawing rectangle.
                page_entities = [
                    self.native.get(value.rsplit("@", 1)[-1])
                    for value in sheet.evidence
                    if value.startswith("paper_page_reference:")
                ]
                parents = {e.geometry.get("parent_insert_handle") for e in page_entities if e}
                frames = [
                    e
                    for e in self.native.values()
                    if e.source_file_id == original.source_file_id
                    and e.space == original.space
                    and e.handle in parents
                ]
                if not any(
                    frame.geometry.get("paper_frame_scan", {}).get("complete")
                    and any(
                        _contains(rect.get("bbox"), original.insert)
                        for rect in frame.geometry["paper_frame_scan"].get("rectangles", [])
                    )
                    for frame in frames
                ):
                    continue
                viewport = next(
                    (
                        e
                        for e in self.native.values()
                        if e.source_file_id == original.source_file_id
                        and e.entity_type == "VIEWPORT"
                        and e.handle == sheet.viewport_handle
                        and e.space == original.space
                    ),
                    None,
                )
                if viewport is None or not viewport.bbox:
                    continue
                box = viewport.bbox
                point = original.insert
                if (
                    box[0] <= point[0] <= box[2]
                    and box[1] - (box[3] - box[1]) <= point[1] <= box[1]
                ):
                    eligible.append((box[1] - point[1], sheet.id, viewport))
            _require(eligible, "native title has no same-layout viewport association")
            nearest = min(record[0] for record in eligible)
            matches = [record for record in eligible if abs(record[0] - nearest) <= 1e-8]
            _require(
                len(matches) == 1 and matches[0][1] == self.sheet.id,
                "native title viewport association ambiguous or wrong sheet",
            )
            viewport = matches[0][2]
            # Native titles omitted by clipping stay native-coordinate metadata;
            # their mapping to this selected panel is explicitly recorded.
            mapped = original.model_copy(
                update={
                    "sheet_id": self.sheet.id,
                    "geometry": {
                        **original.geometry,
                        "original_entity_id": original.id,
                        "native_title_viewport_handle": viewport.handle,
                        "native_title_association": "unique_nearest_same_layout_vertical_column",
                    },
                }
            )
            self.by_id[identifier] = mapped
            self.originals[identifier] = [mapped]
            return mapped
        _require(
            len(matches) == 1, f"entity absent/ambiguous on selected source and sheet: {identifier}"
        )
        return matches[0]

    def original(self, identifier):
        entity = self.resolve(identifier)
        return self.native[_native_id(entity)]

    def visible(self, entity):
        """Read native flags and every indexed parent, including layer 0 inheritance."""
        identifier = _native_id(entity)
        if identifier in self.visibility_cache:
            return self.visibility_cache[identifier]
        original = self.native[identifier]
        records, visited = [original], set()
        valid = True
        while records:
            record = records.pop()
            if record.id in visited:
                valid = False
                break
            visited.add(record.id)
            handle = record.handle or record.geometry.get("source_block_entity_handle")
            raw = self.document.entitydb.get(handle) if handle else None
            if raw is None or not raw.is_alive or record.geometry.get("semantic_hidden"):
                valid = False
                break
            layer = self.document.layers.get(raw.dxf.layer)
            if (
                raw.dxf.get("invisible", 0)
                or raw.dxftype() in {"ATTRIB", "ATTDEF"}
                and raw.dxf.get("flags", 0) & 1
                or layer.is_off()
                or layer.is_frozen()
                or (
                    original.space == "model"
                    and str(raw.dxf.layer).casefold() in self.viewport_frozen_layers
                    and not (
                        str(raw.dxf.layer) == "0"
                        and (
                            record.geometry.get("parent_insert_id")
                            or record.geometry.get("parent_insert_handle")
                        )
                    )
                )
            ):
                valid = False
                break
            parent_id = record.geometry.get("parent_insert_id")
            parent_handle = record.geometry.get("parent_insert_handle")
            if parent_id or parent_handle:
                parent = (
                    self.native.get(parent_id)
                    if parent_id
                    else self.native_by_handle.get((record.space, str(parent_handle)))
                )
                if parent is None or parent.source_file_id != original.source_file_id:
                    valid = False
                    break
                records.append(parent)
        self.visibility_cache[identifier] = valid
        return valid

    @staticmethod
    def _touches_boundary(box, point):
        if not box or not point:
            return False
        # Shared CAD coordinates only: this is not a font-height/proximity radius.
        tolerance = max(1e-7, max(box[2] - box[0], box[3] - box[1]) * 1e-8)
        return (
            box[0] - tolerance <= point[0] <= box[2] + tolerance
            and box[1] - tolerance <= point[1] <= box[3] + tolerance
            and min(
                abs(point[0] - box[0]),
                abs(point[0] - box[2]),
                abs(point[1] - box[1]),
                abs(point[1] - box[3]),
            )
            <= tolerance
        )

    def _label_construction(self, text, point):
        if not text.bbox:
            return None
        center = ((text.bbox[0] + text.bbox[2]) / 2, (text.bbox[1] + text.bbox[3]) / 2)
        parent_handle = text.geometry.get("parent_insert_handle")
        parent = (
            self.native_by_handle.get((text.space, str(parent_handle))) if parent_handle else None
        )
        if parent:
            scan = parent.geometry.get("paper_frame_scan", {})
            if scan.get("complete") and self.visible(parent):
                for rectangle in scan.get("rectangles", []):
                    box = rectangle.get("bbox")
                    if _contains(box, center) and self._touches_boundary(box, point):
                        return ("native_label_frame_shared_endpoint", parent.id, box)
        if _contains(text.bbox, point):
            return ("native_text_bbox_shared_endpoint", parent.id if parent else text.id, text.bbox)
        return None

    def _leader_label_binding(self, entity, leader):
        """Join a bound arrow to the uniquely touched native label construction."""
        text = self.native[_native_id(entity)]
        original = self.native[_native_id(leader)]
        if (text.source_file_id, text.sheet_id, text.space) != (
            original.source_file_id,
            original.sheet_id,
            original.space,
        ):
            return None
        if text.entity_type not in {"ATTRIB", "TEXT", "MTEXT"} or not self.visible(text):
            return None
        if original.entity_type not in {"LEADER", "MLEADER", "MULTILEADER"}:
            return None
        if not self.visible(original) or len(original.geometry.get("leader_targets", [])) != 1:
            return None
        point = original.geometry.get("label_point")
        if point is None:
            return None
        if original.id not in self.label_link_cache:
            matches = {}
            for candidate in self.native.values():
                if (
                    (candidate.source_file_id, candidate.sheet_id, candidate.space)
                    != (text.source_file_id, text.sheet_id, text.space)
                    or candidate.entity_type not in {"ATTRIB", "TEXT", "MTEXT"}
                    or not candidate.text
                    or not self.visible(candidate)
                ):
                    continue
                construction = self._label_construction(candidate, point)
                if construction:
                    matches[candidate.id] = construction
            self.label_link_cache[original.id] = matches
        matches = self.label_link_cache[original.id]
        if text.id not in matches or len({m[1] for m in matches.values()}) != 1:
            return None
        kind, parent_id, box = matches[text.id]
        return {
            "kind": kind,
            "leader_original_id": original.id,
            "label_original_id": text.id,
            "label_parent_original_id": parent_id,
            "native_label_bbox": list(box),
            "native_shared_endpoint": list(point),
            "coordinate_space": original.space,
        }

    def direct(self, entity):
        if not self.visible(entity):
            return None
        geometry = entity.geometry
        # DIMENSION text/line may sit outside the component; both native
        # extension endpoints must bind to its physical envelope.
        if entity.entity_type == "DIMENSION":
            points = [geometry.get(k) for k in ("defpoint2", "defpoint3")]
            if all(_contains(self.view.object_bbox, p) for p in points):
                return "native_dimension_endpoints"
            return None
        if entity.entity_type in {"LEADER", "MLEADER", "MULTILEADER"}:
            if _contains(self.view.object_bbox, geometry.get("leader_target")):
                return "native_leader_target"
            return None
        if _contains(self.view.object_bbox, entity.insert):
            return "native_insertion_in_reviewed_component_bbox"
        if _within(self.view.object_bbox, entity.bbox):
            return "native_bbox_in_reviewed_component_bbox"
        return None

    def cut_path_result(self, entity):
        """Reopen symbol and component geometry; never accept an authored path."""
        original = self.native[_native_id(entity)]
        parent = original.geometry.get("parent_insert_handle")
        pairs = self.reference_pairs_by_parent.get(parent, [])
        if original.entity_type != "ATTRIB" or not any(
            original.id in pair.entity_ids for pair in pairs
        ):
            return None
        key = (original.space, parent)
        if key not in self.cut_path_cache:
            # All references are freshly indexed native attached ATTRIBs. The
            # geometry producer rereads their actual INSERT rather than trusting
            # caller-enriched parent, segment or intersection records.
            references = [
                e
                for e in self.native.values()
                if e.source_file_id == original.source_file_id
                and e.space == original.space
                and e.entity_type == "ATTRIB"
                and e.geometry.get("parent_insert_handle") == parent
                and any(e.id in pair.entity_ids for pair in pairs)
            ]
            self.cut_path_cache[key] = native_cut_paths.trace_native_cut_paths(
                self.document,
                reference_entities=references,
                native_objects=self.native_objects,
                viewport_handle=self.sheet.viewport_handle,
            )
        return self.cut_path_cache[key]

    @staticmethod
    def _cut_path_binding(result):
        if not result or not result.get("supported") or not result.get("complete"):
            return None
        return {
            "kind": "native_cut_path_component_crossing",
            "proof_sha256": _hash(result),
            "path_ids": [p["id"] for p in result["paths"]],
            "native_symbol_parents": result["native_symbol_parents"],
            "crossing_object_ids": result["candidate_object_ids"],
            "coordinate_space": result["coordinate_space"],
            "quantity_or_material_inference": False,
        }

    def bound(self, entity):
        cut = self.cut_path_result(entity)
        if cut and (
            cut.get("symbol_recognized")
            or cut.get("source_symbol_evidence_present")
            or cut.get("symbol_geometry_complete") is False
        ):
            # A real cutting symbol takes precedence over its text insertion.
            # No bbox fallback may erase a wrong/disconnected/unsupported path.
            return self._cut_path_binding(cut)
        direct = self.direct(entity)
        if direct:
            return direct
        parent = entity.geometry.get("parent_insert_handle")
        # Discover native parent/leader links while scanning, including when a
        # reviewer did not enumerate every unrelated candidate in the input.
        automatic = [
            e
            for e in self.entities
            if (parent and e.handle == parent)
            or e.geometry.get("annotation_handle") == entity.handle
            or e.entity_type in {"LEADER", "MLEADER", "MULTILEADER"}
        ]
        for anchor in [*self.anchors, *automatic]:
            if not self.direct(anchor):
                continue
            if parent and (
                parent == anchor.handle or parent == anchor.geometry.get("parent_insert_handle")
            ):
                return f"same_native_parent_as_bound_anchor:{_native_id(anchor)}"
            if anchor.geometry.get("annotation_handle") == entity.handle:
                return f"native_leader_annotation:{_native_id(anchor)}"
            linked = self._leader_label_binding(entity, anchor)
            if linked:
                return linked
        return None

    def evidence(self, identifier, *, title=False):
        entity = self.resolve(identifier)
        _require(self.visible(entity), "hidden or unresolved native entity cannot support a view")
        basis = self.bound(entity)
        if title:
            basis = entity.geometry.get(
                "native_title_association", "replayed_panel_title_membership"
            )
        original = self.native[_native_id(entity)]
        if basis is None:
            box = entity.bbox
            target = self.view.object_bbox
            gap = (
                None
                if box is None
                else math.hypot(
                    max(target[0] - box[2], box[0] - target[2], 0),
                    max(target[1] - box[3], box[1] - target[3], 0),
                )
            )
            raise ViewContextBuildError(
                f"evidence has no component binding: {identifier}",
                diagnostic={
                    "entity_id": identifier,
                    "original_entity_id": original.id,
                    "source_id": original.source_file_id,
                    "sheet_id": self.sheet.id,
                    "handle": original.handle,
                    "entity_type": original.entity_type,
                    "projected_entity_bbox": box,
                    "reviewed_object_bbox": target,
                    "bbox_separation_drawing_units": gap,
                    "native_cut_path": self.cut_path_result(entity),
                },
            )
        self.binding_audit[identifier] = {
            "original_entity_id": original.id,
            "panel_entity_id": entity.id,
            "source_id": original.source_file_id,
            "sheet_id": self.sheet.id,
            "handle": original.handle,
            "entity_type": original.entity_type,
            "bbox": entity.bbox,
            "binding_basis": basis,
            "native_visibility_verified": True,
        }
        return {
            "id": identifier,
            "component_id": "",
            "sheet_id": self.sheet.id,
            "source_id": original.source_file_id,
            "entity_type": original.entity_type,
        }

    def unprojected_references(self, selected_refs):
        """Keep unknown native paper records unresolved when projection omits them."""
        viewport = next(
            (
                e
                for e in self.native.values()
                if e.source_file_id == self.sheet.source_file_id
                and e.entity_type == "VIEWPORT"
                and e.handle == self.sheet.viewport_handle
            ),
            None,
        )
        _require(viewport is not None, "selected panel native viewport missing")
        membership = defaultdict(set)
        for entity in self.all_panel_entities.values():
            if entity.source_file_id == self.sheet.source_file_id:
                membership[_native_id(entity)].add(entity.sheet_id)
        page_metadata = {
            value.rsplit("@", 1)[-1]
            for sheet in self.sheets.values()
            if sheet.source_file_id == self.sheet.source_file_id
            for value in sheet.evidence
            if value.startswith("paper_page_reference:")
        }
        represented = {_native_id(e) for e in self.entities}
        output, issues = [], []
        for entity in sorted(self.native.values(), key=lambda e: e.id):
            if (entity.source_file_id, entity.space) != (
                viewport.source_file_id,
                viewport.space,
            ) or entity.id in represented:
                continue
            codes = extract_reference_codes(entity.text)
            if not codes:
                continue
            if entity.id in selected_refs:
                state, reason = "SELECTED_CONNECTION", "verified native title omitted by clipping"
            elif not self.visible(entity):
                state, reason = (
                    "HIDDEN_NATIVE_REFERENCE",
                    "native flags or parent/layer hide this reference",
                )
            elif entity.id in page_metadata:
                state, reason = "NATIVE_PAGE_METADATA", "exact replayed native page-number identity"
            elif membership[entity.id]:
                state, reason = (
                    "OTHER_REPLAYED_PANEL",
                    "native record belongs to other replayed panels",
                )
            else:
                state = "UNPROJECTED_NATIVE_REFERENCE_SCOPE_UNRESOLVED"
                reason = (
                    "native paper reference has no verified panel or component-scope association"
                )
                issues.append(f"{self.sheet.id}:unprojected_native_reference:{entity.id}")
            output.append(
                {
                    "entity_id": entity.id,
                    "original_entity_id": entity.id,
                    "source_id": entity.source_file_id,
                    "sheet_id": self.sheet.id,
                    "native_sheet_id": entity.sheet_id,
                    "native_space": entity.space,
                    "native_bbox": entity.bbox,
                    "native_insert": entity.insert,
                    "target_codes": sorted(codes),
                    "other_replayed_panel_ids": sorted(membership[entity.id]),
                    "candidate_sheet_ids": sorted(
                        s.id
                        for s in self.sheets.values()
                        if s.drawing_number in codes and s.id != self.sheet.id
                    ),
                    "state": state,
                    "binding_basis": None,
                    "reason": reason,
                }
            )
        return output, issues


def _pair(scope, identifiers):
    _require(len(identifiers) == len(set(identifiers)), "duplicate reference entity")
    original_ids = {scope.original(e).id for e in identifiers}
    _require(len(original_ids) == len(identifiers), "duplicate native reference via aliases")
    source = [e for e in scope.native.values() if e.source_file_id == scope.sheet.source_file_id]
    pairs = [
        r
        for r in extract_structured_reference_callouts(source)
        if set(r.entity_ids) == original_ids
    ]
    _require(len(pairs) == 1, "reference is not an unambiguous native same-parent page/view pair")
    return pairs[0]


def _page(scope):
    entries = [
        value.split(":", 1)[1]
        for value in scope.sheet.evidence
        if value.startswith("paper_page_reference:")
    ]
    codes = {value.split("@", 1)[0] for value in entries}
    _require(len(codes) == 1, "selected panel lacks one replayed native owning page")
    for entry in entries:
        entity = scope.native.get(entry.rsplit("@", 1)[-1])
        _require(
            entity is not None and scope.visible(entity),
            "hidden or missing native owning page evidence",
        )
    _require(
        "paper_page_association_basis:NATIVE_CLOSED_FRAME_CONTAINS_VIEWPORT"
        in scope.sheet.evidence,
        "owning page lacks native closed frame proof",
    )
    return next(iter(codes))


def _title_kind(scope, pair):
    anchor = scope.native[pair.entity_ids[0]]
    members = [
        e
        for e in scope.native.values()
        if (e.source_file_id, e.sheet_id, e.space, e.geometry.get("parent_insert_handle"))
        == (anchor.source_file_id, anchor.sheet_id, anchor.space, pair.parent_insert_handle)
        and scope.visible(e)
    ]
    text = " ".join(e.text or "" for e in members).upper()
    roles = set()
    if "SECTION" in text or "剖面" in text or "剖切" in text:
        roles.add("section")
    if "ELEVATION" in text or "立面" in text:
        roles.add("elevation")
    if "PLAN" in text or "平面" in text:
        roles.add("plan")
    if len(roles) > 1:
        return "ambiguous"
    if roles:
        return next(iter(roles))
    if "DETAIL" in text or "节点" in text or "大样" in text or "详图" in text:
        return "detail"
    if "天花" in text:
        return "plan"
    return None


def _verify_target_title_owner(scope, pair):
    """Local title identity must resolve to the specific native viewport."""
    originals = [scope.native[eid] for eid in pair.entity_ids]
    points = [e.insert for e in originals]
    _require(all(point is not None for point in points), "target title lacks native position")
    source = originals[0]
    page = _page(scope)
    candidates = {}
    for sheet in scope.sheets.values():
        if sheet.source_file_id != source.source_file_id:
            continue
        entries = [
            value.split(":", 1)[1]
            for value in sheet.evidence
            if value.startswith("paper_page_reference:")
        ]
        if {entry.split("@", 1)[0] for entry in entries} != {page}:
            continue
        viewport = scope.native_by_handle.get((source.space, sheet.viewport_handle))
        if viewport is None or viewport.entity_type != "VIEWPORT" or not viewport.bbox:
            continue
        box = viewport.bbox
        height = box[3] - box[1]
        # Same bounded title strip used by native panel navigation: immediately
        # below the viewport or in its bottom title band. No whole-page join.
        if not all(
            box[0] <= p[0] <= box[2] and box[1] - height <= p[1] <= box[1] + height * 0.12
            for p in points
        ):
            continue
        parents = {
            scope.native[entry.rsplit("@", 1)[-1]].geometry.get("parent_insert_handle")
            for entry in entries
            if entry.rsplit("@", 1)[-1] in scope.native
        }
        frames = [scope.native_by_handle.get((source.space, parent)) for parent in parents]
        if not any(
            frame
            and frame.geometry.get("paper_frame_scan", {}).get("complete")
            and all(
                any(
                    _contains(rect.get("bbox"), point)
                    for rect in frame.geometry["paper_frame_scan"].get("rectangles", [])
                )
                for point in points
            )
            for frame in frames
        ):
            continue
        gap = sum(abs(p[1] - box[1]) for p in points) / len(points)
        candidates[viewport.handle] = gap
    _require(candidates, "target title lacks native viewport ownership")
    nearest = min(candidates.values())
    owners = [handle for handle, gap in candidates.items() if abs(gap - nearest) <= 1e-8]
    _require(
        owners == [scope.sheet.viewport_handle],
        "target local title belongs to another or ambiguous native viewport",
    )


def build_view_context(index_path, panels_path, selections_path, *, side, relations_path=None):
    """Return context, generation receipt and separate disposition claims.

    No quantities, prices, TakeoffItems, policies or input files are changed.
    ``side`` is supplied separately by the scorer and must match the selection.
    Relations are inventoried as untrusted navigation candidates; only native
    paired references establish a positive connection.
    """
    producer_paths = {
        "producer_sha256": Path(__file__),
        "native_cut_path_producer_sha256": Path(native_cut_paths.__file__),
    }
    producer_hashes = {key: sha256_file(path) for key, path in producer_paths.items()}
    paths = {
        "index": Path(index_path).resolve(),
        "panels": Path(panels_path).resolve(),
        "selections": Path(selections_path).resolve(),
    }
    if relations_path is not None:
        paths["relations"] = Path(relations_path).resolve()
    hashes = {key: sha256_file(path) for key, path in paths.items()}
    index, panels = _read(paths["index"]), _read(paths["panels"])
    selection = ContextSelections.model_validate(_read(paths["selections"]))
    _require(selection.side == side, "selection side mismatch")
    _require(
        len({c.component_id for c in selection.components}) == len(selection.components),
        "duplicate component identity",
    )
    initial_sheets = _unique([Sheet.model_validate(s) for s in panels["sheets"]], "panel sheet")
    selected_sids = {v.sheet_id for c in selection.components for v in c.views}
    _require(selected_sids.issubset(initial_sheets), "selected sheet absent from panels")
    source_ids = {initial_sheets[sid].source_file_id for sid in selected_sids}
    native_list, sheets, entities, sources, scan_issues, panel_warnings = _source_replay(
        index, panels, source_ids
    )
    native = _unique(native_list, "verified native entity")
    context = {
        "schema_version": "view-disposition-context/1",
        "side": side,
        "index_sha256": hashes["index"],
        "source_sha256": {s["source_file_id"]: s["source_sha256"] for s in sources},
        "views": [],
        "entities": [],
        "connections": [],
        "searches": [],
    }
    claims, component_receipts, owned_native = {}, [], {}
    source_documents = {s["source_file_id"]: ezdxf.readfile(s["source_path"]) for s in sources}
    for component in selection.components:
        role_map = {view.role: view for view in component.views}
        second_role = "section" if component.basis_kind == "plan_section" else "elevation"
        _require(
            set(role_map) == {"plan", second_role}, "selected view roles disagree with topology"
        )
        _require(len({v.id for v in component.views}) == 2, "duplicate view identity")
        _require(
            len({v.sheet_id for v in component.views}) == 2, "two views require distinct panels"
        )
        scopes = {
            role: _Scope(view, sheets, entities, native, source_documents)
            for role, view in role_map.items()
        }
        plan, target = scopes["plan"], scopes[second_role]
        target.title_ids.update(component.target_reference_ids)
        source_pair = _pair(plan, component.source_reference_ids)
        target_pair = _pair(target, component.target_reference_ids)
        _require(source_pair.code == _page(target), "reference target owning page mismatch")
        _require(
            source_pair.view_number == target_pair.view_number,
            "reference target local-view mismatch",
        )
        _require(target_pair.code == _page(plan), "target title back-reference mismatch")
        _require(
            _title_kind(target, target_pair) == second_role, "target native title role mismatch"
        )
        _verify_target_title_owner(target, target_pair)
        used_entities = {}
        binding = {}
        for role, scope in scopes.items():
            view = scope.view
            ids = list(view.evidence_ids)
            refs = (
                component.source_reference_ids if role == "plan" else component.target_reference_ids
            )
            ids = list(dict.fromkeys([*ids, *refs]))
            _require(
                len(view.evidence_ids) == len(set(view.evidence_ids)), "duplicate selected evidence"
            )
            for identifier in ids:
                is_title = role != "plan" and identifier in refs
                record = scope.evidence(identifier, title=is_title)
                record["component_id"] = component.component_id
                native_id = scope.original(identifier).id
                owner = (component.component_id, view.id)
                _require(
                    native_id not in owned_native,
                    "native evidence reused across components, stages or aliases",
                )
                _require(identifier not in used_entities, "duplicate evidence across views")
                owned_native[native_id] = owner
                used_entities[identifier] = record
            _require(
                role == "plan"
                or any(scope.original(e).entity_type == "DIMENSION" for e in view.evidence_ids),
                "governing view native DIMENSION missing",
            )
            _require(
                scope.native_objects
                or any(
                    scope.direct(scope.resolve(e))
                    in {"native_dimension_endpoints", "native_leader_target"}
                    for e in [*view.evidence_ids, *view.binding_entity_ids]
                ),
                "component bbox lacks native object, dimension endpoints or leader target",
            )
            for obj in scope.native_objects:
                identity = obj["id"]
                _require(
                    identity not in owned_native, "native object reused across components or views"
                )
                owned_native[identity] = (component.component_id, view.id)
                ids.append(identity)
                used_entities[identity] = {
                    "id": identity,
                    "component_id": component.component_id,
                    "sheet_id": scope.sheet.id,
                    "source_id": scope.sheet.source_file_id,
                    "entity_type": obj["entity_type"],
                }
                scope.binding_audit[identity] = obj
            binding.update(scope.binding_audit)
            context["views"].append(
                {
                    "id": view.id,
                    "component_id": component.component_id,
                    "role": role,
                    "sheet_id": view.sheet_id,
                    "source_id": scope.sheet.source_file_id,
                    "state": "CONFIRMED",
                    "evidence_ids": ids,
                }
            )
        connection_id = _id(
            "cad-view-connection",
            {
                "component": component.component_id,
                "source": source_pair.entity_ids,
                "target": target_pair.entity_ids,
                "views": [plan.view.id, target.view.id],
                "index": hashes["index"],
            },
        )
        connection = {
            "id": connection_id,
            "component_id": component.component_id,
            "source_view_id": plan.view.id,
            "target_view_id": target.view.id,
            "state": "CONFIRMED",
            "evidence_ids": [*component.source_reference_ids, *component.target_reference_ids],
        }
        context["connections"].append(connection)
        context["entities"].extend(used_entities.values())
        excluded = "elevation" if second_role == "section" else "detail"
        searched = component.negative_search.searched_sheet_ids
        required_sheets = (
            {plan.sheet.id, target.sheet.id} if excluded == "elevation" else {target.sheet.id}
        )
        _require(
            len(searched) == len(set(searched)) and set(searched) == required_sheets,
            "negative search must cover exactly the required selected sheets",
        )
        selected_refs = {plan.original(e).id for e in component.source_reference_ids}
        selected_refs.update(target.original(e).id for e in component.target_reference_ids)
        inventory, unresolved, conflicts = [], [], []
        component_scan_issues = list(scan_issues)
        for scope in scopes.values():
            if scope.sheet.id not in searched:
                continue
            for entity in sorted(scope.entities, key=lambda e: e.id):
                codes = extract_reference_codes(entity.text)
                if not codes:
                    continue
                native_id = _native_id(entity)
                binding_basis = scope.bound(entity)
                candidate_sheets = sorted(
                    s.id
                    for s in sheets.values()
                    if s.drawing_number in codes and s.id != scope.sheet.id
                )
                state = (
                    "SELECTED_CONNECTION"
                    if native_id in selected_refs
                    else ("OUTSIDE_COMPONENT" if binding_basis is None else "UNRESOLVED_REFERENCE")
                )
                cut_result = scope.cut_path_result(entity)
                if (
                    state == "OUTSIDE_COMPONENT"
                    and cut_result
                    and (
                        not cut_result.get("complete")
                        or cut_result.get("symbol_geometry_complete") is False
                        or (
                            (
                                cut_result.get("symbol_recognized")
                                or cut_result.get("source_symbol_evidence_present")
                            )
                            and any(
                                reason != "NO_PROPER_NATIVE_OBJECT_CROSSING"
                                for reason in cut_result.get("reason_codes", [])
                            )
                        )
                    )
                ):
                    # Failure to trace is not proof that a reference belongs to
                    # another component. Retain it for the negative-search gate.
                    state = "UNRESOLVED_REFERENCE"
                if not scope.visible(entity):
                    state = "HIDDEN_NATIVE_REFERENCE"
                if state == "UNRESOLVED_REFERENCE":
                    unresolved.append(entity.id)
                    conflicts.extend(
                        sid for sid in candidate_sheets if sheets[sid].kind == excluded
                    )
                inventory.append(
                    {
                        "entity_id": entity.id,
                        "original_entity_id": native_id,
                        "source_id": entity.source_file_id,
                        "sheet_id": entity.sheet_id,
                        "target_codes": sorted(codes),
                        "candidate_sheet_ids": candidate_sheets,
                        "state": state,
                        "binding_basis": binding_basis,
                        "native_cut_path_proof_sha256": _hash(cut_result)
                        if cut_result is not None
                        else None,
                        "reason": "native reference retained; component relation requires review"
                        if state == "UNRESOLVED_REFERENCE"
                        else "no native geometric or parent binding to reviewed component"
                        if state == "OUTSIDE_COMPONENT"
                        else "native flags or parent/layer hide this reference"
                        if state == "HIDDEN_NATIVE_REFERENCE"
                        else "verified selected positive connection",
                    }
                )
            unprojected, missing_issues = scope.unprojected_references(selected_refs)
            inventory.extend(unprojected)
            component_scan_issues.extend(missing_issues)
            for candidate in unprojected:
                if candidate["state"] == "UNPROJECTED_NATIVE_REFERENCE_SCOPE_UNRESOLVED":
                    unresolved.append(candidate["entity_id"])
                    conflicts.extend(
                        sid
                        for sid in candidate["candidate_sheet_ids"]
                        if sheets[sid].kind == excluded
                    )
        search_id = _id(
            "cad-view-search",
            {
                "component": component.component_id,
                "sheets": sorted(searched),
                "inventory": inventory,
                "index": hashes["index"],
            },
        )
        context["searches"].append(
            {
                "id": search_id,
                "component_id": component.component_id,
                "excluded_stage": excluded,
                "searched_sheet_ids": sorted(searched),
                "complete": not component_scan_issues,
                "unresolved_reference_ids": sorted(set(unresolved)),
                "conflicting_view_ids": sorted(set(conflicts)),
            }
        )
        claims[component.component_id] = {
            excluded: {
                "state": "NOT_APPLICABLE",
                "component_id": component.component_id,
                "basis_kind": component.basis_kind,
                "basis": component.basis,
                "index_sha256": hashes["index"],
                "source_sha256": {
                    sid: context["source_sha256"][sid]
                    for sid in {plan.sheet.source_file_id, target.sheet.source_file_id}
                },
                "support_view_ids": [plan.view.id, target.view.id],
                "connection_id": connection_id,
                "search_id": search_id,
                "evidence_ids": list(used_entities),
                "review": component.review.model_dump(mode="json"),
            }
        }
        component_receipts.append(
            {
                "component_id": component.component_id,
                "state": "VERIFIED"
                if not component_scan_issues and not unresolved and not conflicts
                else "REVIEW",
                "review": component.review.model_dump(mode="json"),
                "views": [v.model_dump(mode="json") for v in component.views],
                "entity_bindings": binding,
                "native_cut_path_diagnostics": [
                    {
                        "view_id": scope.view.id,
                        "native_space": key[0],
                        "parent_insert_handle": key[1],
                        "proof_sha256": _hash(result),
                        "result": result,
                    }
                    for scope in scopes.values()
                    for key, result in sorted(scope.cut_path_cache.items())
                ],
                "reference_inventory": inventory,
                "connection_basis": {
                    "kind": "native_same_parent_reciprocal_page_view_pairs",
                    "source_page": _page(plan),
                    "target_page": _page(target),
                    "local_view_number": source_pair.view_number,
                    "source_reference_original_ids": list(source_pair.entity_ids),
                    "target_reference_original_ids": list(target_pair.entity_ids),
                },
                "scan_issues": component_scan_issues,
            }
        )
    context = ViewDispositionContext.model_validate(context).model_dump(mode="json")
    for key in ("views", "entities", "connections", "searches"):
        _unique(context[key], key)
    relations = _read(paths["relations"]) if "relations" in paths else None
    receipt = {
        "schema_version": "cad-view-context-receipt/1",
        "producer_version": PRODUCER,
        **producer_hashes,
        "side": side,
        "path_scope": "local_run_diagnostics",
        "mutates_takeoff": False,
        "commercial_effect": "NONE",
        "context_sha256": _hash(context),
        "input_sha256": hashes,
        "sources": sources,
        "components": component_receipts,
        "panel_replay_warnings": panel_warnings,
        "relation_candidates": relations,
        "relation_trust": "navigation_only; never used to confirm or erase native references",
        "limitations": [
            "Reviewer identifies the component and semantics; no reviewer authentication.",
            "Negative search covers indexed native text references in the component scope.",
            "No claim about inaccessible xrefs, unindexed graphics or whole-project coverage.",
            "Receipt hashes are reproducibility checks, not signatures; verify by rebuilding.",
        ],
    }
    _require(
        hashes == {key: sha256_file(path) for key, path in paths.items()},
        "input changed during build",
    )
    _require(
        all(sha256_file(Path(s["source_path"])) == s["source_sha256"] for s in sources),
        "source changed during build",
    )
    _require(
        producer_hashes == {key: sha256_file(path) for key, path in producer_paths.items()},
        "producer code changed during build",
    )
    # A receipt must compare identically after being written/read as JSON;
    # native CAD geometry uses tuples internally, while JSON uses arrays.
    return {
        "context": context,
        "receipt": json.loads(_canonical(receipt)),
        "disposition_claims": claims,
    }


def verify_view_context_receipt(
    index_path, panels_path, selections_path, context, receipt, *, side, relations_path=None
):
    """Reproduce all CAD facts, rejecting drift and caller-edited contexts/receipts."""
    current = build_view_context(
        index_path, panels_path, selections_path, side=side, relations_path=relations_path
    )
    _require(current["context"] == context, "context differs from verified CAD rebuild")
    _require(current["receipt"] == receipt, "receipt differs from verified CAD rebuild")
    return True


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("index")
    parser.add_argument("panels")
    parser.add_argument("selections")
    parser.add_argument("--side", choices=("predicted", "gold"), required=True)
    parser.add_argument("--relations")
    parser.add_argument("--out", required=True)
    parser.add_argument("--receipt", required=True)
    parser.add_argument("--claims")
    args = parser.parse_args(argv)
    result = build_view_context(
        args.index, args.panels, args.selections, side=args.side, relations_path=args.relations
    )
    outputs = [Path(p).resolve() for p in (args.out, args.receipt, args.claims) if p]
    inputs = {
        Path(p).resolve() for p in (args.index, args.panels, args.selections, args.relations) if p
    }
    inputs.update(Path(s["source_path"]).resolve() for s in result["receipt"]["sources"])
    _require(
        len(set(outputs)) == len(outputs) and not set(outputs) & inputs,
        "output overwrites input/source",
    )
    write_json_atomic(Path(args.out), result["context"])
    write_json_atomic(Path(args.receipt), result["receipt"])
    if args.claims:
        write_json_atomic(Path(args.claims), result["disposition_claims"])
    print(
        json.dumps(
            {
                "context": args.out,
                "receipt": args.receipt,
                "states": [c["state"] for c in result["receipt"]["components"]],
            }
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
