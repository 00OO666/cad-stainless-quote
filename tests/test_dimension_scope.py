import copy

import ezdxf
import pytest
from cadquote.dimension_scope import probe_model_dimension_scope


def fixture(gap=False):
    doc = ezdxf.new()
    doc.header["$INSUNITS"] = 0
    layout = doc.layouts.get("Layout1")
    a = layout.add_viewport(
        center=(20, 40), size=(60, 60), view_center_point=(25, 0), view_height=60
    )
    b = layout.add_viewport(
        center=(300, 400), size=(60, 60), view_center_point=(100 if gap else 75, 0), view_height=60
    )
    d = doc.modelspace().add_linear_dim(base=(0, 15), p1=(0, 0), p2=(90, 0))
    d.render()
    return doc, a, b, d.dimension


def probe(doc, a, b, **kwargs):
    return probe_model_dimension_scope(
        doc,
        source_file_id="synthetic-source",
        scope_id="synthetic-scope",
        layout_name="Layout1",
        viewport_handles=[a.dxf.handle, b.dxf.handle],
        **kwargs,
    )


def test_split_window_uses_native_length_not_paper_distance():
    doc, a, b, d = fixture()
    before = copy.deepcopy(d.dxf.all_existing_dxf_attribs())
    out = probe(doc, a, b)
    assert out["source_insunits"] == 0 and out["physical_quantity"] is None
    assert len(out["dimensions"]) == 1 and not out["truncated"]
    r = out["dimensions"][0]
    assert r["scope_relation"] == "SPLIT_VIEWPORT_ORIGINS"
    assert r["entity"]["value"] == pytest.approx(90)
    assert r["entity"]["geometry"]["geometric_measurement"] == pytest.approx(90)
    assert r["dimension_role"] is None and r["state"] == "REVIEW"
    assert d.dxf.all_existing_dxf_attribs() == before


def test_gap_between_native_windows_cannot_prove_complete_span():
    out = probe(*fixture(gap=True)[:3])
    assert out["dimensions"] == []
    assert out["rejected_cross_window_spans"][0]["reason"] == "MODEL_SPAN_NOT_FULLY_COVERED"


def test_dimension_visible_in_both_windows_is_not_double_counted():
    doc, a, b, _ = fixture()
    d = doc.modelspace().add_linear_dim(base=(0, 15), p1=(47, 0), p2=(52, 0))
    d.render()
    out = probe(doc, a, b)
    matches = [r for r in out["dimensions"] if r["entity"]["handle"] == d.dimension.dxf.handle]
    assert len(matches) == 1 and len(matches[0]["common_viewport_handles"]) == 2


@pytest.mark.parametrize(
    "mode", ["hidden", "frozen_layer", "missing_origin", "outside_origin", "angular"]
)
def test_invisible_or_unowned_dimensions_excluded(mode):
    doc, a, b, d = fixture()
    if mode == "hidden":
        d.dxf.invisible = 1
    elif mode == "frozen_layer":
        doc.layers.new("measurement-layer")
        d.dxf.layer = "measurement-layer"
        b.frozen_layers = ["measurement-layer"]
    elif mode == "missing_origin":
        d.dxf.discard("defpoint3")
    elif mode == "outside_origin":
        d.dxf.defpoint3 = (1000, 0, 0)
    else:
        d.dxf.dimtype = 34
    assert probe(doc, a, b)["dimensions"] == []


@pytest.mark.parametrize("mode", ["wrong_layout", "inactive", "perspective", "tilted", "duplicate"])
def test_invalid_viewport_scope_rejected(mode):
    doc, a, b, _ = fixture()
    if mode == "wrong_layout":
        a.get_layout().move_to_layout(a, doc.layouts.new("foreign"))
    elif mode == "inactive":
        b.dxf.status = 0
    elif mode == "perspective":
        b.dxf.flags = b.dxf.flags | 1
    elif mode == "tilted":
        b.dxf.view_direction_vector = (1, 0, 1)
    else:
        b = a
    with pytest.raises(ValueError):
        probe(doc, a, b)


def test_cap_cannot_leave_partial_inventory():
    doc, a, b, _ = fixture()
    doc.modelspace().add_line((0, 0), (1, 1))
    out = probe(doc, a, b, max_model_entities=1)
    assert out["truncated"] and out["dimensions"] == []
