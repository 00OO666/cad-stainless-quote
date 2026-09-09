"""Synthetic fixtures only: explicit semantic review never equals automatic OCR."""

import base64
import copy
import hashlib

import ezdxf
import pytest
from cadquote.cad_index import index_dxf
from cadquote.native_graphic_review import (
    NativeGraphicReviewError,
    classify_native_graphic_warnings,
    inspect_native_ole_inventory,
)
from ezdxf.lldxf.tags import Tags
from ezdxf.lldxf.types import DXFBinaryTag, DXFTag, DXFVertex


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def add_ole(block, payload=b"original synthetic non-reference graphic"):
    entity = block.new_entity("OLE2FRAME", dxfattribs={"layer": "0"})
    entity.acdb_ole2frame = Tags(
        [
            DXFTag(100, "AcDbOle2Frame"),
            DXFTag(70, 2),
            DXFVertex(10, (0, 0, 0)),
            DXFVertex(11, (6, 3, 0)),
            DXFTag(90, len(payload)),
            DXFBinaryTag(310, payload),
        ]
    )
    return entity


@pytest.fixture
def graphic_case(tmp_path):
    document = ezdxf.new("R2018")
    block = document.blocks.new("SYNTHETIC_GRAPHIC")
    block.add_text("fixture annotation", dxfattribs={"insert": (0, 0)})
    ole = add_ole(block)
    paper = document.layouts.get("Layout1")
    first = paper.add_blockref(block.name, (20, 20))
    second = paper.add_blockref(block.name, (50, 20))
    path = tmp_path / "synthetic.dxf"
    document.saveas(path)
    inventory = inspect_native_ole_inventory(ezdxf.readfile(path))
    original = tmp_path / "inspected-original-content.bin"
    original.write_bytes(ole.binary_data())
    preview = tmp_path / "inspected-preview.png"
    preview.write_bytes(
        base64.b64decode(
            "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jZF8AAAAASUVORK5CYII="
        )
    )
    definition = {
        **inventory["definitions"][0],
        "semantic_classification": "NON_REFERENCE_GRAPHIC",
        "original_content": {"path": str(original), "sha256": digest(original)},
        "reviewed_image": {"path": str(preview), "sha256": digest(preview)},
    }
    manifest = {
        "schema_version": "native-graphic-review/1",
        "source_sha256": digest(path),
        "review": {
            "reviewer": "synthetic explicit reviewer",
            "reviewed_at": "2026-01-01T00:00:00Z",
            "reason": "Synthetic unit-test attestation, not production image recognition.",
        },
        "definitions": [definition],
        "instances": inventory["instances"],
    }
    warnings = index_dxf(path).warnings
    exact = "paper:Layout1:SYNTHETIC_GRAPHIC:OLE2FRAME:None: non copyable"
    assert warnings == [exact, exact]
    return {
        "path": path,
        "manifest": manifest,
        "warnings": {"source": list(warnings), "fresh": list(warnings)},
        "ole_handle": ole.dxf.handle,
        "first_handle": first.dxf.handle,
        "second_handle": second.dxf.handle,
        "preview": preview,
        "original": original,
        "exact": exact,
    }


def run(case, **kwargs):
    return classify_native_graphic_warnings(
        case["path"], case["warnings"], case["manifest"], **kwargs
    )


def test_exact_source_payload_all_instances_and_each_replay_warning_verified(graphic_case):
    case = graphic_case
    before = (
        copy.deepcopy(case["manifest"]),
        copy.deepcopy(case["warnings"]),
        case["path"].read_bytes(),
    )
    result = run(case)
    assert result["blocking"] is False and result["all_reviewed_groups_covered"] is True
    assert result["remaining"] == {"source": [], "fresh": []}
    assert len(result["classified_warnings"]) == 4
    assert all(
        r["classification"] == "CONTENT_REVIEWED_NON_REFERENCE_GRAPHIC"
        for r in result["classified_warnings"]
    )
    assert all(r["warning"] == case["exact"] for r in result["classified_warnings"])
    assert len(result["inventory"]["instances"]) == 2
    assert (case["manifest"], case["warnings"], case["path"].read_bytes()) == before


