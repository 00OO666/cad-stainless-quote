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
- Detects normalized `MT` annotations and conservative material-table candidates.
- Builds reviewable plan → elevation → detail relationships without treating annotation count as quantity.
- Extracts bounded length, height, quantity, and unfolded-width candidates with unit and paper-space safeguards.
- Applies audited reviewer confirmations and resumes without re-running immutable upstream stages.
- Computes `m`, `m²`, piece, and set quantities deterministically.
- Matches only approved, versioned, specification-compatible prices.
- Exports a four-sheet Excel workbook: quotation, provenance, pending review, and run metadata.
- Separates PASS precision from automation rate when evaluating against a reviewed gold set.

## Safety invariants

1. Original inputs are read-only; every run uses an isolated output directory.
2. MT text occurrences are never used directly as physical quantity.
3. Missing or conflicting evidence stays `REVIEW/BLOCK`.
4. Only `PASS` rows may contribute to the confirmed total.
5. A price requires an approved source, version, date, currency, tax basis, material, thickness, finish, process, unit, and pricing method.
6. Human benchmark spreadsheets are imported as candidate truth and audited before use.

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

For DWG files, configure ODA File Converter, AutoCAD Core Console, or `dwg2dxf` separately. ODA states that non-members may use its File Converter for non-commercial applications only; commercial users must choose and license a suitable backend. The converter is not bundled with this repository.

Review `D:\cadquote-runs\project\review-pack.json`, copy only real candidate IDs into an audited confirmation file, then resume:

```powershell
& .\skills\cad-stainless-quote\scripts\run.ps1 resume `
  "D:\cadquote-runs\project" `
  --confirmations "D:\reviews\confirmations.json"
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
