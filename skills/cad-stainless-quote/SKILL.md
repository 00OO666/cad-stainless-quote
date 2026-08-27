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
- Render every required viewport (including detail panels with no MT text) with `scripts/run.ps1 panel-catalog <index-json> --panels <panels-json> --render-profile cad-dark-full --out <catalog-dir>`. Then run `annotate-panel-catalog <panels-json> <panel-catalog-json> --out <annotated-dir>` to overlay already-projected paper-space text/leaders without redrawing a whole paper layout.
- Run `scripts/run.ps1 selected-evidence <candidate-boards-json> <panel-index-json> <selections-json> --out <evidence-dir>`. This creates row-specific same-stage locator/close-up pairs and blocks one occurrence from being assigned to different components. A bbox is accepted as confirmed framing only with explicit `object_bbox_state=CONFIRMED`; algorithmic or missing bboxes keep the row REVIEW.
- Run `scripts/run.ps1 stage-evidence <panels-json> <edges-json> <selected-evidence-json> <panel-catalog-json> <selections-json> --out <stage-dir>`. The output keeps plan, elevation, and detail as separate stages, preserves all candidates, and never selects the first/highest relation edge. Only an explicit complete three-stage selection with a unique `component_id`, connected relation edges, reviewed bboxes, verified dark-CAD image hashes, and a same-detail-sheet dimension can become `CONFIRMED`.
- Add `--price-book <xlsx-or-json>` only when the user supplied or approved that price source.
- After reviewing `<run-dir>/review-pack.json`, run `scripts/run.ps1 resume <run-dir> --confirmations <json>` so conversion and indexing are reused. `resume` inherits the original pricing context unless it is explicitly changed.
- For diagnosis or another partial rerun, use the stage commands shown by `scripts/run.ps1 --help`.
- The full run writes `analysis/vector_quantity_probes.json`. For an existing index, run `scripts/run.ps1 vector-probe <index-json> <occurrences-json> --panels <panels-json> --out <json>` to revisit raw DXF polylines near MT leader targets. These are REVIEW-only candidates and never auto-fill quantity.
- For an accuracy check, run `scripts/run.ps1 evaluate <predicted-json> <gold-json> --policy <versioned-policy-json> --out <report-json>`. The bundled policy template deliberately leaves disputed length/quantity tolerances pending, so it cannot produce a 95% PASS until those rules are explicitly approved.
- For a held-out suite, run `scripts/run.ps1 evaluate-batch <manifest-json> --out <batch-dir>`. Inspect every project gate in addition to the JSON/Markdown aggregate; the batch may PASS only when every project PASSes.
- A CLI exit code of 2 can mean a safe BLOCK result, not a crash; inspect the printed `status`/`safe_outcome` and `<run-dir>/issues.json`.
- Read [references/workflow.md](references/workflow.md) before adjudicating REVIEW/BLOCK items.
- Read [references/confirmations.md](references/confirmations.md) before writing or applying reviewer choices.
- Read [references/price-book.md](references/price-book.md) before accepting or matching prices.
- Read [references/gold-standard.md](references/gold-standard.md) before importing a human takeoff as evaluation truth.
- Read [references/schema.md](references/schema.md) when changing structured outputs or integrating another tool.
- Read [references/evaluation.md](references/evaluation.md) when claiming accuracy or comparing with a human takeoff.

## Invariants

1. Preserve originals; extract and convert into a run directory.
2. Every output row must carry source evidence down to file, layout/sheet, region, and entity handle when available.
3. Count physical component instances, not MT text occurrences.
4. Link plan → elevation → detail before accepting length, quantity, or unfolded width as PASS.
5. Use deterministic code for formulas, units, totals, and price matching.
6. Missing or conflicting evidence is REVIEW/BLOCK, never a guessed PASS.
7. A price match requires the approved price-book version and compatible material, thickness, finish, process, and pricing basis.
8. Report both precision of PASS items and automation rate; do not hide unresolved items.
9. Treat PDFs and images as inventoried attachments unless a later verified parser explicitly extracts evidence from them.
10. Never use an unscaled paper-space geometric dimension as millimetres, and never let non-PASS rows enter the confirmed quotation total.
11. A repeated-vector group is only a quantity REVIEW candidate. Require one leader-anchored congruent group, independent handles, an untruncated source scan, and later semantic corroboration before a reviewer may accept it.
12. Bind every workbook evidence bundle to `component_id`. Include a locator image and a readable close-up that together trace plan → elevation → detail/dimension, with drawing number, bbox, entity IDs, DXF handles, source/asset SHA-256, and original pixel dimensions.
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

## Outputs

The pipeline produces a machine-readable manifest/index, material-code candidate list, read-only DOCX material-book mappings, unnumbered stainless-material mention diagnostics and candidate edges, raw-DXF repeated-vector quantity probes, evidence graph, component-bound locator/close-up evidence, review pack, takeoff JSON, quotation workbook, pending-confirmation list, and run diagnostics. A quotation with unresolved evidence, missing images, or prices is a takeoff draft, not a completed commercial quote.

This skill currently targets Windows because the supported launcher is PowerShell. DWG conversion is an external adapter (ODA File Converter, AutoCAD Core Console, or `dwg2dxf`) and is not distributed by this project.
