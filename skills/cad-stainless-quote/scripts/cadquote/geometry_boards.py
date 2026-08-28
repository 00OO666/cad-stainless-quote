"""Overlay stable REVIEW-only geometry labels on component close-up renders.

The board connects visible bounding boxes to deterministic component-geometry
primitive/path IDs.  It deliberately does not infer a measurement role, physical
quantity, or component confirmation from linework.
"""

from __future__ import annotations

import hashlib
import math
from collections import defaultdict
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw, ImageFont

from .io import sha256_file, write_json_atomic
from .render import _safe_label

_PATH_COLOR = "#22d3ee"
_PRIMITIVE_COLOR = "#facc15"
_LABEL_BACKGROUND = "#111827"
_SELECTION_CATEGORY_ORDER = (
    "significant_closed_primitive",
    "multi_primitive_path",
    "top_level_handle_primitive",
    "block_handle_primitive",
    "largest_bbox",
    "nearest_region_center",
)


def _array(value: Any) -> bool:
    return isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray))


def _bbox(
    value: Any,
    *,
    require_area: bool,
) -> tuple[float, float, float, float] | None:
    if not _array(value) or len(value) != 4:
        return None
    try:
        result = tuple(float(part) for part in value)
    except (TypeError, ValueError):
        return None
    if not all(math.isfinite(part) for part in result):
        return None
    width = result[2] - result[0]
    height = result[3] - result[1]
    if width < 0 or height < 0 or (require_area and (width <= 0 or height <= 0)):
        return None
    if not require_area and width <= 0 and height <= 0:
        return None
    return result


def _same_bbox(left: Sequence[float], right: Sequence[float]) -> bool:
    return all(
        math.isclose(float(a), float(b), rel_tol=1e-9, abs_tol=1e-6)
        for a, b in zip(left, right, strict=True)
    )


def _selection_key(record: Mapping[str, Any], index: int) -> str:
    for field_name in ("selection_key", "component_id", "gold_row_id", "row_id", "sequence"):
        value = record.get(field_name)
        if value not in (None, ""):
            return str(value)
    return f"selection:{index}"


def _font() -> ImageFont.ImageFont:
    path = Path("C:/Windows/Fonts/msyh.ttc")
    try:
        return ImageFont.truetype(str(path), 20) if path.is_file() else ImageFont.load_default()
    except OSError:
        return ImageFont.load_default()


def _primitive_provenance(
    primitive: Mapping[str, Any],
    *,
    source_file_id: Any,
) -> dict[str, Any]:
    return {
        "source_file_id": source_file_id,
        "provenance_state": primitive.get("provenance_state"),
        "top_level_entity_handle": primitive.get("top_level_entity_handle"),
        "top_level_entity_ordinal": primitive.get("top_level_entity_ordinal"),
        "root_insert_handle": primitive.get("root_insert_handle"),
        "root_insert_instance_ordinal": primitive.get("root_insert_instance_ordinal"),
        "block_path": list(primitive.get("block_path") or []),
        "source_block_entity_handle": primitive.get("source_block_entity_handle"),
        "source_block_entity_ordinal": primitive.get("source_block_entity_ordinal"),
    }


