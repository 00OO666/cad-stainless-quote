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
