from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from cadquote.linking import (
    extract_reference_codes,
    extract_structured_reference_callouts,
    normalize_reference_code,
    rank_evidence_edges,
)
from cadquote.materials import (
    find_material_codes,
    load_material_specs,
    normalize_material_code,
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
from cadquote.mt import (
    cluster_nearby_text,
    detect_material_mentions,
    detect_mt_occurrences,
)
from cadquote.takeoff import build_component_instances
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
        ["材料编号 NUMBER:", "MT-01", "材料品牌 BRAND:", "某品牌"],
        ["材料名称 NAME:", "古铜色不锈钢", "材料型号 MODEL:", "ZE-3002"],
        ["材料规格 SIZE:", "1.2mm厚", "", ""],
        ["", "", "", ""],
        ["材料编号 NUMBER:", "MT", "02"],
        ["材料名称 NAME:", "金属雕花（颜色与MT-01一致）"],
        ["材料规格 SIZE:", "1.2 mm 厚"],
    ]
    specs = parse_material_rows(rows, source_file_id="file:kaili", sheet_name="金属")

    assert [spec.mt_code for spec in specs] == ["MT-01", "MT-02"]
    assert specs[0].name == "古铜色不锈钢"
    assert specs[0].brand == "某品牌"
    assert specs[0].model == "ZE-3002"
    assert specs[0].thickness_mm == pytest.approx(1.2)
    assert specs[1].name == "金属雕花(颜色与MT-01一致)"
    assert specs[1].thickness_mm == pytest.approx(1.2)
    assert all(spec.status == ReviewStatus.REVIEW for spec in specs)
    assert [spec.id for spec in specs] == [
        spec.id
        for spec in parse_material_rows(rows, source_file_id="file:kaili", sheet_name="金属")
    ]


def test_general_material_code_families_preserve_identity_and_scope() -> None:
    matches = find_material_codes("MT-01 / ＧＣ－ＳＳ－１０１ / GC-MT-105 / GC-GL-201 / GC-MR-301")

    assert [value.normalized_code for value in matches] == [
        "MT-01",
        "GC-SS-101",
        "GC-MT-105",
    ]
    assert [value.family for value in matches] == ["MT", "GC-SS", "GC-MT"]
    assert [value.disposition for value in matches] == [
        "stainless",
        "stainless",
        "review",
    ]
    assert normalize_material_code("材料号 GC-SS-101") == "GC-SS-101"
    assert normalize_mt_code("GC-SS-101") is None


def test_material_rows_keep_gc_ss_raw_code_and_ignore_unconfigured_families() -> None:
    rows = [
        ["材料编号", "名称"],
        ["ＧＣ－ＳＳ－１０１", "黑色哑光不锈钢"],
        ["GC-MT-105", "待复核金属"],
        ["GC-GL-201", "玻璃"],
        ["GC-MR-301", "镜子"],
    ]

    specs = parse_material_rows(rows, source_file_id="file:fixture", sheet_name="材料表")

    assert [value.mt_code for value in specs] == ["GC-SS-101", "GC-MT-105"]
    assert [value.material_code_family for value in specs] == ["GC-SS", "GC-MT"]
    assert specs[0].raw_material_code == "ＧＣ－ＳＳ－１０１"


def test_detects_gc_ss_and_reviews_gc_mt_without_absorbing_gl_or_mr() -> None:
    entities = [
        entity("entity:mt", "MT-01", 0, 0),
        entity("entity:ss", "GC-SS-101", 20, 0),
        entity("entity:gc-mt", "GC-MT-105", 40, 0),
        entity("entity:gl", "GC-GL-201", 60, 0),
        entity("entity:mr", "GC-MR-301", 80, 0),
    ]

    detected = detect_mt_occurrences(entities, cluster_distance=5)

    assert {value.mt_code for value in detected} == {
        "MT-01",
        "GC-SS-101",
        "GC-MT-105",
    }
    by_code = {value.mt_code: value for value in detected}
    assert by_code["GC-SS-101"].raw_material_code == "GC-SS-101"
    assert by_code["GC-SS-101"].material_code_family == "GC-SS"
    assert by_code["GC-MT-105"].material_code_family == "GC-MT"
    assert by_code["GC-MT-105"].confidence < by_code["GC-SS-101"].confidence

    configured = detect_mt_occurrences(
        entities,
        cluster_distance=5,
        stainless_code_families={"MT", "GC-SS", "GC-MT"},
        review_code_families=(),
    )
    configured_by_code = {value.mt_code: value for value in configured}
    assert configured_by_code["GC-MT-105"].confidence == pytest.approx(0.88)


