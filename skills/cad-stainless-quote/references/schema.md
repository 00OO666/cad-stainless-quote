# Structured data schema

The canonical Pydantic definitions live in `scripts/cadquote/models.py`. JSON is the current stage-exchange format; SQLite is the query/audit mirror and uses the same identities and field meanings.

## Identity and traceability

- `SourceFile.id`: `file:<content-sha256>:<relative-path-sha256-prefix>`；内容哈希仍单独保存在 `sha256` 字段。
- `Sheet.id`: stable hash of source file id + layout/viewport/drawing number.
- `CadEntity.id`: stable hash of source file id + space + handle; use a geometry hash when a handle is absent.
- `MtOccurrence.id`, `ComponentInstance.id`, and `EvidenceEdge.id` must be deterministic for identical inputs.
- Keep source-relative paths in outputs; absolute paths may appear only in local run diagnostics.

## Component screenshot evidence contract

Any quotation evidence manifest or workbook integration must group images by `component_id`, not
by a mutable row number. Each component bundle requires at least a locator image and a close-up,
with ordered stages sufficient to audit plan → elevation → detail/dimension.

Each image record must preserve:

- `component_id`, evidence role (`locator` or `closeup`), chain stage, and state
  (`CANDIDATE`, `CONFIRMED`, or `MISSING`);
- drawing number/layout, CAD `bbox`, related CAD entity IDs, and DXF handles;
- source-file SHA-256, evidence-asset SHA-256, and a source-relative asset path;
- original pixel width/height, aspect ratio, and any vector render scale/DPI.

Raster assets must retain their original bytes/pixels and aspect ratio and must never be
upscaled. A clearer image must be re-rendered from vector CAD, not interpolated from a smaller
bitmap. `MISSING` at any required stage keeps the item REVIEW/BLOCK. `CANDIDATE` images are
review material only and cannot satisfy PASS evidence. Amount is releasable only when the full
component chain and required images are `CONFIRMED`; otherwise it stays outside the confirmed
total.

Customer assets and identifying metadata are private-run data. Customer drawings, workbooks,
screenshots, names, paths, local absolute paths, hashes tied to a real delivery, and identifiable
project statistics must never be committed to a public repository.

### Stage evidence manifest

`stage-evidence` schema `1.1` stores exactly three required stage buckets: `plan`, `elevation`,
and `detail`. Each bucket contains its own `state`, `selected`, `candidates`, and `reason_codes`.
Each candidate retains `sheet_id/kind`, drawing number, occurrence IDs, relation edge ID/basis,
reference entity IDs, object bbox, measurement IDs, context/close-up image paths, panel bbox, and
render profile. Locator and close-up are image roles inside one stage; they are never interpreted
as separate stages.

Candidate order has no semantic meaning. Every selected stage requires an explicit unique
`candidate_id`; plan and detail also require the exact `relation_edge_id`. The plan edge must end
at the selected elevation and the detail edge must start at that same elevation. A complete chain
can be `CONFIRMED` only when it has a non-empty component ID that is unique in the manifest, a
reviewed CAD bbox, reviewer/reason/timezone-aware timestamp, decodable context and close-up images
whose SHA-256 values match, and an allowed dark-CAD render profile. Context and close-up may not
share one SHA, and no image SHA may be reused across plan/elevation/detail. Detail confirmation
also requires a `DIMENSION` entity on the selected detail sheet.

`detail` may instead have state `NOT_APPLICABLE`, but only from an explicit audited negative
search. That disposition stores `kind=not_applicable`, a non-empty basis, reviewer metadata,
`searched_sheet_ids` covering the selected elevation, and optional in-scope reference entity IDs.
It has no positive candidate, relation edge, bbox, measurement, or image. Absence of a discovered
detail candidate is not proof of non-applicability. The manifest advertises
`accepted_detail_states=[CONFIRMED, NOT_APPLICABLE]`.

A declared stable component/row key is authoritative: a missing or ambiguous key is BLOCK and may
not fall back to sequence. Sequence fallback exists only for uniquely matched legacy rows whose
declared identity fields agree, and it can never produce `CONFIRMED`. An algorithmic component
envelope is stored with `object_bbox_state=REVIEW` and cannot satisfy this gate. Workbook
integrations must iterate every selected/candidate evidence record or write a separate detail row;
indexing only `[0]` is forbidden because it silently destroys the audit trail.

Panel catalogs, merged/annotated catalogs, and stage manifests declare
`path_scope=local_run_diagnostics`. They may contain absolute source/image paths and must not be
committed or published. A public export requires deliberate redaction and source-relative paths.

### Component close-up manifest

