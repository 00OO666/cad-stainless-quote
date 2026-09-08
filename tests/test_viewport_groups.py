import json
from copy import deepcopy

import pytest
from cadquote.viewport_groups import attach_node_group_routes, build_node_view_groups
from test_detail_routes import ent


def vp(handle, box, model):
    return ent(
        handle,
        kind="VIEWPORT",
        parent=None,
        bbox=box,
        geometry={
            "height": box[3] - box[1],
            "view_height": (box[3] - box[1]) * 10,
            "view_direction_vector": [0, 0, 1],
            "view_twist_angle": 0,
            "model_bbox": model,
        },
    )


def title(parent, number, point, back="B3-E-08"):
    return [
        ent(parent + "n", parent=parent, text=number, point=point),
        ent(parent + "c", parent=parent, text=back, point=(point[0], point[1] - 2)),
        ent(parent + "t", parent=parent, text="DETAIL", point=point),
    ]


def fixture():
    native = [
        ent("frame", kind="INSERT", parent=None, bbox=(-20, -150, 500, 200)),
        ent("page", parent="frame", text="B3-QS-09"),
        vp("A", (0, 0, 100, 100), (0, 0, 1000, 1000)),
        vp("B", (104, 0, 204, 100), (2000, 0, 3000, 1000)),
        *title("T", "07", (80, -20)),
    ]
    return native


def test_broken_group_retains_independent_coordinates_and_never_quantity():
    native = fixture()
    before = deepcopy(native)
    out = build_node_view_groups([], native)
    g = out["groups"][0]
    assert g["viewport_handles"] == ["A", "B"]
    assert g["selected_title"]["view_number"] == "07"
    assert g["physical_quantity"] is None and not g["physical_component_confirmed"]
    assert [m["model_bbox_raw"] for m in g["members"]] == [
        [0, 0, 1000, 1000],
        [2000, 0, 3000, 1000],
    ]
    assert "model_bbox" not in g
    assert g["adjacency_edges"][0]["model_gap_raw"] == 1000
    assert native == before
    assert build_node_view_groups([], list(reversed(native))) == out


@pytest.mark.parametrize(
    "change",
    [
        "large_gap",
        "scale",
        "cross_model",
        "reverse_model",
        "overlap",
        "twist",
        "side_view",
        "hidden",
        "other_layout",
    ],
)
def test_nearby_viewports_do_not_automatically_merge(change):
    native = fixture()
    b = native[3]
    if change == "large_gap":
        b.bbox = (115, 0, 215, 100)
    elif change == "scale":
        b.geometry["view_height"] *= 2
    elif change == "cross_model":
        b.geometry["model_bbox"] = [2000, 50, 3000, 1050]
    elif change == "reverse_model":
        b.geometry["model_bbox"] = [-2000, 0, -1000, 1000]
    elif change == "overlap":
        b.bbox = (50, 0, 150, 100)
    elif change == "twist":
        b.geometry["view_twist_angle"] = 90
    elif change == "side_view":
        b.geometry["view_direction_vector"] = [1, 0, 0]
    elif change == "hidden":
        b.geometry["semantic_hidden"] = True
    elif change == "other_layout":
        b.space = "paper:Other"
    assert all(len(g["members"]) == 1 for g in build_node_view_groups([], native)["groups"])


def test_vertical_broken_view_with_bottom_right_caption():
    native = fixture()[:2] + [
        vp("A", (0, 0, 100, 100), (0, 0, 1000, 1000)),
        vp("B", (0, 104, 100, 184), (0, 3000, 1000, 3800)),
        *title("T", "08", (130, 8)),
    ]
    g = build_node_view_groups([], native)["groups"][0]
    assert len(g["members"]) == 2
    assert g["selected_title"]["relation"] == "bottom_right"


def test_adjacent_independent_titles_block_unique_merged_group():
    native = fixture() + title("OTHER", "08", (170, -20))
    g = build_node_view_groups([], native)["groups"][0]
    assert g["group_state"] == "AMBIGUOUS_TITLE_GROUP" and g["selected_title"] is None


def test_competing_view_groups_do_not_choose_closest_caption():
    native = fixture()[:2] + [
        vp("A", (0, 0, 100, 100), (0, 0, 1000, 1000)),
        vp("B", (0, 5, 100, 105), (0, 0, 1000, 1000)),
        *title("T", "07", (50, -20)),
    ]
    assert all(g["selected_title"] is None for g in build_node_view_groups([], native)["groups"])


def test_multiple_page_frames_fail_closed():
    native = fixture() + [
        ent("f2", kind="INSERT", parent=None, bbox=(-21, -151, 501, 201)),
        ent("p2", parent="f2", text="B3-QS-10"),
    ]
    out = build_node_view_groups([], native)
    assert out["groups"] == []
    assert {i["reason"] for i in out["issues"]} == {"MULTIPLE_PAGE_FRAMES"}


def test_global_caption_constraints_resolve_side_caption_without_nearest_guess():
    native = fixture()[:2] + [
        vp("A", (0, 0, 100, 100), (0, 0, 1000, 1000)),
        vp("B", (140, 0, 240, 100), (0, 2000, 1000, 3000)),
        *title("T1", "07", (20, -10)),
        *title("T2", "08", (150, -10)),
    ]
    out = build_node_view_groups([], native)
    mapping = {g["viewport_handles"][0]: g["selected_title"]["view_number"] for g in out["groups"]}
    assert mapping == {"A": "07", "B": "08"}


def test_back_reference_conflict_and_duplicate_pages_never_select():
    catalogue = build_node_view_groups([], fixture())
    r = {
        "route_id": "r",
        "reference_role": "OUTGOING_CALLOUT",
        "target_page": "B3-QS-09",
        "target_view": "07",
        "resolved_source_page": "B3-E-08",
    }
    old = {"records": [r], "summary": {"PAGE_ONLY": 1}}
    out = attach_node_group_routes(old, catalogue)
    assert out == attach_node_group_routes(old, json.loads(json.dumps(catalogue)))
    assert out["records"][0]["suggested_detail_group_id"]
    assert "suggested_detail_group_id" not in r
    assert out["summary"] == old["summary"]
    r["resolved_source_page"] = "B3-E-99"
    assert attach_node_group_routes(old, catalogue)["records"][0]["group_navigation_state"] == (
        "GROUP_BACK_REFERENCE_CONFLICT"
    )
    r["resolved_source_page"] = "B3-E-08"
    catalogue["groups"].append(deepcopy(catalogue["groups"][0]))
    assert (
        attach_node_group_routes(old, catalogue)["records"][0]["suggested_detail_group_id"] is None
    )


@pytest.mark.parametrize("value", [0, -1, float("nan"), float("inf"), 0.5])
def test_invalid_gap_limit(value):
    with pytest.raises(ValueError):
        build_node_view_groups([], [], max_gap_ratio=value)
