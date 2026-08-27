import json
from pathlib import Path

import ezdxf
import pytest
from cadquote.evidence_images import (
    build_excel_evidence_targets,
    render_excel_evidence,
)
from cadquote.models import (
    CadEntity,
    ComponentInstance,
    EvidenceEdge,
    MeasurementCandidate,
    MtOccurrence,
    ReviewStatus,
    Sheet,
    TakeoffItem,
)


def _item(sequence: int, component_id: str | None, mt_code: str = "MT-01") -> TakeoffItem:
    return TakeoffItem(
        sequence=sequence,
        name="不锈钢构件",
        mt_code=mt_code,
        component_id=component_id,
        status=ReviewStatus.REVIEW,
    )


def _entity(
    identifier: str,
    sheet_id: str,
    handle: str,
    bbox: tuple[float, float, float, float],
) -> CadEntity:
    return CadEntity(
        id=identifier,
        source_file_id="file:cad",
        sheet_id=sheet_id,
        handle=handle,
        entity_type="LINE",
        space="Model",
        bbox=bbox,
        geometry={"start": [bbox[0], bbox[1]], "end": [bbox[2], bbox[3]]},
    )


def _strictly_contains(
    outer: tuple[float, float, float, float],
    inner: tuple[float, float, float, float],
) -> bool:
    return (
        outer[0] < inner[0]
        and outer[1] < inner[1]
        and outer[2] > inner[2]
        and outer[3] > inner[3]
    )


def test_targets_bind_by_component_id_and_never_cross_same_mt_components():
    sheets = [
        Sheet(
            id="sheet:plan:1",
            source_file_id="file:cad",
            drawing_number="P-01",
            kind="plan",
            layout="Model",
            bbox=(0, 0, 1_000, 1_000),
        ),
        Sheet(
            id="sheet:detail:1",
            source_file_id="file:cad",
            drawing_number="D-01",
            kind="detail",
            layout="Model",
            bbox=(0, 0, 1_000, 1_000),
        ),
        Sheet(
            id="sheet:plan:2",
            source_file_id="file:cad",
            drawing_number="P-02",
            kind="plan",
            layout="Model",
            bbox=(0, 0, 1_000, 1_000),
        ),
        Sheet(
            id="sheet:detail:2",
            source_file_id="file:cad",
            drawing_number="D-02",
            kind="detail",
            layout="Model",
            bbox=(0, 0, 1_000, 1_000),
        ),
    ]
    entities = [
        _entity("entity:plan:1", "sheet:plan:1", "A1", (90, 90, 110, 110)),
        _entity("entity:detail:1", "sheet:detail:1", "A2", (190, 190, 230, 230)),
        _entity("entity:plan:2", "sheet:plan:2", "B1", (790, 790, 810, 810)),
        _entity("entity:detail:2", "sheet:detail:2", "B2", (690, 690, 730, 730)),
    ]
    occurrences = [
        MtOccurrence(
            id="occurrence:1",
            mt_code="MT-01",
            source_file_id="file:cad",
            sheet_id="sheet:plan:1",
            entity_ids=["entity:plan:1"],
            leader_target=(100, 100),
        ),
        MtOccurrence(
            id="occurrence:2",
            mt_code="MT-01",
            source_file_id="file:cad",
            sheet_id="sheet:plan:2",
            entity_ids=["entity:plan:2"],
            leader_target=(800, 800),
        ),
    ]
    components = [
        ComponentInstance(
            id="component:1",
            mt_code="MT-01",
            plan_occurrence_ids=["occurrence:1"],
            detail_sheet_ids=["sheet:detail:1"],
        ),
        ComponentInstance(
            id="component:2",
            mt_code="MT-01",
            plan_occurrence_ids=["occurrence:2"],
            detail_sheet_ids=["sheet:detail:2"],
        ),
    ]
    measurements = [
        MeasurementCandidate(
            id="measurement:1",
            component_id="component:1",
            role="length",
            raw_value="4200",
            numeric_value=4_200,
            unit="mm",
            source_file_id="file:cad",
            sheet_id="sheet:detail:1",
            entity_ids=["entity:detail:1"],
            confidence=0.95,
        ),
        MeasurementCandidate(
            id="measurement:2",
            component_id="component:2",
            role="length",
            raw_value="8800",
            numeric_value=8_800,
            unit="mm",
            source_file_id="file:cad",
            sheet_id="sheet:detail:2",
            entity_ids=["entity:detail:2"],
            confidence=0.99,
        ),
    ]
    edges = [
        EvidenceEdge(
            id="edge:measurement:1",
            relation="component_to_dimension",
            source_id="component:1",
            target_id="measurement:1",
            status=ReviewStatus.PASS,
        ),
        EvidenceEdge(
            id="edge:measurement:2",
            relation="component_to_dimension",
            source_id="component:2",
            target_id="measurement:2",
            status=ReviewStatus.PASS,
        ),
    ]

    targets = build_excel_evidence_targets(
        [_item(1, "component:1"), _item(2, "component:2")],
        components,
        occurrences,
        measurements,
        edges,
        sheets,
        entities,
    )

    component_one = [value for value in targets if value.component_id == "component:1"]
    component_two = [value for value in targets if value.component_id == "component:2"]
    assert {value.stage for value in component_one} == {"plan", "elevation", "detail"}
    assert {value.stage for value in component_two} == {"plan", "elevation", "detail"}
    assert {handle for value in component_one for handle in value.entity_handles} == {"A1", "A2"}
    assert {handle for value in component_two for handle in value.entity_handles} == {"B1", "B2"}
    assert "measurement:2" not in {
        identifier for value in component_one for identifier in value.measurement_ids
    }
    for target in [value for value in targets if value.stage != "elevation"]:
        assert target.state == "READY"
        assert target.evidence_state == "CANDIDATE"
        assert target.context_bbox is not None
        assert target.detail_bbox is not None
        assert _strictly_contains(target.context_bbox, target.detail_bbox)
    elevation_targets = [value for value in targets if value.stage == "elevation"]
    assert all(value.state == "MISSING" for value in elevation_targets)
    assert all(value.evidence_state == "MISSING" for value in elevation_targets)
    assert all("未关联立面" in (value.reason or "") for value in elevation_targets)
    json.dumps([value.model_dump(mode="json") for value in targets], ensure_ascii=False)


