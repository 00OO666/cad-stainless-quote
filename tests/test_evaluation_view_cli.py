"""Synthetic CLI coverage for independent, reproducible view-disposition inputs."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest
from cad_quote import main
from cadquote.models import EvaluationPolicy


def write_json(path: Path, payload: object) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def context(side: str, field: str) -> dict:
    """Independent selected-view facts, created without inspecting either row."""
    governing_role = "section" if field == "elevation" else "elevation"
    return {
        "schema_version": "view-disposition-context/1",
        "side": side,
        "index_sha256": "a" * 64,
        "source_sha256": {"synthetic-source": "b" * 64},
        "views": [
            {
                "id": "selected-plan", "component_id": "synthetic-component", "role": "plan",
                "sheet_id": "plan-sheet", "source_id": "synthetic-source",
                "state": "CONFIRMED", "evidence_ids": ["plan-entity"],
            },
            {
                "id": "selected-governing", "component_id": "synthetic-component",
                "role": governing_role, "sheet_id": "governing-sheet",
                "source_id": "synthetic-source", "state": "CONFIRMED",
                "evidence_ids": ["dimension-entity"],
            },
        ],
        "entities": [
            {
                "id": "plan-entity", "component_id": "synthetic-component",
                "entity_type": "MTEXT", "sheet_id": "plan-sheet",
                "source_id": "synthetic-source",
            },
            {
                "id": "dimension-entity", "component_id": "synthetic-component",
                "entity_type": "DIMENSION", "sheet_id": "governing-sheet",
                "source_id": "synthetic-source",
            },
        ],
        "connections": [{
            "id": "selected-connection", "component_id": "synthetic-component",
            "source_view_id": "selected-plan", "target_view_id": "selected-governing",
            "state": "CONFIRMED", "evidence_ids": ["plan-entity"],
        }],
        "searches": [{
            "id": "completed-search", "component_id": "synthetic-component",
            "excluded_stage": field, "searched_sheet_ids": ["plan-sheet", "governing-sheet"],
            "complete": True, "unresolved_reference_ids": [], "conflicting_view_ids": [],
        }],
    }


def prepare_inputs(tmp_path: Path, field: str = "elevation") -> None:
    policy = EvaluationPolicy().model_dump(mode="json")
    policy["policy_version"] = "synthetic-view-cli-v1"
    policy["length_mm"]["relative_tolerance"] = 0.05
    policy["quantity"]["relative_tolerance"] = 0.05
    write_json(tmp_path / "policy.json", policy)
    row = {
        "sequence": 1, "component_id": "synthetic-component", "name": "synthetic trim",
        "mt_code": "MT-01", "plan_location": "P-01", "elevation": "E-01", "detail": "D-01",
        "unfolded_spec": "20+30", "length_mm": 1000, "quantity": 2,
        "engineering_quantity": 0.1, "evidence_ids": ["plan-entity", "dimension-entity"],
        "view_dispositions": {
            field: {
                "state": "NOT_APPLICABLE", "component_id": "synthetic-component",
                "basis_kind": (
                    "plan_section" if field == "elevation" else "plan_elevation_without_detail"
                ),
                "basis": "Reviewed governing geometry and complete reference search.",
                "index_sha256": "a" * 64, "source_sha256": {"synthetic-source": "b" * 64},
                "support_view_ids": ["selected-plan", "selected-governing"],
                "connection_id": "selected-connection", "search_id": "completed-search",
                "evidence_ids": ["plan-entity", "dimension-entity"],
                "review": {
                    "reviewer": "synthetic-reviewer", "reviewed_at": "2026-09-10T10:00:00+08:00",
                    "reason": "Reviewed selected views and scope of the completed search.",
                },
            }
        },
    }
    row[field] = None
    for side in ("predicted", "gold"):
        write_json(tmp_path / f"{side}.json", [row])
        write_json(tmp_path / f"{side}-context.json", context(side, field))


def evaluate_args(tmp_path: Path, *, predicted_context: bool = True) -> list[str]:
    arguments = [
        "evaluate", str(tmp_path / "predicted.json"), str(tmp_path / "gold.json"),
        "--policy", str(tmp_path / "policy.json"), "--out", str(tmp_path / "report.json"),
        "--gold-view-context", str(tmp_path / "gold-context.json"),
    ]
    if predicted_context:
        arguments.extend(["--predicted-view-context", str(tmp_path / "predicted-context.json")])
    return arguments


@pytest.mark.parametrize("field", ["elevation", "detail"])
def test_evaluate_cli_loads_independent_contexts_and_records_reproducible_sources(tmp_path, field):
    prepare_inputs(tmp_path, field)
    args = evaluate_args(tmp_path)
    assert main(args) == 0
    report = json.loads((tmp_path / "report.json").read_text(encoding="utf-8"))

    assert report["overall_gate"] == "PASS"
    assert report["correct_rows"] == report["eligible_gold_rows"] == 1
    assert report["replication_recall"] == report["output_precision"] == 1
    assert report["row_results"][0]["field_results"][field]["reason"] == (
        "audited_not_applicable_on_both_sides"
    )
    for side in ("predicted", "gold"):
        path = tmp_path / f"{side}-context.json"
        assert report["inputs"][f"{side}_view_context"] == str(path.resolve())
        assert report["inputs"][f"{side}_view_context_sha256"] == hashlib.sha256(
            path.read_bytes()
        ).hexdigest()
    assert report["inputs"]["predicted_view_context_sha256"] != (
        report["inputs"]["gold_view_context_sha256"]
    )
    assert main(args) == 0
    assert json.loads((tmp_path / "report.json").read_text(encoding="utf-8")) == report


def test_evaluate_cli_never_uses_gold_context_as_prediction_default(tmp_path):
    prepare_inputs(tmp_path)
    assert main(evaluate_args(tmp_path, predicted_context=False)) == 0
    report = json.loads((tmp_path / "report.json").read_text(encoding="utf-8"))
    assert report["overall_gate"] == "FAIL"
    assert report["eligible_gold_rows"] == 1
    assert report["correct_rows"] == 0
    assert "predicted_view_context" not in report["inputs"]
    assert "predicted_view_context_sha256" not in report["inputs"]
    assert report["row_results"][0]["field_results"]["elevation"]["reason"] == (
        "predicted_view_context_missing"
    )


@pytest.mark.parametrize("bad_value, expected", [
    ("missing", "FileNotFoundError"),
    ("{", "JSONDecodeError"),
    ("[]", "must be a JSON object"),
    ("{}", "view_context_invalid"),
    ("wrong-side", "view_context_side_mismatch"),
])
def test_evaluate_cli_bad_context_fails_explicitly(tmp_path, capsys, bad_value, expected):
    prepare_inputs(tmp_path)
    path = tmp_path / "predicted-context.json"
    if bad_value == "missing":
        path.unlink()
    elif bad_value == "wrong-side":
        write_json(path, context("gold", "elevation"))
    else:
        path.write_text(bad_value, encoding="utf-8")

    assert main(evaluate_args(tmp_path)) == 2
    output = json.loads(capsys.readouterr().out)
    assert expected in output["error"]
    assert path.name in output["error"]
    assert not (tmp_path / "report.json").exists()


def test_evaluate_cli_refuses_to_overwrite_context(tmp_path):
    prepare_inputs(tmp_path)
    args = evaluate_args(tmp_path)
    path = tmp_path / "predicted-context.json"
    before = path.read_bytes()
    args[args.index("--out") + 1] = str(path)
    assert main(args) == 2
    assert path.read_bytes() == before


@pytest.mark.parametrize("field", ["predicted", "gold", "policy"])
def test_evaluate_cli_refuses_to_overwrite_original_inputs(tmp_path, monkeypatch, capsys, field):
    prepare_inputs(tmp_path)
    path = tmp_path / f"{field}.json"
    before = path.read_bytes()
    args = evaluate_args(tmp_path)
    # Absolute input paths and a relative output alias must resolve to the same file.
    monkeypatch.chdir(tmp_path)
    args[args.index("--out") + 1] = f"./{field}.json"

    assert main(args) == 2
    output = json.loads(capsys.readouterr().out)
    assert f"would overwrite {field}" in output["error"]
    assert str(path.resolve()) in output["error"]
    assert path.read_bytes() == before


def batch_project(project_id: str = "synthetic-pass") -> dict:
    return {
        "project_id": project_id, "predicted": "predicted.json", "gold": "gold.json",
        "predicted_view_context": "predicted-context.json",
        "gold_view_context": "gold-context.json",
    }


@pytest.mark.parametrize("invalid_context,expected_error", [
    ("missing-context.json", "FileNotFoundError"),
    ("malformed-context.json", "JSONDecodeError"),
    ("gold-context.json", "ValueError"),
])
def test_batch_contexts_are_project_local_and_input_errors_remain_visible(
    tmp_path, invalid_context, expected_error
):
    prepare_inputs(tmp_path)
    (tmp_path / "malformed-context.json").write_text("{", encoding="utf-8")
    invalid = {**batch_project("synthetic-invalid"), "predicted_view_context": invalid_context}
    missing_prediction_context = batch_project("synthetic-no-prediction-context")
    missing_prediction_context.pop("predicted_view_context")
    write_json(tmp_path / "batch.json", {
        "schema_version": "1.0", "batch_id": "synthetic-view-batch", "policy": "policy.json",
        "projects": [batch_project(), invalid, missing_prediction_context],
    })
    output = tmp_path / "output"
    args = ["evaluate-batch", str(tmp_path / "batch.json"), "--out", str(output)]
    assert main(args) == 0
    summary = json.loads((output / "summary.json").read_text(encoding="utf-8"))
    projects = summary["projects"]
    assert summary["overall_gate"] == "BLOCKED"
    assert [project["overall_gate"] for project in projects] == ["PASS", "BLOCKED", "FAIL"]
    assert projects[1]["error"]["type"] == expected_error
    assert invalid_context in projects[1]["error"]["message"]
    assert projects[2]["correct_rows"] == 0
    assert "predicted_view_context" not in projects[2]["inputs"]
    for side in ("predicted", "gold"):
        path = tmp_path / f"{side}-context.json"
        assert projects[0]["inputs"][f"{side}_view_context"] == path.name
        assert projects[0]["inputs"][f"{side}_view_context_sha256"] == hashlib.sha256(
            path.read_bytes()
        ).hexdigest()
    if invalid_context == "malformed-context.json":
        assert projects[1]["inputs"]["predicted_view_context_sha256"] == hashlib.sha256(
            (tmp_path / invalid_context).read_bytes()
        ).hexdigest()
    report = json.loads((output / projects[0]["report_path"]).read_text(encoding="utf-8"))
    assert report["batch_context"]["inputs"] == projects[0]["inputs"]
    assert main(args) == 0
    assert json.loads((output / "summary.json").read_text(encoding="utf-8")) == summary


def test_batch_context_files_are_protected_from_output_collision(tmp_path):
    prepare_inputs(tmp_path)
    context_path = tmp_path / "summary.json"
    write_json(context_path, context("predicted", "elevation"))
    before = context_path.read_bytes()
    write_json(tmp_path / "batch.json", {
        "schema_version": "1.0", "policy": "policy.json",
        "projects": [{**batch_project(), "predicted_view_context": "summary.json"}],
    })
    assert main(["evaluate-batch", str(tmp_path / "batch.json"), "--out", str(tmp_path)]) == 2
    assert context_path.read_bytes() == before


@pytest.mark.parametrize("invalid_path", ["", "  ", False, {}, []])
def test_batch_requires_nonempty_context_path_strings(tmp_path, invalid_path):
    prepare_inputs(tmp_path)
    write_json(tmp_path / "batch.json", {
        "schema_version": "1.0", "policy": "policy.json",
        "projects": [{**batch_project(), "predicted_view_context": invalid_path}],
    })
    output = tmp_path / "output"
    assert main(["evaluate-batch", str(tmp_path / "batch.json"), "--out", str(output)]) == 2
    assert not output.exists()
