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
