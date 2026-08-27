"""Normalize DXF layouts and evidence-bearing entities into JSON and SQLite.

Only semantic entities needed by later evidence stages are indexed here.  The
full geometry remains in the original/converted drawing and can always be
revisited using the source id, layout name, handle, insertion point and bbox.
"""

from __future__ import annotations

import hashlib
import json
import math
import sqlite3
import time
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import ezdxf
from ezdxf import bbox as ezbbox
from ezdxf import recover

from .classifier import classify_sheet
from .io import sha256_file, write_json_atomic
from .models import CadEntity, Sheet

INDEXED_ENTITY_TYPES = frozenset(
    {
        "TEXT",
        "MTEXT",
        "ATTRIB",
        "ATTDEF",
        "DIMENSION",
        "ARC_DIMENSION",
        "LARGE_RADIAL_DIMENSION",
        "LEADER",
        "MLEADER",
        "MULTILEADER",
        "INSERT",
        "VIEWPORT",
    }
)

UNIT_NAMES = {
    0: "unitless",
    1: "inches",
    2: "feet",
    3: "miles",
    4: "millimeters",
    5: "centimeters",
    6: "meters",
    7: "kilometers",
}


@dataclass(slots=True)
class CadIndexResult:
    source_path: str
    source_file_id: str
    source_sha256: str
    dxf_version: str
    units_code: int
    units: str
    sheets: list[Sheet] = field(default_factory=list)
    entities: list[CadEntity] = field(default_factory=list)
    entity_counts: dict[str, int] = field(default_factory=dict)
    audit_error_count: int = 0
    audit_fix_count: int = 0
    recovered: bool = False
    virtual_entity_count: int = 0
    block_expansion_truncated: bool = False
    elapsed_seconds: float = 0.0
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "source_path": self.source_path,
            "source_file_id": self.source_file_id,
            "source_sha256": self.source_sha256,
            "dxf_version": self.dxf_version,
            "units_code": self.units_code,
            "units": self.units,
            "sheets": [sheet.model_dump(mode="json") for sheet in self.sheets],
            "entities": [entity.model_dump(mode="json") for entity in self.entities],
            "entity_counts": self.entity_counts,
            "audit_error_count": self.audit_error_count,
            "audit_fix_count": self.audit_fix_count,
            "recovered": self.recovered,
            "virtual_entity_count": self.virtual_entity_count,
            "block_expansion_truncated": self.block_expansion_truncated,
            "elapsed_seconds": self.elapsed_seconds,
            "warnings": self.warnings,
        }


@dataclass(slots=True)
class CadIndexBundle:
    results: list[CadIndexResult]

    @property
    def sheets(self) -> list[Sheet]:
        return [sheet for result in self.results for sheet in result.sheets]

    @property
    def entities(self) -> list[CadEntity]:
        return [entity for result in self.results for entity in result.entities]

    @property
    def entity_counts(self) -> dict[str, int]:
        counts: Counter[str] = Counter()
        for result in self.results:
            counts.update(result.entity_counts)
        return dict(sorted(counts.items()))

    def to_dict(self) -> dict[str, Any]:
        return {
            "source_count": len(self.results),
            "sheet_count": len(self.sheets),
            "entity_count": len(self.entities),
            "entity_counts": self.entity_counts,
            "sources": [result.to_dict() for result in self.results],
        }


def _stable_id(prefix: str, *parts: object) -> str:
    canonical = json.dumps(parts, ensure_ascii=False, sort_keys=True, default=str)
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:24]
    return f"{prefix}:{digest}"


def _finite(value: object) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return round(number, 9) if math.isfinite(number) else None


def _dxf_get(entity: Any, name: str, default: Any = None) -> Any:
    try:
        return entity.dxf.get(name, default)
    except (AttributeError, ezdxf.DXFError):
        return default


def _point(value: object) -> tuple[float, float] | None:
    if value is None:
        return None
    try:
        if hasattr(value, "x") and hasattr(value, "y"):
            raw_x = value.x  # type: ignore[attr-defined]
            raw_y = value.y  # type: ignore[attr-defined]
        else:
            raw_x = value[0]  # type: ignore[index]
            raw_y = value[1]  # type: ignore[index]
        x = _finite(raw_x)
        y = _finite(raw_y)
    except (AttributeError, IndexError, TypeError):
        return None
    if x is None or y is None:
        return None
    return (x, y)


