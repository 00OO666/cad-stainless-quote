import math

import ezdxf
import pytest
from cadquote.strip_profiles import analyze_native_strip_profile, analyze_strip_outline
from shapely.geometry import LineString


def profile(thickness=2):
    # Synthetic, independent of customer dimensions and contours.
    center = [(0, 20), (0, 0), (30, 0), (30, 15), (55, 15)]
    return list(LineString(center).buffer(thickness / 2, cap_style=2, join_style=2).exterior.coords)


def analyze(points, **kwargs):
    return analyze_strip_outline(points, source_file_id="synthetic", entity_handle="P", **kwargs)


def test_recovers_middle_path_without_using_outline_perimeter_as_unfold():
    data = analyze(profile())
    assert data["profile_state"] == "UNIQUE_GEOMETRIC_STRIP"
    candidate = data["candidates"][0]
    assert candidate["geometric_middle_path_length_mm"] == pytest.approx(90)
    assert candidate["geometric_thickness_mm"] == pytest.approx(2)
    assert data["outline_perimeter_mm"] == pytest.approx(184)
    assert data["unfolded_width_mm"] is None and data["physical_quantity"] is None
    assert data["manufacturing_blank_width_mm"] is None and not data["mutates_takeoff"]


@pytest.mark.parametrize("mode", ["reverse", "start", "rotate", "translate"])
def test_vertex_order_and_rigid_transform_do_not_change_geometry(mode):
    points = profile()[:-1]
    if mode == "reverse":
        points.reverse()
    elif mode == "start":
        points = points[3:] + points[:3]
    elif mode == "rotate":
        angle = math.radians(33)
        points = [
            (x * math.cos(angle) - y * math.sin(angle), x * math.sin(angle) + y * math.cos(angle))
            for x, y in points
        ]
    elif mode == "translate":
        points = [(x + 125000, y - 80000) for x, y in points]
    result = analyze(points)
    assert result["profile_state"] == "UNIQUE_GEOMETRIC_STRIP"
    assert result["candidates"][0]["geometric_middle_path_length_mm"] == pytest.approx(90)


def test_square_has_competing_middle_axes_and_is_not_selected():
    result = analyze([(0, 0), (8, 0), (8, 8), (0, 8)])
    assert result["profile_state"] == "AMBIGUOUS_GEOMETRIC_STRIP"
    assert len(result["candidates"]) == 2
    assert result["unfolded_width_mm"] is None


@pytest.mark.parametrize(
    "points",
    [
        [(0, 0), (10, 0), (12, 4), (0, 2)],
        [(0, 0), (10, 10), (0, 10), (10, 0)],
        [(0, 0), (0, 0), (10, 1), (10, 0)],
        [(0, 0), (10, 0), (math.nan, 1), (0, 1)],
        [(0, 0, 2), (10, 0, 2), (10, 1, 2), (0, 1, 2)],
    ],
)
def test_variable_thickness_invalid_and_nonplanar_shapes_are_not_proved(points):
    assert not analyze(points)["candidates"]


@pytest.mark.parametrize("limits", [{"max_vertices": 3}, {"max_cap_pairs": 1}])
def test_caps_remain_explicit(limits):
    result = analyze([(0, 0), (8, 0), (8, 8), (0, 8)], **limits)
    assert result["truncated"] and not result["candidates"]


def test_unknown_units_are_not_assumed_millimetres():
    assert analyze(profile(), units="unknown")["issues"] == ["MILLIMETRE_UNITS_NOT_VERIFIED"]


@pytest.mark.parametrize("mode", ["normal", "open", "arc", "stroke", "paper", "nested"])
def test_native_wrapper_rejects_unsupported_geometry_without_mutation(mode):
    doc = ezdxf.new("R2018")
    doc.units = 4
    layout = (
        doc.layout("Layout1")
        if mode == "paper"
        else doc.blocks.new("NESTED")
        if mode == "nested"
        else doc.modelspace()
    )
    entity = layout.add_lwpolyline(profile()[:-1], close=True)
    if mode == "open":
        entity.closed = False
    elif mode == "arc":
        pts = list(entity.get_points())
        pts[0] = (*pts[0][:4], 0.1)
        entity.set_points(pts)
    elif mode == "stroke":
        entity.dxf.const_width = 1
    before = list(entity.get_points())
    if mode == "normal":
        result = analyze_native_strip_profile(
            doc, source_file_id="synthetic", entity_handle=entity.dxf.handle
        )
        assert result["profile_state"] == "UNIQUE_GEOMETRIC_STRIP"
    else:
        with pytest.raises(ValueError):
            analyze_native_strip_profile(
                doc, source_file_id="synthetic", entity_handle=entity.dxf.handle
            )
    assert list(entity.get_points()) == before
