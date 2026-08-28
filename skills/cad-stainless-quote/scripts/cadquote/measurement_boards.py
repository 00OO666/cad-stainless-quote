"""Overlay stable measurement labels on high-resolution component close-ups.

The board is a review aid: it lets a model or reviewer refer to the exact CAD
entity behind a visible dimension instead of transcribing an unbound number.
It never selects a measurement or confirms component ownership.
"""

from __future__ import annotations

import hashlib
import math
import re
from collections import defaultdict
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw, ImageFont

from .io import sha256_file, write_json_atomic
from .render import _safe_label

_DIMENSION_TYPES = {"DIMENSION", "ARC_DIMENSION", "LARGE_RADIAL_DIMENSION"}
_TEXT_TYPES = {"TEXT", "MTEXT", "ATTRIB", "ATTDEF"}
_STRUCTURED_TAG_ROLES = {
    "HT": "height",
    "H": "height",
    "HEIGHT": "height",
    "高度": "height",
    "高": "height",
    "LEN": "length",
    "LENGTH": "length",
    "L": "length",
    "长度": "length",
    "长": "length",
    "WD": "width",
    "WIDTH": "width",
    "W": "width",
    "宽度": "width",
    "宽": "width",
    "QTY": "quantity",
    "QUANTITY": "quantity",
    "COUNT": "quantity",
    "数量": "quantity",
}
_EXPLICIT_TEXT_RE = re.compile(
    r"(?:展开|UNFOLDED|长度|高度|宽度|数量|QTY|\b(?:L|H|W)\s*[:=])",
    re.IGNORECASE,
)
_NUMBER_RE = re.compile(r"(?<![A-Za-z\d.])\d+(?:\.\d+)?")
_EXPRESSION_RE = re.compile(r"\d+(?:\.\d+)?(?:\s*[+×xX*]\s*\d+(?:\.\d+)?)+")


def _bbox(value: Any) -> tuple[float, float, float, float] | None:
    if (
        not isinstance(value, Sequence)
        or isinstance(value, (str, bytes, bytearray))
        or len(value) != 4
    ):
        return None
    try:
        result = tuple(float(part) for part in value)
    except (TypeError, ValueError):
        return None
    if not all(math.isfinite(part) for part in result):
        return None
    if result[2] <= result[0] or result[3] <= result[1]:
        return None
    return result


def _intersects(left: Sequence[float], right: Sequence[float]) -> bool:
    return (
        float(left[0]) <= float(right[2])
        and float(left[2]) >= float(right[0])
        and float(left[1]) <= float(right[3])
        and float(left[3]) >= float(right[1])
    )


def _entity_point(entity: Mapping[str, Any]) -> tuple[float, float] | None:
    box = _bbox(entity.get("bbox"))
    if box is not None:
        return ((box[0] + box[2]) / 2, (box[1] + box[3]) / 2)
    insert = entity.get("insert")
    if (
        isinstance(insert, Sequence)
        and not isinstance(insert, (str, bytes, bytearray))
        and len(insert) >= 2
    ):
        try:
            point = (float(insert[0]), float(insert[1]))
        except (TypeError, ValueError):
            return None
        return point if all(math.isfinite(value) for value in point) else None
    return None


def _orientation(entity: Mapping[str, Any]) -> str | None:
    geometry = entity.get("geometry")
    if not isinstance(geometry, Mapping):
        return None
    left = geometry.get("defpoint2")
    right = geometry.get("defpoint3")
    if not (
        isinstance(left, Sequence)
        and isinstance(right, Sequence)
        and len(left) >= 2
        and len(right) >= 2
    ):
        return None
    try:
        dx = abs(float(right[0]) - float(left[0]))
        dy = abs(float(right[1]) - float(left[1]))
    except (TypeError, ValueError):
        return None
    if max(dx, dy) <= 0:
        return None
    if dx >= dy * 1.5:
        return "horizontal"
    if dy >= dx * 1.5:
        return "vertical"
    return "diagonal"


