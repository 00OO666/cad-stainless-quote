# Contributing

Contributions are welcome when they preserve the project's conservative evidence model.

## Ground rules

1. Never add real client drawings, quotations, paths, screenshots, or identifying metadata.
2. Use synthetic fixtures or samples with explicit redistribution rights.
3. Never turn annotation frequency into physical quantity.
4. Never auto-pass a missing, ambiguous, conflicting, or unpriced evidence chain.
5. Keep formulas, unit conversion, totals, and price matching deterministic.
6. Add regression tests for security and business-rule changes.

## Before opening a pull request

```powershell
& .\skills\cad-stainless-quote\.venv\Scripts\python.exe -m pytest -q
& .\skills\cad-stainless-quote\.venv\Scripts\python.exe -m ruff check .
```

Explain the evidence rule being changed, the failure mode it addresses, and why unresolved cases remain safely reviewable.

## Progress and handoff records

Keep [PROGRESS.md](PROGRESS.md) current after a meaningful milestone or before handing work over. Distinguish completed development, published source, pending validation, and unresolved work. A documentation-only update must not imply a code release.

Keep full evidence and client-specific results in private project notes. Publish only a deliberately redacted engineering summary: no client names, paths, drawing identifiers, measurements, screenshots, quotations, or client-level benchmark counts. Test-suite results must state which codebase was tested and must never be presented as takeoff accuracy.

Before publishing, inspect the exact staged file list and diff, run proportionate checks, then verify the pushed commit against the remote. Record a failed or unverified sync as pending, not complete.
