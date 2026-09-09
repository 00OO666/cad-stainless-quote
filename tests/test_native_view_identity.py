"""Native source fixtures; no supplied target page/local-number answers."""

import hashlib

import ezdxf
import pytest
from cadquote.native_view_identity import verify_native_view_identity


def _fixture(tmp_path, variant="ordinary"):
    doc = ezdxf.new("R2018")
    doc.layers.new("VIEWPORTS")
    paper = doc.layouts.new("Paper")
    frame = doc.blocks.new("BORDER")
    frame.add_lwpolyline([(0, 0), (200, 0), (200, 240), (0, 240)], close=True)
    frame.add_lwpolyline([(201, 0), (250, 0), (250, 240), (201, 240)], close=True)
    border = paper.add_blockref("BORDER", (0, 0))
    page = border.add_attrib("SHEET_NUMBER", "DE-03a", (205, 20), dxfattribs={"height": 2})
    border.add_attrib("SHEET_TITLE", "SECTION", (205, 30), dxfattribs={"height": 2})
    vp = paper.add_viewport(
        center=(100, 100), size=(180, 140), view_center_point=(0, 0), view_height=140
    )
    marker = doc.blocks.new("CAPTION")
    circle = marker.add_circle((4, 0), 4)
    marker.add_line((0, 0), (55, 0))

    def title(x=50, y=20):
        parent = paper.add_blockref("CAPTION", (x, y))
        parent.add_attrib("LOCAL", "07b", (x + 3, y + 1), dxfattribs={"height": 1})
        parent.add_attrib("REFERENCE", "PL-02a", (x + 1, y - 2), dxfattribs={"height": 1})
        parent.add_attrib("ENGLISH", "DETAIL", (x + 15, y + 1), dxfattribs={"height": 1})
        parent.add_attrib(
            "VIEW_TITLE", "Cabinet SECTION", (x + 15, y - 2), dxfattribs={"height": 1}
        )
        return parent

    caption = title()
    if variant == "two_titles":
        title(80, 18)
    elif variant == "overlapping_viewports":
        paper.add_viewport(
            center=(100, 100), size=(180, 140), view_center_point=(500, 500), view_height=140
        )
    elif variant == "upper_viewport":
        for boundary in frame.query("LWPOLYLINE"):
            boundary.set_points([(x, 520 if y == 240 else y) for x, y in boundary.get_points("xy")])
        paper.add_viewport(
            center=(100, 350), size=(180, 300), view_center_point=(500, 500), view_height=300
        )
    elif variant == "title_hidden":
        caption.dxf.invisible = 1
    elif variant == "circle_hidden":
        circle.dxf.invisible = 1
    elif variant == "number_hidden":
        caption.attribs[0].dxf.flags = 1
    elif variant == "unknown_layer":
        circle.dxf.layer = "MISSING"
    elif variant == "extra_title_line":
        marker.add_line((30, 0), (40, 10))
    elif variant == "number_outside_circle":
        caption.attribs[0].dxf.insert = (90, 21)
    elif variant == "conflicting_role":
        caption.attribs[-1].dxf.text = "PLAN and SECTION"
    elif variant == "empty_page":
        page.dxf.text = ""
    elif variant == "hidden_page":
        page.dxf.flags = 1
    elif variant == "hidden_frame":
        border.dxf.invisible = 1
    elif variant == "boundary":
        vp.dxf.center = (90, 100)
    elif variant == "crossing_frame":
        vp.dxf.center = (89, 100)
    elif variant == "rotated_frame":
        border.dxf.rotation = 8
    elif variant == "overlapping_frames":
        other = paper.add_blockref("BORDER", (0, 0))
        other.add_attrib("SHEET_NUMBER", "DE-04", (205, 20), dxfattribs={"height": 2})
        other.add_attrib("SHEET_TITLE", "SECTION", (205, 30), dxfattribs={"height": 2})
    elif variant == "clipped_frame":
        border.new_extension_dict().add_dictionary("ACAD_FILTER")
    elif variant == "clipped_viewport":
        vp.dxf.flags |= 65536
    elif variant == "hidden_viewport":
        vp.dxf.status = -1
    elif variant in {"page_only", "loose_text"}:
        paper.delete_entity(caption)
        if variant == "loose_text":
            paper.add_text("07b SECTION", dxfattribs={"insert": (50, 20), "height": 2})
    source = tmp_path / "source.dxf"
    doc.saveas(source)
    return source, vp.dxf.handle, page.dxf.handle


