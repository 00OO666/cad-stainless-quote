import pytest
from cadquote.models import (
    CadEntity,
    EvidenceEdge,
    MaterialSpec,
    MtOccurrence,
    ReviewStatus,
    Sheet,
)
from cadquote.takeoff import build_component_instances, build_takeoff


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


def test_mt_occurrence_count_is_not_used_as_quantity():
    sheets, occurrences, edges, entities = _fixture()
    entities = [entity for entity in entities if entity.id != "quantity-text"]
    draft = build_takeoff(sheets, entities, occurrences, edges)
    assert draft.items[0].quantity is None
    assert draft.items[0].status == ReviewStatus.BLOCK


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
    assert "outside selected chain" in (item.block_reason or "")


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