def _point3(value: object) -> list[float] | None:
    point2 = _point(value)
    if point2 is None:
        return None
    z = _finite(getattr(value, "z", 0.0)) or 0.0
    return [point2[0], point2[1], z]


def _clean_text(entity: Any) -> str | None:
    try:
        entity_type = entity.dxftype()
        if entity_type == "MTEXT":
            text = entity.plain_text()
        elif entity_type in {"TEXT", "ATTRIB", "ATTDEF"}:
            text = _dxf_get(entity, "text", "")
        elif entity_type in {"MLEADER", "MULTILEADER"} and entity.has_mtext_content:
            text = entity.get_mtext_content()
        else:
            return None
    except Exception:
        return None
    normalized = str(text).replace("\x00", "").strip()
    return normalized or None


def _entity_bbox(entity: Any, cache: ezbbox.Cache) -> tuple[float, float, float, float] | None:
    entity_type = entity.dxftype()
    try:
        if entity_type == "VIEWPORT":
            center = _point(_dxf_get(entity, "center"))
            width = _finite(_dxf_get(entity, "width"))
            height = _finite(_dxf_get(entity, "height"))
            if center and width is not None and height is not None:
                return (
                    center[0] - width / 2,
                    center[1] - height / 2,
                    center[0] + width / 2,
                    center[1] + height / 2,
                )
        if entity_type == "INSERT":
            insert = _point(_dxf_get(entity, "insert"))
            if insert:
                return (insert[0], insert[1], insert[0], insert[1])
        if entity_type in {"DIMENSION", "ARC_DIMENSION", "LARGE_RADIAL_DIMENSION"}:
            points = [
                _point(_dxf_get(entity, name))
                for name in (
                    "defpoint",
                    "defpoint2",
                    "defpoint3",
                    "defpoint4",
                    "text_midpoint",
                )
            ]
            usable = [point for point in points if point is not None]
            if usable:
                xs, ys = zip(*usable, strict=True)
                return (min(xs), min(ys), max(xs), max(ys))
        if entity_type in {"LEADER", "MLEADER", "MULTILEADER"}:
            vertices = _leader_vertices(entity)
            if vertices:
                xs = [vertex[0] for vertex in vertices]
                ys = [vertex[1] for vertex in vertices]
                return (min(xs), min(ys), max(xs), max(ys))
        extents = ezbbox.extents([entity], fast=True, cache=cache)
        if extents.has_data:
            values = (
                _finite(extents.extmin.x),
                _finite(extents.extmin.y),
                _finite(extents.extmax.x),
                _finite(extents.extmax.y),
            )
            if all(value is not None for value in values):
                return values  # type: ignore[return-value]
    except Exception:
        pass

    points: list[tuple[float, float]] = []
    for name in (
        "insert",
        "defpoint",
        "defpoint2",
        "defpoint3",
        "defpoint4",
        "text_midpoint",
        "start",
        "end",
        "center",
    ):
        point = _point(_dxf_get(entity, name))
        if point is not None:
            points.append(point)
    if not points:
        return None
    xs, ys = zip(*points, strict=True)
    return (min(xs), min(ys), max(xs), max(ys))


def _dimension_geometry(entity: Any) -> tuple[float | None, str | None, dict[str, Any]]:
    try:
        geometric_value = _finite(entity.get_measurement())
    except Exception:
        geometric_value = None
    stored_value = _finite(_dxf_get(entity, "actual_measurement"))
    raw_value = stored_value if stored_value is not None and stored_value > 0 else geometric_value
    try:
        measurement_factor = _finite(entity.override().get("dimlfac", 1.0))
    except Exception:
        measurement_factor = 1.0
    if measurement_factor is None:
        measurement_factor = 1.0
    value = (
        raw_value * measurement_factor
        if raw_value is not None and math.isfinite(raw_value)
        else None
    )
    text_override = _dxf_get(entity, "text")
    if text_override in {"", "<>"}:
        text_override = None
    geometry: dict[str, Any] = {
        "dimtype": _dxf_get(entity, "dimtype"),
        "raw_measurement": raw_value,
        "geometric_measurement": geometric_value,
        "measurement_factor": measurement_factor,
        "display_measurement": value,
    }
    for name in ("defpoint", "defpoint2", "defpoint3", "defpoint4", "text_midpoint"):
        point = _point3(_dxf_get(entity, name))
        if point is not None:
            geometry[name] = point
    return value, str(text_override) if text_override is not None else None, geometry


