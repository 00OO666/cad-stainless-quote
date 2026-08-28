import pytest
from cadquote.models import (
    CadEntity,
    EvidenceEdge,
    MaterialSpec,
    MeasurementCandidate,
    MtOccurrence,
    ReviewStatus,
    Sheet,
)
from cadquote.takeoff import (
    _engineering_chain_sheet_ids,
    build_component_instances,
    build_takeoff,
)


def _fixture():
    sheets = [
        Sheet(
            id="plan",
            source_file_id="f",
            kind="plan",
            title="洽谈区平面图",
            bbox=(0, 0, 100, 100),
        ),
        Sheet(
            id="elevation",
            source_file_id="f",
            kind="elevation",
            drawing_number="A-E-01",
            title="洽谈区立面图",
            bbox=(0, 0, 100, 100),
        ),
        Sheet(
            id="detail",
            source_file_id="f",
            kind="detail",
            drawing_number="DT-01",
            title="不锈钢收口节点",
            bbox=(0, 0, 100, 100),
        ),
    ]
    occurrences = [
        MtOccurrence(
            id="plan-mt",
            mt_code="MT-01",
            source_file_id="f",
            sheet_id="plan",
            anchor=(50, 50),
            room="洽谈区",
            confidence=0.9,
        ),
        MtOccurrence(
            id="elevation-mt",
            mt_code="MT-01",
            source_file_id="f",
            sheet_id="elevation",
            anchor=(50, 50),
            room="洽谈区",
            confidence=0.9,
        ),
        MtOccurrence(
            id="detail-mt",
            mt_code="MT-01",
            source_file_id="f",
            sheet_id="detail",
            anchor=(45, 50),
            leader_entity_id="detail-leader",
            leader_target=(50, 50),
            confidence=0.9,
        ),
    ]
    edges = [
        EvidenceEdge(
            id="p2e",
            relation="plan_to_elevation",
            source_id="plan",
            target_id="elevation",
            basis=["explicit_reference:A-E-01@h1"],
            confidence=0.9,
            status=ReviewStatus.PASS,
        ),
        EvidenceEdge(
            id="e2d",
            relation="elevation_to_detail",
            source_id="elevation",
            target_id="detail",
            basis=["explicit_reference:DT-01@h2"],
            confidence=0.9,
            status=ReviewStatus.PASS,
        ),
    ]
    entities = [
        CadEntity(
            id="length-dim",
            source_file_id="f",
            sheet_id="elevation",
            entity_type="DIMENSION",
            space="model",
            value=5000,
            insert=(50, 50),
            bbox=(45, 45, 55, 55),
            geometry={"units": "millimeters"},
        ),
        CadEntity(
            id="quantity-text",
            source_file_id="f",
            sheet_id="elevation",
            entity_type="TEXT",
            space="model",
            text="×4",
            insert=(55, 50),
            bbox=(54, 49, 58, 52),
        ),
        CadEntity(
            id="unfold-text",
            source_file_id="f",
            sheet_id="detail",
            entity_type="TEXT",
            space="model",
            text="展开 10+180+10",
            insert=(50, 50),
            bbox=(40, 45, 70, 55),
        ),
    ]
    return sheets, occurrences, edges, entities


def _material(*, conflicts: list[str] | None = None) -> MaterialSpec:
    return MaterialSpec(
        id="material-mt-01",
        mt_code="MT-01",
        name="古铜色不锈钢",
        conflicts=conflicts or [],
    )


def _fixture_without_detail():
    sheets, occurrences, edges, entities = _fixture()
    sheets = [sheet for sheet in sheets if sheet.id != "detail"]
    occurrences = [value for value in occurrences if value.sheet_id != "detail"]
    edges = [edge for edge in edges if edge.relation != "elevation_to_detail"]
    entities = [
        value.model_copy(update={"sheet_id": "elevation"})
        if value.id == "unfold-text"
        else value
        for value in entities
    ]
    return sheets, occurrences, edges, entities


def test_takeoff_requires_measurement_confirmation_before_pass():
    sheets, occurrences, edges, entities = _fixture()
    draft = build_takeoff(sheets, entities, occurrences, edges, materials=[_material()])
    assert len(draft.items) == 1
    assert draft.items[0].engineering_quantity is None
    assert draft.items[0].unit is None
    assert draft.items[0].status == ReviewStatus.BLOCK
    assert "missing confirmed unit" in (draft.items[0].note or "")

    confirmation = {
        draft.components[0].id: {
            **{candidate.role: candidate.id for candidate in draft.measurements},
            "unit": "㎡",
            "pricing_method": "按实际展开面积计算",
        }
    }
    final = build_takeoff(
        sheets,
        entities,
        occurrences,
        edges,
        materials=[_material()],
        confirmations=confirmation,
    )
    assert final.items[0].engineering_quantity == 4
    assert final.items[0].status == ReviewStatus.PASS


def test_audited_not_applicable_detail_uses_elevation_measurements_and_outputs_wu():
    sheets, occurrences, edges, entities = _fixture_without_detail()
    draft = build_takeoff(sheets, entities, occurrences, edges, materials=[_material()])
    component = draft.components[0]
    by_role = {candidate.role: candidate for candidate in draft.measurements}
    confirmation = {
        "unit": "㎡",
        "pricing_method": "按实际展开面积计算",
        "plan_to_elevation_edge": "p2e",
        "unfolded_spec": by_role["unfolded_spec"].id,
        "length": by_role["length"].id,
        "quantity": by_role["quantity"].id,
        "detail_requirement": {
            "kind": "not_applicable",
            "basis": "立面已给出展开、长度和数量，未引用节点或大样",
            "searched_sheet_ids": ["elevation"],
            "reference_entity_ids": ["unfold-text", "length-dim"],
        },
    }

    final = build_takeoff(
        sheets,
        entities,
        occurrences,
        edges,
        materials=[_material()],
        confirmations={component.id: confirmation},
    )

    assert final.items[0].status == ReviewStatus.PASS
    assert final.items[0].detail == "无"
    assert final.items[0].engineering_quantity == 4
    assert {"unfold-text", "length-dim"} <= set(final.items[0].evidence_ids)


def test_not_applicable_detail_requires_selected_elevation_in_search_audit():
    sheets, occurrences, edges, entities = _fixture_without_detail()
    draft = build_takeoff(sheets, entities, occurrences, edges, materials=[_material()])
    component = draft.components[0]
    by_role = {candidate.role: candidate for candidate in draft.measurements}
    confirmation = {
        "unit": "㎡",
        "pricing_method": "按实际展开面积计算",
        **{
            role: by_role[role].id
            for role in ("unfolded_spec", "length", "quantity")
        },
        "detail_requirement": {
            "kind": "not_applicable",
            "basis": "人工声称无节点但没有覆盖对应立面",
            "searched_sheet_ids": ["plan"],
        },
    }

    final = build_takeoff(
        sheets,
        entities,
        occurrences,
        edges,
        materials=[_material()],
        confirmations={component.id: confirmation},
    )

    assert final.items[0].status == ReviewStatus.BLOCK
    assert "does not cover selected elevation sheets" in (final.items[0].block_reason or "")


def test_audited_derived_length_combines_cad_candidates_without_free_numbers():
    sheets, occurrences, edges, entities = _fixture()
    draft = build_takeoff(sheets, entities, occurrences, edges, materials=[_material()])
    component = draft.components[0]
    by_role = {candidate.role: candidate for candidate in draft.measurements}
    confirmation = {
        component.id: {
            "unit": "㎡",
            "pricing_method": "按实际展开面积计算",
            "unfolded_spec": by_role["unfolded_spec"].id,
            "quantity": by_role["quantity"].id,
            "length": {
                "kind": "derived_measurement",
                "expression": "run*2",
                "terms": [
                    {"symbol": "run", "candidate_id": by_role["length"].id}
                ],
                "unit": "mm",
                "basis": "立面显示两段同长构件",
            },
        }
    }

    final = build_takeoff(
        sheets,
        entities,
        occurrences,
        edges,
        materials=[_material()],
        confirmations=confirmation,
    )

    item = final.items[0]
    assert item.status == ReviewStatus.PASS
    assert item.length_mm == 10000
    assert item.engineering_quantity == 8
    derived = next(
        candidate
        for candidate in final.measurements
        if candidate.derived_expression is not None
    )
    assert derived.raw_value == "5000*2"
    assert derived.source_candidate_ids == [by_role["length"].id]
    assert derived.entity_ids == ["length-dim"]
    assert derived.source_sheet_ids == ["elevation"]
    edge = next(
        edge
        for edge in final.evidence_edges
        if edge.relation == "component_to_dimension" and edge.target_id == derived.id
    )
    assert edge.status == ReviewStatus.PASS


