"""Render bounded DXF evidence images while retaining coordinate provenance."""

from __future__ import annotations

import json
import math
import re
from collections import Counter, defaultdict
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import ezdxf
from ezdxf import bbox as ezbbox
from ezdxf import recover

from .io import write_json_atomic

Region = tuple[float, float, float, float]

# Filled polygon entities can dominate both bbox generation and matplotlib
# rendering while adding no usable line/text/dimension evidence.  Keep this
# list deliberately narrow: SOLID/TRACE/WIPEOUT may carry meaningful geometry.
SKIPPED_RENDER_ENTITY_TYPES = frozenset({"HATCH", "MPOLYGON"})
# Raw evidence crops favor explicit linework and dimensions. Expanding INSERT
# block trees is the main source of pathological render latency and can pull in
# thousands of decorative entities unrelated to the crop. The normalized CAD
# index still retains every INSERT/ATTRIB handle for audit and MT association.
SKIPPED_RAW_RENDER_ENTITY_TYPES = frozenset({*SKIPPED_RENDER_ENTITY_TYPES, "INSERT", "WIPEOUT"})

RENDER_PROFILES = frozenset({"white-fast", "cad-dark", "cad-dark-full"})


def _render_profile(name: str) -> dict[str, Any]:
    """Return the visual and entity policy for a bounded evidence render.

    ``white-fast`` preserves the historical diagnostic output.  The dark
    profiles are intended for evidence embedded in a quote workbook, where a
    CAD-like canvas and visible layer colours are materially easier to compare
    with a human AutoCAD screenshot.  ``cad-dark-full`` is deliberately opt-in:
    expanding blocks and hatches can be expensive on decorative drawings.
    """

    normalized = str(name).strip().casefold()
    if normalized not in RENDER_PROFILES:
        raise ValueError(
            f"Unknown render profile {name!r}; expected one of {', '.join(sorted(RENDER_PROFILES))}"
        )
    dark = normalized.startswith("cad-dark")
    return {
        "name": normalized,
        "background": "#111820" if dark else "#FFFFFF",
        "label_foreground": "#FFFFFF" if dark else "#111111",
        "label_background": "#000000" if dark else "#FFFFFF",
        "label_border": "none" if dark else "#888888",
        "skipped_types": (
            frozenset({"WIPEOUT"})
            if normalized == "cad-dark-full"
            else SKIPPED_RAW_RENDER_ENTITY_TYPES
        ),
    }


def _safe_label(value: str) -> str:
    label = re.sub(r"[^0-9A-Za-z._\u4e00-\u9fff-]+", "_", value).strip(" ._")
    return label[:100] or "region"


def _read_document(path: Path) -> Any:
    try:
        return ezdxf.readfile(path)
    except (OSError, ezdxf.DXFError):
        document, auditor = recover.readfile(path)
        if auditor.has_errors:
            document.audit()
        return document


def _validate_region(value: Sequence[float]) -> Region:
    if len(value) != 4:
        raise ValueError("A render region must contain x0, y0, x1, y1")
    x0, y0, x1, y1 = (float(part) for part in value)
    if x1 <= x0 or y1 <= y0:
        raise ValueError(f"Invalid render region: {value!r}")
    return x0, y0, x1, y1


def viewport_model_regions(
    dxf_path: Path | str,
    *,
    layout: str,
    max_paper_extent: float = 2_000.0,
) -> dict[str, Region]:
    """Return approximate model-space regions represented by paper-space viewports."""

    document = _read_document(Path(dxf_path).resolve())
    paper = document.layout(layout)
    regions: dict[str, Region] = {}
    for viewport in paper.query("VIEWPORT"):
        width = float(viewport.dxf.get("width", 0.0) or 0.0)
        height = float(viewport.dxf.get("height", 0.0) or 0.0)
        if width <= 0 or height <= 0 or width > max_paper_extent or height > max_paper_extent:
            continue
        try:
            raw = viewport.get_modelspace_limits()
            region = _validate_region(raw)
        except Exception:
            continue
        target = viewport.dxf.get("view_target_point")
        if target is not None and (abs(target.x) > 1e-6 or abs(target.y) > 1e-6):
            region = (
                region[0] + float(target.x),
                region[1] + float(target.y),
                region[2] + float(target.x),
                region[3] + float(target.y),
            )
        regions[str(viewport.dxf.handle or len(regions) + 1)] = region
    return regions


