# CAD Stainless Quote

Safety-first CAD takeoff and quotation workflow for stainless-steel/metal `MT` items in interior-construction drawings. It is packaged as a Codex Skill plus a deterministic Python CLI.

> 中文说明：输入 DWG/DXF/ZIP/RAR/7z 图纸包，系统生成 MT 候选、平面→立面→节点证据链、尺寸与数量候选、确定性工程量，以及可审计的 Excel 报价草稿。缺图、缺尺寸或缺价格时会明确 `REVIEW/BLOCK`，不会猜答案。

## Project status

This project is experimental. The engineering pipeline is operational and safety-gated, but it has **not** been proven to match a professional quantity surveyor across arbitrary drawing standards. Use the generated workbook as a review draft until every evidence chain and price is approved.

No customer drawings, quotations, private paths, or proprietary runtime packages are included in this repository.

## What it does

- Safely inventories directories and ZIP/RAR/7z archives with traversal, link, encryption, collision, size, and archive-bomb checks.
- Converts DWG to DXF through an installed ODA File Converter and records conversion/audit results.
- Indexes layouts, viewports, block-expanded text, dimensions, leaders, and coordinates into JSON plus SQLite.
- Builds a reusable drawing catalog with searchable MT/drawing-code/room text, dimension candidates, and optional content-addressed sheet-preview caching, so later review does not repeatedly reopen every source drawing.
- Detects normalized `MT` annotations and conservative material-table candidates.
- Splits multi-view sheets into local drawing panels and keeps leader text attached to the arrow-target panel.
- Resolves explicit view-number → target-sheet callouts and preserves the supporting entity IDs.
- Uses exact or unique distinctive material aliases; ambiguous shared aliases remain unresolved.
- Builds reviewable plan → elevation → detail relationships without treating annotation count as quantity.
- Extracts bounded length, height, quantity, and unfolded-width candidates with unit and paper-space safeguards.
- Separates lettered casework variants by exact CAD plan/elevation titles, same-view material codes, and multi-view native dimensions instead of reusing the first shared-material candidate.
- Applies optional, external versioned estimator-convention profiles as non-mutating `REVIEW`/`CONFIRMED` candidates; no bundled customer convention is required or trusted.
- Revisits raw DXF polylines near leader targets to surface conservative repeated-instance quantity candidates; they remain `REVIEW` and never auto-fill the bill.
- Rejects sheet-wide detail dimensions unless an explicit detail relation and a unique component-local material anchor exist.
- Generates component-bound locator and close-up images directly from vector CAD for plan, elevation, and detail stages; missing images stay visible as review rows.
- Generates numbered dark-CAD candidate boards for repeated same-sheet/same-MT occurrences and forbids selecting the first rendered occurrence by default.
- Suggests component/dimension envelopes for better framing while keeping algorithmic bboxes explicitly `REVIEW`.
- Overlays stable `G1`, `G2`, ... labels for high-value bounded primitives and connected paths while preserving their CAD/block provenance and refusing to infer measurement roles.
- Renders bounded `cad-dark-full` panel catalogs (including no-MT details), overlays projected paper annotations, and stores plan/elevation/detail as distinct stage candidates.
- Preserves every evidence candidate instead of silently displaying only the first image bundle.
- Audits screenshot evidence for missing files, cross-component reuse, one image masquerading as multiple stages, near-blank renders, and unreadably small spreadsheet embedding.
- Preserves image aspect ratio, source/image hashes, CAD bounding boxes, entity IDs, DXF handles, and rendered pixel dimensions for audit.
- Builds candidate-recall diagnostics and conservative multi-project held-out evaluation summaries.
- Applies audited reviewer confirmations and resumes without re-running immutable upstream stages.
- Computes `m`, `m²`, piece, and set quantities deterministically.
- Treats a stainless screen with artistic-glass infill as one auditable composite row: whole-elevation projected area, both exact material identities, exclusive evidence ownership, and composite-only price matching. Construction depth is never substituted for a projection axis or quantity.
- Supports a strictly parsed, CAD-evidence-bound engineering-quantity expression when a proven billing axis or internal topology multiplier cannot be represented by the visible width/length/quantity columns; the standard formula remains the default.
- Matches only approved, versioned, specification-compatible prices.
- Exports a five-sheet Excel workbook when evidence rendering is enabled: quotation, provenance, pending review, run metadata, and screenshot evidence. Debug runs with evidence disabled retain the legacy four-sheet layout.
- Separates PASS precision from automation rate when evaluating against a reviewed gold set.