def test_native_identity_retains_suffix_and_backreference_separation(tmp_path):
    source, handle, page = _fixture(tmp_path)
    digest = hashlib.sha256(source.read_bytes()).hexdigest()
    result = verify_native_view_identity(source, layout="Paper", viewport_handle=handle)
    assert result["state"] == "VERIFIED", result
    assert result["page_code"] == "DE-03A"
    assert result["page_code_native_texts"] == ["DE-03a"]
    assert result["local_view_number"] == "07b"
    assert result["backreference_page"] == "PL-02A"
    assert result["backreference_raw_text"] == "PL-02a"
    assert result["role"] == "section"
    assert result["native_handles"]["page_attributes"] == [page]
    assert result["proof"]["selected_title"]["native_geometry"]["rule_handle"]
    assert result["source_sha256"] == digest == hashlib.sha256(source.read_bytes()).hexdigest()


@pytest.mark.parametrize(
    "variant",
    [
        "two_titles",
        "overlapping_viewports",
        "title_hidden",
        "circle_hidden",
        "number_hidden",
        "unknown_layer",
        "extra_title_line",
        "number_outside_circle",
        "conflicting_role",
        "empty_page",
        "hidden_page",
        "hidden_frame",
        "boundary",
        "crossing_frame",
        "rotated_frame",
        "overlapping_frames",
        "clipped_frame",
        "clipped_viewport",
        "hidden_viewport",
    ],
)
def test_unsupported_ambiguous_hidden_or_boundary_is_review(tmp_path, variant):
    source, handle, _ = _fixture(tmp_path, variant)
    result = verify_native_view_identity(source, layout="Paper", viewport_handle=handle)
    assert result["state"] == "REVIEW", (variant, result)
    assert result["reason_codes"]


@pytest.mark.parametrize("variant", ["page_only", "loose_text"])
def test_page_title_does_not_invent_local_number_from_suffix_or_nearest_text(tmp_path, variant):
    source, handle, _ = _fixture(tmp_path, variant)
    result = verify_native_view_identity(source, layout="Paper", viewport_handle=handle)
    assert result["state"] == "REVIEW"
    assert result["page_code"] == "DE-03A"
    assert result["local_view_number"] is None
    assert result["role"] == "section"
    assert result["field_states"]["page_code"] == "VERIFIED"
    assert result["field_states"]["local_view_number"] == "REVIEW"


def test_expected_role_validates_but_does_not_choose_or_replace_native_role(tmp_path):
    source, handle, _ = _fixture(tmp_path)
    result = verify_native_view_identity(
        source, layout="Paper", viewport_handle=handle, expected_role="elevation"
    )
    assert result["state"] == "REVIEW"
    assert result["role"] == "section"
    assert "EXPECTED_ROLE_MISMATCH_OR_UNRESOLVED" in result["reason_codes"]


def test_missing_source_and_wrong_layout_or_handle(tmp_path):
    result = verify_native_view_identity(
        tmp_path / "absent.dxf", layout="Paper", viewport_handle="AA"
    )
    assert result["state"] == "REVIEW"
    assert result["source_sha256"] is None
    source, handle, _ = _fixture(tmp_path)
    for layout, native_handle in (("Other", handle), ("Paper", "FFFF"), ("Model", handle)):
        result = verify_native_view_identity(source, layout=layout, viewport_handle=native_handle)
        assert result["state"] == "REVIEW"
        assert result["page_code"] is None


def test_api_rejects_target_identity_inputs(tmp_path):
    source, handle, _ = _fixture(tmp_path)
    with pytest.raises(TypeError):
        verify_native_view_identity(
            source, layout="Paper", viewport_handle=handle, expected_page="DE-03a"
        )