def test_audited_engineering_quantity_expression_preserves_display_quantity():
    sheets, occurrences, edges, entities = _fixture()
    draft = build_takeoff(sheets, entities, occurrences, edges, materials=[_material()])
    component = draft.components[0]
    by_role = {candidate.role: candidate for candidate in draft.measurements}
    final = build_takeoff(
        sheets,
        entities,
        occurrences,
        edges,
        materials=[_material()],
        confirmations={
            component.id: {
                "unit": "m",
                "pricing_method": "按米计算",
                "length": by_role["length"].id,
                "quantity": by_role["quantity"].id,
                "engineering_quantity": {
                    "kind": "engineering_quantity_expression",
                    "expression": "length_mm*quantity*2/1000",
                    "basis": "立面证实两条独立实体线；数量列保留构件件数",
                    "evidence_ids": ["length-dim"],
                },
            }
        },
    )

    item = final.items[0]
    assert item.status == ReviewStatus.PASS
    assert item.length_mm == 5000
    assert item.quantity == 4
    assert item.engineering_quantity == 40
    assert item.engineering_quantity_expression == "length_mm*quantity*2/1000"
    assert item.engineering_quantity_evidence_ids == ["length-dim"]
    assert "length-dim" in item.evidence_ids
    engineering_edge = next(
        edge
        for edge in final.evidence_edges
        if edge.relation == "component_to_engineering_quantity_evidence"
    )
    assert engineering_edge.source_id == component.id
    assert engineering_edge.target_id == "length-dim"
    assert engineering_edge.status == ReviewStatus.PASS
    assert any(value.startswith("sheet:elevation") for value in engineering_edge.basis)


def test_engineering_quantity_expression_rejects_evidence_outside_selected_chain():
    sheets, occurrences, edges, entities = _fixture()
    entities.append(
        CadEntity(
            id="unrelated-line",
            source_file_id="f",
            sheet_id="unrelated",
            entity_type="LINE",
            space="model",
        )
    )
    draft = build_takeoff(sheets, entities, occurrences, edges, materials=[_material()])
    component = draft.components[0]
    by_role = {candidate.role: candidate for candidate in draft.measurements}
    final = build_takeoff(
        sheets,
        entities,
        occurrences,
        edges,
        materials=[_material()],
        confirmations={
            component.id: {
                "unit": "m",
                "pricing_method": "按米计算",
                "length": by_role["length"].id,
                "quantity": by_role["quantity"].id,
                "engineering_quantity": {
                    "kind": "engineering_quantity_expression",
                    "expression": "length_mm*2/1000",
                    "basis": "错误地引用别处图元",
                    "evidence_ids": ["unrelated-line"],
                },
            }
        },
    )

    assert final.items[0].status == ReviewStatus.BLOCK
    assert "outside selected chain" in (final.items[0].block_reason or "")


def test_engineering_chain_includes_validated_same_drawing_split_panel():
    plan_edge = EvidenceEdge(
        id="p2e",
        relation="plan_to_elevation",
        source_id="plan",
        target_id="elevation-main",
        status=ReviewStatus.PASS,
    )
    detail_edge = EvidenceEdge(
        id="e2d",
        relation="elevation_to_detail",
        source_id="elevation-main",
        target_id="detail-main",
        status=ReviewStatus.PASS,
    )
    split_panel_measurement = MeasurementCandidate(
        id="measurement:split",
        component_id="component:1",
        role="length",
        raw_value="4200",
        numeric_value=4200,
        unit="mm",
        source_file_id="file:1",
        sheet_id="elevation-split",
        source_sheet_ids=["elevation-split"],
        entity_ids=["entity:split-dim"],
    )

    assert _engineering_chain_sheet_ids(
        plan_edge,
        detail_edge,
        {"length": split_panel_measurement},
    ) == {"plan", "elevation-main", "detail-main", "elevation-split"}


def test_derived_measurement_rejects_unanchored_literal_as_a_guess():
    sheets, occurrences, edges, entities = _fixture()
    draft = build_takeoff(sheets, entities, occurrences, edges, materials=[_material()])
    component = draft.components[0]
    by_role = {candidate.role: candidate for candidate in draft.measurements}
    confirmation = {
        component.id: {
            "unit": "㎡",
            "pricing_method": "按实际展开面积计算",
            "unfolded_spec": by_role["unfolded_spec"].id,
            "quantity": by_role["quantity"].id,
            "length": {
                "kind": "derived_measurement",
                "expression": "run+7080",
                "terms": [
                    {"symbol": "run", "candidate_id": by_role["length"].id}
                ],
                "unit": "mm",
                "basis": "缺少另一段 CAD 尺寸",
            },
        }
    }

    result = build_takeoff(
        sheets,
        entities,
        occurrences,
        edges,
        materials=[_material()],
        confirmations=confirmation,
    )

    assert result.items[0].status == ReviewStatus.BLOCK
    assert "numeric literals may only be multiplicities" in (
        result.items[0].block_reason or ""
    )


def test_derived_spec_preserves_three_axis_display_but_uses_confirmed_width_axis():
    sheets, occurrences, edges, entities = _fixture()
    for entity_id, value, x in (
        ("detail-width", 2600, 48),
        ("detail-height", 1050, 50),
        ("detail-depth", 900, 52),
    ):
        entities.append(
            CadEntity(
                id=entity_id,
                source_file_id="f",
                sheet_id="detail",
                entity_type="DIMENSION",
                space="model",
                value=value,
                insert=(x, 50),
                bbox=(x - 1, 49, x + 1, 51),
                geometry={"units": "millimeters"},
            )
        )
    draft = build_takeoff(sheets, entities, occurrences, edges, materials=[_material()])
    component = draft.components[0]
    candidates_by_value = {
        candidate.numeric_value: candidate
        for candidate in draft.measurements
        if candidate.role == "unfolded_spec"
    }
    length = next(candidate for candidate in draft.measurements if candidate.role == "length")
    quantity = next(
        candidate for candidate in draft.measurements if candidate.role == "quantity"
    )
    selection = {
        "kind": "derived_measurement",
        "expression": "width*height*depth",
        "value_expression": "width",
        "terms": [
            {"symbol": "width", "candidate_id": candidates_by_value[2600].id},
            {"symbol": "height", "candidate_id": candidates_by_value[1050].id},
            {"symbol": "depth", "candidate_id": candidates_by_value[900].id},
        ],
        "unit": "mm",
        "basis": "节点标注宽、高、深；面积按宽轴乘立面长度",
    }

    final = build_takeoff(
        sheets,
        entities,
        occurrences,
        edges,
        materials=[_material()],
        confirmations={
            component.id: {
                "unit": "㎡",
                "pricing_method": "按实际展开面积计算",
                "unfolded_spec": selection,
                "length": length.id,
                "quantity": quantity.id,
            }
        },
    )

    assert final.items[0].status == ReviewStatus.PASS
    assert final.items[0].unfolded_spec == "2600*1050*900"
    assert final.items[0].width_mm == 2600
    assert final.items[0].engineering_quantity == 52


def test_mt_occurrence_count_is_not_used_as_quantity():
    sheets, occurrences, edges, entities = _fixture()
    entities = [entity for entity in entities if entity.id != "quantity-text"]
    draft = build_takeoff(sheets, entities, occurrences, edges)
    assert draft.items[0].quantity is None
    assert draft.items[0].status == ReviewStatus.BLOCK


