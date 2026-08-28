---
name: cad-stainless-quote
description: Analyze interior-construction CAD packages for stainless-steel MT items, trace each item from plan to elevation and detail, calculate auditable quantities, match an explicit price book, and generate quotation and review workbooks. Use for DWG/DXF/ZIP/RAR drawing takeoff or quotation requests involving MT codes, metal trims, stainless panels, door/window surrounds, skirting, or related custom metalwork.
---

# CAD stainless-steel quantity takeoff and quotation

Run the deterministic pipeline first; use model reasoning only to rank or resolve evidence candidates. Never invent a missing drawing, dimension, quantity, material property, processing method, or price.

## Operating modes

- Resolve the installed skill root and run commands from that directory; do not assume the user's project is the current directory.
- On first use on Windows, run `powershell -ExecutionPolicy Bypass -File scripts/setup.ps1`, then run `scripts/run.ps1 doctor`. Install any DWG converter and 7-Zip yourself and comply with their licenses; DXF and ZIP input do not require those external tools.
- For a new drawing package, run `scripts/run.ps1 run <input> --out <run-dir>`.
- When a sheet contains repeated occurrences of the same MT code, run `scripts/run.ps1 candidate-boards <index-json> <occurrences-json> --panels <panels-json> --out <board-dir>`. Use the numbered dark-CAD boards to assign one or more occurrences to a physical `component_id`; never select the first available occurrence by default.
- After writing explicit row/component occurrence selections, run `scripts/run.ps1 component-frames <panels-json> <candidate-boards-json> <selections-json> --out <frame-dir>`. Its geometry envelopes improve framing only; they carry `object_bbox_state=REVIEW` and never confirm a component.
- When a raster crop is too small to read, run `scripts/run.ps1 component-closeups <index-json> <panels-json> <component-frames-json> --out <closeup-dir> --target-px 3200 --render-profile cad-dark-full`. This re-renders the bounded region from the original DXF; it never enlarges an existing PNG and remains REVIEW until the component bbox and chain are confirmed.
- When visible component linework is hidden inside anonymous/dynamic/nested blocks, run `scripts/run.ps1 component-geometry <index-json> <component-closeups-json> --out <geometry-dir>`. It recursively expands only blocks intersecting each rendered bbox and emits transformed LINE/ARC/CIRCLE/polyline/SPLINE/ELLIPSE geometry plus same-layer endpoint-connected measurements. Every result and role remains REVIEW; any source, recursion, regional, or global truncation makes the affected probe incomplete.
- Run `scripts/run.ps1 geometry-boards <component-closeups-json> <component-geometry-json> --out <board-dir>` to overlay stable `G1`, `G2`, ... primitive/path bbox labels on the exact high-resolution close-up. Use the manifest IDs, bbox spans, path lengths, layers, and block provenance for review only. Missing images, identity mismatch, source truncation, and board caps remain explicit; the command always leaves `measurement_role=null`.
- Run `scripts/run.ps1 measurement-boards <panels-json> <component-closeups-json> --takeoff <takeoff-json> --out <board-dir>` before choosing dimensions from a busy close-up. Refer to the visible `D1`, `D2`, ... labels and their exact entity IDs/handles/measurement candidate IDs; identical numeric text elsewhere is not equivalent evidence. The board never selects a role or quantity. Omit `--takeoff` only when no component-bound measurement payload exists yet.
- When sibling casework rows end in `A`, `B`, `C`, and so on, run `scripts/run.ps1 variant-bindings <target-free-task-json> <panels-json> --out <json>`. This prevents the siblings from sharing the first material-code bucket: the suffix must match an exact plan/elevation subview title, every selected view must contain the row material code, and footprint width/depth/body height must be corroborated by independent native DIMENSION handles. H/J/K/L target fields are rejected from the task input. Output remains REVIEW until row ownership, units, and cross-project transfer are independently verified.
- When an estimator convention has been supplied as a separate versioned profile, run `scripts/run.ps1 convention-candidates <takeoff-json> <profile-json> --out <json>`. Treat its output as a non-mutating suggestion stage. `CONFIRMED` requires an explicitly approved profile and rule, reviewer/time/reason, a PASS physical component, auditable PASS measurement entities, and no conflicts; it still cannot promote a quotation row or amount. Never default quantity to one. Outer quantity one is eligible only when the bound expression explicitly contains its internal multiplier.
- Render every required viewport (including detail panels with no MT text) with `scripts/run.ps1 panel-catalog <index-json> --panels <panels-json> --render-profile cad-dark-full --out <catalog-dir>`. Then run `annotate-panel-catalog <panels-json> <panel-catalog-json> --out <annotated-dir>` to overlay already-projected paper-space text/leaders without redrawing a whole paper layout.
- Run `scripts/run.ps1 selected-evidence <candidate-boards-json> <panel-index-json> <selections-json> --out <evidence-dir>`. This creates row-specific same-stage locator/close-up pairs and blocks one occurrence from being assigned to different components. A bbox is accepted as confirmed framing only with explicit `object_bbox_state=CONFIRMED`; algorithmic or missing bboxes keep the row REVIEW.
- Run `scripts/run.ps1 stage-evidence <panels-json> <edges-json> <selected-evidence-json> <panel-catalog-json> <selections-json> --out <stage-dir>`. The output keeps plan, elevation, and detail as separate stages, preserves all candidates, and never selects the first/highest relation edge. A chain becomes `CONFIRMED` only through explicit positive stage selections with a unique `component_id`, connected relation edges, reviewed bboxes, verified dark-CAD image hashes, and a same-detail-sheet dimension, or through an audited `detail=NOT_APPLICABLE` negative search covering the selected elevation.
- Add `--price-book <xlsx-or-json>` only when the user supplied or approved that price source.
- After reviewing `<run-dir>/review-pack.json`, run `scripts/run.ps1 resume <run-dir> --confirmations <json>` so conversion and indexing are reused. `resume` inherits the original pricing context unless it is explicitly changed.
- When a human quantity uses a multi-segment sum, repeated run, perimeter, or one axis of a dimensional specification, encode it as the auditable `derived_measurement` structure in [references/confirmations.md](references/confirmations.md). Every symbol must bind to a real candidate from the same confirmed component chain; never paste a target number into the formula.
- For diagnosis or another partial rerun, use the stage commands shown by `scripts/run.ps1 --help`.
- The full run writes `analysis/vector_quantity_probes.json`. For an existing index, run `scripts/run.ps1 vector-probe <index-json> <occurrences-json> --panels <panels-json> --out <json>` to revisit raw DXF polylines near MT leader targets. These are REVIEW-only candidates and never auto-fill quantity.
- For an accuracy check, run `scripts/run.ps1 evaluate <predicted-json> <gold-json> --policy <versioned-policy-json> --out <report-json>`. The bundled policy template deliberately leaves disputed length/quantity tolerances pending, so it cannot produce a 95% PASS until those rules are explicitly approved.
- For a held-out suite, run `scripts/run.ps1 evaluate-batch <manifest-json> --out <batch-dir>`. Inspect every project gate in addition to the JSON/Markdown aggregate; the batch may PASS only when every project PASSes.
- A CLI exit code of 2 can mean a safe BLOCK result, not a crash; inspect the printed `status`/`safe_outcome` and `<run-dir>/issues.json`.
- Read [references/workflow.md](references/workflow.md) before adjudicating REVIEW/BLOCK items.
- Read [references/physical-binding.md](references/physical-binding.md) before choosing a physical component, H/J/K role, whole-sheet fallback, or blind prediction.
- Read [references/confirmations.md](references/confirmations.md) before writing or applying reviewer choices.
- Read [references/price-book.md](references/price-book.md) before accepting or matching prices.
- Read [references/gold-standard.md](references/gold-standard.md) before importing a human takeoff as evaluation truth.
- Read [references/schema.md](references/schema.md) when changing structured outputs or integrating another tool.
- Read [references/evaluation.md](references/evaluation.md) when claiming accuracy or comparing with a human takeoff.

