"""Render row-specific evidence after explicit MT occurrence selection.

This stage consumes a reviewer/vision selection; it never chooses the first
candidate itself.  The resulting locator and close-up are still candidate
evidence until component geometry, dimensions, cross-drawing links, and
quantity roles are confirmed.
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


def _list(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return list(value)
    return [value]


def _font(size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont | None:
    path = Path("C:/Windows/Fonts/msyh.ttc")
    try:
        return ImageFont.truetype(str(path), size) if path.is_file() else None
    except OSError:
        return None


def _safe_label(value: Any) -> str:
    normalized = "".join(character if character.isalnum() else "_" for character in str(value))
    return normalized[:80].strip("_") or "row"


def _selection_key(selection: Mapping[str, Any], index: int) -> str:
    for key in ("component_id", "gold_row_id", "row_id", "sequence"):
        if selection.get(key) not in (None, ""):
            return str(selection[key])
    return f"selection:{index}"


def _component_name(selection: Mapping[str, Any], key: str) -> str:
    return str(selection.get("name") or selection.get("component_name") or key).strip()


def _component_location(selection: Mapping[str, Any]) -> str:
    return str(
        selection.get("room_or_location")
        or selection.get("room")
        or selection.get("location")
        or selection.get("plan_location")
        or ""
    ).strip()


def _compact_identifier(value: Any) -> str:
    return "".join(character for character in str(value).upper() if character.isalnum())


def _declared_labels(selection: Mapping[str, Any], group_id: str) -> list[str]:
    labels: list[str] = []
    for value in _list(selection.get("selected_labels")):
        if isinstance(value, Mapping):
            declared_group = value.get("group_id")
            if declared_group not in (None, "", group_id):
                continue
            if value.get("label") not in (None, ""):
                labels.append(str(value["label"]))
        elif value not in (None, ""):
            labels.append(str(value))
    return labels


def _group_value(selection: Mapping[str, Any], field: str, group_id: str) -> Any:
    value = selection.get(field)
    if isinstance(value, Mapping):
        return value.get(group_id)
    return value


def _bbox_pixel_points(
    value: Any,
    panel_bbox: tuple[float, float, float, float],
    width: int,
    height: int,
) -> tuple[list[tuple[float, float]], list[float]]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)) or len(value) != 4:
        raise ValueError("bbox must contain four coordinates")
    bbox = [float(coordinate) for coordinate in value]
    if not all(math.isfinite(coordinate) for coordinate in bbox):
        raise ValueError("bbox coordinates must be finite")
    bx0, by0, bx1, by1 = bbox
    x0, y0, x1, y1 = panel_bbox
    if bx1 <= bx0 or by1 <= by0:
        raise ValueError("bbox must have positive area")
    if bx0 < x0 or by0 < y0 or bx1 > x1 or by1 > y1:
        raise ValueError("bbox must stay inside the panel bbox")
    return (
        [
            ((bx0 - x0) / (x1 - x0) * width, (y1 - by1) / (y1 - y0) * height),
            ((bx1 - x0) / (x1 - x0) * width, (y1 - by0) / (y1 - y0) * height),
        ],
        bbox,
    )


def _crop_box(
    points: Sequence[tuple[float, float]],
    width: int,
    height: int,
    *,
    minimum_ratio: float = 0.38,
) -> tuple[int, int, int, int]:
    left = min(point[0] for point in points)
    right = max(point[0] for point in points)
    top = min(point[1] for point in points)
    bottom = max(point[1] for point in points)
    margin = max(80, round(min(width, height) * 0.07))
    crop_width = min(
        width,
        max(round(width * minimum_ratio), round(right - left + margin * 2)),
    )
    crop_height = min(
        height,
        max(round(height * minimum_ratio), round(bottom - top + margin * 2)),
    )
    center_x = (left + right) / 2
    center_y = (top + bottom) / 2
    crop_left = min(max(round(center_x - crop_width / 2), 0), width - crop_width)
    crop_top = min(max(round(center_y - crop_height / 2), 0), height - crop_height)
    return crop_left, crop_top, crop_left + crop_width, crop_top + crop_height


def _draw_marker(
    draw: ImageDraw.ImageDraw,
    point: tuple[float, float],
    radius: int,
    label: str,
    *,
    selected: bool,
    font: ImageFont.FreeTypeFont | ImageFont.ImageFont | None,
) -> None:
    color = "#32d583" if selected else "#94a3b8"
    width = max(3, radius // 5) if selected else max(2, radius // 8)
    draw.ellipse(
        (
            point[0] - radius,
            point[1] - radius,
            point[0] + radius,
            point[1] + radius,
        ),
        outline=color,
        width=width,
    )
    if selected:
        draw.text(
            (round(point[0] + radius + 4), round(point[1] - radius - 2)),
            label,
            fill="#d1fae5",
            font=font,
            stroke_width=3,
            stroke_fill="#000000",
        )


def render_selected_occurrence_evidence(
    candidate_manifest: Mapping[str, Any],
    panel_index: Mapping[str, Any],
    selections: Sequence[Mapping[str, Any]],
    output_dir: Path | str,
) -> dict[str, Any]:
    """Render locator/close-up pairs for explicitly selected occurrence IDs."""

    destination = Path(output_dir).resolve()
    locator_dir = destination / "locators"
    closeup_dir = destination / "closeups"
    locator_dir.mkdir(parents=True, exist_ok=True)
    closeup_dir.mkdir(parents=True, exist_ok=True)
    group_by_id = {
        str(group["group_id"]): group
        for group in candidate_manifest.get("groups", [])
        if isinstance(group, Mapping) and group.get("group_id")
    }
    panels = panel_index.get("panels", {})
    if not isinstance(panels, Mapping):
        raise ValueError("panel index must contain a panels mapping")

    occurrence_claims: dict[str, set[tuple[str, str, str]]] = defaultdict(set)
    for index, selection in enumerate(selections, start=1):
        key = _selection_key(selection, index)
        name = _component_name(selection, key)
        location = _component_location(selection)
        for occurrence_id in _list(selection.get("selected_occurrence_ids")):
            occurrence_claims[str(occurrence_id)].add((key, name, location))
    conflicting_occurrences = {
        occurrence_id: [
            {
                "selection_key": key,
                "component_name": name,
                "room_or_location": location,
            }
            for key, name, location in sorted(claims)
        ]
        for occurrence_id, claims in occurrence_claims.items()
        if len(claims) > 1
    }

    records: list[dict[str, Any]] = []
    issues: list[dict[str, Any]] = []
    marker_font = _font(26)
    header_font = _font(24)
    for index, selection in enumerate(selections, start=1):
        key = _selection_key(selection, index)
        sequence = selection.get("sequence", index)
        name = _component_name(selection, key)
        location = _component_location(selection)
        group_ids = [
            str(value)
            for value in _list(selection.get("group_id") or selection.get("group_ids"))
            if value
        ]
        selected_ids = {
            str(value) for value in _list(selection.get("selected_occurrence_ids")) if value
        }
        row_conflicts = sorted(selected_ids & conflicting_occurrences.keys())
        row_record: dict[str, Any] = {
            "selection_key": key,
            "sequence": sequence,
            "name": name,
            "room_or_location": location,
            "decision": selection.get("decision"),
            "selected_occurrence_ids": sorted(selected_ids),
            "group_ids": group_ids,
            "state": "REVIEW",
            "reason_codes": [],
            "evidence": [],
        }
        if row_conflicts:
            row_record["state"] = "BLOCK"
            row_record["reason_codes"].append("OCCURRENCE_ASSIGNED_TO_MULTIPLE_COMPONENTS")
            issues.append(
                {
                    "code": "OCCURRENCE_ASSIGNED_TO_MULTIPLE_COMPONENTS",
                    "selection_key": key,
                    "occurrence_ids": row_conflicts,
                }
            )
            records.append(row_record)
            continue
        if not selected_ids:
            row_record["state"] = "MISSING"
            row_record["reason_codes"].append("NO_SELECTED_OCCURRENCE")
            records.append(row_record)
            continue

        rendered_selected_ids: set[str] = set()
        for group_index, group_id in enumerate(group_ids, start=1):
            group = group_by_id.get(group_id)
            if group is None:
                row_record["reason_codes"].append("CANDIDATE_GROUP_NOT_FOUND")
                continue
            expected_mt = _compact_identifier(selection.get("mt_code") or "")
            actual_mt = _compact_identifier(group.get("mt_code") or "")
            expected_page = _compact_identifier(
                selection.get("drawing_number") or selection.get("page") or ""
            )
            actual_page = _compact_identifier(group.get("drawing_number") or "")
            if expected_mt and actual_mt and expected_mt != actual_mt:
                row_record["state"] = "BLOCK"
                row_record["reason_codes"].append("MT_CODE_MISMATCH")
                row_record["evidence"] = []
                break
            if expected_page and actual_page and actual_page not in expected_page:
                row_record["state"] = "BLOCK"
                row_record["reason_codes"].append("DRAWING_NUMBER_MISMATCH")
                row_record["evidence"] = []
                break
            candidates = [
                candidate
                for candidate in group.get("candidates", [])
                if isinstance(candidate, Mapping) and candidate.get("occurrence_id")
            ]
            chosen = [
                candidate
                for candidate in candidates
                if str(candidate["occurrence_id"]) in selected_ids
            ]
            if not chosen:
                continue
            sheet_id = str(group.get("sheet_id") or "")
            panel_record = panels.get(sheet_id)
            if not isinstance(panel_record, Mapping) or not panel_record.get("absolute_path"):
                row_record["reason_codes"].append("PANEL_IMAGE_NOT_FOUND")
                continue
            panel_path = Path(str(panel_record["absolute_path"]))
            if not panel_path.is_file():
                row_record["reason_codes"].append("PANEL_IMAGE_FILE_MISSING")
                continue
            panel_bbox = group.get("panel_bbox") or panel_record.get("bbox")
            if not isinstance(panel_bbox, Sequence) or len(panel_bbox) != 4:
                row_record["reason_codes"].append("PANEL_BBOX_MISSING")
                continue
            x0, y0, x1, y1 = (float(value) for value in panel_bbox)
            if x1 <= x0 or y1 <= y0:
                row_record["reason_codes"].append("PANEL_BBOX_INVALID")
                continue
            with Image.open(panel_path) as source:
                panel = source.convert("RGB")
            width, height = panel.size

            candidate_points: dict[str, tuple[float, float]] = {}
            candidate_labels: dict[str, str] = {}
            outside_selected_ids: list[str] = []
            for candidate_index, candidate in enumerate(candidates, start=1):
                occurrence_id = str(candidate["occurrence_id"])
                candidate_labels[occurrence_id] = str(
                    candidate.get("label") or f"C{candidate_index}"
                )
                target = candidate.get("leader_target")
                if not isinstance(target, Sequence) or len(target) < 2:
                    continue
                point = (
                    (float(target[0]) - x0) / (x1 - x0) * width,
                    (y1 - float(target[1])) / (y1 - y0) * height,
                )
                if not (0 <= point[0] <= width and 0 <= point[1] <= height):
                    if occurrence_id in selected_ids:
                        outside_selected_ids.append(occurrence_id)
                    continue
                candidate_points[occurrence_id] = point
            actual_selected_labels = sorted(
                candidate_labels[occurrence_id]
                for occurrence_id in selected_ids
                if occurrence_id in candidate_labels
            )
            declared_selected_labels = sorted(_declared_labels(selection, group_id))
            if (
                declared_selected_labels
                and declared_selected_labels != actual_selected_labels
            ):
                row_record["state"] = "BLOCK"
                row_record["reason_codes"].append("SELECTED_LABELS_MISMATCH")
                row_record["evidence"] = []
                break
            if outside_selected_ids:
                row_record["reason_codes"].append("SELECTED_OCCURRENCE_OUTSIDE_PANEL_BBOX")
                row_record["outside_selected_occurrence_ids"] = sorted(outside_selected_ids)
            chosen_points = [
                candidate_points[str(candidate["occurrence_id"])]
                for candidate in chosen
                if str(candidate["occurrence_id"]) in candidate_points
            ]
            if not chosen_points:
                row_record["reason_codes"].append("SELECTED_OCCURRENCE_HAS_NO_POINT")
                continue
            framing_points = list(chosen_points)
            normalized_object_bbox: list[float] | None = None
            framing_basis = "LEADER_POINT_FALLBACK"
            object_bbox_value = _group_value(selection, "object_bbox", group_id)
            if object_bbox_value is not None:
                try:
                    object_points, normalized_object_bbox = _bbox_pixel_points(
                        object_bbox_value,
                        (x0, y0, x1, y1),
                        width,
                        height,
                    )
                except (TypeError, ValueError):
                    row_record["reason_codes"].append("OBJECT_BBOX_INVALID")
                    continue
                framing_points.extend(object_points)
                framing_basis = "OBJECT_BBOX_PLUS_LEADER"
            crop = _crop_box(framing_points, width, height)
            locator = panel.copy()
            locator_draw = ImageDraw.Draw(locator)
            locator_draw.rectangle(crop, outline="#32d583", width=max(5, min(width, height) // 250))
            locator_radius = max(12, round(min(width, height) * 0.012))
            for candidate in candidates:
                occurrence_id = str(candidate["occurrence_id"])
                point = candidate_points.get(occurrence_id)
                if point is None:
                    continue
                _draw_marker(
                    locator_draw,
                    point,
                    locator_radius,
                    candidate_labels[occurrence_id],
                    selected=occurrence_id in selected_ids,
                    font=marker_font,
                )
            locator_draw.rectangle((0, 0, width, 52), fill="#111820")
            locator_draw.text(
                (12, 10),
                f"{sequence} | {name} | {group.get('mt_code')} | {group.get('drawing_number')}",
                fill="#ffffff",
                font=header_font,
            )

            closeup_body = panel.crop(crop)
            closeup = Image.new(
                "RGB",
                (closeup_body.width, closeup_body.height + 52),
                "#111820",
            )
            closeup.paste(closeup_body, (0, 52))
            closeup_draw = ImageDraw.Draw(closeup)
            closeup_draw.text(
                (12, 10),
                f"{sequence} | {name} | selected occurrence",
                fill="#ffffff",
                font=header_font,
            )
            closeup_radius = max(11, round(min(closeup_body.size) * 0.025))
            for candidate in chosen:
                occurrence_id = str(candidate["occurrence_id"])
                point = candidate_points.get(occurrence_id)
                if point is None:
                    continue
                _draw_marker(
                    closeup_draw,
                    (point[0] - crop[0], point[1] - crop[1] + 52),
                    closeup_radius,
                    candidate_labels[occurrence_id],
                    selected=True,
                    font=marker_font,
                )

            try:
                sequence_stem = f"{int(sequence):03d}"
            except (TypeError, ValueError):
                sequence_stem = f"{index:03d}-{_safe_label(sequence)}"
            rendered_ids = {str(candidate["occurrence_id"]) for candidate in chosen}
            collision_guard = hashlib.sha256(
                "\0".join(
                    (
                        str(index),
                        key,
                        group_id,
                        ",".join(sorted(rendered_ids)),
                    )
                ).encode("utf-8")
            ).hexdigest()[:12]
            stem = (
                f"{sequence_stem}-{_safe_label(key)}-{group_index}-{collision_guard}"
            )
            locator_path = locator_dir / f"{stem}.png"
            closeup_path = closeup_dir / f"{stem}.png"
            locator.save(locator_path, format="PNG", optimize=True)
            closeup.save(closeup_path, format="PNG", optimize=True)
            rendered_selected_ids.update(rendered_ids)
            row_record["evidence"].append(
                {
                    "group_id": group_id,
                    "sheet_id": sheet_id,
                    "drawing_number": group.get("drawing_number"),
                    "selected_occurrence_ids": sorted(rendered_ids),
                    "selected_labels": [
                        candidate_labels[occurrence_id]
                        for occurrence_id in sorted(rendered_ids)
                    ],
                    "panel_bbox": [x0, y0, x1, y1],
                    "object_bbox": normalized_object_bbox,
                    "framing_basis": framing_basis,
                    "crop_box_px": list(crop),
                    "locator_image": str(locator_path.relative_to(destination)).replace(
                        "\\", "/"
                    ),
                    "closeup_image": str(closeup_path.relative_to(destination)).replace(
                        "\\", "/"
                    ),
                    "locator_pixel_size": list(locator.size),
                    "closeup_pixel_size": list(closeup.size),
                    "locator_sha256": sha256_file(locator_path),
                    "closeup_sha256": sha256_file(closeup_path),
                }
            )

        missing_selected = sorted(selected_ids - rendered_selected_ids)
        if missing_selected and row_record["state"] != "BLOCK":
            row_record["reason_codes"].append("SELECTED_OCCURRENCE_NOT_RENDERED")
            row_record["missing_selected_occurrence_ids"] = missing_selected
        if row_record["state"] == "BLOCK":
            pass
        elif row_record["evidence"] and not row_record["reason_codes"]:
            row_record["state"] = "CANDIDATE"
        elif not row_record["evidence"]:
            row_record["state"] = "MISSING"
        records.append(row_record)

    result = {
        "schema_version": "1.0",
        "purpose": "row_specific_candidate_evidence",
        "warning": (
            "Occurrence selection is not final component proof. Confirm object bbox, "
            "plan/elevation/detail links, dimensions, and quantity before PASS."
        ),
        "selection_count": len(selections),
        "rendered_selection_count": sum(bool(record["evidence"]) for record in records),
        "candidate_count": sum(record["state"] == "CANDIDATE" for record in records),
        "review_count": sum(record["state"] == "REVIEW" for record in records),
        "missing_count": sum(record["state"] == "MISSING" for record in records),
        "block_count": sum(record["state"] == "BLOCK" for record in records),
        "conflicting_occurrences": conflicting_occurrences,
        "issues": issues,
        "records": records,
    }
    write_json_atomic(destination / "selected_evidence.json", result)
    return result


__all__ = ["render_selected_occurrence_evidence"]
