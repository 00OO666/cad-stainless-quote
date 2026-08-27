"""Render every virtual CAD panel once for cross-drawing evidence review.

Occurrence crops only cover sheets that already contain a detected MT label.
That is insufficient for a plan -> elevation -> detail evidence chain because a
detail panel often contains dimensions but no repeated MT label.  This module
therefore renders an explicit panel catalog.  The catalog is still review
evidence: it does not choose which detail belongs to a component.
"""

from __future__ import annotations

import math
from collections import Counter, defaultdict
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw, ImageFont

from .io import sha256_file, write_json_atomic
from .render import _render_profile, _safe_label, render_regions


def panel_catalog_is_incomplete(result: Mapping[str, Any]) -> bool:
    """Return whether catalog diagnostics require a fail-closed CLI exit."""

    return any(
        result.get(key)
        for key in (
            "failures",
            "failure_count",
            "missing_requested_sheet_ids",
            "missing_requested_count",
            "unrendered_eligible_sheet_ids",
            "unrendered_eligible_count",
            "truncated_eligible_sheet_ids",
            "truncated_eligible_count",
            "truncated_requested_sheet_ids",
            "truncated_requested_count",
        )
    )


def render_panel_catalog(
    sheets: Sequence[Any],
    source_dxfs: Mapping[str, Path | str],
    output_dir: Path | str,
    *,
    maximum: int = 500,
    target_px: int = 2_600,
    render_profile: str = "cad-dark-full",
    sheet_ids: Sequence[str] | None = None,
) -> dict[str, Any]:
    """Render bounded viewport panels, including panels without MT occurrences.

    The catalog currently rebuilds requested panels rather than trusting an
    unkeyed filename cache.  This prevents a fast/dark image from silently
    surviving when the caller requests ``cad-dark-full``.
    """

    if maximum < 1:
        raise ValueError("maximum must be at least 1")
    if target_px < 256:
        raise ValueError("target_px must be at least 256")
    profile = _render_profile(render_profile)
    destination = Path(output_dir).resolve()
    panel_root = destination / "panels"
    panel_root.mkdir(parents=True, exist_ok=True)

    requested_sheet_ids = {str(value) for value in sheet_ids or () if value}
    all_eligible: list[Any] = []
    for sheet in sorted(sheets, key=lambda value: value.id):
        bbox = getattr(sheet, "bbox", None)
        source_id = str(getattr(sheet, "source_file_id", "") or "")
        layout = str(getattr(sheet, "layout", "") or "")
        if (
            bbox is None
            or len(bbox) != 4
            or "#viewport:" not in layout
            or source_id not in source_dxfs
            or (requested_sheet_ids and str(sheet.id) not in requested_sheet_ids)
        ):
            continue
        all_eligible.append(sheet)

    eligible = all_eligible[:maximum]
    truncated_eligible = all_eligible[maximum:]
    eligible_sheet_ids = {str(sheet.id) for sheet in eligible}
    truncated_eligible_sheet_ids = {str(sheet.id) for sheet in truncated_eligible}

    grouped: dict[str, list[Any]] = defaultdict(list)
    for sheet in eligible:
        grouped[str(sheet.source_file_id)].append(sheet)

    panel_records: dict[str, dict[str, Any]] = {}
    skipped_counts: Counter[str] = Counter()
    failures: list[dict[str, Any]] = []
    for source_id, source_sheets in sorted(grouped.items()):
        group_dir = panel_root / _safe_label(source_id)
        group_dir.mkdir(parents=True, exist_ok=True)
        regions = {str(sheet.id): sheet.bbox for sheet in source_sheets}
        try:
            rendered = render_regions(
                Path(source_dxfs[source_id]).resolve(),
                regions,
                group_dir,
                layout="Model",
                margin_ratio=0.0,
                target_px=target_px,
                mark_center=False,
                render_profile=profile["name"],
            )
        except Exception as exc:  # pragma: no cover - external CAD boundary
            failures.append(
                {
                    "source_file_id": source_id,
                    "sheet_ids": sorted(regions),
                    "error_type": type(exc).__name__,
                    "message": str(exc),
                }
            )
            continue
        skipped_counts.update(rendered.get("skipped_entity_type_counts", {}))
        by_id = {str(sheet.id): sheet for sheet in source_sheets}
        for sheet_id, record in rendered.get("regions", {}).items():
            sheet = by_id[sheet_id]
            image_path = group_dir / str(record["file"])
            panel_records[sheet_id] = {
                **record,
                "absolute_path": str(image_path.resolve()),
                "relative_path": str(image_path.relative_to(destination)).replace("\\", "/"),
                "image_sha256": sha256_file(image_path),
                "source_file_id": source_id,
                "drawing_number": getattr(sheet, "drawing_number", None),
                "title": getattr(sheet, "title", None),
                "kind": getattr(sheet, "kind", None),
                "panel_layout": getattr(sheet, "layout", None),
            }

    rendered_sheet_ids = set(panel_records)
    unrendered_eligible_sheet_ids = eligible_sheet_ids - rendered_sheet_ids
    truncated_requested_sheet_ids = requested_sheet_ids & truncated_eligible_sheet_ids
    missing_requested_sheet_ids = requested_sheet_ids - rendered_sheet_ids
    result = {
        "schema_version": "1.0",
        "purpose": "cross_drawing_panel_catalog_review_only",
        "path_scope": "local_run_diagnostics",
        "warning": (
            "A rendered panel is not a component-stage match. Relation evidence or an "
            "explicit reviewer selection is required before quotation export. absolute_path "
            "values contain local material paths and must not be published or committed; "
            "relative_path is authoritative."
        ),
        "requested_count": (
            len(requested_sheet_ids) if requested_sheet_ids else len(eligible_sheet_ids)
        ),
        "requested_sheet_ids": sorted(requested_sheet_ids),
        "missing_requested_count": len(missing_requested_sheet_ids),
        "missing_requested_sheet_ids": sorted(missing_requested_sheet_ids),
        "total_eligible_count": len(eligible_sheet_ids | truncated_eligible_sheet_ids),
        "eligible_count": len(eligible_sheet_ids),
        "eligible_sheet_ids": sorted(eligible_sheet_ids),
        "rendered_count": len(rendered_sheet_ids),
        "rendered_sheet_ids": sorted(rendered_sheet_ids),
        "unrendered_eligible_count": len(unrendered_eligible_sheet_ids),
        "unrendered_eligible_sheet_ids": sorted(unrendered_eligible_sheet_ids),
        "truncated_eligible_count": len(truncated_eligible_sheet_ids),
        "truncated_eligible_sheet_ids": sorted(truncated_eligible_sheet_ids),
        "truncated_requested_count": len(truncated_requested_sheet_ids),
        "truncated_requested_sheet_ids": sorted(truncated_requested_sheet_ids),
        "failure_count": len(failures),
        "render_profile": profile["name"],
        "target_px": target_px,
        "skipped_entity_count": sum(skipped_counts.values()),
        "skipped_entity_type_counts": dict(sorted(skipped_counts.items())),
        "failures": failures,
        "panels": panel_records,
    }
    write_json_atomic(destination / "panel_catalog.json", result)
    return result


