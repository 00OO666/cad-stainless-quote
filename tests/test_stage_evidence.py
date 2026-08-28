import hashlib
from copy import deepcopy
from pathlib import Path

from cadquote.stage_evidence import build_stage_evidence_manifest
from PIL import Image


def _write_png(path: Path, color: tuple[int, int, int]) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (12, 10), color).save(path, format="PNG")
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _fixtures(tmp_path: Path):
    panel_payload = {
        "sheets": [
            {
                "id": "panel:plan",
                "kind": "plan",
                "drawing_number": "P-01",
                "title": "平面",
                "bbox": [0, 0, 100, 100],
            },
            {
                "id": "panel:elevation",
                "kind": "elevation",
                "drawing_number": "E-01",
                "title": "立面",
                "bbox": [100, 100, 300, 300],
            },
            {
                "id": "panel:detail-a",
                "kind": "detail",
                "drawing_number": "D-01",
                "title": "节点A",
                "bbox": [0, 0, 80, 80],
            },
            {
                "id": "panel:detail-b",
                "kind": "detail",
                "drawing_number": "D-02",
                "title": "节点B",
                "bbox": [0, 0, 80, 80],
            },
        ],
        "entities": [
            {
                "id": "dimension:1",
                "sheet_id": "panel:detail-a",
                "entity_type": "DIMENSION",
            },
            {
                "id": "dimension:2",
                "sheet_id": "panel:detail-b",
                "entity_type": "DIMENSION",
            },
            {
                "id": "dimension:elsewhere",
                "sheet_id": "panel:plan",
                "entity_type": "DIMENSION",
            },
            {
                "id": "line:detail-a",
                "sheet_id": "panel:detail-a",
                "entity_type": "LINE",
            },
        ],
    }
    edges = [
        {
            "id": "edge:plan",
            "relation": "plan_to_elevation",
            "source_id": "panel:plan",
            "target_id": "panel:elevation",
            "basis": ["view_reference:E-01@panel_paper_entity:aaaa"],
            "confidence": 0.9,
            "status": "REVIEW",
        },
        {
            "id": "edge:detail-a",
            "relation": "elevation_to_detail",
            "source_id": "panel:elevation",
            "target_id": "panel:detail-a",
            "basis": ["explicit_reference:D-01@panel_paper_entity:bbbb"],
            "confidence": 0.95,
            "status": "REVIEW",
        },
        {
            "id": "edge:detail-b",
            "relation": "elevation_to_detail",
            "source_id": "panel:elevation",
            "target_id": "panel:detail-b",
            "basis": ["same_mt:MT-01"],
            "confidence": 0.99,
            "status": "REVIEW",
        },
    ]
    evidence_root = tmp_path / "selected"
    locator_hash = _write_png(evidence_root / "locator.png", (20, 40, 60))
    closeup_hash = _write_png(evidence_root / "closeup.png", (30, 50, 70))
    selected = {
        "records": [
            {
                "selection_key": "component:1",
                "sequence": 1,
                "name": "门套",
                "room_or_location": "前厅",
                "evidence": [
                    {
                        "sheet_id": "panel:elevation",
                        "selected_occurrence_ids": ["occurrence:1"],
                        "locator_image": "locator.png",
                        "closeup_image": "closeup.png",
                        "locator_sha256": locator_hash,
                        "closeup_sha256": closeup_hash,
                        "object_bbox": [120, 120, 220, 260],
                    }
                ],
            }
        ]
    }
    colors = {
        "panel:plan": (80, 10, 10),
        "panel:elevation": (10, 80, 10),
        "panel:detail-a": (10, 10, 80),
        "panel:detail-b": (70, 70, 10),
    }
    catalog = {"panels": {}}
    for sheet in panel_payload["sheets"]:
        path = tmp_path / f"{sheet['id'].replace(':', '-')}.png"
        catalog["panels"][sheet["id"]] = {
            "absolute_path": str(path),
            "image_sha256": _write_png(path, colors[sheet["id"]]),
            "render_profile": "cad-dark-full",
        }
    closeups = {
        "plan": (
            evidence_root / "plan-closeup.png",
            _write_png(evidence_root / "plan-closeup.png", (90, 20, 20)),
        ),
        "detail": (
            evidence_root / "detail-closeup.png",
            _write_png(evidence_root / "detail-closeup.png", (20, 20, 90)),
        ),
    }
    base_selection = {
        "component_id": "component:1",
        "sequence": 1,
        "name": "门套",
        "room_or_location": "前厅",
        "mt_code": "MT-01",
    }
    return (
        panel_payload,
        edges,
        selected,
        catalog,
        evidence_root,
        base_selection,
        closeups,
    )


def _build(
    tmp_path: Path,
    panel_payload,
    edges,
    selected,
    catalog,
    selection,
    evidence_root,
    *,
    suffix: str,
):
    return build_stage_evidence_manifest(
        panel_payload,
        edges,
        selected,
        catalog,
        [selection],
        tmp_path / f"out-{suffix}",
        selected_evidence_root=evidence_root,
    )


def _candidate(record, stage: str, sheet_id: str):
    return next(
        value for value in record["stages"][stage]["candidates"] if value["sheet_id"] == sheet_id
    )


def _review(minute: int = 0):
    return {
        "reviewer": "reviewer-1",
        "reviewed_at": f"2026-08-28T10:{minute:02d}:00+08:00",
        "reason": "checked against drawing references",
    }


def _nested_states(value):
    if isinstance(value, dict):
        if "state" in value:
            yield value["state"]
        for child in value.values():
            yield from _nested_states(child)
    elif isinstance(value, list):
        for child in value:
            yield from _nested_states(child)


