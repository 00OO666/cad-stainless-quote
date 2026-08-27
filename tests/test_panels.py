from cadquote.models import CadEntity, Sheet
from cadquote.panels import (
    PanelExpansion,
    _inherit_orphan_panel_page_codes,
    choose_analysis_view,
    expand_viewport_panels,
    split_local_drawing_panels,
)


def test_structured_local_title_outvotes_fixed_detail_glyph():
    source_id = "file:title"
    sheets = [
        Sheet(id="model", source_file_id=source_id, layout="Model"),
        Sheet(id="paper", source_file_id=source_id, layout="布局1"),
    ]
    entities = [
        CadEntity(
            id="line",
            source_file_id=source_id,
            sheet_id="model",
            entity_type="LINE",
            space="model",
            bbox=(10, 10, 90, 90),
        ),
        CadEntity(
            id="viewport",
            source_file_id=source_id,
            sheet_id="paper",
            handle="VP",
            entity_type="VIEWPORT",
            space="paper:布局1",
            bbox=(0, 0, 100, 100),
            geometry={"viewport_id": 2, "model_bbox": [0, 0, 100, 100]},
        ),
        CadEntity(
            id="title-block",
            source_file_id=source_id,
            sheet_id="paper",
            handle="TB",
            entity_type="INSERT",
            space="paper:布局1",
            insert=(105, 3),
            bbox=(90, -2, 120, 8),
            geometry={"attribute_handles": ["A1", "A2"]},
        ),
        CadEntity(
            id="generic",
            source_file_id=source_id,
            sheet_id="paper",
            handle="A1",
            entity_type="ATTRIB",
            space="paper:布局1",
            text="DETAIL",
            insert=(105, 5),
            bbox=(101, 4, 111, 6),
            geometry={"tag": "ELEVATION", "parent_insert_handle": "TB"},
        ),
        CadEntity(
            id="actual-title",
            source_file_id=source_id,
            sheet_id="paper",
            handle="A2",
            entity_type="ATTRIB",
            space="paper:布局1",
            text="服务台A正立面图 SCALE:1/10",
            insert=(102, 1),
            bbox=(88, 0, 121, 3),
            geometry={"tag": "立面图SCALE:1/30", "parent_insert_handle": "TB"},
        ),
    ]

    expansion = expand_viewport_panels(
        sheets,
        entities,
        source_names={source_id: "服务台.dwg"},
    )

    assert len(expansion.sheets) == 1
    assert expansion.sheets[0].kind == "elevation"
    assert expansion.sheets[0].title == "服务台A正立面图 SCALE:1/10"


def test_viewport_expansion_assigns_model_entities_to_virtual_sheet():
    source_id = "file:sample"
    sheets = [
        Sheet(
            id="model-sheet",
            source_file_id=source_id,
            kind="unknown",
            layout="Model",
        ),
        Sheet(
            id="paper-sheet",
            source_file_id=source_id,
            kind="unknown",
            layout="布局1",
        ),
    ]
    entities = [
        CadEntity(
            id="model-text",
            source_file_id=source_id,
            sheet_id="model-sheet",
            entity_type="TEXT",
            space="model",
            text="洽谈区立面图 MT-01",
            insert=(50, 50),
            bbox=(40, 45, 80, 55),
        ),
        CadEntity(
            id="viewport",
            source_file_id=source_id,
            sheet_id="paper-sheet",
            handle="AB",
            entity_type="VIEWPORT",
            space="paper:布局1",
            insert=(100, 100),
            bbox=(10, 10, 190, 190),
            geometry={"viewport_id": 2, "model_bbox": [0, 0, 100, 100]},
        ),
    ]

    expansion = expand_viewport_panels(
        sheets,
        entities,
        source_names={source_id: "洽谈区立面图.dwg"},
    )
    assert len(expansion.sheets) == 1
    assert expansion.sheets[0].kind == "elevation"
    assert len(expansion.entities) == 1
    assert expansion.entities[0].geometry["original_entity_id"] == "model-text"
    selected_sheets, selected_entities = choose_analysis_view(sheets, entities, expansion)
    assert selected_sheets == expansion.sheets
    assert selected_entities == expansion.entities


