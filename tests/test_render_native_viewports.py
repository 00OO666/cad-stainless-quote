import ezdxf
from cadquote.render import render_regions


def test_paper_viewport_crop_uses_native_rectangle_not_model_extents(tmp_path, monkeypatch):
    import cadquote.render as render
    import ezdxf.addons.drawing.pipeline as pipeline

    doc = ezdxf.new("R2018")
    doc.modelspace().add_line((2000, 2000), (2100, 2100))
    paper = doc.layouts.get("Layout1")
    paper.add_viewport(
        center=(50, 50), size=(100, 100), view_center_point=(2050, 2050), view_height=100
    )
    paper.add_viewport(
        center=(1050, 50), size=(100, 100), view_center_point=(2050, 2050), view_height=100
    )
    source = tmp_path / "native.dxf"
    doc.saveas(source)
    before = source.read_bytes()
    extents = render.ezbbox.extents
    queried = []
    caches = []
    filter_entities = pipeline.filter_vp_entities

    def capture_cache(msp, limits, bbox_cache=None):
        caches.append(bbox_cache)
        return filter_entities(msp, limits, bbox_cache)

    def spy(entities, *args, **kwargs):
        values = list(entities)
        queried.extend(e.dxftype() for e in values)
        return extents(values, *args, **kwargs)

    monkeypatch.setattr(render.ezbbox, "extents", spy)
    monkeypatch.setattr(pipeline, "filter_vp_entities", capture_cache)
    out = render_regions(
        source,
        {"selected": (0, 0, 100, 100)},
        tmp_path / "images",
        layout="Layout1",
        margin_ratio=0,
        target_px=512,
        mark_center=False,
        render_profile="cad-dark-full",
    )
    assert out["rendered_count"] == 1
    assert out["regions"]["selected"]["entity_count"] == 1
    assert "VIEWPORT" not in queried
    assert caches and all(cache is not None for cache in caches)
    assert source.read_bytes() == before


def test_group_crop_omits_unselected_neighbour_viewports(tmp_path):
    import pytest

    doc = ezdxf.new("R2018")
    paper = doc.layouts.get("Layout1")
    doc.modelspace().add_line((0, 0), (100, 100))
    views = [
        paper.add_viewport(
            center=(50 + 104 * i, 50), size=(100, 100), view_center_point=(50, 50), view_height=100
        )
        for i in range(3)
    ]
    path = tmp_path / "native.dxf"
    doc.saveas(path)
    handles = [v.dxf.handle for v in views[:2]]
    out = render_regions(
        path,
        {"group": (0, 0, 312, 100)},
        tmp_path / "images",
        layout="Layout1",
        target_px=512,
        margin_ratio=0,
        render_profile="cad-dark-full",
        paper_viewport_sets={"group": handles},
    )
    assert out["regions"]["group"]["paper_viewport_selection"] == sorted(handles)
    assert out["regions"]["group"]["entity_count"] == 2
    with pytest.raises(ValueError, match="same-layout"):
        render_regions(
            path,
            {"bad": (0, 0, 100, 100)},
            tmp_path / "bad",
            layout="Layout1",
            paper_viewport_sets={"bad": ["MISSING"]},
        )


def test_adjacent_title_exclusion_is_explicit_and_does_not_modify_source(tmp_path):
    doc = ezdxf.new("R2018")
    layout = doc.layouts.get("Layout1")
    title = layout.add_text("OTHER VIEW", dxfattribs={"insert": (20, 20), "height": 5})
    layout.add_line((0, 0), (100, 100))
    path = tmp_path / "native.dxf"
    doc.saveas(path)
    before = path.read_bytes()
    result = render_regions(
        path,
        {"view": (0, 0, 100, 100)},
        tmp_path / "images",
        layout="Layout1",
        target_px=512,
        paper_excluded_handles={"view": [title.dxf.handle]},
    )
    assert result["regions"]["view"]["entity_count"] == 1
    assert result["regions"]["view"]["excluded_paper_annotation_handles"] == [title.dxf.handle]
    assert path.read_bytes() == before