def _discover(fixture, tmp_path: Path, *, selection=None, suffix="discover"):
    panel_payload, edges, selected, catalog, evidence_root, base, _closeups = fixture
    chosen = deepcopy(selection or base)
    return _build(
        tmp_path,
        panel_payload,
        edges,
        selected,
        catalog,
        chosen,
        evidence_root,
        suffix=suffix,
    )["records"][0]


def _complete_selectors(record, closeups, *, detail_sheet="panel:detail-a"):
    plan = _candidate(record, "plan", "panel:plan")
    elevation = _candidate(record, "elevation", "panel:elevation")
    detail = _candidate(record, "detail", detail_sheet)
    plan_closeup, plan_hash = closeups["plan"]
    detail_closeup, detail_hash = closeups["detail"]
    measurement = "dimension:1" if detail_sheet == "panel:detail-a" else "dimension:2"
    return [
        {
            "stage": "plan",
            "candidate_id": plan["candidate_id"],
            "relation_edge_id": plan["relation_edge_id"],
            "sheet_id": plan["sheet_id"],
            "state": "CONFIRMED",
            "object_bbox": [5, 5, 95, 95],
            "closeup_image": str(plan_closeup),
            "closeup_sha256": plan_hash,
            "review": _review(0),
        },
        {
            "stage": "elevation",
            "candidate_id": elevation["candidate_id"],
            "sheet_id": elevation["sheet_id"],
            "state": "CONFIRMED",
            "object_bbox": [120, 120, 220, 260],
            "review": _review(1),
        },
        {
            "stage": "detail",
            "candidate_id": detail["candidate_id"],
            "relation_edge_id": detail["relation_edge_id"],
            "sheet_id": detail["sheet_id"],
            "state": "CONFIRMED",
            "object_bbox": [5, 5, 60, 70],
            "measurement_ids": [measurement],
            "closeup_image": str(detail_closeup),
            "closeup_sha256": detail_hash,
            "review": _review(2),
        },
    ]


def test_audited_not_applicable_detail_can_complete_plan_elevation_chain(tmp_path: Path):
    fixture = _fixtures(tmp_path)
    panel_payload, edges, selected, catalog, evidence_root, selection, closeups = fixture
    discovery = _discover(fixture, tmp_path, suffix="discover-no-detail")
    selectors = _complete_selectors(discovery, closeups)
    selectors[-1] = {
        "stage": "detail",
        "state": "NOT_APPLICABLE",
        "kind": "not_applicable",
        "basis": "立面已包含全部计量尺寸，且索引未指向节点或大样",
        "searched_sheet_ids": ["panel:elevation"],
        "review": _review(2),
    }
    selection["stages"] = selectors

    record = _build(
        tmp_path,
        panel_payload,
        edges,
        selected,
        catalog,
        selection,
        evidence_root,
        suffix="confirmed-no-detail",
    )["records"][0]

    assert record["state"] == "CONFIRMED"
    assert record["stages"]["plan"]["state"] == "CONFIRMED"
    assert record["stages"]["elevation"]["state"] == "CONFIRMED"
    assert record["stages"]["detail"]["state"] == "NOT_APPLICABLE"
    disposition = record["stages"]["detail"]["selected"][0]
    assert disposition["candidate_id"] == "stage-disposition:detail-not-applicable"
    assert disposition["context_image"] is None
    assert disposition["closeup_image"] is None


def test_not_applicable_detail_search_must_cover_selected_elevation(tmp_path: Path):
    fixture = _fixtures(tmp_path)
    panel_payload, edges, selected, catalog, evidence_root, selection, closeups = fixture
    discovery = _discover(fixture, tmp_path, suffix="discover-bad-no-detail")
    selectors = _complete_selectors(discovery, closeups)
    selectors[-1] = {
        "stage": "detail",
        "state": "NOT_APPLICABLE",
        "kind": "not_applicable",
        "basis": "错误地只检查了平面",
        "searched_sheet_ids": ["panel:plan"],
        "review": _review(2),
    }
    selection["stages"] = selectors

    record = _build(
        tmp_path,
        panel_payload,
        edges,
        selected,
        catalog,
        selection,
        evidence_root,
        suffix="bad-no-detail",
    )["records"][0]

    assert record["state"] == "BLOCK"
    assert (
        "NOT_APPLICABLE_DETAIL_SEARCH_MUST_COVER_SELECTED_ELEVATION"
        in record["stages"]["detail"]["reason_codes"]
    )


def test_stage_evidence_never_takes_first_candidate(tmp_path: Path):
    fixture = _fixtures(tmp_path)
    record = _discover(fixture, tmp_path)

    assert record["state"] == "REVIEW"
    assert record["stages"]["plan"]["selected"] == []
    assert record["stages"]["elevation"]["selected"] == []
    assert record["stages"]["detail"]["selected"] == []
    assert [value["sheet_id"] for value in record["stages"]["detail"]["candidates"]] == [
        "panel:detail-a"
    ]
    assert record["stages"]["detail"]["candidates"][0]["reference_entity_ids"] == [
        "panel_paper_entity:bbbb"
    ]


def test_plan_occurrence_discovers_elevation_pivot_and_detail_candidates(tmp_path: Path):
    fixture = _fixtures(tmp_path)
    selected = fixture[2]
    selected["records"][0]["evidence"][0].update(
        {
            "sheet_id": "panel:plan",
            "selected_occurrence_ids": ["occurrence:plan"],
            "object_bbox": [10, 10, 90, 90],
        }
    )

    record = _discover(fixture, tmp_path, suffix="plan-anchor")

    assert _candidate(record, "elevation", "panel:elevation")["source"] == (
        "relation_pivot_candidate"
    )
    assert _candidate(record, "detail", "panel:detail-a")["relation_edge_id"] == (
        "edge:detail-a"
    )
    relation_plan = [
        candidate
        for candidate in record["stages"]["plan"]["candidates"]
        if candidate.get("relation_edge_id") == "edge:plan"
    ]
    assert len(relation_plan) == 1
    assert record["state"] == "REVIEW"


