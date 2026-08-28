# Review and adjudication workflow

Use this reference only when automatic evidence is incomplete or conflicting.

## Evidence priority

1. Native CAD dimension with source handle and a consistent displayed override.
2. Explicit text dimension tied to the component by leader or geometry.
3. Closed dimension chain whose sum agrees with the marked overall size.
4. Geometry measurement with an established drawing scale.
5. Approved human override stored with reviewer, timestamp, reason, and source crop.

Never promote a lower-priority source over a conflicting higher-priority source without recording the conflict.

## Cross-drawing linkage

Rank a plan→elevation or elevation→detail edge using explicit reference code, room/component identity, MT code, leader target, geometric neighborhood, title/sheet compatibility, and direction. File-name similarity alone is not sufficient for PASS.

When CAD contains only an unnumbered stainless description, an ingested DOCX material book may
provide a material-code candidate. Accept only an explicit configured code family plus a
description, and retain the document hash and row/cell or paragraph locator. A unique normalized
description is still REVIEW; an ambiguous or conflicting mapping is BLOCK. Neither case proves a
physical quantity.

## Excel screenshot evidence

Build one evidence bundle per `component_id`; never bind screenshots only by Excel row number.
The bundle must contain a locator image showing the room/component in context and a readable
close-up of the MT leader, elevation, node, or governing dimensions. Together they must make the
order plan → elevation → detail/dimension auditable.

For every image retain the drawing number/layout, CAD bbox, related entity IDs, DXF handles,
source-file and evidence-file SHA-256, and original pixel width/height. Preserve the original
aspect ratio. Do not enlarge a raster beyond its natural pixels; re-render from vector CAD at a
recorded scale/DPI when more detail is required.

If more than one occurrence shares the same sheet and MT code, generate a numbered candidate
board before building the bundle:

```powershell
& .\scripts\run.ps1 candidate-boards `
  "C:\runs\project\index\cad_index.json" `
  "C:\runs\project\analysis\mt_occurrences.json" `
  --panels "C:\runs\project\analysis\panels.json" `
  --out "C:\runs\project\analysis\candidate-boards"
```

The board is a review aid, not evidence of correctness. Record the selected occurrence IDs,
physical-component name/location, and object bbox. Never default to the first rendered candidate.
Different component names sharing one occurrence image, or one image used as both elevation and
node/detail, is an automatic REVIEW/BLOCK condition.

After recording explicit selections, render row-specific candidate evidence:

```powershell
& .\scripts\run.ps1 selected-evidence `
  "C:\runs\project\analysis\candidate-boards\candidate_boards.json" `
  "C:\runs\project\analysis\candidate-boards\panel-cache\index.json" `
  "C:\review\component-selections.json" `
  --out "C:\runs\project\analysis\selected-evidence"
```

Before that render, a geometry envelope can be suggested without pretending it is confirmed:

```powershell
& .\scripts\run.ps1 component-frames `
  "C:\runs\project\analysis\panels.json" `
  "C:\runs\project\analysis\candidate-boards\candidate_boards.json" `
  "C:\review\component-selections.json" `
  --out "C:\runs\project\analysis\component-frames"
```

The suggestion writes `object_bbox_state=REVIEW`. A reviewer must change the state to
`CONFIRMED` only after checking the physical boundary and governing dimensions. The
`selected-evidence` command refuses cross-component occurrence reuse. Its locator/close-up pair
proves only which occurrence was selected; the pair is two framings of the same drawing stage,
not a plan/elevation/detail chain.

If that close-up was cropped from a low-resolution whole-panel PNG and its annotations are not
readable, re-render the component region directly from the indexed DXF:

```powershell
& .\scripts\run.ps1 component-closeups `
  "C:\runs\project\index\cad_index.json" `
  "C:\runs\project\analysis\panels.json" `
  "C:\runs\project\analysis\component-frames\component_frames.json" `
  --out "C:\runs\project\analysis\component-closeups" `
  --target-px 3200 `
  --render-profile cad-dark-full
```

This is a fresh bounded vector render, not raster upscaling. It includes nearby governing
dimension bboxes when available and records the original source, sheet, CAD bbox, output hashes,
pixel size, and renderer. The command is fail-closed and writes local-run diagnostics. A readable
render remains `REVIEW`: clarity does not confirm that the selected bbox belongs to the intended
physical component.

If the rendered outline is visible but the semantic index contains only its parent `INSERT`, probe
the original block contents inside the same bounded region:

```powershell
& .\scripts\run.ps1 component-geometry `
  "C:\runs\project\index\cad_index.json" `
  "C:\runs\project\analysis\component-closeups\component_closeups.json" `
  --out "C:\runs\project\analysis\component-geometry"