def test_unfolded_spec_requires_one_local_detail_occurrence_anchor():
    sheets, occurrences, edges, entities = _fixture()
    unanchored = [value for value in occurrences if value.id != "detail-mt"]

    result = build_takeoff(sheets, entities, unanchored, edges)

    assert not [
        candidate for candidate in result.measurements if candidate.role == "unfolded_spec"
    ]


def test_explicit_detail_link_with_local_anchor_keeps_local_unfolded_spec():
    sheets, occurrences, edges, entities = _fixture()

    result = build_takeoff(sheets, entities, occurrences, edges)
    unfolded = next(
        candidate for candidate in result.measurements if candidate.role == "unfolded_spec"
    )

    assert unfolded.raw_value == "10+180+10"
    assert "component_local_detail_anchor" in unfolded.basis
    assert "detail_occurrence:detail-mt" in unfolded.basis


def test_linked_detail_native_dimension_is_unfolded_review_candidate():
    sheets, occurrences, edges, entities = _fixture()
    entities = [value for value in entities if value.id != "unfold-text"]
    entities.append(
        CadEntity(
            id="detail-native-dimension",
            source_file_id="f",
            sheet_id="detail",
            entity_type="DIMENSION",
            space="model",
            value=220,
            insert=(52, 50),
            bbox=(48, 48, 56, 52),
            geometry={"units": "millimeters"},
        )
    )

    result = build_takeoff(sheets, entities, occurrences, edges)
    unfolded = next(
        candidate for candidate in result.measurements if candidate.role == "unfolded_spec"
    )

    assert unfolded.numeric_value == 220
    assert unfolded.status == ReviewStatus.REVIEW
    assert "cad_detail_dimension_candidate" in unfolded.basis
    assert "component_local_detail_anchor" in unfolded.basis
    assert "detail_occurrence:detail-mt" in unfolded.basis


def test_structured_height_attribute_is_length_candidate_with_height_semantics():
    sheets, occurrences, edges, entities = _fixture()
    entities = [value for value in entities if value.id != "length-dim"]
    entities.append(
        CadEntity(
            id="height-attribute",
            source_file_id="f",
            sheet_id="elevation",
            entity_type="ATTRIB",
            space="model",
            text="3000",
            insert=(50, 50),
            bbox=(48, 48, 54, 52),
            geometry={"tag": "HT", "parent_insert_handle": "insert-1"},
        )
    )

    result = build_takeoff(sheets, entities, occurrences, edges)
    length = next(
        candidate
        for candidate in result.measurements
        if candidate.role == "length" and candidate.numeric_value == 3000
    )

    assert length.status == ReviewStatus.REVIEW
    assert "structured_numeric_attribute" in length.basis
    assert "structured_attribute_tag:HT" in length.basis
    assert "structured_attribute_semantic:height" in length.basis


def test_structured_quantity_attribute_is_count_candidate_but_decimal_is_not():
    sheets, occurrences, edges, entities = _fixture()
    entities = [value for value in entities if value.id != "quantity-text"]
    entities.extend(
        [
            CadEntity(
                id="quantity-attribute",
                source_file_id="f",
                sheet_id="elevation",
                entity_type="ATTRIB",
                space="model",
                text="6",
                insert=(50, 50),
                geometry={"tag": "QTY", "parent_insert_handle": "insert-2"},
            ),
            CadEntity(
                id="invalid-decimal-count",
                source_file_id="f",
                sheet_id="elevation",
                entity_type="ATTRIB",
                space="model",
                text="2.5",
                insert=(50, 50),
                geometry={"tag": "COUNT", "parent_insert_handle": "insert-3"},
            ),
        ]
    )

    result = build_takeoff(sheets, entities, occurrences, edges)
    quantities = [
        candidate.numeric_value
        for candidate in result.measurements
        if candidate.role == "quantity"
    ]

    assert quantities == [6]


def test_far_structured_height_attribute_remains_bounded_linked_sheet_candidate():
    sheets, occurrences, edges, entities = _fixture()
    entities.append(
        CadEntity(
            id="far-height-attribute",
            source_file_id="f",
            sheet_id="elevation",
            entity_type="ATTRIB",
            space="model",
            text="5950",
            insert=(2_000, 2_000),
            bbox=(1_990, 1_990, 2_050, 2_020),
            geometry={"tag": "HT", "parent_insert_handle": "insert-far"},
        )
    )

    result = build_takeoff(sheets, entities, occurrences, edges)
    candidate = next(
        value
        for value in result.measurements
        if value.role == "length" and value.numeric_value == 5950
    )

    assert "linked_sheet_structured_attribute_fallback" in candidate.basis
    assert candidate.confidence <= 0.54


def test_similarity_only_ceiling_target_cannot_supply_unfolded_spec():
    sheets, occurrences, edges, entities = _fixture()
    sheets.append(
        Sheet(
            id="ceiling",
            source_file_id="f",
            kind="ceiling",
            drawing_number="1F-PL-10",
            title="一层天花布置图",
            bbox=(0, 0, 100, 100),
        )
    )
    edges.append(
        EvidenceEdge(
            id="similar-title-only",
            relation="elevation_to_detail",
            source_id="elevation",
            target_id="ceiling",
            basis=["title_similarity:0.400"],
            confidence=0.06,
            status=ReviewStatus.REVIEW,
        )
    )
    # Even a same-code annotation on the weakly related page cannot make the
    # relation component-specific without an explicit/confirmed detail link.
    occurrences.append(
        MtOccurrence(
            id="ceiling-mt",
            mt_code="MT-01",
            source_file_id="f",
            sheet_id="ceiling",
            leader_entity_id="ceiling-leader",
            leader_target=(50, 50),
        )
    )
    entities.append(
        CadEntity(
            id="ceiling-expression",
            source_file_id="f",
            sheet_id="ceiling",
            entity_type="TEXT",
            space="model",
            text="900+300",
            insert=(50, 50),
            bbox=(45, 45, 65, 55),
        )
    )

    result = build_takeoff(sheets, entities, occurrences, edges)
    unfolded = [
        candidate for candidate in result.measurements if candidate.role == "unfolded_spec"
    ]

    assert [candidate.raw_value for candidate in unfolded] == ["10+180+10"]
    assert all(candidate.sheet_id != "ceiling" for candidate in unfolded)


def test_dimension_multiplication_is_not_misread_as_quantity():
    sheets, occurrences, edges, entities = _fixture()
    entities = [entity for entity in entities if entity.id != "quantity-text"]
    entities.append(
        CadEntity(
            id="dimension-expression",
            source_file_id="f",
            sheet_id="elevation",
            entity_type="TEXT",
            space="model",
            text="50*200*10 50x19 x0.6",
            insert=(52, 50),
            bbox=(50, 49, 65, 52),
        )
    )

    draft = build_takeoff(sheets, entities, occurrences, edges)

    assert not [candidate for candidate in draft.measurements if candidate.role == "quantity"]


def test_split_explicit_quantity_tokens_same_parent_are_review_only():
    sheets, occurrences, edges, entities = _fixture()
    entities = [entity for entity in entities if entity.id != "quantity-text"]
    entities.extend(
        [
            CadEntity(
                id="split-qty-label",
                source_file_id="f",
                sheet_id="elevation",
                entity_type="ATTRIB",
                space="model",
                text="QTY",
                insert=(51, 50),
                geometry={"parent_insert_handle": "COUNT-ANN"},
            ),
            CadEntity(
                id="split-qty-separator",
                source_file_id="f",
                sheet_id="elevation",
                entity_type="ATTRIB",
                space="model",
                text="=",
                insert=(52, 50),
                geometry={"parent_insert_handle": "COUNT-ANN"},
            ),
            CadEntity(
                id="split-qty-value",
                source_file_id="f",
                sheet_id="elevation",
                entity_type="ATTRIB",
                space="model",
                text="2",
                insert=(53, 50),
                geometry={"parent_insert_handle": "COUNT-ANN"},
            ),
            CadEntity(
                id="same-cluster-dimension-product",
                source_file_id="f",
                sheet_id="elevation",
                entity_type="ATTRIB",
                space="model",
                text="50*200",
                insert=(54, 50),
                geometry={"parent_insert_handle": "COUNT-ANN"},
            ),
        ]
    )

    draft = build_takeoff(sheets, entities, occurrences, edges)
    quantities = [candidate for candidate in draft.measurements if candidate.role == "quantity"]

    assert len(quantities) == 1
    assert quantities[0].numeric_value == 2
    assert quantities[0].raw_value == "QTY=2"
    assert quantities[0].status == ReviewStatus.REVIEW
    assert set(quantities[0].entity_ids) == {
        "split-qty-label",
        "split-qty-separator",
        "split-qty-value",
    }
    assert "explicit_count_text_cluster" in quantities[0].basis
    assert "annotation_cluster:parent_insert_handle:COUNT-ANN" in quantities[0].basis
    assert draft.items[0].quantity is None


