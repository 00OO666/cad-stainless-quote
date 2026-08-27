from cadquote.image_matching import _polygon_area, _registration_rejection_reason


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
