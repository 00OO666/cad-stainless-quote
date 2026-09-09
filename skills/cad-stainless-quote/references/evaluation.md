# Evaluation protocol

Do not claim human-level or better accuracy from a single example. Evaluate every held-out
project separately and retain the complete row/field diagnostics.

## Gold-set preparation

Each gold row needs an approved physical component identity, material code, component/name and
plan location, elevation, detail, unfolded specification, length, quantity, engineering quantity,
formula, and source evidence. Mark genuinely unknowable fields as unresolved rather than
fabricating a target. A human workbook imported by `gold-import` is candidate gold until its audit
issues and row-to-drawing evidence have been adjudicated.

During candidate-gold cleanup only, an engineering-quantity cell may resolve an omitted or default
displayed quantity. Preserve the workbook value as `reported_quantity`; derive
`effective_quantity` only when the row unit, displayed dimensions, and authoritative engineering
quantity reduce through the standard unit formula to one unique positive integer. Record
`quantity_source=derived_from_engineering_quantity` plus the exact derivation basis and retain the
row for human audit. A non-integer, ambiguous-axis, custom billing-axis, non-positive, or otherwise
inconsistent result remains unresolved.

This normalization is not prediction evidence. It must run outside every target-free task pack,
and neither blind evaluation nor production CAD takeoff may read a human engineering quantity to
infer CAD quantity. A prediction must prove quantity independently from the selected component's
CAD topology before scoring.

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

## Audited view applicability (evaluation only)

An empty string or bare `N/A`, `无`, or `不适用` is not a drawing reference. A missing
view is different from a view that is genuinely unnecessary. `TakeoffItem.view_dispositions`
can record one audited `NOT_APPLICABLE` stage:

- `elevation`, with `basis_kind=plan_section`: selected plan and section, a confirmed
  plan-to-section connection, native section DIMENSION evidence, and completed negative
  search covering both selected sheets;
- `detail`, with `basis_kind=plan_elevation_without_detail`: selected plan and elevation,
  their confirmed connection, native elevation DIMENSION evidence, and completed negative
  search covering the selected elevation.

See `ViewDisposition` in `models.py` and `ViewDispositionContext` in
`view_dispositions.py` for the strict serialized contracts. Receipts require component,
current index/source SHA-256, selected view IDs, connection/search IDs, actual evidence
IDs, and reviewer/time/reason. The scorer independently resolves prediction and gold
receipts against their own current selected-view contexts. It rejects missing or stale
contexts, wrong-side inputs, broken relations, incomplete searches, conflicting references,
reused cross-stage entities, and two non-applicable stages. `N/A` is never a row identity
anchor. Omissions/extras and all other enabled fields still control the full-row gate.

```powershell
scripts/run.ps1 evaluate predicted.json gold.json --policy approved-policy.json `
  --predicted-view-context predicted-current-context.json `
  --gold-view-context gold-current-context.json --out evaluation.json
```

Batch project entries accept `predicted_view_context` and `gold_view_context` paths relative
to the manifest. Reports retain input paths and raw-file hashes, and output paths cannot
overwrite those inputs. Without an explicit context no context is manufactured.

**Trust boundary:** these are scorer-supplied CAD facts, not authenticated by a self-declared
hash. Build them from the current CAD index and independently reviewed stage selections;
never copy gold context into a prediction. This evaluator does not read CAD or establish
that arbitrary authored facts are true. This addition does not make `stage-evidence`,
`run`/`resume`, or the quotation exporter automatically produce a plan-to-section chain.
Do not relabel a section as an elevation. Freeze new rule/input versions before new scoring;
preserve prior predictions and evaluations, and label known-project regression honestly.

## Additional pipeline metrics

### Bounded current-CAD context producer

The standalone producer accepts current index/panel files and target-free,
explicitly reviewed component/view selections. It reopens each selected source,
verifies bytes and native entity facts, and replays selected-source panels before
issuing a context. Do not treat caller-authored index enrichment or a matching
hash as authority to invent relationships.

```powershell
python -m cadquote.view_context_builder index.json panels.json selections.json `
  --side predicted --relations relation-edges.json --out current-context.json `
  --receipt context-receipt.json --claims disposition-claims.json
```

Use `ContextSelections` and `SelectedView` in `view_context_builder.py` for the
input contract: exactly two selected views per component, native evidence IDs,
bounded object boxes, paired source/target references and searched sheet IDs.
Selected top-level native object handles may supply geometry missing from the
semantic text index. The program derives their actual geometry from CAD; it does
not trust caller-provided types or lengths. There are no target quantities,
caller-supplied edges, or caller-authored complete/conflict flags in this schema.

The result contains `context`, `receipt`, and `disposition_claims`.
`verify_view_context_receipt()` reruns the source-backed production; the receipt
alone is not a signature. Ordinary evidence needs native component ownership and
visibility. A label-to-leader connection requires actual attachment, not general
same-sheet proximity. The local target title must belong to the chosen viewport.
Unresolved in-scope references, missing projection coverage and conflicts remain
visible in the negative-search record instead of being erased by an input edge.

This is deliberately bounded. A semantic choice still requires review; source
replay does not itself understand the physical assembly. Supported native
triangle and circular-divider symbols are traced by their actual geometry and
finite endpoint-connected paths. A unique path must properly cross selected
native object geometry. Touching, bbox overlap, proximity, an unresolved branch,
or a symbol that has not been recognized cannot establish ownership. Circular
markers retain their native arc gaps rather than reconstructing a closed circle.
Source and object visibility includes the selected viewport's frozen layers.

Paper/model association uses a validated parallel top-view projection. Receipts
retain source and object plane Z values and explicitly do not claim a three-
dimensional intersection. Tilt, perspective and unsupported projection remain
incomplete. An unsupported entity may be excluded from the reachable path scope
only through verified disjoint native bounds; this is not a declaration that the
whole layout or a missing-view negative search has been fully interpreted.

Rejected selections must not be enlarged or swapped just to obtain a verified
receipt. The path receipt does not establish material, dimension roles, physical
counts, quoted area, fabrication dimensions, or semantic row correctness.
Context generation
does not change takeoff numbers, confirm prices, automatically normalize gold,
or integrate this path into `run`, `resume` or `stage-evidence`.

The field evaluator does not fail a row merely because its commercial status is
REVIEW/BLOCK or its price is blank. Conversely, nonempty `evidence_ids` are not
source validation: a numeric/text match must be accompanied by a valid production
CAD evidence chain before it is reported as a verified CAD takeoff row. Report
missing enabled fields and unresolved ownership explicitly, not as price issues.

### Diagnostic metrics

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
