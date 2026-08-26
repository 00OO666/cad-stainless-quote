# Evaluation protocol

Do not claim human-level or better accuracy from a single example.

## Gold-set preparation

Each gold row needs an approved physical component identity, MT code, plan location, elevation, detail, dimensions, quantity, unit, formula, and source evidence. Mark genuinely unknowable fields as unresolved rather than fabricating a target.

## Metrics

- MT occurrence recall and precision.
- Physical component recall and duplicate rate.
- Plan→elevation and elevation→detail edge accuracy.
- PASS precision.
- Automatic PASS rate.
- Complete evidence-chain coverage.
- Per-line absolute/relative quantity error.
- Formula/unit validation rate.
- Price-match precision and total amount variance when approved prices exist.

## Dataset roles

- Development set: an authorized, fully adjudicated set used to improve extraction and linking rules.
- Held-out set: a separately authorized generalization test. Do not add dataset-specific exceptions before the first blind result is recorded.
