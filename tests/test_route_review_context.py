"""Synthetic run/resume wiring and fail-closed route-context regressions."""

import hashlib
import json
from copy import deepcopy
from pathlib import Path

import cadquote.pipeline as pipeline
import ezdxf
import pytest
from cadquote.route_review_context import load_route_review_context


def read(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def write(path, value):
    Path(path).write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def synthetic_routes(sheets, entities, native):
    """Navigation ambiguity fixture, not a replacement for pipeline execution."""
    sheet = sheets[0]
    return {
        "schema_version": "numbered-detail-routes/1.1",
        "state": "REVIEW",
        "mutates_takeoff": False,
        "summary": {"AMBIGUOUS": 1},
        "records": [
            {
                "route_id": "synthetic:route",
                "source_file_id": sheet.source_file_id,
                "source_sheet_id": sheet.id,
                "target_page": "QS-01",
                "target_view": "01",
                "resolved_source_page": "EL-01",
                "reference_role": "OUTGOING_CALLOUT",
                "navigation_state": "AMBIGUOUS",
                "state": "REVIEW",
                "candidates": [
                    {
                        "candidate_id": "first",
                        "sheet_id": sheet.id,
                        "source_file_id": sheet.source_file_id,
                        "state": "PASS",
                    },
                    {
                        "candidate_id": "second",
                        "sheet_id": sheet.id,
                        "source_file_id": sheet.source_file_id,
                        "state": "REVIEW",
                    },
                ],
            }
        ],
    }


@pytest.fixture
def run(tmp_path, monkeypatch):
    source = tmp_path / "synthetic.dxf"
    drawing = ezdxf.new("R2018")
    drawing.modelspace().add_text("平面布置图", dxfattribs={"insert": (0, 100)})
    drawing.modelspace().add_text("MT-01", dxfattribs={"insert": (20, 20)})
    drawing.saveas(source)
    monkeypatch.setattr(pipeline, "build_detail_routes", synthetic_routes)
    # Deliberately disable only workbook authoring/rendering, never the real
    # run/resume/index/review-pack flow. No customer data is opened.
    result = pipeline.run_pipeline(
        source, tmp_path / "run", render_evidence=False, export_workbook=False
    )
    root = Path(result.run_dir)
    return root, source, result


def context(root):
    return read(root / "review-pack.json")["detail_route_context"]


def test_real_run_resume_preserves_all_candidates_without_promoting_takeoff(run, monkeypatch):
    root, source, result = run
    first = context(root)
    assert first["available"] is True
    assert first["state"] == "REVIEW"
    assert first["selection"] is None
    assert first["mutates_takeoff"] is False
    assert first["routes"]["summary"] == {"AMBIGUOUS": 1}
    candidates = first["routes"]["records"][0]["candidates"]
    assert [r["candidate_id"] for r in candidates] == ["first", "second"]
    assert [r["state"] for r in candidates] == ["REVIEW", "REVIEW"]
    assert candidates[0]["original_state"] == "PASS"
    assert all(r["candidate_effective_state"] == "REVIEW" for r in candidates)
    raw_routes = read(root / "analysis/detail_routes.json")
    assert raw_routes["records"][0]["candidates"][0]["state"] == "PASS"
    before_takeoff = read(root / "outputs/takeoff.json")
    assert all(row["status"] != "PASS" for row in before_takeoff)
    frozen = [
        source,
        root / "index/cad_index.json",
        root / "analysis/panels.json",
        root / "analysis/detail_routes.json",
        root / "analysis/detail_route_review_receipt.json",
    ]
    before = {str(path): (digest(path), path.stat().st_mtime_ns) for path in frozen}
    monkeypatch.setattr(
        pipeline,
        "build_detail_routes",
        lambda *a, **k: pytest.fail("resume must not regenerate routes"),
    )
    resumed = pipeline.resume_pipeline(root, render_evidence=False, export_workbook=False)
    assert context(root) == first
    assert read(root / "outputs/takeoff.json") == before_takeoff
    assert resumed.quote_path is None and result.quote_path is None
    assert {str(path): (digest(path), path.stat().st_mtime_ns) for path in frozen} == before


@pytest.mark.parametrize(
    "mutation,reason",
    [
        ("missing_route", "ROUTE_CONTEXT_MISSING"),
        ("legacy_receipt", "LEGACY_ROUTE_RECEIPT_MISSING"),
        ("invalid_json", "INVALID_ROUTE_JSON"),
        ("route_version", "UNSUPPORTED_ROUTE_SCHEMA"),
        ("nested_version", "UNSUPPORTED_ROUTE_SECTION_SCHEMA"),
        ("receipt_version", "UNSUPPORTED_ROUTE_RECEIPT_VERSION"),
        ("producer_version", "UNSUPPORTED_ROUTE_RECEIPT_VERSION"),
        ("receipt_corrupt", "INVALID_ROUTE_RECEIPT_JSON"),
        ("route_drift", "ROUTE_RECEIPT_MISMATCH"),
        ("index_drift", "ROUTE_RECEIPT_MISMATCH"),
        ("panel_drift", "ROUTE_RECEIPT_MISMATCH"),
        ("source_drift", "INDEXED_SOURCE_HASH_MISMATCH"),
        ("cross_source", "CROSS_SOURCE_ROUTE_REFERENCE"),
        ("cross_sheet", "UNKNOWN_ROUTE_SHEET"),
        ("duplicate_route", "INVALID_ROUTE_RECORDS"),
    ],
)
def test_real_resume_marks_unusable_context_and_never_repairs_history(run, mutation, reason):
    root, _, _ = run
    route = root / "analysis/detail_routes.json"
    receipt = root / "analysis/detail_route_review_receipt.json"
    data = read(route)
    before_takeoff = read(root / "outputs/takeoff.json")
    if mutation == "missing_route":
        route.unlink()
    elif mutation == "legacy_receipt":
        receipt.unlink()
    elif mutation == "invalid_json":
        route.write_text("{", encoding="utf-8")
    elif mutation == "receipt_corrupt":
        receipt.write_text("[", encoding="utf-8")
    elif mutation in ("receipt_version", "producer_version"):
        rec = read(receipt)
        key = "schema_version" if mutation == "receipt_version" else "producer_version"
        rec[key] = "future/99"
        write(receipt, rec)
    elif mutation in ("index_drift", "panel_drift"):
        path = root / (
            "index/cad_index.json" if mutation == "index_drift" else "analysis/panels.json"
        )
        obj = read(path)
        obj["changed"] = True
        write(path, obj)
    elif mutation == "source_drift":
        src = Path(read(root / "index/cad_index.json")["sources"][0]["source_path"])
        src.write_bytes(src.read_bytes() + b"\n")
    else:
        if mutation == "route_version":
            data["schema_version"] = "future/99"
        elif mutation == "nested_version":
            data["node_view_groups"]["schema_version"] = "future/99"
        elif mutation == "route_drift":
            data["records"][0]["candidates"].reverse()
        elif mutation == "cross_source":
            data["records"][0]["candidates"][0]["source_file_id"] = "another-package"
        elif mutation == "cross_sheet":
            data["records"][0]["candidates"][0]["sheet_id"] = "another-sheet"
        elif mutation == "duplicate_route":
            data["records"].append(deepcopy(data["records"][0]))
        write(route, data)
    before = {str(p): digest(p) for p in (route, receipt) if p.exists()}
    pipeline.resume_pipeline(root, render_evidence=False, export_workbook=False)
    ctx = context(root)
    assert ctx["available"] is False
    assert ctx["reason_codes"] == [reason]
    assert ctx["routes"] is None and ctx["selection"] is None
    assert read(root / "outputs/takeoff.json") == before_takeoff
    assert {str(p): digest(p) for p in (route, receipt) if p.exists()} == before


def test_missing_sources_or_mismatched_source_pair_fail_closed(run):
    root, _, _ = run
    original_sheets, original_entities, _ = pipeline._load_index_snapshot(
        root / "index/cad_index.json"
    )
    panel = pipeline._load_panel_snapshot(root / "analysis/panels.json")
    sheets, _ = pipeline.choose_analysis_view(original_sheets, original_entities, panel)
    route_path = root / "analysis/detail_routes.json"
    data = read(route_path)
    data["records"][0]["source_sheet_id"] = "foreign-sheet"
    write(route_path, data)
    ctx = load_route_review_context(root, sheets)
    assert ctx["reason_codes"] == ["ROUTE_SHEET_SOURCE_MISMATCH"]
    source = Path(read(root / "index/cad_index.json")["sources"][0]["source_path"])
    source.unlink()
    ctx = load_route_review_context(root, sheets)
    assert ctx["reason_codes"] == ["INDEXED_SOURCE_UNAVAILABLE"]


def test_json_only_does_not_export_or_delete_existing_workbook(run, monkeypatch):
    root, _, _ = run
    book = root / "outputs/不锈钢算量报价.xlsx"
    assert not book.exists()
    book.write_bytes(b"synthetic preexisting workbook sentinel")
    before = digest(book)
    monkeypatch.setattr(
        pipeline,
        "build_quote_workbook",
        lambda *a, **k: pytest.fail("JSON-only must not author a workbook"),
    )
    resumed = pipeline.resume_pipeline(root, render_evidence=False, export_workbook=False)
    assert resumed.quote_path is None
    assert resumed.workbook_export == {
        "enabled": False,
        "state": "SKIPPED",
        "reason": "requested_json_review_only",
    }
    assert read(root / "review-pack.json")["metadata"]["workbook_export"] == resumed.workbook_export
    assert read(root / "manifest.json")["metadata"]["workbook_export"] == resumed.workbook_export
    assert read(root / "run.json")["quote_path"] is None
    assert digest(book) == before


def test_fresh_json_only_does_not_call_exporter(tmp_path, monkeypatch):
    source = tmp_path / "synthetic.dxf"
    ezdxf.new("R2018").saveas(source)
    monkeypatch.setattr(
        pipeline,
        "build_quote_workbook",
        lambda *a, **k: pytest.fail("JSON-only must not author a workbook"),
    )
    result = pipeline.run_pipeline(
        source, tmp_path / "run", render_evidence=False, export_workbook=False
    )
    assert result.quote_path is None
    assert Path(result.paths["review_pack"]).is_file()


def test_review_context_does_not_mutate_caller_payload(run):
    root, _, _ = run
    raw = read(root / "analysis/detail_routes.json")
    before = deepcopy(raw)
    ctx = context(root)
    ctx["routes"]["records"][0]["candidates"].clear()
    assert read(root / "analysis/detail_routes.json") == before