def test_detail_occurrence_discovers_elevation_pivot_and_plan_candidates(tmp_path: Path):
    fixture = _fixtures(tmp_path)
    selected = fixture[2]
    selected["records"][0]["evidence"][0].update(
        {
            "sheet_id": "panel:detail-a",
            "selected_occurrence_ids": ["occurrence:detail"],
            "object_bbox": [10, 10, 70, 70],
        }
    )

    record = _discover(fixture, tmp_path, suffix="detail-anchor")

    assert _candidate(record, "elevation", "panel:elevation")["source"] == (
        "relation_pivot_candidate"
    )
    assert _candidate(record, "plan", "panel:plan")["relation_edge_id"] == "edge:plan"
    relation_details = [
        candidate
        for candidate in record["stages"]["detail"]["candidates"]
        if candidate.get("relation_edge_id") == "edge:detail-a"
    ]
    assert len(relation_details) == 1
    assert record["state"] == "REVIEW"


def test_stage_evidence_confirms_only_verified_connected_complete_chain(tmp_path: Path):
    fixture = _fixtures(tmp_path)
    panel_payload, edges, selected, catalog, evidence_root, selection, closeups = fixture
    discovery = _discover(fixture, tmp_path)
    selection["stages"] = _complete_selectors(discovery, closeups)
    result = _build(
        tmp_path,
        panel_payload,
        edges,
        selected,
        catalog,
        selection,
        evidence_root,
        suffix="confirmed",
    )

    record = result["records"][0]
    assert record["state"] == "CONFIRMED"
    assert result["confirmed_chain_count"] == 1
    assert result["path_scope"] == "local_run_diagnostics"
    assert record["stages"]["detail"]["selected"][0]["measurement_ids"] == ["dimension:1"]


def test_empty_or_partial_explicit_selectors_block_every_stage(tmp_path: Path):
    fixture = _fixtures(tmp_path)
    panel_payload, edges, selected, catalog, evidence_root, selection, _closeups = fixture
    discovery = _discover(fixture, tmp_path)
    detail = _candidate(discovery, "detail", "panel:detail-a")
    selection["stages"] = [
        {"stage": "plan"},
        {"stage": "elevation"},
        {"stage": "detail", "candidate_id": detail["candidate_id"]},
    ]
    record = _build(
        tmp_path,
        panel_payload,
        edges,
        selected,
        catalog,
        selection,
        evidence_root,
        suffix="empty-selectors",
    )["records"][0]

    assert record["state"] == "BLOCK"
    for stage in ("plan", "elevation", "detail"):
        assert record["stages"][stage]["reason_codes"] == ["EXPLICIT_STAGE_SELECTOR_INCOMPLETE"]


def test_stage_evidence_rejects_confirmation_without_review_metadata(tmp_path: Path):
    fixture = _fixtures(tmp_path)
    panel_payload, edges, selected, catalog, evidence_root, selection, _closeups = fixture
    discovery = _discover(fixture, tmp_path)
    elevation = _candidate(discovery, "elevation", "panel:elevation")
    selection["stages"] = [
        {
            "stage": "elevation",
            "candidate_id": elevation["candidate_id"],
            "state": "CONFIRMED",
            "object_bbox": [120, 120, 220, 260],
        }
    ]
    record = _build(
        tmp_path,
        panel_payload,
        edges,
        selected,
        catalog,
        selection,
        evidence_root,
        suffix="missing-review",
    )["records"][0]

    assert record["stages"]["elevation"]["state"] == "BLOCK"
    assert (
        "CONFIRMED_STAGE_REQUIRES_REVIEW_METADATA" in record["stages"]["elevation"]["reason_codes"]
    )


def test_stage_evidence_rejects_naive_or_invalid_review_timestamp(tmp_path: Path):
    fixture = _fixtures(tmp_path)
    panel_payload, edges, selected, catalog, evidence_root, selection, _closeups = fixture
    discovery = _discover(fixture, tmp_path)
    elevation = _candidate(discovery, "elevation", "panel:elevation")
    selection["stages"] = [
        {
            "stage": "elevation",
            "candidate_id": elevation["candidate_id"],
            "state": "CONFIRMED",
            "object_bbox": [120, 120, 220, 260],
            "review": {
                "reviewer": "reviewer-1",
                "reviewed_at": "2026-08-28T10:00:00",
                "reason": "timezone was omitted",
            },
        }
    ]
    record = _build(
        tmp_path,
        panel_payload,
        edges,
        selected,
        catalog,
        selection,
        evidence_root,
        suffix="bad-time",
    )["records"][0]

    assert record["stages"]["elevation"]["state"] == "BLOCK"
    assert "CONFIRMED_STAGE_REVIEWED_AT_INVALID" in record["stages"]["elevation"]["reason_codes"]


def test_relation_candidate_without_closeup_cannot_be_confirmed(tmp_path: Path):
    fixture = _fixtures(tmp_path)
    panel_payload, edges, selected, catalog, evidence_root, selection, _closeups = fixture
    discovery = _discover(fixture, tmp_path)
    plan = _candidate(discovery, "plan", "panel:plan")
    selection["stages"] = [
        {
            "stage": "plan",
            "candidate_id": plan["candidate_id"],
            "relation_edge_id": plan["relation_edge_id"],
            "state": "CONFIRMED",
            "object_bbox": [5, 5, 95, 95],
            "review": _review(),
        }
    ]
    record = _build(
        tmp_path,
        panel_payload,
        edges,
        selected,
        catalog,
        selection,
        evidence_root,
        suffix="no-plan-closeup",
    )["records"][0]

    assert (
        "CONFIRMED_STAGE_REQUIRES_CONTEXT_AND_CLOSEUP" in record["stages"]["plan"]["reason_codes"]
    )