@pytest.mark.parametrize(
    "mutation",
    [
        "manifest_missing",
        "reviewer_missing",
        "reason_blank",
        "time_invalid",
        "time_naive",
        "wrong_source",
        "wrong_payload",
        "wrong_payload_size",
        "missing_definition",
        "duplicate_definition",
        "missing_instance",
        "duplicate_instance",
        "instance_chain",
        "instance_matrix",
        "instance_bbox",
        "reference_content",
        "artifact_missing",
        "artifact_hash",
        "original_hash",
    ],
)
def test_incomplete_or_drifting_review_never_exempts_warning(graphic_case, mutation):
    case = graphic_case
    manifest = case["manifest"]
    if mutation == "manifest_missing":
        case["manifest"] = None
    elif mutation == "reviewer_missing":
        manifest["review"].pop("reviewer")
    elif mutation == "reason_blank":
        manifest["review"]["reason"] = "  "
    elif mutation == "time_invalid":
        manifest["review"]["reviewed_at"] = "not-a-date"
    elif mutation == "time_naive":
        manifest["review"]["reviewed_at"] = "2026-01-01T00:00:00"
    elif mutation == "wrong_source":
        manifest["source_sha256"] = "0" * 64
    elif mutation == "wrong_payload":
        manifest["definitions"][0]["payload_sha256"] = "0" * 64
    elif mutation == "wrong_payload_size":
        manifest["definitions"][0]["payload_bytes"] += 1
    elif mutation == "missing_definition":
        manifest["definitions"] = []
    elif mutation == "duplicate_definition":
        manifest["definitions"].append(copy.deepcopy(manifest["definitions"][0]))
    elif mutation == "missing_instance":
        manifest["instances"].pop()
    elif mutation == "duplicate_instance":
        manifest["instances"].append(copy.deepcopy(manifest["instances"][0]))
    elif mutation == "instance_chain":
        manifest["instances"][0]["handle_chain"][0] = "BAD"
    elif mutation == "instance_matrix":
        manifest["instances"][0]["transforms_outer_to_inner"][0]["matrix44_rows"][3][0] += 1
    elif mutation == "instance_bbox":
        manifest["instances"][0]["world_bbox"][0] += 1
    elif mutation == "reference_content":
        manifest["definitions"][0]["semantic_classification"] = "MATERIAL_OR_DRAWING_REFERENCE"
    elif mutation == "artifact_missing":
        manifest["definitions"][0]["reviewed_image"]["path"] += ".missing"
    elif mutation == "artifact_hash":
        case["preview"].write_bytes(b"changed")
    elif mutation == "original_hash":
        case["original"].write_bytes(b"changed")
    result = run(case)
    assert result["blocking"] and result["review_issues"]
    assert result["remaining"] == case["warnings"]
    assert all(row["blocking"] for row in result["classified_warnings"])


@pytest.mark.parametrize(
    "warning",
    [
        "paper:Layout1:SYNTHETIC_GRAPHIC:OLE2FRAME:AB: non copyable",
        "paper:Layout1:SYNTHETIC_GRAPHIC:OLE2FRAME:None: non copyable extra",
        "paper:Layout1:SYNTHETIC_GRAPHIC:OLEFRAME:None: non copyable",
        "paper:Layout1:SYNTHETIC_GRAPHIC:OLE2FRAME:None: unsupported",
        "model:SYNTHETIC_GRAPHIC:OLE2FRAME:None: non copyable",
        "paper:Layout1:OTHER:OLE2FRAME:None: non copyable",
    ],
)
def test_non_exact_or_unreviewed_warning_is_blocking(graphic_case, warning):
    case = graphic_case
    case["warnings"] = {"source": [warning], "fresh": [warning]}
    result = run(case)
    assert result["blocking"]
    assert result["remaining"] == case["warnings"]


@pytest.mark.parametrize(
    "variant", ["one_missing", "duplicate_extra", "fresh_empty", "cross_batch_total_only"]
)
def test_multiplicity_is_per_replay_not_combined(graphic_case, variant):
    case = graphic_case
    if variant == "one_missing":
        case["warnings"]["fresh"].pop()
    elif variant == "duplicate_extra":
        case["warnings"]["fresh"].append(case["exact"])
    elif variant == "fresh_empty":
        case["warnings"]["fresh"] = []
    else:
        case["warnings"]["source"] = [case["exact"]] * 4
        case["warnings"]["fresh"] = []
    result = run(case)
    assert result["blocking"] and not result["all_reviewed_groups_covered"]
    assert any("MULTIPLICITY" in issue for issue in result["review_issues"])
    assert result["remaining"]["fresh"] == case["warnings"]["fresh"]


def test_other_warning_retained_while_exact_reviewed_graphics_are_classified(graphic_case):
    case = graphic_case
    for warnings in case["warnings"].values():
        warnings.append("paper:Layout1: missing external reference")
    result = run(case)
    assert result["blocking"]
    assert result["remaining"] == {
        "source": ["paper:Layout1: missing external reference"],
        "fresh": ["paper:Layout1: missing external reference"],
    }
    assert len(result["classified_warnings"]) == 6


def test_original_content_must_be_full_native_payload_not_arbitrary_hashed_file(graphic_case):
    case = graphic_case
    case["original"].write_bytes(b"arbitrary file with a valid matching file hash")
    case["manifest"]["definitions"][0]["original_content"]["sha256"] = digest(case["original"])
    result = run(case)
    assert result["remaining"] == case["warnings"]
    assert "ORIGINAL_CONTENT_NOT_NATIVE_PAYLOAD" in result["review_issues"]