def _render_with_matplotlib(
    document: Any,
    layout_name: str,
    regions: Mapping[str, Region],
    output_dir: Path,
    margin_ratio: float,
    target_px: int,
    mark_center: bool,
    render_profile: str,
) -> tuple[dict[str, dict[str, Any]], dict[str, int]]:
    import matplotlib

    matplotlib.use("Agg", force=True)
    from ezdxf.addons.drawing import Frontend, RenderContext
    from ezdxf.addons.drawing.config import BackgroundPolicy, ColorPolicy, Configuration
    from ezdxf.addons.drawing.matplotlib import MatplotlibBackend
    from matplotlib.backends.backend_agg import FigureCanvasAgg
    from matplotlib.figure import Figure

    profile = _render_profile(render_profile)
    background = profile["background"]
    skipped_types = profile["skipped_types"]
    space = document.modelspace() if layout_name == "Model" else document.layout(layout_name)
    context = RenderContext(document)
    context.set_current_layout(space)
    context.current_layout_properties.set_colors(background)
    configuration = Configuration(
        background_policy=BackgroundPolicy.CUSTOM,
        custom_bg_color=background,
        color_policy=(ColorPolicy.COLOR_SWAP_BW if background != "#FFFFFF" else ColorPolicy.COLOR),
    )
    cache = ezbbox.Cache()
    bounded: list[tuple[Any, Region]] = []
    skipped_type_counts: Counter[str] = Counter()
    for entity in space:
        entity_type = entity.dxftype().upper()
        if entity_type in skipped_types:
            skipped_type_counts[entity_type] += 1
            continue
        try:
            extents = ezbbox.extents([entity], fast=True, cache=cache)
        except Exception:
            continue
        if extents.has_data:
            bounded.append(
                (
                    entity,
                    (
                        float(extents.extmin.x),
                        float(extents.extmin.y),
                        float(extents.extmax.x),
                        float(extents.extmax.y),
                    ),
                )
            )

    rendered: dict[str, dict[str, Any]] = {}
    used_names: set[str] = set()

    class EvidenceFrontend(Frontend):
        """Apply the evidence filter to top-level and recursively drawn entities."""

        def draw_entities(self, entities: Any, *, filter_func: Any = None) -> None:
            def evidence_filter(entity: Any) -> bool:
                if entity.dxftype().upper() in skipped_types:
                    return False
                return filter_func(entity) if filter_func is not None else True

            super().draw_entities(entities, filter_func=evidence_filter)

    for label, region in regions.items():
        x0, y0, x1, y1 = region
        margin_x = (x1 - x0) * margin_ratio
        margin_y = (y1 - y0) * margin_ratio
        expanded = (x0 - margin_x, y0 - margin_y, x1 + margin_x, y1 + margin_y)
        entities = [
            entity
            for entity, box in bounded
            if box[0] <= expanded[2]
            and box[2] >= expanded[0]
            and box[1] <= expanded[3]
            and box[3] >= expanded[1]
        ]
        if not entities:
            continue
        width = expanded[2] - expanded[0]
        height = expanded[3] - expanded[1]
        long_edge = max(width, height)
        width_px = max(1, round(target_px * width / long_edge))
        height_px = max(1, round(target_px * height / long_edge))
        dpi = 100
        figure = Figure(
            figsize=(width_px / dpi, height_px / dpi),
            dpi=dpi,
            facecolor=background,
        )
        canvas = FigureCanvasAgg(figure)
        axes = figure.add_axes((0, 0, 1, 1))
        backend = MatplotlibBackend(axes, adjust_figure=False)
        backend.set_background(background)
        EvidenceFrontend(context, backend, config=configuration).draw_entities(entities)
        backend.finalize()
        if mark_center:
            center_x = (region[0] + region[2]) / 2
            center_y = (region[1] + region[3]) / 2
            marker_radius = min(width, height) * 0.025
            axes.scatter(
                [center_x],
                [center_y],
                s=70,
                facecolors="none",
                edgecolors="#ff3131",
                zorder=10,
            )
            axes.plot(
                [center_x - marker_radius, center_x + marker_radius],
                [center_y, center_y],
                color="#ff3131",
                linewidth=1.0,
                zorder=10,
            )
            axes.plot(
                [center_x, center_x],
                [center_y - marker_radius, center_y + marker_radius],
                color="#ff3131",
                linewidth=1.0,
                zorder=10,
            )
        axes.text(
            0.01,
            0.99,
            label,
            transform=axes.transAxes,
            ha="left",
            va="top",
            color=profile["label_foreground"],
            fontsize=7,
            bbox={
                "facecolor": profile["label_background"],
                "alpha": 0.82,
                "edgecolor": profile["label_border"],
            },
            zorder=11,
        )
        axes.set_xlim(expanded[0], expanded[2])
        axes.set_ylim(expanded[1], expanded[3])
        axes.set_aspect("equal", adjustable="box")
        axes.margins(0)
        stem = _safe_label(label)
        candidate = stem
        suffix = 2
        while candidate.casefold() in used_names:
            candidate = f"{stem}_{suffix}"
            suffix += 1
        used_names.add(candidate.casefold())
        destination = output_dir / f"{candidate}.png"
        canvas.print_png(destination)
        figure.clear()
        rendered[label] = {
            "file": destination.name,
            "bbox": list(region),
            "layout": layout_name,
            "entity_count": len(entities),
            "backend": "matplotlib-agg",
            "render_profile": profile["name"],
        }
    return rendered, dict(sorted(skipped_type_counts.items()))


