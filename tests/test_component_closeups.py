from pathlib import Path

import cadquote.component_closeups as component_closeups_module
from cadquote.component_closeups import render_component_frame_closeups
from PIL import Image


def test_component_closeups_are_fresh_vector_renders_and_remain_review(
    tmp_path: Path,
    monkeypatch,
):
    source = tmp_path / "drawing.dxf"
    source.write_text("synthetic", encoding="utf-8")
    index = {
        "sources": [
            {
                "source_file_id": "file:1",
                "source_path": str(source),
            }
        ]
    }
    panels = {
        "sheets": [
            {
                "id": "panel:1",
                "source_file_id": "file:1",
                "drawing_number": "E-01",
                "kind": "elevation",
                "bbox": [0, 0, 100, 100],
            }
        ]
    }
    frames = {
        "records": [
            {
                "selection_key": "component:1",
                "sequence": 1,
                "frames": [
                    {
                        "group_id": "group:1",
                        "sheet_id": "panel:1",
                        "object_bbox": [10, 10, 60, 60],
                        "dimension_bboxes": [[5, 20, 80, 90]],
                        "state": "REVIEW",
                        "reason_codes": ["ALGORITHMIC_GEOMETRY_ENVELOPE_REQUIRES_CONFIRMATION"],
                    }
                ],
            }
        ]
    }

    def fake_render_regions(_source, regions, output_dir, **kwargs):
        output = Path(output_dir)
        output.mkdir(parents=True, exist_ok=True)
        rendered = {}
        for label, bbox in regions.items():
            path = output / f"{label}.png"
            Image.new("RGB", (160, 90), "#111820").save(path)
            rendered[label] = {
                "file": path.name,
                "bbox": list(bbox),
                "backend": "synthetic-vector",
                "entity_count": 7,
            }
        assert kwargs["target_px"] == 1024
        assert kwargs["render_profile"] == "cad-dark-full"
        assert kwargs["mark_center"] is False
        return {"regions": rendered}

    monkeypatch.setattr(component_closeups_module, "render_regions", fake_render_regions)

    result = render_component_frame_closeups(
        index,
        panels,
        frames,
        tmp_path / "closeups",
        target_px=1024,
    )

    assert result["rendered_count"] == 1
    assert result["missing_count"] == 0
    record = result["records"][0]
    assert record["state"] == "REVIEW"
    assert record["reason_codes"] == ["FRAME_REQUIRES_CONFIRMATION"]
    evidence = record["evidence"][0]
    assert evidence["render_bbox"] == [5.0, 10.0, 80.0, 90.0]
    assert evidence["pixel_size"] == [160, 90]
    assert evidence["backend"] == "synthetic-vector"
    assert evidence["frame_state"] == "REVIEW"
    assert len(evidence["image_sha256"]) == 64
    assert Path(evidence["absolute_path"]).is_file()


def test_component_closeups_fail_closed_when_source_dxf_is_missing(tmp_path: Path):
    result = render_component_frame_closeups(
        {
            "sources": [
                {
                    "source_file_id": "file:1",
                    "source_path": str(tmp_path / "missing.dxf"),
                }
            ]
        },
        {
            "sheets": [
                {
                    "id": "panel:1",
                    "source_file_id": "file:1",
                    "bbox": [0, 0, 100, 100],
                }
            ]
        },
        {
            "records": [
                {
                    "sequence": 1,
                    "frames": [
                        {
                            "group_id": "group:1",
                            "sheet_id": "panel:1",
                            "object_bbox": [10, 10, 60, 60],
                        }
                    ],
                }
            ]
        },
        tmp_path / "missing-closeups",
    )

    assert result["rendered_count"] == 0
    assert result["missing_count"] == 1
    assert result["records"][0]["state"] == "MISSING"
    assert result["records"][0]["reason_codes"] == ["SOURCE_DXF_MISSING"]
