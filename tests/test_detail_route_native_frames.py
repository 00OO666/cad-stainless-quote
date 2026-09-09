from copy import deepcopy

import pytest
from cadquote.detail_routes import build_detail_routes
from cadquote.models import CadEntity, Sheet


def entity(key, kind="ATTRIB", text=None, parent=None, bbox=None, geometry=None):
    data = dict(geometry or {})
    if parent:
        data["parent_insert_handle"] = parent
    return CadEntity(
        id=key,
        source_file_id="synthetic",
        sheet_id="paper",
        handle=key,
        entity_type=kind,
        space="paper:Layout1",
        text=text,
        insert=(25, -10),
        bbox=bbox,
        geometry=data,
    )


def scan(rectangles=None, **updates):
    return {
        "schema": "native-paper-frame/1",
        "complete": True,
        "issues": [],
        "rectangles": rectangles
        if rectangles is not None
        else [{"bbox": [-10, -20, 110, 110], "source_handles": ["closed-poly"]}],
        **updates,
    }


def fixture(frame_scan=None):
    sheets = [
        Sheet(
            id="node",
            source_file_id="synthetic",
            drawing_number="B3-E-08",
            kind="detail",
            layout="Layout1#viewport:VP",
            viewport_handle="VP",
        )
    ]
    native = [
        entity("VP", "VIEWPORT", bbox=(0, 0, 100, 100)),
        entity(
            "FRAME",
            "INSERT",
            bbox=(900, 900, 900, 900),
            geometry={"paper_frame_scan": scan() if frame_scan is None else frame_scan},
        ),
        entity("page", text="B3-QS-09", parent="FRAME"),
    ]
    return sheets, [], native


def test_verified_rectangles_recover_page_with_far_insert_point():
    args = fixture()
    args[2][-1].insert = (135, -10)  # page ATTRIB can be in the sibling title region
    before = deepcopy(args)
    out = build_detail_routes(*args)
    r = out["page_recovery"]["node"]
    assert r["candidate_page_codes"] == ["B3-QS-09"]
    assert r["frame_geometry_verified"] is True
    assert r["basis"] == "native_closed_paper_frame_contains_entire_viewport"
    assert r["frame_bboxes"] == [[-10.0, -20.0, 110.0, 110.0]]
    assert r["frame_source_handles"] == ["closed-poly"]
    assert args == before and out["state"] == "REVIEW" and not out["mutates_takeoff"]


@pytest.mark.parametrize(
    "broken",
    [
        scan([]),
        scan(complete=False),
        scan(issues=["unsupported"]),
        scan(schema="other"),
        None,
        {},
        scan([{"bbox": [-10, -20, 110, 110], "source_handles": []}]),
        scan([{"bbox": [-10, -20, float("nan"), 110], "source_handles": ["p"]}]),
        scan([{"bbox": [0, 0, 10, 10], "source_handles": ["p"]}]),
    ],
)
def test_explicit_failed_empty_or_noncontaining_scan_never_uses_insert_bbox(broken):
    args = fixture()
    args[2][1].geometry["paper_frame_scan"] = broken
    args[2][1].bbox = (-100, -100, 200, 200)
    assert build_detail_routes(*args)["page_recovery"] == {}


def test_no_scan_keeps_legacy_candidate_with_unverified_basis():
    args = fixture()
    args[2][1].geometry = {}
    args[2][1].bbox = (-10, -20, 110, 110)
    r = build_detail_routes(*args)["page_recovery"]["node"]
    assert r["basis"] == "legacy_insert_bbox_contains_entire_viewport_unverified"
    assert r["frame_geometry_verified"] is False


def test_verified_native_frame_beats_conflicting_legacy_bbox():
    args = fixture()
    args[2].extend(
        [
            entity("OLD", "INSERT", bbox=(-50, -50, 200, 200)),
            entity("old-code", text="B3-QS-10", parent="OLD"),
        ]
    )
    r = build_detail_routes(*args)["page_recovery"]["node"]
    assert r["candidate_page_codes"] == ["B3-QS-09"]
    assert r["frame_handles"] == ["FRAME"] and not r["conflict"]


