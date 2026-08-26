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
- Add `--price-book <xlsx-or-json>` only when the user supplied or approved that price source.
- After reviewing `<run-dir>/review-pack.json`, run `scripts/run.ps1 resume <run-dir> --confirmations <json>` so conversion and indexing are reused. `resume` inherits the original pricing context unless it is explicitly changed.
- For diagnosis or another partial rerun, use the stage commands shown by `scripts/run.ps1 --help`.
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

## Outputs

The pipeline produces a machine-readable manifest/index, MT candidate list, evidence graph, review pack, takeoff JSON, quotation workbook, pending-confirmation list, and run diagnostics. A quotation with unresolved evidence or prices is a takeoff draft, not a completed commercial quote.

This skill currently targets Windows because the supported launcher is PowerShell. DWG conversion is an external adapter (ODA File Converter, AutoCAD Core Console, or `dwg2dxf`) and is not distributed by this project.
