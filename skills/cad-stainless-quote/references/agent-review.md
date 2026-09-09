# CAD-only semantic review with Codex

Use this mode when the user wants reusable CAD-only takeoff, not merely a review
of supplied human rows. The deterministic run finds candidates; Codex still has
to resolve physical identity, measurement roles and counting. There is no claim
that `run` or `resume` executes these decisions unattended.

## Start from the drawing scope

Run `scripts/run.ps1 run <cad-input> --out <new-run> --no-workbook`.
`--no-render` optionally postpones occurrence images until better framing exists.
Declare the intended project/floor/component scope separately from the complete
source inventory. Keep unknown floor/classification and out-of-scope decisions
visible; a material-code-only queue does not establish full stainless coverage.

Use `drawing_catalog` and the verified `detail_route_context` to navigate. The
route context includes every candidate, not a selected route. Distinguish a
material code, owning sheet number, local view number and back-reference. A
common material or title cannot justify a cross-floor relationship.

## Make the semantic decisions, then calculate

- Start with CAD-derived annotations, subview titles and physical geometry, not
  human-row IDs or a stored list of desired coordinates. Runtime-discovered CAD
  handles and bboxes are valid evidence outputs.
- Use candidate/panel boards to inspect all relevant views. Select physical
  component instances and separate repeated views from repeated objects. Keep
  competing selections and contrary evidence in the audit.
- Use native dimensions, endpoint geometry and the selected node profile to
  assign roles. The nearest or numerically largest dimension is not necessarily
  the governing one. A geometric outer envelope is not automatically unfolded
  metal, and a readable dimension alone does not establish its material.
- Apply the supplied estimating policy to the confirmed object class. Do not
  demand irrelevant manufacturing parameters for a projection-based takeoff;
  do not switch an unresolved unfolded-area item to projection to fill a blank.
- Record Codex's choices as model-reviewed, not as staff/user approval. Use the
  existing component/stage evidence and confirmation schemas; missing business
  authority remains missing even if Codex can interpret the geometry.
- Reuse `resume <run> --confirmations <json> --no-workbook` for exact arithmetic
  and validation after review. This reuses snapshots, so changes to discovery
  or linking code need a new run or explicitly regenerated stage, not merely a
  resume of old candidate edges.

For high-fidelity evidence, the `cadquote.native_paper_render` API overlays native
paper DIMENSION/text/leader entities onto the bounded model render. Keep its
transform and source/image hashes. Annotated browsing boards are useful guides,
not replacements for original drawing annotations in final proof images.

### Per-set casework and evidence-role consistency

When the estimating scope explicitly permits a per-set stainless subassembly,
record named `width`, `body_height` and `depth` roles and the displayed axis
order. The whole cabinet's nominal envelope can identify the set without
claiming that every face is steel. List included steel and excluded materials;
keep an independently identified overhead frame or fixture separate. This is
not permission to convert an unresolved area-priced item into a per-set item.

Count the bound site instances, not the product's plan/front/back/section views.
Keep the ordinary room elevation as the site locator and dedicated product
views as dimensional evidence. A local view number, its owning sheet and its
back-reference can differ; preserve their roles instead of overwriting one
with another. A shared side view does not prove that two variants share every
section or dimension.

After changing a selected node, recheck every primary and corroborating
measurement's source sheet, viewport, handle and role. A correct stage-chain
summary cannot repair a stale nested measurement record. Equal numerical values
in another variant are not corroboration. Retain replaced candidates separately
and freeze a new prediction version; do not rewrite the old freeze after scoring.

## Freeze, score and deliver

Freeze predictions and their CAD/code/policy/evidence provenance before opening
the human answer table. In development, disclose prior answer exposure even if
the current process has no workbook input. Replaying a known project is not an
unseen test. Repository devtools may check file contracts and repeatability;
they neither attest isolation nor prove accuracy.

Report full-row matches, omissions, extras and unresolved rows under the approved
policy. Price/amount absence must not be confused with a quantity error when the
user requested takeoff only. Conversely, excluding prices from evaluation does
not grant commercial PASS. An empty or easy-only output cannot satisfy a whole
project goal.

Use the available spreadsheet workflow to produce the reviewed, traceable
workbook with locator/close-up/detail images; retain their natural pixels and
aspect ratio. A successful test suite, candidate route or image export is an
engineering milestone, not a newly correct takeoff row.
