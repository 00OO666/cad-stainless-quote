from cadquote.image_matching import (
    _polygon_area,
    _registration_rejection_reason,
    decompose_dark_cad_screenshot,
    projected_query_corners_to_cad_bbox,
)


def test_registration_rejects_collapsed_transform() -> None:
    points = [[20.0, 20.0], [20.1, 20.0], [20.1, 20.1], [20.0, 20.1]]

    assert _polygon_area(points) < 16.0
    assert (
        _registration_rejection_reason(
            scale=0.1,
            projected=points,
            panel_width=1_000,
            panel_height=800,
        )
        == "COLLAPSED_PROJECTION"
    )


def test_registration_accepts_finite_projection_on_panel() -> None:
    points = [[100.0, 100.0], [500.0, 100.0], [500.0, 350.0], [100.0, 350.0]]

    assert _polygon_area(points) == 100_000.0
    assert (
        _registration_rejection_reason(
            scale=1.25,
            projected=points,
            panel_width=1_000,
            panel_height=800,
        )
        is None
    )


def test_registration_rejects_projection_outside_panel() -> None:
    points = [[2_000.0, 2_000.0], [2_400.0, 2_000.0], [2_400.0, 2_300.0], [2_000.0, 2_300.0]]

    assert (
        _registration_rejection_reason(
            scale=1.0,
            projected=points,
            panel_width=1_000,
            panel_height=800,
        )
        == "PROJECTION_OUTSIDE_PANEL"
    )


def test_projected_full_image_maps_to_full_panel_cad_bbox() -> None:
    result = projected_query_corners_to_cad_bbox(
        [[0, 0], [1_000, 0], [1_000, 800], [0, 800]],
        [100, 200, 2_100, 1_800],
        1_000,
        800,
    )

    assert result["valid"] is True
    assert result["projected_query_cad_bbox"] == [100.0, 200.0, 2_100.0, 1_800.0]
    assert result["state"] == "REVIEW"
    assert result["reason_codes"] == ["SCREENSHOT_BBOX_REGISTRATION_REVIEW_ONLY"]


def test_projected_local_region_maps_by_panel_scale() -> None:
    result = projected_query_corners_to_cad_bbox(
        [[100, 200], [400, 200], [400, 500], [100, 500]],
        [0, 0, 2_000, 1_600],
        1_000,
        800,
    )

    assert result["projected_query_cad_bbox"] == [200.0, 600.0, 800.0, 1_200.0]
    assert result["in_panel_coverage_ratio"] == 1.0


def test_projected_region_flips_image_y_axis_to_cad_y_axis() -> None:
    top_region = projected_query_corners_to_cad_bbox(
        [[0, 0], [100, 0], [100, 200], [0, 200]],
        [0, 0, 1_000, 800],
        1_000,
        800,
    )
    bottom_region = projected_query_corners_to_cad_bbox(
        [[0, 600], [100, 600], [100, 800], [0, 800]],
        [0, 0, 1_000, 800],
        1_000,
        800,
    )

    assert top_region["projected_query_cad_bbox"] == [0.0, 600.0, 100.0, 800.0]
    assert bottom_region["projected_query_cad_bbox"] == [0.0, 0.0, 100.0, 200.0]


def test_projected_region_rejects_non_finite_and_degenerate_inputs() -> None:
    non_finite = projected_query_corners_to_cad_bbox(
        [[0, 0], [100, 0], [float("nan"), 100], [0, 100]],
        [0, 0, 1_000, 800],
        1_000,
        800,
    )
    degenerate = projected_query_corners_to_cad_bbox(
        [[0, 0], [100, 100], [200, 200], [300, 300]],
        [0, 0, 1_000, 800],
        1_000,
        800,
    )

    assert non_finite["projected_query_cad_bbox"] is None
    assert "NON_FINITE_PROJECTED_QUERY_CORNERS" in non_finite["reason_codes"]
    assert degenerate["projected_query_cad_bbox"] is None
    assert "DEGENERATE_PROJECTED_QUERY_BBOX" in degenerate["reason_codes"]


