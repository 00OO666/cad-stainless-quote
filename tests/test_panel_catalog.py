from pathlib import Path
from types import SimpleNamespace

import pytest
from cadquote.panel_catalog import (
    merge_panel_catalogs,
    overlay_panel_catalog_annotations,
    panel_catalog_is_incomplete,
    render_panel_catalog,
)
from PIL import Image


def test_panel_catalog_renders_panels_without_mt_occurrences(tmp_path: Path, monkeypatch):
    source = tmp_path / "source.dxf"
    source.write_text("fixture", encoding="utf-8")
    calls = []

    def fake_render_regions(
        dxf_path,
        regions,
        output_dir,
        *,
        layout,
        margin_ratio,
        target_px,
        mark_center,
        render_profile,
    ):
        calls.append((dxf_path, regions, render_profile))
        output_dir.mkdir(parents=True, exist_ok=True)
        records = {}
        for sheet_id, bbox in regions.items():
            image = output_dir / f"{sheet_id.replace(':', '_')}.png"
            image.write_bytes(b"png")
            records[sheet_id] = {
                "file": image.name,
                "bbox": list(bbox),
                "layout": "Model",
                "render_profile": render_profile,
            }
        return {"regions": records, "skipped_entity_type_counts": {"WIPEOUT": 2}}

    monkeypatch.setattr("cadquote.panel_catalog.render_regions", fake_render_regions)
    sheets = [
        SimpleNamespace(
            id="panel:detail",
            source_file_id="source:1",
            drawing_number="D-01",
            title="节点",
            kind="detail",
            layout="Layout#viewport:AA",
            bbox=[0, 0, 100, 50],
        ),
        SimpleNamespace(
            id="sheet:model",
            source_file_id="source:1",
            drawing_number=None,
            title="model",
            kind="unknown",
            layout="Model",
            bbox=[0, 0, 500, 500],
        ),
    ]

    result = render_panel_catalog(
        sheets,
        {"source:1": source},
        tmp_path / "catalog",
        sheet_ids=["panel:detail"],
    )

    assert result["requested_count"] == 1
    assert result["requested_sheet_ids"] == ["panel:detail"]
    assert result["missing_requested_sheet_ids"] == []
    assert result["eligible_count"] == 1
    assert result["eligible_sheet_ids"] == ["panel:detail"]
    assert result["rendered_count"] == 1
    assert result["rendered_sheet_ids"] == ["panel:detail"]
    assert result["unrendered_eligible_sheet_ids"] == []
    assert result["truncated_eligible_sheet_ids"] == []
    assert result["truncated_requested_sheet_ids"] == []
    assert result["render_profile"] == "cad-dark-full"
    assert result["path_scope"] == "local_run_diagnostics"
    assert "must not be published or committed" in result["warning"]
    assert result["skipped_entity_type_counts"] == {"WIPEOUT": 2}
    assert list(result["panels"]) == ["panel:detail"]
    assert calls[0][2] == "cad-dark-full"
    assert (tmp_path / "catalog" / "panel_catalog.json").is_file()


def test_panel_catalog_overlays_projected_paper_text_and_leader(tmp_path: Path):
    source = tmp_path / "panel.png"
    Image.new("RGB", (400, 200), "#111820").save(source)
    panel_payload = {
        "entities": [
            {
                "id": "panel_paper_entity:text",
                "sheet_id": "panel:1",
                "entity_type": "ATTRIB",
                "text": "MT-01",
                "insert": [50, 50],
                "bbox": [50, 45, 70, 55],
                "geometry": {},
            },
            {
                "id": "panel_paper_entity:leader",
                "sheet_id": "panel:1",
                "entity_type": "LEADER",
                "geometry": {"vertices": [[20, 20], [50, 50], [70, 50]]},
            },
            {
                "id": "panel_entity:model",
                "sheet_id": "panel:1",
                "entity_type": "TEXT",
                "text": "do not duplicate model text",
                "insert": [20, 80],
                "geometry": {},
            },
        ]
    }
    catalog = {
        "render_profile": "cad-dark-full",
        "panels": {
            "panel:1": {
                "absolute_path": str(source),
                "bbox": [0, 0, 100, 100],
                "render_profile": "cad-dark-full",
            }
        },
    }

    result = overlay_panel_catalog_annotations(
        panel_payload,
        catalog,
        tmp_path / "annotated",
    )

    assert result["panel_count"] == 1
    assert result["annotation_count"] == 2
    assert result["path_scope"] == "local_run_diagnostics"
    assert "relative_path is authoritative" in result["warning"]
    target = Path(result["panels"]["panel:1"]["absolute_path"])
    assert target.is_file()
    with Image.open(source) as before, Image.open(target) as after:
        assert before.tobytes() != after.tobytes()


def test_merge_panel_catalogs_preserves_all_shards(tmp_path: Path):
    common = {
        "render_profile": "cad-dark-full",
        "target_px": 1_800,
        "failures": [],
        "skipped_entity_type_counts": {},
    }
    result = merge_panel_catalogs(
        [
            {
                **common,
                "requested_sheet_ids": ["panel:a"],
                "missing_requested_sheet_ids": [],
                "panels": {
                    "panel:a": {
                        "bbox": [0, 0, 10, 10],
                        "image_sha256": "a" * 64,
                    }
                },
            },
            {
                **common,
                "requested_sheet_ids": ["panel:b"],
                "missing_requested_sheet_ids": [],
                "panels": {
                    "panel:b": {
                        "bbox": [10, 10, 20, 20],
                        "image_sha256": "b" * 64,
                    }
                },
            },
        ],
        tmp_path / "merged",
    )

    assert result["shard_count"] == 2
    assert result["rendered_count"] == 2
    assert result["eligible_sheet_ids"] == ["panel:a", "panel:b"]
    assert result["rendered_sheet_ids"] == ["panel:a", "panel:b"]
    assert result["missing_requested_sheet_ids"] == []
    assert result["unrendered_eligible_sheet_ids"] == []
    assert result["truncated_eligible_sheet_ids"] == []
    assert result["path_scope"] == "local_run_diagnostics"
    assert sorted(result["panels"]) == ["panel:a", "panel:b"]


