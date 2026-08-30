from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from cadquote.drawing_catalog import (
    build_drawing_catalog,
    preview_cache_key,
    render_sheet_previews,
    search_drawing_catalog,
    write_drawing_catalog_sqlite,
)
from PIL import Image


def _index_payload(tmp_path: Path) -> dict[str, object]:
    source = tmp_path / "source.dxf"
    source.write_text("fixture", encoding="utf-8")
    return {
        "sources": [
            {
                "source_file_id": "file:source",
                "source_path": str(source),
                "source_sha256": "a" * 64,
                "dxf_version": "AC1032",
                "units_code": 4,
                "units": "millimeters",
                "audit_error_count": 0,
                "audit_fix_count": 0,
                "recovered": False,
                "warnings": [],
                "sheets": [
                    {
                        "id": "sheet:plan",
                        "source_file_id": "file:source",
                        "drawing_number": "1F-E-01",
                        "title": "一层平面图",
                        "kind": "plan",
                        "layout": "Model",
                        "viewport_handle": None,
                        "bbox": [0, 0, 100, 50],
                        "confidence": 0.9,
                        "evidence": ["title:平面图"],
                    }
                ],
                "entities": [
                    {
                        "id": "entity:mt",
                        "source_file_id": "file:source",
                        "sheet_id": "sheet:plan",
                        "handle": "10",
                        "entity_type": "TEXT",
                        "layer": "MT",
                        "space": "model",
                        "text": "MT-01",
                        "value": None,
                        "text_override": None,
                        "insert": [20, 20],
                        "bbox": [20, 20, 25, 25],
                        "geometry": {},
                    },
                    {
                        "id": "entity:el",
                        "source_file_id": "file:source",
                        "sheet_id": "sheet:plan",
                        "handle": "11",
                        "entity_type": "TEXT",
                        "layer": "TEXT",
                        "space": "model",
                        "text": "EL-07-01 前厅",
                        "value": None,
                        "text_override": None,
                        "insert": [30, 20],
                        "bbox": [30, 20, 50, 25],
                        "geometry": {},
                    },
                    {
                        "id": "entity:dim",
                        "source_file_id": "file:source",
                        "sheet_id": "sheet:plan",
                        "handle": "12",
                        "entity_type": "DIMENSION",
                        "layer": "DIM",
                        "space": "model",
                        "text": None,
                        "value": 4200.0,
                        "text_override": None,
                        "insert": [40, 20],
                        "bbox": [35, 18, 45, 22],
                        "geometry": {"display_measurement": 4200.0},
                    },
                ],
            }
        ]
    }


def test_build_catalog_indexes_text_codes_and_dimensions(tmp_path: Path):
    catalog = build_drawing_catalog(_index_payload(tmp_path))

    assert catalog["schema_version"] == "1.0"
    assert catalog["source_count"] == 1
    assert catalog["sheet_count"] == 1
    assert catalog["entity_count"] == 3
    assert catalog["dimension_count"] == 1
    assert catalog["mt_index"] == {"MT-01": ["sheet:plan"]}
    assert catalog["drawing_index"]["EL-07-01"] == ["sheet:plan"]
    assert catalog["sheets"][0]["dimension_count"] == 1
    assert search_drawing_catalog(catalog, "MT-01")[0]["sheet_id"] == "sheet:plan"
    assert search_drawing_catalog(catalog, "EL-07-01", kind="detail") == []


def test_catalog_sqlite_is_queryable_and_repeatable(tmp_path: Path):
    catalog = build_drawing_catalog(_index_payload(tmp_path))
    database = write_drawing_catalog_sqlite(catalog, tmp_path / "catalog.sqlite")

    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT COUNT(*) FROM catalog_sheets").fetchone()[0] == 1
        assert connection.execute(
            "SELECT term FROM catalog_terms WHERE term = ?", ("mt-01",)
        ).fetchone() == ("mt-01",)
        assert connection.execute("SELECT COUNT(*) FROM catalog_dimensions").fetchone()[0] == 1


def test_preview_cache_key_changes_when_render_inputs_change():
    base = dict(
        source_sha256="a" * 64,
        sheet_id="sheet:1",
        layout="Model",
        bbox=[0, 0, 100, 50],
        target_px=1800,
        margin_ratio=0.02,
        render_profile="cad-dark",
    )
    first = preview_cache_key(**base)
    assert first != preview_cache_key(**{**base, "target_px": 2400})
    assert first != preview_cache_key(**{**base, "render_profile": "cad-dark-full"})
    assert first != preview_cache_key(**{**base, "bbox": [0, 0, 101, 50]})


def test_render_sheet_previews_reuses_content_addressed_cache(tmp_path: Path, monkeypatch):
    catalog = build_drawing_catalog(_index_payload(tmp_path))
    calls: list[dict[str, object]] = []

    def fake_render_regions(dxf_path, regions, output_dir, **kwargs):
        calls.append({"dxf_path": dxf_path, "regions": regions, **kwargs})
        output_dir.mkdir(parents=True, exist_ok=True)
        records = {}
        for sheet_id, bbox in regions.items():
            filename = "sheet_plan.png"
            image = output_dir / filename
            Image.new("RGB", (360, 180), "#111820").save(image)
            records[sheet_id] = {
                "file": filename,
                "bbox": list(bbox),
                "layout": kwargs["layout"],
                "render_profile": kwargs["render_profile"],
            }
        return {"regions": records, "skipped_entity_type_counts": {}}

    monkeypatch.setattr("cadquote.drawing_catalog.render_regions", fake_render_regions)
    first = render_sheet_previews(catalog, tmp_path / "preindex")
    assert len(calls) == 1
    assert first["previews"]["rendered_count"] == 1
    assert first["sheets"][0]["preview"]["status"] == "RENDERED"
    assert Path(first["sheets"][0]["preview"]["absolute_path"]).is_file()

    second = render_sheet_previews(first, tmp_path / "preindex")
    assert len(calls) == 1
    assert second["previews"]["reused_count"] == 1
    assert second["previews"]["failures"] == []
    json.loads((tmp_path / "preindex" / "preview_index.json").read_text(encoding="utf-8"))