def render_regions(
    dxf_path: Path | str,
    regions: Mapping[str, Sequence[float]],
    output_dir: Path | str,
    *,
    layout: str = "Model",
    margin_ratio: float = 0.04,
    target_px: int = 2_200,
    mark_center: bool = True,
    render_profile: str = "white-fast",
) -> dict[str, Any]:
    """Render named regions and save an index that maps every image back to CAD coordinates."""

    if not 0 <= margin_ratio <= 1:
        raise ValueError("margin_ratio must be between 0 and 1")
    if target_px < 256:
        raise ValueError("target_px must be at least 256")
    profile = _render_profile(render_profile)
    source = Path(dxf_path).resolve()
    destination = Path(output_dir).resolve()
    destination.mkdir(parents=True, exist_ok=True)
    normalized = {str(label): _validate_region(value) for label, value in regions.items()}
    if not normalized:
        raise ValueError("At least one render region is required")
    document = _read_document(source)
    if layout != "Model" and layout not in document.layout_names():
        raise ValueError(f"DXF layout not found: {layout}")
    rendered, skipped_type_counts = _render_with_matplotlib(
        document,
        layout,
        normalized,
        destination,
        margin_ratio,
        target_px,
        mark_center,
        profile["name"],
    )
    result = {
        "schema_version": "1.1",
        "source": str(source),
        "layout": layout,
        "requested_count": len(normalized),
        "rendered_count": len(rendered),
        "skipped_entity_count": sum(skipped_type_counts.values()),
        "skipped_entity_type_counts": skipped_type_counts,
        "skipped_entity_types": sorted(profile["skipped_types"]),
        "render_profile": profile["name"],
        "regions": rendered,
    }
    write_json_atomic(destination / "index.json", result)
    return result