def test_panel_catalog_reports_requested_eligible_panel_when_renderer_returns_empty(
    tmp_path: Path,
    monkeypatch,
):
    source = tmp_path / "source.dxf"
    source.write_text("fixture", encoding="utf-8")

    def fake_render_regions(*args, **kwargs):
        return {"regions": {}, "skipped_entity_type_counts": {}}

    monkeypatch.setattr("cadquote.panel_catalog.render_regions", fake_render_regions)
    sheet = SimpleNamespace(
        id="panel:empty",
        source_file_id="source:1",
        drawing_number="D-02",
        title="empty render",
        kind="detail",
        layout="Layout#viewport:BB",
        bbox=[0, 0, 100, 50],
    )

    result = render_panel_catalog(
        [sheet],
        {"source:1": source},
        tmp_path / "catalog",
        sheet_ids=["panel:empty"],
    )

    assert result["requested_sheet_ids"] == ["panel:empty"]
    assert result["eligible_sheet_ids"] == ["panel:empty"]
    assert result["rendered_sheet_ids"] == []
    assert result["missing_requested_sheet_ids"] == ["panel:empty"]
    assert result["unrendered_eligible_sheet_ids"] == ["panel:empty"]
    assert result["truncated_requested_sheet_ids"] == []
    assert result["rendered_count"] == 0


def test_panel_catalog_exposes_explicit_and_implicit_maximum_truncation(
    tmp_path: Path,
    monkeypatch,
):
    source = tmp_path / "source.dxf"
    source.write_text("fixture", encoding="utf-8")

    def fake_render_regions(dxf_path, regions, output_dir, **kwargs):
        output_dir.mkdir(parents=True, exist_ok=True)
        records = {}
        for sheet_id, bbox in regions.items():
            image = output_dir / f"{sheet_id.replace(':', '_')}.png"
            image.write_bytes(b"png")
            records[sheet_id] = {"file": image.name, "bbox": list(bbox)}
        return {"regions": records, "skipped_entity_type_counts": {}}

    monkeypatch.setattr("cadquote.panel_catalog.render_regions", fake_render_regions)
    sheets = [
        SimpleNamespace(
            id=f"panel:{suffix}",
            source_file_id="source:1",
            drawing_number=None,
            title=suffix,
            kind="detail",
            layout=f"Layout#viewport:{suffix}",
            bbox=[0, 0, 100, 50],
        )
        for suffix in ("a", "b")
    ]

    explicit = render_panel_catalog(
        sheets,
        {"source:1": source},
        tmp_path / "explicit",
        maximum=1,
        sheet_ids=["panel:a", "panel:b", "panel:missing"],
    )

    assert explicit["eligible_sheet_ids"] == ["panel:a"]
    assert explicit["rendered_sheet_ids"] == ["panel:a"]
    assert explicit["truncated_eligible_sheet_ids"] == ["panel:b"]
    assert explicit["truncated_requested_sheet_ids"] == ["panel:b"]
    assert explicit["missing_requested_sheet_ids"] == ["panel:b", "panel:missing"]
    assert explicit["truncated_eligible_count"] == 1

    implicit = render_panel_catalog(
        sheets,
        {"source:1": source},
        tmp_path / "implicit",
        maximum=1,
    )

    assert implicit["requested_sheet_ids"] == []
    assert implicit["missing_requested_sheet_ids"] == []
    assert implicit["eligible_sheet_ids"] == ["panel:a"]
    assert implicit["rendered_sheet_ids"] == ["panel:a"]
    assert implicit["truncated_eligible_sheet_ids"] == ["panel:b"]
    assert implicit["truncated_eligible_count"] == 1
    assert implicit["truncated_requested_sheet_ids"] == []


@pytest.mark.parametrize(
    "incomplete_signal",
    [
        {"failures": [{"message": "failed"}], "failure_count": 1},
        {"missing_requested_sheet_ids": ["panel:missing"]},
        {"unrendered_eligible_sheet_ids": ["panel:empty"]},
        {"truncated_eligible_sheet_ids": ["panel:truncated"]},
        {"truncated_requested_sheet_ids": ["panel:truncated"]},
    ],
)
def test_panel_catalog_cli_gate_rejects_every_incomplete_signal(incomplete_signal):
    result = {
        "failures": [],
        "failure_count": 0,
        "missing_requested_sheet_ids": [],
        "unrendered_eligible_sheet_ids": [],
        "truncated_eligible_sheet_ids": [],
        "truncated_requested_sheet_ids": [],
        **incomplete_signal,
    }

    assert panel_catalog_is_incomplete(result) is True


def test_panel_catalog_cli_gate_accepts_complete_catalog():
    assert (
        panel_catalog_is_incomplete(
            {
                "failures": [],
                "failure_count": 0,
                "missing_requested_sheet_ids": [],
                "unrendered_eligible_sheet_ids": [],
                "truncated_eligible_sheet_ids": [],
                "truncated_requested_sheet_ids": [],
            }
        )
        is False
    )