`component-closeups` schema `1.0` consumes the CAD index, panel manifest, and component-frame
manifest. Each successful selection records its stable row/component key, source file and sheet,
the requested and rendered CAD bbox, related occurrence and measurement entity IDs, output image
path/SHA-256/natural pixel size, render profile, renderer, and evidence state. The manifest declares
`path_scope=local_run_diagnostics` because its source and output paths may be absolute.

The image must be rendered directly from the source DXF at the requested bounded region. An
existing raster crop must never be resampled to satisfy `target_px`. Missing source paths, missing
or invalid frames, unsupported sources, and render failures remain explicit diagnostics. Every
generated record starts as `REVIEW`; only a separate exact component/chain confirmation can promote
the evidence.

### Bounded component geometry manifest

`component-geometry` schema `1.0` consumes the CAD index and `component-closeups` manifest. For
each rendered model-space bbox it opens the immutable source DXF read-only and recursively expands
intersecting `INSERT` entities, including anonymous/dynamic and nested blocks. Supported world-
coordinate primitives are `LINE`, `ARC`, `CIRCLE`, `LWPOLYLINE`, `POLYLINE`, `SPLINE`, and
`ELLIPSE`.

Every primitive records the root insert handle, block-name path, original block-entity handle when
ezdxf preserves copy provenance, and a deterministic ordinal path when it does not. It also records
effective/source layer, bbox, endpoints, transformed geometry points or exact descriptors, length,
and whether length is exact or a flattening approximation with a declared drawing-unit tolerance.
Same-effective-layer endpoints may form connected-network candidates containing bbox width, bbox
height, and summed path length. Such a network may contain decorative or construction edges, so
all primitives and path values carry `state=REVIEW`, `measurement_role=null`, and cannot auto-fill
length, width, or quantity.

The policy stores per-region/global primitive caps, per-source entity cap, recursion-depth cap,
point and path caps, and both per-region and global truncation flags. A truncated probe is
incomplete, never negative evidence. The manifest is `path_scope=local_run_diagnostics`; its source
paths and component metadata must not be published.

### Geometry board manifest

`geometry-boards` schema `1.0` consumes matching `component-closeups` and
`component-geometry` manifests. Regions are joined only by stable `selection_key`,
`evidence_index`, source/sheet identity, and an equal CAD `render_bbox`; a missing, ambiguous, or
identity-mismatched close-up produces no board. Each successfully decoded source PNG is copied at
its natural pixel size and receives deterministic `G1`, `G2`, ... labels over selected primitive
or connected-path bboxes.

Each candidate retains its primitive or path ID, bbox and drawing-unit width/height, path length,
layer, exact/approximate length method, closed state, and provenance. Primitive provenance includes
top-level handle/ordinal, root insert, block path, and block-entity handle/ordinal. Path provenance contains
its primitive IDs and the available provenance of each member. A deterministic round-robin
interleaves prominent closed primitives, multi-primitive paths, top-level-handle primitives,
block-handle primitives, largest normalized bboxes, and candidates nearest the region center.
Each candidate records the category that selected it and every category it represents. This
balanced order prevents one candidate class from exhausting a capped board. Numbering is an audit
locator only and has no semantic role.

The manifest and every candidate are `REVIEW`-only and set `measurement_role=null`. Any incoming
non-null geometry role is ignored and diagnosed. Per-image candidate caps, total-board caps,
component-geometry truncation, unusable regions, missing images, and invalid identity links are
explicit gates. A partially rendered/truncated board is incomplete and cannot prove a negative or
promote length, width, quantity, component ownership, or PASS. Like its inputs, the manifest is
`path_scope=local_run_diagnostics` and may contain private absolute paths.

### Measurement board manifest

`measurement-boards` schema `1.0` overlays stable `D1`, `D2`, ... labels on each component
close-up. Every candidate retains its CAD entity ID/handle, entity type, raw/numeric value,
role/orientation hint, bbox or insertion point, units metadata, board pixel coordinate, and any
same-component measurement candidate IDs supplied by an optional takeoff payload. The board
record also retains source/board hashes, natural pixel size, sheet/source identity, and the render
bbox.

The manifest is `path_scope=local_run_diagnostics` and every board/candidate is REVIEW-only.
Candidate order and numeric equality have no semantic meaning. A selected candidate still needs
an exact physical-component and drawing-stage binding; a board cannot determine quantity or
promote a measurement to PASS.

### Lettered casework variant binding

