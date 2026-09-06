"""Fresh viewport crops with original paper DIMENSION, text and leader graphics.

This is a local REVIEW renderer, not an ownership or measurement decision. It
uses native top-view transforms and refuses unsupported/clipped viewports.
"""

from __future__ import annotations

import math
from collections import Counter
from pathlib import Path

from PIL import Image

from .io import sha256_file, write_json_atomic
from .render import _read_document, _validate_region, render_regions


def _finite_region(value):
    box = _validate_region(value)
    if not all(math.isfinite(v) for v in box):
        raise ValueError("NONFINITE_REGION")
    return box


def native_viewport_region(viewport, region, margin_ratio=0.0):
    """Map a model region using native WCS-to-paper matrix, never a guessed scale."""
    if viewport is None or viewport.dxftype() != "VIEWPORT":
        raise ValueError("NATIVE_VIEWPORT_MISSING")
    dxf = viewport.dxf
    if not viewport.is_visible or int(dxf.id) <= 1:
        raise ValueError("NATIVE_VIEWPORT_INACTIVE")
    if int(dxf.flags) & (1 | 2 | 4 | 65536) or viewport.has_extended_clipping_path:
        raise ValueError("VIEWPORT_CLIPPING_OR_PERSPECTIVE_UNSUPPORTED")
    direction = tuple(dxf.view_direction_vector)
    if not all(math.isfinite(v) for v in direction) or not viewport.is_top_view:
        raise ValueError("VIEWPORT_DIRECTION_UNSUPPORTED")
    if sum(v * v for v in direction) < 1e-12:
        raise ValueError("VIEWPORT_DIRECTION_UNSUPPORTED")
    twist = float(dxf.view_twist_angle)
    if not math.isfinite(twist) or abs(math.remainder(twist, 360.0)) > 1e-7:
        raise ValueError("VIEWPORT_ROTATION_UNSUPPORTED")
    if not math.isfinite(viewport.get_scale()) or viewport.get_scale() <= 0:
        raise ValueError("VIEWPORT_SCALE_INVALID")
    if not math.isfinite(margin_ratio) or not 0 <= margin_ratio <= 0.5:
        raise ValueError("MARGIN_INVALID")
    native_box = _finite_region(viewport.get_modelspace_limits())
    x0, y0, x1, y1 = _finite_region(region)
    if (
        x0 < native_box[0] - 1e-6
        or y0 < native_box[1] - 1e-6
        or x1 > native_box[2] + 1e-6
        or y1 > native_box[3] + 1e-6
    ):
        raise ValueError("REGION_OUTSIDE_SOURCE_VIEWPORT")
    mx, my = (x1 - x0) * margin_ratio, (y1 - y0) * margin_ratio
    expanded = (x0 - mx, y0 - my, x1 + mx, y1 + my)
    clipped = (
        max(expanded[0], native_box[0]),
        max(expanded[1], native_box[1]),
        min(expanded[2], native_box[2]),
        min(expanded[3], native_box[3]),
    )
    matrix = viewport.get_transformation_matrix()
    lower = matrix.transform((clipped[0], clipped[1], 0))
    upper = matrix.transform((clipped[2], clipped[3], 0))
    paper_box = _finite_region((lower.x, lower.y, upper.x, upper.y))
    return {
        "model_bbox": list(clipped),
        "paper_bbox": list(paper_box),
        "native_model_bbox": list(native_box),
        "margin_clipped": expanded != clipped,
        "model_to_paper_scale": viewport.get_scale(),
        "model_to_paper_matrix": list(matrix),
        "viewport_handle": dxf.handle,
        "paper_layout": viewport.get_layout().name,
    }


