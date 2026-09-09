"""Synthetic original DXF frames; no project-specific reference identities."""

from copy import deepcopy

import ezdxf
import pytest
from cadquote.cad_index import index_dxf
from cadquote.native_reference_scope import NativeReferenceScope


def _case(tmp_path, variant="ordinary"):
    doc = ezdxf.new("R2018")
    paper = doc.layouts.new("Paper")
    frame_block = doc.blocks.new("PAGE_BORDER")
    frame_block.add_lwpolyline([(0, 0), (100, 0), (100, 100), (0, 100)], close=True)
    frame_block.add_lwpolyline([(101, 0), (120, 0), (120, 100), (101, 100)], close=True)
    frames, pages = [], []
    for x, code in ((0, "PL-01"), (200, "DE-01")):
        frame = paper.add_blockref("PAGE_BORDER", (x, 0))
        page = frame.add_attrib("SHEET_NUMBER", code, (x + 103, 20), dxfattribs={"height": 1})
        frame.add_attrib("SHEET_TITLE", "SECTION", (x + 103, 30), dxfattribs={"height": 1})
        frames.append(frame)
        pages.append(page)
    viewport = paper.add_viewport(
        center=(50, 50), size=(90, 90), view_center_point=(50, 50), view_height=90
    )
    marker = doc.blocks.new("REFERENCE_MARK")
    marker.add_circle((0, 0), 2)
    ref_parent = paper.add_blockref("REFERENCE_MARK", (230, 50))
    ref = ref_parent.add_attrib("SHEET_NUMBER", "DT-09", (230, 50), dxfattribs={"height": 1})
    ref_parent.add_attrib("#", "01", (230, 53), dxfattribs={"height": 1})
    if variant.startswith("hatch_"):
        hatch = marker.add_hatch()
        if variant == "hatch_unsupported_edges":
            hatch.paths.add_edge_path().add_spline(control_points=[(0, 0), (1, 2), (3, 4), (5, 0)])
        else:
            left = -40 if variant == "hatch_crosses_frame" else -3
            hatch.paths.add_polyline_path(
                [(left, 0, 1), (3, 0, 0), (0, -3, 0)], is_closed=variant != "hatch_open"
            )
        if variant == "hatch_transformed":
            ref_parent.dxf.rotation = 30
            ref_parent.dxf.xscale = -2
            ref_parent.dxf.yscale = 1.5
    if variant == "current_page":
        ref_parent.translate(-200, 0, 0)
    elif variant == "boundary":
        ref.dxf.insert = (200, 50)
    elif variant == "hidden_reference":
        ref.dxf.flags = 1
    elif variant == "hidden_page":
        pages[1].dxf.flags = 1
    elif variant == "hidden_frame":
        frames[1].dxf.invisible = 1
    elif variant == "clipped_frame":
        frames[1].new_extension_dict().add_dictionary("ACAD_FILTER")
    elif variant in {"overlapping_frames", "rotated_competitor"}:
        other = paper.add_blockref(
            "PAGE_BORDER",
            (200, 0),
            dxfattribs={"rotation": 5 if variant == "rotated_competitor" else 0},
        )
        other.add_attrib("SHEET_NUMBER", "DE-02", (303, 20), dxfattribs={"height": 1})
        other.add_attrib("SHEET_TITLE", "SECTION", (303, 30), dxfattribs={"height": 1})
    elif variant == "duplicate_page_code":
        pages[1].dxf.text = "PL-01"
    elif variant == "parent_crosses_frame":
        ref_parent.add_attrib("OTHER", "X", (190, 50), dxfattribs={"height": 1})
    source = tmp_path / "native-paper-scope.dxf"
    doc.saveas(source)
    indexed = index_dxf(source)
    by_handle = {e.handle: e for e in indexed.entities if e.handle}
    selected = {
        "id": "selected-panel",
        "source_file_id": indexed.source_file_id,
        "viewport_handle": viewport.dxf.handle,
        "evidence": [f"paper_page_reference:PL-01@{by_handle[pages[0].dxf.handle].id}"],
    }
    scope = NativeReferenceScope(
        doc, source_file_id=indexed.source_file_id, native_entities=indexed.entities
    )
    return scope, by_handle[ref.dxf.handle], selected, doc, indexed


