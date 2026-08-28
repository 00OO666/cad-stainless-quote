"""Re-render component frame candidates directly from vector CAD at high resolution.

Cropping a small object from a whole-panel PNG preserves only the few pixels that
were present in the overview.  This module instead uses the reviewed/suggested CAD
bbox as a fresh render region, so dimensions remain readable without interpolating
or upscaling a raster screenshot.  The image is still REVIEW evidence until the
physical component bbox and drawing chain are explicitly confirmed.
"""

from __future__ import annotations

import hashlib
import math
from collections import defaultdict
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from PIL import Image

from .io import sha256_file, write_json_atomic
from .render import _safe_label, render_regions


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


def _union(boxes: Sequence[Sequence[float]]) -> tuple[float, float, float, float]:
    return (
        min(float(value[0]) for value in boxes),
        min(float(value[1]) for value in boxes),
        max(float(value[2]) for value in boxes),
        max(float(value[3]) for value in boxes),
    )


def _clip(
    value: Sequence[float],
    panel: Sequence[float],
) -> tuple[float, float, float, float] | None:
    clipped = (
        max(float(value[0]), float(panel[0])),
        max(float(value[1]), float(panel[1])),
        min(float(value[2]), float(panel[2])),
        min(float(value[3]), float(panel[3])),
    )
    return clipped if clipped[2] > clipped[0] and clipped[3] > clipped[1] else None


def _selection_key(record: Mapping[str, Any], index: int) -> str:
    for field in ("selection_key", "component_id", "gold_row_id", "row_id", "sequence"):
        if record.get(field) not in (None, ""):
            return str(record[field])
    return f"selection:{index}"


def _request_label(
    selection_key: str,
    sequence: Any,
    group_id: str,
    index: int,
) -> str:
    digest = hashlib.sha256(
        f"{selection_key}\0{group_id}\0{index}".encode()
    ).hexdigest()[:12]
    return f"{sequence}-{_safe_label(selection_key)}-{digest}"


