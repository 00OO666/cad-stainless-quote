"""Feature-based registration for human CAD screenshots and rendered panels."""

from __future__ import annotations

import math
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from .io import write_json_atomic


def projected_query_corners_to_cad_bbox(
    projected_query_corners: Sequence[Sequence[float]],
    panel_cad_bbox: Sequence[float],
    panel_pixel_width: int,
    panel_pixel_height: int,
    *,
    clip_to_panel: bool = True,
    low_coverage_threshold: float = 0.5,
) -> dict[str, Any]:
    """Map registered panel-image pixels to an axis-aligned CAD bbox.

    Pixel coordinates use the normal image convention (origin at top-left,
    positive Y downward), while the CAD bbox uses positive Y upward. The
    result is deliberately REVIEW-only: screenshot registration can create a
    useful development label, but it cannot confirm a physical component.
    """

    reason_codes = ["SCREENSHOT_BBOX_REGISTRATION_REVIEW_ONLY"]
    result: dict[str, Any] = {
        "projected_query_cad_bbox": None,
        "state": "REVIEW",
        "valid": False,
        "reason_codes": reason_codes,
        "projected_query_pixel_bbox": None,
        "clipped_query_pixel_bbox": None,
        "in_panel_coverage_ratio": 0.0,
        "clipped_to_panel": False,
        "panel_natural_pixel_size": [panel_pixel_width, panel_pixel_height],
    }

    try:
        pixel_width = float(panel_pixel_width)
        pixel_height = float(panel_pixel_height)
    except (TypeError, ValueError):
        reason_codes.append("INVALID_PANEL_PIXEL_SIZE")
        return result
    if (
        not math.isfinite(pixel_width)
        or not math.isfinite(pixel_height)
        or pixel_width <= 0
        or pixel_height <= 0
    ):
        reason_codes.append("INVALID_PANEL_PIXEL_SIZE")
        return result

    try:
        cad_bbox = [float(value) for value in panel_cad_bbox]
    except (TypeError, ValueError):
        reason_codes.append("INVALID_PANEL_CAD_BBOX")
        return result
    if len(cad_bbox) != 4:
        reason_codes.append("INVALID_PANEL_CAD_BBOX")
        return result
    if not all(math.isfinite(value) for value in cad_bbox):
        reason_codes.append("NON_FINITE_PANEL_CAD_BBOX")
        return result
    cad_left, cad_bottom, cad_right, cad_top = cad_bbox
    if cad_right <= cad_left or cad_top <= cad_bottom:
        reason_codes.append("DEGENERATE_PANEL_CAD_BBOX")
        return result

    try:
        corners = [
            [float(point[0]), float(point[1])]
            for point in projected_query_corners
            if len(point) == 2
        ]
    except (TypeError, ValueError, IndexError):
        reason_codes.append("INVALID_PROJECTED_QUERY_CORNERS")
        return result
    if len(corners) != 4:
        reason_codes.append("INVALID_PROJECTED_QUERY_CORNERS")
        return result
    if not all(math.isfinite(value) for point in corners for value in point):
        reason_codes.append("NON_FINITE_PROJECTED_QUERY_CORNERS")
        return result

    xs = [point[0] for point in corners]
    ys = [point[1] for point in corners]
    pixel_left, pixel_top = min(xs), min(ys)
    pixel_right, pixel_bottom = max(xs), max(ys)
    projected_bbox = [pixel_left, pixel_top, pixel_right, pixel_bottom]
    result["projected_query_pixel_bbox"] = projected_bbox
    projected_width = pixel_right - pixel_left
    projected_height = pixel_bottom - pixel_top
    if projected_width <= 0 or projected_height <= 0 or _polygon_area(corners) <= 0:
        reason_codes.append("DEGENERATE_PROJECTED_QUERY_BBOX")
        return result

    clipped_left = max(0.0, pixel_left)
    clipped_top = max(0.0, pixel_top)
    clipped_right = min(pixel_width, pixel_right)
    clipped_bottom = min(pixel_height, pixel_bottom)
    if clipped_right <= clipped_left or clipped_bottom <= clipped_top:
        reason_codes.append("PROJECTED_QUERY_OUTSIDE_PANEL")
        return result

    clipped_bbox = [clipped_left, clipped_top, clipped_right, clipped_bottom]
    clipped = clipped_bbox != projected_bbox
    clipped_area = (clipped_right - clipped_left) * (clipped_bottom - clipped_top)
    coverage_ratio = clipped_area / (projected_width * projected_height)
    result.update(
        {
            "clipped_query_pixel_bbox": clipped_bbox,
            "in_panel_coverage_ratio": round(coverage_ratio, 6),
            "clipped_to_panel": clipped,
        }
    )
    if clipped:
        reason_codes.append("PROJECTED_QUERY_CLIPPED_TO_PANEL")
        if not clip_to_panel:
            reason_codes.append("PANEL_CLIPPING_DISABLED")
            return result
    if not 0.0 <= low_coverage_threshold <= 1.0:
        reason_codes.append("INVALID_LOW_COVERAGE_THRESHOLD")
        return result
    if coverage_ratio < low_coverage_threshold:
        reason_codes.append("LOW_IN_PANEL_COVERAGE")

    cad_width = cad_right - cad_left
    cad_height = cad_top - cad_bottom
    mapped_left = cad_left + (clipped_left / pixel_width) * cad_width
    mapped_right = cad_left + (clipped_right / pixel_width) * cad_width
    # Image Y grows downward, so pixel top maps to CAD top and vice versa.
    mapped_top = cad_top - (clipped_top / pixel_height) * cad_height
    mapped_bottom = cad_top - (clipped_bottom / pixel_height) * cad_height
    result.update(
        {
            "projected_query_cad_bbox": [
                mapped_left,
                mapped_bottom,
                mapped_right,
                mapped_top,
            ],
            "valid": True,
        }
    )
    return result


