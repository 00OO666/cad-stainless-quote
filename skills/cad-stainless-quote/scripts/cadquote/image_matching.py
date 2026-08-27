"""Feature-based registration for human CAD screenshots and rendered panels."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .io import write_json_atomic


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
    return {
        **base,
        **diagnostics,
        "status": status,
        "homography": original_homography.round(10).tolist(),
        "projected_query_corners": projected.round(3).tolist(),
    }


def write_registration(path: Path | str, result: dict[str, Any]) -> Path:
    destination = Path(path).resolve()
    write_json_atomic(destination, result)
    return destination