def test_unique_other_native_page_has_original_frame_and_page_attribute_proof(tmp_path):
    scope, reference, selected, _, _ = _case(tmp_path)
    result = scope.classify(reference, selected)
    assert result["exclude_from_selected_scope"], result
    assert result["classification"] == "OTHER_NATIVE_PAGE"
    owner = result["reference_page_owners"][0]
    assert owner["page_code"] == "DE-01"
    assert owner["page_number_evidence"][0]["handle"]
    assert owner["containing_rectangles"][0]["source_handle_chains"]
    assert result["state"] == "REVIEW"
    assert not result["proves_component_or_target_view_ownership"]


def test_current_page_title_stays_in_scope(tmp_path):
    scope, reference, selected, _, _ = _case(tmp_path, "current_page")
    result = scope.classify(reference, selected)
    assert result["classification"] == "CURRENT_NATIVE_PAGE_RETAINED", result
    assert not result["exclude_from_selected_scope"]


@pytest.mark.parametrize("variant", ["hatch_closed", "hatch_transformed"])
def test_native_closed_hatch_bounds_include_bulges_and_insert_transform(tmp_path, variant):
    scope, reference, selected, _, _ = _case(tmp_path, variant)
    result = scope.classify(reference, selected)
    assert result["exclude_from_selected_scope"], result
    enclosure = result["reference_page_owners"][0]["native_parent_bound_evidence"][
        "native_hatch_enclosures"
    ][0]
    assert enclosure["native_paths"][0][0][2] == 1
    assert enclosure["insert_matrices_outer_to_inner"]


@pytest.mark.parametrize(
    "variant",
    [
        "boundary",
        "hidden_reference",
        "hidden_page",
        "hidden_frame",
        "clipped_frame",
        "overlapping_frames",
        "rotated_competitor",
        "duplicate_page_code",
        "parent_crosses_frame",
        "hatch_crosses_frame",
        "hatch_open",
        "hatch_unsupported_edges",
    ],
)
def test_unverified_or_ambiguous_scope_is_never_excluded(tmp_path, variant):
    scope, reference, selected, _, _ = _case(tmp_path, variant)
    result = scope.classify(reference, selected)
    assert not result["exclude_from_selected_scope"], result
    assert result["reason_codes"]


def test_missing_source_document_is_explicit(tmp_path):
    _, reference, selected, _, indexed = _case(tmp_path)
    scope = NativeReferenceScope(
        None, source_file_id=indexed.source_file_id, native_entities=indexed.entities
    )
    result = scope.classify(reference, selected)
    assert result["reason_codes"] == ["SOURCE_DOCUMENT_OR_IDENTITY_MISSING"]


def test_source_mismatch_cannot_borrow_another_document_frame(tmp_path):
    scope, reference, selected, _, _ = _case(tmp_path)
    selected["source_file_id"] = "different-source"
    result = scope.classify(reference, selected)
    assert not result["exclude_from_selected_scope"]
    assert "SOURCE_SCOPE_MISMATCH" in result["reason_codes"]


def test_selected_replayed_page_identity_is_independently_checked(tmp_path):
    scope, reference, selected, _, _ = _case(tmp_path)
    selected["evidence"] = ["paper_page_reference:DE-99@unrelated-attribute"]
    result = scope.classify(reference, selected)
    assert not result["exclude_from_selected_scope"]
    assert "SELECTED_REPLAYED_PAGE_IDENTITY_MISMATCH" in result["reason_codes"]


def test_changed_reference_does_not_reuse_an_identity_only_cache(tmp_path):
    scope, reference, selected, _, _ = _case(tmp_path)
    assert scope.classify(reference, selected)["exclude_from_selected_scope"]
    forged = deepcopy(reference.model_dump())
    forged["insert"] = [1, 1]
    result = scope.classify(forged, selected)
    assert not result["exclude_from_selected_scope"]
    assert "REFERENCE_NOT_IDENTICAL_TO_SOURCE_NATIVE_INDEX" in result["reason_codes"]
