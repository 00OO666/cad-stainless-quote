"""Versioned, source-verified navigation context; never a takeoff decision.

Receipts bind a freshly generated route ledger to its exact index and panel
snapshots. They are reproducibility checks, not signatures or authorization.
Legacy ledgers without a receipt remain unavailable rather than being certified
after the fact. No source, route, or snapshot is rewritten during resume.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from copy import deepcopy
from pathlib import Path
from typing import Any

from .io import sha256_file, write_json_atomic

CONTEXT_SCHEMA = "route-review-context/1.0"
RECEIPT_SCHEMA = "route-review-receipt/1.0"
ROUTE_SCHEMA = "numbered-detail-routes/1.1"
PRODUCER_VERSION = "enhanced-numbered-route-review/1"


class _Unavailable(ValueError):
    def __init__(self, code: str, detail: str = ""):
        super().__init__(detail)
        self.code = code


def _read_object(path: Path, code: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(value, dict):
            raise ValueError("expected JSON object")
        return value
    except (OSError, ValueError, TypeError) as exc:
        raise _Unavailable(code, str(exc)) from exc


def _review_only(value: Any, *, candidate: bool = False) -> Any:
    """Keep original navigation states but never expose them as acceptance."""
    if isinstance(value, list):
        return [_review_only(item, candidate=candidate) for item in value]
    if not isinstance(value, dict):
        return deepcopy(value)
    result = {
        key: _review_only(
            item,
            candidate=(
                key in {"records", "candidates", "groups", "selected_title"}
                or key.endswith("_candidates")
            ),
        )
        for key, item in value.items()
    }
    # Every record, alternative and nested title is review context, even when a
    # future upstream navigation state happens to be named PASS or CONFIRMED.
    if candidate or "state" in result or "route_id" in result:
        result["candidate_effective_state"] = "REVIEW"
    if "state" in result:
        result["original_state"] = result["state"]
        result["state"] = "REVIEW"
    return result


def _source_manifest(index: Mapping[str, Any]) -> list[dict[str, str]]:
    sources = index.get("sources")
    if not isinstance(sources, list) or not sources:
        raise _Unavailable("INVALID_INDEX_SOURCE_MANIFEST")
    result = []
    seen = set()
    for source in sources:
        if not isinstance(source, dict):
            raise _Unavailable("INVALID_INDEX_SOURCE_MANIFEST")
        fid, digest, raw_path = (
            source.get("source_file_id"),
            source.get("source_sha256"),
            source.get("source_path"),
        )
        if (
            not all(isinstance(v, str) and v for v in (fid, digest, raw_path))
            or len(digest) != 64
            or fid in seen
        ):
            raise _Unavailable("INVALID_INDEX_SOURCE_MANIFEST")
        path = Path(raw_path).resolve()
        if path.suffix.lower() != ".dxf" or not path.is_file():
            raise _Unavailable("INDEXED_SOURCE_UNAVAILABLE", str(path))
        try:
            actual = sha256_file(path)
        except OSError as exc:
            raise _Unavailable("INDEXED_SOURCE_UNAVAILABLE", str(path)) from exc
        if actual != digest:
            raise _Unavailable("INDEXED_SOURCE_HASH_MISMATCH", fid)
        result.append({"source_file_id": fid, "source_sha256": digest, "source_path": str(path)})
        seen.add(fid)
    return sorted(result, key=lambda item: item["source_file_id"])


def _validate_routes(
    routes: dict[str, Any], sources: list[dict[str, str]], sheets: Sequence[Any]
) -> None:
    if routes.get("schema_version") != ROUTE_SCHEMA:
        raise _Unavailable("UNSUPPORTED_ROUTE_SCHEMA")
    if not isinstance(routes.get("records"), list):
        raise _Unavailable("INVALID_ROUTE_RECORDS")
    for key, version in (
        ("node_view_groups", "node-view-groups/1.0"),
        ("native_frame_context", "native-route-frame-context/1.0"),
    ):
        section = routes.get(key)
        if not isinstance(section, dict) or section.get("schema_version") != version:
            raise _Unavailable("UNSUPPORTED_ROUTE_SECTION_SCHEMA", key)
    source_ids = {source["source_file_id"] for source in sources}
    sheet_sources = {sheet.id: sheet.source_file_id for sheet in sheets}
    route_ids = set()
    for record in routes["records"]:
        if (
            not isinstance(record, dict)
            or not isinstance(record.get("route_id"), str)
            or not record["route_id"]
            or record["route_id"] in route_ids
            or not isinstance(record.get("candidates"), list)
        ):
            raise _Unavailable("INVALID_ROUTE_RECORDS")
        route_ids.add(record["route_id"])
        if record.get("source_file_id") not in source_ids:
            raise _Unavailable("CROSS_SOURCE_ROUTE_REFERENCE")
        if sheet_sources.get(record.get("source_sheet_id")) != record["source_file_id"]:
            raise _Unavailable("ROUTE_SHEET_SOURCE_MISMATCH")
        for candidate in record["candidates"]:
            if not isinstance(candidate, dict) or not candidate.get("source_file_id"):
                raise _Unavailable("INVALID_ROUTE_CANDIDATE")
            if candidate.get("sheet_id") not in sheet_sources:
                raise _Unavailable("UNKNOWN_ROUTE_SHEET")

    def walk(value):
        if isinstance(value, list):
            for item in value:
                walk(item)
        elif isinstance(value, dict):
            fid = value.get("source_file_id")
            if fid is not None and fid not in source_ids:
                raise _Unavailable("CROSS_SOURCE_ROUTE_REFERENCE")
            for key in (
                "sheet_id",
                "source_sheet_id",
                "suggested_target_sheet_id",
                "suggested_detail_sheet_id",
            ):
                sid = value.get(key)
                if sid is not None and sid not in sheet_sources:
                    raise _Unavailable("UNKNOWN_ROUTE_SHEET")
            # A candidate's sheet belongs to its own source, not necessarily to
            # the calling elevation source (cross-file navigation is allowed).
            if fid is not None and value.get("sheet_id") is not None:
                if sheet_sources[value["sheet_id"]] != fid:
                    raise _Unavailable("ROUTE_SHEET_SOURCE_MISMATCH")
            for item in value.values():
                walk(item)

    walk(routes)


def load_route_review_context(
    run_dir: Path,
    sheets: Sequence[Any],
    *,
    freshly_generated: bool = False,
) -> dict[str, Any]:
    """Return all verified alternatives, or an explicit unavailable diagnostic.

    ``freshly_generated`` is reserved for run immediately after it writes its
    route ledger. Resume must never use it to bless a historical ledger.
    """
    root = Path(run_dir)
    route_path = root / "analysis" / "detail_routes.json"
    receipt_path = root / "analysis" / "detail_route_review_receipt.json"
    index_path = root / "index" / "cad_index.json"
    panels_path = root / "analysis" / "panels.json"
    result: dict[str, Any] = {
        "schema_version": CONTEXT_SCHEMA,
        "state": "REVIEW",
        "available": False,
        "path_scope": "local_run_diagnostics",
        "mutates_takeoff": False,
        "commercial_effect": "NONE",
        "selection": None,
        "reason_codes": [],
        "route_path": str(route_path),
        "receipt_path": str(receipt_path),
        "routes": None,
        "record_count": 0,
        "limitations": [
            "Navigation candidates do not bind a physical component or measurement role.",
            "Candidate order and suggested navigation IDs are not component selections.",
            "No candidates, or an incomplete scan, cannot establish that a detail is absent.",
            "Receipts verify snapshot consistency, not authenticity or business acceptance.",
        ],
    }
    try:
        if not route_path.is_file():
            raise _Unavailable("ROUTE_CONTEXT_MISSING")
        if not freshly_generated and not receipt_path.is_file():
            raise _Unavailable("LEGACY_ROUTE_RECEIPT_MISSING")
        routes = _read_object(route_path, "INVALID_ROUTE_JSON")
        index = _read_object(index_path, "INVALID_INDEX_JSON")
        sources = _source_manifest(index)
        _validate_routes(routes, sources, sheets)
        current = {
            "schema_version": RECEIPT_SCHEMA,
            "producer_version": PRODUCER_VERSION,
            "route_schema_version": ROUTE_SCHEMA,
            "route_sha256": sha256_file(route_path),
            "index_sha256": sha256_file(index_path),
            "panels_sha256": sha256_file(panels_path),
            "sources": sources,
        }
        if freshly_generated:
            write_json_atomic(receipt_path, current)
        else:
            receipt = _read_object(receipt_path, "INVALID_ROUTE_RECEIPT_JSON")
            if (
                receipt.get("schema_version") != RECEIPT_SCHEMA
                or receipt.get("producer_version") != PRODUCER_VERSION
                or receipt.get("route_schema_version") != ROUTE_SCHEMA
            ):
                raise _Unavailable("UNSUPPORTED_ROUTE_RECEIPT_VERSION")
            for key in ("route_sha256", "index_sha256", "panels_sha256", "sources"):
                if receipt.get(key) != current[key]:
                    raise _Unavailable("ROUTE_RECEIPT_MISMATCH", key)
        # A second hash pass detects source changes while context was assembled.
        if _source_manifest(index) != sources:
            raise _Unavailable("SOURCE_CHANGED_DURING_CONTEXT")
        result.update(
            available=True,
            routes=_review_only(routes),
            record_count=len(routes["records"]),
            provenance=current,
            incomplete=bool(
                routes["native_frame_context"].get("incomplete")
                or routes["node_view_groups"].get("issues")
            ),
        )
    except _Unavailable as exc:
        result["reason_codes"] = [exc.code]
        result["detail"] = str(exc)
    except (OSError, ValueError, TypeError, KeyError) as exc:
        result["reason_codes"] = ["ROUTE_CONTEXT_UNREADABLE"]
        result["detail"] = f"{type(exc).__name__}: {exc}"
    return result