`variant-bindings` emits schema `variant-title-physical-binding/1.0`. Its task rows contain only
row identity, name, material code, and candidate occurrence sheet IDs; H/J/K/L target keys are a
hard validation error. Each output row records eligibility, `binding_state`, REVIEW prediction,
reason codes, exact matched and rejected panels, parsed row variant, and role-wise native
dimension evidence down to source file, panel, viewport, entity ID, handle, bbox, value, axis,
units, and original entity ID.

A bound candidate requires one exact plan title for the row suffix, at least one matching
elevation title, material-code text inside every selected view, duplicate-handle plan depth,
plan/elevation width corroboration, and a repeated plausible elevation height. The resulting
`width*height*depth` expression is a candidate only. Opposite variants remain explicit negative
evidence, and the payload always has `production_eligible=false`.

### Screenshot-registration CAD bbox bridge

`image-match --panel-bbox x0 y0 x1 y1` can translate the registered screenshot corners from the
panel's natural pixel coordinates into an axis-aligned CAD bbox. Image Y is inverted during the
mapping because raster Y grows downward while CAD Y grows upward. The result records the natural
panel pixel size, unclipped/clipped pixel ranges, in-panel coverage, and reason codes. Partial
out-of-panel projections are clipped by default and low coverage is explicit; invalid, non-finite,
degenerate, or wholly outside projections produce no CAD bbox. Every produced bbox has
`state=REVIEW`: it is a development-label candidate only and never confirms a physical component
or promotes an image registration to `MATCH`.

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

## Derived measurement contract

A `MeasurementCandidate` created from reviewer arithmetic records `derived_expression`,
`value_expression`, ordered `source_candidate_ids`, all `source_sheet_ids`, and the union of
source `entity_ids`. Its stable ID includes the component, role, expressions, and symbol-to-source
bindings. `raw_value` is rendered from source-candidate values; `numeric_value` is computed by the
safe arithmetic evaluator.

The confirmation payload binds each symbolic term to an existing candidate. Dimension symbols
have one length dimension; quantity symbols have one count dimension. Addition/subtraction must
combine like dimensions, and the `value_expression` must resolve to one length/count dimension.
Only small integer multiplicities are accepted as free literals. Missing, cross-component,
wrong-role, wrong-unit, or out-of-chain inputs are BLOCK, not overrides.

## Audited engineering-quantity expression

`TakeoffItem` retains the unchanged 17-column business output plus three audit fields:
`engineering_quantity_expression`, `engineering_quantity_basis`, and
`engineering_quantity_evidence_ids`. They are used only when the visible width/length/quantity
columns cannot faithfully encode a CAD-proved billing axis or internal topology multiplier.

The expression must reference CAD-backed fields and match one controlled unit template. `m` has a
length-dimensional numerator and final divisor `1000`; `㎡` references both `width_mm` and
`length_mm`, has area dimension, and final divisor `1000000`; `件`/`套` reference `quantity` only.
Numerator topology factors are integers 1–100, there is no nested division, and `quantity` is at
most one positive multiplicative factor. A visible quantity other than one must participate.
Variable-free target values, decimal fitting coefficients, dimensional mixing, wrong conversion,
calls, attributes, arbitrary names, cell references, missing inputs, division by zero,
non-positive/non-finite/out-of-range intermediate or final values, excessive length/complexity, a
missing basis, or an empty evidence list are BLOCK.

Every evidence ID resolves to a CAD entity on the selected plan→elevation→detail chain (including
a same-drawing split panel already validated for a selected measurement). Takeoff emits one PASS or
REVIEW `component_to_engineering_quantity_evidence` edge per entity; its basis stores the exact
expression, exact explanation, source file, sheet, handle, and bbox. Portable export requires a
PASS edge matching `(component_id, entity_id, expression, basis)` exactly, so a stale edge cannot
authorize a changed formula. The exporter compiles the same AST into fixed I/J/K cell references,
uses the deterministic calculated value as the XLSX cache, and writes expression/basis/evidence
into the engineering-quantity cell comment and provenance sheet. It never accepts raw Excel syntax
or trusts a stale cached engineering quantity.

This field is not a target-value override. Without the audited structure the standard unit formula
remains authoritative, and unresolved billing semantics remain REVIEW/BLOCK.

## Estimator convention profile and candidate output

`convention-candidates` validates profile schema `estimator-convention-profile/v1`; see
`assets/estimator-convention-profile-schema-v1.json` and the deliberately synthetic
`assets/estimator-convention-profile-template.json`. A profile has a stable `profile_id`,
`profile_version`, lifecycle state, optional approval audit, external normalization families, and
one or more rules. Rules contain match conditions, an action, an independent lifecycle state, and
optional `required_measurement_roles`. Runtime validation rejects duplicate rule IDs, unsupported
schema versions, timezone-naive approval timestamps, an `APPROVED` profile without reviewer/time/
reason, and any action that attempts to write amount, unit price, price ID, or commercial status.