def _annotation_font(size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont | None:
    path = Path("C:/Windows/Fonts/msyh.ttc")
    try:
        return ImageFont.truetype(str(path), size) if path.is_file() else None
    except OSError:
        return None


def _point(value: Any) -> tuple[float, float] | None:
    if (
        not isinstance(value, Sequence)
        or isinstance(value, str | bytes | bytearray)
        or len(value) < 2
    ):
        return None
    try:
        result = float(value[0]), float(value[1])
    except (TypeError, ValueError):
        return None
    return result if all(math.isfinite(part) for part in result) else None


def overlay_panel_catalog_annotations(
    panel_payload: Mapping[str, Any],
    panel_catalog: Mapping[str, Any],
    output_dir: Path | str,
) -> dict[str, Any]:
    """Overlay already projected paper text/leaders on raw model panel images.

    ``expand_viewport_panels`` projects relevant paper-space annotations into
    model coordinates.  Drawing those lightweight entities on the cached full
    model render is much faster and safer than rendering a complete paper
    layout with every title-block and proxy object.
    """

    destination = Path(output_dir).resolve()
    image_root = destination / "annotated-panels"
    image_root.mkdir(parents=True, exist_ok=True)
    entities_by_sheet: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for entity in panel_payload.get("entities", []):
        if (
            isinstance(entity, Mapping)
            and entity.get("sheet_id")
            and str(entity.get("id") or "").startswith("panel_paper_entity:")
        ):
            entities_by_sheet[str(entity["sheet_id"])].append(entity)

    records: dict[str, dict[str, Any]] = {}
    missing_images: list[str] = []
    for sheet_id, panel_record in sorted(panel_catalog.get("panels", {}).items()):
        if not isinstance(panel_record, Mapping):
            continue
        source_path = Path(str(panel_record.get("absolute_path") or ""))
        panel_bbox = panel_record.get("bbox")
        if (
            not source_path.is_file()
            or not isinstance(panel_bbox, Sequence)
            or len(panel_bbox) != 4
        ):
            missing_images.append(str(sheet_id))
            continue
        x0, y0, x1, y1 = (float(value) for value in panel_bbox)
        if x1 <= x0 or y1 <= y0:
            missing_images.append(str(sheet_id))
            continue
        with Image.open(source_path) as source:
            image = source.convert("RGB")
        width, height = image.size
        draw = ImageDraw.Draw(image)

        def pixel(
            point: Sequence[float],
            *,
            px0: float = x0,
            py0: float = y0,
            px1: float = x1,
            py1: float = y1,
            image_width: int = width,
            image_height: int = height,
        ) -> tuple[float, float]:
            return (
                (float(point[0]) - px0) / (px1 - px0) * image_width,
                (py1 - float(point[1])) / (py1 - py0) * image_height,
            )

        annotation_count = 0
        skipped_outside_count = 0
        for entity in entities_by_sheet.get(str(sheet_id), []):
            entity_type = str(entity.get("entity_type") or "").upper()
            geometry = entity.get("geometry") if isinstance(entity.get("geometry"), Mapping) else {}
            segments: list[list[tuple[float, float]]] = []
            for key in ("vertices", "points", "control_points", "fit_points"):
                values = geometry.get(key)
                if not isinstance(values, Sequence) or isinstance(values, str | bytes):
                    continue
                points = [value for raw in values if (value := _point(raw)) is not None]
                if len(points) >= 2:
                    segments.append(points)
                    break
            start = _point(geometry.get("start"))
            end = _point(geometry.get("end"))
            if start is not None and end is not None:
                segments.append([start, end])
            drawn = False
            for segment in segments:
                pixels = [pixel(value) for value in segment]
                if not any(0 <= value[0] <= width and 0 <= value[1] <= height for value in pixels):
                    continue
                draw.line(pixels, fill="#D8DEE9", width=max(1, round(min(width, height) / 900)))
                drawn = True

            text = str(entity.get("text") or "").strip()
            if text and entity_type in {"TEXT", "MTEXT", "ATTRIB"}:
                anchor = _point(entity.get("insert"))
                bbox = entity.get("bbox")
                if anchor is None and isinstance(bbox, Sequence) and len(bbox) == 4:
                    anchor = float(bbox[0]), float(bbox[3])
                if anchor is not None:
                    target = pixel(anchor)
                    if 0 <= target[0] <= width and 0 <= target[1] <= height:
                        font_px = 18
                        if isinstance(bbox, Sequence) and len(bbox) == 4:
                            cad_height = abs(float(bbox[3]) - float(bbox[1]))
                            font_px = round(cad_height / (y1 - y0) * height * 0.85)
                        font = _annotation_font(min(42, max(11, font_px)))
                        draw.text(
                            target,
                            text,
                            fill="#FFF4A3",
                            font=font,
                            stroke_width=1,
                            stroke_fill="#111820",
                        )
                        drawn = True
                    else:
                        skipped_outside_count += 1
            if drawn:
                annotation_count += 1

        target_path = image_root / f"{_safe_label(str(sheet_id))}.png"
        image.save(target_path, format="PNG", optimize=True)
        records[str(sheet_id)] = {
            **panel_record,
            "absolute_path": str(target_path.resolve()),
            "relative_path": str(target_path.relative_to(destination)).replace("\\", "/"),
            "image_sha256": sha256_file(target_path),
            "annotation_overlay": True,
            "annotation_count": annotation_count,
            "annotation_skipped_outside_count": skipped_outside_count,
        }

    result = {
        "schema_version": "1.0",
        "purpose": "full_model_panel_plus_projected_paper_annotations",
        "path_scope": "local_run_diagnostics",
        "warning": (
            "Projected annotations improve review fidelity but do not reproduce CTB/STB, "
            "missing SHX fonts, unresolved Xrefs, or unsupported proxy objects. absolute_path "
            "values contain local material paths and must not be published or committed; "
            "relative_path is authoritative."
        ),
        "source_render_profile": panel_catalog.get("render_profile"),
        "panel_count": len(records),
        "annotation_count": sum(value["annotation_count"] for value in records.values()),
        "missing_image_sheet_ids": missing_images,
        "panels": records,
    }
    write_json_atomic(destination / "panel_catalog_annotated.json", result)
    return result


def merge_panel_catalogs(
    catalogs: Sequence[Mapping[str, Any]],
    output_dir: Path | str,
) -> dict[str, Any]:
    """Merge independently rendered catalog shards with conflict checks."""

    if not catalogs:
        raise ValueError("At least one panel catalog is required")
    destination = Path(output_dir).resolve()
    destination.mkdir(parents=True, exist_ok=True)
    profiles = {str(value.get("render_profile") or "") for value in catalogs}
    target_sizes = {int(value.get("target_px") or 0) for value in catalogs}
    if len(profiles) != 1 or len(target_sizes) != 1:
        raise ValueError("Panel catalog shards must use one render profile and target_px")
    panels: dict[str, dict[str, Any]] = {}
    failures: list[dict[str, Any]] = []
    requested_sheet_ids: set[str] = set()
    eligible_sheet_ids: set[str] = set()
    unrendered_eligible_sheet_ids: set[str] = set()
    truncated_eligible_sheet_ids: set[str] = set()
    truncated_requested_sheet_ids: set[str] = set()
    skipped_counts: Counter[str] = Counter()
    for catalog_index, catalog in enumerate(catalogs, start=1):
        requested_sheet_ids.update(str(value) for value in catalog.get("requested_sheet_ids", []))
        eligible_sheet_ids.update(str(value) for value in catalog.get("eligible_sheet_ids", []))
        unrendered_eligible_sheet_ids.update(
            str(value) for value in catalog.get("unrendered_eligible_sheet_ids", [])
        )
        truncated_eligible_sheet_ids.update(
            str(value) for value in catalog.get("truncated_eligible_sheet_ids", [])
        )
        truncated_requested_sheet_ids.update(
            str(value) for value in catalog.get("truncated_requested_sheet_ids", [])
        )
        skipped_counts.update(catalog.get("skipped_entity_type_counts", {}))
        failures.extend(catalog.get("failures", []))
        for sheet_id, record in catalog.get("panels", {}).items():
            if not isinstance(record, Mapping):
                continue
            normalized = dict(record)
            existing = panels.get(str(sheet_id))
            if existing is not None and (
                existing.get("image_sha256") != normalized.get("image_sha256")
                or existing.get("bbox") != normalized.get("bbox")
            ):
                raise ValueError(
                    f"Conflicting panel catalog shard for {sheet_id} at input {catalog_index}"
                )
            panels[str(sheet_id)] = normalized
            eligible_sheet_ids.add(str(sheet_id))
    rendered_sheet_ids = set(panels)
    missing_requested_sheet_ids = requested_sheet_ids - rendered_sheet_ids
    unrendered_eligible_sheet_ids.update(eligible_sheet_ids - rendered_sheet_ids)
    unrendered_eligible_sheet_ids -= rendered_sheet_ids
    truncated_eligible_sheet_ids -= rendered_sheet_ids
    truncated_requested_sheet_ids -= rendered_sheet_ids
    result = {
        "schema_version": "1.0",
        "purpose": "merged_cross_drawing_panel_catalog_review_only",
        "path_scope": "local_run_diagnostics",
        "warning": (
            "Merged panel images remain relation candidates until an explicit component-stage "
            "selection confirms them. absolute_path values contain local material paths and "
            "must not be published or committed; relative_path is authoritative."
        ),
        "shard_count": len(catalogs),
        "render_profile": next(iter(profiles)),
        "target_px": next(iter(target_sizes)),
        "requested_count": len(requested_sheet_ids),
        "requested_sheet_ids": sorted(requested_sheet_ids),
        "missing_requested_count": len(missing_requested_sheet_ids),
        "missing_requested_sheet_ids": sorted(missing_requested_sheet_ids),
        "total_eligible_count": len(eligible_sheet_ids | truncated_eligible_sheet_ids),
        "eligible_count": len(eligible_sheet_ids),
        "eligible_sheet_ids": sorted(eligible_sheet_ids),
        "rendered_count": len(rendered_sheet_ids),
        "rendered_sheet_ids": sorted(rendered_sheet_ids),
        "unrendered_eligible_count": len(unrendered_eligible_sheet_ids),
        "unrendered_eligible_sheet_ids": sorted(unrendered_eligible_sheet_ids),
        "truncated_eligible_count": len(truncated_eligible_sheet_ids),
        "truncated_eligible_sheet_ids": sorted(truncated_eligible_sheet_ids),
        "truncated_requested_count": len(truncated_requested_sheet_ids),
        "truncated_requested_sheet_ids": sorted(truncated_requested_sheet_ids),
        "failure_count": len(failures),
        "skipped_entity_count": sum(skipped_counts.values()),
        "skipped_entity_type_counts": dict(sorted(skipped_counts.items())),
        "failures": failures,
        "panels": panels,
    }
    write_json_atomic(destination / "panel_catalog.json", result)
    return result


__all__ = [
    "merge_panel_catalogs",
    "overlay_panel_catalog_annotations",
    "panel_catalog_is_incomplete",
    "render_panel_catalog",
]
