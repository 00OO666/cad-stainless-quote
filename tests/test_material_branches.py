import copy
import math

import pytest
from cadquote.material_branches import discover_material_leader_branches
from cadquote.models import CadEntity, MtOccurrence


def entity(key, kind, **kwargs):
    return CadEntity(
        id=key,
        handle=key,
        entity_type=kind,
        source_file_id="synthetic",
        sheet_id="sheet",
        space="model",
        layer="0",
        **kwargs,
    )


def fixture():
    frame = entity(
        "frame",
        "INSERT",
        geometry={
            "annotation_boundary": [[0, 0], [40, 0], [40, 12], [0, 12]],
        },
    )
    label = entity("label", "ATTRIB", text="MT-09", geometry={"parent_insert_handle": "frame"})
    a = entity("arrow-a", "LEADER", geometry={"vertices": [[-50, 30], [-20, 6], [0, 6]]})
    b = entity("arrow-b", "LEADER", geometry={"vertices": [[-70, -25], [-20, 6], [0, 6]]})
    occ = MtOccurrence(
        id="occ",
        source_file_id="synthetic",
        sheet_id="sheet",
        mt_code="MT-09",
        entity_ids=["label"],
        anchor=(20, 6),
    )
    return [frame, label, a, b], [occ]


def test_two_native_branches_are_preserved_without_creating_occurrences_or_quantity():
    entities, occurrences = fixture()
    before = copy.deepcopy((entities, occurrences))
    result = discover_material_leader_branches(entities, occurrences)
    branches = result["records"][0]["branches"]
    assert len(branches) == 2
    assert {tuple(b["leader_target"]) for b in branches} == {(-50, 30), (-70, -25)}
    assert all(b["geometry_routing_eligible"] and b["physical_quantity"] is None for b in branches)
    assert all(b["measurement_role"] is None and b["state"] == "REVIEW" for b in branches)
    assert not result["creates_occurrences"] and not result["mutates_takeoff"]
    assert occurrences[0].leader_target is None and (entities, occurrences) == before


def test_branch_ids_are_order_independent():
    entities, occ = fixture()
    first = discover_material_leader_branches(entities, occ)
    second = discover_material_leader_branches(list(reversed(entities)), occ)
    assert first == second


@pytest.mark.parametrize(
    "field,value", [("source_file_id", "other"), ("sheet_id", "other"), ("space", "paper:Other")]
)
def test_other_scope_cannot_join_annotation(field, value):
    entities, occ = fixture()
    entities[2] = entities[2].model_copy(update={field: value})
    result = discover_material_leader_branches(entities, occ)
    assert len(result["records"][0]["branches"]) == 1


@pytest.mark.parametrize("index", [0, 1, 2])
def test_hidden_annotations_or_leaders_not_connected(index):
    entities, occ = fixture()
    entities[index].geometry["semantic_hidden"] = True
    data = discover_material_leader_branches(entities, occ)
    assert len(data["records"][0]["branches"]) == (1 if index == 2 else 0)


def test_nearby_endpoint_not_treated_as_border_contact():
    entities, occ = fixture()
    entities[2].geometry["vertices"][-1] = [0.01, 6]
    result = discover_material_leader_branches(entities, occ)
    assert len(result["records"][0]["branches"]) == 1


def test_competing_annotation_boxes_retain_conflict_for_every_branch():
    entities, occ = fixture()
    entities.append(entities[0].model_copy(update={"id": "other-box", "handle": "other-box"}))
    data = discover_material_leader_branches(entities, occ)
    assert len(data["records"][0]["branches"]) == 2
    assert all(
        not b["geometry_routing_eligible"] and "AMBIGUOUS_ANNOTATION_OWNER" in b["reason_codes"]
        for b in data["records"][0]["branches"]
    )


@pytest.mark.parametrize("mode", ["attachment", "multi-target", "nan", "unsupported", "duplicate"])
def test_conflicting_or_unsupported_branch_is_not_routed(mode):
    entities, occ = fixture()
    if mode == "attachment":
        entities[2].geometry["annotation_handle"] = "another-label"
    elif mode == "multi-target":
        entities[2].geometry["leader_targets"] = [[-50, 30], [99, 88]]
    elif mode == "nan":
        entities[2].geometry["vertices"][0] = [math.nan, 30]
    elif mode == "unsupported":
        entities[2].entity_type = "MULTILEADER"
    else:
        entities.append(entities[2].model_copy(deep=True))
    data = discover_material_leader_branches(entities, occ)
    eligible = [b for b in data["records"][0]["branches"] if b["geometry_routing_eligible"]]
    assert [b["leader_handle"] for b in eligible] == ["arrow-b"]


@pytest.mark.parametrize(
    "limit", ["max_entities", "max_occurrences", "max_contact_checks", "max_branches"]
)
def test_resource_limits_never_emit_routable_partial_result(limit):
    entities, occ = fixture()
    if limit == "max_occurrences":
        occ.append(occ[0].model_copy(update={"id": "other"}))
    data = discover_material_leader_branches(entities, occ, **{limit: 1})
    assert data["truncated"]
    assert not any(b["geometry_routing_eligible"] for r in data["records"] for b in r["branches"])


def test_duplicate_parent_handles_are_not_resolved_by_first_match():
    entities, occ = fixture()
    entities.append(entities[0].model_copy(update={"id": "duplicate-handle"}))
    data = discover_material_leader_branches(entities, occ)
    assert not data["records"][0]["branches"]


def test_material_branches_do_not_cross_parent_blocks():
    entities, occ = fixture()
    entities[1].geometry["parent_insert_handle"] = "not-frame"
    assert not discover_material_leader_branches(entities, occ)["records"][0]["branches"]