```

The command recursively expands nested and anonymous/dynamic blocks in transformed model-space
coordinates. It retains root insert/block-entity handles when available, otherwise an explicit
ordinal-only provenance, and marks SPLINE/ELLIPSE or other flattened lengths with the configured
tolerance. Inspect its same-layer endpoint-connected networks as candidates only. A connected
network is not proof of billable perimeter, length, width, or quantity, and a cap/depth truncation
means the region is incomplete.

Overlay the high-value primitive and connected-path bboxes on the exact vector close-up before
referring to one in a review:

```powershell
& .\scripts\run.ps1 geometry-boards `
  "C:\runs\project\analysis\component-closeups\component_closeups.json" `
  "C:\runs\project\analysis\component-geometry\component_geometry.json" `
  --out "C:\runs\project\analysis\geometry-boards"
```

The command checks `selection_key`, evidence index, source/sheet identity, CAD bbox, and source PNG
before drawing. Missing or mismatched images fail closed. A candidate/source/board-limit
truncation stays explicit and makes the board incomplete. The stable `G1`, `G2`, ... labels map to
manifest primitive/path IDs, bbox spans, path lengths, layers, and original block provenance; they
never choose a measurement role or physical quantity and always retain `measurement_role=null`.
When the board is capped, a deterministic category round-robin preserves prominent closed
primitives, multi-primitive paths, top-level/block handles, large bboxes, and region-center
candidates instead of allowing one class to consume every label.

Create a numbered measurement board before selecting among several visible dimensions:

```powershell
& .\scripts\run.ps1 measurement-boards `
  "C:\runs\project\analysis\panels.json" `
  "C:\runs\project\analysis\component-closeups\component_closeups.json" `
  --takeoff "C:\runs\project\analysis\takeoff_draft.json" `
  --out "C:\runs\project\analysis\measurement-boards"
```

The overlay labels eligible native dimensions and explicit structured measurement text as
`D1`, `D2`, and so on. Its manifest maps each label to the exact CAD entity ID, handle, raw value,
bbox/insert, orientation, unit metadata, and board pixel. When component IDs agree and
`--takeoff` is supplied, it also lists the exact measurement candidate IDs accepted by the
confirmation contract. A reviewer or model must select those IDs and verify the component/stage
relationship; equal numbers in another region are not a valid
substitute. The board is a REVIEW aid and cannot infer physical quantity.

If related casework rows use letter suffixes, split them by their physical CAD views before any
formula or convention ranking:

```powershell
& .\scripts\run.ps1 variant-bindings `
  "C:\review\target-free-row-tasks.json" `
  "C:\runs\project\analysis\panels.json" `
  --out "C:\runs\project\analysis\variant-bindings.json"
```

The task input is deliberately target-free: `unfolded_spec`, `width_mm`, `length_mm`, `quantity`,
and `engineering_quantity` are rejected. A row suffix must match an exact component plan/elevation
title, every selected panel must contain the row material code, and each recovered width, depth,
or height must have independent native DIMENSION corroboration. The command does not mutate the
takeoff, and all bindings remain REVIEW until physical row ownership, units, and transfer have
been separately accepted.

If the estimating team has supplied a versioned convention profile, generate a separate candidate
manifest after the component and measurement facts are available:

```powershell
& .\scripts\run.ps1 convention-candidates `
  "C:\runs\project\analysis\takeoff_draft.json" `
  "C:\review\estimator-conventions.json" `
  --out "C:\runs\project\analysis\convention-candidates.json"
```

Do not merge profile values into drawing extraction or use the profile to repair a missing
component, sheet, entity, or measurement. Quantity does not default to one. An outer aggregation
quantity of one is a double-counting guard only when the audited expression already contains an
explicit multiplier. Area, linear, and set rules must bind the required width/path/physical-count
roles separately. A `CONFIRMED` convention candidate is still not a PASS takeoff item and has no
commercial effect; apply any accepted value through the normal reviewed confirmation/evidence
flow.

Render cross-drawing panels and assemble the distinct stages next:

```powershell
& .\scripts\run.ps1 panel-catalog `
  "C:\runs\project\index\cad_index.json" `
  --panels "C:\runs\project\analysis\panels.json" `
  --render-profile cad-dark-full `
  --out "C:\runs\project\analysis\panel-catalog"

& .\scripts\run.ps1 annotate-panel-catalog `
  "C:\runs\project\analysis\panels.json" `
  "C:\runs\project\analysis\panel-catalog\panel_catalog.json" `
  --out "C:\runs\project\analysis\panel-catalog-annotated"

