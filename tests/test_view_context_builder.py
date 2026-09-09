"""Original synthetic CAD, never self-authored evaluation facts or customer rows."""

from copy import deepcopy

import ezdxf
import pytest
from cadquote.cad_index import CadIndexBundle, index_dxf
from cadquote.io import write_json_atomic
from cadquote.models import TakeoffItem
from cadquote.panels import expand_viewport_panels
from cadquote.view_context_builder import (
    ViewContextBuildError,
    build_view_context,
    verify_view_context_receipt,
)
from cadquote.view_dispositions import prepare_view_context, resolve_view_applicability


def _write(path, data):
    write_json_atomic(path, data)
    return path


@pytest.fixture
def cad_case(tmp_path, request):
    variant = getattr(request, "param", "ordinary")
    doc = ezdxf.new("R2018")
    doc.units = 4
    model = doc.modelspace()
    plan_object = model.add_lwpolyline([(20, 20), (80, 20), (80, 70), (20, 70)], close=True)
    target_object = model.add_lwpolyline([(220, 20), (280, 20), (280, 70), (220, 70)], close=True)
    if variant in {"target_nested_visible", "target_nested_frozen", "target_nested_missing_layer"}:
        model.delete_entity(target_object)
        doc.layers.new("NESTED_CHILD")
        child = doc.blocks.new("OBJECT_CHILD")
        child.add_lwpolyline(
            [(220, 20), (280, 20), (280, 70), (220, 70)],
            close=True,
            dxfattribs={"layer": "NESTED_CHILD"},
        )
        outer = doc.blocks.new("OBJECT_OUTER")
        outer.add_blockref("OBJECT_CHILD", (0, 0))
        target_object = model.add_blockref("OBJECT_OUTER", (0, 0))
        if variant == "target_nested_missing_layer":
            doc.layers.remove("NESTED_CHILD")
    plan_material = model.add_text("MT-01", dxfattribs={"insert": (25, 25), "height": 2})
    model.add_text("MT-01", dxfattribs={"insert": (225, 25), "height": 2})
    dimension = model.add_linear_dim(base=(250, 80), p1=(220, 20), p2=(280, 20))
    dimension.render()
    if variant == "target_viewport_frozen":
        doc.layers.new("TARGET_VIEWPORT_FROZEN")
        target_object.dxf.layer = "TARGET_VIEWPORT_FROZEN"
        dimension.dimension.dxf.layer = "TARGET_VIEWPORT_FROZEN"
    if variant in {"unresolved", "outside", "conflict"}:
        position = (250, 40) if variant != "outside" else (290, 90)
        model.add_text("EL-09", dxfattribs={"insert": position, "height": 2})
    frame = doc.blocks.new("PAGE_FRAME")
    frame_right = 205 if variant.startswith("secondary_title") else 105
    frame.add_lwpolyline([(-5, -10), (frame_right, -10), (frame_right, 105), (-5, 105)], close=True)
    refblock = doc.blocks.new("REF")
    refblock.add_circle((0, 0), 1)
    if variant.startswith("cut_"):
        refblock.add_solid([(0, 0), (-2, -1), (-2, 1), (-2, 1)])
        refblock.add_line((-2, 0), (-5, 0))
    refs = {}
    ref_parents = {}
    material_tag = material_leader = None
    layout_specs = [
        ("Plan", "PL-01", (50, 50), "SE-01"),
        ("Section", "SE-01", (250, 50), "PL-01"),
    ]
    if variant == "conflict":
        layout_specs.append(("Elevation", "EL-09", (450, 50), "PL-01"))
        model.add_text("ELEVATION", dxfattribs={"insert": (450, 50), "height": 2})
    for name, page, center, target in layout_specs:
        layout = doc.layouts.new(name)
        viewport = layout.add_viewport(
            center=(50, 50), size=(100, 100), view_center_point=center, view_height=100
        )
        if name == "Section" and variant == "target_viewport_frozen":
            viewport.frozen_layers = ["TARGET_VIEWPORT_FROZEN"]
        if name == "Section" and variant == "target_nested_frozen":
            viewport.frozen_layers = ["NESTED_CHILD"]
        frame_insert = layout.add_blockref("PAGE_FRAME", (0, 0))
        page_attribute = frame_insert.add_attrib(
            "SHEET_NUMBER", page, (70, -7), dxfattribs={"height": 2}
        )
        if name == "Plan" and variant == "hidden_page_slot":
            page_attribute.dxf.flags = 1
        frame_insert.add_attrib("SHEET_TITLE", name.upper(), (30, -7), dxfattribs={"height": 2})
        point = (40, 40) if name == "Plan" else (40, 5)
        if name == "Plan" and variant == "outside_callout":
            point = (90, 40)
        if name == "Plan" and variant.startswith("cut_"):
            point = (90, 90) if variant == "cut_wrong_object" else (90, 40)
            if variant == "cut_inside_no_crossing":
                point = (40, 40)
            if variant == "cut_boundary_touch":
                point = (90, 20)
            start = (point[0] - 5, point[1])
            if variant == "cut_gap":
                start = (start[0] - 0.1, start[1])
            endpoint = (30, 40) if variant == "cut_inside_no_crossing" else (0, point[1])
            layout.add_line(start, endpoint)
            if variant == "cut_branch":
                layout.add_line(start, (start[0], start[1] + 20))
        if name == "Section" and variant == "clipped_title":
            point = (40, -6)
        insert = layout.add_blockref("REF", point)
        ref_parents[name] = insert
        attrpoint = (90, 40) if name == "Plan" and variant == "outside_annotation" else point
        refs[name] = [
            insert.add_attrib("SHEET_NUMBER", target, attrpoint, dxfattribs={"height": 2}),
            insert.add_attrib(
                "#", "01", (attrpoint[0], attrpoint[1] + 3), dxfattribs={"height": 2}
            ),
        ]
        if name == "Plan" and variant == "hidden_attribute":
            refs[name][0].dxf.flags = 1
        if name == "Plan" and variant == "hidden_parent":
            insert.dxf.invisible = 1
        if name == "Plan" and variant == "hidden_parent_layer":
            doc.layers.new("HIDDEN_NATIVE_PARENT")
            doc.layers.get("HIDDEN_NATIVE_PARENT").off()
            insert.dxf.layer = "HIDDEN_NATIVE_PARENT"
        if name == "Plan" and variant.startswith("label_"):
            label_block = doc.blocks.new("MATERIAL_LABEL")
            label_block.add_lwpolyline([(0, 0), (16, 0), (16, 4), (0, 4)], close=True)
            label = layout.add_blockref("MATERIAL_LABEL", (84, 45))
            material_tag = label.add_attrib("MATERIAL", "MT-01", (85, 46), dxfattribs={"height": 1})
            endpoint = (83.5, 45) if variant == "label_gap" else (84, 45)
            material_leader = layout.add_leader([(50, 50), (80, 45), endpoint])
            if variant == "label_ambiguous":
                other = layout.add_blockref("MATERIAL_LABEL", (84, 45))
                other.add_attrib("MATERIAL", "MT-02", (85, 46), dxfattribs={"height": 1})
            if variant == "label_hidden_leader":
                material_leader.dxf.invisible = 1
        if name == "Plan" and variant in {"unprojected_reference", "unprojected_hidden"}:
            extra = layout.add_text("EL-99", dxfattribs={"insert": (150, 50), "height": 2})
            if variant == "unprojected_hidden":
                extra.dxf.invisible = 1
        if name == "Plan" and variant == "unselected_hidden_cut":
            extra_block = doc.blocks.new("UNSELECTED_HIDDEN_CUT")
            arrow = extra_block.add_solid([(0, 0), (-2, -1), (-2, 1), (-2, 1)])
            arrow.dxf.invisible = 1
            extra_block.add_line((-2, 0), (-5, 0))
            extra = layout.add_blockref("UNSELECTED_HIDDEN_CUT", (85, 45))
            extra.add_attrib("SHEET_NUMBER", "EL-99", (85, 45), dxfattribs={"height": 2})
            extra.add_attrib("#", "09", (85, 48), dxfattribs={"height": 2})
            layout.add_line((80, 45), (0, 45))
        if name == "Plan" and variant == "unselected_circle_divider":
            extra_block = doc.blocks.new("UNSELECTED_CIRCLE_DIVIDER")
            extra_block.add_circle((0, 0), 2)
            extra_block.add_line((-2, 0), (2, 0))
            extra = layout.add_blockref("UNSELECTED_CIRCLE_DIVIDER", (85, 45))
            extra.add_attrib("SHEET_NUMBER", "EL-99", (85, 45), dxfattribs={"height": 2})
            extra.add_attrib("#", "09", (85, 48), dxfattribs={"height": 2})
            layout.add_line((83, 45), (0, 45))
        if name == "Section":
            title = "ELEVATION" if variant == "elevation_chain" else "SECTION"
            if variant == "mixed_title":
                title = "SECTION ELEVATION"
            if variant == "negated_mixed_title":
                title = "NOT A SECTION - ELEVATION"
            title_attribute = insert.add_attrib(
                "TITLE", title, (40, point[1] - 4), dxfattribs={"height": 2}
            )
            if variant == "hidden_title":
                title_attribute.dxf.flags = 1
        else:
            layout.add_text("PLAN", dxfattribs={"insert": (10, 3), "height": 2})
        if name == "Plan" and variant in {
            "unprojected_other_page",
            "unprojected_other_page_boundary",
        }:
            other_frame = layout.add_blockref("PAGE_FRAME", (300, 100))
            other_frame.add_attrib("SHEET_NUMBER", "PL-02", (370, 93), dxfattribs={"height": 2})
            other_frame.add_attrib("SHEET_TITLE", "OTHER PLAN", (330, 93), dxfattribs={"height": 2})
            x = 340 if variant == "unprojected_other_page" else 401
            layout.add_text("EL-99", dxfattribs={"insert": (x, 150), "height": 2})
        if name == "Section" and variant.startswith("secondary_title"):
            model.add_line((430, 30), (470, 70))
            model.add_text("SECONDARY SECTION", dxfattribs={"insert": (450, 50), "height": 2})
            layout.add_viewport(
                center=(165, 50), size=(60, 70), view_center_point=(450, 50), view_height=70
            )
            caption_block = doc.blocks.new("NATIVE_SECONDARY_VIEW_TITLE")
            caption_block.add_circle((0, 0), 2)
            line_end = 24 if variant == "secondary_title_caption_rule" else 2
            caption_block.add_line((-2, 0), (line_end, 0))
            if variant == "secondary_title_cut":
                caption_block.add_solid([(0, 0), (-2, -1), (-2, 1), (-2, 1)])
            if variant == "secondary_title_line_arrow":
                for start, end in (
                    ((-3, 0), (-5, -1)),
                    ((-5, -1), (-5, 1)),
                    ((-5, 1), (-3, 0)),
                    ((-5, 0), (-8, 0)),
                ):
                    caption_block.add_line(start, end)
                layout.add_line((157, 11), (40, 40))
            caption = layout.add_blockref(caption_block.name, (165, 11))
            code = "PL-09" if variant == "secondary_title_wrong_plan" else "PL-01"
            caption.add_attrib("SHEET_NUMBER", code, (165, 11), dxfattribs={"height": 2})
            caption.add_attrib("#", "09", (165, 14), dxfattribs={"height": 2})
            caption.add_attrib("ROLE", "SECTION", (175, 14), dxfattribs={"height": 2})
            scale = caption.add_attrib(
                "CAPTION", "SECTION SCALE:1/1", (165, 7), dxfattribs={"height": 2}
            )
            if variant == "secondary_title_hidden_scale":
                doc.layers.new("HIDDEN_SCALE").off()
                scale.dxf.layer = "HIDDEN_SCALE"
        if name == "Plan" and variant.startswith("fastener_"):
            note = {
                "fastener_instruction": "M8膨胀螺栓固定",
                "fastener_reference": "参见M8膨胀螺栓固定详图",
                "fastener_mixed": "M8膨胀螺栓固定，参见 EL-99",
                "fastener_duplicate_reference": "M8膨胀螺栓固定；M-08",
            }[variant]
            layout.add_mtext(note, dxfattribs={"insert": (140, 150), "char_height": 2})
    source = tmp_path / "synthetic.dxf"
    doc.saveas(source)
    indexed = index_dxf(source)
    panels = expand_viewport_panels(
        indexed.sheets, indexed.entities, source_names={indexed.source_file_id: source.name}
    )
    panel_data = {
        "sheets": [s.model_dump(mode="json") for s in panels.sheets],
        "entities": [e.model_dump(mode="json") for e in panels.entities],
    }
    assert len(panels.sheets) == len(layout_specs) + int(variant.startswith("secondary_title"))
    by_layout = {}
    for sheet in panels.sheets:
        by_layout.setdefault(sheet.layout.split("#")[0], sheet)
    by_handle = {e.handle: e.id for e in indexed.entities if e.handle}
    source_ids = [by_handle[e.dxf.handle] for e in refs["Plan"]]
    target_ids = [by_handle[e.dxf.handle] for e in refs["Section"]]
    section_dimension = by_handle[dimension.dimension.dxf.handle]
    selection = {
        "schema_version": "cad-view-context-selections/1",
        "side": "predicted",
        "components": [
            {
                "component_id": "synthetic-component",
                "basis_kind": "plan_section",
                "basis": "Reviewed native plan and section are sufficient for this component.",
                "review": {
                    "reviewer": "independent CAD model review",
                    "reviewed_at": "2026-01-01T00:00:00Z",
                    "reason": "Selected physical region and native reciprocal references.",
                },
                "views": [
                    {
                        "id": "plan-view",
                        "role": "plan",
                        "sheet_id": by_layout["Plan"].id,
                        "object_bbox": [19, 19, 81, 71],
                        "evidence_ids": source_ids,
                        "native_object_handles": [plan_object.dxf.handle],
                    },
                    {
                        "id": "section-view",
                        "role": "section",
                        "sheet_id": by_layout["Section"].id,
                        "object_bbox": [219, 19, 281, 71],
                        "evidence_ids": [section_dimension],
                        "native_object_handles": [target_object.dxf.handle],
                    },
                ],
                "source_reference_ids": source_ids,
                "target_reference_ids": target_ids,
                "negative_search": {
                    "searched_sheet_ids": [by_layout["Plan"].id, by_layout["Section"].id]
                },
            }
        ],
    }
    if variant == "elevation_chain":
        component = selection["components"][0]
        component["basis_kind"] = "plan_elevation_without_detail"
        component["views"][1]["role"] = "elevation"
        component["negative_search"]["searched_sheet_ids"] = [by_layout["Section"].id]
    if material_tag is not None:
        view = selection["components"][0]["views"][0]
        view["evidence_ids"] = [by_handle[material_tag.dxf.handle]]
        view["binding_entity_ids"] = [by_handle[material_leader.dxf.handle]]
    if variant == "outside_callout":
        selection["components"][0]["views"][0]["binding_entity_ids"] = [
            by_handle[plan_material.dxf.handle]
        ]
    index_path = _write(tmp_path / "index.json", CadIndexBundle([indexed]).to_dict())
    panels_path = _write(tmp_path / "panels.json", panel_data)
    selections_path = _write(tmp_path / "selection.json", selection)
    return {
        "source": source,
        "index": index_path,
        "panels": panels_path,
        "selection": selections_path,
        "data": selection,
        "indexed": indexed,
        "panel_data": panel_data,
        "source_parent_handle": ref_parents["Plan"].dxf.handle,
    }