def test_confirmed_image_must_exist_and_match_sha256(tmp_path: Path):
    fixture = _fixtures(tmp_path)
    panel_payload, edges, selected, catalog, evidence_root, selection, _closeups = fixture
    discovery = _discover(fixture, tmp_path)
    elevation = _candidate(discovery, "elevation", "panel:elevation")
    selection["stages"] = [
        {
            "stage": "elevation",
            "candidate_id": elevation["candidate_id"],
            "state": "CONFIRMED",
            "object_bbox": [120, 120, 220, 260],
            "closeup_image": "missing.png",
            "closeup_sha256": "0" * 64,
            "context_sha256": "f" * 64,
            "review": _review(),
        }
    ]
    record = _build(
        tmp_path,
        panel_payload,
        edges,
        selected,
        catalog,
        selection,
        evidence_root,
        suffix="bad-images",
    )["records"][0]
    reasons = record["stages"]["elevation"]["reason_codes"]

    assert "CONFIRMED_STAGE_IMAGE_FILE_MISSING" in reasons
    assert "CONFIRMED_STAGE_IMAGE_SHA256_MISMATCH" in reasons


def test_confirmed_image_requires_supported_render_profile(tmp_path: Path):
    fixture = _fixtures(tmp_path)
    panel_payload, edges, selected, catalog, evidence_root, selection, _closeups = fixture
    catalog["panels"]["panel:elevation"]["render_profile"] = "legacy-light"
    discovery = _discover(fixture, tmp_path)
    elevation = _candidate(discovery, "elevation", "panel:elevation")
    selection["stages"] = [
        {
            "stage": "elevation",
            "candidate_id": elevation["candidate_id"],
            "state": "CONFIRMED",
            "object_bbox": [120, 120, 220, 260],
            "review": _review(),
        }
    ]
    record = _build(
        tmp_path,
        panel_payload,
        edges,
        selected,
        catalog,
        selection,
        evidence_root,
        suffix="profile",
    )["records"][0]

    assert "CONFIRMED_STAGE_RENDER_PROFILE_INVALID" in record["stages"]["elevation"]["reason_codes"]


def test_same_image_sha_cannot_serve_as_context_and_closeup(tmp_path: Path):
    fixture = _fixtures(tmp_path)
    panel_payload, edges, selected, catalog, evidence_root, selection, closeups = fixture
    discovery = _discover(fixture, tmp_path, suffix="same-image-within-stage-discovery")
    selectors = _complete_selectors(discovery, closeups)
    plan = selectors[0]
    plan_panel = catalog["panels"]["panel:plan"]
    plan["closeup_image"] = plan_panel["absolute_path"]
    plan["closeup_sha256"] = plan_panel["image_sha256"]
    selection["stages"] = selectors
    result = _build(
        tmp_path,
        panel_payload,
        edges,
        selected,
        catalog,
        selection,
        evidence_root,
        suffix="same-image-within-stage",
    )
    record = result["records"][0]

    assert record["state"] == "BLOCK"
    assert result["confirmed_chain_count"] == 0
    assert record["stages"]["plan"]["state"] == "BLOCK"
    assert "SAME_IMAGE_SHA_REUSED_WITHIN_STAGE_ROLES" in record["stages"]["plan"]["reason_codes"]


def test_same_image_sha_cannot_prove_two_different_stages(tmp_path: Path):
    fixture = _fixtures(tmp_path)
    panel_payload, edges, selected, catalog, evidence_root, selection, closeups = fixture
    discovery = _discover(fixture, tmp_path, suffix="same-image-across-stage-discovery")
    selectors = _complete_selectors(discovery, closeups)
    selectors[2]["closeup_image"] = selectors[0]["closeup_image"]
    selectors[2]["closeup_sha256"] = selectors[0]["closeup_sha256"]
    selection["stages"] = selectors
    result = _build(
        tmp_path,
        panel_payload,
        edges,
        selected,
        catalog,
        selection,
        evidence_root,
        suffix="same-image-across-stages",
    )
    record = result["records"][0]

    assert record["state"] == "BLOCK"
    assert result["confirmed_chain_count"] == 0
    assert record["stages"]["elevation"]["state"] == "CONFIRMED"
    for stage in ("plan", "detail"):
        assert record["stages"][stage]["state"] == "BLOCK"
        assert "SAME_IMAGE_SHA_REUSED_ACROSS_STAGES" in record["stages"][stage]["reason_codes"]


def test_stage_evidence_rejects_measurement_from_other_sheet(tmp_path: Path):
    fixture = _fixtures(tmp_path)
    panel_payload, edges, selected, catalog, evidence_root, selection, closeups = fixture
    discovery = _discover(fixture, tmp_path)
    detail = _complete_selectors(discovery, closeups)[2]
    detail["measurement_ids"] = ["dimension:elsewhere"]
    selection["stages"] = [detail]
    record = _build(
        tmp_path,
        panel_payload,
        edges,
        selected,
        catalog,
        selection,
        evidence_root,
        suffix="wrong-sheet",
    )["records"][0]

    assert record["stages"]["detail"]["reason_codes"] == [
        "MEASUREMENT_OUTSIDE_SELECTED_STAGE_SHEET"
    ]