def test_projected_region_clips_out_of_bounds_and_marks_low_coverage_review() -> None:
    result = projected_query_corners_to_cad_bbox(
        [[-900, 100], [100, 100], [100, 300], [-900, 300]],
        [0, 0, 1_000, 800],
        1_000,
        800,
    )

    assert result["valid"] is True
    assert result["projected_query_cad_bbox"] == [0.0, 500.0, 100.0, 700.0]
    assert result["clipped_to_panel"] is True
    assert result["in_panel_coverage_ratio"] == 0.1
    assert "PROJECTED_QUERY_CLIPPED_TO_PANEL" in result["reason_codes"]
    assert "LOW_IN_PANEL_COVERAGE" in result["reason_codes"]
    assert result["state"] == "REVIEW"


def test_image_match_cli_accepts_optional_panel_bbox() -> None:
    from cad_quote import build_parser

    args = build_parser().parse_args(
        [
            "image-match",
            "human-crop.png",
            "rendered-panel.png",
            "--panel-bbox",
            "-100",
            "200",
            "900",
            "1200",
        ]
    )

    assert args.panel_bbox == [-100.0, 200.0, 900.0, 1_200.0]


def _draw_frame(
    image: object,
    bbox: tuple[int, int, int, int],
    *,
    colour: tuple[int, int, int] = (220, 120, 20),
) -> None:
    import numpy as np

    values = np.asarray(image)
    left, top, right, bottom = bbox
    values[top : top + 2, left:right] = colour
    values[bottom - 2 : bottom, left:right] = colour
    values[top:bottom, left : left + 2] = colour
    values[top:bottom, right - 2 : right] = colour
    values[top + 8 : bottom - 8 : 8, left + 5 : right - 5] = (30, 180, 240)


def test_decompose_dark_cad_screenshot_finds_horizontal_montage_tiles() -> None:
    import numpy as np

    image = np.full((120, 600, 3), (48, 40, 33), dtype=np.uint8)
    expected = [
        (20, 20, 120, 105),
        (135, 20, 235, 105),
        (250, 20, 350, 105),
        (365, 20, 465, 105),
        (480, 20, 580, 105),
    ]
    for bbox in expected:
        _draw_frame(image, bbox)

    result = decompose_dark_cad_screenshot(image)

    assert result["is_composite"] is True
    assert result["layout_axis"] == "x"
    assert [tile["bbox"] for tile in result["tiles"]] == [list(bbox) for bbox in expected]
    assert all(tile["frame_score"] >= 0.5 for tile in result["tiles"])


def test_decompose_dark_cad_screenshot_preserves_single_view() -> None:
    import numpy as np

    image = np.full((160, 240, 3), (20, 24, 28), dtype=np.uint8)
    _draw_frame(image, (18, 16, 222, 145))

    result = decompose_dark_cad_screenshot(image)

    assert result["is_composite"] is False
    assert result["tiles"] == [
        {
            "tile_index": 1,
            "bbox": [0, 0, 240, 160],
            "bbox_source": "full_image",
            "content_density": None,
            "frame_score": None,
        }
    ]


def test_decompose_dark_cad_screenshot_rejects_unframed_fragments() -> None:
    import numpy as np

    image = np.full((120, 420, 3), (24, 24, 24), dtype=np.uint8)
    image[30:70:4, 20:100] = (255, 255, 255)
    image[30:70:4, 160:240] = (255, 255, 255)
    image[30:70:4, 300:380] = (255, 255, 255)

    result = decompose_dark_cad_screenshot(image)

    assert result["is_composite"] is False
    assert result["tiles"][0]["bbox"] == [0, 0, 420, 120]
    assert "NO_CONSERVATIVE_COMPOSITE_LAYOUT" in result["reason_codes"]