def _leader_vertices(entity: Any) -> list[list[float]]:
    vertices: list[list[float]] = []
    entity_type = entity.dxftype()
    try:
        if entity_type == "LEADER":
            for vertex in entity.vertices:
                point = _point3(vertex)
                if point is not None:
                    vertices.append(point)
        else:
            context = entity.context
            for leader in context.leaders:
                for line in leader.lines:
                    for vertex in line.vertices:
                        point = _point3(vertex)
                        if point is not None:
                            vertices.append(point)
    except Exception:
        pass
    return vertices


def _viewport_geometry(entity: Any) -> dict[str, Any]:
    geometry: dict[str, Any] = {
        "width": _finite(_dxf_get(entity, "width")),
        "height": _finite(_dxf_get(entity, "height")),
        "status": _dxf_get(entity, "status"),
        "viewport_id": _dxf_get(entity, "id"),
        "view_height": _finite(_dxf_get(entity, "view_height")),
        "view_twist_angle": _finite(_dxf_get(entity, "view_twist_angle")),
        "custom_scale": _finite(_dxf_get(entity, "custom_scale")),
    }
    for name in ("view_center_point", "view_target_point", "view_direction_vector"):
        point = _point3(_dxf_get(entity, name))
        if point is not None:
            geometry[name] = point
    try:
        limits = tuple(_finite(value) for value in entity.get_modelspace_limits())
        if len(limits) == 4 and all(value is not None for value in limits):
            geometry["model_bbox"] = list(limits)
            target = _point(_dxf_get(entity, "view_target_point"))
            if target and (abs(target[0]) > 1e-9 or abs(target[1]) > 1e-9):
                geometry["model_bbox_target_shifted"] = [
                    limits[0] + target[0],  # type: ignore[operator]
                    limits[1] + target[1],  # type: ignore[operator]
                    limits[2] + target[0],  # type: ignore[operator]
                    limits[3] + target[1],  # type: ignore[operator]
                ]
    except Exception:
        pass
    return geometry


def _insert_geometry(entity: Any) -> dict[str, Any]:
    return {
        "name": _dxf_get(entity, "name"),
        "rotation": _finite(_dxf_get(entity, "rotation")),
        "xscale": _finite(_dxf_get(entity, "xscale")),
        "yscale": _finite(_dxf_get(entity, "yscale")),
        "zscale": _finite(_dxf_get(entity, "zscale")),
        "row_count": _dxf_get(entity, "row_count"),
        "column_count": _dxf_get(entity, "column_count"),
        "attribute_handles": [_dxf_get(attribute, "handle") for attribute in entity.attribs],
    }