def test_stage_evidence_rejects_non_dimension_measurement(tmp_path: Path):
    fixture = _fixtures(tmp_path)
    panel_payload, edges, selected, catalog, evidence_root, selection, closeups = fixture
    discovery = _discover(fixture, tmp_path)
    detail = _complete_selectors(discovery, closeups)[2]
    detail["measurement_ids"] = ["line:detail-a"]
    selection["stages"] = [detail]
    record = _build(
        tmp_path,
        panel_payload,
        edges,
        selected,
        catalog,
        selection,
        evidence_root,
        suffix="wrong-type",
    )["records"][0]

    assert record["stages"]["detail"]["reason_codes"] == ["MEASUREMENT_ENTITY_TYPE_NOT_DIMENSION"]


def test_confirmed_detail_requires_at_least_one_measurement(tmp_path: Path):
    fixture = _fixtures(tmp_path)
    panel_payload, edges, selected, catalog, evidence_root, selection, closeups = fixture
    discovery = _discover(fixture, tmp_path)
    detail = _complete_selectors(discovery, closeups)[2]
    detail["measurement_ids"] = []
    selection["stages"] = [detail]
    record = _build(
        tmp_path,
        panel_payload,
        edges,
        selected,
        catalog,
        selection,
        evidence_root,
        suffix="no-measurement",
    )["records"][0]

    assert (
        "CONFIRMED_DETAIL_REQUIRES_DIMENSION_MEASUREMENT"
        in record["stages"]["detail"]["reason_codes"]
    )


def test_stage_evidence_blocks_selection_outside_relation_pool(tmp_path: Path):
    fixture = _fixtures(tmp_path)
    panel_payload, edges, selected, catalog, evidence_root, selection, _closeups = fixture
    selection["stages"] = [
        {
            "stage": "detail",
            "candidate_id": "stage-candidate:not-present",
            "relation_edge_id": "edge:detail-b",
            "state": "CONFIRMED",
        }
    ]
    record = _build(
        tmp_path,
        panel_payload,
        edges,
        selected,
        catalog,
        selection,
        evidence_root,
        suffix="outside-pool",
    )["records"][0]

    assert record["stages"]["detail"]["state"] == "BLOCK"
    assert record["stages"]["detail"]["selected"] == []
    assert record["stages"]["detail"]["reason_codes"] == [
        "EXPLICIT_STAGE_SELECTION_NOT_UNIQUE_IN_CANDIDATE_POOL"
    ]


def test_selected_evidence_binding_prefers_unique_stable_key(tmp_path: Path):
    fixture = _fixtures(tmp_path)
    panel_payload, edges, selected, catalog, evidence_root, selection, _closeups = fixture
    impostor = deepcopy(selected["records"][0])
    impostor["selection_key"] = "component:other"
    impostor["name"] = "错误构件"
    selected["records"].append(impostor)
    record = _build(
        tmp_path,
        panel_payload,
        edges,
        selected,
        catalog,
        selection,
        evidence_root,
        suffix="stable-key",
    )["records"][0]

    assert record["state"] == "REVIEW"
    assert len(record["stages"]["elevation"]["candidates"]) == 1
    assert not any(code.startswith("SELECTED_EVIDENCE_") for code in record["reason_codes"])


def test_stable_key_miss_never_falls_back_to_matching_sequence(tmp_path: Path):
    fixture = _fixtures(tmp_path)
    panel_payload, edges, selected, catalog, evidence_root, selection, _closeups = fixture
    selection["component_id"] = "component:missing"
    record = _build(
        tmp_path,
        panel_payload,
        edges,
        selected,
        catalog,
        selection,
        evidence_root,
        suffix="stable-key-missing",
    )["records"][0]

    assert record["state"] == "BLOCK"
    assert record["selected_evidence_binding"] is None
    assert "SELECTED_EVIDENCE_STABLE_KEY_NOT_FOUND" in record["reason_codes"]
    assert record["stages"]["elevation"]["candidates"] == []


def test_stable_key_ambiguity_never_falls_back_to_matching_sequence(tmp_path: Path):
    fixture = _fixtures(tmp_path)
    panel_payload, edges, selected, catalog, evidence_root, selection, _closeups = fixture
    duplicate = deepcopy(selected["records"][0])
    duplicate["sequence"] = 999
    selected["records"].append(duplicate)
    record = _build(
        tmp_path,
        panel_payload,
        edges,
        selected,
        catalog,
        selection,
        evidence_root,
        suffix="stable-key-ambiguous",
    )["records"][0]

    assert record["state"] == "BLOCK"
    assert record["selected_evidence_binding"] is None
    assert "SELECTED_EVIDENCE_STABLE_KEY_AMBIGUOUS" in record["reason_codes"]
    assert record["stages"]["elevation"]["candidates"] == []


def test_legacy_sequence_fallback_requires_unique_record_and_matching_identity(tmp_path: Path):
    fixture = _fixtures(tmp_path)
    panel_payload, edges, selected, catalog, evidence_root, selection, _closeups = fixture
    selection.pop("component_id")
    selection["name"] = "错误构件"
    mismatch = _build(
        tmp_path,
        panel_payload,
        edges,
        selected,
        catalog,
        selection,
        evidence_root,
        suffix="sequence-mismatch",
    )["records"][0]
    assert mismatch["state"] == "BLOCK"
    assert "SELECTED_EVIDENCE_SEQUENCE_IDENTITY_MISMATCH" in mismatch["reason_codes"]

    selection["name"] = "门套"
    duplicate = deepcopy(selected["records"][0])
    duplicate["selection_key"] = "component:duplicate"
    selected["records"].append(duplicate)
    ambiguous = _build(
        tmp_path,
        panel_payload,
        edges,
        selected,
        catalog,
        selection,
        evidence_root,
        suffix="sequence-ambiguous",
    )["records"][0]
    assert ambiguous["state"] == "BLOCK"
    assert "SELECTED_EVIDENCE_SEQUENCE_AMBIGUOUS" in ambiguous["reason_codes"]