## Invariants

1. Preserve originals; extract and convert into a run directory.
2. Every output row must carry source evidence down to file, layout/sheet, region, and entity handle when available.
3. Derive the billable multiplier from the physical component and its estimating topology, not MT text occurrences. Distinguish whole assemblies from repeated instances and repeated billable sides/faces (for example paired jambs or four-edge surrounds); never default any of them to one.
4. Link plan → elevation → detail before accepting length, quantity, or unfolded width as PASS. If there is genuinely no detail, require an audited negative search covering the selected elevation and bind every measurement to that elevation; never infer `无` from missing candidates.
5. Use deterministic code for formulas, units, totals, and price matching.
6. Missing or conflicting evidence is REVIEW/BLOCK, never a guessed PASS.
7. A price match requires the approved price-book version and compatible material, thickness, finish, process, and pricing basis.
8. Report both precision of PASS items and automation rate; do not hide unresolved items.
9. Treat PDFs and images as inventoried attachments unless a later verified parser explicitly extracts evidence from them.
10. Never use an unscaled paper-space geometric dimension as millimetres, and never let non-PASS rows enter the confirmed quotation total.
11. A repeated-vector group is only a quantity REVIEW candidate. Require one leader-anchored congruent group, independent handles, an untruncated source scan, and later semantic corroboration before a reviewer may accept it.
12. Bind every workbook evidence bundle to `component_id`. Include a locator image and a readable close-up that together trace plan → elevation → detail/dimension (or the audited detail-not-applicable disposition), with drawing number, bbox, entity IDs, DXF handles, source/asset SHA-256, and original pixel dimensions.
13. Preserve aspect ratio and original raster pixels; never upscale a screenshot. A missing required image is `MISSING`/REVIEW, and a candidate image must stay visibly `CANDIDATE` rather than masquerade as confirmed evidence. PASS and amount release require the complete confirmed chain.
14. Never place customer drawings, workbooks, screenshots, customer paths, local absolute paths, or identifiable project statistics in a public repository.
15. A screenshot is produced only after physical-component selection. Frame the component geometry plus its MT leader and governing dimensions; use a variable-aspect crop rather than a fixed square around the leader point.
16. Locator, component close-up, and node/detail are distinct evidence stages. Reusing one image as multiple stages, or reusing one occurrence image for differently named components, is REVIEW/BLOCK.
17. Use a dark CAD render profile for workbook evidence. If missing Xrefs, fonts, proxy objects, plot styles, blocks, or hatches prevent a faithful render, record the degradation and do not promote the row to PASS.
18. Human workbooks and screenshots may create development labels (`component_id`, sheet/view, CAD bbox, handles, dimension roles, and quantity basis), but they cannot be visible to a held-out prediction run used to claim accuracy.
19. A locator and close-up from one sheet are framing variants of one stage, never proof of two chain stages. Preserve every stage candidate in the audit output; a workbook must not silently display only `evidence[0]`.
20. Final CAD review images use full model-space blocks/hatches plus projected paper annotations where available. Never reuse an image cache keyed only by filename; profile/source/bbox/render-parameter drift must force a rebuild or remain explicitly uncached.
21. `panel-catalog`, merged/annotated panel catalogs, and `stage-evidence` are local-run diagnostics that may contain absolute paths. Never commit or publish their manifests without an explicit redaction/export step.
22. The staged-evidence commands are explicit review stages; do not imply that `run`/`resume` automatically executes them or writes their results into the standard quotation workbook until that integration exists.
23. Estimator conventions are external policy, not drawing evidence. A convention candidate never mutates takeoff rows, prices, status, or commercial totals; project/customer-specific profiles and their observed values must not be bundled in the public Skill.
23. A composite measurement may become PASS only when every dimension symbol resolves to a current component candidate and the dimensional arithmetic is valid. Small integers may express repetition; free additive dimensions, missing source IDs, cross-component terms, and out-of-chain sheets remain BLOCK.
24. A high-resolution close-up must be a fresh bounded vector render from the indexed source DXF. Its manifest must retain the source/sheet/bbox, renderer, hashes, and natural pixel size; re-rendering improves readability but never proves component ownership.
25. A visible measurement value is not a binding. Record the numbered measurement-board candidate and exact CAD entity/handle, then verify it belongs to the confirmed physical component and stage before using it directly or in a derived expression.
26. Lettered casework variants are distinct physical components. Bind the row suffix to exact CAD subview titles and reject opposite variants as negative evidence; never let lexical order or one shared material bucket choose the dimensions.
27. Keep the visible quantity column as the confirmed physical/billable count. When CAD proves that the engineering quantity uses another displayed axis or an internal topology multiplier, use the audited `engineering_quantity_expression` confirmation; bind its basis to CAD entities on the selected chain. Never distort the visible quantity, copy a human target, or use an unaudited free formula to obtain a match.

## Outputs

The pipeline produces a machine-readable manifest/index, material-code candidate list, read-only DOCX material-book mappings, unnumbered stainless-material mention diagnostics and candidate edges, raw-DXF repeated-vector quantity probes, evidence graph, component-bound locator/close-up evidence, review pack, takeoff JSON, quotation workbook, pending-confirmation list, and run diagnostics. A quotation with unresolved evidence, missing images, or prices is a takeoff draft, not a completed commercial quote.

This skill currently targets Windows because the supported launcher is PowerShell. DWG conversion is an external adapter (ODA File Converter, AutoCAD Core Console, or `dwg2dxf`) and is not distributed by this project.