def test_detects_detached_gc_ss_family_and_number() -> None:
    detected = detect_mt_occurrences(
        [
            entity("entity:family", "GC-SS", 0, 0),
            entity("entity:number", "102", 8, 0),
        ],
        cluster_distance=12,
    )

    assert len(detected) == 1
    assert detected[0].mt_code == "GC-SS-102"
    assert detected[0].raw_material_code == "GC-SS 102"
    assert detected[0].material_code_family == "GC-SS"


def test_unnumbered_stainless_text_is_diagnostic_not_fabricated_mt() -> None:
    entities = [entity("entity:mention", "黑色哑光不锈钢门套", 10, 10)]

    detected = detect_mt_occurrences(entities)
    mentions = detect_material_mentions(entities, occurrences=detected)

    assert detected == []
    assert len(mentions) == 1
    assert mentions[0].raw_text == "黑色哑光不锈钢门套"
    assert mentions[0].confidence < 0.5
    assert "mt_code" not in mentions[0].model_dump()


def test_component_candidate_preserves_material_code_family() -> None:
    occurrence_value = MtOccurrence(
        id="occurrence:ss",
        mt_code="GC-SS-101",
        raw_material_code="ＧＣ－ＳＳ－１０１",
        material_code_family="GC-SS",
        source_file_id="file:fixture",
        sheet_id="sheet:plan",
        entity_ids=["entity:ss"],
    )
    sheets = [Sheet(id="sheet:plan", source_file_id="file:fixture", kind="plan")]

    components = build_component_instances(sheets, [occurrence_value], [])

    assert len(components) == 1
    assert components[0].mt_code == "GC-SS-101"
    assert components[0].raw_material_code == "ＧＣ－ＳＳ－１０１"
    assert components[0].material_code_family == "GC-SS"


def test_parses_tabular_xlsx_and_surfaces_conflicts(tmp_path: Path) -> None:
    workbook_path = tmp_path / "materials.xlsx"
    workbook = Workbook()
    worksheet = workbook.active
    worksheet.title = "材料表"
    worksheet.append(["MT编号", "名称", "材质", "厚度", "表面处理", "工艺"])
    worksheet.append(["MT01", "黑色镜面不锈钢", "304", 0.91, "镜面黑色", "折弯"])
    worksheet.append(["MT-01", "黑色镜面不锈钢", "304", 1.2, "镜面黑色", "折弯"])
    workbook.save(workbook_path)

    specs = load_material_specs(workbook_path, source_file_id="file:xlsx")

    assert len(specs) == 2
    assert {spec.grade for spec in specs} == {"304"}
    assert {spec.thickness_mm for spec in specs} == {0.91, 1.2}
    assert all(any("thickness_mm" in conflict for conflict in spec.conflicts) for spec in specs)


def test_parses_same_baseline_cad_material_table_without_crossing_rows() -> None:
    entities = [
        entity("entity:mt1", "MT-01", 0, 20, sheet_id="sheet:materials"),
        entity("entity:name1", "苹果砂不锈钢 304 1.2mm厚", 40, 20, sheet_id="sheet:materials"),
        entity("entity:mt2", "MT-02", 0, 10, sheet_id="sheet:materials"),
        entity("entity:name2", "黑色镜面不锈钢", 40, 10, sheet_id="sheet:materials"),
    ]

    specs = parse_cad_material_specs(entities)

    assert {spec.mt_code: spec.name for spec in specs} == {
        "MT-01": "苹果砂不锈钢 304 1.2mm厚",
        "MT-02": "黑色镜面不锈钢",
    }
    mt1 = next(spec for spec in specs if spec.mt_code == "MT-01")
    assert mt1.grade == "304"
    assert mt1.thickness_mm == pytest.approx(1.2)