def _record_entity(
    entity: Any,
    source_file_id: str,
    sheet_id: str,
    space: str,
    cache: ezbbox.Cache,
    *,
    parent_insert_handle: str | None = None,
    virtual_context: Mapping[str, Any] | None = None,
) -> CadEntity:
    entity_type = entity.dxftype()
    handle = _dxf_get(entity, "handle")
    insert = _point(_dxf_get(entity, "insert"))
    value = None
    text_override = None
    geometry: dict[str, Any] = {"layout": space.removeprefix("paper:")}

    if entity_type in {"DIMENSION", "ARC_DIMENSION", "LARGE_RADIAL_DIMENSION"}:
        value, text_override, geometry_values = _dimension_geometry(entity)
        geometry.update(geometry_values)
    elif entity_type in {"LEADER", "MLEADER", "MULTILEADER"}:
        vertices = _leader_vertices(entity)
        geometry.update(
            {
                "vertices": vertices,
                "annotation_handle": _dxf_get(entity, "annotation_handle"),
            }
        )
        if vertices:
            insert = (vertices[0][0], vertices[0][1])
    elif entity_type == "INSERT":
        geometry.update(_insert_geometry(entity))
    elif entity_type in {"ATTRIB", "ATTDEF"}:
        geometry.update(
            {
                "tag": _dxf_get(entity, "tag"),
                "rotation": _finite(_dxf_get(entity, "rotation")),
                "parent_insert_handle": parent_insert_handle,
            }
        )
    elif entity_type == "VIEWPORT":
        geometry.update(_viewport_geometry(entity))
        insert = _point(_dxf_get(entity, "center"))
    elif entity_type in {"TEXT", "MTEXT"}:
        geometry.update(
            {
                "height": _finite(
                    _dxf_get(entity, "char_height")
                    if entity_type == "MTEXT"
                    else _dxf_get(entity, "height")
                ),
                "rotation": _finite(_dxf_get(entity, "rotation")),
                "style": _dxf_get(entity, "style"),
            }
        )

    if virtual_context:
        geometry.update(virtual_context)

    entity_bbox = _entity_bbox(entity, cache)
    identity_basis: object = handle
    if virtual_context:
        identity_basis = {
            "source_block_entity_handle": virtual_context.get(
                "source_block_entity_handle"
            ),
            "parent_insert_id": virtual_context.get("parent_insert_id"),
            "block_path": virtual_context.get("block_path"),
            "virtual_ordinal": virtual_context.get("virtual_ordinal"),
            "type": entity_type,
        }
    elif not handle:
        identity_basis = {
            "type": entity_type,
            "insert": insert,
            "bbox": entity_bbox,
            "text": _clean_text(entity),
            "geometry": geometry,
        }
    return CadEntity(
        id=_stable_id("entity", source_file_id, space, identity_basis),
        source_file_id=source_file_id,
        sheet_id=sheet_id,
        handle=str(handle) if handle else None,
        entity_type=entity_type,
        layer=_dxf_get(entity, "layer"),
        space=space,
        text=_clean_text(entity),
        value=value,
        text_override=text_override,
        insert=insert,
        bbox=entity_bbox,
        geometry=geometry,
    )


def _union_bboxes(
    values: Iterable[tuple[float, float, float, float] | None],
) -> tuple[float, float, float, float] | None:
    bboxes = [value for value in values if value is not None]
    if not bboxes:
        return None
    return (
        min(value[0] for value in bboxes),
        min(value[1] for value in bboxes),
        max(value[2] for value in bboxes),
        max(value[3] for value in bboxes),
    )


def _read_document(path: Path) -> tuple[Any, Any, bool]:
    try:
        document = ezdxf.readfile(path)
        return document, document.audit(), False
    except Exception as direct_error:
        try:
            document, auditor = recover.readfile(path)
            return document, auditor, True
        except Exception as recovery_error:
            raise ValueError(
                f"Cannot read DXF {path}: direct={direct_error}; recovery={recovery_error}"
            ) from recovery_error


@dataclass(slots=True)
class _BlockExpansionState:
    limit: int
    count: int = 0
    truncated: bool = False


def _source_entity_handle(entity: Any) -> str | None:
    try:
        source = entity.source_of_copy
    except (AttributeError, ezdxf.DXFError):
        source = None
    handle = _dxf_get(source, "handle") if source is not None else None
    return str(handle) if handle else None


def _block_has_indexable_content(
    document: Any,
    block_name: str,
    cache: dict[str, bool],
) -> bool:
    cache_key = block_name.casefold()
    if cache_key in cache:
        return cache[cache_key]
    try:
        block = document.blocks.get(block_name)
    except Exception:
        cache[cache_key] = False
        return False
    result = any(entity.dxftype() in INDEXED_ENTITY_TYPES for entity in block)
    cache[cache_key] = result
    return result


def _append_expansion_warning(warnings: list[str], message: str) -> None:
    if len(warnings) < 1_000:
        warnings.append(message)


