# Physical component and measurement-role binding

Use this protocol when the CAD index contains the right numbers but the row, component, or
measurement role is ambiguous. It is a model-review procedure over deterministic manifests; it
does not permit target values, workbook screenshots, or row order to leak into a blind prediction.

## 1. Discover components before calculating rows

Traverse every relevant plan/elevation sheet and enumerate physical metal components from CAD
evidence. Do not begin from a desired output count. One candidate component requires a bounded
geometry region plus at least one of:

- a material/MT leader whose target lands inside the region;
- a material description in the same closed subview and an associated leader;
- an explicit plan/elevation reference tied to the room and component;
- an approved negative-detail disposition plus component-bound elevation dimensions.

Keep material legends, title blocks, duplicated viewport annotations, and repeated MT text outside
physical-component counts. Deduplicate the same component across plan and elevation by evidence
edges, not by material code alone.

## 2. Select the physical region

For each row/component candidate, inspect a numbered CAD board that shows the whole subview and
readable local alternatives. Use this evidence order:

1. leader target inside compatible visible geometry;
2. room/subview title and explicit drawing reference;
3. component-family shape (panel, niche, door leaf, surround, linear trim, railing, counter, and so on);
4. closed-frame topology and native dimension endpoints;
5. material text proximity.

An explicit MT occurrence does not win when its target lands on the wrong physical object. One
material note may feed several leaders, and one leader neighborhood may contain a door leaf,
surround, wall panel, and trim. Keep those objects separate.

If no material anchor exists but the reported or discovered sheet is known, do not silently drop
the row. Render the whole subview in overlapping, readable tiles and use closed geometry/native
dimension clusters as REVIEW candidates. A sheet tile is a retrieval fallback, not confirmation.

When two same-name rows compete for two objects, solve an all-different assignment using component
geometry and room/subview evidence. Never assign by Excel sequence, lexical order, or “first
candidate”. If no discriminator exists, leave both REVIEW.

## 3. Classify specification H before selecting numbers

First classify the specification grammar; the same column can contain different meanings:

- `FOLD_CHAIN`: additive transverse profile from a detail/section;
- `PROJECTED_SIZE_2D`: width × height, with one axis used for area;
- `PROJECTED_SIZE_3D`: width × height × depth, with explicit `value_expression` selecting the billed axis;
- `LINEAR_SECTION`: one transverse width for linear pricing;
- `ASSEMBLY_ENVELOPE`: overall product dimensions, not a sheet-metal unfold;
- `UNKNOWN`: insufficient evidence, therefore REVIEW.

Preserve `expression` (what the workbook should display) separately from `value_expression` (the
numeric axis used by the calculation). Do not turn nearby small dimensions into a fold chain unless
they are connected in order on the selected profile. Do not manufacture return allowances or round
CAD dimensions unless an approved external estimator-convention profile explicitly supplies that
policy.

## 4. Bind length J by component topology

The governing axis depends on the physical family:

- panel/door leaf: selected closed frame width or height required by the pricing grammar;
- surround/door casing: audited three-edge or four-edge path, or a separately dimensioned repeated jamb;
- skirting/linear trim: continuous centerline/perimeter with opening deductions stated explicitly;
- railing/window sill: sum of the selected contiguous runs, including slopes where applicable;
- repeated panels/shelves: one-piece governing length plus a separate multiplier;
- curved work: native arc/spline/path length with flattening tolerance recorded.

The largest local DIM, closest DIM, or visually longest line is not a universal J rule. Every term of
an aggregate must bind to a current component entity/handle; unknown opening deductions keep the
result REVIEW.

## 5. Bind quantity K as a typed multiplier

Quantity is not MT occurrence count and is not always whole-assembly count. Record one semantic
type for the selected multiplier:

- `physical_instance_count`;
- `paired_jamb_count`;
- `billable_face_count`;
- `surround_edge_count`;
- another versioned external-profile type.

Use explicit QTY text first, then independent repeated leaders/congruent component frames, then
audited component topology. A single visible assembly does not prove `1`. If an internal repeat is
already present in H or J, do not multiply it again in K.

## 6. Evidence images

Generate images only after choosing a component candidate. Retain:

- a locator showing room/subview context;
- a component close-up containing the material leader and object boundary;
- a separate detail/section image when H uses a fold profile;
- numbered native DIM/geometry overlays whose labels map to exact handles.

Do not use a photo/rendering as a CAD detail, infer image meaning from its Excel column, reuse one
crop for distinct stages, or enlarge a low-resolution raster. A missing stage remains visible as
MISSING/REVIEW.

## 7. Freeze and score

For development or acceptance tests:

1. hash the target-free task pack and CAD-only visual assets;
2. freeze predictions with `numeric_gold_accessed=false`, `human_screenshots_accessed=false`, and
   `heldout_accessed=false`;
3. only then let an independent scorer read development gold;
4. report candidate recall, automatic-output precision, full-row recall, extra rows, and missing rows;
5. require both full-row precision and recall to meet the project gate.

Candidate recall, teacher-forced accuracy, and accuracy among a small AUTO subset are diagnostics,
not end-to-end CAD-to-workbook accuracy.