def test_split_multiplier_tokens_can_share_explicit_leader_annotation_identity():
    sheets, occurrences, edges, entities = _fixture()
    entities = [entity for entity in entities if entity.id != "quantity-text"]
    entities.extend(
        [
            CadEntity(
                id="count-multileader",
                source_file_id="f",
                sheet_id="elevation",
                handle="COUNT-LEADER",
                entity_type="MULTILEADER",
                space="model",
                text="×",
                insert=(51, 50),
                geometry={"annotation_handle": "COUNT-VALUE"},
            ),
            CadEntity(
                id="count-leader-value",
                source_file_id="f",
                sheet_id="elevation",
                handle="COUNT-VALUE",
                entity_type="TEXT",
                space="model",
                text="2",
                insert=(52, 50),
            ),
        ]
    )

    draft = build_takeoff(sheets, entities, occurrences, edges)
    quantities = [candidate for candidate in draft.measurements if candidate.role == "quantity"]

    assert len(quantities) == 1
    assert quantities[0].numeric_value == 2
    assert quantities[0].raw_value == "×2"
    assert quantities[0].status == ReviewStatus.REVIEW
    assert set(quantities[0].entity_ids) == {"count-multileader", "count-leader-value"}
    assert "annotation_cluster:leader_annotation_handle:COUNT-VALUE" in quantities[0].basis


def test_split_quantity_tokens_never_cross_annotation_clusters_or_parse_dimensions():
    sheets, occurrences, edges, entities = _fixture()
    entities = [entity for entity in entities if entity.id != "quantity-text"]
    entities.extend(
        [
            CadEntity(
                id="orphan-qty-label",
                source_file_id="f",
                sheet_id="elevation",
                entity_type="ATTRIB",
                space="model",
                text="数量",
                insert=(51, 50),
                geometry={"parent_insert_handle": "ANN-A"},
            ),
            CadEntity(
                id="other-cluster-number",
                source_file_id="f",
                sheet_id="elevation",
                entity_type="ATTRIB",
                space="model",
                text="2",
                insert=(51.1, 50),
                geometry={"parent_insert_handle": "ANN-B"},
            ),
            CadEntity(
                id="dimension-x-marker",
                source_file_id="f",
                sheet_id="elevation",
                entity_type="ATTRIB",
                space="model",
                text="×",
                insert=(52, 50),
                geometry={"parent_insert_handle": "DIM-ANN"},
            ),
            CadEntity(
                id="dimension-first-value",
                source_file_id="f",
                sheet_id="elevation",
                entity_type="ATTRIB",
                space="model",
                text="50",
                insert=(53, 50),
                geometry={"parent_insert_handle": "DIM-ANN"},
            ),
            CadEntity(
                id="dimension-second-value",
                source_file_id="f",
                sheet_id="elevation",
                entity_type="ATTRIB",
                space="model",
                text="200",
                insert=(54, 50),
                geometry={"parent_insert_handle": "DIM-ANN"},
            ),
            CadEntity(
                id="plain-dimension-product",
                source_file_id="f",
                sheet_id="elevation",
                entity_type="TEXT",
                space="model",
                text="50*200",
                insert=(55, 50),
                geometry={"parent_insert_handle": "PRODUCT-ANN"},
            ),
            CadEntity(
                id="cross-sheet-qty-label",
                source_file_id="f",
                sheet_id="elevation",
                entity_type="ATTRIB",
                space="model",
                text="QTY",
                insert=(56, 50),
                geometry={"parent_insert_handle": "SHARED-ANN"},
            ),
            CadEntity(
                id="cross-sheet-qty-value",
                source_file_id="f",
                sheet_id="detail",
                entity_type="ATTRIB",
                space="model",
                text="2",
                insert=(56, 50),
                geometry={"parent_insert_handle": "SHARED-ANN"},
            ),
        ]
    )

    draft = build_takeoff(sheets, entities, occurrences, edges)

    assert not [candidate for candidate in draft.measurements if candidate.role == "quantity"]


def test_duplicate_mt_evidence_is_conservatively_grouped_without_quantity():
    sheet = Sheet(
        id="plan",
        source_file_id="source",
        kind="plan",
        bbox=(0, 0, 1_000, 1_000),
    )
    occurrences = [
        MtOccurrence(
            id="joined-label",
            mt_code="MT-01",
            source_file_id="source",
            sheet_id="plan",
            entity_ids=["mt-text"],
            anchor=(100.0, 100.0),
            leader_entity_id="leader-1",
            leader_target=(80.0, 100.0),
        ),
        MtOccurrence(
            id="split-label",
            mt_code="MT-01",
            source_file_id="source",
            sheet_id="plan",
            entity_ids=["mt-text", "mt-number"],
            anchor=(100.02, 100.01),
            leader_entity_id="leader-1",
            leader_target=(80.01, 100.0),
        ),
    ]

    components = build_component_instances([sheet], occurrences, [])
    assert len(components) == 1
    assert components[0].plan_occurrence_ids == ["joined-label", "split-label"]

    result = build_takeoff([sheet], [], occurrences, [])
    assert len(result.items) == 1
    assert result.items[0].quantity is None
    assert result.items[0].status == ReviewStatus.BLOCK


def test_near_anchor_dedupe_uses_tiny_tolerance_and_never_crosses_sheets():
    sheets = [
        Sheet(
            id="plan-a",
            source_file_id="source",
            kind="plan",
            bbox=(0, 0, 1_000, 1_000),
        ),
        Sheet(
            id="plan-b",
            source_file_id="source",
            kind="plan",
            bbox=(0, 0, 1_000, 1_000),
        ),
    ]
    occurrences = [
        MtOccurrence(
            id="a-1",
            mt_code="MT-01",
            source_file_id="source",
            sheet_id="plan-a",
            anchor=(10.0, 10.0),
        ),
        MtOccurrence(
            id="a-duplicate",
            mt_code="MT-01",
            source_file_id="source",
            sheet_id="plan-a",
            anchor=(10.000005, 10.000004),
        ),
        MtOccurrence(
            id="a-distinct",
            mt_code="MT-01",
            source_file_id="source",
            sheet_id="plan-a",
            anchor=(10.001, 10.0),
        ),
        MtOccurrence(
            id="b-same-coordinate",
            mt_code="MT-01",
            source_file_id="source",
            sheet_id="plan-b",
            anchor=(10.0, 10.0),
        ),
    ]

    components = build_component_instances(sheets, occurrences, [])
    component_occurrences = sorted(component.plan_occurrence_ids for component in components)
    assert component_occurrences == [
        ["a-1", "a-duplicate"],
        ["a-distinct"],
        ["b-same-coordinate"],
    ]