& .\scripts\run.ps1 stage-evidence `
  "C:\runs\project\analysis\panels.json" `
  "C:\runs\project\analysis\relation_edges.json" `
  "C:\runs\project\analysis\selected-evidence\selected_evidence.json" `
  "C:\runs\project\analysis\panel-catalog-annotated\panel_catalog_annotated.json" `
  "C:\review\component-selections.json" `
  --out "C:\runs\project\analysis\stage-evidence"
```

`stage-evidence` preserves all explicit-reference candidates. Candidate ordering is never a
selection. A plan source must be an incoming `plan_to_elevation` edge, and a detail must be an
outgoing `elevation_to_detail` target; ambiguity remains REVIEW. Put a reviewed CAD-coordinate
`object_bbox` in the explicit stage selection to frame the physical component; otherwise the
chain cannot become `CONFIRMED`.

For confirmation, every positive stage selection must name its exact `candidate_id`; plan and
detail must also name the exact `relation_edge_id`. The two edges must connect through the selected elevation.
Require a unique non-empty `component_id`, reviewer, reason, timezone-aware `reviewed_at`, verified
context/close-up image files and SHA-256 values, and an allowed dark-CAD render profile. A detail
selection additionally requires at least one same-sheet `DIMENSION` entity ID. Reusing one image
SHA for context and close-up or across stages is BLOCK. A stable key that does not bind exactly is
BLOCK; sequence fallback is legacy REVIEW-only and can never confirm a chain.

The only exception is an audited detail disposition with `state=NOT_APPLICABLE` and
`kind=not_applicable`. It must include a non-empty basis, reviewer metadata, and
`searched_sheet_ids` covering the selected elevation; optional reference entity IDs must exist
inside that search scope. It must not contain a detail candidate, relation edge, bbox, measurement,
or image. A missing candidate alone never triggers this disposition.

Minimal selection shape (IDs and hashes must come from the generated candidate manifests):

```json
{
  "component_id": "component:stable-id",
  "sequence": 1,
  "stages": {
    "plan": {
      "state": "CONFIRMED",
      "candidate_id": "stage-candidate:...",
      "relation_edge_id": "edge:plan-to-elevation",
      "object_bbox": [0, 0, 100, 100],
      "context_image": "relative/path/to/plan-context.png",
      "context_sha256": "<sha256>",
      "closeup_image": "relative/path/to/plan-closeup.png",
      "closeup_sha256": "<different-sha256>",
      "review": {
        "reviewer": "reviewer-id",
        "reviewed_at": "2026-01-01T10:00:00+08:00",
        "reason": "verified against the CAD callout"
      }
    }
  }
}
```

The panel-catalog, merged/annotated catalog, component-closeups, component-geometry,
geometry-boards, measurement-boards, and
stage-evidence manifests are
`path_scope=local_run_diagnostics`; they may contain absolute paths. Keep the entire run directory
private and never commit or publish these manifests without a deliberate redaction/export step.
These commands are explicit review stages and are not yet invoked automatically by the standard
`run`/`resume` workflow or merged into its standard quotation workbook.

Frame the close-up around the selected component geometry, MT leader, and governing dimensions.
Preserve the content aspect ratio; a fixed square around the leader point is not a valid substitute.
Use a dark CAD render for workbook evidence. Missing Xrefs/fonts/proxy objects/plot styles or a
blocks-and-hatches fallback must be recorded as degraded evidence.

For final review screenshots prefer `cad-dark-full` on bounded model panels, then overlay the
paper annotations already projected by panel expansion. Rendering an entire paper layout can be
pathologically slow. Full rendering still cannot guarantee missing Xrefs/SHX, unsupported proxy
objects, WIPEOUT behavior, or CTB/STB fidelity; record those limitations. Do not reuse an existing
PNG solely because its filename matches a panel ID.

Use explicit evidence states:

- `CANDIDATE`: plausible crop awaiting chain/role confirmation; it must be labelled as such in the workbook.
- `CONFIRMED`: reviewed evidence bound to the accepted component and chain.
- `NOT_APPLICABLE`: reviewed negative-detail search proving that this component has no node/detail stage; valid only for `detail`.
- `MISSING`: a required plan, elevation, detail, or dimension image is absent; keep the item REVIEW/BLOCK and show `待确认`.

A candidate crop cannot satisfy a confirmed chain merely because it looks similar to the human
workbook. Release amount and include it in the confirmed total only after the component has a
complete confirmed plan → elevation → detail/dimension chain and all required images, or an
explicitly audited `detail=NOT_APPLICABLE` disposition with its measurements bound to the selected
elevation. Keep all
customer drawings, screenshots, workbook extracts, customer/local paths, and identifiable project
statistics out of public repositories.

## Quantity evidence

Prefer an explicit, component-bound quantity label. Split labels such as `QTY`, `=`, `2`
may be joined only inside the same CAD parent block or a real leader annotation identity;
never join nearby free text, cross-sheet tokens, or a size expression such as `50*200`.

Treat the workbook quantity as a **billable multiplier**, not automatically as a count of MT
labels or whole assemblies. It may represent repeated physical instances, symmetric jambs,
repeated faces/panels, or the sides of a surround. Record that semantic role explicitly (for
example `physical_instance_count`, `paired_jamb_count`, `billable_face_count`, or
`surround_edge_count`) and bind it to the selected component geometry. A single leader or one
visible assembly does not prove `quantity=1`; an internal side/face multiplier must not also be
counted again in an outer aggregation expression.

Keep the visible quantity as that confirmed physical/billable count. If CAD instead proves that
the final engineering quantity uses another displayed axis or an internal topology multiplier,
record an audited `engineering_quantity` confirmation rather than changing the visible count.
The safe expression, physical basis, and CAD entity IDs must all be present on the selected chain;
see `confirmations.md`. This exception is BLOCK when it is unsupported and may never be introduced
merely because it reproduces a human workbook value.

When the drawing has no quantity text, inspect `analysis/vector_quantity_probes.json`.
The probe revisits straight `LWPOLYLINE`/`POLYLINE` entities near the leader target and
groups translation-equivalent shapes on the same layer. Even a unique group is REVIEW:
the reviewer must verify that the repeated shapes are physical billable instances rather
than decorative lines, construction geometry, or multiple edges of one component. A
truncated scan or more than one leader-anchored repeated group produces no recommendation.

```powershell
& .\scripts\run.ps1 vector-probe `
  "C:\runs\project\index\cad_index.json" `
  "C:\runs\project\analysis\mt_occurrences.json" `
  --panels "C:\runs\project\analysis\panels.json" `
  --out "C:\runs\project\analysis\vector_quantity_probes.json"