def _build(case, **kwargs):
    return build_view_context(
        case["index"], case["panels"], case["selection"], side="predicted", **kwargs
    )


def test_native_source_produces_verifiable_context_and_receipt(cad_case):
    before = {key: cad_case[key].read_bytes() for key in ("source", "index", "panels", "selection")}
    result = _build(cad_case)
    assert result["receipt"]["components"][0]["state"] == "VERIFIED"
    assert result["context"]["searches"][0]["complete"] is True
    assert result["context"]["searches"][0]["unresolved_reference_ids"] == []
    assert (
        verify_view_context_receipt(
            cad_case["index"],
            cad_case["panels"],
            cad_case["selection"],
            result["context"],
            result["receipt"],
            side="predicted",
        )
        is True
    )
    assert {key: cad_case[key].read_bytes() for key in before} == before
    assert any(e["entity_type"] == "LWPOLYLINE" for e in result["context"]["entities"])
    # The actual evaluator accepts only the independently resolved native chain.
    claim = result["disposition_claims"]["synthetic-component"]
    item = TakeoffItem(
        sequence=1,
        component_id="synthetic-component",
        name="Synthetic",
        mt_code="MT-01",
        elevation=None,
        evidence_ids=claim["elevation"]["evidence_ids"],
        view_dispositions=claim,
    )
    applicability = resolve_view_applicability(
        item, "elevation", prepare_view_context(result["context"], "predicted")
    )
    assert applicability.state == "NOT_APPLICABLE"