def test_unabsorbed_elevation_and_door_occurrences_become_review_candidates():
    sheets = [
        Sheet(id="plan", source_file_id="source", kind="plan"),
        Sheet(id="linked-elevation", source_file_id="source", kind="elevation"),
        Sheet(id="orphan-elevation", source_file_id="source", kind="elevation"),
        Sheet(id="orphan-door", source_file_id="source", kind="door"),
        Sheet(id="detail", source_file_id="source", kind="detail"),
    ]
    occurrences = [
        MtOccurrence(
            id="plan-mt",
            mt_code="MT-01",
            source_file_id="source",
            sheet_id="plan",
            room="匹配房间",
        ),
        MtOccurrence(
            id="absorbed-mt",
            mt_code="MT-01",
            source_file_id="source",
            sheet_id="linked-elevation",
            room="匹配房间",
        ),
        MtOccurrence(
            id="orphan-elevation-mt",
            mt_code="MT-01",
            source_file_id="source",
            sheet_id="orphan-elevation",
            entity_ids=["same-original-entity"],
            leader_target=(20, 20),
        ),
        # Duplicate detector output for the same original annotation must not
        # become a second physical component candidate.
        MtOccurrence(
            id="orphan-elevation-mt-duplicate",
            mt_code="MT-01",
            source_file_id="source",
            sheet_id="orphan-elevation",
            entity_ids=["same-original-entity"],
            leader_target=(20, 20),
        ),
        MtOccurrence(
            id="orphan-door-mt",
            mt_code="MT-02",
            source_file_id="source",
            sheet_id="orphan-door",
            leader_target=(30, 30),
        ),
        # Repeating an occurrence ID in input cannot create a duplicate candidate.
        MtOccurrence(
            id="orphan-door-mt",
            mt_code="MT-02",
            source_file_id="source",
            sheet_id="orphan-door",
            leader_target=(30, 30),
        ),
        # A bare material label remains occurrence evidence and must not become
        # a physical orphan line item by itself.
        MtOccurrence(
            id="bare-elevation-mt",
            mt_code="MT-03",
            source_file_id="source",
            sheet_id="orphan-elevation",
            anchor=(80, 80),
        ),
    ]
    edges = [
        EvidenceEdge(
            id="plan-link",
            relation="plan_to_elevation",
            source_id="plan",
            target_id="linked-elevation",
        ),
        EvidenceEdge(
            id="detail-link",
            relation="elevation_to_detail",
            source_id="orphan-elevation",
            target_id="detail",
        ),
    ]

    components = build_component_instances(sheets, occurrences, edges)
    assert len(components) == 3
    assert sum("absorbed-mt" in value.elevation_occurrence_ids for value in components) == 1
    orphan_components = [value for value in components if not value.plan_occurrence_ids]
    assert len(orphan_components) == 2
    assert all(value.status == ReviewStatus.REVIEW for value in orphan_components)
    grouped = next(
        value
        for value in orphan_components
        if "orphan-elevation-mt" in value.elevation_occurrence_ids
    )
    assert grouped.elevation_occurrence_ids == [
        "orphan-elevation-mt",
        "orphan-elevation-mt-duplicate",
    ]
    assert grouped.detail_sheet_ids == ["detail"]

    plan_only = build_component_instances(
        sheets,
        occurrences,
        edges,
        include_orphan_elevations=False,
    )
    assert len(plan_only) == 1

    takeoff = build_takeoff(sheets, [], occurrences, edges)
    assert all(item.quantity is None for item in takeoff.items)


def _confirmed_measurements(draft, unit: str, pricing_method: str):
    required = {
        "㎡": {"unfolded_spec", "length", "quantity"},
        "m": {"length", "quantity"},
        "件": {"quantity"},
        "套": {"quantity"},
    }[unit]
    values = {
        candidate.role: candidate.id
        for candidate in draft.measurements
        if candidate.role in required
    }
    return {**values, "unit": unit, "pricing_method": pricing_method}


@pytest.mark.parametrize(
    ("unit", "pricing_method", "excluded_entities", "expected_quantity"),
    [
        ("㎡", "按实际展开面积计算", set(), 4.0),
        ("m", "按米计算", {"unfold-text"}, 20.0),
        ("件", "按件计算", {"unfold-text", "length-dim"}, 4.0),
        ("套", "按套计算", {"unfold-text", "length-dim"}, 4.0),
    ],
)
def test_required_measurements_depend_on_confirmed_unit(
    unit, pricing_method, excluded_entities, expected_quantity
):
    sheets, occurrences, edges, entities = _fixture()
    entities = [value for value in entities if value.id not in excluded_entities]
    draft = build_takeoff(sheets, entities, occurrences, edges, materials=[_material()])
    confirmation = {draft.components[0].id: _confirmed_measurements(draft, unit, pricing_method)}
    final = build_takeoff(
        sheets,
        entities,
        occurrences,
        edges,
        materials=[_material()],
        confirmations=confirmation,
    )
    assert final.items[0].status == ReviewStatus.PASS
    assert final.items[0].engineering_quantity == expected_quantity


def test_confirmed_relation_edges_can_promote_a_coherent_review_chain():
    sheets, occurrences, edges, entities = _fixture()
    review_edges = [edge.model_copy(update={"status": ReviewStatus.REVIEW}) for edge in edges]
    draft = build_takeoff(sheets, entities, occurrences, review_edges, materials=[_material()])
    confirmation = _confirmed_measurements(draft, "㎡", "按实际展开面积计算")
    confirmation.update(
        {
            "plan_to_elevation_edge": "p2e",
            "elevation_to_detail_edge": "e2d",
        }
    )
    final = build_takeoff(
        sheets,
        entities,
        occurrences,
        review_edges,
        materials=[_material()],
        confirmations={draft.components[0].id: confirmation},
    )
    assert final.items[0].status == ReviewStatus.PASS
    relation_status = {
        edge.id: edge.status for edge in final.evidence_edges if edge.id in {"p2e", "e2d"}
    }
    assert relation_status == {
        "p2e": ReviewStatus.PASS,
        "e2d": ReviewStatus.PASS,
    }


def test_wrong_relation_confirmation_never_passes():
    sheets, occurrences, edges, entities = _fixture()
    draft = build_takeoff(sheets, entities, occurrences, edges, materials=[_material()])
    confirmation = _confirmed_measurements(draft, "㎡", "按实际展开面积计算")
    confirmation["plan_to_elevation_edge"] = "not-a-real-component-edge"
    final = build_takeoff(
        sheets,
        entities,
        occurrences,
        edges,
        materials=[_material()],
        confirmations={draft.components[0].id: confirmation},
    )
    assert final.items[0].status == ReviewStatus.BLOCK
    assert "does not exist in component chain" in (final.items[0].block_reason or "")


def test_multiple_pass_relation_paths_still_require_edge_disambiguation():
    sheets, occurrences, edges, entities = _fixture()
    edges.append(
        edges[0].model_copy(
            update={"id": "p2e-duplicate", "basis": ["another sheet-level reference"]}
        )
    )
    draft = build_takeoff(sheets, entities, occurrences, edges, materials=[_material()])
    measurement_confirmation = _confirmed_measurements(draft, "㎡", "按实际展开面积计算")
    ambiguous = build_takeoff(
        sheets,
        entities,
        occurrences,
        edges,
        materials=[_material()],
        confirmations={draft.components[0].id: measurement_confirmation},
    )
    assert ambiguous.items[0].status == ReviewStatus.REVIEW
    assert "ambiguous plan_to_elevation" in (ambiguous.items[0].note or "")

    measurement_confirmation["plan_to_elevation_edge"] = "p2e"
    resolved = build_takeoff(
        sheets,
        entities,
        occurrences,
        edges,
        materials=[_material()],
        confirmations={draft.components[0].id: measurement_confirmation},
    )
    assert resolved.items[0].status == ReviewStatus.PASS


