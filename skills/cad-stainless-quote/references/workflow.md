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

## Quantity evidence

Prefer an explicit, component-bound quantity label. Split labels such as `QTY`, `=`, `2`
may be joined only inside the same CAD parent block or a real leader annotation identity;
never join nearby free text, cross-sheet tokens, or a size expression such as `50*200`.

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