def test_every_item_keeps_a_missing_target_when_binding_or_anchor_is_absent():
    sheet = Sheet(
        id="sheet:plan",
        source_file_id="file:cad",
        kind="plan",
        layout="Model",
        bbox=(0, 0, 1_000, 1_000),
    )
    occurrence = MtOccurrence(
        id="occurrence:no-anchor",
        mt_code="MT-01",
        source_file_id="file:cad",
        sheet_id=sheet.id,
        entity_ids=["entity:no-anchor"],
    )
    component = ComponentInstance(
        id="component:no-anchor",
        mt_code="MT-01",
        plan_occurrence_ids=[occurrence.id],
    )

    targets = build_excel_evidence_targets(
        [_item(1, None), _item(2, component.id)],
        [component],
        [occurrence],
        [],
        [],
        [sheet],
        [_entity("entity:no-anchor", sheet.id, "M1", (20, 20, 40, 40))],
    )

    assert {value.sequence for value in targets} == {1, 2}
    assert all(value.state == "MISSING" for value in targets)
    assert all(value.status == ReviewStatus.BLOCK for value in targets)
    assert "禁止按 MT 编号猜测" in next(value.reason for value in targets if value.sequence == 1)
    assert "缺少 MT/引线锚点" in next(
        value.reason
        for value in targets
        if value.sequence == 2 and value.stage == "plan"
    )


