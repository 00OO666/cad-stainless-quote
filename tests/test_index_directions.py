import math

import ezdxf
import pytest
from cadquote.index_directions import extract_index_directions, project_index_direction
from cadquote.models import CadEntity


def fixture(rotation=0, xscale=1, flip_arrow=False):
    doc = ezdxf.new()
    block = doc.blocks.new("synthetic-index")
    block.add_arc((0, 0), 5, 0, 180)
    t = 5 / math.sqrt(2)
    sign = -1 if flip_arrow else 1
    hatch = block.add_hatch()
    hatch.paths.add_polyline_path(
        [(t * sign, t * sign, 0), (0, 8 * sign, 0), (-t * sign, t * sign, -math.tan(math.pi / 8))],
        is_closed=True,
    )
    insert = doc.layouts.get("Layout1").add_blockref(
        "synthetic-index", (40, 40), dxfattribs={"rotation": rotation, "xscale": xscale}
    )
    insert.add_attrib("NO.", "9", (40, 40))
    insert.add_attrib("DWG_NO.", "EL-99", (40, 38))
    return doc, block, insert


def extract(doc, **kw):
    return extract_index_directions(doc, source_file_id="synth", layout_name="Layout1", **kw)


@pytest.mark.parametrize("angle", [0, 37, 90, 180, 270])
def test_native_arrow_rotates_and_retains_provenance(angle):
    doc, block, insert = fixture(angle)
    row = extract(doc)["records"][0]
    assert row["arrow_vector"] == pytest.approx(
        [-math.sin(math.radians(angle)), math.cos(math.radians(angle))]
    )
    assert row["parent_insert_handle"] == insert.dxf.handle
    assert row["geometry_evidence"]["hatch_handle"] == next(iter(block.query("HATCH"))).dxf.handle
    assert row["state"] == "REVIEW" and row["physical_quantity"] is None
    assert row["view_binding_confirmed"] is False


def test_identical_insert_rotation_does_not_imply_identical_direction():
    doc_a, _, _ = fixture(24)
    doc_b, _, _ = fixture(24, flip_arrow=True)
    a, b = extract(doc_a)["records"][0], extract(doc_b)["records"][0]
    assert a["arrow_vector"] == pytest.approx([-v for v in b["arrow_vector"]])


def test_reflected_insert_uses_native_matrix():
    doc, _, _ = fixture(90, xscale=-1)
    assert extract(doc)["records"][0]["arrow_vector"] == pytest.approx([-1, 0])


@pytest.mark.parametrize(
    "mode,reason",
    [
        ("duplicate", "AMBIGUOUS_OR_MISSING_SAME_PARENT_ATTRIBUTES"),
        ("missing", "AMBIGUOUS_OR_MISSING_SAME_PARENT_ATTRIBUTES"),
        ("hidden", "HIDDEN_INSERT"),
        ("nested", "NESTED_SYMBOL_UNSUPPORTED"),
        ("two_arrows", "NO_UNIQUE_SUPPORTED_RADIAL_ARROW"),
        ("no_arrow", "NO_UNIQUE_SUPPORTED_RADIAL_ARROW"),
        ("different_circle", "AMBIGUOUS_CIRCULAR_CALLOUT"),
    ],
)
def test_unsupported_or_ambiguous_has_no_vector(mode, reason):
    doc, block, insert = fixture()
    if mode == "duplicate":
        insert.add_attrib("NO", "8", (40, 40))
    elif mode == "missing":
        insert.delete_attrib("NO.")
        # A nearby label on another parent is not a substitute.
        other = doc.layouts.get("Layout1").add_blockref("synthetic-index", (40, 40))
        other.add_attrib("NO", "9", (40, 40))
    elif mode == "hidden":
        insert.dxf.invisible = 1
    elif mode == "nested":
        doc.blocks.new("nested")
        block.add_blockref("nested", (0, 0))
    elif mode == "two_arrows":
        block.add_entity(next(iter(block.query("HATCH"))).copy())
    elif mode == "no_arrow":
        next(iter(block.query("HATCH"))).dxf.invisible = 1
    elif mode == "different_circle":
        block.add_circle((9, 9), 2)
    row = extract(doc)["records"][0]
    assert row["arrow_vector"] is None and reason in row["reason_codes"]


def test_resource_cap_is_explicit():
    doc, _, _ = fixture()
    doc.layouts.get("Layout1").add_blockref("synthetic-index", (70, 70))
    assert extract(doc, max_inserts=1)["truncated"] is True
    assert extract(doc, max_block_entities=1)["records"][0]["arrow_vector"] is None


def viewport(**changes):
    data = dict(
        id="vp",
        handle="VP",
        source_file_id="synth",
        sheet_id="paper",
        space="paper:Layout1",
        entity_type="VIEWPORT",
        bbox=(0, 0, 100, 100),
        geometry={"model_bbox": [1000, 2000, 2000, 3000]},
    )
    data.update(changes)
    return CadEntity(**data)


def test_original_viewport_mapping_preserves_raw_geometry_and_does_not_bind_objects():
    doc, _, _ = fixture()
    raw = extract(doc)["records"][0]
    out = project_index_direction(raw, viewport())
    assert out["model_center"] == pytest.approx([1400, 2400])
    assert out["model_tip"] == pytest.approx([1400, 2480])
    assert out["model_arrow_vector"] == pytest.approx([0, 1])
    assert "model_center" not in raw
    assert out["physical_quantity"] is None and out["state"] == "REVIEW"


