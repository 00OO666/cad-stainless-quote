"""Build a reusable, searchable pre-index for a CAD drawing package.

The normal pipeline already produces a semantic ``cad_index.json``.  This
module turns that low-level snapshot into the next practical layer for a
human-like takeoff workflow: one catalog entry per drawing/sheet, exact text
and drawing-code lookup tables, dimension candidates, and optional cached
sheet previews.  The catalog never decides physical ownership or measurement
roles; it only makes the source package cheap to search and review.
"""

from __future__ import annotations

import copy
import hashlib
import json
import re
import sqlite3
import unicodedata
from collections import Counter, defaultdict
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from PIL import Image

from .io import sha256_file, write_json_atomic
from .render import _render_profile, render_regions

CATALOG_SCHEMA_VERSION = "1.0"
PREVIEW_RENDERER_VERSION = "drawing-catalog-render-v1"

_TOKEN_RE = re.compile(
    r"MT[-_/]?[A-Z0-9]+|"
    r"(?:[A-Z]{0,4}[-_/])?(?:EL|E|D|P|A|B|F|L)[-_]?[A-Z0-9]+|"
    r"[A-Z]{2,}[A-Z0-9_.-]*|"
    r"[\u4e00-\u9fff]{2,}",
    re.IGNORECASE,
)
_MT_RE = re.compile(r"(?<![A-Z0-9])MT[-_/]?[A-Z0-9]+(?![A-Z0-9])", re.IGNORECASE)
_DRAWING_CODE_RE = re.compile(
    r"(?<![A-Z0-9])"
    r"(?:[A-Z]{0,4}[-_/])?(?:EL|E|D|P|A|B|F|L)[-_]?[A-Z0-9]+"
    r"(?:[-_/][A-Z0-9]+){0,3}"
    r"(?![A-Z0-9])",
    re.IGNORECASE,
)


def _text_key(value: object) -> str:
    normalized = unicodedata.normalize("NFKC", str(value or ""))
    return re.sub(r"\s+", " ", normalized).strip().casefold()


def _tokens(value: object) -> list[str]:
    text = unicodedata.normalize("NFKC", str(value or ""))
    output: set[str] = set()
    full = _text_key(text)
    if full:
        output.add(full)
    for match in _TOKEN_RE.finditer(text):
        token = _text_key(match.group(0))
        if token:
            output.add(token)
    return sorted(output)


def _codes(value: object, pattern: re.Pattern[str]) -> list[str]:
    text = unicodedata.normalize("NFKC", str(value or ""))
    return sorted({_text_key(match.group(0)).upper() for match in pattern.finditer(text)})


def preview_cache_key(
    *,
    source_sha256: str,
    sheet_id: str,
    layout: str | None,
    bbox: Sequence[float],
    target_px: int,
    margin_ratio: float,
    render_profile: str,
    renderer_version: str = PREVIEW_RENDERER_VERSION,
) -> str:
    """Return a content-addressed preview key.

    A filename alone is never a cache key.  Source content, sheet identity,
    geometry, profile, target size and renderer version all participate, so a
    changed drawing or render request cannot silently reuse an old image.
    """

    payload = {
        "source_sha256": str(source_sha256),
        "sheet_id": str(sheet_id),
        "layout": str(layout or "Model"),
        "bbox": [round(float(value), 9) for value in bbox],
        "target_px": int(target_px),
        "margin_ratio": round(float(margin_ratio), 9),
        "render_profile": str(render_profile),
        "renderer_version": str(renderer_version),
    }
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:32]


def _source_payloads(index_payload: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    raw = index_payload.get("sources", [])
    if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes, bytearray)):
        raise ValueError("CAD index sources must be an array")
    return [value for value in raw if isinstance(value, Mapping)]


def _entity_payloads(source: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    raw = source.get("entities", [])
    if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes, bytearray)):
        return []
    return [value for value in raw if isinstance(value, Mapping)]


def _sheet_payloads(source: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    raw = source.get("sheets", [])
    if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes, bytearray)):
        return []
    return [value for value in raw if isinstance(value, Mapping)]


