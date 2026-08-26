from __future__ import annotations

from pathlib import Path

import pytest
from cadquote.linking import (
    extract_reference_codes,
    normalize_reference_code,
    rank_evidence_edges,
)
from cadquote.materials import (
    load_material_specs,
    normalize_mt_code,
    parse_cad_material_specs,
    parse_material_rows,
)
from cadquote.models import (
    CadEntity,
    MaterialSpec,
    MtOccurrence,
    ReviewStatus,
    Sheet,
)
from cadquote.mt import cluster_nearby_text, detect_mt_occurrences
from openpyxl import Workbook


def entity(
    entity_id: str,
    text: str | None,
    x: float | None,
    y: float | None,
    *,
    sheet_id: str = "sheet:plan",
    entity_type: str = "TEXT",
    geometry: dict | None = None,
) -> CadEntity:
    return CadEntity(
        id=entity_id,
        source_file_id="file:fixture",
        sheet_id=sheet_id,
        handle=entity_id.split(":")[-1],
        entity_type=entity_type,
        space="model",
        text=text,
        insert=(x, y) if x is not None and y is not None else None,
        geometry=geometry or {},
    )


def occurrence(
    occurrence_id: str,
    sheet_id: str,
    *,
    mt_code: str = "MT-01",
    room: str | None = None,
) -> MtOccurrence:
    return MtOccurrence(
        id=occurrence_id,
        mt_code=mt_code,
        source_file_id="file:fixture",
        sheet_id=sheet_id,
        entity_ids=[f"entity:{occurrence_id}"],
        confidence=0.8,
        room=room,
    )


def test_normalizes_full_width_and_detached_material_cards() -> None:
    assert normalize_mt_code("ＭＴ－１") == "MT-01"
    assert normalize_mt_code("说明 MT01 青古铜") == "MT-01"
    assert normalize_mt_code("01") is None

    rows = [
        ["材料编号 NUMBER:", "MT-01", "材料品牌 BRAND:", "示例品牌"],
        ["材料名称 NAME:", "示例拉丝不锈钢", "材料型号 MODEL:", "SYN-1001"],
        ["材料规格 SIZE:", "0.8mm厚", "", ""],
        ["", "", "", ""],
        ["材料编号 NUMBER:", "MT", "02"],
        ["材料名称 NAME:", "示例金属饰件（颜色与MT-01一致）"],
        ["材料规格 SIZE:", "0.8 mm 厚"],
    ]
    specs = parse_material_rows(rows, source_file_id="file:synthetic", sheet_name="合成材料")

    assert [spec.mt_code for spec in specs] == ["MT-01", "MT-02"]
    assert specs[0].name == "示例拉丝不锈钢"
    assert specs[0].brand == "示例品牌"
    assert specs[0].model == "SYN-1001"
    assert specs[0].thickness_mm == pytest.approx(0.8)
    assert specs[1].name == "示例金属饰件(颜色与MT-01一致)"
    assert specs[1].thickness_mm == pytest.approx(0.8)
    assert all(spec.status == ReviewStatus.REVIEW for spec in specs)
    assert [spec.id for spec in specs] == [
        spec.id
        for spec in parse_material_rows(
            rows, source_file_id="file:synthetic", sheet_name="合成材料"
        )
    ]


def test_parses_tabular_xlsx_and_surfaces_conflicts(tmp_path: Path) -> None:
    workbook_path = tmp_path / "materials.xlsx"
    workbook = Workbook()
    worksheet = workbook.active
    worksheet.title = "材料表"
    worksheet.append(["MT编号", "名称", "材质", "厚度", "表面处理", "工艺"])
    worksheet.append(["MT01", "示例镜面不锈钢", "304", 0.8, "镜面", "折弯"])
    worksheet.append(["MT-01", "示例镜面不锈钢", "304", 1.0, "镜面", "折弯"])
    workbook.save(workbook_path)

    specs = load_material_specs(workbook_path, source_file_id="file:xlsx")

    assert len(specs) == 2
    assert {spec.grade for spec in specs} == {"304"}
    assert {spec.thickness_mm for spec in specs} == {0.8, 1.0}
    assert all(any("thickness_mm" in conflict for conflict in spec.conflicts) for spec in specs)


