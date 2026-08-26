"""Render bounded DXF evidence images while retaining coordinate provenance."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import ezdxf
from ezdxf import bbox as ezbbox
from ezdxf import recover

from .io import write_json_atomic

Region = tuple[float, float, float, float]


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
) -> dict[str, dict[str, Any]]:
    import matplotlib

    matplotlib.use("Agg", force=True)
    from ezdxf.addons.drawing import Frontend, RenderContext
    from ezdxf.addons.drawing.config import BackgroundPolicy, Configuration
    from ezdxf.addons.drawing.matplotlib import MatplotlibBackend
    from matplotlib.backends.backend_agg import FigureCanvasAgg
    from matplotlib.figure import Figure

    space = document.modelspace() if layout_name == "Model" else document.layout(layout_name)
    context = RenderContext(document)
    context.set_current_layout(space)
    context.current_layout_properties.set_colors("#FFFFFF")
    configuration = Configuration(background_policy=BackgroundPolicy.WHITE)
    cache = ezbbox.Cache()
    bounded: list[tuple[Any, Region]] = []
    for entity in space:
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
            facecolor="#FFFFFF",
        )
        canvas = FigureCanvasAgg(figure)
        axes = figure.add_axes((0, 0, 1, 1))
        backend = MatplotlibBackend(axes, adjust_figure=False)
        backend.set_background("#FFFFFF")
        Frontend(context, backend, config=configuration).draw_entities(entities)
        backend.finalize()
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
        }
    return rendered


def render_regions(
    dxf_path: Path | str,
    regions: Mapping[str, Sequence[float]],
    output_dir: Path | str,
    *,
    layout: str = "Model",
    margin_ratio: float = 0.04,
    target_px: int = 2_200,
) -> dict[str, Any]:
    """Render named regions and save an index that maps every image back to CAD coordinates."""

    if not 0 <= margin_ratio <= 1:
        raise ValueError("margin_ratio must be between 0 and 1")
    if target_px < 256:
        raise ValueError("target_px must be at least 256")
    source = Path(dxf_path).resolve()
    destination = Path(output_dir).resolve()
    destination.mkdir(parents=True, exist_ok=True)
    normalized = {str(label): _validate_region(value) for label, value in regions.items()}
    if not normalized:
        raise ValueError("At least one render region is required")
    document = _read_document(source)
    if layout != "Model" and layout not in document.layout_names():
        raise ValueError(f"DXF layout not found: {layout}")
    rendered = _render_with_matplotlib(
        document,
        layout,
        normalized,
        destination,
        margin_ratio,
        target_px,
    )
    result = {
        "source": str(source),
        "layout": layout,
        "requested_count": len(normalized),
        "rendered_count": len(rendered),
        "regions": rendered,
    }
    write_json_atomic(destination / "index.json", result)
    return result


def load_regions_json(path: Path | str) -> dict[str, Region]:
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("Regions JSON must be an object mapping labels to bboxes")
    return {str(label): _validate_region(value) for label, value in payload.items()}