def build_drawing_catalog(index_payload: Mapping[str, Any]) -> dict[str, Any]:
    """Create a deterministic sheet/text/MT/dimension catalog from an index.

    The output intentionally keeps all candidate entity IDs.  It is a lookup
    layer, not a hidden matching model: downstream stages must still confirm
    the physical component and measurement role.
    """

    source_rows = _source_payloads(index_payload)
    sheets: list[dict[str, Any]] = []
    source_rows_out: list[dict[str, Any]] = []
    term_hits: dict[str, dict[str, set[str]]] = defaultdict(lambda: defaultdict(set))
    mt_hits: dict[str, set[str]] = defaultdict(set)
    drawing_hits: dict[str, set[str]] = defaultdict(set)
    dimensions: list[dict[str, Any]] = []
    total_entities = 0

    for source in sorted(source_rows, key=lambda value: str(value.get("source_file_id", ""))):
        source_id = str(source.get("source_file_id") or "")
        if not source_id:
            continue
        source_rows_out.append(
            {
                "source_file_id": source_id,
                "source_path": str(source.get("source_path") or ""),
                "source_sha256": str(source.get("source_sha256") or ""),
                "dxf_version": source.get("dxf_version"),
                "units_code": source.get("units_code"),
                "units": source.get("units"),
                "audit_error_count": int(source.get("audit_error_count") or 0),
                "audit_fix_count": int(source.get("audit_fix_count") or 0),
                "recovered": bool(source.get("recovered")),
                "warnings": list(source.get("warnings") or []),
            }
        )
        entities = _entity_payloads(source)
        total_entities += len(entities)
        by_sheet: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
        for entity in entities:
            sheet_id = str(entity.get("sheet_id") or "")
            if sheet_id:
                by_sheet[sheet_id].append(entity)

        for raw_sheet in sorted(
            _sheet_payloads(source), key=lambda value: str(value.get("id", ""))
        ):
            sheet_id = str(raw_sheet.get("id") or "")
            if not sheet_id:
                continue
            sheet_entities = by_sheet.get(sheet_id, [])
            entity_counts = Counter(
                str(entity.get("entity_type") or "UNKNOWN") for entity in sheet_entities
            )
            text_records: list[dict[str, Any]] = []
            sheet_terms: set[str] = set()
            mt_codes: set[str] = set()
            drawing_codes: set[str] = set()
            sheet_dimensions: list[str] = []
            for entity in sorted(sheet_entities, key=lambda value: str(value.get("id", ""))):
                entity_id = str(entity.get("id") or "")
                text = str(entity.get("text") or "").strip()
                if text:
                    entity_terms = _tokens(text)
                    sheet_terms.update(entity_terms)
                    for term in entity_terms:
                        term_hits[term][sheet_id].add(entity_id)
                    mt_codes.update(_codes(text, _MT_RE))
                    drawing_codes.update(_codes(text, _DRAWING_CODE_RE))
                    text_records.append(
                        {
                            "entity_id": entity_id,
                            "entity_type": entity.get("entity_type"),
                            "text": text,
                            "handle": entity.get("handle"),
                            "bbox": entity.get("bbox"),
                            "insert": entity.get("insert"),
                        }
                    )
                value = entity.get("value")
                if value is not None or str(entity.get("entity_type") or "").upper() in {
                    "DIMENSION",
                    "ARC_DIMENSION",
                    "LARGE_RADIAL_DIMENSION",
                }:
                    record = {
                        "entity_id": entity_id,
                        "source_file_id": source_id,
                        "sheet_id": sheet_id,
                        "entity_type": entity.get("entity_type"),
                        "handle": entity.get("handle"),
                        "value": value,
                        "text_override": entity.get("text_override"),
                        "bbox": entity.get("bbox"),
                        "insert": entity.get("insert"),
                        "geometry": entity.get("geometry") or {},
                    }
                    dimensions.append(record)
                    sheet_dimensions.append(entity_id)

            for code in mt_codes:
                mt_hits[code].add(sheet_id)
            for code in drawing_codes:
                drawing_hits[code].add(sheet_id)
            sheets.append(
                {
                    "id": sheet_id,
                    "source_file_id": source_id,
                    "drawing_number": raw_sheet.get("drawing_number"),
                    "title": raw_sheet.get("title"),
                    "kind": raw_sheet.get("kind", "unknown"),
                    "layout": raw_sheet.get("layout"),
                    "viewport_handle": raw_sheet.get("viewport_handle"),
                    "bbox": raw_sheet.get("bbox"),
                    "confidence": raw_sheet.get("confidence", 0.0),
                    "evidence": list(raw_sheet.get("evidence") or []),
                    "entity_count": len(sheet_entities),
                    "entity_counts": dict(sorted(entity_counts.items())),
                    "text_count": len(text_records),
                    "dimension_count": len(sheet_dimensions),
                    "dimension_entity_ids": sorted(sheet_dimensions),
                    "text_records": text_records,
                    "terms": sorted(sheet_terms),
                    "mt_codes": sorted(mt_codes),
                    "drawing_codes": sorted(drawing_codes),
                }
            )

    source_map = {str(value["source_file_id"]): value for value in source_rows_out}
    for sheet in sheets:
        source = source_map.get(str(sheet["source_file_id"]), {})
        bbox = sheet.get("bbox")
        if (
            isinstance(bbox, Sequence)
            and not isinstance(bbox, (str, bytes, bytearray))
            and len(bbox) == 4
        ):
            sheet["preview_cache_seed"] = preview_cache_key(
                source_sha256=str(source.get("source_sha256") or ""),
                sheet_id=str(sheet["id"]),
                layout=sheet.get("layout"),
                bbox=bbox,
                target_px=1800,
                margin_ratio=0.02,
                render_profile="cad-dark",
            )
        else:
            sheet["preview_cache_seed"] = None

    catalog = {
        "schema_version": CATALOG_SCHEMA_VERSION,
        "purpose": "reusable_cad_drawing_preindex",
        "warning": (
            "This catalog accelerates lookup only. It does not confirm physical component "
            "ownership, "
            "dimension roles, quantities, or quotation rows."
        ),
        "source_count": len(source_rows_out),
        "sheet_count": len(sheets),
        "entity_count": total_entities,
        "dimension_count": len(dimensions),
        "term_count": len(term_hits),
        "mt_code_count": len(mt_hits),
        "drawing_code_count": len(drawing_hits),
        "sources": source_rows_out,
        "sheets": sorted(sheets, key=lambda value: str(value["id"])),
        "dimensions": sorted(dimensions, key=lambda value: str(value["entity_id"])),
        "terms": {
            term: [
                {"sheet_id": sheet_id, "entity_ids": sorted(entity_ids)}
                for sheet_id, entity_ids in sorted(sheet_map.items())
            ]
            for term, sheet_map in sorted(term_hits.items())
        },
        "mt_index": {code: sorted(sheet_ids) for code, sheet_ids in sorted(mt_hits.items())},
        "drawing_index": {
            code: sorted(sheet_ids) for code, sheet_ids in sorted(drawing_hits.items())
        },
        "previews": {
            "schema_version": "1.0",
            "status": "NOT_RENDERED",
            "rendered_count": 0,
            "records": {},
        },
    }
    return catalog