```

Never copy a vector probe directly into the commercial workbook. It must first be bound to
one physical component and explicitly confirmed with reviewer, time, reason, and evidence.

## REVIEW versus BLOCK

- REVIEW: plausible candidates exist but require a choice, deduction, or business convention.
- BLOCK: a required source is absent, conversion failed, material/price is missing, or candidates conflict without a defensible resolution.

Approved overrides do not erase original issues; preserve both in evidence.

## First run and review loop

```powershell
# 先进入已安装的 SKILL 根目录，例如：
Set-Location "$env:USERPROFILE\.codex\skills\cad-stainless-quote"

# First use only
powershell -ExecutionPolicy Bypass -File .\scripts\setup.ps1

# Environment check
& .\scripts\run.ps1 doctor

# Initial analysis. Without a price book this is deliberately a takeoff draft.
& .\scripts\run.ps1 run "C:\drawings\project.zip" --out "C:\runs\project"
```

DWG conversion and RAR extraction use external tools that you install and license separately. Native DXF and ZIP input do not need them; absence of optional DWG/RAR tools is reported as `REVIEW` by `doctor` and becomes a per-input block only when that capability is actually needed.

Review `outputs/不锈钢算量报价.xlsx`, `<run-dir>/review-pack.json`, `issues.json`, and the evidence images. Copy `assets/confirmations-template.json`, replace component/candidate IDs only with values from the review pack, and record reviewer, review time, reason, and source evidence.

命令退出码 `2` 可以表示安全的 `BLOCK`（证据不足而拒绝报价），不等于程序崩溃。以终端 JSON 的 `status`、`safe_outcome` 以及 `<run-dir>/issues.json` 为准；真正的执行异常也会给出 `error` 字段。

```powershell
& .\scripts\run.ps1 resume "C:\runs\project" `
  --confirmations "C:\review\confirmations.json" `
  --price-book "C:\prices\approved.json"
```

Resume must not repeat extraction, DWG conversion, or indexing. Review unresolved rows again; do not call a workbook a commercial quotation while any required row remains REVIEW/BLOCK or lacks an approved price.

`resume` 默认继承首次运行的价格库路径、SHA-256、版本、报价日期、币种和含税口径。原价格库丢失或哈希变化会阻断商业报价；显式替换价格库或口径时，变化会写入 manifest 审计记录。

## Output meaning

- `PASS`: confirmed chain, required measurement roles, compatible material, pricing basis, and approved exact price are complete.
- `REVIEW`: plausible evidence exists, but an explicit decision or business convention is still required.
- `BLOCK`: required evidence is absent/invalid, or the run encountered a condition that makes calculation unsafe.

Only PASS amounts belong in the confirmed total.