def test_nested_source_warning_group_replays_actual_parent_chain(graphic_case):
    case = graphic_case
    document = ezdxf.readfile(case["path"])
    wrapper = document.blocks.new("SYNTHETIC_WRAPPER")
    wrapper.add_blockref("SYNTHETIC_GRAPHIC", (10, 4), dxfattribs={"rotation": 90})
    document.layouts.get("Layout1").add_blockref(wrapper.name, (100, 200))
    document.saveas(case["path"])
    inventory = inspect_native_ole_inventory(ezdxf.readfile(case["path"]))
    case["manifest"]["source_sha256"] = digest(case["path"])
    case["manifest"]["instances"] = inventory["instances"]
    warnings = index_dxf(case["path"]).warnings
    case["warnings"] = {"source": list(warnings), "fresh": list(warnings)}
    assert len(warnings) == 3 and all(w == case["exact"] for w in warnings)
    result = run(case)
    assert result["blocking"] is False
    assert any(len(i["handle_chain"]) == 3 for i in result["inventory"]["instances"])


@pytest.mark.parametrize(
    "mutation", ["new_instance", "new_definition", "changed_payload", "source_drift"]
)
def test_current_native_bytes_not_manifest_inventory_are_authoritative(graphic_case, mutation):
    case = graphic_case
    document = ezdxf.readfile(case["path"])
    if mutation in {"new_instance", "source_drift"}:
        document.layouts.get("Layout1").add_blockref("SYNTHETIC_GRAPHIC", (80, 20))
    elif mutation == "new_definition":
        add_ole(document.blocks.get("SYNTHETIC_GRAPHIC"), b"unreviewed new content")
    else:
        document.entitydb[case["ole_handle"]].acdb_ole2frame.append(DXFBinaryTag(310, b"modified"))
    document.saveas(case["path"])
    if mutation != "source_drift":
        # Even authoring a new source hash cannot hide stale payload/instance review.
        case["manifest"]["source_sha256"] = digest(case["path"])
    result = run(case)
    assert result["blocking"] and result["remaining"] == case["warnings"]


def test_caps_do_not_turn_missing_instances_into_complete_review(graphic_case):
    result = run(graphic_case, max_entities=1)
    assert result["blocking"] and "NATIVE_INSTANCE_ENTITY_CAP" in result["review_issues"]


@pytest.mark.parametrize("target", ["source", "artifact", "manifest"])
def test_midflight_review_dependencies_cannot_change(graphic_case, monkeypatch, target):
    import cadquote.native_graphic_review as module

    case = graphic_case
    real_digest = module._digest
    source_reads = 0
    asset_reads = 0

    def drifting(path):
        nonlocal source_reads, asset_reads
        if str(path) == str(case["path"]):
            source_reads += 1
            if target == "source" and source_reads > 1:
                return "0" * 64
        if str(path) == str(case["preview"]):
            asset_reads += 1
            if target == "artifact" and asset_reads > 1:
                return "0" * 64
            if target == "manifest":
                case["manifest"]["review"]["reason"] = "changed after review hash capture"
        return real_digest(path)

    monkeypatch.setattr(module, "_digest", drifting)
    result = run(case)
    assert result["blocking"] and result["review_issues"]
    assert result["remaining"] == case["warnings"]


def test_nested_instance_basepoint_rotation_and_negative_scale_are_recorded():
    document = ezdxf.new("R2018")
    inner = document.blocks.new("INNER", base_point=(2, 1, 0))
    add_ole(inner)
    outer = document.blocks.new("OUTER")
    nested = outer.add_blockref(
        "INNER", (4, 5), dxfattribs={"rotation": 90, "xscale": -2, "yscale": 3}
    )
    root = document.layouts.get("Layout1").add_blockref("OUTER", (100, 200))
    inventory = inspect_native_ole_inventory(document)
    row = inventory["instances"][0]
    assert row["handle_chain"][:2] == [root.dxf.handle, nested.dxf.handle]
    assert row["block_chain"] == ["OUTER", "INNER"]
    assert len(row["transforms_outer_to_inner"]) == 2
    assert row["world_bbox"] == pytest.approx([98, 197, 107, 209])


@pytest.mark.parametrize(
    "mutation", ["cycle", "minsert", "xref", "missing_bounds", "empty_payload", "depth"]
)
def test_native_inventory_unknown_or_unsupported_instance_fails_closed(mutation):
    document = ezdxf.new("R2018")
    block = document.blocks.new("GRAPHIC")
    ole = add_ole(block)
    root = document.layouts.get("Layout1").add_blockref("GRAPHIC", (0, 0))
    limit = 32
    if mutation == "cycle":
        block.add_blockref("GRAPHIC", (0, 0))
    elif mutation == "minsert":
        root.dxf.row_count = 2
    elif mutation == "xref":
        block.block.dxf.flags |= 4
    elif mutation == "missing_bounds":
        ole.acdb_ole2frame = Tags([DXFBinaryTag(310, b"test")])
    elif mutation == "empty_payload":
        ole.acdb_ole2frame = Tags([DXFVertex(10, (0, 0, 0)), DXFVertex(11, (1, 1, 0))])
    else:
        parent = document.blocks.new("PARENT")
        parent.add_blockref("GRAPHIC", (0, 0))
        document.layouts.get("Layout1").add_blockref("PARENT", (0, 0))
        limit = 1
    with pytest.raises(NativeGraphicReviewError):
        inspect_native_ole_inventory(document, max_depth=limit)
