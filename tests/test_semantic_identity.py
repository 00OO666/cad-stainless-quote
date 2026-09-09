"""Synthetic source/review fixtures; explicit replay trust is never self-certified.

These tests exercise a trusted-record reader at the declared callback boundary,
not a claim that this comparator itself decodes arbitrary CAD or reviews images.
Review facts are frozen before candidate mutations and read from local files.
"""

import hashlib
import json
from copy import deepcopy

import ezdxf
import pytest
from cadquote.semantic_identity import (
    ArtifactRef,
    IdentityValue,
    LocationValue,
    NameValue,
    ReplayResult,
    SemanticRow,
    SemanticSidecar,
    TrustedReplay,
    ViewValue,
    compare_semantic_sidecars,
    compare_typed_values,
    proof_subject,
    validate_sidecar,
)
from pydantic import ValidationError

REVIEW = {
    "reviewer": "synthetic independent reviewer",
    "reviewed_at": "2026-01-01T00:00:00Z",
    "reason": "Original synthetic evidence",
}


def asset(path):
    return {"path": str(path.resolve()), "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}


def write(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
    return asset(path)


def claim(raw, value):
    return {
        "raw_text": raw,
        "state": "SUPPORTED",
        "complete": True,
        "candidates": 1,
        "review": deepcopy(REVIEW),
        "proofs": [],
        "value": value,
    }


def rerecord(case, side, *, proof_kinds=None):
    """Explicit synthetic re-review, never used by production comparison code."""
    sidecar = case[side]
    row = SemanticRow.model_validate(sidecar["rows"][0])
    kinds = {
        "identity": ["FIELD_INTERPRETATION", "NATIVE_INSTANCE_IDENTITY"],
        "name": ["FIELD_INTERPRETATION", "NAME_CLASSIFICATION"],
        "plan_location": ["FIELD_INTERPRETATION", "ROOM_LOCATION"],
        "elevation": [
            "FIELD_INTERPRETATION",
            "NATIVE_TITLE_VIEWPORT_OWNERSHIP",
            "REVIEWED_NATIVE_GEOMETRY_CORRESPONDENCE",
        ],
        "detail": [
            "FIELD_INTERPRETATION",
            "NATIVE_TITLE_VIEWPORT_OWNERSHIP",
            "VERIFIED_NATIVE_CUT_PATH",
        ],
    }
    for field in ("elevation", "detail"):
        value = getattr(row, field).value
        if value and value.reference_kind == "WHOLE_PAGE_VIEW":
            kinds[field][1] = "NATIVE_WHOLE_PAGE_VIEW"
        if value and value.room_direction:
            kinds[field].append("NATIVE_ROOM_DIRECTION")
    for field in kinds:
        if getattr(row, field).alias_ids:
            kinds[field].append("SCOPED_ALIAS")
    if row.plan_location.value and row.plan_location.value.direction_assertions:
        kinds["plan_location"].append("DIRECTION_FRAME")
    if proof_kinds:
        kinds.update(proof_kinds)
    facts = {}
    for field, field_kinds in kinds.items():
        for kind in field_kinds:
            locator = f"{field}/{kind}"
            facts[locator] = {
                "kind": kind,
                "side": side,
                "locator": locator,
                "source_sha256": [s["sha256"] for s in sidecar["sources"]],
                "subject": proof_subject(field, getattr(row, field), kind, row.identity.value),
                "state": "SUPPORTED",
                "complete": True,
                "candidates": 1,
                "review": deepcopy(REVIEW),
            }
    case["generation"][side] += 1
    reviewed = write(case["folder"] / f"{side}-review-{case['generation'][side]}.json", facts)
    for field, field_kinds in kinds.items():
        sidecar["rows"][0][field]["proofs"] = [
            {"kind": kind, "artifact": reviewed, "locator": f"{field}/{kind}"}
            for kind in field_kinds
        ]
    case["approved"][side] = frozenset({reviewed["sha256"]})


@pytest.fixture
def native_case(tmp_path):
    doc = ezdxf.new("R2018")
    doc.units = 4
    model = doc.modelspace()
    body = doc.blocks.new("NATIVE_COUNTER")
    body.add_lwpolyline([(0, 0), (4, 0), (4, 2), (0, 2)], close=True)
    insert = model.add_blockref(body.name, (10, 10))
    model.add_text("ROOM R1", dxfattribs={"insert": (10, 10)})
    viewport_handles = []
    for name, page, local in (
        ("Plan", "PL-01", "01"),
        ("Elevation", "E-03", "03"),
        ("Detail", "GS-01", "07"),
    ):
        paper = doc.layouts.new(name)
        vp = paper.add_viewport(
            center=(50, 50), size=(100, 100), view_center_point=(10, 10), view_height=100
        )
        paper.add_text(page, dxfattribs={"insert": (20, -5)})
        paper.add_text(local, dxfattribs={"insert": (10, -5)})
        viewport_handles.append(vp.dxf.handle)
    source = tmp_path / "original.dxf"
    doc.saveas(source)
    source_ref = asset(source)
    contract = write(tmp_path / "contract.json", {"schema": "synthetic-semantic-contract-v1"})
    instance = {
        "source_sha256": source_ref["sha256"],
        "space": "model",
        "insert_chain": [],
        "entity_handle": insert.dxf.handle,
        "minsert_cell": None,
    }
    identity = {"project_id": "synthetic-project", "instances": [instance], "quote_scope": "steel"}
    name = {
        "kind": "service_counter",
        "variant": "A",
        "quote_scope": "steel",
        "room_qualifier": None,
    }
    location = {
        "project_id": "synthetic-project",
        "building_id": "building-1",
        "level_id": "1F",
        "room_id": "R1",
        "plan_source_sha256": source_ref["sha256"],
        "plan_space": "paper:Plan",
        "plan_page_code": "PL-01",
        "plan_viewport_handle": viewport_handles[0],
        "instances": [instance],
        "direction_assertions": [],
    }
    elevation = {
        "role": "ordinary_elevation",
        "source_sha256": source_ref["sha256"],
        "space": "paper:Elevation",
        "page_code": "E-03",
        "reference_kind": "LOCAL_VIEW",
        "local_view": "03",
        "viewport_handle": viewport_handles[1],
        "room_direction": None,
    }
    detail = {
        **elevation,
        "role": "section",
        "space": "paper:Detail",
        "page_code": "GS-01",
        "local_view": "07",
        "viewport_handle": viewport_handles[2],
    }
    case = {
        "folder": tmp_path,
        "source": source,
        "contract": contract,
        "generation": {"predicted": 0, "gold": 0},
        "approved": {},
    }
    for side in ("predicted", "gold"):
        row = {
            "row_id": f"{side}-local-id",
            "identity": claim("original native instance", deepcopy(identity)),
            "name": claim("服务台A" if side == "predicted" else "大厅服务台-A", deepcopy(name)),
            "plan_location": claim(
                "1F大堂A台" if side == "predicted" else "大厅/服务台A", deepcopy(location)
            ),
            "elevation": claim("03/E-03", deepcopy(elevation)),
            "detail": claim("07/GS-01", deepcopy(detail)),
        }
        row["elevation"]["raw_reference_role"] = "LOCAL_VIEW"
        row["detail"]["raw_reference_role"] = "LOCAL_VIEW"
        if side == "gold":
            row["name"]["value"]["room_qualifier"] = "R1"
        original = write(tmp_path / f"{side}-original.json", {"rows": [row]})
        case[side] = {
            "schema_version": "cad-native-semantic-claims/1",
            "side": side,
            "original_artifact": original,
            "contract": contract,
            "sources": [source_ref],
            "rows": [row],
        }
        rerecord(case, side)
    return case


def provider(case, mutate=None):
    def replay(proof, context):
        # This reader only sees the actual approved file and its own provenance.
        # It has no candidate claim, desired subject, desired view or gold row.
        facts = json.loads(open(proof.artifact.path, encoding="utf-8").read())
        actual = facts[proof.locator]
        actual.update(
            artifact_sha256=proof.artifact.sha256,
            original_artifact_sha256=context.original_artifact.sha256,
            contract_sha256=context.contract_sha256,
        )
        if mutate:
            mutate(actual)
        return ReplayResult.model_validate(actual)

    return TrustedReplay("synthetic independent artifact reader", case["approved"], replay)


def compare(case, **kwargs):
    return compare_semantic_sidecars(
        case["predicted"],
        case["gold"],
        contract=case["contract"],
        trusted_replay=kwargs.get("trusted", provider(case)),
    )


def status(case, field, **kwargs):
    result = compare(case, **kwargs)
    return result["matches"][0]["fields"][field]["status"]


def test_S01_optional_room_prefix_and_S03_location_paraphrase(native_case):
    result = compare(native_case)
    assert all(f["status"] == "PASS" for f in result["matches"][0]["fields"].values())
    assert result["accuracy_claim"] is None and result["whole_row_acceptance"] is None
    assert result["legacy_policy_modified"] is False


def test_S02_wrong_variant_is_fail(native_case):
    native_case["predicted"]["rows"][0]["name"]["value"]["variant"] = "B"
    rerecord(native_case, "predicted")
    assert status(native_case, "name") == "FAIL"


def test_S04_wrong_room_is_fail_and_name_qualifier_constrains_both_sides(native_case):
    native_case["predicted"]["rows"][0]["plan_location"]["value"]["room_id"] = "R2"
    rerecord(native_case, "predicted")
    assert status(native_case, "plan_location") == "FAIL"
    assert status(native_case, "name") == "FAIL"


def test_S05_explicit_tuple_syntax(native_case):
    native_case["predicted"]["rows"][0]["elevation"]["raw_text"] = "E-03（03号）"
    rerecord(native_case, "predicted")
    assert status(native_case, "elevation") == "PASS"


@pytest.mark.parametrize("local,page", [("04", "E-03"), ("03a", "E-03a")])
def test_S06_S07_wrong_local_or_suffix_never_passes(native_case, local, page):
    claim = native_case["predicted"]["rows"][0]["elevation"]
    claim["value"].update(local_view=local, page_code=page)
    claim["raw_text"] = f"{local}/{page}"
    rerecord(native_case, "predicted")
    assert status(native_case, "elevation") == "FAIL"


def test_S08_missing_local_is_unresolved_even_when_claim_self_says_supported(native_case):
    c = native_case["predicted"]["rows"][0]["elevation"]
    c["value"]["local_view"] = None
    c["raw_text"] = "E-03"
    c["raw_reference_role"] = "PAGE_ONLY"
    rerecord(native_case, "predicted")
    assert status(native_case, "elevation") == "UNRESOLVED"


def test_S09_native_local_completion_does_not_require_display_token(native_case):
    c = native_case["predicted"]["rows"][0]["elevation"]
    c["raw_text"] = "E-03"
    c["raw_reference_role"] = "PAGE_ONLY"
    rerecord(native_case, "predicted")
    assert status(native_case, "elevation") == "PASS"


def test_S10_distinct_instances_never_pair_by_same_names_or_row_ids(native_case):
    p = native_case["predicted"]["rows"][0]
    p["row_id"] = native_case["gold"]["rows"][0]["row_id"]
    original = deepcopy(p["identity"]["value"])
    p["identity"]["value"]["instances"][0]["entity_handle"] = "DIFFERENT_INSTANCE"
    p["plan_location"]["value"]["instances"][0]["entity_handle"] = "DIFFERENT_INSTANCE"
    rerecord(native_case, "predicted")
    assert compare(native_case)["matches"] == []
    assert (
        compare_typed_values(
            IdentityValue.model_validate(original),
            IdentityValue.model_validate(p["identity"]["value"]),
        )
        == "FAIL"
    )


def test_S11_explicit_raw_suffix_conflict_cannot_be_reviewer_washed(native_case):
    native_case["predicted"]["rows"][0]["elevation"]["raw_text"] = "03a/E-03a"
    rerecord(native_case, "predicted")
    assert status(native_case, "elevation") == "FAIL"


def test_S12_different_revision_same_handle_is_unresolved(native_case):
    a = ViewValue.model_validate(native_case["predicted"]["rows"][0]["elevation"]["value"])
    b = a.model_copy(update={"source_sha256": "a" * 64})
    assert compare_typed_values(a, b) == "UNRESOLVED"


def test_S13_ambiguous_scoped_alias_is_unresolved(native_case):
    native_case["predicted"]["rows"][0]["plan_location"]["alias_ids"] = ["project-R1-lobby"]
    rerecord(native_case, "predicted")

    def ambiguity(actual):
        if actual["kind"] == "SCOPED_ALIAS":
            actual["candidates"] = 2

    assert (
        status(native_case, "plan_location", trusted=provider(native_case, ambiguity))
        == "UNRESOLVED"
    )


def test_S14_unproven_screen_up_is_not_world_north(native_case):
    c = native_case["predicted"]["rows"][0]["plan_location"]
    c["value"]["direction_assertions"] = ["world:north"]
    rerecord(
        native_case,
        "predicted",
        proof_kinds={"plan_location": ["FIELD_INTERPRETATION", "ROOM_LOCATION"]},
    )
    assert status(native_case, "plan_location") == "UNRESOLVED"


def test_optional_native_frame_assertion_does_not_require_equal_verbosity(native_case):
    c = native_case["predicted"]["rows"][0]["plan_location"]
    c["value"]["direction_assertions"] = ["screen:plan-viewport:upper"]
    rerecord(native_case, "predicted")
    assert status(native_case, "plan_location") == "PASS"


def test_wrong_direction_conflicts_with_independent_frame_replay(native_case):
    c = native_case["predicted"]["rows"][0]["plan_location"]
    c["value"]["direction_assertions"] = ["world:north"]
    rerecord(native_case, "predicted")

    def actual_south(actual):
        if actual["kind"] == "DIRECTION_FRAME":
            actual["subject"]["value"]["direction_assertions"] = ["world:south"]

    assert (
        status(native_case, "plan_location", trusted=provider(native_case, actual_south)) == "FAIL"
    )


def test_S15_different_quote_scope_is_not_same_row(native_case):
    a = IdentityValue.model_validate(native_case["predicted"]["rows"][0]["identity"]["value"])
    b = a.model_copy(update={"quote_scope": "whole_host_with_stone"})
    assert compare_typed_values(a, b) == "FAIL"


def test_S16_geometry_identity_survives_missing_individual_direct_route(native_case):
    result = compare(native_case)
    assert result["matches"][0]["fields"]["elevation"]["status"] == "PASS"
    audit = result["validation"]["predicted"]["rows"][0]["fields"]["elevation"]
    assert audit["direct_component_route"] == "UNRESOLVED"


def test_S17_room_context_does_not_supply_detail_cut_proof(native_case):
    p = native_case["predicted"]["rows"][0]["detail"]
    p["proofs"] = [x for x in p["proofs"] if x["kind"] != "VERIFIED_NATIVE_CUT_PATH"]
    assert status(native_case, "detail") == "UNRESOLVED"


def add_direction(case, page="E-03", room="R1"):
    c = case["predicted"]["rows"][0]["elevation"]
    c["value"]["room_direction"] = {
        "source_sha256": case["predicted"]["sources"][0]["sha256"],
        "space": "paper:Plan",
        "parent_handle": "DIRECTION_PARENT",
        "room_id": room,
        "direction_token": "03",
        "page_code": page,
    }
    c["raw_reference_role"] = "ROOM_DIRECTION"
    rerecord(case, "predicted")


def test_S18_optional_room_direction_kept_separate_from_view_identity(native_case):
    add_direction(native_case)
    result = compare(native_case)
    assert result["matches"][0]["fields"]["elevation"]["status"] == "PASS"
    proofs = result["validation"]["predicted"]["rows"][0]["fields"]["elevation"]["proofs"]
    assert next(p for p in proofs if p["kind"] == "NATIVE_ROOM_DIRECTION")["state"] == "SUPPORTED"


@pytest.mark.parametrize("page,room", [("E-04", "R1"), ("E-03", "R2")])
def test_room_direction_must_not_conflict_with_target_or_room(native_case, page, room):
    add_direction(native_case, page, room)
    assert status(native_case, "elevation") == "FAIL"


def whole_page(case, side):
    c = case[side]["rows"][0]["elevation"]
    c["value"].update(reference_kind="WHOLE_PAGE_VIEW", local_view=None)
    c["raw_text"] = "E-03"
    c["raw_reference_role"] = "PAGE_ONLY"
    rerecord(case, side)


def test_S19_complete_native_whole_page_is_supported(native_case):
    whole_page(native_case, "predicted")
    whole_page(native_case, "gold")
    assert status(native_case, "elevation") == "PASS"


def test_S20_whole_page_requires_complete_native_inventory_proof(native_case):
    whole_page(native_case, "predicted")
    whole_page(native_case, "gold")

    def incomplete(actual):
        if actual["kind"] == "NATIVE_WHOLE_PAGE_VIEW":
            actual["complete"] = False

    assert (
        status(native_case, "elevation", trusted=provider(native_case, incomplete)) == "UNRESOLVED"
    )


def test_whole_page_direction_number_is_not_local_subview_number(native_case):
    whole_page(native_case, "predicted")
    whole_page(native_case, "gold")
    add_direction(native_case)
    native_case["predicted"]["rows"][0]["elevation"]["raw_text"] = "03/E-03"
    rerecord(native_case, "predicted")
    assert status(native_case, "elevation") == "PASS"


def test_unproved_whole_page_raw_prefix_is_not_silently_deleted(native_case):
    whole_page(native_case, "predicted")
    whole_page(native_case, "gold")
    native_case["predicted"]["rows"][0]["elevation"]["raw_text"] = "03/E-03"
    rerecord(native_case, "predicted")
    assert status(native_case, "elevation") == "FAIL"


def test_hash_only_or_missing_trust_never_supplies_semantic_proof(native_case):
    result = compare(native_case, trusted=None)
    assert result["matches"] == []
    assert all(
        f["state"] == "UNRESOLVED"
        for f in result["validation"]["predicted"]["rows"][0]["fields"].values()
    )


def test_sidecar_cannot_supply_its_own_trusted_replay(native_case):
    native_case["predicted"]["trusted_replay"] = {"approved": True}
    with pytest.raises(ValidationError):
        SemanticSidecar.model_validate(native_case["predicted"])


@pytest.mark.parametrize("field", ["engineering_quantity", "amount", "target_gold_row"])
def test_no_numeric_or_target_id_schema_escape(native_case, field):
    native_case["predicted"]["rows"][0][field] = 1
    with pytest.raises(ValidationError):
        SemanticSidecar.model_validate(native_case["predicted"])


def test_same_bytes_declared_on_wrong_side_rejected(native_case):
    with pytest.raises(ValueError, match="wrong semantic side"):
        validate_sidecar(
            native_case["gold"],
            expected_side="predicted",
            contract=native_case["contract"],
            trusted_replay=provider(native_case),
        )


@pytest.mark.parametrize("which", ["source", "original", "review", "contract"])
def test_source_and_all_artifacts_must_remain_fresh(native_case, which):
    from pathlib import Path

    p = native_case["predicted"]
    path = {
        "source": native_case["source"],
        "original": Path(p["original_artifact"]["path"]),
        "review": Path(p["rows"][0]["name"]["proofs"][0]["artifact"]["path"]),
        "contract": Path(p["contract"]["path"]),
    }[which]
    path.write_bytes(path.read_bytes() + b" changed")
    assert compare(native_case)["matches"] == []


def test_replay_subject_is_not_echoed_from_candidate(native_case):
    native_case["predicted"]["rows"][0]["name"]["value"]["variant"] = "B"
    assert status(native_case, "name") == "FAIL"


def test_replay_requires_independent_approval_allowlist(native_case):
    trust = provider(native_case)
    trust = TrustedReplay(
        trust.provider_id, {"gold": native_case["approved"]["gold"]}, trust.replay
    )
    assert compare(native_case, trusted=trust)["matches"] == []


def test_replay_source_or_contract_binding_cannot_drift(native_case):
    def wrong_contract(actual):
        actual["contract_sha256"] = "f" * 64

    assert compare(native_case, trusted=provider(native_case, wrong_contract))["matches"] == []


def test_later_side_replay_cannot_change_previously_checked_side_asset(native_case):
    from pathlib import Path

    old = Path(native_case["predicted"]["original_artifact"]["path"])

    def mutate_previous_side(actual):
        if actual["side"] == "gold":
            old.write_bytes(old.read_bytes() + b"changed after predicted validation")

    result = compare(native_case, trusted=provider(native_case, mutate_previous_side))
    assert result["matches"] == []
    assert (
        "INPUT_ASSET_CHANGED_DURING_COMPARISON"
        in result["validation"]["predicted"]["envelope_issues"]
    )


def test_duplicate_identity_blocks_all_duplicates_not_pair_by_sequence(native_case):
    second = deepcopy(native_case["predicted"]["rows"][0])
    second["row_id"] = "second-local-id"
    native_case["predicted"]["rows"].append(second)
    result = compare(native_case)
    assert result["matches"] == [] and len(result["duplicates"]) == 1
    assert len(result["unmatched"]["predicted"]) == 2


def test_full_insert_chain_is_part_of_identity(native_case):
    a = IdentityValue.model_validate(native_case["predicted"]["rows"][0]["identity"]["value"])
    b = a.model_copy(deep=True)
    b.instances[0].insert_chain = ["ANOTHER_PARENT"]
    assert compare_typed_values(a, b) == "FAIL"


def test_timezone_required(native_case):
    native_case["predicted"]["rows"][0]["name"]["review"]["reviewed_at"] = "2026-01-01"
    with pytest.raises(ValidationError):
        SemanticSidecar.model_validate(native_case["predicted"])


def test_absolute_local_artifact_only():
    with pytest.raises(ValidationError):
        ArtifactRef.model_validate({"path": "https://example.test/review", "sha256": "a" * 64})


def test_no_fuzzy_name_or_location_equality(native_case):
    n = NameValue.model_validate(native_case["predicted"]["rows"][0]["name"]["value"])
    assert compare_typed_values(n, n.model_copy(update={"kind": "service_countre"})) == "FAIL"
    loc = LocationValue.model_validate(
        native_case["predicted"]["rows"][0]["plan_location"]["value"]
    )
    assert compare_typed_values(loc, loc.model_copy(update={"level_id": "2F"})) == "FAIL"