def _candidate_values(region: Mapping[str, Any]) -> list[dict[str, Any]]:
    source_file_id = region.get("source_file_id")
    raw_primitives = region.get("primitives", [])
    primitives = [value for value in raw_primitives if isinstance(value, Mapping)] if _array(
        raw_primitives
    ) else []
    primitives_by_id = {
        str(value["id"]): value for value in primitives if value.get("id") not in (None, "")
    }
    result: list[dict[str, Any]] = []

    raw_paths = region.get("path_candidates", [])
    paths = (
        [value for value in raw_paths if isinstance(value, Mapping)]
        if _array(raw_paths)
        else []
    )
    for path in paths:
        primitive_ids = sorted(
            str(value) for value in (path.get("primitive_ids") or []) if value not in (None, "")
        )
        # A singleton path duplicates the primitive box without adding connected-network value.
        if len(primitive_ids) < 2 or path.get("id") in (None, ""):
            continue
        box = _bbox(path.get("bbox"), require_area=False)
        try:
            length = float(path.get("path_length_candidate_drawing_units"))
        except (TypeError, ValueError):
            continue
        if box is None or not math.isfinite(length) or length <= 0:
            continue
        present = [primitives_by_id[value] for value in primitive_ids if value in primitives_by_id]
        result.append(
            {
                "candidate_kind": "path",
                "geometry_id": str(path["id"]),
                "path_id": str(path["id"]),
                "primitive_id": None,
                "state": "REVIEW",
                "measurement_role": None,
                "layer": path.get("layer"),
                "bbox": list(box),
                "bbox_width_drawing_units": box[2] - box[0],
                "bbox_height_drawing_units": box[3] - box[1],
                "path_length_drawing_units": length,
                "length_method": path.get("length_method"),
                "primitive_count": len(primitive_ids),
                "provenance": {
                    "source_file_id": source_file_id,
                    "primitive_ids": primitive_ids,
                    "missing_primitive_ids": sorted(set(primitive_ids) - set(primitives_by_id)),
                    "primitive_provenance": [
                        _primitive_provenance(value, source_file_id=source_file_id)
                        for value in present
                    ],
                },
                "warning": "REVIEW-only connected path; no measurement role is assigned.",
            }
        )

    for primitive in primitives:
        if primitive.get("id") in (None, ""):
            continue
        box = _bbox(primitive.get("bbox"), require_area=False)
        try:
            length = float(primitive.get("length_drawing_units"))
        except (TypeError, ValueError):
            continue
        if box is None or not math.isfinite(length) or length <= 0:
            continue
        result.append(
            {
                "candidate_kind": "primitive",
                "geometry_id": str(primitive["id"]),
                "path_id": None,
                "primitive_id": str(primitive["id"]),
                "entity_type": primitive.get("entity_type"),
                "closed": bool(primitive.get("closed")),
                "state": "REVIEW",
                "measurement_role": None,
                "layer": primitive.get("layer"),
                "bbox": list(box),
                "bbox_width_drawing_units": box[2] - box[0],
                "bbox_height_drawing_units": box[3] - box[1],
                "path_length_drawing_units": length,
                "length_method": primitive.get("length_method"),
                "primitive_count": 1,
                "provenance": _primitive_provenance(
                    primitive,
                    source_file_id=source_file_id,
                ),
                "warning": "REVIEW-only primitive geometry; no measurement role is assigned.",
            }
        )

    def sort_key(value: Mapping[str, Any]) -> tuple[Any, ...]:
        if value["candidate_kind"] == "path":
            return (
                0,
                -int(value["primitive_count"]),
                -float(value["path_length_drawing_units"]),
                str(value["geometry_id"]),
            )
        provenance = value.get("provenance") or {}
        provenance_rank = (
            0 if provenance.get("provenance_state") != "BLOCK_ENTITY_ORDINAL_ONLY" else 1
        )
        return (
            1,
            provenance_rank,
            -float(value["path_length_drawing_units"]),
            str(value["geometry_id"]),
        )

    result.sort(key=sort_key)
    return result


def _candidate_identity(value: Mapping[str, Any]) -> tuple[str, str]:
    return str(value.get("candidate_kind") or ""), str(value.get("geometry_id") or "")


def _bbox_rank_metrics(
    value: Mapping[str, Any],
    render_bbox: Sequence[float],
) -> tuple[float, float, float]:
    region_width = float(render_bbox[2]) - float(render_bbox[0])
    region_height = float(render_bbox[3]) - float(render_bbox[1])
    width = float(value.get("bbox_width_drawing_units") or 0.0) / region_width
    height = float(value.get("bbox_height_drawing_units") or 0.0) / region_height
    return width * height, math.hypot(width, height), max(width, height)