@pytest.mark.parametrize("cad_case", ["cut_connected"], indirect=True)
def test_external_symbol_binds_by_actual_cut_path_without_enlarging_bbox(cad_case):
    result = _build(cad_case)
    component = result["receipt"]["components"][0]
    assert component["state"] == "VERIFIED"
    source_ids = cad_case["data"]["components"][0]["source_reference_ids"]
    for identifier in source_ids:
        basis = component["entity_bindings"][identifier]["binding_basis"]
        assert basis["kind"] == "native_cut_path_component_crossing"
        assert basis["crossing_object_ids"]
    proofs = [
        proof
        for proof in component["native_cut_path_diagnostics"]
        if proof["view_id"] == "plan-view"
    ]
    assert len(proofs) == 1  # Same-parent attributes reuse one native path scan.
    assert proofs[0]["result"]["supported"] is True
    assert result["receipt"]["native_cut_path_producer_sha256"]
    assert verify_view_context_receipt(
        cad_case["index"],
        cad_case["panels"],
        cad_case["selection"],
        result["context"],
        result["receipt"],
        side="predicted",
    )


@pytest.mark.parametrize(
    "cad_case",
    ["cut_gap", "cut_wrong_object", "cut_inside_no_crossing", "cut_boundary_touch", "cut_branch"],
    indirect=True,
)
def test_cut_path_counterevidence_is_not_overridden_by_text_or_bbox(cad_case):
    with pytest.raises(ViewContextBuildError, match="no component binding") as error:
        _build(cad_case)
    assert error.value.diagnostic["native_cut_path"]["supported"] is False


