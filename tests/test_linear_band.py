import copy

import pytest
from cadquote.linear_band import summarize_rectangular_band


def rectangle(key, x0, x1, y0=0, y1=50):
    return {
        "id": key,
        "entity_type": "LWPOLYLINE",
        "closed": True,
        "world_points_truncated": False,
        "approximation": False,
        "length_method": "EXACT",
        "world_points": [[x0, y0], [x1, y0], [x1, y1], [x0, y1], [x0, y0]],
        "geometry": {"curve_segments_present": False},
        "length_drawing_units": 2 * (x1 - x0 + y1 - y0),
    }


def region(*paths):
    return {
        "usable": True,
        "truncation": {
            "any": False,
            "flags": [],
            "dropped_primitive_count": 0,
            "dropped_path_candidate_count": 0,
        },
        "render_bbox": [-10, -10, 10010, 100],
        "primitives": list(paths),
        "units": {"code": 0},
    }


def summarize(r, **kwargs):
    return summarize_rectangular_band(r, baseline=0, height=50, extent=[0, 10000], **kwargs)


def test_floor_opening_gap_but_raised_door_not_subtracted():
    r = region(
        rectangle("wall1", 0, 3000),
        rectangle("wall2", 4000, 10000),
        rectangle("raised-door", 5000, 5800, 50, 2400),
    )
    report = summarize(r)
    assert report["longitudinal_union_drawing_units"] == 9000
    assert report["gaps"] == [[3000, 4000]]
    assert report["partition_residual"] == 0
    assert report["state"] == "REVIEW"
    assert report["quantity"] is None
    assert report["engineering_quantity"] is None
    assert report["material_confirmed"] is False
    assert report["drawing_units"] == {"code": 0}


def test_outline_perimeter_not_length_and_overlaps_count_once():
    report = summarize(
        region(rectangle("a", 0, 3000), rectangle("b", 0, 3000), rectangle("c", 2500, 5000))
    )
    assert report["longitudinal_union_drawing_units"] == 5000
    assert report["candidates"][0]["outline_perimeter"] == 6100
    assert len(report["union"]) == 1


@pytest.mark.parametrize(
    "modification",
    [
        {"closed": False},
        {"approximation": True},
        {"world_points_truncated": True},
        {"geometry": {"curve_segments_present": True}},
        {"entity_type": "CIRCLE"},
        {"world_points": [[0, 0], [1000, 25], [1000, 50], [0, 25], [0, 0]]},
        {"world_points": [[0, 0], [1000, 50], [1000, 0], [0, 50], [0, 0]]},
        {"world_points": [[0, 0], [float("nan"), 0], [1000, 50], [0, 50], [0, 0]]},
    ],
)
def test_reject_non_native_rectangles(modification):
    p = rectangle("a", 0, 1000)
    p.update(modification)
    assert summarize(region(p))["candidates"] == []


def test_top_band_and_whole_door_are_not_floor_strip():
    assert (
        summarize(
            region(rectangle("top", 0, 10000, 2950, 3000), rectangle("door", 0, 1000, 0, 2400))
        )["candidates"]
        == []
    )


def test_positive_gap_not_silently_closed():
    report = summarize(region(rectangle("a", 0, 1000), rectangle("b", 1000.01, 2000)))
    assert len(report["union"]) == 2
    assert report["gaps"][0] == [1000, 1000.01]


@pytest.mark.parametrize(
    "change",
    [{"usable": False}, {"truncation": {"limit": True}}, {"render_bbox": [100, 0, 10000, 50]}],
)
def test_incomplete_probe_fails(change):
    r = region(rectangle("a", 0, 1000))
    r.update(change)
    with pytest.raises(ValueError):
        summarize(r)


def test_outside_extent_not_arbitrarily_clipped_into_component():
    report = summarize(region(rectangle("a", -5, 1000)))
    assert report["candidates"] == []
    assert report["rejected"][0]["reason"] == "EXTENDS_OUTSIDE_SELECTED_EXTENT"


def test_duplicate_id_fails():
    r = region(rectangle("same", 0, 1000), rectangle("same", 2000, 3000))
    with pytest.raises(ValueError, match="unique"):
        summarize(r)


@pytest.mark.parametrize("tolerance", [0, -1, True, float("inf"), 25])
def test_bad_tolerance(tolerance):
    with pytest.raises(ValueError):
        summarize(region(), tolerance=tolerance)


def test_input_not_modified():
    r = region(rectangle("a", 0, 1000))
    original = copy.deepcopy(r)
    summarize(r)
    assert r == original


@pytest.mark.parametrize(
    "field", ["world_points_truncated", "approximation", "length_method", "geometry"]
)
def test_missing_primitive_completeness_is_not_safe(field):
    p = rectangle("a", 0, 1000)
    del p[field]
    assert summarize(region(p))["candidates"] == []


def test_declared_approximate_method_is_not_exact():
    p = rectangle("a", 0, 1000)
    p["length_method"] = "APPROXIMATE_FLATTENING"
    assert summarize(region(p))["candidates"] == []


@pytest.mark.parametrize(
    "field", ["any", "flags", "dropped_primitive_count", "dropped_path_candidate_count"]
)
def test_missing_scan_completeness_is_not_safe(field):
    r = region(rectangle("a", 0, 1000))
    del r["truncation"][field]
    with pytest.raises(ValueError, match="completeness"):
        summarize(r)


def test_missing_truncation_receipt_is_not_safe():
    r = region(rectangle("a", 0, 1000))
    del r["truncation"]
    with pytest.raises(ValueError, match="completeness"):
        summarize(r)


def test_reported_bbox_does_not_replace_native_path():
    p = rectangle("a", 0, 1000)
    p["bbox"] = [0, 0, 10000, 50]
    assert summarize(region(p))["longitudinal_union_drawing_units"] == 1000