def _prominence_key(
    value: Mapping[str, Any],
    render_bbox: Sequence[float],
) -> tuple[Any, ...]:
    area, diagonal, span = _bbox_rank_metrics(value, render_bbox)
    return (
        -area,
        -diagonal,
        -span,
        -float(value.get("path_length_drawing_units") or 0.0),
        _candidate_identity(value),
    )


def _center_key(
    value: Mapping[str, Any],
    render_bbox: Sequence[float],
) -> tuple[Any, ...]:
    box = value["bbox"]
    region_width = float(render_bbox[2]) - float(render_bbox[0])
    region_height = float(render_bbox[3]) - float(render_bbox[1])
    region_center_x = (float(render_bbox[0]) + float(render_bbox[2])) / 2.0
    region_center_y = (float(render_bbox[1]) + float(render_bbox[3])) / 2.0
    center_x = (float(box[0]) + float(box[2])) / 2.0
    center_y = (float(box[1]) + float(box[3])) / 2.0
    distance = math.hypot(
        (center_x - region_center_x) / region_width,
        (center_y - region_center_y) / region_height,
    )
    return (distance, *_prominence_key(value, render_bbox))


def _balanced_candidates(
    candidates: Sequence[dict[str, Any]],
    render_bbox: Sequence[float],
    maximum: int,
) -> list[dict[str, Any]]:
    """Select deterministic representatives without allowing one geometry class to starve others."""

    primitives = [value for value in candidates if value["candidate_kind"] == "primitive"]
    paths = [
        value
        for value in candidates
        if value["candidate_kind"] == "path" and int(value.get("primitive_count") or 0) >= 2
    ]
    closed = [value for value in primitives if bool(value.get("closed"))]
    top_level = [
        value
        for value in primitives
        if (value.get("provenance") or {}).get("top_level_entity_handle") not in (None, "")
    ]
    block_handle = [
        value
        for value in primitives
        if (value.get("provenance") or {}).get("source_block_entity_handle") not in (None, "")
        or (value.get("provenance") or {}).get("provenance_state")
        == "BLOCK_ENTITY_HANDLE"
    ]

    pools = {
        "significant_closed_primitive": sorted(
            closed,
            key=lambda value: _prominence_key(value, render_bbox),
        ),
        "multi_primitive_path": sorted(
            paths,
            key=lambda value: (
                -int(value.get("primitive_count") or 0),
                *_prominence_key(value, render_bbox),
            ),
        ),
        "top_level_handle_primitive": sorted(
            top_level,
            key=lambda value: _prominence_key(value, render_bbox),
        ),
        "block_handle_primitive": sorted(
            block_handle,
            key=lambda value: _prominence_key(value, render_bbox),
        ),
        "largest_bbox": sorted(
            candidates,
            key=lambda value: _prominence_key(value, render_bbox),
        ),
        "nearest_region_center": sorted(
            candidates,
            key=lambda value: _center_key(value, render_bbox),
        ),
    }
    memberships: dict[tuple[str, str], list[str]] = defaultdict(list)
    for category in _SELECTION_CATEGORY_ORDER:
        for candidate in pools[category]:
            memberships[_candidate_identity(candidate)].append(category)

    positions = {category: 0 for category in _SELECTION_CATEGORY_ORDER}
    selected: list[dict[str, Any]] = []
    selected_ids: set[tuple[str, str]] = set()
    target_count = min(maximum, len(candidates))
    while len(selected) < target_count:
        made_progress = False
        for category in _SELECTION_CATEGORY_ORDER:
            pool = pools[category]
            while positions[category] < len(pool):
                candidate = pool[positions[category]]
                positions[category] += 1
                identity = _candidate_identity(candidate)
                if identity in selected_ids:
                    continue
                selected_candidate = dict(candidate)
                selected_candidate["selected_by_category"] = category
                selected_candidate["selection_categories"] = memberships[identity]
                selected.append(selected_candidate)
                selected_ids.add(identity)
                made_progress = True
                break
            if len(selected) >= target_count:
                break
        if not made_progress:
            break
    return selected