def test_sequence_fallback_binds_when_unique_and_identity_matches(tmp_path: Path):
    fixture = _fixtures(tmp_path)
    panel_payload, edges, selected, catalog, evidence_root, selection, _closeups = fixture
    selection.pop("component_id")
    record = _build(
        tmp_path,
        panel_payload,
        edges,
        selected,
        catalog,
        selection,
        evidence_root,
        suffix="sequence-safe",
    )["records"][0]

    assert record["state"] == "REVIEW"
    assert record["component_id"] is None
    assert record["selected_evidence_binding"] == "sequence_fallback"
    assert "SELECTED_EVIDENCE_BOUND_BY_LEGACY_SEQUENCE_REVIEW_ONLY" in record["reason_codes"]
    assert len(record["stages"]["elevation"]["candidates"]) == 1


def test_legacy_sequence_binding_cannot_confirm_complete_chain(tmp_path: Path):
    fixture = _fixtures(tmp_path)
    panel_payload, edges, selected, catalog, evidence_root, selection, closeups = fixture
    selection.pop("component_id")
    discovery = _discover(fixture, tmp_path, selection=selection, suffix="legacy-discovery")
    selection["stages"] = _complete_selectors(discovery, closeups)
    result = _build(
        tmp_path,
        panel_payload,
        edges,
        selected,
        catalog,
        selection,
        evidence_root,
        suffix="legacy-confirm",
    )
    record = result["records"][0]

    assert record["state"] == "BLOCK"
    assert result["confirmed_chain_count"] == 0
    assert "LEGACY_SEQUENCE_BINDING_CANNOT_CONFIRM" in record["reason_codes"]
    assert "CONFIRMED_CHAIN_REQUIRES_COMPONENT_ID" in record["reason_codes"]
    for stage in ("plan", "elevation", "detail"):
        stage_payload = record["stages"][stage]
        assert stage_payload["state"] == "BLOCK"
        assert "LEGACY_SEQUENCE_BINDING_CANNOT_CONFIRM" in stage_payload["reason_codes"]
        assert stage_payload["selected"]
        assert all(candidate["state"] == "BLOCK" for candidate in stage_payload["selected"])
        assert all(
            "LEGACY_SEQUENCE_BINDING_CANNOT_CONFIRM" in candidate["reason_codes"]
            for candidate in stage_payload["selected"]
        )

    assert "CONFIRMED" not in set(_nested_states(record))


def test_confirmed_chain_requires_explicit_component_id_even_with_other_stable_key(
    tmp_path: Path,
):
    fixture = _fixtures(tmp_path)
    panel_payload, edges, selected, catalog, evidence_root, selection, closeups = fixture
    selection["gold_row_id"] = selection.pop("component_id")
    discovery = _discover(fixture, tmp_path, selection=selection, suffix="gold-row-discovery")
    selection["stages"] = _complete_selectors(discovery, closeups)
    record = _build(
        tmp_path,
        panel_payload,
        edges,
        selected,
        catalog,
        selection,
        evidence_root,
        suffix="missing-component-id",
    )["records"][0]

    assert record["state"] == "BLOCK"
    assert record["selected_evidence_binding"] == "stable_key"
    assert record["component_id"] is None
    assert "CONFIRMED_CHAIN_REQUIRES_COMPONENT_ID" in record["reason_codes"]
    for stage in ("plan", "elevation", "detail"):
        stage_payload = record["stages"][stage]
        assert stage_payload["state"] == "BLOCK"
        assert "CONFIRMED_CHAIN_REQUIRES_COMPONENT_ID" in stage_payload["reason_codes"]
        assert all(candidate["state"] == "BLOCK" for candidate in stage_payload["selected"])
        assert all(
            "CONFIRMED_CHAIN_REQUIRES_COMPONENT_ID" in candidate["reason_codes"]
            for candidate in stage_payload["selected"]
        )
    assert "CONFIRMED" not in set(_nested_states(record))


def test_confirmed_chain_requires_component_id_unique_across_manifest(tmp_path: Path):
    fixture = _fixtures(tmp_path)
    panel_payload, edges, selected, catalog, evidence_root, selection, closeups = fixture
    discovery = _discover(fixture, tmp_path, suffix="duplicate-component-discovery")
    selection["stages"] = _complete_selectors(discovery, closeups)
    duplicate = deepcopy(selection)
    duplicate["sequence"] = 2
    result = build_stage_evidence_manifest(
        panel_payload,
        edges,
        selected,
        catalog,
        [selection, duplicate],
        tmp_path / "out-duplicate-component-id",
        selected_evidence_root=evidence_root,
    )

    assert result["confirmed_chain_count"] == 0
    assert len(result["records"]) == 2
    assert all(record["state"] == "BLOCK" for record in result["records"])
    assert all(
        "CONFIRMED_CHAIN_COMPONENT_ID_NOT_UNIQUE" in record["reason_codes"]
        for record in result["records"]
    )
    for record in result["records"]:
        for stage in ("plan", "elevation", "detail"):
            stage_payload = record["stages"][stage]
            assert stage_payload["state"] == "BLOCK"
            assert "CONFIRMED_CHAIN_COMPONENT_ID_NOT_UNIQUE" in stage_payload["reason_codes"]
            assert all(candidate["state"] == "BLOCK" for candidate in stage_payload["selected"])
            assert all(
                "CONFIRMED_CHAIN_COMPONENT_ID_NOT_UNIQUE" in candidate["reason_codes"]
                for candidate in stage_payload["selected"]
            )
        assert "CONFIRMED" not in set(_nested_states(record))