def _candidate(entity: Mapping[str, Any]) -> dict[str, Any] | None:
    entity_type = str(entity.get("entity_type") or "").upper()
    geometry = entity.get("geometry")
    geometry = geometry if isinstance(geometry, Mapping) else {}
    raw_value: str | None = None
    numeric_value: float | None = None
    role_hint: str | None = None
    if entity_type in _DIMENSION_TYPES:
        raw = entity.get("text_override")
        displayed = geometry.get("display_measurement")
        if raw not in (None, ""):
            raw_value = str(raw).strip()
        elif isinstance(displayed, (int, float)):
            raw_value = f"{float(displayed):g}"
        elif isinstance(entity.get("value"), (int, float)):
            raw_value = f"{float(entity['value']):g}"
        if isinstance(displayed, (int, float)) and math.isfinite(float(displayed)):
            numeric_value = float(displayed)
        elif isinstance(entity.get("value"), (int, float)) and math.isfinite(
            float(entity["value"])
        ):
            numeric_value = float(entity["value"])
        orientation = _orientation(entity)
        if orientation == "horizontal":
            role_hint = "horizontal_length_or_width"
        elif orientation == "vertical":
            role_hint = "vertical_height_or_length"
    elif entity_type in _TEXT_TYPES:
        text = str(entity.get("text") or entity.get("text_override") or "").strip()
        tag = str(geometry.get("tag") or "").strip().upper()
        role_hint = _STRUCTURED_TAG_ROLES.get(tag)
        if role_hint is None and not (
            _EXPLICIT_TEXT_RE.search(text) or _EXPRESSION_RE.search(text)
        ):
            return None
        raw_value = text
        numbers = _NUMBER_RE.findall(text)
        if len(numbers) == 1:
            numeric_value = float(numbers[0])
    else:
        return None
    if not raw_value:
        return None
    point = _entity_point(entity)
    if point is None:
        return None
    return {
        "entity_id": entity.get("id"),
        "handle": entity.get("handle"),
        "entity_type": entity_type,
        "raw_value": raw_value,
        "numeric_value": numeric_value,
        "role_hint": role_hint,
        "orientation": _orientation(entity),
        "bbox": list(_bbox(entity.get("bbox"))) if _bbox(entity.get("bbox")) else None,
        "insert": list(entity.get("insert")) if entity.get("insert") is not None else None,
        "point": list(point),
        "units": geometry.get("units"),
    }


def _pixel_point(
    point: Sequence[float],
    render_bbox: Sequence[float],
    width: int,
    height: int,
) -> tuple[int, int]:
    x0, y0, x1, y1 = (float(value) for value in render_bbox)
    x = (float(point[0]) - x0) / (x1 - x0) * width
    y = (y1 - float(point[1])) / (y1 - y0) * height
    return (
        min(max(round(x), 8), max(8, width - 8)),
        min(max(round(y), 8), max(8, height - 8)),
    )


def _font() -> ImageFont.ImageFont:
    path = Path("C:/Windows/Fonts/msyh.ttc")
    try:
        return ImageFont.truetype(str(path), 22) if path.is_file() else ImageFont.load_default()
    except OSError:
        return ImageFont.load_default()


