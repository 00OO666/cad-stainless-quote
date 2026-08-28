from __future__ import annotations

import json
from pathlib import Path

from cad_quote import main
from cadquote.evaluation import evaluation_batch_markdown, summarize_evaluation_batch
from cadquote.models import EvaluationPolicy, ReviewStatus, TakeoffItem


def item(sequence: int = 1, **updates: object) -> TakeoffItem:
    values: dict[str, object] = {
        "sequence": sequence,
        "name": f"门套-{sequence}",
        "mt_code": "MT-01",
        "plan_location": f"1F/房间-{sequence}",
        "elevation": f"EL-{sequence:02d}",
        "detail": f"DT-{sequence:02d}",
        "unfolded_spec": "10+20+30",
        "width_mm": 60,
        "length_mm": 1000,
        "quantity": 10,
        "engineering_quantity": 0.6,
        "unit": "㎡",
        "pricing_method": "按展开面积",
        "component_id": f"component-{sequence}",
        "evidence_ids": [f"entity-{sequence}"],
        "status": ReviewStatus.PASS,
    }
    values.update(updates)
    return TakeoffItem.model_validate(values)


def confirmed_policy() -> EvaluationPolicy:
    values = EvaluationPolicy().model_dump(mode="json")
    values["policy_version"] = "batch-test-v1"
    values["length_mm"]["relative_tolerance"] = 0.05
    values["quantity"]["relative_tolerance"] = 0.05
    return EvaluationPolicy.model_validate(values)


def write_rows(path: Path, rows: list[TakeoffItem]) -> None:
    path.write_text(
        json.dumps([row.model_dump(mode="json") for row in rows], ensure_ascii=False),
        encoding="utf-8",
    )


def test_batch_summary_never_lets_aggregate_rates_override_a_project_gate():
    projects = [
        {
            "project_id": "pass",
            "overall_gate": "PASS",
            "gold_rows": 19,
            "predicted_rows": 19,
            "eligible_gold_rows": 19,
            "correct_rows": 19,
            "matched_rows": 19,
            "missing_rows": 0,
            "unexpected_rows": 0,
            "replication_recall": 1.0,
            "output_precision": 1.0,
        },
        {
            "project_id": "blocked",
            "overall_gate": "BLOCKED",
            "gold_rows": 1,
            "predicted_rows": 1,
            "eligible_gold_rows": 0,
            "correct_rows": 0,
            "matched_rows": 1,
            "missing_rows": 0,
            "unexpected_rows": 0,
            "replication_recall": None,
            "output_precision": 0.0,
        },
    ]

    summary = summarize_evaluation_batch(projects, batch_id="synthetic")

    assert summary["aggregate"]["micro_replication_recall"] == 1
    assert summary["aggregate"]["micro_output_precision"] == 0.95
    assert summary["overall_gate"] == "BLOCKED"
    assert summary["all_projects_pass"] is False
    assert summary["gate_counts"] == {
        "BLOCKED": 1,
        "INDETERMINATE": 0,
        "FAIL": 0,
        "PASS": 1,
    }

    duplicate_ids = summarize_evaluation_batch(
        [
            {**projects[0], "project_id": "same"},
            {**projects[0], "project_id": "same"},
        ],
        batch_id="duplicate",
    )
    assert duplicate_ids["overall_gate"] == "BLOCKED"
    assert duplicate_ids["invalid_project_ids"] == ["same"]