Output schema `estimator-convention-candidates/v1` stores the immutable profile identity, policy
flags, summary, and candidate records. Each candidate retains rule/component/row identity,
matched context, proposed fields, formula/calculation basis, the original profile suggestion,
symbol-to-measurement bindings, measurement candidate IDs, CAD entity IDs, sheet IDs,
confirmation basis, and review reasons. `state=CONFIRMED` describes only that convention
candidate; every record has `mutates_takeoff=false` and `commercial_effect=NONE`.

The engine understands component/name/room/material families, normalized area/linear/set pricing
bases, measurement roles and basis tokens, explicit arithmetic expressions, and existing takeoff
fields. Formula symbols are bound conservatively: `width_mm` to width, `length_mm` and
`governing_path_total_mm` to length, and `physical_quantity`/`aggregation_quantity` to a proved
quantity measurement. Conflicting numeric facts or takeoff values keep the candidate `REVIEW`.
Takeoff-item values may support a REVIEW suggestion but are not auditable entity evidence.

An action containing `candidate_quantity=1` with `write_field=false` remains a ranking suggestion,
not a quantity candidate. `outer_quantity=1` is emitted only when a bound measurement expression
contains an explicit multiplication operator. Area formulas use two role-labelled dimensions,
linear formulas require a governing path role, and set formulas use the proved physical assembly
count; the unit alone never supplies quantity.

Quantity evidence must also declare its multiplier semantics. At minimum distinguish
`physical_instance_count`, `paired_jamb_count`, `billable_face_count`, and
`surround_edge_count`; project-specific extensions remain external profile data. The physical
component count and an internal sides/faces multiplier are separate axes and must not be silently
collapsed or multiplied twice. A material annotation count supplies neither axis.

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

## Gold image asset manifest

`gold-image-export` writes manifest schema `1.0` next to a content-addressed `assets/`
directory. It is a read-only companion to `gold-import`; it does not replace or rewrite the
source workbook. Each `GoldImageAsset` contains:

- `sheet`, zero-based `sheet_index`, `cell`, one-based `row`/`column`, and optional `end_cell`;
- evidence `category` and `source_type` (`embedded` or `dispimg`);
- `formula_id` for `DISPIMG` references;
- SHA-256 of the original media bytes;
- normalized `package_path` inside the OOXML ZIP;
- `export_relative_path` below the requested output directory.

Assets are sorted deterministically. Identical media bytes with the same safe extension share a
content-addressed export path, while each workbook use remains a separate manifest entry. Unsafe
or ambiguous ZIP members/relationships are BLOCK issues and are never extracted. A legacy `.xls`
request produces `LEGACY_XLS_IMAGE_EXPORT_UNSUPPORTED`; absence of exported assets must not be
interpreted as absence of images in that workbook.

## Evaluation policy and report

`EvaluationPolicy` schema `1.0` is the canonical versioned acceptance contract. It contains
per-field enablement, text normalization, unfolded-expression mode, independent numeric relative
tolerances, explicit gold-zero handling, the optional exact amount rule, and the project target
accuracy. A `null` numeric tolerance is a pending business decision, not a wildcard or zero
tolerance. `source_evidence` is an unconditional evaluation result derived from non-empty
`TakeoffItem.evidence_ids`; it cannot be disabled by policy.

Evaluation report schema `2.0` writes the validated policy body, `policy_version`, and canonical
`policy_hash`. Each entry in `projects` contains eligible/correct row counts, replication recall,
output precision, missing/extra/duplicate diagnostics, field summaries, and row-level field
results. `overall_gate` is one of `PASS`, `FAIL`, `INDETERMINATE`, or `BLOCKED`. The aggregate block
does not supersede the individual project result. Legacy comparison metrics remain top-level for
backward-compatible diagnostics but do not control the versioned acceptance gate.

Evaluation batch manifest schema `1.0` contains a unique `batch_id`, an optional global policy and
legacy diagnostic tolerance, and a non-empty `projects` array. Every project requires a unique
`project_id`, prediction JSON path, and gold JSON path, and may override the policy/tolerance.
Paths are relative to the manifest unless absolute.

Batch summary schema `1.0` stores compact project summaries, gate counts, summed row counts,
micro/macro recall and precision, a manifest SHA-256, and links to the full per-project reports.
Its `overall_gate` is conservative: any `BLOCKED` project blocks the batch; otherwise an
`INDETERMINATE` project keeps the batch indeterminate, then any `FAIL` fails it. Only all-project
PASS yields batch PASS. Aggregate percentages are never an acceptance override.