def build_measurement_boards(
    panel_payload: Mapping[str, Any],
    closeup_payload: Mapping[str, Any],
    output_dir: Path | str,
    *,
    takeoff_payload: Mapping[str, Any] | None = None,
    closeup_root: Path | str | None = None,
    maximum_per_image: int = 120,
) -> dict[str, Any]:
    """Create labeled review boards for every readable component close-up."""

    if maximum_per_image < 1:
        raise ValueError("maximum_per_image must be at least 1")
    destination = Path(output_dir).resolve()
    board_dir = destination / "boards"
    board_dir.mkdir(parents=True, exist_ok=True)
    source_root = Path(closeup_root).resolve() if closeup_root else None

    entities_by_sheet: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for entity in panel_payload.get("entities", []):
        if isinstance(entity, Mapping) and entity.get("sheet_id"):
            entities_by_sheet[str(entity["sheet_id"])].append(entity)

    measurement_bindings: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    if takeoff_payload is not None:
        for measurement in takeoff_payload.get("measurements", []):
            if not isinstance(measurement, Mapping):
                continue
            component_id = str(measurement.get("component_id") or "")
            candidate_id = str(measurement.get("id") or "")
            if not component_id or not candidate_id:
                continue
            for entity_id in measurement.get("entity_ids", []):
                if entity_id:
                    measurement_bindings[(component_id, str(entity_id))].append(
                        {
                            "candidate_id": candidate_id,
                            "role": measurement.get("role"),
                            "raw_value": measurement.get("raw_value"),
                            "numeric_value": measurement.get("numeric_value"),
                            "unit": measurement.get("unit"),
                            "status": measurement.get("status"),
                        }
                    )

    records: list[dict[str, Any]] = []
    missing_count = 0
    truncated_count = 0
    board_count = 0
    font = _font()
    raw_records = closeup_payload.get("records", [])
    if not isinstance(raw_records, Sequence) or isinstance(raw_records, (str, bytes, bytearray)):
        raise ValueError("closeup_payload.records must be an array")

    for record_index, raw_record in enumerate(raw_records, start=1):
        if not isinstance(raw_record, Mapping):
            continue
        selection_key = str(
            raw_record.get("selection_key")
            or raw_record.get("component_id")
            or raw_record.get("sequence")
            or f"selection:{record_index}"
        )
        component_id = str(raw_record.get("component_id") or "")
        if not component_id and selection_key.startswith("component:"):
            component_id = selection_key
        output_record = {
            "selection_key": selection_key,
            "sequence": raw_record.get("sequence", record_index),
            "state": "MISSING",
            "reason_codes": [],
            "boards": [],
        }
        evidence = raw_record.get("evidence", [])
        if not isinstance(evidence, Sequence) or isinstance(evidence, (str, bytes, bytearray)):
            evidence = []
        for evidence_index, raw_evidence in enumerate(evidence, start=1):
            if not isinstance(raw_evidence, Mapping):
                continue
            sheet_id = str(raw_evidence.get("sheet_id") or "")
            render_bbox = _bbox(raw_evidence.get("render_bbox"))
            raw_path = raw_evidence.get("absolute_path")
            if not raw_path and source_root is not None and raw_evidence.get("relative_path"):
                raw_path = source_root / str(raw_evidence["relative_path"])
            image_path = Path(str(raw_path)).resolve() if raw_path else None
            if render_bbox is None or image_path is None or not image_path.is_file():
                output_record["reason_codes"].append("CLOSEUP_OR_RENDER_BBOX_MISSING")
                missing_count += 1
                continue
            candidates: list[dict[str, Any]] = []
            for entity in entities_by_sheet.get(sheet_id, []):
                entity_box = _bbox(entity.get("bbox"))
                point = _entity_point(entity)
                if point is None:
                    continue
                if entity_box is not None:
                    if not _intersects(entity_box, render_bbox):
                        continue
                elif not (
                    render_bbox[0] <= point[0] <= render_bbox[2]
                    and render_bbox[1] <= point[1] <= render_bbox[3]
                ):
                    continue
                value = _candidate(entity)
                if value is not None:
                    value["measurement_candidates"] = measurement_bindings.get(
                        (component_id, str(value.get("entity_id") or "")),
                        [],
                    )
                    candidates.append(value)
            candidates.sort(
                key=lambda value: (
                    -float(value["point"][1]),
                    float(value["point"][0]),
                    str(value.get("entity_id") or ""),
                )
            )
            total_candidates = len(candidates)
            if total_candidates > maximum_per_image:
                candidates = candidates[:maximum_per_image]
                truncated_count += 1
                output_record["reason_codes"].append("MEASUREMENT_BOARD_TRUNCATED")
            with Image.open(image_path) as opened:
                image = opened.convert("RGB")
            draw = ImageDraw.Draw(image)
            used_positions: list[tuple[int, int]] = []
            for candidate_index, candidate in enumerate(candidates, start=1):
                label = f"D{candidate_index}"
                candidate["candidate_label"] = label
                x, y = _pixel_point(candidate["point"], render_bbox, image.width, image.height)
                offset = 0
                while any(
                    abs(x - used_x) < 70 and abs(y + offset - used_y) < 28
                    for used_x, used_y in used_positions
                ):
                    offset += 28
                y = min(y + offset, max(8, image.height - 30))
                used_positions.append((x, y))
                caption = f"{label} {candidate['raw_value']}"
                text_box = draw.textbbox((x + 10, y - 12), caption, font=font)
                draw.rectangle(
                    (text_box[0] - 4, text_box[1] - 2, text_box[2] + 4, text_box[3] + 2),
                    fill="#111827",
                    outline="#facc15",
                    width=2,
                )
                draw.ellipse((x - 7, y - 7, x + 7, y + 7), fill="#ef4444", outline="#ffffff")
                draw.text((x + 10, y - 12), caption, fill="#fef08a", font=font)
                candidate["board_pixel"] = [x, y]
            digest = hashlib.sha256(
                f"{selection_key}\0{sheet_id}\0{evidence_index}".encode()
            ).hexdigest()[:12]
            board_name = (
                f"{_safe_label(str(output_record['sequence']))}-"
                f"{_safe_label(selection_key)}-{digest}.png"
            )
            board_path = board_dir / board_name
            image.save(board_path, format="PNG")
            output_record["boards"].append(
                {
                    "sheet_id": sheet_id,
                    "component_id": component_id or None,
                    "source_file_id": raw_evidence.get("source_file_id"),
                    "drawing_number": raw_evidence.get("drawing_number"),
                    "kind": raw_evidence.get("kind"),
                    "render_bbox": list(render_bbox),
                    "source_image_path": str(image_path),
                    "source_image_sha256": sha256_file(image_path),
                    "board_absolute_path": str(board_path),
                    "board_relative_path": str(
                        board_path.relative_to(destination)
                    ).replace("\\", "/"),
                    "board_sha256": sha256_file(board_path),
                    "pixel_size": [image.width, image.height],
                    "candidate_count": len(candidates),
                    "total_candidate_count": total_candidates,
                    "truncated": total_candidates > maximum_per_image,
                    "state": "REVIEW",
                    "reason_codes": ["MEASUREMENT_ENTITY_SELECTION_REQUIRED"],
                    "candidates": candidates,
                }
            )
            board_count += 1
        if output_record["boards"]:
            output_record["state"] = "REVIEW"
            output_record["reason_codes"].append("MEASUREMENT_SELECTION_REQUIRED")
        records.append(output_record)

    result = {
        "schema_version": "1.0",
        "purpose": "labeled_measurement_entity_review_boards",
        "path_scope": "local_run_diagnostics",
        "warning": (
            "Labels bind visible values to CAD entities but do not select a role, confirm a "
            "component, or prove quantity. Absolute paths must not be published."
        ),
        "selection_count": len(records),
        "board_count": board_count,
        "missing_count": missing_count,
        "truncated_count": truncated_count,
        "records": records,
    }
    write_json_atomic(destination / "measurement_boards.json", result)
    return result


__all__ = ["build_measurement_boards"]