def load_regions_json(path: Path | str) -> dict[str, Region]:
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("Regions JSON must be an object mapping labels to bboxes")
    return {str(label): _validate_region(value) for label, value in payload.items()}


def _indexed_point(value: Any) -> tuple[float, float] | None:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)) or len(value) < 2:
        return None
    try:
        point = float(value[0]), float(value[1])
    except (TypeError, ValueError):
        return None
    return point if all(math.isfinite(part) for part in point) else None


def _indexed_entity_segments(entity: Any) -> list[list[tuple[float, float]]]:
    """Extract lightweight linework from an indexed semantic CAD entity."""

    geometry = entity.geometry
    segments: list[list[tuple[float, float]]] = []
    start = _indexed_point(geometry.get("start"))
    end = _indexed_point(geometry.get("end"))
    if start is not None and end is not None:
        segments.append([start, end])
    for key in ("vertices", "points", "control_points", "fit_points"):
        raw_points = geometry.get(key)
        if not isinstance(raw_points, Sequence) or isinstance(raw_points, (str, bytes)):
            continue
        points = [point for value in raw_points if (point := _indexed_point(value)) is not None]
        if len(points) >= 2:
            segments.append(points)
            break
    return segments


def _indexed_intersects(entity: Any, region: Region) -> bool:
    if entity.bbox is not None:
        return (
            entity.bbox[0] <= region[2]
            and entity.bbox[2] >= region[0]
            and entity.bbox[1] <= region[3]
            and entity.bbox[3] >= region[1]
        )
    point = entity.insert
    return bool(point and region[0] <= point[0] <= region[2] and region[1] <= point[1] <= region[3])