## Safety invariants

1. Original inputs are read-only; every run uses an isolated output directory.
2. MT text occurrences are never used directly as physical quantity.
3. Missing or conflicting evidence stays `REVIEW/BLOCK`.
4. Only `PASS` rows may contribute to the confirmed total.
5. A price requires an approved source, version, date, currency, tax basis, material, thickness, finish, process, unit, and pricing method.
6. Human benchmark spreadsheets are imported as candidate truth and audited before use.
7. Repeated linework is only a review candidate; a reviewer must prove that it represents independent billable components.
8. A `PASS` item must have rendered locator and close-up images for the same sequence and `component_id` at plan, elevation, and detail stages. The sole exception is an audited `detail=NOT_APPLICABLE` negative search covering the selected elevation; missing detail candidates never qualify automatically.
9. Different component names cannot reuse one occurrence image, and one image cannot stand in for multiple evidence stages.
10. A composite screen cannot become commercial `PASS` from proximity alone: its glass code must target the reviewed screen INSERT boundary, one glass evidence entity cannot support multiple physical rows, and a stainless-only price is ineligible.

## Requirements

- Windows 10/11
- Python 3.11+
- A separately installed and appropriately licensed DWG converter for DWG input; native DXF needs no converter
- [7-Zip](https://www.7-zip.org/) for advertised RAR and 7z extraction support
- PowerShell

The setup script installs only the Python environment. It does not install or accept licenses for a DWG converter or 7-Zip; install external tools yourself when needed.

## Quick start

```powershell
powershell -ExecutionPolicy Bypass -File .\skills\cad-stainless-quote\scripts\setup.ps1
& .\skills\cad-stainless-quote\scripts\run.ps1 doctor

& .\skills\cad-stainless-quote\scripts\run.ps1 run `
  "D:\drawings\project.zip" `
  --out "D:\cadquote-runs\project"
```

The full run writes `index\drawing_catalog.json` and
`index\drawing_catalog.sqlite` after the immutable CAD index.  To build or
refresh the lookup layer from an existing run, optionally render one cached
preview per eligible sheet:

```powershell
& .\skills\cad-stainless-quote\scripts\run.ps1 preindex `
  "D:\cadquote-runs\project\index\cad_index.json" `
  --out "D:\cadquote-runs\project\index\drawing-catalog" `
  --render-previews
```

The preview cache is keyed by source hash, sheet/layout/bbox, render profile,
target pixels, margin, and renderer version.  A preview is a navigation aid,
not proof of physical component ownership; the later candidate/selection and
evidence stages still have to confirm the plan → elevation → detail chain.
Search the catalog without reopening CAD:

```powershell
& .\skills\cad-stainless-quote\scripts\run.ps1 catalog-search `
  "D:\cadquote-runs\project\index\drawing-catalog\drawing_catalog.json" MT-01
```

For DWG files, configure ODA File Converter, AutoCAD Core Console, or `dwg2dxf` separately. ODA states that non-members may use its File Converter for non-commercial applications only; commercial users must choose and license a suitable backend. The converter is not bundled with this repository.

Review `D:\cadquote-runs\project\review-pack.json`, copy only real candidate IDs into an audited confirmation file, then resume:

```powershell
& .\skills\cad-stainless-quote\scripts\run.ps1 resume `
  "D:\cadquote-runs\project" `
  --confirmations "D:\reviews\confirmations.json"
```

When repeated occurrences share a sheet and MT code, generate a numbered review board before confirming a physical component:

```powershell
& .\skills\cad-stainless-quote\scripts\run.ps1 candidate-boards `
  "D:\cadquote-runs\project\index\cad_index.json" `
  "D:\cadquote-runs\project\analysis\mt_occurrences.json" `
  --panels "D:\cadquote-runs\project\analysis\panels.json" `
  --out "D:\cadquote-runs\project\analysis\candidate-boards"
```

After a reviewer or vision stage explicitly selects occurrence IDs for each
physical component, render row-specific locator and close-up images. This
stage refuses to guess by taking the first candidate and blocks one occurrence
from being assigned to differently named components:

```powershell
& .\skills\cad-stainless-quote\scripts\run.ps1 selected-evidence `
  "D:\cadquote-runs\project\analysis\candidate-boards\candidate_boards.json" `
  "D:\cadquote-runs\project\analysis\candidate-boards\panel-cache\index.json" `
  "D:\cadquote-runs\project\analysis\component-selections.json" `
  --out "D:\cadquote-runs\project\analysis\selected-evidence"
```

The locator and close-up are two views of the same drawing stage. They remain review evidence until the component bounding box,
plan/elevation/detail chain, dimension roles, and quantity basis are confirmed.
When a reviewed CAD-coordinate `object_bbox` with `object_bbox_state=CONFIRMED` is present in the selection, the
close-up frames that geometry together with the selected MT leader; otherwise
the row remains REVIEW. Use `component-frames`, `panel-catalog`,
`annotate-panel-catalog`, and `stage-evidence` to build the auditable three-stage
review pack; none of those commands chooses a relation merely because it ranks first.

If a close-up inherited too few pixels from a whole-panel raster, re-render its bounded CAD
region directly from the DXF instead of enlarging the PNG:

```powershell
& .\skills\cad-stainless-quote\scripts\run.ps1 component-closeups `
  "D:\cadquote-runs\project\index\cad_index.json" `
  "D:\cadquote-runs\project\analysis\panels.json" `
  "D:\cadquote-runs\project\analysis\component-frames\component_frames.json" `
  --out "D:\cadquote-runs\project\analysis\component-closeups" `
  --target-px 3200
```

The vector re-render is easier to inspect but remains REVIEW until its physical-component bbox
and drawing chain are explicitly confirmed.

When important outlines are inside anonymous, dynamic, or nested blocks, expand geometry only
inside those rendered component bboxes:

```powershell
& .\skills\cad-stainless-quote\scripts\run.ps1 component-geometry `
  "D:\cadquote-runs\project\index\cad_index.json" `
  "D:\cadquote-runs\project\analysis\component-closeups\component_closeups.json" `
  --out "D:\cadquote-runs\project\analysis\component-geometry"
```

The probe preserves transformed world coordinates, root/block provenance, exact or declared-
tolerance approximate curve length, and same-layer endpoint-connected width/height/path-length
candidates. All results remain `REVIEW`; they do not assign a length or quantity role. Limit or
recursion truncation is explicit and makes the affected region incomplete.

Create a numbered geometry board to inspect those candidates on the exact vector close-up:

```powershell
& .\skills\cad-stainless-quote\scripts\run.ps1 geometry-boards `
  "D:\cadquote-runs\project\analysis\component-closeups\component_closeups.json" `
  "D:\cadquote-runs\project\analysis\component-geometry\component_geometry.json" `
  --out "D:\cadquote-runs\project\analysis\geometry-boards"
```

`G1`, `G2`, ... are deterministic review ordinals, not length/width/quantity roles. The manifest
retains primitive/path IDs, bbox width and height, path length, layer, block provenance, image
hashes, and explicit source/candidate truncation. Missing or identity-mismatched close-ups produce
no board, and every surviving candidate remains `measurement_role=null`. Capped boards use a
stable balanced round-robin across closed primitives, connected paths, handle-backed primitives,
largest bboxes, and region-center candidates so a single geometry class cannot consume the board.

Label each visible native dimension with an auditable CAD entity reference before choosing it:

```powershell
& .\skills\cad-stainless-quote\scripts\run.ps1 measurement-boards `
  "D:\cadquote-runs\project\analysis\panels.json" `
  "D:\cadquote-runs\project\analysis\component-closeups\component_closeups.json" `
  --takeoff "D:\cadquote-runs\project\analysis\takeoff_draft.json" `
  --out "D:\cadquote-runs\project\analysis\measurement-boards"
```

The `D1`, `D2`, ... overlay is a review aid. It binds a visible value to an entity/handle but does
not decide its role, component ownership, or physical quantity.

Keep lettered sibling counters/cabinets in separate physical buckets before applying an estimator
convention:

```powershell
& .\skills\cad-stainless-quote\scripts\run.ps1 variant-bindings `
  "D:\review\target-free-row-tasks.json" `
  "D:\cadquote-runs\project\analysis\panels.json" `
  --out "D:\cadquote-runs\project\analysis\variant-bindings.json"
```

The task JSON may contain row name, material code, and candidate sheet IDs, but H/J/K/L target
fields are rejected. A suffix such as `A` or `B` must match exact CAD plan/elevation titles; the
material code must appear in every selected view, and plan width/depth plus elevation height need
independent native dimension handles. Opposite variants are retained as negative evidence. The
output is target-free but still `REVIEW`, never a direct quotation update.

After component and measurement facts exist, an estimator convention profile can propose a
quantity role, calculation basis, unfolded-profile suggestion, or deterministic area/linear/set
formula without editing the takeoff:

```powershell
& .\skills\cad-stainless-quote\scripts\run.ps1 convention-candidates `
  "D:\cadquote-runs\project\analysis\takeoff_draft.json" `
  "D:\review\estimator-conventions.json" `
  --out "D:\cadquote-runs\project\analysis\convention-candidates.json"
```

Start from `assets/estimator-convention-profile-template.json`; the machine-readable contract is
`assets/estimator-convention-profile-schema-v1.json`. A rule match is `REVIEW` unless the profile
and rule are both explicitly `APPROVED`, profile approval includes reviewer/timezone-aware
timestamp/reason, the physical component is `PASS`, every formula input is an auditable `PASS`
measurement entity, and no input or existing takeoff value conflicts. Even `CONFIRMED` convention
candidates have `commercial_effect=NONE`: they do not mutate the takeoff, promote a quotation row,
set a price, or release an amount. Quantity never defaults to one. Outer quantity `1` is proposed
only when an audited expression explicitly contains its own multiplication factor, preventing
double multiplication.

These staged-evidence commands are currently explicit review steps. The standard
`run`/`resume` commands do not yet invoke them or merge their manifest into the
standard quotation workbook automatically. Their panel and stage manifests use
`path_scope=local_run_diagnostics` and may contain absolute paths; do not commit or
publish those manifests. Export only deliberately redacted, source-relative data.

Run the negative-only image gate on row-level evidence JSON before calling a workbook reviewable:

```powershell
& .\skills\cad-stainless-quote\scripts\run.ps1 evidence-quality `
  "D:\cadquote-runs\project\analysis\evidence-rows.json" `
  --out "D:\cadquote-runs\project\analysis\evidence-quality.json"
```

Add `--price-book` only when the price source is approved. Templates are under `skills/cad-stainless-quote/assets/`.

## Repository layout

```text
skills/cad-stainless-quote/
  SKILL.md                 Codex operating contract
  scripts/cad_quote.py     CLI entry point
  scripts/cadquote/        deterministic pipeline
  references/              schemas and review rules
  assets/                  confirmation and price templates
tests/                     unit, security, integration, and regression tests
```

## Development

```powershell
& .\skills\cad-stainless-quote\.venv\Scripts\python.exe -m pip install pytest ruff
& .\skills\cad-stainless-quote\.venv\Scripts\python.exe -m pip install -e ".[vision]"
& .\skills\cad-stainless-quote\.venv\Scripts\python.exe -m pytest -q
& .\skills\cad-stainless-quote\.venv\Scripts\python.exe -m ruff check .
```

Real client data must never be committed. Add new behavior with synthetic fixtures or explicitly redistributable samples only.

## Known limitations

- Windows-first because the supported launcher is PowerShell; native DXF is the most portable input path.
- PDF and raster images are inventoried attachments, not trusted CAD evidence.
- Custom proxy objects and unusual office-specific drawing conventions may require manual review.
- Accurate component grouping and evidence selection still depend on drawing quality and convention.
- Commercial totals require an approved price book supplied by the user.

## License

MIT. External tools and Python dependencies retain their own licenses; no DWG converter or 7-Zip binary is redistributed here. See [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).