def _polygon_area(points: Any) -> float:
    """Return the absolute shoelace area for an ordered four-corner polygon."""

    import numpy as np

    values = np.asarray(points, dtype=float).reshape(-1, 2)
    if len(values) < 3 or not np.isfinite(values).all():
        return 0.0
    x_values = values[:, 0]
    y_values = values[:, 1]
    return float(
        abs(
            np.dot(x_values, np.roll(y_values, -1))
            - np.dot(y_values, np.roll(x_values, -1))
        )
        / 2.0
    )


def _registration_rejection_reason(
    *,
    scale: float,
    projected: Any,
    panel_width: int,
    panel_height: int,
) -> str | None:
    """Reject collapsed, explosive, or wholly off-panel similarity transforms."""

    import numpy as np

    values = np.asarray(projected, dtype=float).reshape(-1, 2)
    if not np.isfinite(scale) or not np.isfinite(values).all():
        return "NON_FINITE_TRANSFORM"
    if not 0.02 <= scale <= 50.0:
        return "IMPLAUSIBLE_SCALE"
    area = _polygon_area(values)
    if area < 16.0:
        return "COLLAPSED_PROJECTION"
    left = float(values[:, 0].min())
    top = float(values[:, 1].min())
    right = float(values[:, 0].max())
    bottom = float(values[:, 1].max())
    bounding_area = max(0.0, right - left) * max(0.0, bottom - top)
    intersection_width = max(0.0, min(right, panel_width) - max(left, 0.0))
    intersection_height = max(0.0, min(bottom, panel_height) - max(top, 0.0))
    intersection_area = intersection_width * intersection_height
    if bounding_area <= 0 or intersection_area / bounding_area < 0.01:
        return "PROJECTION_OUTSIDE_PANEL"
    return None


def _cv2() -> Any:
    try:
        import cv2
    except ImportError as exc:  # pragma: no cover - optional runtime boundary
        raise RuntimeError(
            "Screenshot matching requires the optional 'vision' dependency: "
            "python -m pip install 'opencv-python-headless>=4.10,<5'"
        ) from exc
    return cv2