@pytest.mark.parametrize("cad_case", ["target_viewport_frozen"], indirect=True)
def test_hidden_in_selected_viewport_cannot_supply_target_evidence(cad_case):
    with pytest.raises(ViewContextBuildError, match="hidden native object"):
        _build(cad_case)


@pytest.mark.parametrize("cad_case", ["target_nested_visible"], indirect=True)
def test_visible_nested_native_object_preserves_visibility_proof(cad_case):
    result = _build(cad_case)
    bindings = result["receipt"]["components"][0]["entity_bindings"].values()
    block = next(b for b in bindings if b.get("entity_type") == "INSERT")
    assert block["native_instance_visibility"]["complete"]
    assert block["native_instance_visibility"]["visible_leaf_entities"] == 1


@pytest.mark.parametrize(
    "cad_case", ["target_nested_frozen", "target_nested_missing_layer"], indirect=True
)
def test_hidden_or_unknown_nested_object_cannot_supply_full_block_bbox(cad_case):
    with pytest.raises(
        ViewContextBuildError, match="hidden native object descendant|missing layer"
    ):
        _build(cad_case)


@pytest.mark.parametrize("cad_case", ["unselected_hidden_cut"], indirect=True)
def test_unresolved_hidden_symbol_is_not_negative_scope_exclusion(cad_case):
    result = _build(cad_case)
    component = result["receipt"]["components"][0]
    assert component["state"] == "REVIEW"
    matches = [r for r in component["reference_inventory"] if "EL-99" in r["target_codes"]]
    assert matches
    assert all(r["state"] == "UNRESOLVED_REFERENCE" for r in matches)
    assert result["context"]["searches"][0]["unresolved_reference_ids"]


@pytest.mark.parametrize(
    "mutation",
    [
        "source_bytes",
        "source_hash",
        "source_path",
        "duplicate_entity",
        "wrong_entity_source",
        "panel_entity_source",
        "panel_entity_sheet",
        "invented_type",
        "panel_bbox",
    ],
)
def test_source_and_snapshot_tampering_fail_closed(cad_case, mutation, tmp_path):
    import json

    index = json.loads(cad_case["index"].read_text(encoding="utf-8"))
    panels = deepcopy(cad_case["panel_data"])
    if mutation == "source_bytes":
        with cad_case["source"].open("ab") as file:
            file.write(b"\n")
    elif mutation == "source_hash":
        index["sources"][0]["source_sha256"] = "0" * 64
    elif mutation == "source_path":
        other = tmp_path / "other.dxf"
        ezdxf.new().saveas(other)
        index["sources"][0]["source_path"] = str(other)
    elif mutation == "duplicate_entity":
        index["sources"][0]["entities"].append(index["sources"][0]["entities"][0])
    elif mutation == "wrong_entity_source":
        index["sources"][0]["entities"][0]["source_file_id"] = "file:wrong"
    elif mutation == "panel_entity_source":
        panels["entities"][0]["source_file_id"] = "file:wrong"
    elif mutation == "panel_entity_sheet":
        panels["entities"][0]["sheet_id"] = next(
            s["id"] for s in panels["sheets"] if s["id"] != panels["entities"][0]["sheet_id"]
        )
    elif mutation == "invented_type":
        index["sources"][0]["entities"][0]["entity_type"] = "DIMENSION"
    elif mutation == "panel_bbox":
        panels["sheets"][0]["bbox"][0] += 1
    _write(cad_case["index"], index)
    _write(cad_case["panels"], panels)
    with pytest.raises(ViewContextBuildError):
        _build(cad_case)


@pytest.mark.parametrize(
    "mutation",
    [
        "side",
        "target",
        "duplicate",
        "wrong_view",
        "whole_sheet",
        "target_value",
        "complete",
        "object",
        "cross_object",
    ],
)
def test_selections_cannot_supply_facts_or_reuse_wrong_source_entities(cad_case, mutation):
    from pydantic import ValidationError

    selected = deepcopy(cad_case["data"])
    component = selected["components"][0]
    if mutation == "side":
        selected["side"] = "gold"
    elif mutation == "target":
        component["target_reference_ids"] = component["source_reference_ids"]
    elif mutation == "duplicate":
        component["views"][1]["evidence_ids"] *= 2
    elif mutation == "wrong_view":
        component["views"][1]["role"] = "elevation"
    elif mutation == "whole_sheet":
        component["views"][0]["object_bbox"] = next(
            s["bbox"]
            for s in cad_case["panel_data"]["sheets"]
            if s["id"] == component["views"][0]["sheet_id"]
        )
    elif mutation == "target_value":
        component["quantity"] = 1
    elif mutation == "complete":
        component["negative_search"]["complete"] = True
    elif mutation == "object":
        component["views"][0]["native_object_handles"] = ["MISSING"]
    elif mutation == "cross_object":
        component["views"][0]["native_object_handles"] = component["views"][1][
            "native_object_handles"
        ]
    _write(cad_case["selection"], selected)
    with pytest.raises((ViewContextBuildError, ValidationError)):
        _build(cad_case)