def render_indexed_occurrences(
    sheets: Sequence[Any],
    entities: Sequence[Any],
    occurrences: Sequence[Any],
    output_dir: Path | str,
    *,
    maximum: int = 250,
    target_px: int = 1_200,
) -> dict[str, Any]:
    """Render fast, auditable evidence crops from the normalized CAD index.

    Unlike raw-DXF rendering this includes paper annotations already projected
    into a viewport panel, so the MT label and arrow remain visible beside the
    model geometry.  It deliberately draws only semantic line/text primitives;
    decorative fills are excluded and counted.
    """

    if maximum < 1:
        raise ValueError("maximum must be at least 1")
    if target_px < 256:
        raise ValueError("target_px must be at least 256")
    import matplotlib

    matplotlib.use("Agg", force=True)
    from matplotlib.backends.backend_agg import FigureCanvasAgg
    from matplotlib.figure import Figure
    from matplotlib.font_manager import FontProperties
    from matplotlib.patches import Arc, Circle

    destination = Path(output_dir).resolve()
    destination.mkdir(parents=True, exist_ok=True)
    sheet_by_id = {sheet.id: sheet for sheet in sheets}
    entities_by_sheet: dict[str, list[Any]] = {}
    for entity in entities:
        if entity.sheet_id:
            entities_by_sheet.setdefault(entity.sheet_id, []).append(entity)
    font_path = Path("C:/Windows/Fonts/msyh.ttc")
    font = FontProperties(fname=str(font_path)) if font_path.is_file() else None
    records: dict[str, dict[str, Any]] = {}
    skipped_counts: Counter[str] = Counter()
    used_names: set[str] = set()
    for occurrence in sorted(occurrences, key=lambda value: value.id)[:maximum]:
        sheet = sheet_by_id.get(occurrence.sheet_id)
        point = occurrence.leader_target or occurrence.anchor
        if sheet is None or point is None:
            continue
        radius = 600.0
        if sheet.bbox is not None:
            diagonal = math.hypot(
                sheet.bbox[2] - sheet.bbox[0],
                sheet.bbox[3] - sheet.bbox[1],
            )
            radius = max(180.0, min(1_800.0, diagonal * 0.055))
        region = (point[0] - radius, point[1] - radius, point[0] + radius, point[1] + radius)
        selected = [
            entity
            for entity in entities_by_sheet.get(sheet.id, ())
            if _indexed_intersects(entity, region)
        ]
        if not selected:
            continue
        dpi = 100
        figure = Figure(
            figsize=(target_px / dpi, target_px / dpi),
            dpi=dpi,
            facecolor="#111820",
        )
        canvas = FigureCanvasAgg(figure)
        axes = figure.add_axes((0, 0, 1, 1), facecolor="#111820")
        highlighted = set(occurrence.entity_ids)
        if occurrence.leader_entity_id:
            highlighted.add(occurrence.leader_entity_id)
        drawn = 0
        for entity in selected:
            entity_type = entity.entity_type.upper()
            if entity_type in SKIPPED_RENDER_ENTITY_TYPES:
                skipped_counts[entity_type] += 1
                continue
            color = "#ff4fd8" if entity.id in highlighted else "#69d2e7"
            if entity_type in {"DIMENSION", "ARC_DIMENSION", "LARGE_RADIAL_DIMENSION"}:
                color = "#5078ff"
            segments = _indexed_entity_segments(entity)
            for segment in segments:
                axes.plot(
                    [value[0] for value in segment],
                    [value[1] for value in segment],
                    color=color,
                    linewidth=1.6 if entity.id in highlighted else 0.55,
                    alpha=0.95,
                )
                drawn += 1
            center = _indexed_point(entity.geometry.get("center"))
            try:
                raw_radius = float(entity.geometry.get("radius") or 0.0)
            except (TypeError, ValueError):
                raw_radius = 0.0
            if center is not None and raw_radius > 0 and math.isfinite(raw_radius):
                if entity_type == "ARC":
                    start_angle = float(entity.geometry.get("start_angle") or 0.0)
                    end_angle = float(entity.geometry.get("end_angle") or 360.0)
                    axes.add_patch(
                        Arc(
                            center,
                            raw_radius * 2,
                            raw_radius * 2,
                            theta1=start_angle,
                            theta2=end_angle,
                            color=color,
                            linewidth=0.6,
                        )
                    )
                else:
                    axes.add_patch(
                        Circle(center, raw_radius, fill=False, color=color, linewidth=0.6)
                    )
                drawn += 1
            if entity.text:
                text_point = entity.insert
                if text_point is None and entity.bbox is not None:
                    text_point = (
                        (entity.bbox[0] + entity.bbox[2]) / 2,
                        (entity.bbox[1] + entity.bbox[3]) / 2,
                    )
                if text_point is not None:
                    axes.text(
                        text_point[0],
                        text_point[1],
                        entity.text[:80],
                        color="#ffea70" if entity.id not in highlighted else "#ff4fd8",
                        fontsize=5.5,
                        fontproperties=font,
                        clip_on=True,
                    )
                    drawn += 1
        axes.scatter([point[0]], [point[1]], s=90, facecolors="none", edgecolors="#ff3131")
        axes.plot(
            [point[0] - radius * 0.035, point[0] + radius * 0.035],
            [point[1], point[1]],
            color="#ff3131",
            linewidth=1.2,
        )
        axes.plot(
            [point[0], point[0]],
            [point[1] - radius * 0.035, point[1] + radius * 0.035],
            color="#ff3131",
            linewidth=1.2,
        )
        axes.text(
            0.012,
            0.985,
            f"{occurrence.mt_code}  |  {sheet.drawing_number or sheet.title or sheet.layout}",
            transform=axes.transAxes,
            ha="left",
            va="top",
            color="#ffffff",
            fontsize=10,
            fontproperties=font,
            bbox={"facecolor": "#000000", "alpha": 0.72, "edgecolor": "none"},
        )
        axes.set_xlim(region[0], region[2])
        axes.set_ylim(region[1], region[3])
        axes.set_aspect("equal", adjustable="box")
        axes.axis("off")
        stem = _safe_label(occurrence.id)
        candidate = stem
        suffix = 2
        while candidate.casefold() in used_names:
            candidate = f"{stem}_{suffix}"
            suffix += 1
        used_names.add(candidate.casefold())
        path = destination / f"{candidate}.png"
        canvas.print_png(path)
        figure.clear()
        records[occurrence.id] = {
            "file": path.name,
            "bbox": list(region),
            "sheet_id": sheet.id,
            "drawing_number": sheet.drawing_number,
            "layout": sheet.layout,
            "entity_count": len(selected),
            "primitive_count": drawn,
            "backend": "indexed-matplotlib-agg",
        }
    result = {
        "schema_version": "1.0",
        "requested_count": min(len(occurrences), maximum),
        "rendered_count": len(records),
        "skipped_entity_count": sum(skipped_counts.values()),
        "skipped_entity_type_counts": dict(sorted(skipped_counts.items())),
        "regions": records,
    }
    write_json_atomic(destination / "index.json", result)
    return result


