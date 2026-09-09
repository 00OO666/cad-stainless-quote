"""Fresh synthetic DXF -> index -> page ownership, not injected frame evidence."""

import ezdxf
import pytest
from cadquote.cad_index import index_dxf
from cadquote.panels import expand_viewport_panels


@pytest.mark.parametrize("own_code", ["L4-D-31A", "L4-D-31", ""])
def test_fresh_index_recovers_own_page_not_closer_reciprocal_title(tmp_path, own_code):
    doc = ezdxf.new("R2010")
    doc.modelspace().add_text("MT-01", dxfattribs={"insert": (40, 40), "height": 3})
    block = doc.blocks.new("SYNTHETIC_PAPER_SHEET")
    block.add_lwpolyline([(0, 0), (500, 0), (500, 420), (0, 420)], close=True)
    block.add_lwpolyline([(500, 0), (590, 0), (590, 420), (500, 420)], close=True)
    block.add_attdef("DWG_NO", (520, 15), height=2)
    block.add_attdef("SHEET_TITLE", (520, 30), height=2)
    local = doc.blocks.new("SYNTHETIC_VIEW_TITLE")
    local.add_circle((0, 0), 3)
    local.add_attdef("DWG_NO", (2, 0), height=2)
    local.add_attdef("VIEW_TITLE", (2, 4), height=2)
    paper = doc.layouts.get("Layout1")
    frame = paper.add_blockref(block.name, (1000, 1000))
    frame.add_auto_attribs({"DWG_NO": own_code, "SHEET_TITLE": "JOINERY DETAIL"})
    near = paper.add_blockref(local.name, (1230, 1170))
    near.add_auto_attribs({"DWG_NO": "L4-E-72", "VIEW_TITLE": "SECTION DETAIL"})
    viewport = paper.add_viewport(
        center=(1200, 1220),
        size=(80, 80),
        view_center_point=(40, 40),
        view_height=80,
    )
    source = tmp_path / "drawing.dxf"
    doc.saveas(source)
    before = source.read_bytes()
    index = index_dxf(source)
    indexed_frame = next(e for e in index.entities if e.handle == frame.dxf.handle)
    assert indexed_frame.bbox == (1000, 1000, 1000, 1000)
    scan = indexed_frame.geometry["paper_frame_scan"]
    assert scan["complete"] and not scan["issues"] and len(scan["rectangles"]) == 2
    result = expand_viewport_panels(index.sheets, index.entities)
    sheet = next(s for s in result.sheets if s.viewport_handle == viewport.dxf.handle)
    assert sheet.drawing_number == (own_code or None)
    if own_code:
        assert any("NATIVE_CLOSED_FRAME_CONTAINS_VIEWPORT" in e for e in sheet.evidence)
        assert any("paper_page_closed_frame:" in e for e in sheet.evidence)
    else:
        assert any("UNPARSEABLE_NATIVE_PAGE_FRAMES" in e for e in sheet.evidence)
    assert source.read_bytes() == before