def test_analysis_view_retains_model_entities_outside_all_viewports():
    source_id = "file:sample"
    model_sheet = Sheet(
        id="model-sheet",
        source_file_id=source_id,
        kind="plan",
        layout="Model",
    )
    inside = CadEntity(
        id="inside",
        source_file_id=source_id,
        sheet_id=model_sheet.id,
        entity_type="TEXT",
        space="model",
        text="inside",
        insert=(50, 50),
        bbox=(45, 45, 55, 55),
    )
    outside = CadEntity(
        id="outside",
        source_file_id=source_id,
        sheet_id=model_sheet.id,
        entity_type="TEXT",
        space="model",
        text="古铜色不锈钢",
        insert=(500, 500),
        bbox=(490, 490, 520, 510),
    )
    viewport = CadEntity(
        id="viewport",
        source_file_id=source_id,
        sheet_id="paper",
        handle="AB",
        entity_type="VIEWPORT",
        space="paper:布局1",
        bbox=(0, 0, 100, 100),
        geometry={"viewport_id": 2, "model_bbox": [0, 0, 100, 100]},
    )
    expansion = expand_viewport_panels(
        [
            model_sheet,
            Sheet(
                id="paper",
                source_file_id=source_id,
                kind="unknown",
                layout="布局1",
            ),
        ],
        [inside, outside, viewport],
    )

    selected_sheets, selected_entities = choose_analysis_view(
        [model_sheet],
        [inside, outside],
        expansion,
    )

    assert any(
        entity.geometry.get("original_entity_id") == "inside"
        for entity in selected_entities
    )
    assert any(entity.id == "outside" for entity in selected_entities)
    assert model_sheet in selected_sheets


def test_paper_annotations_are_projected_into_their_model_panel():
    source_id = "file:sample"
    model_sheet = Sheet(
        id="model",
        source_file_id=source_id,
        kind="unknown",
        layout="Model",
    )
    paper_sheet = Sheet(
        id="paper",
        source_file_id=source_id,
        kind="unknown",
        layout="布局1",
    )
    model_line = CadEntity(
        id="model-line",
        source_file_id=source_id,
        sheet_id=model_sheet.id,
        entity_type="LINE",
        space="model",
        insert=(1_200, 2_300),
        bbox=(1_100, 2_200, 1_300, 2_400),
    )
    mt_tag = CadEntity(
        id="paper-mt",
        source_file_id=source_id,
        sheet_id=paper_sheet.id,
        entity_type="ATTRIB",
        space="paper:布局1",
        text="MT-01",
        insert=(20, 30),
        bbox=(18, 29, 25, 31),
        geometry={"height": 2.5, "tag": "MT"},
    )
    leader = CadEntity(
        id="paper-leader",
        source_file_id=source_id,
        sheet_id=paper_sheet.id,
        entity_type="LEADER",
        space="paper:布局1",
        insert=(20, 30),
        bbox=(20, 30, 40, 50),
        geometry={"vertices": [[20, 30, 0], [40, 50, 0]]},
    )
    default_viewport = CadEntity(
        id="default-viewport",
        source_file_id=source_id,
        sheet_id=paper_sheet.id,
        entity_type="VIEWPORT",
        space="paper:布局1",
        bbox=(0, 0, 500, 500),
        geometry={"viewport_id": 1, "model_bbox": [0, 0, 500, 500]},
    )
    viewport = CadEntity(
        id="viewport",
        source_file_id=source_id,
        sheet_id=paper_sheet.id,
        handle="AB",
        entity_type="VIEWPORT",
        space="paper:布局1",
        bbox=(0, 0, 100, 100),
        geometry={
            "viewport_id": 2,
            "model_bbox": [1_000, 2_000, 2_000, 3_000],
            # Some exporters retain a non-unit view direction. Direction, not
            # vector magnitude, determines whether this is a safe 2D mapping.
            "view_direction_vector": [0, 0, 103251.5],
            "view_twist_angle": 0,
        },
    )

    expansion = expand_viewport_panels(
        [model_sheet, paper_sheet],
        [model_line, mt_tag, leader, default_viewport, viewport],
        source_names={source_id: "立面图.dwg"},
    )

    projected = next(
        entity
        for entity in expansion.entities
        if entity.geometry.get("original_entity_id") == mt_tag.id
    )
    projected_leader = next(
        entity
        for entity in expansion.entities
        if entity.geometry.get("original_entity_id") == leader.id
    )
    assert projected.insert == (1_200.0, 2_300.0)
    assert projected.bbox == (1_180.0, 2_290.0, 1_250.0, 2_310.0)
    assert projected.geometry["height"] == 25.0
    assert projected.geometry["original_paper_insert"] == [20.0, 30.0]
    assert projected_leader.geometry["vertices"] == [
        [1_200.0, 2_300.0, 0.0],
        [1_400.0, 2_500.0, 0.0],
    ]
    assert projected.space == next(
        entity
        for entity in expansion.entities
        if entity.geometry.get("original_entity_id") == model_line.id
    ).space

    selected_sheets, selected_entities = choose_analysis_view(
        [model_sheet, paper_sheet],
        [model_line, mt_tag, leader, default_viewport, viewport],
        expansion,
    )
    assert selected_sheets == expansion.sheets
    assert not any(entity.id in {mt_tag.id, leader.id} for entity in selected_entities)