def test_invented_confirmed_edge_never_establishes_connection(cad_case, tmp_path):
    selected = deepcopy(cad_case["data"])
    selected["components"][0]["source_reference_ids"] = selected["components"][0][
        "target_reference_ids"
    ]
    _write(cad_case["selection"], selected)
    relations = _write(
        tmp_path / "relations.json",
        [
            {
                "id": "made-up",
                "source_id": "plan-view",
                "target_id": "section-view",
                "relation": "plan_to_section",
                "status": "CONFIRMED",
            }
        ],
    )
    with pytest.raises(ViewContextBuildError):
        _build(cad_case, relations_path=relations)


def test_context_edit_rejected_on_reproduction(cad_case):
    result = _build(cad_case)
    result["context"]["entities"][0]["entity_type"] = "ARBITRARY"
    with pytest.raises(ViewContextBuildError, match="context differs"):
        verify_view_context_receipt(
            cad_case["index"],
            cad_case["panels"],
            cad_case["selection"],
            result["context"],
            result["receipt"],
            side="predicted",
        )


@pytest.mark.parametrize(
    "cad_case", ["clipped_title", "outside_annotation", "elevation_chain"], indirect=True
)
def test_native_annotation_associations_and_second_topology(cad_case):
    result = _build(cad_case)
    assert result["receipt"]["components"][0]["state"] == "VERIFIED"
    component = result["receipt"]["components"][0]
    assert component["entity_bindings"]
    assert result["context"]["searches"][0]["unresolved_reference_ids"] == []


@pytest.mark.parametrize("cad_case", ["unresolved", "outside", "conflict"], indirect=True)
def test_negative_search_keeps_real_scoped_candidates_and_conflicts(cad_case):
    result = _build(cad_case)
    rows = [
        r
        for r in result["receipt"]["components"][0]["reference_inventory"]
        if r["target_codes"] == ["EL-09"]
    ]
    assert len(rows) == 1
    search = result["context"]["searches"][0]
    if rows[0]["state"] == "OUTSIDE_COMPONENT":
        assert search["unresolved_reference_ids"] == []
        assert rows[0]["reason"]
    else:
        assert rows[0]["entity_id"] in search["unresolved_reference_ids"]
        assert result["receipt"]["components"][0]["state"] == "REVIEW"
        if rows[0]["candidate_sheet_ids"]:
            assert search["conflicting_view_ids"] == rows[0]["candidate_sheet_ids"]


def test_incomplete_index_scan_is_not_a_negative_proof(cad_case):
    import json

    payload = json.loads(cad_case["index"].read_text(encoding="utf-8"))
    payload["sources"][0]["block_expansion_truncated"] = True
    _write(cad_case["index"], payload)
    result = _build(cad_case)
    assert result["context"]["searches"][0]["complete"] is False
    assert result["receipt"]["components"][0]["scan_issues"]


def test_same_native_entity_twice_through_alias_is_rejected(cad_case):
    selected = deepcopy(cad_case["data"])
    view = selected["components"][0]["views"][1]
    original_id = view["evidence_ids"][0]
    alias = next(
        e["id"]
        for e in cad_case["panel_data"]["entities"]
        if e["sheet_id"] == view["sheet_id"]
        and e["geometry"].get("original_entity_id") == original_id
    )
    view["evidence_ids"].append(alias)
    _write(cad_case["selection"], selected)
    with pytest.raises(ViewContextBuildError, match="aliases"):
        _build(cad_case)


def test_standalone_cli_roundtrip_and_source_overwrite_guard(cad_case, tmp_path):
    import json

    from cadquote.view_context_builder import main

    context_path = tmp_path / "context.json"
    receipt_path = tmp_path / "receipt.json"
    claims_path = tmp_path / "claims.json"
    args = [str(cad_case[key]) for key in ("index", "panels", "selection")]
    args += [
        "--side",
        "predicted",
        "--out",
        str(context_path),
        "--receipt",
        str(receipt_path),
        "--claims",
        str(claims_path),
    ]
    assert main(args) == 0
    assert (
        verify_view_context_receipt(
            cad_case["index"],
            cad_case["panels"],
            cad_case["selection"],
            json.loads(context_path.read_text(encoding="utf-8")),
            json.loads(receipt_path.read_text(encoding="utf-8")),
            side="predicted",
        )
        is True
    )
    before = cad_case["source"].read_bytes()
    args[args.index("--out") + 1] = str(cad_case["source"])
    with pytest.raises(ViewContextBuildError, match="overwrites"):
        main(args)
    assert cad_case["source"].read_bytes() == before


def test_current_native_reference_cannot_be_omitted_from_index(cad_case):
    import json

    payload = json.loads(cad_case["index"].read_text(encoding="utf-8"))
    identifier = cad_case["data"]["components"][0]["source_reference_ids"][0]
    payload["sources"][0]["entities"] = [
        e for e in payload["sources"][0]["entities"] if e["id"] != identifier
    ]
    _write(cad_case["index"], payload)
    with pytest.raises(ViewContextBuildError, match="inventory differs"):
        _build(cad_case)


@pytest.mark.parametrize("cad_case", ["conflict"], indirect=True)
def test_untrusted_relation_complete_flag_does_not_erase_native_conflict(cad_case, tmp_path):
    relation_path = _write(
        tmp_path / "edges.json", {"complete": True, "conflicts": [], "edges": []}
    )
    result = _build(cad_case, relations_path=relation_path)
    assert result["context"]["searches"][0]["conflicting_view_ids"]
    assert result["receipt"]["relation_candidates"]["complete"] is True


@pytest.mark.parametrize("cad_case", ["label_frame"], indirect=True)
def test_native_leader_endpoint_binds_actual_label_frame_not_text_insertion(cad_case):
    result = _build(cad_case)
    bindings = result["receipt"]["components"][0]["entity_bindings"]
    selected = cad_case["data"]["components"][0]["views"][0]["evidence_ids"][0]
    basis = bindings[selected]["binding_basis"]
    assert basis["kind"] == "native_label_frame_shared_endpoint"
    assert basis["native_shared_endpoint"] == [84.0, 45.0, 0.0]
    assert basis["label_parent_original_id"]
    assert bindings[selected]["native_visibility_verified"] is True