@pytest.mark.parametrize(
    "changes",
    [
        {"source_file_id": "other"},
        {"space": "paper:Other"},
        {"bbox": [100, 100, 200, 200]},
        {"geometry": {"model_bbox": [0, 0, 1000, 1000], "view_twist_angle": 0.2}},
        {"geometry": {"model_bbox": [0, 0, 1000, 1000], "view_direction_vector": [0, 0, -1]}},
        {
            "geometry": {
                "model_bbox": [0, 0, 1000, 1000],
                "model_bbox_target_shifted": [1, 0, 1001, 1000],
            }
        },
    ],
)
def test_projection_refuses_cross_source_or_unproved_mapping(changes):
    doc, _, _ = fixture()
    row = project_index_direction(extract(doc)["records"][0], viewport(**changes))
    assert row["projection_state"] == "UNRESOLVED" and row["model_center"] is None


def test_cli_writes_provenance_without_mutating_native_file(tmp_path):
    import hashlib
    import json

    from cad_quote import build_parser

    doc, _, _ = fixture()
    source, output = tmp_path / "synthetic.dxf", tmp_path / "result.json"
    doc.saveas(source)
    before = source.read_bytes()
    args = build_parser().parse_args(
        [
            "index-directions",
            str(source),
            "--source-file-id",
            "synth",
            "--layout",
            "Layout1",
            "--out",
            str(output),
        ]
    )
    assert args.handler(args) == 0
    result = json.loads(output.read_text(encoding="utf8"))
    assert result["source_sha256"] == hashlib.sha256(before).hexdigest()
    assert result["summary"] == {"GEOMETRY_RESOLVED": 1}
    assert source.read_bytes() == before


def test_cli_refuses_source_overwrite(tmp_path):
    from cad_quote import build_parser

    source = tmp_path / "synthetic.dxf"
    args = build_parser().parse_args(
        [
            "index-directions",
            str(source),
            "--source-file-id",
            "synth",
            "--layout",
            "Layout1",
            "--out",
            str(source),
        ]
    )
    with pytest.raises(ValueError, match="separate JSON"):
        args.handler(args)


def chevron_fixture(rotation=0, xscale=1):
    doc = ezdxf.new()
    block = doc.blocks.new("synthetic-chevron")
    block.add_circle((0, 0), 4)
    block.add_lwpolyline([(4, 0), (7, 0), (0, 9), (-7, 0), (-4, 0)])
    ins = doc.layouts.get("Layout1").add_blockref(
        "synthetic-chevron", (40, 40), dxfattribs={"rotation": rotation, "xscale": xscale}
    )
    ins.add_attrib("E1", "07", (40, 40))
    ins.add_attrib("1.1-E01", "B3-E-08", (40, 38))
    return doc, block, ins


@pytest.mark.parametrize("angle", [0, 37, 90, 180, 270])
def test_custom_attributes_and_open_five_vertex_arrow(angle):
    doc, _, _ = chevron_fixture(angle)
    r = extract(doc)["records"][0]
    assert r["page_code"] == "B3-E-08" and r["view_number"] == "07"
    assert r["arrow_vector"] == pytest.approx(
        [-math.sin(math.radians(angle)), math.cos(math.radians(angle))]
    )
    assert r["geometry_evidence"]["arrow_entity_type"] == "LWPOLYLINE"
    assert not r["view_binding_confirmed"]


@pytest.mark.parametrize("mode", ["asymmetric", "off_diameter", "arc", "duplicate", "hidden"])
def test_chevron_does_not_accept_arbitrary_or_ambiguous_polygon(mode):
    doc, block, _ = chevron_fixture()
    poly = next(iter(block.query("LWPOLYLINE")))
    if mode == "asymmetric":
        poly.set_points([(4, 0), (8, 0), (0, 9), (-7, 0), (-4, 0)])
    elif mode == "off_diameter":
        poly.set_points([(4, 0), (7, 1), (0, 9), (-7, 0), (-4, 0)])
    elif mode == "arc":
        poly.set_points([(4, 0, 0.2), (7, 0, 0), (0, 9, 0), (-7, 0, 0), (-4, 0, 0)], format="xyb")
    elif mode == "duplicate":
        block.add_entity(poly.copy())
    else:
        poly.dxf.invisible = 1
    assert extract(doc)["records"][0]["arrow_vector"] is None


def test_native_inverse_ignores_stale_double_shift_and_handles_twist():
    from cadquote.cad_index import _record_entity
    from ezdxf import bbox

    doc, _, _ = chevron_fixture()
    layout = doc.layouts.get("Layout1")
    native = layout.add_viewport(
        center=(40, 40), size=(100, 100), view_center_point=(20, 30), view_height=800
    )
    native.dxf.view_target_point = (1230, 2450, 0)
    native.dxf.view_twist_angle = 0.2
    indexed = _record_entity(native, "synth", "paper", "paper:Layout1", bbox.Cache())
    raw = extract(doc)["records"][0]
    mapped = project_index_direction(raw, indexed, native_viewport=native)
    assert mapped["projection_state"] == "GEOMETRY_RESOLVED"
    assert mapped["projection_basis"]["legacy_shifted_bbox_used"] is False
    assert mapped["projection_basis"]["roundtrip_residual"] < 1e-6
    assert "model_center" not in raw
    indexed.source_file_id = "wrong"
    assert project_index_direction(raw, indexed, native_viewport=native)["model_center"] is None
