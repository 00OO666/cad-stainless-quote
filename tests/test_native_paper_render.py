import math

import ezdxf
import pytest
from cadquote.component_closeups import render_component_frame_closeups
from cadquote.io import sha256_file
from cadquote.native_paper_render import native_viewport_region, render_native_paper_regions
from PIL import Image


def fixture(tmp_path):
    doc = ezdxf.new("R2018")
    model = doc.modelspace()
    model.add_lwpolyline([(20, 20), (180, 20), (180, 160), (20, 160)], close=True)
    doc.layers.new("HIDDEN_NATIVE")
    model.add_circle((100, 100), radius=40, dxfattribs={"layer": "HIDDEN_NATIVE"})
    paper = doc.layouts.new("P")
    vp = paper.add_viewport(
        center=(100, 100), size=(200, 200), view_center_point=(100, 100), view_height=200
    )
    vp.frozen_layers = ["HIDDEN_NATIVE"]
    dim = paper.add_linear_dim(
        base=(20, 175),
        p1=(20, 160),
        p2=(180, 160),
        override={"dimtxt": 5, "dimasz": 3},
        dxfattribs={"color": 2},
    )
    dim.render()
    hidden = paper.add_text("HIDDEN LABEL", dxfattribs={"insert": (40, 40), "invisible": 1})
    note = paper.add_text("MT-09", dxfattribs={"insert": (25, 25), "height": 7, "color": 2})
    other = doc.layouts.new("OTHER")
    other.add_text("WRONG LAYOUT", dxfattribs={"insert": (40, 40)})
    path = tmp_path / "synthetic.dxf"
    doc.saveas(path)
    return doc, vp, dim.dimension.dxf.handle, hidden.dxf.handle, note.dxf.handle, path


@pytest.mark.parametrize("angle", [0, 360, -720])
def test_native_transform_roundtrip_uses_target_and_whole_turns(tmp_path, angle):
    _, vp, _, _, _, _ = fixture(tmp_path)
    vp.dxf.view_twist_angle = angle
    vp.dxf.view_target_point = (900, -600, 0)
    mapped = native_viewport_region(vp, [930, -570, 1070, -430])
    assert mapped["paper_bbox"] == pytest.approx([30, 30, 170, 170])
    assert mapped["model_to_paper_scale"] == 1


@pytest.mark.parametrize(
    "case",
    [
        "rotation",
        "radians",
        "negative-z",
        "zero-direction",
        "perspective",
        "clip",
        "inactive",
        "outside",
        "nan",
    ],
)
def test_unsupported_mapping_is_not_guessed(tmp_path, case):
    _, vp, _, _, _, _ = fixture(tmp_path)
    region = [20, 20, 180, 180]
    if case == "rotation":
        vp.dxf.view_twist_angle = 20
    elif case == "radians":
        vp.dxf.view_twist_angle = 2 * math.pi
    elif case == "negative-z":
        vp.dxf.view_direction_vector = (0, 0, -1)
    elif case == "zero-direction":
        vp.dxf.view_direction_vector = (0, 0, 0)
    elif case == "perspective":
        vp.dxf.flags = 1
    elif case == "clip":
        vp.dxf.flags = 65536
    elif case == "inactive":
        vp.dxf.status = 0
    elif case == "outside":
        region[0] = -50
    else:
        region[0] = math.nan
    with pytest.raises(ValueError):
        native_viewport_region(vp, region)


def test_native_dimension_graphics_and_layout_visibility_are_preserved(tmp_path):
    _, vp, dim, hidden, note, path = fixture(tmp_path)
    before = sha256_file(path)
    result = render_native_paper_regions(
        path,
        {"region": [10, 10, 190, 190]},
        tmp_path / "out",
        viewport_handles={"region": vp.dxf.handle},
        target_px=800,
    )
    assert result["failure_count"] == 0 and result["rendered_count"] == 1
    rec = result["regions"]["region"]
    handles = {e["handle"] for e in rec["paper_entities"]}
    assert {dim, note} <= handles and hidden not in handles
    assert rec["paper_entity_types"]["DIMENSION"] == 1
    assert rec["viewport_frozen_layers"] == ["HIDDEN_NATIVE"]
    assert rec["source_sha256"] == before == sha256_file(path)
    assert rec["state"] == "REVIEW" and rec.get("quantity") is None
    with Image.open(tmp_path / "out" / rec["file"]) as im:
        assert list(im.size) == rec["pixel_size"] == [800, 800]
        # Yellow native paper dimension/text pixels are absent from white model lines.
        pixels = im.convert("RGB").tobytes()
        assert sum(
            r > 100 and g > 100 and b < 80
            for r, g, b in zip(pixels[0::3], pixels[1::3], pixels[2::3], strict=True)
        ) > 100


def test_region_failures_and_resource_cap_remain_explicit(tmp_path):
    _, vp, _, _, _, path = fixture(tmp_path)
    result = render_native_paper_regions(
        path,
        {"bad": [-20, 0, 180, 180], "good": [10, 10, 190, 190]},
        tmp_path / "out",
        viewport_handles={k: vp.dxf.handle for k in ["bad", "good"]},
        target_px=512,
    )
    assert list(result["regions"]) == ["good"]
    assert result["failures"] == [{"label": "bad", "reason": "REGION_OUTSIDE_SOURCE_VIEWPORT"}]
    capped = render_native_paper_regions(
        path,
        {"good": [10, 10, 190, 190]},
        tmp_path / "cap",
        viewport_handles={"good": vp.dxf.handle},
        max_paper_entities=1,
        target_px=512,
    )
    assert capped["rendered_count"] == 0 and capped["failures"][0]["reason"] == "PAPER_ENTITY_CAP"


@pytest.mark.parametrize("hash_ok", [True, False])
def test_component_closeups_auto_native_paper_and_source_hash_gate(tmp_path, hash_ok):
    _, vp, dim, _, _, path = fixture(tmp_path)
    index = {
        "sources": [
            {
                "source_file_id": "synthetic",
                "source_path": str(path),
                "source_sha256": sha256_file(path) if hash_ok else "bad",
            }
        ]
    }
    panels = {
        "sheets": [
            {
                "id": "sheet",
                "source_file_id": "synthetic",
                "bbox": [0, 0, 200, 200],
                "viewport_handle": vp.dxf.handle,
                "kind": "detail",
            }
        ]
    }
    frames = {
        "records": [
            {
                "component_id": "component:synthetic",
                "frames": [
                    {
                        "sheet_id": "sheet",
                        "object_bbox": [20, 20, 180, 160],
                        "dimension_bboxes": [[20, 170, 180, 182]],
                        "selected_occurrence_ids": ["occ"],
                        "entity_ids": ["dim"],
                    }
                ],
            }
        ]
    }
    out = render_component_frame_closeups(
        index, panels, frames, tmp_path / "closeups", target_px=800
    )
    if not hash_ok:
        assert out["rendered_count"] == 0
        assert "SOURCE_SHA256_MISMATCH" in out["failures"][0]["message"]
    else:
        ev = out["records"][0]["evidence"][0]
        assert ev["annotation_fidelity"] == "NATIVE_SUPPORTED_PAPER_ENTITIES"
        assert dim in {e["handle"] for e in ev["paper_entities"]}
        assert ev["source_sha256"] == sha256_file(path)
        assert ev["selected_occurrence_ids"] == ["occ"]
        assert ev["render_bbox"] != ev["requested_render_bbox"]