def render_component_frame_closeups(
    index_payload: Mapping[str, Any],
    panel_payload: Mapping[str, Any],
    frame_payload: Mapping[str, Any],
    output_dir: Path | str,
    *,
    target_px: int = 3_200,
    render_profile: str = "cad-dark-full",
    margin_ratio: float = 0.04,
    maximum: int = 500,
) -> dict[str, Any]:
    """Render one crisp CAD region for every component-frame suggestion.

    ``index_payload`` supplies source DXF paths, ``panel_payload`` supplies the
    virtual-sheet/source binding, and ``frame_payload`` is the output of
    :func:`suggest_component_frames`.
    """

    if not 512 <= target_px <= 8_000:
        raise ValueError("target_px must be between 512 and 8000")
    if not 0 <= margin_ratio <= 0.5:
        raise ValueError("margin_ratio must be between 0 and 0.5")
    if maximum < 1:
        raise ValueError("maximum must be at least 1")

    destination = Path(output_dir).resolve()
    destination.mkdir(parents=True, exist_ok=True)
    source_paths = {
        str(value["source_file_id"]): Path(str(value["source_path"])).resolve()
        for value in index_payload.get("sources", [])
        if isinstance(value, Mapping)
        and value.get("source_file_id")
        and value.get("source_path")
    }
    sheets = {
        str(value["id"]): value
        for value in panel_payload.get("sheets", [])
        if isinstance(value, Mapping) and value.get("id")
    }

    records: list[dict[str, Any]] = []
    requests_by_source: dict[str, dict[str, tuple[float, float, float, float]]] = defaultdict(dict)
    request_meta: dict[str, dict[str, Any]] = {}
    missing_count = 0
    truncated_count = 0
    requested_count = 0
    frame_records = frame_payload.get("records", [])
    if not isinstance(frame_records, Sequence) or isinstance(
        frame_records, (str, bytes, bytearray)
    ):
        raise ValueError("frame_payload.records must be an array")

    for record_index, raw_record in enumerate(frame_records, start=1):
        if not isinstance(raw_record, Mapping):
            continue
        selection_key = _selection_key(raw_record, record_index)
        output_record = {
            "selection_key": selection_key,
            "sequence": raw_record.get("sequence", record_index),
            "state": "MISSING",
            "reason_codes": [],
            "evidence": [],
        }
        raw_frames = raw_record.get("frames", [])
        if not isinstance(raw_frames, Sequence) or isinstance(
            raw_frames, (str, bytes, bytearray)
        ):
            raw_frames = []
        for frame_index, raw_frame in enumerate(raw_frames, start=1):
            if requested_count >= maximum:
                truncated_count += 1
                continue
            if not isinstance(raw_frame, Mapping):
                continue
            requested_count += 1
            sheet_id = str(raw_frame.get("sheet_id") or "")
            group_id = str(raw_frame.get("group_id") or "")
            sheet = sheets.get(sheet_id)
            object_bbox = _bbox(raw_frame.get("object_bbox"))
            panel_bbox = _bbox(sheet.get("bbox")) if sheet is not None else None
            if sheet is None or object_bbox is None or panel_bbox is None:
                output_record["reason_codes"].append("FRAME_OR_PANEL_BBOX_MISSING")
                missing_count += 1
                continue
            source_file_id = str(sheet.get("source_file_id") or "")
            if source_file_id not in source_paths or not source_paths[source_file_id].is_file():
                output_record["reason_codes"].append("SOURCE_DXF_MISSING")
                missing_count += 1
                continue
            dimension_bboxes = [
                value
                for raw_value in raw_frame.get("dimension_bboxes", [])
                if (value := _bbox(raw_value)) is not None
            ]
            combined = _union([object_bbox, *dimension_bboxes])
            region = _clip(combined, panel_bbox)
            if region is None:
                output_record["reason_codes"].append("FRAME_OUTSIDE_PANEL")
                missing_count += 1
                continue
            label = _request_label(
                selection_key,
                output_record["sequence"],
                group_id,
                frame_index,
            )
            requests_by_source[source_file_id][label] = region
            request_meta[label] = {
                "record": output_record,
                "selection_key": selection_key,
                "sequence": output_record["sequence"],
                "group_id": group_id,
                "sheet_id": sheet_id,
                "source_file_id": source_file_id,
                "drawing_number": sheet.get("drawing_number"),
                "kind": sheet.get("kind"),
                "object_bbox": list(object_bbox),
                "dimension_bboxes": [list(value) for value in dimension_bboxes],
                "render_bbox": list(region),
                "frame_state": raw_frame.get("state", "REVIEW"),
                "frame_reason_codes": list(raw_frame.get("reason_codes", [])),
            }
        records.append(output_record)

    failures: list[dict[str, Any]] = []
    rendered_count = 0
    for source_file_id, regions in sorted(requests_by_source.items()):
        # Local run directories and selection labels can already be long.  A
        # content-derived short directory prevents the Windows MAX_PATH limit
        # from turning an otherwise valid vector render into FileNotFoundError.
        source_token = hashlib.sha256(source_file_id.encode()).hexdigest()[:16]
        source_dir = destination / "images" / f"source_{source_token}"
        try:
            rendered = render_regions(
                source_paths[source_file_id],
                regions,
                source_dir,
                layout="Model",
                margin_ratio=margin_ratio,
                target_px=target_px,
                mark_center=False,
                render_profile=render_profile,
            )
        except Exception as exc:  # pragma: no cover - external CAD boundary
            failures.append(
                {
                    "source_file_id": source_file_id,
                    "error_type": type(exc).__name__,
                    "message": str(exc),
                    "request_labels": sorted(regions),
                }
            )
            for label in regions:
                request_meta[label]["record"]["reason_codes"].append("VECTOR_RENDER_FAILED")
            continue
        rendered_regions = rendered.get("regions", {})
        for label in regions:
            meta = request_meta[label]
            record = meta.pop("record")
            image_record = rendered_regions.get(label)
            if not isinstance(image_record, Mapping) or not image_record.get("file"):
                record["reason_codes"].append("VECTOR_RENDER_EMPTY")
                missing_count += 1
                continue
            image_path = source_dir / str(image_record["file"])
            try:
                with Image.open(image_path) as image:
                    pixel_size = [int(image.width), int(image.height)]
                    image.verify()
            except (OSError, ValueError):
                record["reason_codes"].append("VECTOR_RENDER_UNREADABLE")
                missing_count += 1
                continue
            record["evidence"].append(
                {
                    **meta,
                    "state": "REVIEW",
                    "absolute_path": str(image_path.resolve()),
                    "relative_path": str(image_path.relative_to(destination)).replace("\\", "/"),
                    "image_sha256": sha256_file(image_path),
                    "pixel_size": pixel_size,
                    "target_px": target_px,
                    "render_profile": render_profile,
                    "margin_ratio": margin_ratio,
                    "backend": image_record.get("backend"),
                    "entity_count": image_record.get("entity_count"),
                }
            )
            rendered_count += 1

    for record in records:
        if record["evidence"]:
            record["state"] = "REVIEW"
            if "FRAME_REQUIRES_CONFIRMATION" not in record["reason_codes"]:
                record["reason_codes"].append("FRAME_REQUIRES_CONFIRMATION")

    result = {
        "schema_version": "1.0",
        "purpose": "high_resolution_vector_component_closeups_review_only",
        "path_scope": "local_run_diagnostics",
        "warning": (
            "Images are fresh vector renders, not raster upscales. They improve legibility but "
            "remain REVIEW until component bbox, measurement roles, and the full drawing chain "
            "are explicitly confirmed. Absolute paths must not be published."
        ),
        "target_px": target_px,
        "render_profile": render_profile,
        "margin_ratio": margin_ratio,
        "selection_count": len(records),
        "requested_count": requested_count,
        "rendered_count": rendered_count,
        "missing_count": missing_count,
        "truncated_count": truncated_count,
        "failure_count": len(failures),
        "failures": failures,
        "records": records,
    }
    write_json_atomic(destination / "component_closeups.json", result)
    return result


__all__ = ["render_component_frame_closeups"]