def test_rotated_viewport_does_not_project_paper_annotations():
    source_id = "file:sample"
    model_sheet = Sheet(id="model", source_file_id=source_id, layout="Model")
    paper_sheet = Sheet(id="paper", source_file_id=source_id, layout="布局1")
    model_entity = CadEntity(
        id="model-entity",
        source_file_id=source_id,
        sheet_id=model_sheet.id,
        entity_type="LINE",
        space="model",
        bbox=(0, 0, 100, 100),
    )
    annotation = CadEntity(
        id="annotation",
        source_file_id=source_id,
        sheet_id=paper_sheet.id,
        entity_type="TEXT",
        space="paper:布局1",
        text="MT-01",
        insert=(50, 50),
    )
    viewport = CadEntity(
        id="viewport",
        source_file_id=source_id,
        sheet_id=paper_sheet.id,
        entity_type="VIEWPORT",
        space="paper:布局1",
        bbox=(0, 0, 100, 100),
        geometry={
            "viewport_id": 2,
            "model_bbox": [0, 0, 100, 100],
            "view_twist_angle": 0.25,
        },
    )

    expansion = expand_viewport_panels(
        [model_sheet, paper_sheet],
        [model_entity, annotation, viewport],
    )
    assert not any(
        entity.geometry.get("original_entity_id") == annotation.id
        for entity in expansion.entities
    )
    assert any("unsupported paper-to-model transform" in warning for warning in expansion.warnings)