def write_drawing_catalog_sqlite(catalog: Mapping[str, Any], path: Path | str) -> Path:
    """Persist the catalog as a small query-friendly SQLite snapshot."""

    database_path = Path(path).expanduser().resolve()
    database_path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(database_path) as connection:
        connection.executescript(
            """
            PRAGMA journal_mode=WAL;
            DROP TABLE IF EXISTS catalog_meta;
            DROP TABLE IF EXISTS catalog_sources;
            DROP TABLE IF EXISTS catalog_sheets;
            DROP TABLE IF EXISTS catalog_terms;
            DROP TABLE IF EXISTS catalog_dimensions;
            CREATE TABLE catalog_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE catalog_sources (
                source_file_id TEXT PRIMARY KEY,
                source_path TEXT NOT NULL,
                source_sha256 TEXT NOT NULL,
                units TEXT,
                warnings_json TEXT NOT NULL
            );
            CREATE TABLE catalog_sheets (
                id TEXT PRIMARY KEY,
                source_file_id TEXT NOT NULL,
                drawing_number TEXT,
                title TEXT,
                kind TEXT NOT NULL,
                layout TEXT,
                bbox_json TEXT,
                entity_count INTEGER NOT NULL,
                text_count INTEGER NOT NULL,
                dimension_count INTEGER NOT NULL,
                mt_codes_json TEXT NOT NULL,
                drawing_codes_json TEXT NOT NULL
            );
            CREATE TABLE catalog_terms (
                term TEXT NOT NULL,
                sheet_id TEXT NOT NULL,
                entity_ids_json TEXT NOT NULL,
                PRIMARY KEY (term, sheet_id)
            );
            CREATE TABLE catalog_dimensions (
                entity_id TEXT PRIMARY KEY,
                source_file_id TEXT NOT NULL,
                sheet_id TEXT NOT NULL,
                entity_type TEXT NOT NULL,
                handle TEXT,
                value REAL,
                text_override TEXT,
                bbox_json TEXT,
                geometry_json TEXT NOT NULL
            );
            CREATE INDEX idx_catalog_sheets_kind ON catalog_sheets(kind);
            CREATE INDEX idx_catalog_sheets_drawing_number ON catalog_sheets(drawing_number);
            CREATE INDEX idx_catalog_terms_term ON catalog_terms(term);
            CREATE INDEX idx_catalog_dimensions_sheet ON catalog_dimensions(sheet_id);
            """
        )
        for key in ("schema_version", "purpose", "source_count", "sheet_count", "entity_count"):
            connection.execute(
                "INSERT INTO catalog_meta(key, value) VALUES (?, ?)",
                (key, json.dumps(catalog.get(key), ensure_ascii=False)),
            )
        connection.executemany(
            "INSERT INTO catalog_sources VALUES (?, ?, ?, ?, ?)",
            [
                (
                    str(source.get("source_file_id")),
                    str(source.get("source_path") or ""),
                    str(source.get("source_sha256") or ""),
                    source.get("units"),
                    json.dumps(source.get("warnings") or [], ensure_ascii=False),
                )
                for source in catalog.get("sources", [])
            ],
        )
        connection.executemany(
            "INSERT INTO catalog_sheets VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                (
                    str(sheet.get("id")),
                    str(sheet.get("source_file_id")),
                    sheet.get("drawing_number"),
                    sheet.get("title"),
                    str(sheet.get("kind") or "unknown"),
                    sheet.get("layout"),
                    json.dumps(sheet.get("bbox"), ensure_ascii=False),
                    int(sheet.get("entity_count") or 0),
                    int(sheet.get("text_count") or 0),
                    int(sheet.get("dimension_count") or 0),
                    json.dumps(sheet.get("mt_codes") or [], ensure_ascii=False),
                    json.dumps(sheet.get("drawing_codes") or [], ensure_ascii=False),
                )
                for sheet in catalog.get("sheets", [])
            ],
        )
        term_rows = []
        for term, hits in (catalog.get("terms") or {}).items():
            for hit in hits:
                term_rows.append(
                    (
                        str(term),
                        str(hit.get("sheet_id")),
                        json.dumps(hit.get("entity_ids") or [], ensure_ascii=False),
                    )
                )
        connection.executemany("INSERT INTO catalog_terms VALUES (?, ?, ?)", term_rows)
        connection.executemany(
            "INSERT INTO catalog_dimensions VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                (
                    str(value.get("entity_id")),
                    str(value.get("source_file_id")),
                    str(value.get("sheet_id")),
                    str(value.get("entity_type") or "UNKNOWN"),
                    value.get("handle"),
                    value.get("value"),
                    value.get("text_override"),
                    json.dumps(value.get("bbox"), ensure_ascii=False),
                    json.dumps(value.get("geometry") or {}, ensure_ascii=False),
                )
                for value in catalog.get("dimensions", [])
            ],
        )
        connection.commit()
    return database_path


