# Agent-review pilot contract (experimental)

This is a standalone, standard-library-only **file-audit and prediction-freezing tool**. It does not modify `run`, `resume`, or the production pipeline, discover CAD components, enforce an operating-system sandbox, establish CAD ownership, or measure accuracy. It never emits `PASS`.

Keep actual manifests, drawings, policies, images, predictions, freezes, and reports in a **private directory outside this repository**. Public tests create synthetic files in a temporary directory and do not load customer fixtures.

## Workflow

1. Write the small input manifest below. It permits explicit DWG/DXF files and structured rule/policy files only. No selector/query, target row, workbook, old finding, screenshot, handle, or bbox input fields are accepted, including nested source/rule fields.
2. Run the same CAD-only agent entry point. Its adapter should preserve unknowns and emit the prediction contract below. Every specified source gets an explicit `SCANNED`, `UNSUPPORTED`, or `ERROR` audit entry; being inventoried is not evidence that it was fully scanned.
3. Freeze the prediction with the exact code-file inventory. Then freeze a second independent execution using the same input and code inventory.
4. Compare the freezes. Only after freezing, give separate copies to an independent gold scorer. This tool neither loads gold nor substitutes reproducibility for precision/recall.

```console
python devtools/agent-review-pilot/pilot_contract.py freeze private/manifest.json private/prediction.json --code private/entry.py --code private/adapter.py --out private/frozen-1.json
python devtools/agent-review-pilot/pilot_contract.py verify private/frozen-1.json
python devtools/agent-review-pilot/pilot_contract.py compare private/frozen-1.json private/frozen-2.json --out private/comparison.json
```

These paths are placeholders, not repository output locations. Relative manifest paths, prediction image paths, and `--code` paths resolve against the manifest directory. Freeze/compare output paths follow the current working directory. Output uses exclusive creation: an existing file is never overwritten.

Python APIs are `validate_manifest(manifest, base_dir)`, `freeze(manifest, prediction, code_files, base_dir)`, `verify_frozen(frozen)`, and `compare(first, second)`. Violations raise `ContractError`; the standalone command reports `INVALID` and exits 2. `EMPTY`, `DIFFERENT`, and `CONTEXT_DRIFT` also exit 2. A successful exit is **not** an accuracy or commercial gate.

## Input manifest

```json
{
  "schema": "agent-review-pilot/1",
  "experiment_id": "development-experiment",
  "sources": [{"source_id": "drawing-a", "path": "drawing.dxf", "sha256": "CURRENT_64_LOWERCASE_HEX_HASH"}],
  "policy_files": [{"path": "rules.json", "sha256": "CURRENT_64_LOWERCASE_HEX_HASH"}]
}
```

Source IDs and resolved paths must be unique, and the CAD list must be non-empty. Both the extension and the actual DWG/DXF signature are checked: a renamed workbook or JSON file is not accepted merely because its name ends in `.dxf`. This is an early file-type check, **not a full CAD parser**. Archives must first be safely extracted outside this tool; enumerate the resulting sources explicitly.

Each policy is a JSON object with exactly these fields:

```json
{
  "schema": "pilot-rules/1",
  "policy_id": "quantity-rules",
  "version": "1",
  "scope": "general",
  "rules": [{"rule_id": "count-physical", "statement": "Material annotation counts are not physical instance counts."}]
}
```

`scope` can instead be `confirmed_policy`, requiring `approval: {"by": "...", "at": "...", "basis": "..."}` with exactly those three non-empty text fields. A `general` policy cannot contain `approval`. This preserves the caller's approval declaration; it does not authenticate a person or infer approval from chat history. Only `rule_id` and `statement` are accepted per rule. Structured target fields/selectors are rejected. **Free text can still contain disguised prior knowledge**, which requires independent human/code review.

## Prediction contract

Top-level keys are mandatory:

```json
{
  "schema": "agent-review-pilot/1",
  "declared_access": {
    "numeric_gold_accessed": false,
    "human_screenshots_accessed": false,
    "old_predictions_accessed": false,
    "prior_labels_in_context": true
  },
  "source_audit": [{"source_id": "drawing-a", "state": "SCANNED", "reason": ""}],
  "components": []
}
```

The first three access flags must be false to freeze this declared CAD-only experiment. They are **self-reported**, not access-monitoring results. `prior_labels_in_context` must be honest: a developer who has seen this project can set it to true without pretending the run was blind. `components: []` is valid but freezes as `EMPTY`, including when the source is unsupported; repeating it never becomes a successful non-empty replay.

Each component requires the following exact keys:

| Key | Meaning |
|---|---|
| `component_id` | Stable CAD-derived identity, never an imported human row number. |
| `fields` | All 13 fields listed below; unknown values are explicitly `null`. |
| `quantity_role` | `null` or one of the typed roles below. |
| `instances` | `[{"instance_id": "...", "evidence_ids": ["..."]}]`; IDs unique across the run. |
| `formula` | `null`, or the formula object described below. |
| `evidence` | Every retained evidence record, not just the first selected image. |
| `stage_states` | Exactly `plan`, `elevation`, `detail`: each `CANDIDATE`, `MISSING`, or `NOT_APPLICABLE_CLAIMED`. |
| `state` | `PREDICTED`, `REVIEW`, or `BLOCK`; never `PASS`. |
| `unknowns` | A list of explicit unresolved issues. |