def test_parses_horizontal_cad_material_rows_without_crossing_rows() -> None:
    entities = [
        entity("entity:mt1", "MT-01", 0, 20, sheet_id="sheet:materials"),
        entity("entity:name1", "示例拉丝不锈钢 316 0.8mm厚", 40, 20, sheet_id="sheet:materials"),
        entity("entity:mt2", "MT-02", 0, 10, sheet_id="sheet:materials"),
        entity("entity:name2", "示例镜面不锈钢", 40, 10, sheet_id="sheet:materials"),
    ]

    specs = parse_cad_material_specs(entities)

    assert {spec.mt_code: spec.name for spec in specs} == {
        "MT-01": "示例拉丝不锈钢 316 0.8mm厚",
        "MT-02": "示例镜面不锈钢",
    }
    mt1 = next(spec for spec in specs if spec.mt_code == "MT-01")
    assert mt1.grade == "316"
    assert mt1.thickness_mm == pytest.approx(0.8)


def test_detects_detached_mt_keywords_room_and_leader_without_counting_labels() -> None:
    entities = [
        entity("entity:mt1", "MT", 0, 0),
        entity("entity:n1", "01", 8, 0),
        entity("entity:material1", "示例拉丝不锈钢", 0, -5),
        entity("entity:mt2", "ＭＴ", 30, 0),
        entity("entity:n2", "０１", 38, 0),
        entity("entity:material2", "示例不锈钢踢脚线", 30, -5),
        entity("entity:room", "测试接待区", 0, 60),
        entity(
            "entity:leader1",
            None,
            50,
            50,
            entity_type="LEADER",
            geometry={"vertices": [(50, 50), (4, 0)]},
        ),
        entity(
            "entity:leader2",
            None,
            None,
            None,
            entity_type="LEADER",
            geometry={"leader_target": [80, 25], "label_point": [34, 0]},
        ),
    ]

    detected = detect_mt_occurrences(
        entities,
        cluster_distance=12,
        leader_bind_distance=15,
        room_search_distance=100,
    )

    # Two labels remain two occurrences; quantity is intentionally not inferred.
    assert len(detected) == 2
    assert {item.mt_code for item in detected} == {"MT-01"}
    assert {item.leader_entity_id for item in detected} == {
        "entity:leader1",
        "entity:leader2",
    }
    assert {item.leader_target for item in detected} == {(50.0, 50.0), (80.0, 25.0)}
    assert all(item.room == "测试接待区" for item in detected)
    assert all(item.status == ReviewStatus.REVIEW for item in detected)
    assert [item.model_dump() for item in detected] == [
        item.model_dump()
        for item in detect_mt_occurrences(
            entities,
            cluster_distance=12,
            leader_bind_distance=15,
            room_search_distance=100,
        )
    ]

    clusters = cluster_nearby_text(entities[:3], max_distance=12)
    assert len(clusters) == 1
    assert set(clusters[0].entity_ids) == {
        "entity:mt1",
        "entity:n1",
        "entity:material1",
    }


def test_material_name_only_detection_requires_unique_registry_match() -> None:
    materials = [
        MaterialSpec(id="material:1", mt_code="MT-07", name="苹果砂不锈钢"),
        MaterialSpec(id="material:2", mt_code="MT-08", name="镜面不锈钢"),
    ]
    entities = [entity("entity:apple", "苹果砂不锈钢墙面收口", 10, 10)]

    detected = detect_mt_occurrences(entities, materials=materials, cluster_distance=20)

    assert len(detected) == 1
    assert detected[0].mt_code == "MT-07"
    assert detected[0].confidence == pytest.approx(0.69)
    assert detected[0].status == ReviewStatus.REVIEW


def test_shared_descriptor_does_not_merge_two_callouts() -> None:
    entities = [
        entity("entity:code-left", "MT-01", 0, 0),
        entity("entity:code-right", "MT-01", 20, 0),
        entity("entity:shared-description", "青古铜不锈钢", 10, -3),
    ]

    detected = detect_mt_occurrences(entities, cluster_distance=15)

    assert len(detected) == 2
    assert {item.entity_ids[0] for item in detected} >= {
        "entity:code-left",
        "entity:code-right",
    }


def test_plain_material_descriptor_is_not_used_as_component_name() -> None:
    entities = [
        entity("entity:code", "MT-01", 0, 0),
        entity("entity:material", "古铜色不锈钢", 3, 0),
        entity("entity:component", "社区门厅顶线", 5, 0),
    ]

    detected = detect_mt_occurrences(entities, cluster_distance=12)

    assert len(detected) == 1
    assert detected[0].component_hint == "社区门厅顶线"


def test_drawing_title_is_not_used_as_component_name() -> None:
    entities = [
        entity("entity:code", "MT-01", 0, 0),
        entity("entity:title", "社区门厅立面图", 2, 0),
        entity("entity:component", "门套收口", 8, 0),
    ]

    detected = detect_mt_occurrences(entities, cluster_distance=12)

    assert len(detected) == 1
    assert detected[0].component_hint == "门套收口"


