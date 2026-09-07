from copy import deepcopy

import pytest
from cadquote.detail_routes import annotate_detail_route_edges, build_detail_routes
from cadquote.models import CadEntity, EvidenceEdge, ReviewStatus, Sheet


def ent(
    key,
    *,
    source="cad",
    sheet="paper",
    kind="ATTRIB",
    text=None,
    point=(20, -10),
    parent="TITLE",
    space="paper:Layout1",
    **kw,
):
    geometry = kw.pop("geometry", {})
    if parent:
        geometry["parent_insert_handle"] = parent
    return CadEntity(
        id=key,
        source_file_id=source,
        sheet_id=sheet,
        handle=key,
        entity_type=kind,
        text=text,
        insert=point,
        space=space,
        geometry=geometry,
        **kw,
    )


def fixture():
    sheets = [
        Sheet(id="elev", source_file_id="cad", drawing_number="B3-E-08", kind="elevation"),
        Sheet(
            id="node",
            source_file_id="cad",
            drawing_number="B3-QS-09",
            kind="detail",
            layout="Layout1#viewport:VP",
            viewport_handle="VP",
            bbox=(1000, 2000, 1100, 2100),
        ),
    ]
    entities = [
        ent("callout-code", sheet="elev", text="B3-QS-09", parent="CALL"),
        ent("callout-num", sheet="elev", text="07", parent="CALL"),
        ent("steel", sheet="node", text="GC-SS-907", parent="STEEL"),
        ent("stone", sheet="node", text="GC-ST-901", parent="STONE"),
    ]
    native = [
        ent("VP", kind="VIEWPORT", parent=None, bbox=(0, 0, 100, 100)),
        ent("title-code", text="B3-E-08"),
        ent("title-number", text="07"),
        ent("title-text", text="墙面节点图 SCALE:1:12"),
    ]
    return sheets, entities, native


def test_recovers_title_outside_viewport_without_reclassifying_materials_or_pass():
    args = fixture()
    before = deepcopy(args)
    out = build_detail_routes(*args)
    r = out["records"][0]
    assert r["navigation_state"] == "UNIQUE_RECIPROCAL_CANDIDATE"
    assert r["suggested_detail_sheet_id"] == "node"
    assert r["state"] == "REVIEW" and r["physical_quantity"] is None
    assert r["physical_component_confirmed"] is False
    materials = r["candidates"][0]["material_candidates"]
    assert {m["parent_insert_handle"] for m in materials} == {"STEEL", "STONE"}
    assert args == before


@pytest.mark.parametrize(
    "mode",
    [
        "wrong_number",
        "wrong_backref",
        "hidden",
        "no_title",
        "other_parent",
        "other_layout",
        "far_title",
        "inside_view",
    ],
)
def test_missing_or_conflicting_evidence_cannot_select(mode):
    sheets, entities, native = fixture()
    if mode == "wrong_number":
        native[2].text = "08"
    elif mode == "wrong_backref":
        native[1].text = "B3-E-09"
    elif mode == "hidden":
        native[1].geometry["semantic_hidden"] = True
    elif mode == "no_title":
        native[3].text = "ordinary note"
    elif mode == "other_parent":
        native[2].geometry["parent_insert_handle"] = "OTHER"
    elif mode == "other_layout":
        native[0].space = "paper:Layout2"
    elif mode == "far_title":
        native[1].insert = native[2].insert = (20, -500)
    elif mode == "inside_view":
        native[1].insert = native[2].insert = (20, 20)
    r = build_detail_routes(sheets, entities, native)["records"][0]
    assert r["navigation_state"] == "PAGE_ONLY"
    assert r["suggested_detail_sheet_id"] is None


def test_duplicate_viewports_are_ambiguous_not_first_match():
    sheets, entities, native = fixture()
    sheets.append(sheets[1].model_copy(update={"id": "node2", "viewport_handle": "VP2"}))
    native.append(native[0].model_copy(update={"id": "VP2", "handle": "VP2"}))
    r = build_detail_routes(sheets, entities, native)["records"][0]
    assert r["navigation_state"] == "AMBIGUOUS_RECIPROCAL"
    assert r["reciprocal_candidate_count"] == 2
    assert r["suggested_detail_sheet_id"] is None


def test_unrelated_source_with_same_handle_cannot_supply_title():
    sheets, entities, native = fixture()
    for e in native:
        e.source_file_id = "another-cad"
    assert (
        build_detail_routes(sheets, entities, native)["records"][0]["navigation_state"]
        == "PAGE_ONLY"
    )