`fields` requires `name`, `mt_code`, `material`, `plan_location`, `elevation`, `detail`, `specification`, `width_mm`, `length_mm`, `quantity`, `engineering_quantity`, `unit`, `calc_kind`. The four numeric fields accept positive finite numbers or `null`, not boolean/zero-as-unknown. Numbers and formula intermediates must fit the evaluator's finite floating-point range; overflow is a contract error. Quantity must be an integer with a declared role. Physical count must agree with the instance inventory. Other typed roles are `paired_jamb_count`, `billable_face_count`, and `surround_edge_count`; they are not silently equated with physical assemblies. Unknown fields or missing/claimed-inapplicable stages require `REVIEW`/`BLOCK` and an explanation.

Each evidence object has exactly:

```json
{
  "evidence_id": "object-a-elevation-closeup",
  "source_id": "drawing-a",
  "layout": "Model",
  "stage": "elevation",
  "role": "closeup",
  "state": "CANDIDATE",
  "handles": ["CAD_DERIVED_AT_RUNTIME"],
  "bbox": [-8, 12, 165, 433],
  "image": {"path": "render.png", "sha256": "CURRENT_64_LOWERCASE_HEX_HASH", "pixels": [1600, 900]}
}
```

The coordinates above are synthetic schema illustrations, not inputs or targets. Runtime output handles/bboxes are allowed; the manifest cannot supply them as selectors. Evidence IDs are globally unique; references resolve only within their component. Stages allow `plan`, `elevation`, `detail`, `material`; roles allow `locator`, `closeup`, `dimension`, `geometry`, `material`, `reference`; states allow `CANDIDATE`, `REVIEW`, `MISSING`. Bbox and image may be `null`. A missing locator/closeup is explicitly `MISSING`, not a candidate with an absent file. Every available image must exist, match its declared hash and natural pixels, and pass bounded PNG chunk/CRC/decompression/scanline checks. IDAT chunks must be consecutive; unknown critical chunks and invalid palette/transparency structure or ordering are rejected. This pilot supports non-interlaced 8-bit grayscale/RGB/grayscale-alpha/RGBA PNG only; other formats require a separately audited adapter, not renaming or upscaling.

Formula schema:

```json
{
  "expression": "W*L*Q/1000000",
  "terms": [
    {"symbol": "W", "value": 173, "unit": "mm", "role": "projection_width", "evidence_ids": ["object-a-elevation-closeup"]},
    {"symbol": "L", "value": 421, "unit": "mm", "role": "projection_length", "evidence_ids": ["object-a-elevation-closeup"]},
    {"symbol": "Q", "value": 1, "unit": "count", "role": "physical_instance_count", "evidence_ids": ["object-a-elevation-closeup"]}
  ],
  "result": 0.072833,
  "output_unit": "m2"
}
```

Formula values above are wholly synthetic. All non-empty terms must be referenced, and every term must refer to same-component evidence. A bounded AST evaluator supports `+ - * /`, not calls, attributes, indexing, or `eval`. It independently recomputes the result; the component engineering quantity and output unit must agree when filled. `count` terms require a typed quantity role and integer value, and every typed quantity role requires the `count` unit. Physical count terms must match the instance inventory, and same-role count terms must match the visible quantity. **Numeric consistency does not prove dimensional correctness or CAD ownership**: the actual measuring/semantic pipeline must validate those separately. Unknown formulas remain null, not a copied result disguised as evidence.

## Freeze and comparison

The freeze contains the validated manifest, source/policy hashes, explicit code inventory (including this verifier), predictions, normalized prediction hash, freeze hash, and immutable limitations. Verification rechecks all current declared source, policy, code and PNG bytes. Drift or a manually edited freeze is an error. There is no cryptographic signing authority: someone with edit access can regenerate hashes; this is a reproducibility record, not tamper-proof attestation.

Normalization only sorts identities and unordered reference lists, and removes image transport paths while retaining actual image hash/pixels and all CAD source IDs/handles/bboxes/roles/states. It does not round quantities, omit unknowns, merge components, collapse physical instances, or compare only totals. A fresh output folder with identical image bytes is equivalent; missing/extra components, altered instance identities, changed formulas/materials/roles, lost evidence, or null/state changes are differences. Input or code context changes are `CONTEXT_DRIFT`. Two non-empty equal normalized records give `REPEATED`; this means repeatability only.

Limitations are deliberately embedded in every freeze: file-byte audit is not OS isolation; declarations and code inventory are caller-supplied; executed code, dependencies and environment are not attested; code/policy text can contain prior answers; CAD signature and image validity do not establish semantic correctness or original-render provenance. The separate scorer must report full-row precision/recall, extras, omissions and unresolved rows, not reinterpret these contract checks as 95% accuracy.

## Synthetic tests

```console
python -m unittest discover -s tests -p test_agent_review_pilot.py
```

Tests cover repeats, additions/omissions, instance/evidence/state/role differences, empty output, source/code/policy drift, malformed/missing images, renamed non-CAD input, forbidden selectors, bad policy declarations, duplicate identities, formula recomputation, count mismatch, and non-overwrite behavior. No customer data or private outputs are required.