def test_viewport_inherits_nearest_title_block_page_number():
    source_id = "file:sample"
    model_sheet = Sheet(id="model", source_file_id=source_id, layout="Model")
    paper_sheet = Sheet(id="paper", source_file_id=source_id, layout="布局1")
    model_entity = CadEntity(
        id="model-entity",
        source_file_id=source_id,
        sheet_id=model_sheet.id,
        entity_type="LINE",
        space="model",
        bbox=(0, 0, 100, 100),
    )
    viewport = CadEntity(
        id="viewport",
        source_file_id=source_id,
        sheet_id=paper_sheet.id,
        entity_type="VIEWPORT",
        space="paper:布局1",
        bbox=(0, 0, 100, 100),
        geometry={"viewport_id": 2, "model_bbox": [0, 0, 100, 100]},
    )
    title_block = CadEntity(
        id="title-block",
        source_file_id=source_id,
        sheet_id=paper_sheet.id,
        handle="TB",
        entity_type="INSERT",
        space="paper:布局1",
        insert=(120, 0),
        geometry={
            "name": "A2",
            "attribute_handles": ["PAGE", "TITLE"],
        },
    )
    page_number = CadEntity(
        id="page-number",
        source_file_id=source_id,
        sheet_id=paper_sheet.id,
        handle="PAGE",
        entity_type="ATTRIB",
        space="paper:布局1",
        text="1F-E-03",
        insert=(105, 5),
        geometry={"tag": "SHEET_NO", "parent_insert_handle": "TB"},
    )
    page_title = CadEntity(
        id="page-title",
        source_file_id=source_id,
        sheet_id=paper_sheet.id,
        handle="TITLE",
        entity_type="ATTRIB",
        space="paper:布局1",
        text="大厅立面图",
        insert=(105, 10),
        geometry={"tag": "SHEET_TITLE", "parent_insert_handle": "TB"},
    )
    callout = CadEntity(
        id="callout",
        source_file_id=source_id,
        sheet_id=paper_sheet.id,
        handle="CO",
        entity_type="INSERT",
        space="paper:布局1",
        insert=(50, 50),
        geometry={
            "name": "图号",
            "attribute_handles": ["REF", "REF_TITLE"],
        },
    )
    callout_number = CadEntity(
        id="callout-number",
        source_file_id=source_id,
        sheet_id=paper_sheet.id,
        handle="REF",
        entity_type="ATTRIB",
        space="paper:布局1",
        text="1F-QS-02",
        insert=(50, 50),
        geometry={"parent_insert_handle": "CO"},
    )
    callout_title = CadEntity(
        id="callout-title",
        source_file_id=source_id,
        sheet_id=paper_sheet.id,
        handle="REF_TITLE",
        entity_type="ATTRIB",
        space="paper:布局1",
        text="立面图 SCALE:1/40",
        insert=(55, 50),
        geometry={"parent_insert_handle": "CO"},
    )

    expansion = expand_viewport_panels(
        [model_sheet, paper_sheet],
        [
            model_entity,
            viewport,
            title_block,
            page_number,
            page_title,
            callout,
            callout_number,
            callout_title,
        ],
        source_names={source_id: "立面图.dwg"},
    )

    assert expansion.sheets[0].drawing_number == "1F-E-03"
    assert any(
        value.startswith("paper_page_reference:1F-E-03@")
        for value in expansion.sheets[0].evidence
    )