def test_confirmed_measurement_must_belong_to_selected_relation_chain():
    sheets, occurrences, edges, entities = _fixture()
    sheets.append(
        Sheet(
            id="detail-other",
            source_file_id="f",
            kind="detail",
            drawing_number="DT-02",
            title="另一节点",
            bbox=(0, 0, 100, 100),
        )
    )
    edges.append(
        EvidenceEdge(
            id="e2d-other",
            relation="elevation_to_detail",
            source_id="elevation",
            target_id="detail-other",
            basis=["explicit_reference:DT-02"],
            confidence=0.9,
            status=ReviewStatus.PASS,
        )
    )
    entities.append(
        CadEntity(
            id="other-unfold",
            source_file_id="f",
            sheet_id="detail-other",
            entity_type="TEXT",
            space="model",
            text="展开 20+200+20",
            insert=(50, 50),
            bbox=(40, 45, 70, 55),
        )
    )
    occurrences.append(
        MtOccurrence(
            id="detail-other-mt",
            mt_code="MT-01",
            source_file_id="f",
            sheet_id="detail-other",
            anchor=(45, 50),
            leader_entity_id="detail-other-leader",
            leader_target=(50, 50),
            confidence=0.9,
        )
    )
    draft = build_takeoff(sheets, entities, occurrences, edges, materials=[_material()])
    component = next(value for value in draft.components if value.plan_occurrence_ids)
    candidates = [value for value in draft.measurements if value.component_id == component.id]
    wrong_unfold = next(
        value
        for value in candidates
        if value.role == "unfolded_spec" and value.sheet_id == "detail-other"
    )
    confirmation = {
        "unit": "㎡",
        "pricing_method": "按实际展开面积计算",
        "plan_to_elevation_edge": "p2e",
        "elevation_to_detail_edge": "e2d",
        "unfolded_spec": wrong_unfold.id,
        **{value.role: value.id for value in candidates if value.role in {"length", "quantity"}},
    }
    final = build_takeoff(
        sheets,
        entities,
        occurrences,
        edges,
        materials=[_material()],
        confirmations={component.id: confirmation},
    )
    item = next(value for value in final.items if value.component_id == component.id)
    assert item.status == ReviewStatus.BLOCK
    assert "confirmed unfolded_spec candidate does not exist" in (item.block_reason or "")


@pytest.mark.parametrize("materials", [[], [_material(conflicts=["finish: A | B"])]])
def test_missing_or_conflicting_material_never_passes(materials):
    sheets, occurrences, edges, entities = _fixture()
    draft = build_takeoff(sheets, entities, occurrences, edges, materials=materials)
    confirmation = _confirmed_measurements(draft, "㎡", "按实际展开面积计算")
    final = build_takeoff(
        sheets,
        entities,
        occurrences,
        edges,
        materials=materials,
        confirmations={draft.components[0].id: confirmation},
    )
    assert final.items[0].status == ReviewStatus.BLOCK


def test_legacy_role_only_confirmation_is_accepted_but_cannot_guess_unit():
    sheets, occurrences, edges, entities = _fixture()
    draft = build_takeoff(sheets, entities, occurrences, edges, materials=[_material()])
    legacy = {candidate.role: candidate.id for candidate in draft.measurements}
    final = build_takeoff(
        sheets,
        entities,
        occurrences,
        edges,
        materials=[_material()],
        confirmations={draft.components[0].id: legacy},
    )
    assert final.items[0].status == ReviewStatus.BLOCK
    assert final.items[0].unit is None
    passed_dimensions = [
        edge
        for edge in final.evidence_edges
        if edge.relation == "component_to_dimension" and edge.status == ReviewStatus.PASS
    ]
    assert len(passed_dimensions) == 3


def test_paper_space_dimension_is_never_silently_treated_as_millimetres():
    sheets, occurrences, edges, entities = _fixture()
    entities = [
        value.model_copy(update={"space": "paper:A-01"}) if value.id == "length-dim" else value
        for value in entities
    ]
    draft = build_takeoff(sheets, entities, occurrences, edges, materials=[_material()])
    assert not any(value.role == "length" for value in draft.measurements)


def test_paper_space_dimension_requires_explicit_display_measurement_metadata():
    sheets, occurrences, edges, entities = _fixture()
    entities = [
        value.model_copy(
            update={
                "space": "paper:A-01",
                "geometry": {
                    **value.geometry,
                    "display_measurement": 5000,
                },
            }
        )
        if value.id == "length-dim"
        else value
        for value in entities
    ]
    draft = build_takeoff(sheets, entities, occurrences, edges, materials=[_material()])
    length = next(value for value in draft.measurements if value.role == "length")
    assert length.numeric_value == 5000


def test_detail_sheet_dimensions_are_not_component_length_candidates():
    sheets, occurrences, edges, entities = _fixture()
    entities = [value for value in entities if value.id != "length-dim"]
    entities.append(
        CadEntity(
            id="detail-fold-dimension",
            source_file_id="f",
            sheet_id="detail",
            entity_type="DIMENSION",
            space="model",
            value=10,
            geometry={"units": "millimeters", "display_measurement": 10},
        )
    )
    draft = build_takeoff(sheets, entities, occurrences, edges, materials=[_material()])
    assert not any(value.role == "length" for value in draft.measurements)


@pytest.mark.parametrize("geometry", [{}, {"units": "meters"}, {"units": "unknown"}])
def test_unknown_or_non_mm_dimension_unit_is_rejected(geometry):
    sheets, occurrences, edges, entities = _fixture()
    entities = [
        value.model_copy(update={"geometry": geometry}) if value.id == "length-dim" else value
        for value in entities
    ]
    draft = build_takeoff(sheets, entities, occurrences, edges, materials=[_material()])
    assert not any(value.role == "length" for value in draft.measurements)


def test_dimension_override_is_the_confirmable_numeric_value():
    sheets, occurrences, edges, entities = _fixture()
    entities = [
        value.model_copy(update={"value": 1000, "text_override": "1200"})
        if value.id == "length-dim"
        else value
        for value in entities
    ]
    draft = build_takeoff(sheets, entities, occurrences, edges, materials=[_material()])
    length = next(value for value in draft.measurements if value.role == "length")
    assert length.raw_value == "1200"
    assert length.numeric_value == 1200
    confirmation = _confirmed_measurements(draft, "㎡", "按实际展开面积计算")
    final = build_takeoff(
        sheets,
        entities,
        occurrences,
        edges,
        materials=[_material()],
        confirmations={draft.components[0].id: confirmation},
    )
    assert final.items[0].status == ReviewStatus.PASS
    assert final.items[0].engineering_quantity == 0.96


def test_unparseable_dimension_override_does_not_fall_back_to_hidden_geometry():
    sheets, occurrences, edges, entities = _fixture()
    entities = [
        value.model_copy(update={"value": 5000, "text_override": "VARIES"})
        if value.id == "length-dim"
        else value
        for value in entities
    ]
    draft = build_takeoff(sheets, entities, occurrences, edges, materials=[_material()])
    assert not any(value.role == "length" for value in draft.measurements)


def test_sheet_level_mt_fanout_does_not_claim_every_elevation_occurrence():
    sheets, occurrences, edges, _ = _fixture()
    occurrences[0] = occurrences[0].model_copy(update={"room": None})
    occurrences[1] = occurrences[1].model_copy(
        update={"room": None, "leader_target": (50, 50)}
    )
    occurrences.extend(
        [
            occurrences[1].model_copy(update={"id": "elevation-mt-2", "anchor": (60, 50)}),
            occurrences[1].model_copy(update={"id": "elevation-mt-3", "anchor": (70, 50)}),
        ]
    )
    components = build_component_instances(sheets, occurrences, edges)
    plan_component = next(value for value in components if value.plan_occurrence_ids)
    assert plan_component.elevation_occurrence_ids == []
    orphan_ids = {
        occurrence_id
        for value in components
        if not value.plan_occurrence_ids
        for occurrence_id in value.elevation_occurrence_ids
    }
    assert orphan_ids == {"elevation-mt", "elevation-mt-2", "elevation-mt-3"}