def decompose_dark_cad_screenshot(
    image: Any,
    *,
    foreground_delta: int = 10,
) -> dict[str, Any]:
    """Conservatively split a dark-background CAD screenshot into view tiles.

    Human workbooks sometimes store several CAD view screenshots in one raster.
    Feature registration must treat those views independently; otherwise unrelated
    keypoints vote for one transform.  This helper finds content bands separated by
    background-colour gutters and accepts a composite only when the resulting large
    regions form a regular, non-overlapping, frame-like layout.

    The helper deliberately preserves a normal single-view screenshot as one tile
    spanning the original image.  Returned boxes use half-open pixel coordinates
    ``[left, top, right, bottom]`` and are diagnostic candidates, not confirmed CAD
    evidence.
    """

    import numpy as np

    values = np.asarray(image)
    if values.ndim == 2:
        values = np.repeat(values[:, :, None], 3, axis=2)
    if values.ndim != 3 or values.shape[2] < 3 or values.size == 0:
        raise ValueError("image must be a non-empty grayscale or BGR/RGB raster")
    values = values[:, :, :3].astype(np.int16, copy=False)
    height, width = values.shape[:2]

    full_tile = {
        "tile_index": 1,
        "bbox": [0, 0, int(width), int(height)],
        "bbox_source": "full_image",
        "content_density": None,
        "frame_score": None,
    }
    result: dict[str, Any] = {
        "schema_version": "1.0",
        "method": "dark-background gutter projections with conservative frame/layout validation",
        "image_pixel_size": [int(width), int(height)],
        "is_composite": False,
        "background_bgr": None,
        "background_luma": None,
        "border_uniform_fraction": 0.0,
        "foreground_fraction": 0.0,
        "layout_axis": None,
        "reason_codes": [],
        "tiles": [full_tile],
        "candidate_tiles": [],
    }
    if width < 24 or height < 24:
        result["reason_codes"].append("IMAGE_TOO_SMALL_FOR_DECOMPOSITION")
        return result

    border = np.concatenate(
        [values[0, :, :], values[-1, :, :], values[:, 0, :], values[:, -1, :]],
        axis=0,
    )
    # A median is stable when a screen capture contains anti-aliased lines on a
    # mostly uniform CAD canvas.  Snap it to the most common exact border colour
    # when that colour is well represented so PNG screenshots remain deterministic.
    background = np.median(border, axis=0)
    colours, counts = np.unique(border, axis=0, return_counts=True)
    mode_index = int(np.argmax(counts))
    if int(counts[mode_index]) / len(border) >= 0.20:
        background = colours[mode_index].astype(float)
    background = np.asarray(background, dtype=np.int16)
    border_delta = np.max(np.abs(border - background), axis=1)
    border_uniform_fraction = float(np.mean(border_delta <= foreground_delta))
    # OpenCV images are BGR; the same weights still provide a useful darkness
    # gate even if a caller supplies RGB because all three channels are dark.
    background_luma = float(
        0.114 * background[0] + 0.587 * background[1] + 0.299 * background[2]
    )
    result.update(
        {
            "background_bgr": [int(value) for value in background],
            "background_luma": round(background_luma, 3),
            "border_uniform_fraction": round(border_uniform_fraction, 6),
        }
    )
    if background_luma > 100 or border_uniform_fraction < 0.30:
        result["reason_codes"].append("BACKGROUND_NOT_DARK_AND_UNIFORM")
        return result

    delta = np.max(np.abs(values - background), axis=2)
    foreground = (delta >= foreground_delta).astype(np.uint8)
    result["foreground_fraction"] = round(float(np.mean(foreground)), 6)
    if int(foreground.sum()) < 64:
        result["reason_codes"].append("INSUFFICIENT_FOREGROUND_CONTENT")
        return result

    def close_short_gaps(active: Any, maximum_gap: int) -> Any:
        closed = np.asarray(active, dtype=bool).reshape(-1).copy()
        if maximum_gap <= 0 or len(closed) < 3:
            return closed
        gap_start: int | None = None
        for position, enabled in enumerate(closed):
            if not enabled and gap_start is None:
                gap_start = position
            elif enabled and gap_start is not None:
                if gap_start > 0 and position - gap_start <= maximum_gap:
                    closed[gap_start:position] = True
                gap_start = None
        return closed

    def runs(active: Any) -> list[tuple[int, int]]:
        found: list[tuple[int, int]] = []
        start: int | None = None
        for position, enabled in enumerate(np.r_[active, False]):
            if enabled and start is None:
                start = position
            elif not enabled and start is not None:
                found.append((start, position))
                start = None
        return found

    minimum_width = max(18, round(width * 0.045))
    minimum_height = max(18, round(height * 0.10))
    minimum_area = max(324, round(width * height * 0.006))

    def frame_score(box: tuple[int, int, int, int]) -> float:
        left, top, right, bottom = box
        crop = foreground[top:bottom, left:right]
        crop_height, crop_width = crop.shape
        band = max(1, round(min(crop_width, crop_height) * 0.02))
        top_score = float(np.mean(crop[:band, :]))
        bottom_score = float(np.mean(crop[-band:, :]))
        left_score = float(np.mean(crop[:, :band]))
        right_score = float(np.mean(crop[:, -band:]))
        return (max(top_score, bottom_score) + max(left_score, right_score)) / 2.0

    def title_strip_metrics(box: tuple[int, int, int, int]) -> dict[str, float]:
        left, top, right, bottom = box
        crop = values[top:bottom, left:right]
        strip_start = max(0, round(crop.shape[1] * 0.80))
        strip = crop[:, strip_start:]
        blue, green, red = [strip[:, :, index].astype(float) for index in range(3)]
        yellow = (
            (red > 100)
            & (green > 100)
            & (blue < np.minimum(red, green) * 0.75)
        )
        white = (red > 130) & (green > 130) & (blue > 130)
        yellow_fraction = float(np.mean(yellow))
        white_fraction = float(np.mean(white))
        return {
            "right_title_strip_score": round(
                yellow_fraction + 0.50 * white_fraction, 6
            ),
            "right_title_strip_yellow_fraction": round(yellow_fraction, 6),
            "right_title_strip_white_fraction": round(white_fraction, 6),
        }

    def axis_candidates(axis: str) -> list[dict[str, Any]]:
        if axis == "x":
            outer_counts = foreground.sum(axis=0)
            outer_length, inner_length = width, height
        else:
            outer_counts = foreground.sum(axis=1)
            outer_length, inner_length = height, width
        outer_active = outer_counts >= max(2, math.ceil(inner_length * 0.005))
        outer_active = close_short_gaps(outer_active, max(1, round(outer_length * 0.002)))
        outer_minimum = minimum_width if axis == "x" else minimum_height
        inner_minimum = minimum_height if axis == "x" else minimum_width
        candidates: list[dict[str, Any]] = []
        for outer_start, outer_end in runs(outer_active):
            if outer_end - outer_start < outer_minimum:
                continue
            if axis == "x":
                inner_counts = foreground[:, outer_start:outer_end].sum(axis=1)
            else:
                inner_counts = foreground[outer_start:outer_end, :].sum(axis=0)
            inner_active = inner_counts >= max(
                2, math.ceil((outer_end - outer_start) * 0.005)
            )
            inner_active = close_short_gaps(
                inner_active,
                max(1, round(len(inner_active) * 0.002)),
            )
            for inner_start, inner_end in runs(inner_active):
                if inner_end - inner_start < inner_minimum:
                    continue
                box = (
                    (outer_start, inner_start, outer_end, inner_end)
                    if axis == "x"
                    else (inner_start, outer_start, inner_end, outer_end)
                )
                left, top, right, bottom = box
                area = (right - left) * (bottom - top)
                if area < minimum_area:
                    continue
                density = float(np.mean(foreground[top:bottom, left:right]))
                if density < 0.006:
                    continue
                candidates.append(
                    {
                        "bbox": [int(value) for value in box],
                        "bbox_source": f"{axis}_gutter_projection",
                        "content_density": round(density, 6),
                        "frame_score": round(frame_score(box), 6),
                        **title_strip_metrics(box),
                    }
                )
        return candidates

    def intersection_over_union(left: dict[str, Any], right: dict[str, Any]) -> float:
        left_box = left["bbox"]
        right_box = right["bbox"]
        intersection_width = max(
            0, min(left_box[2], right_box[2]) - max(left_box[0], right_box[0])
        )
        intersection_height = max(
            0, min(left_box[3], right_box[3]) - max(left_box[1], right_box[1])
        )
        intersection = intersection_width * intersection_height
        left_area = (left_box[2] - left_box[0]) * (left_box[3] - left_box[1])
        right_area = (right_box[2] - right_box[0]) * (right_box[3] - right_box[1])
        union = left_area + right_area - intersection
        return intersection / union if union else 0.0

    def deduplicate(candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
        selected: list[dict[str, Any]] = []
        for candidate in sorted(
            candidates,
            key=lambda value: (
                -float(value["frame_score"]),
                -(
                    (value["bbox"][2] - value["bbox"][0])
                    * (value["bbox"][3] - value["bbox"][1])
                ),
                value["bbox"],
            ),
        ):
            if any(intersection_over_union(candidate, kept) >= 0.80 for kept in selected):
                continue
            selected.append(candidate)
        return sorted(selected, key=lambda value: (value["bbox"][1], value["bbox"][0]))

    def layout_quality(candidates: list[dict[str, Any]], axis: str) -> tuple[bool, float]:
        if len(candidates) < 2:
            return False, 0.0
        boxes = [candidate["bbox"] for candidate in candidates]
        areas = np.asarray(
            [(box[2] - box[0]) * (box[3] - box[1]) for box in boxes], dtype=float
        )
        median_area = float(np.median(areas))
        comparable_fraction = float(
            np.mean((areas >= median_area * 0.30) & (areas <= median_area * 3.00))
        )
        two_tile_spread_ok = bool(
            len(areas) != 2 or float(np.max(areas) / max(np.min(areas), 1.0)) <= 3.50
        )
        frame_fraction = float(
            np.mean([float(candidate["frame_score"]) >= 0.18 for candidate in candidates])
        )
        title_strip_fraction = float(
            np.mean(
                [
                    float(candidate["right_title_strip_score"]) >= 0.04
                    for candidate in candidates
                ]
            )
        )
        pairwise_overlaps = [
            intersection_over_union(candidates[left], candidates[right])
            for left in range(len(candidates))
            for right in range(left + 1, len(candidates))
        ]
        non_overlapping = max(pairwise_overlaps, default=0.0) < 0.20

        # Repeated rows/columns may be a grid.  At least one other tile should
        # share most of the perpendicular span with each accepted tile.
        perpendicular_overlap: list[float] = []
        for index, box in enumerate(boxes):
            values_for_box = []
            for other_index, other in enumerate(boxes):
                if index == other_index:
                    continue
                if axis == "x":
                    overlap = max(0, min(box[3], other[3]) - max(box[1], other[1]))
                    denominator = max(1, min(box[3] - box[1], other[3] - other[1]))
                else:
                    overlap = max(0, min(box[2], other[2]) - max(box[0], other[0]))
                    denominator = max(1, min(box[2] - box[0], other[2] - other[0]))
                values_for_box.append(overlap / denominator)
            perpendicular_overlap.append(max(values_for_box, default=0.0))
        aligned_fraction = float(np.mean(np.asarray(perpendicular_overlap) >= 0.65))
        strong_frames = frame_fraction >= 0.50
        accepted = bool(
            comparable_fraction >= 0.70
            and aligned_fraction >= 0.70
            and non_overlapping
            and two_tile_spread_ok
            and strong_frames
            and title_strip_fraction >= 0.50
            and (len(candidates) >= 3 or frame_fraction >= 1.0)
        )
        quality = (
            2.0 * len(candidates)
            + comparable_fraction
            + aligned_fraction
            + frame_fraction
            + title_strip_fraction
            + (1.0 if non_overlapping else 0.0)
        )
        return accepted, quality

    x_candidates = deduplicate(axis_candidates("x"))
    y_candidates = deduplicate(axis_candidates("y"))
    x_accepted, x_quality = layout_quality(x_candidates, "x")
    y_accepted, y_quality = layout_quality(y_candidates, "y")
    candidates_by_axis = {
        "x": x_candidates,
        "y": y_candidates,
    }
    accepted_axes = [
        (axis, quality)
        for axis, accepted, quality in (
            ("x", x_accepted, x_quality),
            ("y", y_accepted, y_quality),
        )
        if accepted
    ]
    all_candidates = deduplicate(x_candidates + y_candidates)
    result["candidate_tiles"] = all_candidates
    if not accepted_axes:
        result["reason_codes"].append("NO_CONSERVATIVE_COMPOSITE_LAYOUT")
        return result

    selected_axis = max(accepted_axes, key=lambda value: (value[1], value[0]))[0]
    selected = candidates_by_axis[selected_axis]
    result.update(
        {
            "is_composite": True,
            "layout_axis": selected_axis,
            "reason_codes": ["DARK_GUTTERED_FRAME_LAYOUT_DETECTED"],
            "tiles": [
                {**candidate, "tile_index": index}
                for index, candidate in enumerate(selected, start=1)
            ],
        }
    )
    return result


def _prepare(path: Path, *, maximum_edge: int = 1_800) -> tuple[Any, float]:
    cv2 = _cv2()
    image = cv2.imread(str(path), cv2.IMREAD_GRAYSCALE)
    if image is None:
        raise ValueError(f"Unable to read image: {path}")
    # AutoCAD screenshots often use a dark canvas while deterministic renders
    # use white. SIFT becomes substantially more stable after normalizing both
    # to dark-line-on-light polarity.
    if float(image.mean()) < 127:
        image = 255 - image
    height, width = image.shape[:2]
    scale = min(1.0, maximum_edge / max(width, height))
    if scale < 1.0:
        image = cv2.resize(
            image,
            (max(1, round(width * scale)), max(1, round(height * scale))),
            interpolation=cv2.INTER_AREA,
        )
    image = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8)).apply(image)
    # Descriptors on edges focus on CAD geometry and dimensions instead of the
    # dark/light canvas polarity or colored hatch fills.
    image = cv2.Canny(image, 45, 150, apertureSize=3, L2gradient=True)
    return image, scale


