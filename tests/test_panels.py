from cadquote.models import CadEntity, Sheet
from cadquote.panels import choose_analysis_view, expand_viewport_panels


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