def test_named_object_views_aggregate_without_same_sheet_material_fanout():
    sheets = [
        Sheet(id="plan-a", source_file_id="f", kind="plan", title="服务台A平面图"),
        Sheet(
            id="front-a",
            source_file_id="f",
            kind="elevation",
            drawing_number="GS-01",
            title="服务台A正立面图 SCALE:1/10",
        ),
        Sheet(
            id="side-a",
            source_file_id="f",
            kind="elevation",
            drawing_number="GS-02",
            title="服务台A侧立面图 SCALE:1/10",
        ),
        Sheet(
            id="generic",
            source_file_id="f",
            kind="elevation",
            drawing_number="E-01",
            title="立面图 SCALE:1/40",
        ),
        Sheet(id="detail", source_file_id="f", kind="detail", title="服务台A节点图"),
    ]
    occurrences = [
        MtOccurrence(id="plan-a-mt", mt_code="MT-01", source_file_id="f", sheet_id="plan-a"),
        MtOccurrence(
            id="front-a-mt-1",
            mt_code="MT-01",
            source_file_id="f",
            sheet_id="front-a",
            leader_target=(10, 10),
        ),
        MtOccurrence(
            id="front-a-mt-2",
            mt_code="MT-01",
            source_file_id="f",
            sheet_id="front-a",
            leader_target=(20, 10),
        ),
        MtOccurrence(
            id="side-a-mt",
            mt_code="MT-01",
            source_file_id="f",
            sheet_id="side-a",
            leader_target=(30, 10),
        ),
        MtOccurrence(
            id="generic-mt-1",
            mt_code="MT-01",
            source_file_id="f",
            sheet_id="generic",
            leader_target=(10, 20),
        ),
        MtOccurrence(
            id="generic-mt-2",
            mt_code="MT-01",
            source_file_id="f",
            sheet_id="generic",
            leader_target=(20, 20),
        ),
        MtOccurrence(
            id="detail-mt",
            mt_code="MT-01",
            source_file_id="f",
            sheet_id="detail",
            leader_target=(10, 10),
        ),
    ]
    components = build_component_instances(sheets, occurrences, [])

    assert len(components) == 3
    service_desk = next(value for value in components if value.plan_occurrence_ids)
    assert service_desk.name == "服务台A"
    assert service_desk.plan_occurrence_ids == ["plan-a-mt"]
    assert set(service_desk.elevation_occurrence_ids) == {
        "front-a-mt-1",
        "front-a-mt-2",
        "side-a-mt",
    }
    assert {
        occurrence_id
        for value in components
        if value.id != service_desk.id
        for occurrence_id in value.elevation_occurrence_ids
    } == {"generic-mt-1", "generic-mt-2"}
    assert all(
        "detail-mt" not in {*value.plan_occurrence_ids, *value.elevation_occurrence_ids}
        for value in components
    )


def test_named_views_with_conflicting_component_hints_do_not_merge():
    sheets = [
        Sheet(id="front", source_file_id="f", kind="elevation", title="服务台A正立面图"),
        Sheet(id="side", source_file_id="f", kind="elevation", title="服务台A侧立面图"),
    ]
    occurrences = [
        MtOccurrence(
            id="front-mt",
            mt_code="MT-01",
            source_file_id="f",
            sheet_id="front",
            leader_target=(10, 10),
            component_hint="台面包边",
        ),
        MtOccurrence(
            id="side-mt",
            mt_code="MT-01",
            source_file_id="f",
            sheet_id="side",
            leader_target=(20, 10),
            component_hint="踢脚线",
        ),
    ]

    components = build_component_instances(sheets, occurrences, [])

    assert len(components) == 2


def test_named_plan_and_elevation_can_merge_across_linked_source_files():
    sheets = [
        Sheet(id="plan", source_file_id="plan-file", kind="plan", title="接待台平面图"),
        Sheet(
            id="elevation",
            source_file_id="elevation-file",
            kind="elevation",
            title="接待台正立面图",
        ),
    ]
    occurrences = [
        MtOccurrence(
            id="plan-mt",
            mt_code="MT-01",
            source_file_id="plan-file",
            sheet_id="plan",
        ),
        MtOccurrence(
            id="elevation-mt",
            mt_code="MT-01",
            source_file_id="elevation-file",
            sheet_id="elevation",
            leader_target=(10, 10),
        ),
    ]
    edge = EvidenceEdge(
        id="p2e",
        relation="plan_to_elevation",
        source_id="plan",
        target_id="elevation",
        confidence=0.8,
        status=ReviewStatus.REVIEW,
    )

    linked = build_component_instances(sheets, occurrences, [edge])
    unlinked = build_component_instances(sheets, occurrences, [])

    assert len(linked) == 1
    assert linked[0].plan_occurrence_ids == ["plan-mt"]
    assert linked[0].elevation_occurrence_ids == ["elevation-mt"]
    assert len(unlinked) == 2


def test_confirmed_elevation_occurrence_is_validated_and_suppresses_its_orphan():
    sheets, occurrences, edges, entities = _fixture()
    occurrences[0] = occurrences[0].model_copy(update={"room": None})
    occurrences[1] = occurrences[1].model_copy(
        update={"room": None, "leader_target": (50, 50)}
    )
    occurrences.append(
        occurrences[1].model_copy(update={"id": "elevation-mt-2", "anchor": (60, 50)})
    )
    draft = build_takeoff(sheets, entities, occurrences, edges, materials=[_material()])
    plan_component = next(value for value in draft.components if value.plan_occurrence_ids)
    confirmation = {
        "unit": "件",
        "pricing_method": "按件计算",
        "elevation_occurrence": "elevation-mt",
    }
    selected_draft = build_takeoff(
        sheets,
        entities,
        occurrences,
        edges,
        materials=[_material()],
        confirmations={plan_component.id: confirmation},
    )
    quantity = next(
        value
        for value in selected_draft.measurements
        if value.component_id == plan_component.id and value.role == "quantity"
    )
    confirmation["quantity"] = quantity.id
    final = build_takeoff(
        sheets,
        entities,
        occurrences,
        edges,
        materials=[_material()],
        confirmations={plan_component.id: confirmation},
    )
    selected_component = next(value for value in final.components if value.id == plan_component.id)
    assert selected_component.elevation_occurrence_ids == ["elevation-mt"]
    assert all(
        "elevation-mt" not in value.elevation_occurrence_ids
        for value in final.components
        if not value.plan_occurrence_ids
    )

    invalid = build_takeoff(
        sheets,
        entities,
        occurrences,
        edges,
        materials=[_material()],
        confirmations={
            plan_component.id: {
                **confirmation,
                "elevation_occurrence": "not-real",
            }
        },
    )
    invalid_item = next(value for value in invalid.items if value.component_id == plan_component.id)
    assert invalid_item.status == ReviewStatus.BLOCK


def _merge_fixture():
    sheets, occurrences, edges, entities = _fixture()
    sheets.append(
        Sheet(
            id="door",
            source_file_id="f",
            kind="door",
            title="洽谈区门套立面",
            bbox=(0, 0, 100, 100),
        )
    )
    occurrences.append(
        MtOccurrence(
            id="door-mt",
            mt_code="MT-01",
            source_file_id="f",
            sheet_id="door",
            anchor=(50, 50),
            leader_target=(50, 50),
            room="洽谈区",
        )
    )
    edges.append(
        EvidenceEdge(
            id="door-e2d",
            relation="elevation_to_detail",
            source_id="door",
            target_id="detail",
            basis=["explicit_reference:DT-01@door"],
            confidence=0.9,
            status=ReviewStatus.PASS,
        )
    )
    return sheets, occurrences, edges, entities


@pytest.mark.parametrize("as_list", [False, True])
def test_explicit_component_merge_suppresses_source_and_marks_evidence_pass(as_list):
    sheets, occurrences, edges, entities = _merge_fixture()
    draft = build_takeoff(sheets, entities, occurrences, edges, materials=[_material()])
    target = next(value for value in draft.components if value.plan_occurrence_ids)
    source = next(value for value in draft.components if not value.plan_occurrence_ids)
    merge_value = [source.id] if as_list else source.id
    commercial_confirmation = {
        "merge_component_ids": merge_value,
        "unit": "件",
        "pricing_method": "按件计算",
    }
    merged_draft = build_takeoff(
        sheets,
        entities,
        occurrences,
        edges,
        materials=[_material()],
        confirmations={target.id: commercial_confirmation},
    )
    quantity = next(
        value
        for value in merged_draft.measurements
        if value.component_id == target.id and value.role == "quantity"
    )
    final = build_takeoff(
        sheets,
        entities,
        occurrences,
        edges,
        materials=[_material()],
        confirmations={
            target.id: {
                **commercial_confirmation,
                "quantity": quantity.id,
            }
        },
    )
    assert [value.id for value in final.components] == [target.id]
    assert final.items[0].status == ReviewStatus.PASS
    assert set(final.components[0].elevation_occurrence_ids) == {
        "elevation-mt",
        "door-mt",
    }
    explicit_edges = [
        edge
        for edge in final.evidence_edges
        if edge.relation == "occurrence_to_component"
        and edge.source_id == "door-mt"
        and edge.target_id == target.id
    ]
    assert len(explicit_edges) == 1
    assert explicit_edges[0].status == ReviewStatus.PASS
    assert "confirmation:merge_component_ids" in explicit_edges[0].basis