def test_batch_cli_writes_individual_reports_and_json_markdown_summary(tmp_path: Path):
    policy_path = tmp_path / "policy.json"
    policy_path.write_text(confirmed_policy().model_dump_json(indent=2), encoding="utf-8")
    pass_predicted = tmp_path / "pass-predicted.json"
    pass_gold = tmp_path / "pass-gold.json"
    fail_predicted = tmp_path / "fail-predicted.json"
    fail_gold = tmp_path / "fail-gold.json"
    write_rows(pass_predicted, [item()])
    write_rows(pass_gold, [item()])
    write_rows(fail_predicted, [item(engineering_quantity=1.0)])
    write_rows(fail_gold, [item(engineering_quantity=0.6)])
    manifest_path = tmp_path / "batch.json"
    manifest_path.write_text(
        json.dumps(
            {
                "schema_version": "1.0",
                "batch_id": "held-out-synthetic",
                "policy": "policy.json",
                "projects": [
                    {
                        "project_id": "pass-project",
                        "predicted": "pass-predicted.json",
                        "gold": "pass-gold.json",
                    },
                    {
                        "project_id": "../unsafe|fail-project",
                        "predicted": "fail-predicted.json",
                        "gold": "fail-gold.json",
                    },
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    output_dir = tmp_path / "batch-output"

    exit_code = main(["evaluate-batch", str(manifest_path), "--out", str(output_dir)])
    summary = json.loads((output_dir / "summary.json").read_text(encoding="utf-8"))
    markdown = (output_dir / "summary.md").read_text(encoding="utf-8")
    reports = sorted((output_dir / "projects").glob("*.json"))

    assert exit_code == 0
    assert summary["project_count"] == 2
    assert summary["overall_gate"] == "FAIL"
    assert summary["gate_counts"]["PASS"] == 1
    assert summary["gate_counts"]["FAIL"] == 1
    assert summary["aggregate"]["eligible_gold_rows"] == 2
    assert summary["aggregate"]["correct_rows"] == 1
    assert summary["aggregate"]["micro_replication_recall"] == 0.5
    assert summary["aggregate"]["micro_output_precision"] == 0.5
    assert all(project["policy_version"] == "batch-test-v1" for project in summary["projects"])
    assert all(len(project["policy_hash"]) == 64 for project in summary["projects"])
    assert all(
        len(project["inputs"]["predicted_sha256"]) == 64
        and len(project["inputs"]["gold_sha256"]) == 64
        for project in summary["projects"]
    )
    assert len(reports) == 2
    assert all("unsafe" not in report.name for report in reports)
    assert not (tmp_path / "unsafe|fail-project.json").exists()
    assert "Every project must PASS individually" in markdown
    assert "unsafe\\|fail-project" in markdown
    fail_report = next(
        json.loads(report.read_text(encoding="utf-8"))
        for report in reports
        if json.loads(report.read_text(encoding="utf-8"))["project_id"] == "../unsafe|fail-project"
    )
    assert fail_report["matched_count"] == 1
    assert (
        fail_report["row_results"][0]["field_results"]["engineering_quantity"]["status"] == "FAIL"
    )


def test_batch_cli_blocks_missing_gold_evidence_and_continues_after_file_error(
    tmp_path: Path,
):
    policy_path = tmp_path / "policy.json"
    policy_path.write_text(confirmed_policy().model_dump_json(indent=2), encoding="utf-8")
    predicted_path = tmp_path / "predicted.json"
    gold_path = tmp_path / "gold.json"
    write_rows(predicted_path, [item(detail=None)])
    write_rows(gold_path, [item(detail=None)])
    manifest_path = tmp_path / "batch.json"
    manifest_path.write_text(
        json.dumps(
            {
                "schema_version": "1.0",
                "batch_id": "blocked-batch",
                "policy": "policy.json",
                "projects": [
                    {
                        "project_id": "missing-evidence",
                        "predicted": "predicted.json",
                        "gold": "gold.json",
                    },
                    {
                        "project_id": "missing-file",
                        "predicted": "does-not-exist.json",
                        "gold": "gold.json",
                    },
                ],
            }
        ),
        encoding="utf-8",
    )
    output_dir = tmp_path / "output"

    assert main(["evaluate-batch", str(manifest_path), "--out", str(output_dir)]) == 0
    summary = json.loads((output_dir / "summary.json").read_text(encoding="utf-8"))

    assert summary["overall_gate"] == "BLOCKED"
    assert summary["gate_counts"]["BLOCKED"] == 2
    assert summary["projects"][0]["eligible_gold_rows"] == 0
    assert summary["projects"][0]["correct_rows"] == 0
    assert summary["projects"][1]["error"]["type"] == "FileNotFoundError"
    assert len(list((output_dir / "projects").glob("*.json"))) == 2


def test_batch_without_policy_is_indeterminate_and_duplicate_ids_are_rejected(
    tmp_path: Path,
):
    predicted_path = tmp_path / "predicted.json"
    gold_path = tmp_path / "gold.json"
    write_rows(predicted_path, [item()])
    write_rows(gold_path, [item()])
    manifest_path = tmp_path / "pending.json"
    manifest_path.write_text(
        json.dumps(
            {
                "schema_version": "1.0",
                "projects": [
                    {
                        "project_id": "pending",
                        "predicted": "predicted.json",
                        "gold": "gold.json",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    output_dir = tmp_path / "pending-output"

    assert main(["evaluate-batch", str(manifest_path), "--out", str(output_dir)]) == 0
    summary = json.loads((output_dir / "summary.json").read_text(encoding="utf-8"))
    assert summary["overall_gate"] == "INDETERMINATE"
    assert summary["projects"][0]["policy_pending_fields"] == [
        "length_mm.relative_tolerance",
        "quantity.relative_tolerance",
    ]

    duplicate_manifest = tmp_path / "duplicate.json"
    duplicate_manifest.write_text(
        json.dumps(
            {
                "schema_version": "1.0",
                "projects": [
                    {
                        "project_id": "same",
                        "predicted": "predicted.json",
                        "gold": "gold.json",
                    },
                    {
                        "project_id": "same",
                        "predicted": "predicted.json",
                        "gold": "gold.json",
                    },
                ],
            }
        ),
        encoding="utf-8",
    )
    duplicate_output = tmp_path / "duplicate-output"
    assert main(["evaluate-batch", str(duplicate_manifest), "--out", str(duplicate_output)]) == 2
    assert not (duplicate_output / "summary.json").exists()


def test_batch_markdown_uses_project_rows_and_does_not_claim_pooled_pass():
    summary = summarize_evaluation_batch(
        [
            {
                "project_id": "one|line\nname",
                "overall_gate": "INDETERMINATE",
                "gold_rows": 1,
                "predicted_rows": 1,
                "eligible_gold_rows": 1,
                "correct_rows": 0,
                "matched_rows": 1,
                "missing_rows": 0,
                "unexpected_rows": 0,
                "replication_recall": 0,
                "output_precision": 0,
                "report_path": "projects/001.json",
            }
        ],
        batch_id="markdown",
    )

    markdown = evaluation_batch_markdown(summary)

    assert "one\\|line name" in markdown
    assert "**INDETERMINATE**" in markdown
    assert "Every project must PASS individually" in markdown
    assert "[projects/001.json](projects/001.json)" in markdown


def test_batch_refuses_to_overwrite_manifest_or_input_files(tmp_path: Path):
    predicted_path = tmp_path / "predicted.json"
    gold_path = tmp_path / "gold.json"
    write_rows(predicted_path, [item()])
    write_rows(gold_path, [item()])
    manifest_path = tmp_path / "summary.json"
    original = json.dumps(
        {
            "schema_version": "1.0",
            "projects": [
                {
                    "project_id": "collision",
                    "predicted": "predicted.json",
                    "gold": "gold.json",
                }
            ],
        }
    )
    manifest_path.write_text(original, encoding="utf-8")

    assert main(["evaluate-batch", str(manifest_path), "--out", str(tmp_path)]) == 2
    assert manifest_path.read_text(encoding="utf-8") == original


def test_batch_never_passes_rows_without_source_evidence(tmp_path: Path):
    policy_path = tmp_path / "policy.json"
    policy_path.write_text(confirmed_policy().model_dump_json(indent=2), encoding="utf-8")
    predicted_path = tmp_path / "predicted.json"
    gold_path = tmp_path / "gold.json"
    write_rows(predicted_path, [item(evidence_ids=[])])
    write_rows(gold_path, [item(evidence_ids=[])])
    manifest_path = tmp_path / "batch.json"
    manifest_path.write_text(
        json.dumps(
            {
                "schema_version": "1.0",
                "policy": "policy.json",
                "projects": [
                    {
                        "project_id": "no-evidence",
                        "predicted": "predicted.json",
                        "gold": "gold.json",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    output_dir = tmp_path / "output"

    assert main(["evaluate-batch", str(manifest_path), "--out", str(output_dir)]) == 0
    summary = json.loads((output_dir / "summary.json").read_text(encoding="utf-8"))
    report_path = output_dir / summary["projects"][0]["report_path"]
    report = json.loads(report_path.read_text(encoding="utf-8"))

    assert summary["overall_gate"] == "BLOCKED"
    assert summary["projects"][0]["eligible_gold_rows"] == 0
    assert report["invalid_gold_rows"][0]["missing_required_fields"] == ["source_evidence"]
    assert report["row_results"][0]["field_results"]["source_evidence"]["status"] == "UNRESOLVED"


def test_batch_does_not_pair_ambiguous_rows_by_nearest_engineering_quantity(
    tmp_path: Path,
):
    policy_path = tmp_path / "policy.json"
    policy_path.write_text(confirmed_policy().model_dump_json(indent=2), encoding="utf-8")
    shared = {
        "component_id": None,
        "name": "同名门套",
        "plan_location": "同一房间",
        "elevation": "EL-01",
        "detail": "DT-01",
    }
    gold = [
        item(1, engineering_quantity=1, **shared),
        item(2, engineering_quantity=100, **shared),
    ]
    predicted = [
        item(1, engineering_quantity=100, **shared),
        item(2, engineering_quantity=1, **shared),
    ]
    predicted_path = tmp_path / "predicted.json"
    gold_path = tmp_path / "gold.json"
    write_rows(predicted_path, predicted)
    write_rows(gold_path, gold)
    manifest_path = tmp_path / "batch.json"
    manifest_path.write_text(
        json.dumps(
            {
                "schema_version": "1.0",
                "policy": "policy.json",
                "projects": [
                    {
                        "project_id": "ambiguous",
                        "predicted": "predicted.json",
                        "gold": "gold.json",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    output_dir = tmp_path / "output"

    assert main(["evaluate-batch", str(manifest_path), "--out", str(output_dir)]) == 0
    summary = json.loads((output_dir / "summary.json").read_text(encoding="utf-8"))
    report = json.loads(
        (output_dir / summary["projects"][0]["report_path"]).read_text(encoding="utf-8")
    )

    assert report["matched_count"] == 0
    assert report["missing_count"] == 2
    assert report["unexpected_count"] == 2
    assert report["correct_rows"] == 0
    assert report["overall_gate"] == "FAIL"
    assert all(
        row["field_results"]["engineering_quantity"]["reason"]
        == "predicted_row_missing"
        for row in report["row_results"]
    )
