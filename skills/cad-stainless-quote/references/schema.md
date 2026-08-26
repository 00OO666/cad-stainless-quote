# Structured data schema

The canonical Pydantic definitions live in `scripts/cadquote/models.py`. JSON is the current stage-exchange format; SQLite is the query/audit mirror and uses the same identities and field meanings.

## Identity and traceability

- `SourceFile.id`: `file:<content-sha256>:<relative-path-sha256-prefix>`；内容哈希仍单独保存在 `sha256` 字段。
- `Sheet.id`: stable hash of source file id + layout/viewport/drawing number.
- `CadEntity.id`: stable hash of source file id + space + handle; use a geometry hash when a handle is absent.
- `MtOccurrence.id`, `ComponentInstance.id`, and `EvidenceEdge.id` must be deterministic for identical inputs.
- Keep source-relative paths in outputs; absolute paths may appear only in local run diagnostics.

## Material-code compatibility

- `mt_code` remains the normalized business-code field used by the existing pipeline and the
  17-column quote sheet. It may contain a legacy `MT-*` code or another configured material-code
  family such as `GC-SS-*`; non-MT families must not be rewritten as `MT-*`.
- `MaterialSpec`, `MtOccurrence`, and `ComponentInstance` add optional
  `raw_material_code` and `material_code_family` fields. Older JSON without these fields remains
  valid.
- The default detector treats `MT` and `GC-SS` as stainless candidates, retains `GC-MT` only as a
  lower-confidence review family, and excludes unconfigured families such as `GC-GL`/`GC-MR`.
- Unnumbered text that explicitly describes stainless steel is written to
  `analysis/material_mentions.json` as a low-confidence `MaterialMention`. This diagnostic model
  has no `mt_code` field and must never be promoted by inventing a code.
- An ingested DOCX project material book is parsed read-only from OOXML only when a row or
  paragraph contains an explicitly configured code family and a non-empty description.
  `MaterialSpec.source_type=docx_material_book` keeps the document SHA-256 plus table/cell or
  paragraph locators in `source_sha256`, `source_location`, and `source_evidence`. These records
  always start as `REVIEW`.
- `analysis/material_mention_matches.json` stores auditable
  `material_mention_to_material` candidate edges. A normalized description that maps to one
  stainless code is `REVIEW`; multiple codes or a conflicting material definition are `BLOCK`.
  A unique edge may seed a low-confidence `MtOccurrence`, but never a PASS item or quantity.

## PASS requirements

A `TakeoffItem` may be PASS only when:

1. its MT code resolves to a non-conflicting material record;
2. its physical component instance is not a duplicate;
3. plan location, elevation and detail evidence are linked, unless a documented pricing rule explicitly makes a stage unnecessary;
4. width/length/quantity are backed by entity handles or an approved visual/manual override;
5. the deterministic calculation validates;
6. an approved, versioned, exact price entry matches the material and pricing context.

Otherwise the item remains REVIEW or BLOCK with a reason. A run without a price book may contain a valid quantity draft, but it is not a commercial PASS quotation.

## Quote workbook fields

The required business columns are:

`序号、名称、MT编号、材料、平面图位置、对应立面、对应节点、展开规格、宽、长度、数量、工程量、单位、计价方式、单价、金额、备注`.

Additional sheets store entity-level evidence, unresolved issues, price provenance, and run metadata.

## Candidate-gold workbook import

`gold-import` schema `1.1` keeps the canonical `TakeoffItem` separate from source-only
audit data. `GoldRow.source_material_code` preserves a workbook's material/project
code without treating it as an MT code, and `GoldRow.mt_code_source` records whether
an explicit MT came from the MT, material-code, or material-description cell.
`GoldRow.image_evidence` and `GoldSheetEvidence.images` retain workbook image anchors,
categories, and `DISPIMG` references. `GoldCellEvidence` retains cached values together
with normal/array formula metadata. These records are candidate evidence only and do
not satisfy CAD plan → elevation → detail PASS requirements by themselves.

## Evaluation policy and report

`EvaluationPolicy` schema `1.0` is the canonical versioned acceptance contract. It contains
per-field enablement, text normalization, unfolded-expression mode, independent numeric relative
tolerances, explicit gold-zero handling, the optional exact amount rule, and the project target
accuracy. A `null` numeric tolerance is a pending business decision, not a wildcard or zero
tolerance.

Evaluation report schema `2.0` writes the validated policy body, `policy_version`, and canonical
`policy_hash`. Each entry in `projects` contains eligible/correct row counts, replication recall,
output precision, missing/extra/duplicate diagnostics, field summaries, and row-level field
results. `overall_gate` is one of `PASS`, `FAIL`, `INDETERMINATE`, or `BLOCKED`. The aggregate block
does not supersede the individual project result. Legacy comparison metrics remain top-level for
backward-compatible diagnostics but do not control the versioned acceptance gate.