def test_leader_and_outside_attribute_move_to_panel_atomically():
    source_id = "file:sample"
    model_sheet = Sheet(id="model", source_file_id=source_id, layout="Model")
    paper_sheet = Sheet(id="paper", source_file_id=source_id, layout="布局1")
    model_entity = CadEntity(
        id="model-entity",
        source_file_id=source_id,
        sheet_id=model_sheet.id,
        entity_type="LINE",
        space="model",
        bbox=(0, 0, 100, 100),
    )
    viewport = CadEntity(
        id="viewport",
        source_file_id=source_id,
        sheet_id=paper_sheet.id,
        entity_type="VIEWPORT",
        space="paper:布局1",
        bbox=(0, 0, 100, 100),
        geometry={"viewport_id": 2, "model_bbox": [0, 0, 1000, 1000]},
    )
    annotation_insert = CadEntity(
        id="annotation-insert",
        source_file_id=source_id,
        sheet_id=paper_sheet.id,
        handle="ANN",
        entity_type="INSERT",
        space="paper:布局1",
        insert=(115, 50),
        geometry={"name": "材料标注", "attribute_handles": ["MT"]},
    )
    mt_attribute = CadEntity(
        id="mt-attribute",
        source_file_id=source_id,
        sheet_id=paper_sheet.id,
        handle="MT",
        entity_type="ATTRIB",
        space="paper:布局1",
        text="MT-01",
        insert=(115, 50),
        bbox=(113, 49, 119, 51),
        geometry={"tag": "MT", "parent_insert_handle": "ANN", "height": 2.5},
    )
    leader = CadEntity(
        id="leader",
        source_file_id=source_id,
        sheet_id=paper_sheet.id,
        entity_type="LEADER",
        space="paper:布局1",
        insert=(50, 50),
        bbox=(50, 50, 115, 50.1),
        geometry={"vertices": [[50, 50, 0], [115, 50, 0]]},
    )

    expansion = expand_viewport_panels(
        [model_sheet, paper_sheet],
        [model_entity, viewport, annotation_insert, mt_attribute, leader],
    )

    represented = {
        entity.geometry.get("original_entity_id") for entity in expansion.entities
    }
    assert {annotation_insert.id, mt_attribute.id, leader.id} <= represented
    projected_mt = next(
        entity
        for entity in expansion.entities
        if entity.geometry.get("original_entity_id") == mt_attribute.id
    )
    assert projected_mt.insert == (1_150.0, 500.0)


def test_title_block_direction_beats_closer_neighboring_page_code():
    source_id = "file:sample"
    model_sheet = Sheet(id="model", source_file_id=source_id, layout="Model")
    paper_sheet = Sheet(id="paper", source_file_id=source_id, layout="布局1")
    model_entity = CadEntity(
        id="model-entity",
        source_file_id=source_id,
        sheet_id=model_sheet.id,
        entity_type="LINE",
        space="model",
        bbox=(0, 0, 100, 100),
    )
    viewport = CadEntity(
        id="viewport",
        source_file_id=source_id,
        sheet_id=paper_sheet.id,
        entity_type="VIEWPORT",
        space="paper:布局1",
        bbox=(100, 200, 300, 400),
        geometry={"viewport_id": 2, "model_bbox": [0, 0, 100, 100]},
    )

    def title_block(prefix: str, code: str, point: tuple[float, float]) -> list[CadEntity]:
        return [
            CadEntity(
                id=f"{prefix}-block",
                source_file_id=source_id,
                sheet_id=paper_sheet.id,
                handle=f"{prefix}-BLOCK",
                entity_type="INSERT",
                space="paper:布局1",
                geometry={
                    "name": "A2",
                    "attribute_handles": [f"{prefix}-PAGE", f"{prefix}-TITLE"],
                },
            ),
            CadEntity(
                id=f"{prefix}-page",
                source_file_id=source_id,
                sheet_id=paper_sheet.id,
                handle=f"{prefix}-PAGE",
                entity_type="ATTRIB",
                space="paper:布局1",
                text=code,
                insert=point,
                geometry={"parent_insert_handle": f"{prefix}-BLOCK"},
            ),
            CadEntity(
                id=f"{prefix}-title",
                source_file_id=source_id,
                sheet_id=paper_sheet.id,
                handle=f"{prefix}-TITLE",
                entity_type="ATTRIB",
                space="paper:布局1",
                text="大厅立面图",
                insert=(point[0], point[1] + 5),
                geometry={"parent_insert_handle": f"{prefix}-BLOCK"},
            ),
        ]

    # The wrong page is slightly closer to the viewport center but is above
    # and left. The actual page code is in the conventional lower-right title
    # block position.
    wrong = title_block("wrong", "1F-E-01", (150, 410))
    correct = title_block("correct", "1F-E-06", (315, 190))
    expansion = expand_viewport_panels(
        [model_sheet, paper_sheet],
        [model_entity, viewport, *wrong, *correct],
        source_names={source_id: "立面图.dwg"},
    )
    assert expansion.sheets[0].drawing_number == "1F-E-06"


