from cadquote.group_evidence import render_probed_groups
from cadquote.group_materials import probe_node_view_group
from cadquote.io import sha256_file
from test_group_materials import fixture


def test_native_probe_to_group_image_manifest(tmp_path):
    doc, group, *_ = fixture()
    layout = doc.layouts.get("Layout1")
    own = layout.add_text("NODE", dxfattribs={"insert": (150, -30)})
    other = layout.add_text("NEIGHBOUR", dxfattribs={"insert": (20, 10)})
    group.update(
        {
            "frame_handle": "synthetic-frame",
            "viewport_handles": [m["viewport_handle"] for m in group["members"]],
            "selected_title": {"parent_handle": own.dxf.handle},
            "title_candidates": [
                {"parent_handle": own.dxf.handle},
                {"parent_handle": other.dxf.handle},
            ],
        }
    )
    path = tmp_path / "source.dxf"
    doc.saveas(path)
    digest = sha256_file(path)
    source = {"source_file_id": "synthetic", "source_path": str(path), "source_sha256": digest}
    probe = probe_node_view_group(doc, group)
    probe["source_sha256"] = digest
    routes = {"node_view_groups": {"groups": [group]}}
    result = render_probed_groups(
        {"sources": [source]}, routes, {"results": {"g": probe}}, tmp_path / "images", target_px=512
    )
    assert result["rendered_groups"] == 1 and not result["issues"]
    image = result["images"]["g"]
    assert max(image["natural_pixels"]) == 512
    assert image["source_sha256"] == digest and image["state"] == "REVIEW"
    assert image["excluded_paper_annotation_handles"] == [other.dxf.handle]
    assert sha256_file(path) == digest
    probe["source_sha256"] = "wrong"
    failed = render_probed_groups(
        {"sources": [source]}, routes, {"results": {"g": probe}}, tmp_path / "bad", target_px=512
    )
    assert failed["rendered_groups"] == 0 and failed["issues"]