def test_component_merge_room_conflict_blocks_without_suppressing_source():
    sheets, occurrences, edges, entities = _merge_fixture()
    occurrences = [
        value.model_copy(update={"room": "另一区"}) if value.id == "door-mt" else value
        for value in occurrences
    ]
    draft = build_takeoff(sheets, entities, occurrences, edges, materials=[_material()])
    target = next(value for value in draft.components if value.plan_occurrence_ids)
    source = next(value for value in draft.components if not value.plan_occurrence_ids)
    final = build_takeoff(
        sheets,
        entities,
        occurrences,
        edges,
        materials=[_material()],
        confirmations={target.id: {"merge_component_ids": source.id}},
    )
    assert {value.id for value in final.components} == {target.id, source.id}
    target_item = next(value for value in final.items if value.component_id == target.id)
    assert target_item.status == ReviewStatus.BLOCK
    assert "merge room conflict" in (target_item.block_reason or "")


def test_component_merge_name_conflict_blocks_without_guessing():
    sheets, occurrences, edges, entities = _merge_fixture()
    occurrences = [
        value.model_copy(update={"component_hint": "连续收口"})
        if value.id == "plan-mt"
        else value.model_copy(update={"component_hint": "门套收边"})
        if value.id == "door-mt"
        else value
        for value in occurrences
    ]
    draft = build_takeoff(sheets, entities, occurrences, edges, materials=[_material()])
    target = next(value for value in draft.components if value.plan_occurrence_ids)
    source = next(value for value in draft.components if not value.plan_occurrence_ids)
    final = build_takeoff(
        sheets,
        entities,
        occurrences,
        edges,
        materials=[_material()],
        confirmations={target.id: {"merge_component_ids": source.id}},
    )
    assert len(final.components) == 2
    target_item = next(value for value in final.items if value.component_id == target.id)
    assert target_item.status == ReviewStatus.BLOCK
    assert "merge name conflict" in (target_item.block_reason or "")


@pytest.mark.parametrize("invalid_kind", ["missing", "self", "mt_mismatch"])
def test_invalid_component_merge_never_passes_or_suppresses(invalid_kind):
    sheets, occurrences, edges, entities = _merge_fixture()
    if invalid_kind == "mt_mismatch":
        occurrences = [
            value.model_copy(update={"mt_code": "MT-02"}) if value.id == "door-mt" else value
            for value in occurrences
        ]
    draft = build_takeoff(sheets, entities, occurrences, edges, materials=[_material()])
    target = next(value for value in draft.components if value.plan_occurrence_ids)
    source = next(value for value in draft.components if not value.plan_occurrence_ids)
    merge_id = {
        "missing": "component:not-real",
        "self": target.id,
        "mt_mismatch": source.id,
    }[invalid_kind]
    final = build_takeoff(
        sheets,
        entities,
        occurrences,
        edges,
        materials=[_material()],
        confirmations={target.id: {"merge_component_ids": merge_id}},
    )
    assert len(final.components) == 2
    target_item = next(value for value in final.items if value.component_id == target.id)
    assert target_item.status == ReviewStatus.BLOCK
    assert "merge" in (target_item.block_reason or "") or "itself" in (
        target_item.block_reason or ""
    )


def test_merge_source_cannot_be_claimed_by_multiple_targets():
    sheets, occurrences, edges, entities = _merge_fixture()
    sheets.extend(
        [
            Sheet(id="plan-2", source_file_id="f", kind="plan", title="包间平面"),
            Sheet(id="elevation-2", source_file_id="f", kind="elevation", title="包间立面"),
        ]
    )
    occurrences.extend(
        [
            MtOccurrence(
                id="plan-mt-2",
                mt_code="MT-01",
                source_file_id="f",
                sheet_id="plan-2",
                room="包间",
            ),
            MtOccurrence(
                id="elevation-mt-2",
                mt_code="MT-01",
                source_file_id="f",
                sheet_id="elevation-2",
                room="包间",
            ),
        ]
    )
    edges.extend(
        [
            EvidenceEdge(
                id="p2e-2",
                relation="plan_to_elevation",
                source_id="plan-2",
                target_id="elevation-2",
                status=ReviewStatus.PASS,
            ),
            EvidenceEdge(
                id="e2d-2",
                relation="elevation_to_detail",
                source_id="elevation-2",
                target_id="detail",
                status=ReviewStatus.PASS,
            ),
        ]
    )
    draft = build_takeoff(sheets, entities, occurrences, edges, materials=[_material()])
    targets = [value for value in draft.components if value.plan_occurrence_ids]
    source = next(value for value in draft.components if not value.plan_occurrence_ids)
    final = build_takeoff(
        sheets,
        entities,
        occurrences,
        edges,
        materials=[_material()],
        confirmations={target.id: {"merge_component_ids": source.id} for target in targets},
    )
    assert len(final.components) == 3
    target_items = [
        value for value in final.items if value.component_id in {item.id for item in targets}
    ]
    assert len(target_items) == 2
    assert all(value.status == ReviewStatus.BLOCK for value in target_items)
    assert all(
        "claimed by multiple targets" in (value.block_reason or "") for value in target_items
    )


def test_dense_sheet_measurement_candidates_are_bounded_and_audited():
    sheets, occurrences, edges, entities = _fixture()
    entities = [
        value for value in entities if value.id not in {"length-dim", "quantity-text"}
    ]
    entities.extend(
        CadEntity(
            id=f"dense-length-{index}",
            source_file_id="f",
            sheet_id="elevation",
            entity_type="DIMENSION",
            space="model",
            value=1_000 + index,
            insert=(50 + index / 100, 50),
            geometry={"units": "millimeters"},
        )
        for index in range(100)
    )

    result = build_takeoff(sheets, entities, occurrences, edges, materials=[_material()])
    lengths = [value for value in result.measurements if value.role == "length"]

    assert len(lengths) == 24
    assert all(
        any(marker == "candidate_pool_truncated:100->24" for marker in value.basis)
        for value in lengths
    )
    assert any(issue.code == "MEASUREMENT_CANDIDATES_TRUNCATED" for issue in result.issues)
    assert result.items[0].status == ReviewStatus.BLOCK


def test_explicit_linked_sheet_labels_remain_reviewable_outside_local_radius():
    sheets, occurrences, edges, _ = _fixture()
    far_bbox = (1_990, 1_990, 2_100, 2_020)
    entities = [
        CadEntity(
            id="far-length",
            source_file_id="f",
            sheet_id="elevation",
            entity_type="TEXT",
            space="model",
            text="长度=1200mm L=1200mm",
            insert=(2_000, 2_000),
            bbox=far_bbox,
        ),
        CadEntity(
            id="far-quantity",
            source_file_id="f",
            sheet_id="elevation",
            entity_type="TEXT",
            space="model",
            text="QTY=2",
            insert=(2_000, 2_000),
            bbox=far_bbox,
        ),
        CadEntity(
            id="explicit-unfolded",
            source_file_id="f",
            sheet_id="detail",
            entity_type="TEXT",
            space="model",
            text="展开宽度=200mm",
            insert=(2_000, 2_000),
            bbox=far_bbox,
        ),
    ]

    result = build_takeoff(sheets, entities, occurrences, edges, materials=[_material()])
    by_role = {value.role: value for value in result.measurements}

    assert by_role["length"].numeric_value == 1200
    assert by_role["length"].sheet_id == "elevation"
    assert "linked_sheet_labeled_fallback" in by_role["length"].basis
    assert by_role["quantity"].numeric_value == 2
    assert by_role["quantity"].sheet_id == "elevation"
    assert "linked_sheet_labeled_fallback" in by_role["quantity"].basis
    assert by_role["unfolded_spec"].numeric_value == 200
    assert by_role["unfolded_spec"].sheet_id == "detail"