@pytest.mark.parametrize(
    "cad_case", ["label_gap", "label_ambiguous", "label_hidden_leader"], indirect=True
)
def test_nearby_or_ambiguous_or_hidden_leader_cannot_bind_label(cad_case):
    with pytest.raises(ViewContextBuildError, match="no component binding"):
        _build(cad_case)


@pytest.mark.parametrize(
    "cad_case",
    ["hidden_attribute", "hidden_parent", "hidden_parent_layer", "hidden_page_slot"],
    indirect=True,
)
def test_native_attribute_parent_and_inherited_layer_visibility(cad_case):
    with pytest.raises(ViewContextBuildError, match="hidden"):
        _build(cad_case)


@pytest.mark.parametrize(
    "cad_case", ["hidden_title", "mixed_title", "negated_mixed_title"], indirect=True
)
def test_hidden_sibling_title_cannot_supply_view_role(cad_case):
    with pytest.raises(ViewContextBuildError, match="native title role"):
        _build(cad_case)


def test_component_boundary_accepts_serialization_error_but_not_drawing_gap():
    from cadquote.view_context_builder import _contains

    assert _contains((100, 200, 300, 400), (100 - 3e-10, 250))
    assert not _contains((100, 200, 300, 400), (100 - 1e-4, 250))


@pytest.mark.parametrize("cad_case", ["unprojected_reference"], indirect=True)
def test_unprojected_unknown_native_reference_keeps_negative_search_incomplete(cad_case):
    result = _build(cad_case)
    component = result["receipt"]["components"][0]
    rows = [r for r in component["reference_inventory"] if r["target_codes"] == ["EL-99"]]
    assert len(rows) == 1
    assert rows[0]["state"] == "UNPROJECTED_NATIVE_REFERENCE_SCOPE_UNRESOLVED"
    assert rows[0]["entity_id"] in result["context"]["searches"][0]["unresolved_reference_ids"]
    assert result["context"]["searches"][0]["complete"] is False
    assert component["state"] == "REVIEW" and component["scan_issues"]


@pytest.mark.parametrize("cad_case", ["unprojected_hidden"], indirect=True)
def test_unprojected_hidden_reference_is_retained_with_native_reason(cad_case):
    result = _build(cad_case)
    rows = [
        r
        for r in result["receipt"]["components"][0]["reference_inventory"]
        if r["target_codes"] == ["EL-99"]
    ]
    assert rows[0]["state"] == "HIDDEN_NATIVE_REFERENCE"
    assert "native flags" in rows[0]["reason"]


@pytest.mark.parametrize("cad_case", ["unselected_circle_divider"], indirect=True)
def test_unrecognized_native_circle_divider_cannot_prove_negative_search(cad_case):
    result = _build(cad_case)
    component = result["receipt"]["components"][0]
    rows = [r for r in component["reference_inventory"] if r["target_codes"] == ["EL-99"]]
    assert len(rows) == 1
    assert rows[0]["state"] == "UNRESOLVED_REFERENCE"
    assert component["state"] == "REVIEW"
    assert rows[0]["entity_id"] in result["context"]["searches"][0]["unresolved_reference_ids"]
    claim = result["disposition_claims"]["synthetic-component"]
    item = TakeoffItem(
        sequence=1,
        component_id="synthetic-component",
        name="Synthetic",
        mt_code="MT-01",
        evidence_ids=claim["elevation"]["evidence_ids"],
        view_dispositions=claim,
    )
    applicability = resolve_view_applicability(
        item, "elevation", prepare_view_context(result["context"], "predicted")
    )
    assert applicability.state != "NOT_APPLICABLE"


@pytest.mark.parametrize(
    "filename",
    [
        "view_context_builder.py",
        "native_cut_paths.py",
        "native_reference_scope.py",
        "native_graphic_review.py",
    ],
)
def test_producer_code_drift_during_build_cannot_issue_receipt(cad_case, monkeypatch, filename):
    from cadquote import view_context_builder as producer

    real_hash = producer.sha256_file
    reads = 0

    def changing_hash(path):
        nonlocal reads
        if path.name == filename:
            reads += 1
            if reads > 1:
                return "0" * 64
        return real_hash(path)

    monkeypatch.setattr(producer, "sha256_file", changing_hash)
    with pytest.raises(ViewContextBuildError, match="producer code changed during build"):
        _build(cad_case)


@pytest.mark.parametrize(
    "cad_case", ["unprojected_other_page", "unprojected_other_page_boundary"], indirect=True
)
def test_original_other_page_scope_is_audited_without_erasing_boundary_reference(cad_case):
    result = _build(cad_case)
    component = result["receipt"]["components"][0]
    rows = [r for r in component["reference_inventory"] if r["target_codes"] == ["EL-99"]]
    assert len(rows) == 1
    proof = rows[0]["native_page_scope"]
    assert proof and proof["proves_component_or_target_view_ownership"] is False
    if proof["exclude_from_selected_scope"]:
        assert rows[0]["state"] == "OTHER_NATIVE_PAGE"
        assert proof["reference_page_owners"][0]["page_code"] == "PL-02"
        assert proof["selected_page_owners"][0]["page_code"] == "PL-01"
        assert (
            rows[0]["entity_id"] not in result["context"]["searches"][0]["unresolved_reference_ids"]
        )
        assert component["state"] == "VERIFIED"
    else:
        assert rows[0]["state"] == "UNPROJECTED_NATIVE_REFERENCE_SCOPE_UNRESOLVED"
        assert rows[0]["entity_id"] in result["context"]["searches"][0]["unresolved_reference_ids"]
        assert component["state"] == "REVIEW"
    native = next(e for e in cad_case["indexed"].entities if e.text == "EL-99")
    assert proof["exclude_from_selected_scope"] == (native.insert[0] == 340)