def test_oversized_elevation_panel_splits_on_repeated_local_sheet_titles() -> None:
    panel = Sheet(
        id="panel:wide",
        source_file_id="file:1",
        drawing_number="2F-E-21",
        title="一层立面图",
        kind="elevation",
        layout="立面#viewport:ABC",
        viewport_handle="ABC",
        bbox=(0.0, 0.0, 300.0, 100.0),
        confidence=0.82,
    )
    entities = [
        CadEntity(
            id="title:2",
            source_file_id="file:1",
            sheet_id=panel.id,
            entity_type="TEXT",
            space="model@立面#ABC",
            text="一层立面图",
            insert=(178.0, 8.0),
        ),
        CadEntity(
            id="code:2",
            source_file_id="file:1",
            sheet_id=panel.id,
            entity_type="TEXT",
            space="model@立面#ABC",
            text="2F-E-21",
            insert=(180.0, 4.0),
        ),
        CadEntity(
            id="title:3",
            source_file_id="file:1",
            sheet_id=panel.id,
            entity_type="TEXT",
            space="model@立面#ABC",
            text="一层立面图",
            insert=(278.0, 8.0),
        ),
        CadEntity(
            id="code:3",
            source_file_id="file:1",
            sheet_id=panel.id,
            entity_type="TEXT",
            space="model@立面#ABC",
            text="2F-E-22",
            insert=(280.0, 4.0),
        ),
        CadEntity(
            id="mt:left",
            source_file_id="file:1",
            sheet_id=panel.id,
            entity_type="TEXT",
            space="model@立面#ABC",
            text="MT-01",
            insert=(45.0, 55.0),
        ),
        CadEntity(
            id="mt:middle",
            source_file_id="file:1",
            sheet_id=panel.id,
            entity_type="TEXT",
            space="model@立面#ABC",
            text="MT-02",
            insert=(190.0, 55.0),
        ),
        CadEntity(
            id="mt:right",
            source_file_id="file:1",
            sheet_id=panel.id,
            entity_type="TEXT",
            space="model@立面#ABC",
            text="MT-03",
            insert=(285.0, 55.0),
        ),
    ]
    expansion = PanelExpansion(
        sheets=[panel],
        entities=entities,
        source_panel_counts={"file:1": 1},
    )

    split_local_drawing_panels(expansion)

    assert [sheet.drawing_number for sheet in expansion.sheets] == [
        "2F-E-21",
        "2F-E-22",
    ]
    sheet_by_id = {sheet.id: sheet for sheet in expansion.sheets}
    mt_pages = {
        entity.text: sheet_by_id[entity.sheet_id].drawing_number
        for entity in expansion.entities
        if entity.text and entity.text.startswith("MT-")
    }
    assert mt_pages == {
        "MT-01": "2F-E-21",
        "MT-02": "2F-E-21",
        "MT-03": "2F-E-22",
    }
    assert len(expansion.entities) == len(entities)
    assert expansion.source_panel_counts == {"file:1": 2}


