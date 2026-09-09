# Replay file-boundary audit

This optional developer diagnostic runs a Python replay script in a fresh process
and records file-access audit events. It is not imported by the quotation pipeline
and does not change any takeoff, evidence, review, or pricing decision.

Use Python 3.11 or newer with this repository's dependencies. The following is an
illustrative invocation for a separate `demo` project; substitute its script and
arguments with your own:

```powershell
python devtools/replay-boundary-audit/audit_replay.py `
  --project-root ./demo `
  --script ./demo/replay.py `
  --source ./demo/inputs/drawing.dxf `
  --source ./demo/query.json `
  --out-root ./demo/audit-out `
  --report ./demo/audit-out/read-audit.json `
  -- --input inputs/drawing.dxf --out audit-out/result
```

Repeat `--source` for each explicitly allowed DXF or semantic JSON configuration.
The target runs with the project root as its working directory. Both the report
and all target writes must stay inside `--out-root`. Temporary files and common
rendering caches are redirected there. The target may read data it created during
this run; it may not read preexisting output data before initialization.

Unlisted project data reads are blocked. Old `outputs` and gold sources cannot be
allowlisted, and spreadsheet/image sources are rejected. Code imports and known
runtime/font assets are permitted; merely naming a project folder `deps` or
`venv` does not grant access to its data. Source hashes are recorded before and
after execution. A caught access rejection still makes the audit fail, and the
report retains every rejection and the full target exception without truncation.

Integer file descriptors are resolved to their actual filesystem paths. Use
absolute paths with `os.open`: its Python audit event omits `dir_fd`, so relative
`os.open` calls are rejected. Ordinary `open` calls may use relative paths.
Links and child-process execution are outside this diagnostic and are blocked.

The command exits with `0` for `AUDITED_PASS` and `2` for a failed audit or setup.
Setup failures are emitted as JSON to stdout; an invalid report location never
authorizes a write outside the output directory. Reports contain local paths and
source hashes: keep them private rather than committing or publishing them.

## Validation

```powershell
python -B devtools/replay-boundary-audit/test_audit_replay.py
python -m ruff check devtools/replay-boundary-audit
```

Tests create disposable synthetic projects, including a generated DXF and a plot.
They do not open customer drawings or run customer replay scripts. An optional
`--receipt <private-path>` saves the complete test log and source hashes.

## Limits

This is a Python audit-event boundary, not an operating-system sandbox. Native
extensions and operations on inherited descriptors can perform I/O without Python
audit events. Allowed code can also embed data, and the diagnostic does not prove
that an allowlisted JSON contains only semantic configuration. Directory listings
are recorded as metadata, and open events are attempts rather than byte counts.

Inspect replay code independently. A successful audit establishes only that the
observed access policy passed; it does not establish blind prediction, correct
CAD interpretation, complete rows, or an accuracy result.