def _load_preview_manifest(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def render_sheet_previews(
    catalog: Mapping[str, Any],
    output_dir: Path | str,
    *,
    maximum: int = 500,
    target_px: int = 1_800,
    margin_ratio: float = 0.02,
    render_profile: str = "cad-dark",
) -> dict[str, Any]:
    """Render or reuse deterministic per-sheet previews.

    Only sheets with a source path and bbox are eligible.  Cache reuse is
    allowed only when the content-addressed render key, source hash, bbox and
    renderer parameters all match.  Missing or failed previews remain explicit
    and never promote a quotation row.
    """

    if maximum < 1:
        raise ValueError("maximum must be at least 1")
    if target_px < 256:
        raise ValueError("target_px must be at least 256")
    if not 0 <= margin_ratio <= 1:
        raise ValueError("margin_ratio must be between 0 and 1")
    profile = _render_profile(render_profile)
    destination = Path(output_dir).expanduser().resolve()
    preview_root = destination / "previews"
    preview_root.mkdir(parents=True, exist_ok=True)
    existing_manifest = _load_preview_manifest(destination / "preview_index.json")
    existing_records = existing_manifest.get("records", {})
    if not isinstance(existing_records, Mapping):
        existing_records = {}

    result = copy.deepcopy(dict(catalog))
    source_by_id = {
        str(source.get("source_file_id")): source for source in result.get("sources", [])
    }
    eligible: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    for sheet in sorted(result.get("sheets", []), key=lambda value: str(value.get("id", ""))):
        source = source_by_id.get(str(sheet.get("source_file_id")), {})
        bbox = sheet.get("bbox")
        if (
            not str(source.get("source_path") or "").strip()
            or not Path(str(source.get("source_path"))).is_file()
            or not isinstance(bbox, Sequence)
            or isinstance(bbox, (str, bytes, bytearray))
            or len(bbox) != 4
        ):
            skipped.append(
                {
                    "sheet_id": str(sheet.get("id")),
                    "status": "UNAVAILABLE",
                    "reason": "missing_source_or_bbox",
                }
            )
            continue
        eligible.append(sheet)

    selected = eligible[:maximum]
    truncated = eligible[maximum:]
    grouped: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    records: dict[str, dict[str, Any]] = {}
    failures: list[dict[str, Any]] = []
    for sheet in selected:
        source = source_by_id[str(sheet["source_file_id"])]
        layout = str(sheet.get("layout") or "Model")
        cache_key = preview_cache_key(
            source_sha256=str(source.get("source_sha256") or ""),
            sheet_id=str(sheet["id"]),
            layout=layout,
            bbox=sheet["bbox"],
            target_px=target_px,
            margin_ratio=margin_ratio,
            render_profile=profile["name"],
        )
        previous = existing_records.get(str(sheet["id"]))
        previous_path = (
            destination / str(previous.get("relative_path"))
            if isinstance(previous, Mapping) and previous.get("relative_path")
            else None
        )
        if (
            isinstance(previous, Mapping)
            and previous.get("cache_key") == cache_key
            and previous_path is not None
            and previous_path.is_file()
            and previous.get("image_sha256") == sha256_file(previous_path)
        ):
            records[str(sheet["id"])] = dict(previous)
            continue
        grouped[(str(sheet["source_file_id"]), layout)].append(sheet)

    for (source_id, layout), group in sorted(grouped.items()):
        source = source_by_id[source_id]
        source_token = hashlib.sha256(source_id.encode("utf-8")).hexdigest()[:16]
        group_dir = preview_root / f"source_{source_token}"
        group_dir.mkdir(parents=True, exist_ok=True)
        regions = {str(sheet["id"]): sheet["bbox"] for sheet in group}
        try:
            rendered = render_regions(
                Path(str(source["source_path"])).resolve(),
                regions,
                group_dir,
                layout=layout,
                margin_ratio=margin_ratio,
                target_px=target_px,
                mark_center=False,
                render_profile=profile["name"],
            )
        except Exception as exc:  # pragma: no cover - renderer boundary
            failures.append(
                {
                    "source_file_id": source_id,
                    "layout": layout,
                    "sheet_ids": sorted(regions),
                    "error_type": type(exc).__name__,
                    "message": str(exc),
                }
            )
            continue
        rendered_regions = rendered.get("regions", {})
        for sheet in group:
            sheet_id = str(sheet["id"])
            raw_record = rendered_regions.get(sheet_id)
            if not isinstance(raw_record, Mapping):
                failures.append(
                    {
                        "source_file_id": source_id,
                        "layout": layout,
                        "sheet_ids": [sheet_id],
                        "error_type": "MISSING_RENDER",
                        "message": "renderer returned no region for eligible sheet",
                    }
                )
                continue
            image_path = group_dir / str(raw_record.get("file") or "")
            if not image_path.is_file():
                continue
            with Image.open(image_path) as image:
                width_px, height_px = image.size
            cache_key = preview_cache_key(
                source_sha256=str(source.get("source_sha256") or ""),
                sheet_id=sheet_id,
                layout=layout,
                bbox=sheet["bbox"],
                target_px=target_px,
                margin_ratio=margin_ratio,
                render_profile=profile["name"],
            )
            records[sheet_id] = {
                "sheet_id": sheet_id,
                "source_file_id": source_id,
                "source_sha256": str(source.get("source_sha256") or ""),
                "layout": layout,
                "bbox": list(sheet["bbox"]),
                "cache_key": cache_key,
                "render_profile": profile["name"],
                "target_px": target_px,
                "margin_ratio": margin_ratio,
                "renderer_version": PREVIEW_RENDERER_VERSION,
                "relative_path": str(image_path.relative_to(destination)).replace("\\", "/"),
                "absolute_path": str(image_path.resolve()),
                "image_sha256": sha256_file(image_path),
                "width_px": width_px,
                "height_px": height_px,
                "status": "RENDERED",
            }

    for sheet in result.get("sheets", []):
        sheet_id = str(sheet.get("id"))
        sheet["preview"] = records.get(
            sheet_id,
            next((value for value in skipped if value.get("sheet_id") == sheet_id), None),
        )
    preview_payload = {
        "schema_version": "1.0",
        "purpose": "cached_cad_sheet_previews",
        "render_profile": profile["name"],
        "target_px": target_px,
        "margin_ratio": margin_ratio,
        "renderer_version": PREVIEW_RENDERER_VERSION,
        "requested_count": len(selected),
        "eligible_count": len(eligible),
        "rendered_count": len(records),
        "reused_count": sum(
            1
            for value in records.values()
            if isinstance(existing_records.get(str(value.get("sheet_id"))), Mapping)
            and existing_records[str(value.get("sheet_id"))].get("cache_key")
            == value.get("cache_key")
        ),
        "truncated_count": len(truncated),
        "truncated_sheet_ids": [str(value.get("id")) for value in truncated],
        "skipped": skipped,
        "failures": failures,
        "records": records,
    }
    result["previews"] = preview_payload
    result["preview_count"] = len(records)
    write_json_atomic(destination / "preview_index.json", preview_payload)
    write_json_atomic(destination / "drawing_catalog.json", result)
    return result


def search_drawing_catalog(
    catalog: Mapping[str, Any],
    query: str,
    *,
    kind: str | None = None,
    limit: int = 50,
) -> list[dict[str, Any]]:
    """Search exact indexed terms/codes without inventing a component match."""

    if limit < 1:
        raise ValueError("limit must be at least 1")
    normalized = _text_key(query)
    if not normalized:
        return []
    sheet_map = {
        str(sheet.get("id")): sheet
        for sheet in catalog.get("sheets", [])
        if isinstance(sheet, Mapping)
    }
    hits: dict[str, dict[str, Any]] = {}
    exact_terms = catalog.get("terms", {})
    if isinstance(exact_terms, Mapping):
        for term, entries in exact_terms.items():
            term_key = _text_key(term)
            score = 1.0 if term_key == normalized else 0.8 if normalized in term_key else 0.0
            if score == 0.0:
                continue
            if not isinstance(entries, Sequence) or isinstance(entries, (str, bytes, bytearray)):
                continue
            for entry in entries:
                if not isinstance(entry, Mapping):
                    continue
                sheet_id = str(entry.get("sheet_id") or "")
                sheet = sheet_map.get(sheet_id)
                if sheet is None or (kind and str(sheet.get("kind")) != kind):
                    continue
                current = hits.setdefault(
                    sheet_id,
                    {
                        "sheet_id": sheet_id,
                        "source_file_id": sheet.get("source_file_id"),
                        "drawing_number": sheet.get("drawing_number"),
                        "title": sheet.get("title"),
                        "kind": sheet.get("kind"),
                        "layout": sheet.get("layout"),
                        "matched_terms": [],
                        "entity_ids": set(),
                        "score": 0.0,
                    },
                )
                current["score"] = max(float(current["score"]), score)
                current["matched_terms"].append(str(term))
                current["entity_ids"].update(str(value) for value in entry.get("entity_ids", []))
    output: list[dict[str, Any]] = []
    for value in hits.values():
        output.append(
            {
                **value,
                "matched_terms": sorted(set(value["matched_terms"])),
                "entity_ids": sorted(value["entity_ids"]),
                "score": round(float(value["score"]), 3),
            }
        )
    return sorted(output, key=lambda value: (-value["score"], str(value["sheet_id"])))[:limit]


__all__ = [
    "CATALOG_SCHEMA_VERSION",
    "PREVIEW_RENDERER_VERSION",
    "build_drawing_catalog",
    "preview_cache_key",
    "render_sheet_previews",
    "search_drawing_catalog",
    "write_drawing_catalog_sqlite",
]