def test_conflicting_titles_are_not_resolved_by_order():
    sheets, entities, native = fixture()
    native.extend(
        [
            ent("conflict-code", text="B3-E-09", parent="CONFLICT"),
            ent("conflict-num", text="07", parent="CONFLICT"),
            ent("conflict-text", text="DETAIL", parent="CONFLICT"),
        ]
    )
    assert (
        build_detail_routes(sheets, entities, native)["records"][0]["navigation_state"]
        == "PAGE_ONLY"
    )


def test_missing_target_and_hidden_source_remain_explicit():
    sheets, entities, native = fixture()
    assert (
        build_detail_routes(sheets[:1], entities, native)["records"][0]["navigation_state"]
        == "TARGET_NOT_FOUND"
    )
    entities[0].geometry["semantic_hidden"] = True
    assert build_detail_routes(sheets, entities, native)["records"] == []


def test_reordered_input_is_stable():
    args = fixture()
    assert build_detail_routes(*args) == build_detail_routes(*(list(reversed(v)) for v in args))


def test_graph_integration_never_promotes_candidate():
    route = build_detail_routes(*fixture())
    edge = EvidenceEdge(
        id="old",
        relation="elevation_to_detail",
        source_id="elev",
        target_id="node",
        basis=["explicit_reference"],
        confidence=0.8,
        status=ReviewStatus.REVIEW,
    )
    out = annotate_detail_route_edges([edge], route)[0]
    assert out.status == ReviewStatus.REVIEW
    assert out.confidence == edge.confidence and edge.basis == ["explicit_reference"]
    assert any(b.startswith("numbered_detail_navigation_candidate:") for b in out.basis)
    assert out.id != edge.id


def test_native_frame_recovers_wrong_panel_classification_without_mutating_sheet():
    sheets, entities, native = fixture()
    sheets[1].drawing_number = 'B3-E-08'
    sheets[1].kind = 'elevation'
    native.extend([
        ent('FRAME', kind='INSERT', parent=None, bbox=(-10,-20,110,110)),
        ent('frame-page', text='B3-QS-09', parent='FRAME'),
    ])
    result = build_detail_routes(sheets, entities, native)
    r = result['records'][0]
    assert r['suggested_detail_sheet_id'] == 'node'
    assert r['candidates'][0]['page_recovery']['code_entity_ids'] == ['frame-page']
    assert sheets[1].drawing_number == 'B3-E-08' and sheets[1].kind == 'elevation'


def test_full_run_writes_navigation_snapshot_and_resume_does_not_recompute(tmp_path, monkeypatch):
    import ezdxf
    from cadquote.pipeline import resume_pipeline, run_pipeline

    source = tmp_path / 'simple.dxf'
    doc = ezdxf.new()
    doc.modelspace().add_text('TEST')
    doc.saveas(source)
    out = tmp_path / 'run'
    result = run_pipeline(source, out, render_evidence=False)
    path = out / 'analysis/detail_routes.json'
    before = path.read_bytes()
    assert result.paths['detail_routes'] == str(path)
    def forbidden(*args, **kwargs):
        raise AssertionError('resume must not replace routing snapshot')
    monkeypatch.setattr('cadquote.pipeline.build_detail_routes', forbidden)
    resumed = resume_pipeline(out, render_evidence=False)
    assert resumed.paths['detail_routes'] == str(path) and path.read_bytes() == before


def test_cli_is_source_preserving_and_uses_no_workbook(tmp_path):
    import json

    from cad_quote import build_parser

    sheets, entities, native = fixture()
    index, panels, output = [tmp_path / n for n in ["index.json", "panels.json", "routes.json"]]
    index.write_text(
        json.dumps(
            {"sources": [{"sheets": [], "entities": [e.model_dump(mode="json") for e in native]}]}
        )
    )
    panels.write_text(
        json.dumps(
            {
                "sheets": [s.model_dump(mode="json") for s in sheets],
                "entities": [e.model_dump(mode="json") for e in entities],
                "source_panel_counts": {"cad": 2},
                "warnings": [],
            }
        )
    )
    before = [index.read_bytes(), panels.read_bytes()]
    args = build_parser().parse_args(
        ["detail-routes", str(index), "--panels", str(panels), "--out", str(output)]
    )
    assert args.handler(args) == 0
    assert json.loads(output.read_text())["records"][0]["suggested_detail_sheet_id"] == "node"
    assert before == [index.read_bytes(), panels.read_bytes()]
    args.out = index
    with pytest.raises(ValueError, match="separate JSON"):
        args.handler(args)