def _add_second_elevation(fixture, tmp_path: Path):
    panel_payload, edges, selected, catalog, evidence_root, _selection, _closeups = fixture
    panel_payload["sheets"].append(
        {
            "id": "panel:elevation-2",
            "kind": "elevation",
            "drawing_number": "E-02",
            "title": "立面2",
            "bbox": [400, 400, 600, 600],
        }
    )
    panel_path = tmp_path / "panel-elevation-2.png"
    catalog["panels"]["panel:elevation-2"] = {
        "absolute_path": str(panel_path),
        "image_sha256": _write_png(panel_path, (20, 90, 30)),
        "render_profile": "cad-dark-full",
    }
    locator_hash = _write_png(evidence_root / "locator-2.png", (15, 45, 75))
    closeup_hash = _write_png(evidence_root / "closeup-2.png", (25, 55, 85))
    selected["records"][0]["evidence"].append(
        {
            "sheet_id": "panel:elevation-2",
            "selected_occurrence_ids": ["occurrence:2"],
            "locator_image": "locator-2.png",
            "closeup_image": "closeup-2.png",
            "locator_sha256": locator_hash,
            "closeup_sha256": closeup_hash,
            "object_bbox": [420, 420, 520, 560],
        }
    )
    edges.extend(
        [
            {
                "id": "edge:plan-2",
                "relation": "plan_to_elevation",
                "source_id": "panel:plan",
                "target_id": "panel:elevation-2",
                "basis": ["view_reference:E-02@panel_paper_entity:cccc"],
                "confidence": 0.88,
                "status": "REVIEW",
            },
            {
                "id": "edge:detail-2",
                "relation": "elevation_to_detail",
                "source_id": "panel:elevation-2",
                "target_id": "panel:detail-b",
                "basis": ["explicit_reference:D-02@panel_paper_entity:dddd"],
                "confidence": 0.9,
                "status": "REVIEW",
            },
        ]
    )


def _selector_for_candidate(candidate, bbox, review_minute, **extra):
    result = {
        "stage": candidate["stage"],
        "candidate_id": candidate["candidate_id"],
        "sheet_id": candidate["sheet_id"],
        "state": "CONFIRMED",
        "object_bbox": bbox,
        "review": _review(review_minute),
    }
    if candidate["stage"] in {"plan", "detail"}:
        result["relation_edge_id"] = candidate["relation_edge_id"]
    result.update(extra)
    return result


def test_confirmed_plan_edge_must_target_selected_elevation(tmp_path: Path):
    fixture = _fixtures(tmp_path)
    _add_second_elevation(fixture, tmp_path)
    panel_payload, edges, selected, catalog, evidence_root, selection, closeups = fixture
    discovery = _discover(fixture, tmp_path, suffix="two-elevations-plan")
    plan = next(
        value
        for value in discovery["stages"]["plan"]["candidates"]
        if value["relation_edge_id"] == "edge:plan"
    )
    elevation = _candidate(discovery, "elevation", "panel:elevation-2")
    detail = next(
        value
        for value in discovery["stages"]["detail"]["candidates"]
        if value["relation_edge_id"] == "edge:detail-2"
    )
    selection["stages"] = [
        _selector_for_candidate(
            plan,
            [5, 5, 95, 95],
            0,
            closeup_image=str(closeups["plan"][0]),
            closeup_sha256=closeups["plan"][1],
        ),
        _selector_for_candidate(elevation, [420, 420, 520, 560], 1),
        _selector_for_candidate(
            detail,
            [5, 5, 60, 70],
            2,
            measurement_ids=["dimension:2"],
            closeup_image=str(closeups["detail"][0]),
            closeup_sha256=closeups["detail"][1],
        ),
    ]
    record = _build(
        tmp_path,
        panel_payload,
        edges,
        selected,
        catalog,
        selection,
        evidence_root,
        suffix="disconnected-plan",
    )["records"][0]

    assert record["state"] == "BLOCK"
    assert "CONFIRMED_STAGE_CHAIN_DISCONNECTED" in record["reason_codes"]
    for stage in ("plan", "detail"):
        stage_payload = record["stages"][stage]
        assert stage_payload["state"] == "BLOCK"
        assert "CONFIRMED_STAGE_CHAIN_DISCONNECTED" in stage_payload["reason_codes"]
        assert all(candidate["state"] == "BLOCK" for candidate in stage_payload["selected"])


def test_confirmed_detail_edge_must_start_at_selected_elevation(tmp_path: Path):
    fixture = _fixtures(tmp_path)
    _add_second_elevation(fixture, tmp_path)
    panel_payload, edges, selected, catalog, evidence_root, selection, closeups = fixture
    discovery = _discover(fixture, tmp_path, suffix="two-elevations-detail")
    plan = next(
        value
        for value in discovery["stages"]["plan"]["candidates"]
        if value["relation_edge_id"] == "edge:plan"
    )
    elevation = _candidate(discovery, "elevation", "panel:elevation")
    detail = next(
        value
        for value in discovery["stages"]["detail"]["candidates"]
        if value["relation_edge_id"] == "edge:detail-2"
    )
    selection["stages"] = [
        _selector_for_candidate(
            plan,
            [5, 5, 95, 95],
            0,
            closeup_image=str(closeups["plan"][0]),
            closeup_sha256=closeups["plan"][1],
        ),
        _selector_for_candidate(elevation, [120, 120, 220, 260], 1),
        _selector_for_candidate(
            detail,
            [5, 5, 60, 70],
            2,
            measurement_ids=["dimension:2"],
            closeup_image=str(closeups["detail"][0]),
            closeup_sha256=closeups["detail"][1],
        ),
    ]
    record = _build(
        tmp_path,
        panel_payload,
        edges,
        selected,
        catalog,
        selection,
        evidence_root,
        suffix="disconnected-detail",
    )["records"][0]

    assert record["state"] == "BLOCK"
    assert "CONFIRMED_STAGE_CHAIN_DISCONNECTED" in record["reason_codes"]
    for stage in ("plan", "detail"):
        stage_payload = record["stages"][stage]
        assert stage_payload["state"] == "BLOCK"
        assert "CONFIRMED_STAGE_CHAIN_DISCONNECTED" in stage_payload["reason_codes"]
        assert all(candidate["state"] == "BLOCK" for candidate in stage_payload["selected"])