def register_screenshot_to_panel(
    screenshot: Path | str,
    panel: Path | str,
    *,
    ratio_threshold: float = 0.76,
    reprojection_threshold: float = 6.0,
    panel_cad_bbox: Sequence[float] | None = None,
) -> dict[str, Any]:
    """Register a screenshot to a rendered CAD panel using SIFT + RANSAC.

    Returned homography maps original screenshot pixels to original panel
    pixels. Low-evidence results remain explicit instead of fabricating a match.
    """

    cv2 = _cv2()
    import numpy as np

    screenshot_path = Path(screenshot).resolve()
    panel_path = Path(panel).resolve()
    query, query_scale = _prepare(screenshot_path)
    reference, reference_scale = _prepare(panel_path)
    sift = cv2.SIFT_create(nfeatures=8_000, contrastThreshold=0.02, edgeThreshold=15)
    query_keypoints, query_descriptors = sift.detectAndCompute(query, None)
    panel_keypoints, panel_descriptors = sift.detectAndCompute(reference, None)
    base = {
        "schema_version": "1.0",
        "screenshot": str(screenshot_path),
        "panel": str(panel_path),
        "query_keypoint_count": len(query_keypoints or ()),
        "panel_keypoint_count": len(panel_keypoints or ()),
        "good_match_count": 0,
        "inlier_count": 0,
        "inlier_ratio": 0.0,
        "status": "NO_MATCH",
        "homography": None,
        "projected_query_corners": None,
    }
    if query_descriptors is None or panel_descriptors is None:
        return {**base, "reason": "INSUFFICIENT_FEATURES"}
    matcher = cv2.BFMatcher(cv2.NORM_L2)
    paired = matcher.knnMatch(query_descriptors, panel_descriptors, k=2)
    good = [left for left, right in paired if left.distance < ratio_threshold * right.distance]
    base["good_match_count"] = len(good)
    if len(good) < 4:
        return {**base, "reason": "INSUFFICIENT_GOOD_MATCHES"}
    query_points = np.float32([query_keypoints[value.queryIdx].pt for value in good]).reshape(
        -1, 1, 2
    )
    panel_points = np.float32([panel_keypoints[value.trainIdx].pt for value in good]).reshape(
        -1, 1, 2
    )
    affine, mask = cv2.estimateAffinePartial2D(
        query_points.reshape(-1, 2),
        panel_points.reshape(-1, 2),
        method=cv2.RANSAC,
        ransacReprojThreshold=reprojection_threshold,
        maxIters=10_000,
        confidence=0.999,
        refineIters=25,
    )
    if affine is None or mask is None:
        return {**base, "reason": "RANSAC_FAILED"}
    homography = np.vstack([affine, [0.0, 0.0, 1.0]])
    inlier_count = int(mask.ravel().sum())
    inlier_ratio = inlier_count / len(good)
    # Convert from resized-image coordinates to original pixels:
    # p_panel_orig = inv(S_panel) * H_scaled * S_query * p_query_orig.
    query_transform = np.diag([query_scale, query_scale, 1.0])
    panel_inverse = np.diag([1.0 / reference_scale, 1.0 / reference_scale, 1.0])
    original_homography = panel_inverse @ homography @ query_transform
    original_homography /= original_homography[2, 2]
    raw_query = cv2.imread(str(screenshot_path), cv2.IMREAD_GRAYSCALE)
    raw_panel = cv2.imread(str(panel_path), cv2.IMREAD_GRAYSCALE)
    query_height, query_width = raw_query.shape[:2]
    panel_height, panel_width = raw_panel.shape[:2]
    corners = np.float32(
        [[[0, 0], [query_width, 0], [query_width, query_height], [0, query_height]]]
    )
    projected = cv2.perspectiveTransform(corners, original_homography)[0]
    linear = original_homography[:2, :2]
    scale = float((np.linalg.norm(linear[:, 0]) + np.linalg.norm(linear[:, 1])) / 2)
    rejection_reason = _registration_rejection_reason(
        scale=scale,
        projected=projected,
        panel_width=panel_width,
        panel_height=panel_height,
    )
    diagnostics = {
        "inlier_count": inlier_count,
        "inlier_ratio": round(inlier_ratio, 6),
        "transform_type": "similarity",
        "scale": round(scale, 8),
    }
    if rejection_reason is not None:
        return {
            **base,
            **diagnostics,
            "reason": rejection_reason,
        }
    status = (
        "MATCH"
        if inlier_count >= 10 and inlier_ratio >= 0.22
        else "REVIEW"
    )
    result = {
        **base,
        **diagnostics,
        "status": status,
        "homography": original_homography.round(10).tolist(),
        "projected_query_corners": projected.round(3).tolist(),
    }
    if panel_cad_bbox is not None:
        cad_projection = projected_query_corners_to_cad_bbox(
            projected.tolist(),
            panel_cad_bbox,
            panel_width,
            panel_height,
        )
        result.update(cad_projection)
    return result


def write_registration(path: Path | str, result: dict[str, Any]) -> Path:
    destination = Path(path).resolve()
    write_json_atomic(destination, result)
    return destination