def _virtual_context(
    *,
    parent: CadEntity,
    parent_handle: str | None,
    handle_chain: list[str],
    block_name: str,
    block_path: list[str],
    source_handle: str | None,
    ordinal: str,
) -> dict[str, Any]:
    return {
        "virtual": True,
        "parent_insert_id": parent.id,
        "parent_insert_handle": parent_handle,
        "parent_insert_chain": handle_chain,
        "block_name": block_name,
        "block_path": block_path,
        "source_block_entity_handle": source_handle,
        "virtual_ordinal": ordinal,
    }


def _expand_insert_entities(
    insert: Any,
    parent_record: CadEntity,
    *,
    document: Any,
    source_file_id: str,
    sheet_id: str,
    space: str,
    bbox_cache: ezbbox.Cache,
    semantic_block_cache: dict[str, bool],
    state: _BlockExpansionState,
    warnings: list[str],
    block_path: list[str],
    handle_chain: list[str],
    depth: int,
    max_depth: int,
) -> list[CadEntity]:
    """Expand visible block semantics into the parent's world coordinates."""

    if state.truncated:
        return []
    block_name = str(_dxf_get(insert, "name", "") or "")
    if not block_name or not _block_has_indexable_content(
        document, block_name, semantic_block_cache
    ):
        return []
    if block_name.casefold() in {value.casefold() for value in block_path}:
        _append_expansion_warning(
            warnings,
            f"{space}: cyclic block reference stopped at {' > '.join([*block_path, block_name])}",
        )
        return []
    if depth > max_depth:
        _append_expansion_warning(
            warnings,
            f"{space}: block expansion depth {max_depth} reached at {block_name}",
        )
        return []

    parent_handle = _source_entity_handle(insert) or (
        str(_dxf_get(insert, "handle")) if _dxf_get(insert, "handle") else None
    )
    current_path = [*block_path, block_name]
    current_chain = [*handle_chain]
    if parent_handle:
        current_chain.append(parent_handle)

    def skipped_entity_callback(skipped: Any, reason: str) -> None:
        _append_expansion_warning(
            warnings,
            f"{space}:{block_name}:{skipped.dxftype()}:{_source_entity_handle(skipped)}: {reason}",
        )

    try:
        children = insert.virtual_entities(skipped_entity_callback=skipped_entity_callback)
    except Exception as exc:
        _append_expansion_warning(
            warnings,
            f"{space}: cannot expand block {block_name}: {type(exc).__name__}: {exc}",
        )
        return []

    records: list[CadEntity] = []
    try:
        for ordinal, child in enumerate(children):
            if state.count >= state.limit:
                state.truncated = True
                _append_expansion_warning(
                    warnings,
                    f"{space}: virtual entity limit {state.limit} reached; expansion truncated",
                )
                break
            child_type = child.dxftype()
            child_record: CadEntity | None = None
            source_handle = _source_entity_handle(child)
            if child_type == "ATTDEF":
                child_tag = str(_dxf_get(child, "tag", "")).casefold()
                actual_tags = {
                    str(_dxf_get(attribute, "tag", "")).casefold()
                    for attribute in insert.attribs
                }
                if child_tag and child_tag in actual_tags:
                    continue
            if child_type in INDEXED_ENTITY_TYPES:
                context = _virtual_context(
                    parent=parent_record,
                    parent_handle=parent_handle,
                    handle_chain=current_chain,
                    block_name=block_name,
                    block_path=current_path,
                    source_handle=source_handle,
                    ordinal=str(ordinal),
                )
                try:
                    child_record = _record_entity(
                        child,
                        source_file_id,
                        sheet_id,
                        space,
                        bbox_cache,
                        parent_insert_handle=parent_handle,
                        virtual_context=context,
                    )
                    records.append(child_record)
                    state.count += 1
                except Exception as exc:
                    _append_expansion_warning(
                        warnings,
                        f"{space}:{block_name}:{child_type}:{source_handle}: {exc}",
                    )

            if child_type != "INSERT" or child_record is None or state.truncated:
                continue

            child_handle = source_handle
            child_block_name = str(_dxf_get(child, "name", "") or "")
            attribute_path = [*current_path, child_block_name]
            attribute_chain = [*current_chain]
            if child_handle:
                attribute_chain.append(child_handle)
            for attribute_ordinal, attribute in enumerate(child.attribs):
                if state.count >= state.limit:
                    state.truncated = True
                    _append_expansion_warning(
                        warnings,
                        f"{space}: virtual entity limit {state.limit} reached; expansion truncated",
                    )
                    break
                attribute_source_handle = _source_entity_handle(attribute)
                context = _virtual_context(
                    parent=child_record,
                    parent_handle=child_handle,
                    handle_chain=attribute_chain,
                    block_name=child_block_name,
                    block_path=attribute_path,
                    source_handle=attribute_source_handle,
                    ordinal=f"{ordinal}.attrib.{attribute_ordinal}",
                )
                try:
                    records.append(
                        _record_entity(
                            attribute,
                            source_file_id,
                            sheet_id,
                            space,
                            bbox_cache,
                            parent_insert_handle=child_handle,
                            virtual_context=context,
                        )
                    )
                    state.count += 1
                except Exception as exc:
                    _append_expansion_warning(
                        warnings,
                        f"{space}:{child_block_name}:ATTRIB:{attribute_source_handle}: {exc}",
                    )

            records.extend(
                _expand_insert_entities(
                    child,
                    child_record,
                    document=document,
                    source_file_id=source_file_id,
                    sheet_id=sheet_id,
                    space=space,
                    bbox_cache=bbox_cache,
                    semantic_block_cache=semantic_block_cache,
                    state=state,
                    warnings=warnings,
                    block_path=current_path,
                    handle_chain=current_chain,
                    depth=depth + 1,
                    max_depth=max_depth,
                )
            )
            if state.truncated:
                break
    except Exception as exc:
        _append_expansion_warning(
            warnings,
            f"{space}: iteration failed in block {block_name}: {type(exc).__name__}: {exc}",
        )
    return records


