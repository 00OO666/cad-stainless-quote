"""Bind explicit non-reference graphic reviews to immutable native OLE instances.

This is a replay checker, not image recognition or reviewer authentication. A
trusted, separately recorded semantic review is required. No block-name, logo,
customer hash, or caller-authored ``ignore_warning`` rule is built in. Original
warnings remain in the audit, including those proven non-blocking. OLE objects
are never activated, copied, rendered by an external program, or sent online.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections import Counter
from datetime import datetime
from itertools import product
from pathlib import Path

import ezdxf
from ezdxf.math import Vec3

SCHEMA = "native-graphic-review/1"
_HASH = re.compile(r"[0-9a-f]{64}\Z")
_WARNING = re.compile(r"paper:([^:\r\n]+):([^:\r\n]+):OLE2FRAME:None: non copyable\Z")


class NativeGraphicReviewError(ValueError):
    pass


def _require(condition, reason):
    if not condition:
        raise NativeGraphicReviewError(reason)


def _digest(path):
    result = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            result.update(chunk)
    return result.hexdigest()


def _canonical_hash(value):
    return hashlib.sha256(
        json.dumps(
            value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False
        ).encode()
    ).hexdigest()


def _same(actual, reviewed):
    if isinstance(actual, (float, int)) and not isinstance(actual, bool):
        return (
            isinstance(reviewed, (float, int))
            and not isinstance(reviewed, bool)
            and math.isfinite(reviewed)
            and math.isclose(actual, reviewed, rel_tol=1e-12, abs_tol=1e-8)
        )
    if isinstance(actual, list):
        return (
            isinstance(reviewed, list)
            and len(actual) == len(reviewed)
            and all(_same(a, b) for a, b in zip(actual, reviewed, strict=True))
        )
    if isinstance(actual, dict):
        return (
            isinstance(reviewed, dict)
            and actual.keys() == reviewed.keys()
            and all(_same(v, reviewed[k]) for k, v in actual.items())
        )
    return actual == reviewed


def inspect_native_ole_inventory(document, *, max_entities=100000, max_depth=32):
    """Enumerate all active instances without copying OLE or expanding geometry.

    Native block graphs first select OLE-reachable branches. Every reachable
    instance, including hidden instances, is retained. MINSERT, xrefs, cycles,
    nonfinite geometry and depth/entity limits fail closed. Uninstantiated
    definitions cannot produce rendered-instance warnings and are listed only
    in the definition discovery count.
    """
    _require(
        type(max_entities) is int and max_entities > 0 and type(max_depth) is int and max_depth > 0,
        "INVALID_SCAN_LIMIT",
    )
    blocks = {block.name: block for block in document.blocks}
    names = {name.casefold(): name for name in blocks}
    edges = {
        name: {
            names.get(str(e.dxf.name).casefold(), str(e.dxf.name))
            for e in block
            if e.dxftype() == "INSERT"
        }
        for name, block in blocks.items()
    }
    direct = {
        name for name, block in blocks.items() if any(e.dxftype() == "OLE2FRAME" for e in block)
    }
    reachable = set(direct)
    while True:
        updated = reachable | {name for name, targets in edges.items() if targets & reachable}
        if updated == reachable:
            break
        reachable = updated
    definitions, instances = {}, []
    visited = 0

    def walk(entity, layout, chain, block_chain, steps, matrices):
        nonlocal visited
        visited += 1
        _require(visited <= max_entities, "NATIVE_INSTANCE_ENTITY_CAP")
        handle = entity.dxf.get("handle")
        _require(handle and entity.is_alive, "MISSING_NATIVE_HANDLE")
        if entity.dxftype() == "INSERT":
            name = names.get(str(entity.dxf.name).casefold(), str(entity.dxf.name))
            if name not in reachable:
                return
            _require(len(block_chain) < max_depth, "NATIVE_INSTANCE_DEPTH_CAP")
            _require(
                name.casefold() not in {n.casefold() for n in block_chain}, "NATIVE_INSTANCE_CYCLE"
            )
            _require(
                entity.dxf.get("row_count", 1) == 1 and entity.dxf.get("column_count", 1) == 1,
                "UNSUPPORTED_NATIVE_MINSERT",
            )
            block = blocks.get(name)
            _require(block is not None and not block.block.is_xref, "MISSING_OR_EXTERNAL_BLOCK")
            matrix = entity.matrix44()
            rows = [list(matrix.get_row(i)) for i in range(4)]
            _require(
                all(math.isfinite(v) for row in rows for v in row), "NONFINITE_INSTANCE_MATRIX"
            )
            step = {"insert_handle": handle, "block": name, "matrix44_rows": rows}
            for child in block:
                if child.dxftype() in {"INSERT", "OLE2FRAME"}:
                    walk(
                        child,
                        layout,
                        [*chain, handle],
                        [*block_chain, name],
                        [*steps, step],
                        [*matrices, matrix],
                    )
            return
        _require(entity.dxftype() == "OLE2FRAME", "UNSUPPORTED_GRAPHIC_TYPE")
        payload = entity.binary_data()
        _require(bool(payload), "EMPTY_NATIVE_PAYLOAD")
        bounds = entity.bbox()
        _require(bounds.has_data, "MISSING_NATIVE_OLE_BOUND")
        local = [list(bounds.extmin), list(bounds.extmax)]
        _require(
            all(math.isfinite(v) for point in local for v in point), "NONFINITE_NATIVE_OLE_BOUND"
        )
        points = []
        for coordinate in product(*[(bounds.extmin[i], bounds.extmax[i]) for i in range(3)]):
            point = Vec3(coordinate)
            for matrix in reversed(matrices):
                point = matrix.transform(point)
            points.append(point)
        _require(
            all(math.isfinite(v) for point in points for v in point),
            "NONFINITE_TRANSFORMED_OLE_BOUND",
        )
        world = [
            min(p.x for p in points),
            min(p.y for p in points),
            max(p.x for p in points),
            max(p.y for p in points),
        ]
        _require(world[2] > world[0] and world[3] > world[1], "DEGENERATE_NATIVE_OLE_BOUND")
        definition = {
            "handle": handle,
            "owner_block": block_chain[-1] if block_chain else None,
            "payload_sha256": hashlib.sha256(payload).hexdigest(),
            "payload_bytes": len(payload),
        }
        _require(
            handle not in definitions or definitions[handle] == definition,
            "AMBIGUOUS_NATIVE_DEFINITION",
        )
        definitions[handle] = definition
        instances.append(
            {
                "layout": layout.name,
                "space": "model" if layout.name.casefold() == "model" else "paper",
                "definition_handle": handle,
                "handle_chain": [*chain, handle],
                "block_chain": block_chain,
                "transforms_outer_to_inner": steps,
                "local_bbox": local,
                "world_bbox": world,
            }
        )

    for layout in document.layouts:
        for entity in layout:
            if entity.dxftype() == "OLE2FRAME":
                walk(entity, layout, [], [], [], [])
            elif entity.dxftype() == "INSERT":
                name = names.get(str(entity.dxf.name).casefold(), str(entity.dxf.name))
                if name in reachable:
                    walk(entity, layout, [], [], [], [])
    instances.sort(key=lambda r: (r["layout"], r["handle_chain"]))
    keys = [(r["layout"], tuple(r["handle_chain"])) for r in instances]
    _require(len(set(keys)) == len(keys), "DUPLICATE_NATIVE_INSTANCE")
    return {
        "definitions": sorted(definitions.values(), key=lambda r: r["handle"]),
        "instances": instances,
        "scan": {
            "complete": True,
            "visited_entities": visited,
            "definition_blocks_discovered": len(direct),
        },
    }


def _validate_review(manifest, inventory, source_sha256):
    _require(
        isinstance(manifest, dict) and manifest.get("schema_version") == SCHEMA,
        "MISSING_OR_INVALID_EXPLICIT_REVIEW",
    )
    _require(manifest.get("source_sha256") == source_sha256, "REVIEW_SOURCE_HASH_MISMATCH")
    review = manifest.get("review", {})
    _require(
        all(
            isinstance(review.get(key), str) and review[key].strip()
            for key in ("reviewer", "reviewed_at", "reason")
        ),
        "REVIEW_METADATA_REQUIRED",
    )
    try:
        stamp = datetime.fromisoformat(review["reviewed_at"].replace("Z", "+00:00"))
        _require(stamp.tzinfo is not None, "REVIEW_TIME_ZONE_REQUIRED")
    except (ValueError, TypeError) as exc:
        raise NativeGraphicReviewError("INVALID_REVIEW_TIME") from exc
    declared = manifest.get("definitions")
    _require(isinstance(declared, list) and declared, "REVIEW_DEFINITIONS_REQUIRED")
    by_handle = {}
    artifacts = []
    for row in declared:
        _require(
            isinstance(row, dict) and row.get("handle") not in by_handle,
            "DUPLICATE_OR_INVALID_REVIEW_DEFINITION",
        )
        by_handle[row.get("handle")] = row
    _require(
        set(by_handle) == {r["handle"] for r in inventory["definitions"]},
        "REVIEW_DEFINITION_COVERAGE_MISMATCH",
    )
    for actual in inventory["definitions"]:
        declared_row = by_handle[actual["handle"]]
        _require(
            all(_same(value, declared_row.get(key)) for key, value in actual.items()),
            "REVIEW_PAYLOAD_OR_DEFINITION_MISMATCH",
        )
        _require(
            declared_row.get("semantic_classification") == "NON_REFERENCE_GRAPHIC",
            "REFERENCE_CONTENT_NOT_EXEMPT",
        )
        # original_content is the complete OLE binary_data() payload, not an
        # arbitrary extracted stream. The reviewed image's interpretation and
        # derivation remain an explicit semantic review, not automatic decoding.
        for key in ("original_content", "reviewed_image"):
            asset = declared_row.get(key, {})
            _require(
                isinstance(asset, dict)
                and isinstance(asset.get("path"), str)
                and bool(asset["path"]),
                "REVIEW_ARTIFACT_REQUIRED",
            )
            _require(
                isinstance(asset.get("sha256"), str) and bool(_HASH.fullmatch(asset["sha256"])),
                "INVALID_REVIEW_ARTIFACT_HASH",
            )
            if key == "original_content":
                _require(
                    asset["sha256"] == actual["payload_sha256"],
                    "ORIGINAL_CONTENT_NOT_NATIVE_PAYLOAD",
                )
            path = Path(asset["path"])
            _require(
                path.is_file() and _digest(path) == asset["sha256"], "REVIEW_ARTIFACT_HASH_MISMATCH"
            )
            artifacts.append(
                {
                    "definition_handle": actual["handle"],
                    "role": key,
                    "path": str(path.resolve()),
                    "sha256": asset["sha256"],
                }
            )
    declared_instances = manifest.get("instances")
    _require(isinstance(declared_instances, list), "REVIEW_INSTANCES_REQUIRED")
    reviewed = sorted(declared_instances, key=lambda r: (r["layout"], r["handle_chain"]))
    _require(
        _same(inventory["instances"], reviewed), "REVIEW_INSTANCE_COVERAGE_OR_TRANSFORM_MISMATCH"
    )
    _require(bool(reviewed), "NO_REVIEWED_INSTANCES")
    _require(
        all(r["space"] == "paper" and r["block_chain"] for r in reviewed),
        "UNSUPPORTED_WARNING_INSTANCE_SCOPE",
    )
    return artifacts


def classify_native_graphic_warnings(
    source_path, warning_batches, manifest, *, max_entities=100000, max_depth=32
):
    """Return classified originals and blocking remainders, never mutate inputs.

    ``warning_batches`` maps replay labels (e.g. source/fresh) to raw warning
    lists. Exact message multiplicity is checked independently in every batch.
    One invalid source/payload/instance/artifact review blocks all exceptions.
    Other unrecognized warnings stay blocking even when an OLE group is valid.

    Manifest shape: schema_version, source_sha256, review {reviewer, reviewed_at,
    reason}, definitions [{native inventory fields, semantic_classification,
    original_content:{path,sha256} (complete native OLE binary_data bytes),
    reviewed_image:{path,sha256}}], instances
    [exact records from inspect_native_ole_inventory]. The caller supplies the
    semantic review; this module checks byte/instance provenance, not truth of
    that review. Callers must retain the manifest hash and explicit review trust.
    """
    _require(
        isinstance(warning_batches, dict)
        and warning_batches
        and all(
            isinstance(k, str) and isinstance(v, list) and all(isinstance(w, str) for w in v)
            for k, v in warning_batches.items()
        ),
        "INVALID_WARNING_BATCHES",
    )
    output = {
        "schema_version": "native-graphic-warning-audit/1",
        "source_sha256": None,
        "review_manifest_sha256": None,
        "review_issues": [],
        "inventory": None,
        "classified_warnings": [],
        "remaining": {},
        "reviewed_artifacts": [],
        "semantic_review_is_explicit_not_automatic": True,
        "scope": (
            "Exact native payload and complete active instance review; "
            "not all-OLE waiver or whole-project coverage."
        ),
    }
    expected = Counter()
    valid = False
    try:
        path = Path(source_path).resolve()
        _require(path.is_file() and path.suffix.lower() == ".dxf", "NATIVE_SOURCE_UNAVAILABLE")
        source_hash = _digest(path)
        output["source_sha256"] = source_hash
        output["review_manifest_sha256"] = _canonical_hash(manifest)
        document = ezdxf.readfile(path)
        inventory = inspect_native_ole_inventory(
            document, max_entities=max_entities, max_depth=max_depth
        )
        output["inventory"] = inventory
        artifacts = _validate_review(manifest, inventory, source_hash)
        output["reviewed_artifacts"] = artifacts
        for row in inventory["instances"]:
            _require(
                ":" not in row["layout"] and ":" not in row["block_chain"][-1],
                "AMBIGUOUS_WARNING_GROUP",
            )
            expected[
                f"paper:{row['layout']}:{row['block_chain'][-1]}:OLE2FRAME:None: non copyable"
            ] += 1
        _require(_digest(path) == source_hash, "SOURCE_CHANGED_DURING_REVIEW")
        _require(
            all(_digest(a["path"]) == a["sha256"] for a in artifacts),
            "ARTIFACT_CHANGED_DURING_REVIEW",
        )
        _require(
            _canonical_hash(manifest) == output["review_manifest_sha256"],
            "REVIEW_CHANGED_DURING_REPLAY",
        )
        valid = True
    except Exception as exc:
        # No source or review failure is ever converted to an empty warning list.
        output["review_issues"].append(
            str(exc)
            if isinstance(exc, NativeGraphicReviewError)
            else f"NATIVE_REVIEW_UNRESOLVED:{type(exc).__name__}"
        )
    for batch, warnings in warning_batches.items():
        counts = Counter(w for w in warnings if _WARNING.fullmatch(w))
        mismatches = {w for w in expected if counts[w] != expected[w]} if valid else set()
        if mismatches:
            output["review_issues"].append(f"WARNING_MULTIPLICITY_MISMATCH:{batch}")
        remaining = []
        for index, warning in enumerate(warnings):
            recognized = bool(_WARNING.fullmatch(warning))
            reviewed = valid and recognized and warning in expected and warning not in mismatches
            if not reviewed:
                remaining.append(warning)
            output["classified_warnings"].append(
                {
                    "batch": batch,
                    "index": index,
                    "warning": warning,
                    "classification": "CONTENT_REVIEWED_NON_REFERENCE_GRAPHIC"
                    if reviewed
                    else "BLOCKING_UNRESOLVED_WARNING",
                    "blocking": not reviewed,
                    "expected_native_instances_in_group": expected.get(warning) if valid else None,
                    "observed_warning_count_in_batch": counts.get(warning, 0)
                    if recognized
                    else None,
                    "definition_handles": sorted(
                        {
                            i["definition_handle"]
                            for i in (output["inventory"] or {}).get("instances", [])
                            if reviewed
                            and warning
                            == (
                                f"paper:{i['layout']}:{i['block_chain'][-1]}:"
                                "OLE2FRAME:None: non copyable"
                            )
                        }
                    ),
                    "reason": (
                        "Explicit semantic review + exact native payload "
                        "+ complete instance replay + exact warning multiplicity"
                    )
                    if reviewed
                    else (
                        "Unrecognized warning, unreviewed source/content/instance, "
                        "or incomplete warning coverage"
                    ),
                }
            )
        output["remaining"][batch] = remaining
    output["all_reviewed_groups_covered"] = valid and not output["review_issues"]
    output["blocking"] = bool(output["review_issues"] or any(output["remaining"].values()))
    return output