def test_render_excel_evidence_writes_two_images_integrity_and_index(tmp_path: Path):
    drawing = ezdxf.new("R2018")
    line = drawing.modelspace().add_line((0, 0), (100, 100))
    drawing.modelspace().add_text(
        "MT-01",
        dxfattribs={"insert": (45, 55), "height": 5},
    )
    source = tmp_path / "source.dxf"
    drawing.saveas(source)
    sheet = Sheet(
        id="sheet:plan",
        source_file_id="file:cad",
        drawing_number="P-01",
        kind="plan",
        layout="Model",
        bbox=(-50, -50, 150, 150),
    )
    entity = _entity(
        "entity:line",
        sheet.id,
        str(line.dxf.handle),
        (0, 0, 100, 100),
    )
    occurrence = MtOccurrence(
        id="occurrence:line",
        mt_code="MT-01",
        source_file_id="file:cad",
        sheet_id=sheet.id,
        entity_ids=[entity.id],
        leader_target=(50, 50),
    )
    component = ComponentInstance(
        id="component:line",
        mt_code="MT-01",
        plan_occurrence_ids=[occurrence.id],
    )
    targets = build_excel_evidence_targets(
        [_item(1, component.id)],
        [component],
        [occurrence],
        [],
        [],
        [sheet],
        [entity],
    )

    output = tmp_path / "evidence"
    records = render_excel_evidence(
        targets,
        {"file:cad": source},
        output,
        context_target_px=320,
        detail_target_px=300,
    )

    assert len(records) == len(targets) == 3
    record = next(value for value in records if value.stage == "plan")
    assert record.render_state == "RENDERED"
    assert record.evidence_state == "CANDIDATE"
    assert record.entity_handles == [str(line.dxf.handle)]
    assert record.context_image and (output / record.context_image).is_file()
    assert record.detail_image and (output / record.detail_image).is_file()
    assert record.context_pixel_size and max(record.context_pixel_size) == 320
    assert record.detail_pixel_size and max(record.detail_pixel_size) == 300
    assert record.context_target_px == 320
    assert record.detail_target_px == 300
    assert record.context_aspect_ratio == pytest.approx(
        record.context_pixel_size[0] / record.context_pixel_size[1]
    )
    assert record.detail_aspect_ratio == pytest.approx(
        record.detail_pixel_size[0] / record.detail_pixel_size[1]
    )
    assert record.context_sha256 and len(record.context_sha256) == 64
    assert record.detail_sha256 and len(record.detail_sha256) == 64
    assert record.source_sha256 and len(record.source_sha256) == 64
    index = json.loads((output / "index.json").read_text(encoding="utf-8"))
    assert index["target_count"] == 3
    assert index["record_count"] == 3
    assert index["rendered_count"] == 1
    assert index["missing_count"] == 2
    assert {value["component_id"] for value in index["records"]} == {component.id}


def test_render_failures_and_missing_targets_are_not_dropped(tmp_path: Path):
    ready = next(
        value
        for value in build_excel_evidence_targets(
        [_item(1, "component:ready")],
        [
            ComponentInstance(
                id="component:ready",
                mt_code="MT-01",
                plan_occurrence_ids=["occurrence:ready"],
            )
        ],
        [
            MtOccurrence(
                id="occurrence:ready",
                mt_code="MT-01",
                source_file_id="file:missing",
                sheet_id="sheet:ready",
                leader_target=(10, 10),
            )
        ],
        [],
        [],
        [
            Sheet(
                id="sheet:ready",
                source_file_id="file:missing",
                kind="plan",
                layout="Model",
                bbox=(0, 0, 100, 100),
            )
        ],
            [],
        )
        if value.stage == "plan"
    )
    missing = build_excel_evidence_targets(
        [_item(2, None)],
        [],
        [],
        [],
        [],
        [],
        [],
    )[0]

    records = render_excel_evidence([ready, missing], {}, tmp_path / "failed")

    assert len(records) == 2
    by_sequence = {value.sequence: value for value in records}
    assert by_sequence[1].render_state == "FAILED"
    assert by_sequence[1].evidence_state == "MISSING"
    assert "找不到 file:missing 的 DXF 路径" in by_sequence[1].render_reason
    assert by_sequence[2].render_state == "MISSING"
    assert by_sequence[2].render_reason
    index = json.loads((tmp_path / "failed" / "index.json").read_text(encoding="utf-8"))
    assert index["failed_count"] == 1
    assert index["missing_count"] == 1
    assert index["record_count"] == 2