def test_confirmed_chain_requires_unique_relation_edge_ids(tmp_path: Path):
    fixture = _fixtures(tmp_path)
    panel_payload, edges, selected, catalog, evidence_root, selection, closeups = fixture
    discovery = _discover(fixture, tmp_path)
    selection["stages"] = _complete_selectors(discovery, closeups)
    edges.append(deepcopy(edges[0]))
    record = _build(
        tmp_path,
        panel_payload,
        edges,
        selected,
        catalog,
        selection,
        evidence_root,
        suffix="duplicate-edge",
    )["records"][0]

    assert record["state"] == "BLOCK"
    assert "CONFIRMED_CHAIN_RELATION_EDGE_NOT_UNIQUE" in record["reason_codes"]
    for stage in ("plan", "detail"):
        stage_payload = record["stages"][stage]
        assert stage_payload["state"] == "BLOCK"
        assert "CONFIRMED_CHAIN_RELATION_EDGE_NOT_UNIQUE" in stage_payload["reason_codes"]
        assert all(candidate["state"] == "BLOCK" for candidate in stage_payload["selected"])


def test_unknown_sheet_kind_never_defaults_to_elevation(tmp_path: Path):
    fixture = _fixtures(tmp_path)
    panel_payload, edges, selected, catalog, evidence_root, selection, _closeups = fixture
    next(sheet for sheet in panel_payload["sheets"] if sheet["id"] == "panel:elevation")["kind"] = (
        "unknown"
    )
    record = _build(
        tmp_path,
        panel_payload,
        edges,
        selected,
        catalog,
        selection,
        evidence_root,
        suffix="unknown-kind",
    )["records"][0]

    assert record["stages"]["elevation"]["candidates"] == []
    assert "SELECTED_EVIDENCE_KIND_OUTSIDE_STAGE_MODEL" in record["reason_codes"]


def test_exact_material_code_recovers_detail_candidate_without_confirming_it(
    tmp_path: Path,
):
    fixture = _fixtures(tmp_path)
    panel_payload, _edges, _selected, _catalog, _root, selection, _closeups = fixture
    selection["mt_code"] = "GC-SS-987"
    next(
        sheet for sheet in panel_payload["sheets"] if sheet["id"] == "panel:detail-b"
    )["title"] = "门套节点图"
    panel_payload["entities"].extend(
        [
            {
                "id": "material:exact",
                "sheet_id": "panel:detail-b",
                "entity_type": "MTEXT",
                "text": "304不锈钢 GC SS 987",
            },
            {
                "id": "material:near-miss",
                "sheet_id": "panel:detail-a",
                "entity_type": "MTEXT",
                "text": "GC-SS-9870",
            },
        ]
    )

    record = _discover(fixture, tmp_path, suffix="exact-material-retrieval")
    recovered = [
        value
        for value in record["stages"]["detail"]["candidates"]
        if value["source"] == "exact_material_code_candidate"
    ]

    assert len(recovered) == 1
    assert recovered[0]["sheet_id"] == "panel:detail-b"
    assert recovered[0]["reference_entity_ids"] == ["material:exact"]
    assert recovered[0]["relation_edge_id"] is None
    assert recovered[0]["state"] == "CANDIDATE"
    assert record["stages"]["detail"]["candidates"][0]["sheet_id"] == (
        "panel:detail-b"
    )
    assert recovered[0]["component_semantic_support"]["score"] > 0.7
    assert record["state"] == "REVIEW"


def test_explicit_companion_material_code_is_retrieval_only(tmp_path: Path):
    fixture = _fixtures(tmp_path)
    panel_payload, _edges, _selected, _catalog, _root, selection, _closeups = fixture
    selection["mt_code"] = "GC-MR-101"
    selection["companion_material_codes"] = ["GC-SS-987"]
    panel_payload["entities"].append(
        {
            "id": "material:companion",
            "sheet_id": "panel:detail-b",
            "entity_type": "ATTRIB",
            "text": "GC-SS-987",
        }
    )

    record = _discover(fixture, tmp_path, suffix="companion-material-retrieval")
    recovered = [
        value
        for value in record["stages"]["detail"]["candidates"]
        if value["source"] == "companion_material_code_candidate"
    ]

    assert len(recovered) == 1
    assert recovered[0]["sheet_id"] == "panel:detail-b"
    assert recovered[0]["reference_entity_ids"] == ["material:companion"]
    assert recovered[0]["relation_basis"] == ["exact_material_code:GC-SS-987"]
    assert recovered[0]["reason_codes"] == [
        "COMPANION_MATERIAL_CODE_IS_RETRIEVAL_ONLY"
    ]
    assert recovered[0]["relation_edge_id"] is None
    assert record["state"] == "REVIEW"