def test_numeric_attribute_with_mt_tag_is_structured_code_evidence() -> None:
    tagged = entity("entity:tagged", "7", 5, 5, entity_type="ATTRIB")
    tagged = tagged.model_copy(update={"geometry": {"tag": "MT_NO"}})

    detected = detect_mt_occurrences([tagged], cluster_distance=10)

    assert len(detected) == 1
    assert detected[0].mt_code == "MT-07"
    assert detected[0].confidence == pytest.approx(0.84)
    assert detected[0].status == ReviewStatus.REVIEW


def test_reference_normalization_range_and_ranked_evidence_edges() -> None:
    assert normalize_reference_code("ＡＥ１") == "A-E-01"
    assert extract_reference_codes("会所立面 A-E-01～A-E-03") >= {
        "A-E-01",
        "A-E-02",
        "A-E-03",
    }

    sheets = [
        Sheet(
            id="sheet:plan",
            source_file_id="file:fixture",
            drawing_number="P-01",
            title="接待前厅平面索引图",
            kind="elevation_index",
        ),
        Sheet(
            id="sheet:elevation:good",
            source_file_id="file:fixture",
            drawing_number="A-E-03",
            title="接待前厅立面图",
            kind="elevation",
        ),
        Sheet(
            id="sheet:elevation:other",
            source_file_id="file:fixture",
            drawing_number="A-E-04",
            title="茶室立面图",
            kind="elevation",
        ),
        Sheet(
            id="sheet:detail:good",
            source_file_id="file:fixture",
            drawing_number="DT-05",
            title="接待台节点大样图",
            kind="detail",
        ),
        Sheet(
            id="sheet:detail:other",
            source_file_id="file:fixture",
            drawing_number="DT-06",
            title="茶室壁炉大样图",
            kind="detail",
        ),
    ]
    occurrences = [
        occurrence("mt:plan", "sheet:plan", room="接待前厅"),
        occurrence("mt:elev-good", "sheet:elevation:good", room="接待前厅"),
        occurrence("mt:elev-other", "sheet:elevation:other", room="茶室"),
        occurrence("mt:detail-good", "sheet:detail:good", room="接待前厅"),
    ]
    entities = [
        entity("entity:plan-ref-prefix", "A-E", 0, 0, sheet_id="sheet:plan"),
        entity("entity:plan-ref-number", "03", 8, 0, sheet_id="sheet:plan"),
        entity(
            "entity:elev-ref",
            "详见节点 DT-05",
            0,
            0,
            sheet_id="sheet:elevation:good",
        ),
    ]

    default_edges = rank_evidence_edges(sheets, occurrences, entities, top_k=2)
    plan_edges = [edge for edge in default_edges if edge.relation == "plan_to_elevation"]
    detail_edges = [
        edge
        for edge in default_edges
        if edge.relation == "elevation_to_detail"
        and edge.source_id == "sheet:elevation:good"
    ]

    assert plan_edges[0].target_id == "sheet:elevation:good"
    assert detail_edges[0].target_id == "sheet:detail:good"
    assert any("explicit_reference:A-E-03" in basis for basis in plan_edges[0].basis)
    assert all(edge.status == ReviewStatus.REVIEW for edge in default_edges)

    promoted = rank_evidence_edges(
        sheets,
        occurrences,
        entities,
        top_k=2,
        promote_explicit=True,
    )
    explicit_pairs = {
        ("sheet:plan", "sheet:elevation:good"),
        ("sheet:elevation:good", "sheet:detail:good"),
    }
    for edge in promoted:
        if (edge.source_id, edge.target_id) in explicit_pairs:
            assert edge.status == ReviewStatus.PASS
        else:
            assert edge.status == ReviewStatus.REVIEW


def test_multiple_explicit_sheet_targets_are_not_all_promoted() -> None:
    sheets = [
        Sheet(id="plan", source_file_id="f", kind="plan"),
        Sheet(
            id="e1",
            source_file_id="f",
            kind="elevation",
            drawing_number="A-E-01",
        ),
        Sheet(
            id="e2",
            source_file_id="f",
            kind="elevation",
            drawing_number="A-E-02",
        ),
    ]
    entities = [
        entity("ref1", "A-E-01", 0, 0, sheet_id="plan"),
        entity("ref2", "A-E-02", 10, 0, sheet_id="plan"),
    ]

    edges = rank_evidence_edges(sheets, entities=entities, promote_explicit=True)

    assert len(edges) == 2
    assert all(edge.status == ReviewStatus.REVIEW for edge in edges)