def render_panel_occurrence_crops(
    sheets: Sequence[Any],
    occurrences: Sequence[Any],
    source_dxfs: Mapping[str, Path],
    output_dir: Path | str,
    *,
    maximum: int = 250,
    target_px: int = 2_200,
    crop_ratio: float = 0.38,
    render_profile: str = "cad-dark",
) -> dict[str, Any]:
    """Render each used viewport once, then crop all MT evidence from that image.

    This keeps raw linework fidelity while avoiding the old O(panels × MT)
    redraw pattern.  Crops are marked at the exact leader target and retain the
    source panel bbox needed to reproduce the pixel mapping.
    """

    if maximum < 1:
        raise ValueError("maximum must be at least 1")
    if not 0.2 <= crop_ratio <= 1.0:
        raise ValueError("crop_ratio must be between 0.2 and 1.0")
    profile = _render_profile(render_profile)
    from PIL import Image, ImageDraw, ImageFont

    destination = Path(output_dir).resolve()
    panel_root = destination / "panels"
    crop_root = destination / "crops"
    panel_root.mkdir(parents=True, exist_ok=True)
    crop_root.mkdir(parents=True, exist_ok=True)
    sheet_by_id = {sheet.id: sheet for sheet in sheets}
    selected_occurrences: list[Any] = []
    grouped_sheets: dict[str, dict[str, Any]] = defaultdict(dict)
    for occurrence in sorted(occurrences, key=lambda value: value.id):
        if len(selected_occurrences) >= maximum:
            break
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
        selected_occurrences.append(occurrence)
        grouped_sheets[occurrence.source_file_id][sheet.id] = sheet

    panel_records: dict[str, dict[str, Any]] = {}
    skipped_counts: Counter[str] = Counter()
    for source_id, source_sheets in sorted(grouped_sheets.items()):
        group_dir = panel_root / _safe_label(source_id)
        # Never reuse a PNG by filename alone.  Older implementations could
        # render ``cad-dark`` and then falsely label the same bytes as
        # ``cad-dark-full`` because profile, source digest, bbox and target_px
        # were absent from the cache key.  Rebuilding is slower but preserves
        # evidence integrity until a content-addressed cache is introduced.
        pending_regions = {
            sheet_id: sheet.bbox for sheet_id, sheet in sorted(source_sheets.items())
        }
        if pending_regions:
            result = render_regions(
                source_dxfs[source_id],
                pending_regions,
                group_dir,
                layout="Model",
                margin_ratio=0.0,
                target_px=target_px,
                mark_center=False,
                render_profile=profile["name"],
            )
            skipped_counts.update(result.get("skipped_entity_type_counts", {}))
            for sheet_id, record in result.get("regions", {}).items():
                panel_records[sheet_id] = {
                    **record,
                    "source_file_id": source_id,
                    "absolute_path": str(group_dir / record["file"]),
                }

    font_path = Path("C:/Windows/Fonts/msyh.ttc")
    try:
        header_font = ImageFont.truetype(str(font_path), 24) if font_path.is_file() else None
    except OSError:
        header_font = None
    occurrence_records: dict[str, dict[str, Any]] = {}
    for occurrence in selected_occurrences:
        sheet = sheet_by_id[occurrence.sheet_id]
        panel_record = panel_records.get(sheet.id)
        point = occurrence.leader_target or occurrence.anchor
        if panel_record is None or point is None or sheet.bbox is None:
            continue
        panel_path = Path(panel_record["absolute_path"])
        with Image.open(panel_path) as panel_image:
            panel = panel_image.convert("RGB")
        width, height = panel.size
        x0, y0, x1, y1 = sheet.bbox
        if x1 <= x0 or y1 <= y0:
            continue
        pixel_x = (point[0] - x0) / (x1 - x0) * width
        pixel_y = (y1 - point[1]) / (y1 - y0) * height
        crop_side = int(max(420, min(width, height) * crop_ratio))
        crop_side = min(crop_side, width, height)
        left = int(round(pixel_x - crop_side / 2))
        top = int(round(pixel_y - crop_side / 2))
        left = min(max(left, 0), max(width - crop_side, 0))
        top = min(max(top, 0), max(height - crop_side, 0))
        right = left + crop_side
        bottom = top + crop_side
        crop = panel.crop((left, top, right, bottom))
        marker_x = int(round(pixel_x - left))
        marker_y = int(round(pixel_y - top))
        header_height = 52
        canvas = Image.new("RGB", (crop.width, crop.height + header_height), "#111820")
        canvas.paste(crop, (0, header_height))
        draw = ImageDraw.Draw(canvas)
        marker_y += header_height
        marker_radius = max(10, crop_side // 45)
        draw.ellipse(
            (
                marker_x - marker_radius,
                marker_y - marker_radius,
                marker_x + marker_radius,
                marker_y + marker_radius,
            ),
            outline="#ff3131",
            width=4,
        )
        draw.line(
            (marker_x - marker_radius * 2, marker_y, marker_x + marker_radius * 2, marker_y),
            fill="#ff3131",
            width=3,
        )
        draw.line(
            (marker_x, marker_y - marker_radius * 2, marker_x, marker_y + marker_radius * 2),
            fill="#ff3131",
            width=3,
        )
        label = f"{occurrence.mt_code}  |  {sheet.drawing_number or sheet.title or sheet.layout}"
        draw.text((14, 11), label, fill="#ffffff", font=header_font)
        crop_path = crop_root / f"{_safe_label(occurrence.id)}.png"
        canvas.save(crop_path, format="PNG", optimize=True)
        occurrence_records[occurrence.id] = {
            "file": str(crop_path.relative_to(destination)).replace("\\", "/"),
            "absolute_path": str(crop_path),
            "sheet_id": sheet.id,
            "drawing_number": sheet.drawing_number,
            "panel_file": str(panel_path),
            "panel_bbox": list(sheet.bbox),
            "leader_target": list(point),
            "panel_pixel": [round(pixel_x, 3), round(pixel_y, 3)],
            "crop_box": [left, top, right, bottom],
            "backend": "raw-panel-then-crop",
            "render_profile": profile["name"],
            "crop_ratio": crop_ratio,
        }
    result = {
        "schema_version": "1.0",
        "requested_count": min(len(occurrences), maximum),
        "eligible_occurrence_count": len(selected_occurrences),
        "panel_count": len(panel_records),
        "rendered_count": len(occurrence_records),
        "skipped_entity_count": sum(skipped_counts.values()),
        "skipped_entity_type_counts": dict(sorted(skipped_counts.items())),
        "render_profile": profile["name"],
        "cache_policy": "rebuild_no_unkeyed_reuse",
        "crop_ratio": crop_ratio,
        "panels": panel_records,
        "occurrences": occurrence_records,
    }
    write_json_atomic(destination / "index.json", result)
    return result
