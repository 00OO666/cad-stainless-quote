from cadquote.evidence_stages import canonical_stage_for_kind


def test_canonical_stage_mapping_is_fail_closed():
    assert canonical_stage_for_kind("plan") == "plan"
    assert canonical_stage_for_kind("elevation_index") == "plan"
    assert canonical_stage_for_kind("elevation") == "elevation"
    assert canonical_stage_for_kind("detail") == "detail"
    assert canonical_stage_for_kind("door") == "detail"
    assert canonical_stage_for_kind("ceiling") == "detail"
    assert canonical_stage_for_kind("floor") == "detail"
    assert canonical_stage_for_kind("unknown") is None
    assert canonical_stage_for_kind("other") is None
    assert canonical_stage_for_kind("future_kind") is None
    assert canonical_stage_for_kind(None) is None