def test_detects_detached_mt_keywords_room_and_leader_without_counting_labels() -> None:
    entities = [
        entity("entity:mt1", "MT", 0, 0),
        entity("entity:n1", "01", 8, 0),
        entity("entity:material1", "青古铜不锈钢", 0, -5),
        entity("entity:mt2", "ＭＴ", 30, 0),
        entity("entity:n2", "０１", 38, 0),
        entity("entity:material2", "青古铜不锈钢踢脚线", 30, -5),
        entity("entity:room", "接待前厅", 0, 60),
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
    assert all(item.room == "接待前厅" for item in detected)
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


def test_unique_distinctive_material_alias_can_recover_material_code() -> None:
    materials = [
        MaterialSpec(id="material:4", mt_code="MT-04", name="深灰铁艺,实心扁铁"),
    ]
    entities = [entity("entity:rail", "铁艺栏板", 10, 10)]

    detected = detect_mt_occurrences(entities, materials=materials, cluster_distance=20)

    assert len(detected) == 1
    assert detected[0].mt_code == "MT-04"
    assert detected[0].status == ReviewStatus.REVIEW


def test_shared_material_alias_remains_unresolved() -> None:
    materials = [
        MaterialSpec(id="material:4", mt_code="MT-04", name="深灰铁艺,实心扁铁"),
        MaterialSpec(id="material:5", mt_code="MT-05", name="铁艺雕花"),
    ]
    entities = [entity("entity:rail", "铁艺栏板", 10, 10)]

    assert detect_mt_occurrences(entities, materials=materials, cluster_distance=20) == []


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


@pytest.mark.parametrize(
    "component_name",
    [
        "电视壁龛",
        "天花灯槽",
        "银镜框",
        "毛巾吊架",
        "电梯按钮板",
        "大堂旋转门",
        "电视背景板",
        "走廊酒柜侧板",
        "推拉门",
        "门扇",
    ],
)
def test_common_interior_component_nouns_are_retained_as_hints(
    component_name: str,
) -> None:
    detected = detect_mt_occurrences(
        [
            entity("entity:code", "MT-01", 0, 0),
            entity("entity:component", component_name, 5, 0),
        ],
        cluster_distance=12,
    )

    assert len(detected) == 1
    assert detected[0].component_hint == component_name


def test_same_attributed_callout_block_binds_far_component_description() -> None:
    detected = detect_mt_occurrences(
        [
            entity(
                "entity:code",
                "GC-MR-101",
                0,
                0,
                entity_type="ATTRIB",
                geometry={"parent_insert_handle": "CALL-1", "tag": "MATERIAL_CODE"},
            ),
            entity(
                "entity:description",
                "镜子",
                1_000,
                0,
                entity_type="ATTRIB",
                geometry={"parent_insert_handle": "CALL-1", "tag": "DESCRIPTION"},
            ),
            entity("entity:nearby", "门套", 5, 0),
        ],
        cluster_distance=12,
        review_code_families={"GC-MR"},
    )

    assert len(detected) == 1
    assert detected[0].component_hint == "镜子"


def test_stale_bare_mirror_attribute_is_not_a_stainless_component_hint() -> None:
    detected = detect_mt_occurrences(
        [
            entity(
                "entity:code",
                "GC-SS-987",
                0,
                0,
                entity_type="ATTRIB",
                geometry={"parent_insert_handle": "CALL-1"},
            ),
            entity(
                "entity:stale-description",
                "镜子(加防爆膜)",
                1_000,
                0,
                entity_type="ATTRIB",
                geometry={"parent_insert_handle": "CALL-1"},
            ),
        ],
        cluster_distance=12,
        stainless_code_families={"GC-SS"},
    )

    assert len(detected) == 1
    assert detected[0].component_hint is None


def test_specific_view_title_contributes_only_its_component_part() -> None:
    detected = detect_mt_occurrences(
        [
            entity("entity:code", "MT-01", 0, 0),
            entity("entity:title", "服务台A正立面图  SCALE:1/10", 5, 0),
        ],
        cluster_distance=12,
    )

    assert len(detected) == 1
    assert detected[0].component_hint == "服务台A"


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
    assert normalize_reference_code("１Ｆ－Ｅ－１") == "1F-E-01"
    assert normalize_reference_code("2026-03-16") is None
    assert extract_reference_codes("会所立面 A-E-01～A-E-03") >= {
        "A-E-01",
        "A-E-02",
        "A-E-03",
    }
    assert extract_reference_codes("大厅立面 1F-E-01～1F-E-03") >= {
        "1F-E-01",
        "1F-E-02",
        "1F-E-03",
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
        if edge.relation == "elevation_to_detail" and edge.source_id == "sheet:elevation:good"
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


def test_structured_plan_callout_preserves_view_number_and_target_sheet() -> None:
    parent = "CALL-SYNTHETIC"
    entities = [
        entity(
            "entity:view",
            "07",
            0,
            0,
            sheet_id="sheet:plan",
            entity_type="ATTRIB",
            geometry={"parent_insert_handle": parent, "tag": "DETAIL_NO"},
        ),
        entity(
            "entity:page",
            "2F-E-08",
            0,
            -5,
            sheet_id="sheet:plan",
            entity_type="ATTRIB",
            geometry={"parent_insert_handle": parent, "tag": "SHEET_NO"},
        ),
    ]

    callouts = extract_structured_reference_callouts(entities)

    assert len(callouts) == 1
    assert callouts[0].view_number == "07"
    assert callouts[0].code == "2F-E-08"
    assert set(callouts[0].entity_ids) == {"entity:view", "entity:page"}

    sheets = [
        Sheet(id="sheet:plan", source_file_id="file:fixture", kind="plan"),
        Sheet(
            id="sheet:elevation",
            source_file_id="file:fixture",
            kind="elevation",
            drawing_number="2F-E-08",
        ),
    ]
    edges = rank_evidence_edges(sheets, entities=entities)

    assert len(edges) == 1
    assert any(
        value.startswith("view_reference:07->2F-E-08@") for value in edges[0].basis
    )


def test_title_block_without_view_number_is_not_a_structured_callout() -> None:
    parent = "TITLE-BLOCK"
    entities = [
        entity(
            "entity:page",
            "2F-E-08",
            0,
            0,
            entity_type="ATTRIB",
            geometry={"parent_insert_handle": parent, "tag": "SHEET_NO"},
        ),
        entity(
            "entity:scale",
            "1:30",
            0,
            -5,
            entity_type="ATTRIB",
            geometry={"parent_insert_handle": parent, "tag": "SCALE"},
        ),
    ]

    assert extract_structured_reference_callouts(entities) == []


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


def test_available_a2_dxf_derived_text_recovers_mt01_occurrences() -> None:
    source = Path(os.environ.get("CADQUOTE_REAL_DXF_TEXT_JSON", ""))
    if not source.is_file():
        pytest.skip("set CADQUOTE_REAL_DXF_TEXT_JSON to run the private DXF-text check")

    payload = json.loads(source.read_text(encoding="utf-8"))
    entities = [
        entity(
            f"entity:{item.get('h') or index}",
            item["t"],
            item.get("x"),
            item.get("y"),
            sheet_id="sheet:a2-elevation",
            entity_type="ATTRIB" if item.get("blk") else "TEXT",
        )
        for index, item in enumerate(payload["texts"])
    ]

    detected = detect_mt_occurrences(entities, cluster_distance=12)

    assert len(detected) >= 5
    assert {item.mt_code for item in detected} == {"MT-01"}
    assert all(item.status == ReviewStatus.REVIEW for item in detected)


def test_opt_in_material_workbook_yields_both_codes_and_thickness() -> None:
    source = Path(os.environ.get("CADQUOTE_REAL_MATERIAL_XLS", ""))
    if not source.is_file():
        pytest.skip("set CADQUOTE_REAL_MATERIAL_XLS to run the private material check")

    specs = load_material_specs(source, source_file_id="file:private-materials")
    relevant = [spec for spec in specs if spec.mt_code in {"MT-01", "MT-02"}]

    assert {spec.mt_code for spec in relevant} == {"MT-01", "MT-02"}
    assert all(spec.thickness_mm == pytest.approx(1.2) for spec in relevant)
    assert all("\ufffd" not in (spec.name or "") for spec in relevant)
    assert all(spec.status == ReviewStatus.REVIEW for spec in relevant)


def test_unnumbered_metal_component_is_diagnostic_not_fabricated_mt() -> None:
    entities = [entity("entity:metal-mention", "金属栏板", 10, 10)]

    detected = detect_mt_occurrences(entities)
    mentions = detect_material_mentions(entities, occurrences=detected)

    assert detected == []
    assert len(mentions) == 1
    assert mentions[0].raw_text == "金属栏板"
    assert mentions[0].reason == "metal component description has no recognized material code"
    assert "mt_code" not in mentions[0].model_dump()


def test_generic_nonmetal_component_is_not_a_material_mention() -> None:
    entities = [entity("entity:nonmetal", "木饰面栏板", 10, 10)]

    assert detect_material_mentions(entities) == []


def test_structured_explicit_edges_are_not_truncated_by_top_k() -> None:
    plan = Sheet(id="plan-many-refs", source_file_id="synthetic", kind="elevation_index")
    elevations = [
        Sheet(
            id=f"elevation:{number}",
            source_file_id="synthetic",
            kind="elevation",
            drawing_number=f"B2-E-{number:02d}",
        )
        for number in range(1, 8)
    ]
    entities: list[CadEntity] = []
    for number in range(1, 8):
        parent = f"CALL-{number}"
        entities.extend(
            [
                entity(
                    f"view:{number}",
                    f"{number:02d}",
                    number * 10,
                    0,
                    sheet_id=plan.id,
                    entity_type="ATTRIB",
                    geometry={"parent_insert_handle": parent, "tag": "VIEW_NO"},
                ),
                entity(
                    f"page:{number}",
                    f"B2-E-{number:02d}",
                    number * 10,
                    -5,
                    sheet_id=plan.id,
                    entity_type="ATTRIB",
                    geometry={"parent_insert_handle": parent, "tag": "SHEET_NO"},
                ),
            ]
        )

    edges = rank_evidence_edges([plan, *elevations], entities=entities, top_k=1)
    plan_edges = [edge for edge in edges if edge.relation == "plan_to_elevation"]

    assert len(plan_edges) == 7
    assert {edge.target_id for edge in plan_edges} == {
        elevation.id for elevation in elevations
    }
    assert all(
        any(value.startswith("view_reference:") for value in edge.basis)
        for edge in plan_edges
    )


def test_explicit_drawing_sheet_reference_preserves_all_detail_panels() -> None:
    elevation = Sheet(
        id="elevation:fixture",
        source_file_id="synthetic",
        kind="elevation",
        drawing_number="9F-E-97",
    )
    details = [
        Sheet(
            id=f"detail:{number}",
            source_file_id="synthetic",
            kind="detail",
            drawing_number="9F-QS-97",
            title=f"节点 {number}",
        )
        for number in range(1, 9)
    ]
    material_titled_panel = Sheet(
        id="detail:material-title",
        source_file_id="synthetic",
        kind="detail",
        drawing_number="9F-QS-99",
        title="GC-SS-987",
    )
    entities = [
        entity(
            "entity:sheet-reference",
            "详见 9F-QS-97",
            10,
            10,
            sheet_id=elevation.id,
        ),
        entity(
            "entity:material-code",
            "GC-SS-987",
            20,
            20,
            sheet_id=elevation.id,
        ),
    ]

    edges = rank_evidence_edges(
        [elevation, *details, material_titled_panel],
        entities=entities,
        top_k=1,
    )
    detail_edges = [
        edge
        for edge in edges
        if edge.relation == "elevation_to_detail" and edge.source_id == elevation.id
    ]

    preserved = [edge for edge in detail_edges if edge.target_id.startswith("detail:")]
    assert {edge.target_id for edge in preserved} >= {detail.id for detail in details}
    for edge in preserved:
        if edge.target_id in {detail.id for detail in details}:
            assert any(
                value.startswith("drawing_sheet_reference:9F-QS-97@")
                for value in edge.basis
            )
            assert edge.status == ReviewStatus.REVIEW