def _pixel_bbox(
    box: Sequence[float],
    render_bbox: Sequence[float],
    width: int,
    height: int,
) -> tuple[int, int, int, int]:
    x0, y0, x1, y1 = (float(value) for value in render_bbox)
    left = round((float(box[0]) - x0) / (x1 - x0) * width)
    right = round((float(box[2]) - x0) / (x1 - x0) * width)
    top = round((y1 - float(box[3])) / (y1 - y0) * height)
    bottom = round((y1 - float(box[1])) / (y1 - y0) * height)
    left, right = sorted((min(max(left, 0), width - 1), min(max(right, 0), width - 1)))
    top, bottom = sorted((min(max(top, 0), height - 1), min(max(bottom, 0), height - 1)))
    if left == right:
        left = max(0, left - 2)
        right = min(width - 1, right + 2)
    if top == bottom:
        top = max(0, top - 2)
        bottom = min(height - 1, bottom + 2)
    return left, top, right, bottom


def _draw_candidates(
    image: Image.Image,
    candidates: list[dict[str, Any]],
    render_bbox: Sequence[float],
) -> None:
    draw = ImageDraw.Draw(image)
    font = _font()
    used_labels: list[tuple[int, int]] = []
    for index, candidate in enumerate(candidates, start=1):
        label = f"G{index}"
        candidate["candidate_label"] = label
        pixel_box = _pixel_bbox(candidate["bbox"], render_bbox, image.width, image.height)
        candidate["board_pixel_bbox"] = list(pixel_box)
        color = _PATH_COLOR if candidate["candidate_kind"] == "path" else _PRIMITIVE_COLOR
        draw.rectangle(pixel_box, outline=color, width=3)
        x = pixel_box[0]
        y = pixel_box[1]
        offset = 0
        while any(
            abs(x - used_x) < 52 and abs(y + offset - used_y) < 24
            for used_x, used_y in used_labels
        ):
            offset += 24
        y = min(y + offset, max(0, image.height - 24))
        used_labels.append((x, y))
        candidate["label_pixel"] = [x, y]
        caption = f"{label} {'PATH' if candidate['candidate_kind'] == 'path' else 'GEOM'}"
        text_box = draw.textbbox((x + 4, y + 2), caption, font=font)
        draw.rectangle(
            (text_box[0] - 3, text_box[1] - 2, text_box[2] + 3, text_box[3] + 2),
            fill=_LABEL_BACKGROUND,
            outline=color,
            width=2,
        )
        draw.text((x + 4, y + 2), caption, fill=color, font=font)