def test_local_split_keeps_uncoded_leader_annotation_with_arrow_target_page() -> None:
    panel = Sheet(
        id="panel:wide",
        source_file_id="file:1",
        drawing_number="2F-E-21",
        title="一层立面图",
        kind="elevation",
        layout="立面#viewport:ABC",
        viewport_handle="ABC",
        bbox=(0.0, 0.0, 300.0, 100.0),
    )
    entities = [
        CadEntity(
            id="title:2",
            source_file_id="file:1",
            sheet_id=panel.id,
            entity_type="TEXT",
            space="model@立面#ABC",
            text="一层立面图",
            insert=(178.0, 8.0),
        ),
        CadEntity(
            id="code:2",
            source_file_id="file:1",
            sheet_id=panel.id,
            entity_type="TEXT",
            space="model@立面#ABC",
            text="2F-E-21",
            insert=(180.0, 4.0),
        ),
        CadEntity(
            id="title:3",
            source_file_id="file:1",
            sheet_id=panel.id,
            entity_type="TEXT",
            space="model@立面#ABC",
            text="一层立面图",
            insert=(278.0, 8.0),
        ),
        CadEntity(
            id="code:3",
            source_file_id="file:1",
            sheet_id=panel.id,
            entity_type="TEXT",
            space="model@立面#ABC",
            text="2F-E-22",
            insert=(280.0, 4.0),
        ),
        CadEntity(
            id="rail-label",
            source_file_id="file:1",
            sheet_id=panel.id,
            entity_type="TEXT",
            space="model@立面#ABC",
            text="金属玻璃栏板",
            insert=(220.0, 55.0),
            geometry={"height": 5.0},
        ),
        CadEntity(
            id="rail-leader",
            source_file_id="file:1",
            sheet_id=panel.id,
            entity_type="LEADER",
            space="model@立面#ABC",
            insert=(250.0, 55.0),
            geometry={"vertices": [[250.0, 55.0], [220.0, 55.0]]},
        ),
    ]
    expansion = PanelExpansion(sheets=[panel], entities=entities)

    split_local_drawing_panels(expansion)

    sheet_by_id = {sheet.id: sheet for sheet in expansion.sheets}
    assigned_pages = {
        entity.geometry.get("parent_panel_entity_id"): sheet_by_id[
            entity.sheet_id
        ].drawing_number
        for entity in expansion.entities
        if entity.geometry.get("parent_panel_entity_id")
        in {"rail-label", "rail-leader"}
    }
    assert assigned_pages == {
        "rail-label": "2F-E-22",
        "rail-leader": "2F-E-22",
    }


def test_regular_single_elevation_panel_is_not_split() -> None:
    panel = Sheet(
        id="panel:single",
        source_file_id="file:1",
        drawing_number="1F-EL-01",
        title="一层立面图",
        kind="elevation",
        bbox=(0.0, 0.0, 100.0, 100.0),
    )
    entities = [
        CadEntity(
            id="title:1",
            source_file_id="file:1",
            sheet_id=panel.id,
            entity_type="TEXT",
            space="model",
            text="一层立面图",
            insert=(50.0, 8.0),
        ),
        CadEntity(
            id="code:1",
            source_file_id="file:1",
            sheet_id=panel.id,
            entity_type="TEXT",
            space="model",
            text="1F-EL-01",
            insert=(52.0, 4.0),
        ),
    ]
    expansion = PanelExpansion(sheets=[panel], entities=entities)

    split_local_drawing_panels(expansion)

    assert expansion.sheets == [panel]
    assert expansion.entities == entities


def test_small_unnumbered_detail_viewport_inherits_explicit_local_page_band() -> None:
    page = Sheet(
        id="subview:23",
        source_file_id="file:1",
        drawing_number="L1-EL-05",
        title="一层立面图",
        kind="elevation",
        layout="立面#viewport:WIDE#subview:L1-EL-05",
        bbox=(370_000.0, -78_000.0, 391_000.0, -69_000.0),
        confidence=0.92,
        evidence=["local_subview_parent:panel:wide"],
    )
    detail = Sheet(
        id="panel:detail",
        source_file_id="file:1",
        title="造型剖面图",
        kind="elevation",
        layout="立面#viewport:SMALL",
        bbox=(386_700.0, -81_700.0, 387_200.0, -80_600.0),
        confidence=0.76,
    )

    result = _inherit_orphan_panel_page_codes([page, detail])

    inherited = result[1]
    assert inherited.drawing_number == "L1-EL-05"
    assert inherited.confidence == 0.72
    assert any(
        value.startswith("inherited_local_page_code:L1-EL-05@")
        for value in inherited.evidence
    )