@pytest.mark.parametrize(
    "cad_case", ["fastener_instruction", "fastener_reference", "fastener_mixed"], indirect=True
)
def test_native_fastener_instruction_does_not_erase_explicit_references(cad_case):
    result = _build(cad_case)
    rows = [
        r
        for r in result["receipt"]["components"][0]["reference_inventory"]
        if "M-08" in r["target_codes"]
    ]
    assert len(rows) == 1
    entity = next(e for e in cad_case["indexed"].entities if e.id == rows[0]["entity_id"])
    is_instruction = entity.text == "M8膨胀螺栓固定"
    assert (rows[0]["state"] == "NON_REFERENCE_CONSTRUCTION_NOTE") == is_instruction
    assert result["context"]["searches"][0]["complete"] == is_instruction


@pytest.mark.parametrize(
    "cad_case",
    ["secondary_title", "secondary_title_wrong_plan", "secondary_title_cut"],
    indirect=True,
)
def test_title_metadata_requires_actual_native_role_viewport_and_connected_plan(cad_case):
    result = _build(cad_case)
    inventory = result["receipt"]["components"][0]["reference_inventory"]
    rows = [
        r
        for r in inventory
        if (r.get("native_semantic_scope") or {}).get("kind")
        == "native_same_parent_view_title_backreference"
    ]
    document = ezdxf.readfile(cad_case["source"])
    title_block = document.blocks.get("NATIVE_SECONDARY_VIEW_TITLE")
    has_cut = any(e.dxftype() == "SOLID" for e in title_block)
    has_wrong_plan = any(e.text == "PL-09" for e in cad_case["indexed"].entities)
    assert len(rows) == int(not has_cut and not has_wrong_plan)
    if rows:
        assert rows[0]["native_semantic_scope"]["local_view_number"] == "09"
        assert rows[0]["native_semantic_scope"]["backreference_page"] == "PL-01"
        assert rows[0]["state"] == "NATIVE_VIEW_TITLE_BACKREFERENCE"
        assert result["context"]["searches"][0]["complete"] is True
    else:
        assert result["context"]["searches"][0]["complete"] is False


@pytest.mark.parametrize("cad_case", ["fastener_duplicate_reference"], indirect=True)
def test_fastener_note_cannot_erase_a_second_identical_reference(cad_case):
    result = _build(cad_case)
    rows = result["receipt"]["components"][0]["reference_inventory"]
    row = next(row for row in rows if row["target_codes"] == ["M-08"])
    assert row["state"] == "UNPROJECTED_NATIVE_REFERENCE_SCOPE_UNRESOLVED"
    assert row["native_semantic_scope"] is None
    assert result["receipt"]["components"][0]["state"] == "REVIEW"
    assert result["context"]["searches"][0]["complete"] is False


@pytest.mark.parametrize("cad_case", ["secondary_title_hidden_scale"], indirect=True)
def test_title_metadata_cannot_use_off_layer_scale_attribute(cad_case):
    result = _build(cad_case)
    rows = result["receipt"]["components"][0]["reference_inventory"]
    assert not any(row["state"] == "NATIVE_VIEW_TITLE_BACKREFERENCE" for row in rows)
    assert any(row["state"] == "UNPROJECTED_NATIVE_REFERENCE_SCOPE_UNRESOLVED" for row in rows)
    assert result["receipt"]["components"][0]["state"] == "REVIEW"
    assert result["context"]["searches"][0]["complete"] is False


@pytest.mark.parametrize("cad_case", ["secondary_title_line_arrow"], indirect=True)
def test_title_metadata_cannot_hide_an_arrow_drawn_with_lines(cad_case):
    result = _build(cad_case)
    rows = result["receipt"]["components"][0]["reference_inventory"]
    assert not any(row["state"] == "NATIVE_VIEW_TITLE_BACKREFERENCE" for row in rows)
    assert any(row["state"] == "UNPROJECTED_NATIVE_REFERENCE_SCOPE_UNRESOLVED" for row in rows)
    assert result["receipt"]["components"][0]["state"] == "REVIEW"
    assert result["context"]["searches"][0]["complete"] is False


@pytest.mark.parametrize("cad_case", ["secondary_title_caption_rule"], indirect=True)
def test_native_circle_diameter_may_extend_as_a_caption_rule(cad_case):
    result = _build(cad_case)
    rows = result["receipt"]["components"][0]["reference_inventory"]
    row = next(row for row in rows if row["state"] == "NATIVE_VIEW_TITLE_BACKREFERENCE")
    geometry = row["native_semantic_scope"]["native_title_geometry"]
    assert geometry["circle_radius"] == 2
    assert geometry["line_end"][0] == 24
    assert geometry["additional_line_or_arrow_primitives"] is False
    assert result["receipt"]["components"][0]["state"] == "VERIFIED"
    assert result["context"]["searches"][0]["complete"] is True


@pytest.mark.parametrize("cad_case", ["outside_callout"], indirect=True)
def test_enriched_parent_injection_cannot_bind_cross_component_reference(cad_case):
    import json

    from cadquote.models import CadEntity, Sheet

    with pytest.raises(ViewContextBuildError, match="no component binding"):
        _build(cad_case)
    payload = json.loads(cad_case["index"].read_text(encoding="utf-8"))
    source = payload["sources"][0]
    anchor_id = cad_case["data"]["components"][0]["views"][0]["binding_entity_ids"][0]
    anchor = next(e for e in source["entities"] if e["id"] == anchor_id)
    assert anchor["entity_type"] == "TEXT" and "parent_insert_handle" not in anchor["geometry"]
    anchor["geometry"]["parent_insert_handle"] = cad_case["source_parent_handle"]
    # Rebuilding panels from the adulterated index must not certify its invented
    # relation; the producer independently rebuilds from the immutable CAD.
    poisoned = expand_viewport_panels(
        [Sheet.model_validate(s) for s in source["sheets"]],
        [CadEntity.model_validate(e) for e in source["entities"]],
        source_names={source["source_file_id"]: cad_case["source"].name},
    )
    _write(cad_case["index"], payload)
    _write(
        cad_case["panels"],
        {
            "sheets": [s.model_dump(mode="json") for s in poisoned.sheets],
            "entities": [e.model_dump(mode="json") for e in poisoned.entities],
        },
    )
    with pytest.raises(ViewContextBuildError, match="source replay"):
        _build(cad_case)


