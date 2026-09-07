"""Refresh damaged paper-frame extents from immutable native CAD, not text guesses."""

from __future__ import annotations

from collections import defaultdict
from pathlib import Path

import ezdxf
from ezdxf import bbox

from .io import sha256_file
from .linking import extract_structured_reference_callouts, normalize_reference_code


def refresh_native_route_frames(index_payload, native_entities, *, max_sources=64, max_frames=2000):
    """Return a separate navigation view; never overwrite the semantic CAD index.

    Old indexes can retain only an INSERT's insertion point as its bbox. A
    moved block can have its actual frame far from that insertion point.
    Codes still require visible native sibling attributes and viewport containment.
    Failed/capped source refreshes are explicit, not proof of missing drawings.
    """
    if max_sources < 1 or max_frames < 1:
        raise ValueError("positive context caps required")
    entities = list(native_entities)
    page_codes = {r.code for r in extract_structured_reference_callouts(entities)}
    page_codes.update(
        normalize_reference_code(s.get("drawing_number"))
        for src in index_payload.get("sources", [])
        for s in src.get("sheets", [])
    )
    page_codes.discard(None)
    groups = defaultdict(list)
    for e in entities:
        if (
            e.entity_type == "ATTRIB"
            and e.space.startswith("paper:")
            and not e.geometry.get("semantic_hidden")
            and normalize_reference_code(e.text)
            and (not page_codes or normalize_reference_code(e.text) in page_codes)
        ):
            groups[(e.source_file_id, e.space, e.geometry.get("parent_insert_handle"))].append(e)
    candidates = defaultdict(list)
    for e in entities:
        if (
            e.entity_type == "INSERT"
            and not e.geometry.get("semantic_hidden")
            and (e.source_file_id, e.space, e.handle) in groups
        ):
            candidates[e.source_file_id].append(e)
    sources = {s.get("source_file_id"): s for s in index_payload.get("sources", [])}
    updates, records, issues = {}, [], []
    examined = 0
    for ordinal, (fid, frames) in enumerate(sorted(candidates.items())):
        if ordinal >= max_sources:
            issues.append({"source_file_id": fid, "reason": "SOURCE_CAP"})
            continue
        source_updates, source_records = {}, []
        try:
            src = sources[fid]
            path = Path(src["source_path"])
            digest = src["source_sha256"]
            if path.suffix.lower() != ".dxf" or sha256_file(path) != digest:
                raise ValueError("indexed DXF hash mismatch")
            doc = ezdxf.readfile(path)
            cache = bbox.Cache()
            for old in sorted(frames, key=lambda e: e.id):
                if examined >= max_frames:
                    issues.append({"source_file_id": fid, "reason": "FRAME_CAP"})
                    break
                examined += 1
                ins = doc.entitydb.get(old.handle)
                if (
                    ins is None
                    or ins.dxftype() != "INSERT"
                    or ins.get_layout().name != old.space.removeprefix("paper:")
                ):
                    issues.append({"entity_id": old.id, "reason": "NATIVE_IDENTITY_MISMATCH"})
                    continue
                layer = doc.layers.get(ins.dxf.layer)
                if ins.dxf.get("invisible", 0) or layer.is_off() or layer.is_frozen():
                    continue
                ext = bbox.extents([ins], fast=True, cache=cache)
                if not ext.has_data or min(ext.size.x, ext.size.y) <= 0:
                    continue
                box = (ext.extmin.x, ext.extmin.y, ext.extmax.x, ext.extmax.y)
                vps = [
                    v
                    for v in ins.get_layout().query("VIEWPORT")
                    if v.dxf.id > 1
                    and box[0] <= v.dxf.center.x - v.dxf.width / 2
                    and box[2] >= v.dxf.center.x + v.dxf.width / 2
                    and box[1] <= v.dxf.center.y - v.dxf.height / 2
                    and box[3] >= v.dxf.center.y + v.dxf.height / 2
                ]
                if not vps:
                    continue
                source_updates[old.id] = old.model_copy(
                    update={
                        "bbox": box,
                        "geometry": {**old.geometry, "native_route_frame_bbox": list(box)},
                    }
                )
                source_records.append(
                    {
                        "entity_id": old.id,
                        "source_file_id": fid,
                        "handle": old.handle,
                        "space": old.space,
                        "original_bbox": old.bbox,
                        "native_bbox": list(box),
                        "source_sha256": digest,
                        "viewport_handles": sorted(v.dxf.handle for v in vps),
                        "basis": "hash_verified_native_insert_extents_contains_viewport",
                    }
                )
            if sha256_file(path) != digest:
                raise ValueError("source changed during frame refresh")
            updates.update(source_updates)
            records.extend(source_records)
        except Exception as exc:
            issues.append({"source_file_id": fid, "reason": type(exc).__name__ + ": " + str(exc)})
    return [updates.get(e.id, e) for e in entities], {
        "schema_version": "native-route-frame-context/1.0",
        "state": "REVIEW",
        "path_scope": "local_run_diagnostics",
        "mutates_index": False,
        "candidate_sources": len(candidates),
        "examined_inserts": examined,
        "page_code_scope_count": len(page_codes),
        "refreshed_frames": len(records),
        "records": records,
        "issues": issues,
        "incomplete": bool(issues),
        "max_sources": max_sources,
        "max_frames": max_frames,
    }