def build_geometry_boards(
    closeup_payload: Mapping[str, Any],
    geometry_payload: Mapping[str, Any],
    output_dir: Path | str,
    *,
    closeup_root: Path | str | None = None,
    maximum_per_image: int = 80,
    maximum_boards: int = 500,
) -> dict[str, Any]:
    """Create numbered geometry review boards without assigning measurement roles."""

    if maximum_per_image < 1:
        raise ValueError("maximum_per_image must be at least 1")
    if maximum_boards < 1:
        raise ValueError("maximum_boards must be at least 1")
    destination = Path(output_dir).resolve()
    board_dir = destination / "boards"
    board_dir.mkdir(parents=True, exist_ok=True)
    source_root = Path(closeup_root).resolve() if closeup_root else None

    closeups: dict[
        tuple[str, int],
        list[tuple[Mapping[str, Any], Mapping[str, Any]]],
    ] = defaultdict(list)
    raw_records = closeup_payload.get("records", [])
    if not _array(raw_records):
        raise ValueError("closeup_payload.records must be an array")
    for record_index, record in enumerate(raw_records, start=1):
        if not isinstance(record, Mapping):
            continue
        selection_key = _selection_key(record, record_index)
        evidence_values = record.get("evidence", [])
        if not _array(evidence_values):
            continue
        for evidence_index, evidence in enumerate(evidence_values, start=1):
            if isinstance(evidence, Mapping):
                closeups[(selection_key, evidence_index)].append((record, evidence))

    raw_regions = geometry_payload.get("regions", [])
    if not _array(raw_regions):
        raise ValueError("geometry_payload.regions must be an array")
    regions = [value for value in raw_regions if isinstance(value, Mapping)]
    regions.sort(
        key=lambda value: (
            str(value.get("selection_key") or ""),
            int(value.get("evidence_index") or 0),
            str(value.get("region_id") or ""),
        )
    )

    records: list[dict[str, Any]] = []
    board_count = 0
    missing_count = 0
    unusable_count = 0
    truncated_count = 0
    input_role_ignored_count = 0
    global_geometry_truncated = bool(
        (geometry_payload.get("summary") or {}).get("global_output_truncated")
    )

    for region_index, region in enumerate(regions, start=1):
        selection_key = str(region.get("selection_key") or f"selection:{region_index}")
        try:
            evidence_index = int(region.get("evidence_index") or 0)
        except (TypeError, ValueError):
            evidence_index = 0
        output = {
            "selection_key": selection_key,
            "sequence": region.get("sequence", region_index),
            "region_id": region.get("region_id"),
            "evidence_index": evidence_index,
            "source_file_id": region.get("source_file_id"),
            "sheet_id": region.get("sheet_id"),
            "state": "REVIEW",
            "measurement_roles_assigned": False,
            "reason_codes": ["GEOMETRY_SELECTION_REQUIRED"],
            "render_bbox": region.get("render_bbox"),
            "source_geometry_truncated": False,
            "candidate_count": 0,
            "total_candidate_count": 0,
            "candidate_truncated": False,
            "board": None,
        }
        matched = closeups.get((selection_key, evidence_index), [])
        if len(matched) != 1:
            output["state"] = "MISSING"
            output["reason_codes"].append(
                "CLOSEUP_EVIDENCE_NOT_FOUND" if not matched else "CLOSEUP_EVIDENCE_AMBIGUOUS"
            )
            missing_count += 1
            records.append(output)
            continue
        closeup_record, evidence = matched[0]
        output["component_id"] = closeup_record.get("component_id")
        region_bbox = _bbox(region.get("render_bbox"), require_area=True)
        closeup_bbox = _bbox(evidence.get("render_bbox"), require_area=True)
        identity_mismatch = any(
            left not in (None, "") and right not in (None, "") and str(left) != str(right)
            for left, right in (
                (region.get("source_file_id"), evidence.get("source_file_id")),
                (region.get("sheet_id"), evidence.get("sheet_id")),
            )
        )
        if (
            region_bbox is None
            or closeup_bbox is None
            or not _same_bbox(region_bbox, closeup_bbox)
            or identity_mismatch
        ):
            output["state"] = "MISSING"
            output["reason_codes"].append(
                "CLOSEUP_GEOMETRY_IDENTITY_MISMATCH"
                if identity_mismatch
                else "RENDER_BBOX_MISMATCH_OR_INVALID"
            )
            missing_count += 1
            records.append(output)
            continue
        raw_path = evidence.get("absolute_path")
        if not raw_path and source_root is not None and evidence.get("relative_path"):
            raw_path = source_root / str(evidence["relative_path"])
        image_path = Path(str(raw_path)).resolve() if raw_path else None
        if image_path is None or not image_path.is_file():
            output["state"] = "MISSING"
            output["reason_codes"].append("CLOSEUP_IMAGE_MISSING")
            missing_count += 1
            records.append(output)
            continue
        if not bool(region.get("usable", True)):
            output["reason_codes"].append("GEOMETRY_REGION_UNUSABLE")
            unusable_count += 1
            records.append(output)
            continue

        raw_candidates = [
            value
            for field_name in ("primitives", "path_candidates")
            for value in (region.get(field_name) or [])
            if isinstance(value, Mapping)
        ]
        if any(value.get("measurement_role") not in (None, "") for value in raw_candidates):
            output["reason_codes"].append("INPUT_MEASUREMENT_ROLE_IGNORED")
            input_role_ignored_count += 1

        all_candidates = _candidate_values(region)
        total_candidate_count = len(all_candidates)
        candidates = _balanced_candidates(
            all_candidates,
            region_bbox,
            maximum_per_image,
        )
        if total_candidate_count > maximum_per_image:
            output["candidate_truncated"] = True
            output["reason_codes"].append("GEOMETRY_BOARD_CANDIDATES_TRUNCATED")
        source_truncated = bool((region.get("truncation") or {}).get("any")) or bool(
            global_geometry_truncated
        )
        if source_truncated:
            output["source_geometry_truncated"] = True
            output["reason_codes"].append("SOURCE_GEOMETRY_TRUNCATED")
        if output["candidate_truncated"] or source_truncated:
            truncated_count += 1
        output["candidate_count"] = len(candidates)
        output["total_candidate_count"] = total_candidate_count
        if not candidates:
            output["reason_codes"].append("HIGH_VALUE_GEOMETRY_CANDIDATES_MISSING")
            unusable_count += 1
            records.append(output)
            continue
        if board_count >= maximum_boards:
            output["candidate_truncated"] = True
            output["reason_codes"].append("GEOMETRY_BOARD_LIMIT_REACHED")
            if not source_truncated and total_candidate_count <= maximum_per_image:
                truncated_count += 1
            records.append(output)
            continue

        with Image.open(image_path) as opened:
            image = opened.convert("RGB")
        _draw_candidates(image, candidates, region_bbox)
        digest = hashlib.sha256(
            f"{region.get('region_id')}\0{sha256_file(image_path)}".encode()
        ).hexdigest()[:12]
        board_name = (
            f"{_safe_label(str(output['sequence']))}-"
            f"{_safe_label(selection_key)}-{digest}.png"
        )
        board_path = board_dir / board_name
        image.save(board_path, format="PNG")
        output["board"] = {
            "source_image_path": str(image_path),
            "source_image_sha256": sha256_file(image_path),
            "board_absolute_path": str(board_path),
            "board_relative_path": str(board_path.relative_to(destination)).replace("\\", "/"),
            "board_sha256": sha256_file(board_path),
            "pixel_size": [image.width, image.height],
            "render_bbox": list(region_bbox),
            "candidates": candidates,
        }
        board_count += 1
        records.append(output)

    result = {
        "schema_version": "1.0",
        "purpose": "numbered_component_geometry_review_boards",
        "path_scope": "local_run_diagnostics",
        "policy": {
            "status": "REVIEW_ONLY",
            "auto_assign_measurement_role": False,
            "auto_assign_length": False,
            "auto_assign_width": False,
            "auto_assign_quantity": False,
            "labels_are_stable_ordinals_only": True,
            "maximum_per_image": maximum_per_image,
            "maximum_boards": maximum_boards,
            "selection_strategy": "deterministic_balanced_round_robin",
            "selection_order": [
                "significant closed primitives",
                "multi-primitive connected paths",
                "top-level handle primitives",
                "block-handle primitives",
                "largest normalized bboxes",
                "nearest region-center candidates",
            ],
        },
        "warning": (
            "G labels identify geometry/path candidates only. They never assign a measurement "
            "role, confirm component ownership, or prove physical quantity. Truncated boards and "
            "source scans are incomplete; absolute paths must not be published."
        ),
        "region_count": len(regions),
        "board_count": board_count,
        "missing_count": missing_count,
        "unusable_count": unusable_count,
        "truncated_count": truncated_count,
        "input_role_ignored_count": input_role_ignored_count,
        "board_limit_reached": board_count >= maximum_boards and len(regions) > board_count,
        "records": records,
    }
    write_json_atomic(destination / "geometry_boards.json", result)
    return result


__all__ = ["build_geometry_boards"]