def index_dxf(
    path: Path | str,
    *,
    source_file_id: str | None = None,
    expand_blocks: bool = True,
    max_block_depth: int = 4,
    max_virtual_entities: int = 100_000,
) -> CadIndexResult:
    """Index modelspace and every paperspace layout of one DXF."""

    started = time.monotonic()
    source_path = Path(path).expanduser().resolve()
    source_hash = sha256_file(source_path)
    resolved_source_id = source_file_id or f"file:{source_hash}"
    document, auditor, recovered = _read_document(source_path)
    cache = ezbbox.Cache()
    sheets: list[Sheet] = []
    entities: list[CadEntity] = []
    warnings: list[str] = []
    if max_block_depth < 1:
        raise ValueError("max_block_depth must be at least 1")
    if max_virtual_entities < 1:
        raise ValueError("max_virtual_entities must be at least 1")
    expansion_state = _BlockExpansionState(max_virtual_entities)
    semantic_block_cache: dict[str, bool] = {}

    for layout in document.layouts:
        layout_name = str(layout.name)
        is_model = layout_name.casefold() == "model"
        space = "model" if is_model else f"paper:{layout_name}"
        sheet_id = _stable_id("sheet", resolved_source_id, layout_name)
        layout_records: list[CadEntity] = []
        seen: set[tuple[str, str | None]] = set()

        for entity in layout:
            entity_type = entity.dxftype()
            if entity_type not in INDEXED_ENTITY_TYPES:
                continue
            key = (entity_type, _dxf_get(entity, "handle"))
            root_record: CadEntity | None = None
            if key not in seen:
                try:
                    root_record = _record_entity(
                        entity, resolved_source_id, sheet_id, space, cache
                    )
                    layout_records.append(root_record)
                    seen.add(key)
                except Exception as exc:
                    _append_expansion_warning(
                        warnings,
                        f"{layout_name}:{entity_type}:{_dxf_get(entity, 'handle')}: {exc}"
                    )

            if entity_type == "INSERT" and root_record is not None:
                for attribute in entity.attribs:
                    attribute_key = ("ATTRIB", _dxf_get(attribute, "handle"))
                    if attribute_key in seen:
                        continue
                    try:
                        layout_records.append(
                            _record_entity(
                                attribute,
                                resolved_source_id,
                                sheet_id,
                                space,
                                cache,
                                parent_insert_handle=_dxf_get(entity, "handle"),
                            )
                        )
                        seen.add(attribute_key)
                    except Exception as exc:
                        _append_expansion_warning(
                            warnings,
                            f"{layout_name}:ATTRIB:{_dxf_get(attribute, 'handle')}: {exc}",
                        )
                if expand_blocks and not expansion_state.truncated:
                    layout_records.extend(
                        _expand_insert_entities(
                            entity,
                            root_record,
                            document=document,
                            source_file_id=resolved_source_id,
                            sheet_id=sheet_id,
                            space=space,
                            bbox_cache=cache,
                            semantic_block_cache=semantic_block_cache,
                            state=expansion_state,
                            warnings=warnings,
                            block_path=[],
                            handle_chain=[],
                            depth=1,
                            max_depth=max_block_depth,
                        )
                    )

        title_texts = [record.text for record in layout_records if record.text]
        classification = classify_sheet(
            source_path.name,
            title_texts,
            layout_name=layout_name,
        )
        viewport_handles = [
            record.handle for record in layout_records if record.entity_type == "VIEWPORT"
        ]
        sheet = Sheet(
            id=sheet_id,
            source_file_id=resolved_source_id,
            drawing_number=classification.drawing_number,
            title=classification.title,
            kind=classification.kind,  # type: ignore[arg-type]
            layout=layout_name,
            viewport_handle=viewport_handles[0] if len(viewport_handles) == 1 else None,
            bbox=_union_bboxes(record.bbox for record in layout_records),
            confidence=classification.confidence,
            evidence=classification.evidence,
        )
        sheets.append(sheet)
        entities.extend(layout_records)

    units_code = int(document.header.get("$INSUNITS", 0) or 0)
    units_name = UNIT_NAMES.get(units_code, "other")
    entities = [
        entity.model_copy(
            update={
                "geometry": {
                    **entity.geometry,
                    "units_code": units_code,
                    "units": units_name,
                }
            }
        )
        if entity.entity_type in {"DIMENSION", "ARC_DIMENSION", "LARGE_RADIAL_DIMENSION"}
        else entity
        for entity in entities
    ]
    counts = Counter(entity.entity_type for entity in entities)
    return CadIndexResult(
        source_path=str(source_path),
        source_file_id=resolved_source_id,
        source_sha256=source_hash,
        dxf_version=str(document.dxfversion),
        units_code=units_code,
        units=units_name,
        sheets=sheets,
        entities=entities,
        entity_counts=dict(sorted(counts.items())),
        audit_error_count=len(auditor.errors),
        audit_fix_count=len(auditor.fixes),
        recovered=recovered,
        virtual_entity_count=expansion_state.count,
        block_expansion_truncated=expansion_state.truncated,
        elapsed_seconds=round(time.monotonic() - started, 3),
        warnings=warnings,
    )