def render_native_paper_regions(
    dxf_path,
    regions,
    output_dir,
    *,
    viewport_handles,
    target_px=3200,
    margin_ratio=0.04,
    max_paper_entities=100_000,
):
    """Return per-region images/failures in the render_regions exchange shape.

    Each output includes the original DIMENSION symbol rendering, not a numeric
    value redrawn at an inferred point. Frame selection remains the caller's job.
    """
    from ezdxf import bbox
    from ezdxf.addons.drawing import Frontend, RenderContext
    from ezdxf.addons.drawing.config import BackgroundPolicy, ColorPolicy, Configuration
    from ezdxf.addons.drawing.matplotlib import MatplotlibBackend
    from matplotlib.backends.backend_agg import FigureCanvasAgg
    from matplotlib.figure import Figure

    if not isinstance(target_px, int) or not 512 <= target_px <= 8000:
        raise ValueError("target_px must be between 512 and 8000")
    if not math.isfinite(margin_ratio) or not 0 <= margin_ratio <= 0.5:
        raise ValueError("margin_ratio must be between 0 and 0.5")
    if not isinstance(max_paper_entities, int) or max_paper_entities < 1:
        raise ValueError("max_paper_entities must be positive")
    source = Path(dxf_path).resolve()
    destination = Path(output_dir).resolve()
    destination.mkdir(parents=True, exist_ok=True)
    digest = sha256_file(source)
    doc = _read_document(source)
    result = {
        "schema_version": "native-paper-regions/1.0",
        "state": "REVIEW",
        "source_sha256": digest,
        "path_scope": "local_run_diagnostics",
        "requested_count": len(regions),
        "regions": {},
        "failures": [],
    }
    accepted, transforms, prepared = {}, {}, {}
    for label, region in regions.items():
        try:
            tr = native_viewport_region(
                doc.entitydb.get(viewport_handles.get(label)), region, margin_ratio
            )
            layout = doc.layout(tr["paper_layout"])
            if len(layout) > max_paper_entities:
                raise ValueError("PAPER_ENTITY_CAP")
        except (ValueError, TypeError, KeyError, AttributeError) as exc:
            result["failures"].append({"label": label, "reason": str(exc)})
            continue
        accepted[label], transforms[label] = tr["model_bbox"], tr
        if layout.name not in prepared:
            context = RenderContext(doc)
            context.set_current_layout(layout)
            native, issues = [], []
            for e in layout:
                if e.dxftype() == "VIEWPORT" or not context.resolve_all(e).is_visible:
                    continue
                try:
                    box = bbox.extents([e], fast=True)
                except Exception as exc:  # native bounding boundary
                    issues.append({"handle": e.dxf.handle, "reason": type(exc).__name__})
                    continue
                if box.has_data:
                    native.append((e, (box.extmin.x, box.extmin.y, box.extmax.x, box.extmax.y)))
                else:
                    issues.append({"handle": e.dxf.handle, "reason": "PAPER_ENTITY_WITHOUT_BOUNDS"})
            prepared[layout.name] = context, native, issues
    if accepted:
        model = render_regions(
            source,
            accepted,
            destination / "model",
            layout="Model",
            viewport_handles={k: viewport_handles[k] for k in accepted},
            target_px=target_px,
            margin_ratio=0.0,
            mark_center=False,
            render_profile="cad-dark-full",
        )
        cfg = Configuration(
            background_policy=BackgroundPolicy.CUSTOM,
            custom_bg_color="#111820",
            color_policy=ColorPolicy.COLOR_SWAP_BW,
        )

        class PaperFrontend(Frontend):
            def draw_entities(self, entities, *, filter_func=None):
                def safe(entity):
                    return entity.dxftype() not in {"VIEWPORT", "WIPEOUT"} and (
                        filter_func(entity) if filter_func else True
                    )

                super().draw_entities(entities, filter_func=safe)

        for label, tr in transforms.items():
            base = model["regions"].get(label)
            if base is None:
                result["failures"].append({"label": label, "reason": "MODEL_RENDER_EMPTY"})
                continue
            context, native, issues = prepared[tr["paper_layout"]]
            pb = tr["paper_bbox"]
            keep = [
                e
                for e, b in native
                if b[0] <= pb[2] and b[2] >= pb[0] and b[1] <= pb[3] and b[3] >= pb[1]
            ]
            base_path = destination / "model" / base["file"]
            with Image.open(base_path) as im:
                pixels = list(im.size)
                fig = Figure(
                    figsize=(pixels[0] / 100, pixels[1] / 100), dpi=100, facecolor="#111820"
                )
                canvas = FigureCanvasAgg(fig)
                ax = fig.add_axes((0, 0, 1, 1))
                ax.imshow(
                    im.convert("RGB"),
                    extent=(pb[0], pb[2], pb[1], pb[3]),
                    zorder=-10,
                    interpolation="nearest",
                    aspect="equal",
                )
            try:
                backend = MatplotlibBackend(ax, adjust_figure=False)
                PaperFrontend(context, backend, config=cfg).draw_entities(keep)
                backend.finalize()
                ax.set_xlim(pb[0], pb[2])
                ax.set_ylim(pb[1], pb[3])
                ax.set_axis_off()
                path = destination / base["file"]
                canvas.print_png(path)
            finally:
                fig.clear()
            result["regions"][label] = {
                **base,
                "file": path.name,
                "bbox": tr["model_bbox"],
                "pixel_size": pixels,
                "image_sha256": sha256_file(path),
                "source_sha256": digest,
                "native_paper_transform": tr,
                "annotation_fidelity": "NATIVE_SUPPORTED_PAPER_ENTITIES",
                "paper_entities": [
                    {"handle": e.dxf.handle, "entity_type": e.dxftype()} for e in keep
                ],
                "paper_entity_types": dict(Counter(e.dxftype() for e in keep)),
                "paper_scan_issues": issues,
                "limitations": [
                    "Viewport framing does not prove component or dimension ownership.",
                    "WIPEOUT is excluded; proxies, Xrefs, SHX and plot styles may differ.",
                    "Other paper annotations are preserved, not material assignments.",
                ],
                "backend": "native-model-plus-paper-frontend",
                "state": "REVIEW",
            }
    if sha256_file(source) != digest:
        raise ValueError("SOURCE_CHANGED_DURING_RENDER")
    result["rendered_count"] = len(result["regions"])
    result["failure_count"] = len(result["failures"])
    write_json_atomic(destination / "native_paper_render.json", result)
    return result
