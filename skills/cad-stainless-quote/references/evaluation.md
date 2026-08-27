# Evaluation protocol

Do not claim human-level or better accuracy from a single example. Evaluate every held-out
project separately and retain the complete row/field diagnostics.

## Gold-set preparation

Each gold row needs an approved physical component identity, material code, component/name and
plan location, elevation, detail, unfolded specification, length, quantity, engineering quantity,
formula, and source evidence. Mark genuinely unknowable fields as unresolved rather than
fabricating a target. A human workbook imported by `gold-import` is candidate gold until its audit
issues and row-to-drawing evidence have been adjudicated.

## Versioned acceptance policy

`assets/evaluation-policy-template.json` is the conservative policy template. The validated policy
and its SHA-256 hash are embedded in every evaluation report.

- Enabled text fields select `strict` or `canonical` normalization.
- `unfolded_spec.mode` is either `strict` text comparison or `evaluated-total`, where safe
  arithmetic expressions such as `10+20+30` and `60` are equivalent.
- Length, physical quantity, and engineering quantity use separate relative tolerances. The
  boundary is inclusive: an error of exactly 5% passes a 5% rule.
- A gold numeric value of zero cannot use relative division. Each field must choose `exact`,
  `absolute` plus `zero_absolute_tolerance`, or `unresolved`.
- Amount comparison is disabled by default. When explicitly enabled it is exact (0%) and is
  evaluated only when both rows contain an amount.
- Unit and unit price are outside the current row-correctness gate.
- Non-empty source evidence is mandatory on both sides and is not a policy opt-out. Missing gold
  evidence blocks eligibility; missing prediction evidence fails the row.

The safe default fixes engineering quantity at 5% but leaves length and physical-quantity
tolerances as `null`. Those pending fields force `overall_gate=INDETERMINATE`; callers must approve
a versioned policy before the system can report that 95% passed. Do not infer the missing business
rule from a screenshot or an earlier contradictory note.

## Identity and row matching

Matching never uses nearest engineering quantity. It proceeds in this order:

1. a common, unique `component_id`;
2. a common, unique stable wrapper row ID, including a `GoldRow.id` retained by the CLI;
3. an exact deterministic categorical signature;
4. a conservative mutual-unique diagnostic match with the same material code and at least two
   other categorical anchors.

Unmatched gold rows are omissions; unmatched predictions are extras. Duplicate component IDs,
stable IDs, and categorical signatures are reported. Duplicate gold identities block the gate.

## Row and project gates

A matched row is correct only when every enabled field is `PASS`. Each field result is one of
`PASS`, `FAIL`, or `UNRESOLVED`, with normalized values or numeric errors in the report.

- `eligible_gold_rows`: gold rows containing every enabled field marked `required_in_gold`.
- `replication_recall = correct_rows / eligible_gold_rows`.
- `output_precision = correct_rows / predicted_rows`; extra rows therefore reduce precision.
- A project passes only when both metrics meet `target_accuracy` and there are no pending policy
  fields, unresolved comparisons, invalid gold rows, or duplicate gold identities.
- Missing required gold data produces `BLOCKED`; an incomplete policy or unresolved optional
  comparison produces `INDETERMINATE`; otherwise the result is `PASS` or `FAIL`.

Ratios are not rounded before the gate. Therefore 10 correct rows out of 11 is about 90.91% and
does not pass a 95% requirement. Human `10.00` versus prediction `9.96` has 0.4% relative error and
passes a 5% engineering-quantity rule.

The report contains a `projects` array and an aggregate block. The current CLI invocation compares
one predicted/gold pair as one project; run it once per held-out project and do not replace project
gates with only a pooled average.

```powershell
scripts/run.ps1 evaluate predicted.json gold.json `
  --policy assets/evaluation-policy-template.json `
  --project-id held-out-01 `
  --out evaluation.json
```

The legacy absolute engineering-quantity metrics and `--tolerance` option remain diagnostic only;
they do not control the authoritative 95% gate.

## Multi-project batch evaluation

Use `evaluate-batch` to run the same evaluator across a versioned project list. The manifest uses
schema `1.0`; paths are resolved relative to the manifest and a project may override the global
policy or legacy diagnostic tolerance.

```json
{
  "schema_version": "1.0",
  "batch_id": "held-out-batch-v1",
  "policy": "evaluation-policy.json",
  "projects": [
    {
      "project_id": "held-out-01",
      "predicted": "predictions/held-out-01.json",
      "gold": "gold/held-out-01.json"
    }
  ]
}
```

```powershell
scripts/run.ps1 evaluate-batch batch.json --out evaluation-batch
```

The output directory contains `projects/*.json`, `summary.json`, and `summary.md`. Project report
filenames use an ordinal plus a project-ID hash, so a project ID cannot become a path. The command
also rejects output paths that would overwrite the manifest, prediction, gold, or policy inputs.
A missing or invalid project input produces a per-project `BLOCKED` report while the remaining
projects continue.

Batch micro/macro rates are diagnostic. The batch gate is `PASS` only when every project is
`PASS`; otherwise `BLOCKED` takes precedence, followed by `INDETERMINATE` and `FAIL`. Thus a large
easy project can never hide a failed or evidence-incomplete project.

## Additional pipeline metrics

- MT/material-code occurrence recall and precision.
- Physical component recall and duplicate rate.
- Plan→elevation and elevation→detail edge accuracy.
- PASS precision and automatic PASS rate.
- Complete evidence-chain coverage.
- Formula/unit validation rate.
- Price-match precision and total amount variance when approved prices exist.

## Dataset roles

- Development set: an authorized, fully adjudicated set used to improve extraction and linking
  rules.
- Held-out set: a separately authorized generalization test. Do not add dataset-specific exceptions
  before the first blind result is recorded.
