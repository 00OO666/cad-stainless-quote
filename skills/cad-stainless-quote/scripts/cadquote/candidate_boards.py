"""Render auditable, numbered boards for ambiguous MT occurrences.

Candidate boards are a review bridge, not a quantity prediction.  They make
every same-sheet/same-MT occurrence visible at once so a reviewer (human or
vision model) can bind one or more labels to a physical component without the
unsafe historical rule of taking the first rendered occurrence.
"""

from __future__ import annotations

import hashlib
from collections import defaultdict
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw, ImageFont

from .io import write_json_atomic
from .render import render_panel_occurrence_crops


def _safe_label(value: str) -> str:
    return "".join(character if character.isalnum() else "_" for character in value)[:96]


def _font(size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont | None:
    path = Path("C:/Windows/Fonts/msyh.ttc")
    try:
        return ImageFont.truetype(str(path), size) if path.is_file() else None
    except OSError:
        return None


def _crop_box(
    points: Sequence[tuple[float, float]],
    width: int,
    height: int,
) -> tuple[int, int, int, int]:
    """Keep all labels visible while retaining enough panel context."""

    minimum_ratio = 0.55 if len(points) > 1 else 0.38
    minimum_width = max(360, round(width * minimum_ratio))
    minimum_height = max(300, round(height * minimum_ratio))
    left = min(point[0] for point in points)
    right = max(point[0] for point in points)
    top = min(point[1] for point in points)
    bottom = max(point[1] for point in points)
    marker_margin = max(90, round(min(width, height) * 0.07))
    content_width = max(minimum_width, round(right - left + marker_margin * 2))
    content_height = max(minimum_height, round(bottom - top + marker_margin * 2))
    content_width = min(width, content_width)
    content_height = min(height, content_height)
    center_x = (left + right) / 2
    center_y = (top + bottom) / 2
    crop_left = min(max(round(center_x - content_width / 2), 0), width - content_width)
    crop_top = min(max(round(center_y - content_height / 2), 0), height - content_height)
    return crop_left, crop_top, crop_left + content_width, crop_top + content_height


def render_occurrence_candidate_boards(
    sheets: Sequence[Any],
    occurrences: Sequence[Any],
    source_dxfs: Mapping[str, Path | str],
    output_dir: Path | str,
    *,
    maximum_groups: int = 200,
    target_px: int = 2_600,
    render_profile: str = "cad-dark",
) -> dict[str, Any]:
    """Render one numbered review board for each sheet/MT candidate group.

    The output deliberately remains ``REVIEW`` even for a one-occurrence group:
    occurrence uniqueness does not prove physical-component identity, node
    linkage, quantity, or dimension roles.
    """

    if maximum_groups < 1:
        raise ValueError("maximum_groups must be at least 1")
    destination = Path(output_dir).resolve()
    panel_crop_dir = destination / "panel-cache"
    board_dir = destination / "boards"
    locator_dir = destination / "locators"
    board_dir.mkdir(parents=True, exist_ok=True)
    locator_dir.mkdir(parents=True, exist_ok=True)
    sheet_by_id = {sheet.id: sheet for sheet in sheets}
    grouped: dict[tuple[str, str], list[Any]] = defaultdict(list)
    for occurrence in occurrences:
        sheet = sheet_by_id.get(occurrence.sheet_id)
        point = occurrence.leader_target or occurrence.anchor
        if (
            sheet is None
            or sheet.bbox is None
            or point is None
            or "#viewport:" not in sheet.layout
            or occurrence.source_file_id not in source_dxfs
        ):
            continue
        grouped[(sheet.id, occurrence.mt_code)].append(occurrence)
    selected_keys = sorted(grouped)[:maximum_groups]
    selected_occurrences = [
        occurrence
        for key in selected_keys
        for occurrence in sorted(grouped[key], key=lambda value: value.id)
    ]
    panel_result = render_panel_occurrence_crops(
        sheets,
        selected_occurrences,
        {key: Path(value).resolve() for key, value in source_dxfs.items()},
        panel_crop_dir,
        maximum=max(1, len(selected_occurrences)),
        target_px=target_px,
        crop_ratio=0.38,
        render_profile=render_profile,
    )
    panel_records = panel_result.get("panels", {})
    records: list[dict[str, Any]] = []
    label_font = _font(30)
    header_font = _font(28)
    for sheet_id, mt_code in selected_keys:
        panel_record = panel_records.get(sheet_id)
        sheet = sheet_by_id[sheet_id]
        if not isinstance(panel_record, Mapping) or not panel_record.get("absolute_path"):
            continue
        panel_path = Path(str(panel_record["absolute_path"]))
        with Image.open(panel_path) as source_image:
            panel = source_image.convert("RGB")
        width, height = panel.size
        x0, y0, x1, y1 = sheet.bbox
        if x1 <= x0 or y1 <= y0:
            continue
        candidates: list[dict[str, Any]] = []
        pixel_points: list[tuple[float, float]] = []
        for index, occurrence in enumerate(
            sorted(grouped[(sheet_id, mt_code)], key=lambda value: value.id),
            start=1,
        ):
            point = occurrence.leader_target or occurrence.anchor
            pixel = (
                (point[0] - x0) / (x1 - x0) * width,
                (y1 - point[1]) / (y1 - y0) * height,
            )
            pixel_points.append(pixel)
            candidates.append(
                {
                    "label": f"C{index}",
                    "occurrence_id": occurrence.id,
                    "leader_target": [float(point[0]), float(point[1])],
                    "panel_pixel": [round(pixel[0], 3), round(pixel[1], 3)],
                    "entity_ids": sorted(set(occurrence.entity_ids)),
                    "leader_entity_id": occurrence.leader_entity_id,
                }
            )
        annotated = panel.copy()
        draw = ImageDraw.Draw(annotated)
        marker_radius = max(16, round(min(width, height) * 0.018))
        for candidate, (pixel_x, pixel_y) in zip(candidates, pixel_points, strict=True):
            draw.ellipse(
                (
                    pixel_x - marker_radius,
                    pixel_y - marker_radius,
                    pixel_x + marker_radius,
                    pixel_y + marker_radius,
                ),
                outline="#ff3155",
                width=max(4, marker_radius // 5),
            )
            label_box = (
                round(pixel_x + marker_radius),
                round(pixel_y - marker_radius - 4),
            )
            draw.text(
                label_box,
                candidate["label"],
                fill="#ffe66d",
                font=label_font,
                stroke_width=3,
                stroke_fill="#000000",
            )
        header = (
            f"{mt_code} | {sheet.drawing_number or sheet.title or sheet.layout} | "
            f"候选 {len(candidates)}"
        )
        draw.rectangle((0, 0, width, 54), fill="#111820")
        draw.text((14, 10), header, fill="#ffffff", font=header_font)
        digest = hashlib.sha256(f"{sheet_id}|{mt_code}".encode()).hexdigest()[:12]
        stem = f"{_safe_label(mt_code)}-{digest}"
        locator_path = locator_dir / f"{stem}.png"
        annotated.save(locator_path, format="PNG", optimize=True)
        crop = _crop_box(pixel_points, width, height)
        board_path = board_dir / f"{stem}.png"
        annotated.crop(crop).save(board_path, format="PNG", optimize=True)
        records.append(
            {
                "group_id": f"candidate-group:{digest}",
                "sheet_id": sheet_id,
                "source_file_id": sheet.source_file_id,
                "mt_code": mt_code,
                "drawing_number": sheet.drawing_number,
                "layout": sheet.layout,
                "candidate_count": len(candidates),
                "state": "REVIEW",
                "reason_codes": (
                    ["MULTIPLE_OCCURRENCES_SAME_SHEET_MT"]
                    if len(candidates) > 1
                    else ["PHYSICAL_COMPONENT_NOT_CONFIRMED"]
                ),
                "locator_file": str(locator_path.relative_to(destination)).replace("\\", "/"),
                "board_file": str(board_path.relative_to(destination)).replace("\\", "/"),
                "board_crop_box": list(crop),
                "panel_bbox": list(sheet.bbox),
                "candidates": candidates,
            }
        )
    result = {
        "schema_version": "1.0",
        "purpose": "candidate_review_only",
        "warning": (
            "Numbered candidates are not predictions. Select physical components, "
            "elevation/detail links, dimension roles and quantity evidence before export."
        ),
        "selection_contract": {
            "required_fields": [
                "component_id",
                "selected_occurrence_ids",
                "object_bbox",
                "component_name",
                "room_or_location",
            ],
            "forbid_first_candidate_default": True,
            "forbid_cross_name_image_reuse": True,
        },
        "requested_group_count": min(len(grouped), maximum_groups),
        "rendered_group_count": len(records),
        "ambiguous_group_count": sum(record["candidate_count"] > 1 for record in records),
        "render_profile": render_profile,
        "groups": records,
    }
    write_json_atomic(destination / "candidate_boards.json", result)
    return result


__all__ = ["render_occurrence_candidate_boards"]