@pytest.mark.parametrize("mismatch", ["source", "space", "parent", "hidden"])
def test_native_frame_preserves_source_space_parent_and_visibility(mismatch):
    args = fixture()
    if mismatch == "source":
        args[2][1].source_file_id = "other"
    elif mismatch == "space":
        args[2][1].space = "paper:Other"
    elif mismatch == "parent":
        args[2][-1].geometry["parent_insert_handle"] = "OTHER"
    else:
        args[2][1].geometry["semantic_hidden"] = True
    assert build_detail_routes(*args)["page_recovery"] == {}


def test_two_verified_overlapping_frames_preserve_conflict():
    args = fixture()
    args[2].extend(
        [
            entity("OTHER", "INSERT", geometry={"paper_frame_scan": scan()}),
            entity("other-code", text="B3-QS-10", parent="OTHER"),
        ]
    )
    r = build_detail_routes(*args)["page_recovery"]["node"]
    assert r["conflict"] and r["candidate_page_codes"] == ["B3-QS-09", "B3-QS-10"]


def test_same_verified_parent_conflicting_page_numbers_do_not_disappear():
    args = fixture()
    args[2].append(entity("conflicting-code", text="B3-QS-10", parent="FRAME"))
    r = build_detail_routes(*args)["page_recovery"]["node"]
    assert r["conflict"] and r["candidate_page_codes"] == ["B3-QS-09", "B3-QS-10"]
    assert r["frame_handles"] == ["FRAME"]


@pytest.mark.parametrize("bad_text", [None, "", "--", "B3-QS-09AB", "GC-SS-907"])
@pytest.mark.parametrize("keep_valid", [True, False])
def test_explicit_missing_or_invalid_page_slot_blocks_stale_page_fallback(bad_text, keep_valid):
    args = fixture()
    if not keep_valid:
        args[2].pop()
    args[2].append(entity("bad-slot", text=bad_text, parent="FRAME", geometry={"tag": "DWG_NO"}))
    r = build_detail_routes(*args)["page_recovery"]["node"]
    assert r["state"] == "UNRESOLVED" and r["conflict"]
    assert r["invalid_page_slot_ids"] == ["bad-slot"]
    assert r["blocks_stale_page_fallback"]
    assert r["candidate_page_codes"] == (["B3-QS-09"] if keep_valid else [])


def test_hidden_invalid_slot_does_not_block_visible_page():
    args = fixture()
    args[2].append(
        entity("hidden", parent="FRAME", geometry={"tag": "DWG_NO", "semantic_hidden": True})
    )
    assert not build_detail_routes(*args)["page_recovery"]["node"]["conflict"]


@pytest.mark.parametrize("block_source", [True, False])
def test_invalid_source_or_target_slot_cannot_restore_reciprocal_selection(block_source):
    sheets, projected, native = fixture()
    sheets.append(
        Sheet(
            id="elev",
            source_file_id="synthetic",
            drawing_number="B3-E-08",
            kind="elevation",
            layout="Layout2#viewport:EVP",
            viewport_handle="EVP",
        )
    )
    projected.extend(
        [
            entity("call-code", text="B3-QS-09", parent="CALL").model_copy(
                update={"sheet_id": "elev"}
            ),
            entity("call-number", text="07", parent="CALL").model_copy(update={"sheet_id": "elev"}),
        ]
    )
    native.extend(
        [
            entity("title-code", text="B3-E-08", parent="TITLE"),
            entity("title-number", text="07", parent="TITLE"),
            entity("title-text", text="DETAIL", parent="TITLE"),
        ]
    )
    if block_source:
        native.extend(
            e.model_copy(update={"space": "paper:Layout2"})
            for e in [
                entity("EVP", "VIEWPORT", bbox=(0, 0, 100, 100)),
                entity("EFRAME", "INSERT", geometry={"paper_frame_scan": scan()}),
                entity("epage", text="B3-E-08", parent="EFRAME"),
                entity("invalid", parent="EFRAME", geometry={"tag": "DWG_NO"}),
            ]
        )
    else:
        native.append(entity("invalid", parent="FRAME", geometry={"tag": "DWG_NO"}))
    r = build_detail_routes(sheets, projected, native)["records"][0]
    assert r["navigation_state"] == "PAGE_ONLY"
    assert r["suggested_target_sheet_id"] is None and r["suggested_detail_sheet_id"] is None
    if block_source:
        assert r["resolved_source_page"] is None


def test_contains_requires_full_viewport_not_center_or_overlap():
    args = fixture(scan([{"bbox": [-10, -10, 80, 80], "source_handles": ["p"]}]))
    assert build_detail_routes(*args)["page_recovery"] == {}