def test_same_page_same_back_reference_still_requires_correct_native_viewport(tmp_path):
    from cadquote.linking import extract_structured_reference_callouts
    from cadquote.view_context_builder import SelectedView, _Scope, _verify_target_title_owner

    doc = ezdxf.new("R2018")
    doc.modelspace().add_text("MT-01", dxfattribs={"insert": (50, 50), "height": 2})
    doc.modelspace().add_text("MT-01", dxfattribs={"insert": (450, 50), "height": 2})
    frame = doc.blocks.new("PAPER_PAGE")
    frame.add_lwpolyline([(-5, -10), (305, -10), (305, 110), (-5, 110)], close=True)
    title = doc.blocks.new("LOCAL_TITLE")
    title.add_circle((0, 0), 1)
    paper = doc.layouts.get("Layout1")
    page = paper.add_blockref("PAPER_PAGE", (0, 0))
    page.add_attrib("SHEET_NUMBER", "SE-01", (70, -7), dxfattribs={"height": 2})
    page.add_attrib("SHEET_TITLE", "SECTION", (30, -7), dxfattribs={"height": 2})
    left = paper.add_viewport(
        center=(150, 50), size=(300, 100), view_center_point=(150, 50), view_height=100
    )
    right = paper.add_viewport(
        center=(250, 50), size=(80, 80), view_center_point=(450, 50), view_height=80
    )
    for x, number in ((40, "01"), (220, "02")):
        local = paper.add_blockref("LOCAL_TITLE", (x, 5))
        local.add_attrib("SHEET_NUMBER", "PL-01", (x, 5), dxfattribs={"height": 2})
        local.add_attrib("#", number, (x, 8), dxfattribs={"height": 2})
        local.add_attrib("TITLE", "SECTION", (x, 1), dxfattribs={"height": 2})
    source = tmp_path / "two-viewports.dxf"
    doc.saveas(source)
    native = index_dxf(source)
    panels = expand_viewport_panels(
        native.sheets, native.entities, source_names={native.source_file_id: source.name}
    )
    sheets = {s.id: s for s in panels.sheets}
    records = {e.id: e for e in native.entities}
    pairs = {r.view_number: r for r in extract_structured_reference_callouts(native.entities)}
    for viewport, box, valid, invalid in (
        (left, (40, 40, 60, 60), "01", "02"),
        (right, (440, 40, 460, 60), "02", "01"),
    ):
        selected = next(s for s in panels.sheets if s.viewport_handle == viewport.dxf.handle)
        scope = _Scope(
            SelectedView(
                id="selected",
                role="section",
                sheet_id=selected.id,
                object_bbox=box,
                evidence_ids=[pairs[valid].entity_ids[0]],
            ),
            sheets,
            {e.id: e for e in panels.entities},
            records,
            {native.source_file_id: ezdxf.readfile(source)},
        )
        _verify_target_title_owner(scope, pairs[valid])
        with pytest.raises(ViewContextBuildError, match="native viewport"):
            _verify_target_title_owner(scope, pairs[invalid])


@pytest.mark.parametrize("output_flag", ["--out", "--receipt"])
@pytest.mark.parametrize("artifact_role", ["original_content", "reviewed_image"])
def test_invalid_graphic_review_still_protects_declared_artifact_inputs(
    cad_case, tmp_path, output_flag, artifact_role
):
    import hashlib

    from cadquote.view_context_builder import main

    protected = tmp_path / "declared-review-input.bin"
    original_bytes = b"SYNTHETIC ORIGINAL REVIEW INPUT: MUST NOT BE OVERWRITTEN"
    protected.write_bytes(original_bytes)
    original_sha = hashlib.sha256(original_bytes).hexdigest()
    other = tmp_path / "other-review-input.bin"
    other.write_bytes(original_bytes)
    assets = {
        role: {"path": str(protected if role == artifact_role else other), "sha256": original_sha}
        for role in ("original_content", "reviewed_image")
    }
    # This definition is deliberately absent from the original CAD. Its review
    # cannot be accepted, but declared inputs must remain protected nonetheless.
    bundle = _write(
        tmp_path / "invalid-graphic-review.json",
        {
            "schema_version": "cad-view-graphic-reviews/1",
            "sources": [
                {
                    "source_file_id": cad_case["indexed"].source_file_id,
                    "review": {
                        "schema_version": "native-graphic-review/1",
                        "source_sha256": hashlib.sha256(
                            cad_case["source"].read_bytes()
                        ).hexdigest(),
                        "review": {
                            "reviewer": "synthetic regression",
                            "reviewed_at": "2026-01-01T00:00:00Z",
                            "reason": "Invalid review does not authorize overwriting its inputs.",
                        },
                        "definitions": [
                            {
                                "handle": "DOES_NOT_EXIST",
                                "owner_block": "FAKE",
                                "payload_bytes": len(original_bytes),
                                "payload_sha256": original_sha,
                                "semantic_classification": "NON_REFERENCE_GRAPHIC",
                                **assets,
                            }
                        ],
                        "instances": [],
                    },
                }
            ],
        },
    )
    context_path = protected if output_flag == "--out" else tmp_path / "result-context.json"
    receipt_path = protected if output_flag == "--receipt" else tmp_path / "result-receipt.json"
    with pytest.raises(ViewContextBuildError, match="output overwrites input/source"):
        main(
            [
                str(cad_case["index"]),
                str(cad_case["panels"]),
                str(cad_case["selection"]),
                "--side",
                "predicted",
                "--graphic-reviews",
                str(bundle),
                "--out",
                str(context_path),
                "--receipt",
                str(receipt_path),
            ]
        )
    assert protected.read_bytes() == original_bytes
    assert other.read_bytes() == original_bytes
    assert not (tmp_path / "result-context.json").exists()
    assert not (tmp_path / "result-receipt.json").exists()