def write_index_sqlite(bundle: CadIndexBundle, path: Path | str) -> Path:
    """Upsert an index bundle into a portable SQLite store."""

    database_path = Path(path).expanduser().resolve()
    database_path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(database_path) as connection:
        connection.executescript(
            """
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS cad_sources (
                source_file_id TEXT PRIMARY KEY,
                source_path TEXT NOT NULL,
                source_sha256 TEXT NOT NULL,
                dxf_version TEXT,
                units_code INTEGER,
                units TEXT,
                audit_error_count INTEGER NOT NULL,
                audit_fix_count INTEGER NOT NULL,
                recovered INTEGER NOT NULL,
                elapsed_seconds REAL NOT NULL,
                warnings_json TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS sheets (
                id TEXT PRIMARY KEY,
                source_file_id TEXT NOT NULL,
                drawing_number TEXT,
                title TEXT,
                kind TEXT NOT NULL,
                layout TEXT,
                viewport_handle TEXT,
                bbox_json TEXT,
                confidence REAL NOT NULL,
                evidence_json TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_sheets_source ON sheets(source_file_id);
            CREATE INDEX IF NOT EXISTS idx_sheets_kind ON sheets(kind);
            CREATE TABLE IF NOT EXISTS cad_entities (
                id TEXT PRIMARY KEY,
                source_file_id TEXT NOT NULL,
                sheet_id TEXT,
                handle TEXT,
                entity_type TEXT NOT NULL,
                layer TEXT,
                space TEXT NOT NULL,
                text TEXT,
                value REAL,
                text_override TEXT,
                insert_json TEXT,
                bbox_json TEXT,
                geometry_json TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_entities_source ON cad_entities(source_file_id);
            CREATE INDEX IF NOT EXISTS idx_entities_sheet ON cad_entities(sheet_id);
            CREATE INDEX IF NOT EXISTS idx_entities_type ON cad_entities(entity_type);
            CREATE INDEX IF NOT EXISTS idx_entities_handle ON cad_entities(source_file_id, handle);
            """
        )
        for result in bundle.results:
            connection.execute(
                "DELETE FROM cad_entities WHERE source_file_id = ?", (result.source_file_id,)
            )
            connection.execute(
                "DELETE FROM sheets WHERE source_file_id = ?", (result.source_file_id,)
            )
            connection.execute(
                "DELETE FROM cad_sources WHERE source_file_id = ?", (result.source_file_id,)
            )
            connection.execute(
                """
                INSERT INTO cad_sources VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    result.source_file_id,
                    result.source_path,
                    result.source_sha256,
                    result.dxf_version,
                    result.units_code,
                    result.units,
                    result.audit_error_count,
                    result.audit_fix_count,
                    int(result.recovered),
                    result.elapsed_seconds,
                    json.dumps(result.warnings, ensure_ascii=False),
                ),
            )
            connection.executemany(
                "INSERT INTO sheets VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [
                    (
                        sheet.id,
                        sheet.source_file_id,
                        sheet.drawing_number,
                        sheet.title,
                        sheet.kind,
                        sheet.layout,
                        sheet.viewport_handle,
                        json.dumps(sheet.bbox, ensure_ascii=False),
                        sheet.confidence,
                        json.dumps(sheet.evidence, ensure_ascii=False),
                    )
                    for sheet in result.sheets
                ],
            )
            connection.executemany(
                "INSERT INTO cad_entities VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [
                    (
                        entity.id,
                        entity.source_file_id,
                        entity.sheet_id,
                        entity.handle,
                        entity.entity_type,
                        entity.layer,
                        entity.space,
                        entity.text,
                        entity.value,
                        entity.text_override,
                        json.dumps(entity.insert, ensure_ascii=False),
                        json.dumps(entity.bbox, ensure_ascii=False),
                        json.dumps(entity.geometry, ensure_ascii=False),
                    )
                    for entity in result.entities
                ],
            )
        connection.commit()
    return database_path


def build_cad_index(
    paths: Path | str | Iterable[Path | str],
    *,
    source_file_ids: Mapping[str, str] | None = None,
    sqlite_path: Path | str | None = None,
    json_path: Path | str | None = None,
    expand_blocks: bool = True,
    max_block_depth: int = 4,
    max_virtual_entities: int = 100_000,
) -> CadIndexBundle:
    """Index one or more DXFs and optionally persist both supported formats."""

    values: Sequence[Path | str]
    if isinstance(paths, (str, Path)):
        values = [paths]
    else:
        values = list(paths)
    resolved_paths = sorted(
        (Path(value).expanduser().resolve() for value in values),
        key=lambda value: str(value).casefold(),
    )
    results: list[CadIndexResult] = []
    for resolved_path in resolved_paths:
        source_id = None
        if source_file_ids:
            source_id = source_file_ids.get(str(resolved_path)) or source_file_ids.get(
                resolved_path.name
            )
        results.append(
            index_dxf(
                resolved_path,
                source_file_id=source_id,
                expand_blocks=expand_blocks,
                max_block_depth=max_block_depth,
                max_virtual_entities=max_virtual_entities,
            )
        )
    bundle = CadIndexBundle(results)
    if sqlite_path is not None:
        write_index_sqlite(bundle, sqlite_path)
    if json_path is not None:
        write_json_atomic(Path(json_path), bundle.to_dict())
    return bundle


# Descriptive alias used by integrations that call this stage a parser.
parse_dxf = index_dxf