def test_viewport_geometry_not_text_distance_resolves_vertical_stack(tmp_path):
    source, handle, _ = _fixture(tmp_path, "upper_viewport")
    result = verify_native_view_identity(source, layout="Paper", viewport_handle=handle)
    assert result["state"] == "VERIFIED", result
    assert result["proof"]["selected_title"]["blocked_viewports"]


def test_unknown_layer_competing_viewport_is_not_silently_hidden(tmp_path):
    source, handle, _ = _fixture(tmp_path, "overlapping_viewports")
    doc = ezdxf.readfile(source)
    other = next(
        v
        for v in doc.layouts.get("Paper").query("VIEWPORT")
        if v.dxf.handle != handle and v.dxf.id > 1
    )
    other.dxf.layer = "UNKNOWN_LAYER"
    doc.saveas(source)
    result = verify_native_view_identity(source, layout="Paper", viewport_handle=handle)
    assert result["state"] == "REVIEW", result
    assert "LOCAL_TITLE_UNVERIFIED_OR_VIEWPORT_AMBIGUOUS" in result["reason_codes"]
    title = next(
        t for t in result["proof"]["local_title_candidates"] if handle in t["candidate_viewports"]
    )
    assert len(title["candidate_viewports"]) == 2


def test_explicit_placeholder_backreference_is_not_replaced_with_owning_page(tmp_path):
    source, handle, _ = _fixture(tmp_path)
    doc = ezdxf.readfile(source)
    title = next(p for p in doc.layouts.get("Paper").query("INSERT") if p.dxf.name == "CAPTION")
    title.attribs[1].dxf.text = "--"
    doc.saveas(source)
    result = verify_native_view_identity(source, layout="Paper", viewport_handle=handle)
    assert result["state"] == "VERIFIED", result
    assert result["backreference_page"] is None
    assert result["backreference_raw_text"] == "--"


def test_source_replay_exception_cannot_leave_partially_verified_fields(tmp_path, monkeypatch):
    from cadquote import native_view_identity

    source, handle, _ = _fixture(tmp_path)

    def failed_replay(document, layout, viewport, result):
        result["field_states"]["page_code"] = "VERIFIED"
        raise ValueError("incomplete replay")

    monkeypatch.setattr(native_view_identity, "_verify", failed_replay)
    result = native_view_identity.verify_native_view_identity(
        source, layout="Paper", viewport_handle=handle
    )
    assert result["state"] == "REVIEW"
    assert set(result["field_states"].values()) == {"REVIEW"}


@pytest.mark.parametrize("center_x", [90, 20])
@pytest.mark.parametrize("unknown_layer", [False, True])
def test_touching_or_crossing_competitor_cannot_donate_title_to_upper_viewport(
    tmp_path, center_x, unknown_layer
):
    source, lower_handle, _ = _fixture(tmp_path, "upper_viewport")
    doc = ezdxf.readfile(source)
    paper = doc.layouts.get("Paper")
    upper_handle = next(
        v.dxf.handle
        for v in paper.query("VIEWPORT")
        if v.dxf.id > 1 and v.dxf.handle != lower_handle
    )
    lower = doc.entitydb[lower_handle]
    lower.dxf.center = (center_x, 100)
    if unknown_layer:
        lower.dxf.layer = "UNREGISTERED_LAYER"
    doc.saveas(source)
    result = verify_native_view_identity(source, layout="Paper", viewport_handle=upper_handle)
    assert result["state"] == "REVIEW", result
    assert result["local_view_number"] is None
    competitor = next(
        v for v in result["proof"]["same_page_viewports"] if v["handle"] == lower_handle
    )
    assert competitor["frame_relation"] == "TOUCHING_OR_CROSSING"
    if unknown_layer:
        assert competitor["visibility_issue"]
    title = result["proof"]["local_title_candidates"][0]
    assert any(
        blocked["viewport"] == upper_handle and lower_handle in blocked["occluding_viewports"]
        for blocked in title["blocked_viewports"]
    )
