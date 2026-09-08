# Numbered detail navigation

This stage separates a local detail number from its containing drawing page.
It uses only CAD inputs and produces navigation candidates, not accepted quote rows.

```powershell
scripts/run.ps1 detail-routes run/index/cad_index.json --panels run/analysis/panels.json --out review/detail-routes.json --probe-native
```

The flow is: elevation callout → exact target page → numbered detail title and
reciprocal elevation reference → viewport candidate → optional native material
label-border/leader contact → nearby straight model-polyline candidates.

- Titles omitted by viewport clipping are recovered from original paper-space attributes.
- A title's immediate vertical-column viewport association is a framing candidate,
  not proof of physical component identity. Alternatives and conflicts are retained.
- Native title-block containment can supply page-code candidates without rewriting
  a legacy panel's drawing number or classification.
- `UNIQUE_RECIPROCAL_CANDIDATE`, `AMBIGUOUS_RECIPROCAL`, `PAGE_ONLY`, and
  `TARGET_NOT_FOUND` describe numbered-reference navigation only. Bare, unnumbered,
  or unsupported references are outside this stage, so the counts are not recall.
- Same-material labels remain separate. No MT count is used as physical quantity.
- Native probing checks indexed source hashes; default cap is 20 unique node
  candidates. Caps, unsupported paths and source errors remain explicit.
- Straight, top-level model polylines are currently supported; nested and curved
  profiles are not covered. Raw boundary length is not unfolded width.
- Native viewport inverse projection is distinct from the legacy shifted bbox.
  Source units are retained, never silently changed to millimetres.

Full `run` writes `analysis/detail_routes.json` automatically and returns its path.
`resume` exposes that existing snapshot without recomputing it. Old runs can use
the command above for an explicit new output; they are not silently backfilled.
The optional native probe is explicit, not part of the default full run.

`link ... --detail-routes-out review/routes.json` additionally annotates existing
relation candidates with route IDs, without changing confidence or PASS state.
All pricing, measurement, physical binding and screenshot confirmation gates remain.

`index-directions` now includes a same-parent custom-attribute/five-vertex chevron
family, in addition to circular curved-base hatches. Unsupported or competing
arrows remain unresolved; a resolved arrow vector is not a confirmed view binding.

Tests use synthetic CAD only. Runtime manifests and drawing images are private;
none belong in this repository. Navigation success is not end-to-end accuracy or
evidence of a particular percentage of human time saved.

## Native frame context and reciprocal plan navigation

Use `detail-routes index.json --panels panels.json --out routes.json
--refresh-native-frames --review-out routes.md` to revisit immutable native
paper frames and produce a human-readable ledger. The original index is not
overwritten. Frame recovery uses source hashes and same-layout native handles,
not the INSERT origin or a nearby text guess. Only catalogued/referenced page
codes are in this refresh scope; source/frame caps and errors are explicit.

Numbered view headings are classified separately from outgoing callouts. Their
page/view reference is resolved against reciprocal plan index attributes, not
against a numbered detail title. `UNIQUE_PLAN_INDEX_CANDIDATE` and
`AMBIGUOUS_PLAN_INDEX` never establish physical ownership or quantity. Duplicate
plan bubbles, conflicting back-references and all page-only results remain in
the ledger. The original drawing number and recovered page code are both kept.

Full `run` now refreshes native frame context for its navigation snapshot. The
optional `link` annotation output remains indexed-only. Native material probes
remain opt-in. A node can span multiple broken viewports; one matching title
and viewport does not establish its complete visual or physical scope.

Paper rendering filters viewport footprints using native paper rectangles and
shares a bounding-box cache with the drawing frontend. It still preserves
unsupported/proxy diagnostics, and image rendering is not screenshot approval.

## Broken-view groups and native group evidence

`detail-routes` and full `run` now add a separate `node_view_groups` catalogue.
It groups only collinear, close-gap, same-frame viewports with consistent
top-view scale and transverse model alignment. Paper and model gaps remain
separate diagnostics; neither is an unfolded dimension. Inactive, unsupported,
overlapping or multiply framed viewports cannot silently establish complete scope.

Below-view and bottom-right numbered captions compete in a bounded bipartite
matching. A connected component must have exactly one complete caption/group
assignment; missing, duplicate, ambiguous or capped matches remain unresolved.
The original single-viewport navigation ledger is preserved. Group navigation
can flag a formerly selected viewport as unresolved: this is not a reason to
fall back silently or to report the two ledgers as cumulative matches.

`--probe-native` also probes unique reciprocal groups. Material annotation
search uses the native containing page, but exact leader tip membership routes
each branch through exactly one native viewport inverse matrix. Source hashes,
caps, raw units, visibility and unsupported geometry remain explicit. Paper and
model dimensions are extension-point-owned candidates, with no automatic length,
unfolded-width or physical-count role. Repeated view fragments are not pieces.

Add `--group-images-dir review/group-images` to export the successfully probed
groups directly from the CAD source. This requires `--probe-native`. Each image
records native viewport selection, explicitly excluded neighbouring numbered
title handles, source/image hashes, natural pixel size and REVIEW state. Images
retain other context annotations and native renderer/proxy limitations; they
are not verified quotation screenshots. Exporting a group never approves quantity.

```powershell
scripts/run.ps1 detail-routes run/index/cad_index.json --panels run/analysis/panels.json --out review/routes.json --refresh-native-frames --probe-native --max-native-nodes 20 --review-out review/routes.md --group-images-dir review/group-images
```

Native reads/probes and optional image export are bounded separately. Full `run`
adds the indexed group catalogue, not automatic probes/images or quote acceptance.
The installed personal skill copy is not upgraded by modifying this repository.
If native context, probing or image export is incomplete, `detail-routes` now
reports `REVIEW_INCOMPLETE` and exits with code 2. Inspect the retained issues;
an inaccessible source is not evidence that its drawings or components are absent.

## Dimension-supported profile hypothesis

Native group probing now includes `dimension_supported_profiles`. Within each
leader's exact viewport it checks open straight polyline skins for constant
signed offset, matching turns and full polygon reconstruction. A unique pair
must have one uniquely supported skin, backed by native dimension endpoints in
at least two nonparallel directions. Projection is allowed only when both
extension points are vertices of that same skin. Equal values, nearest labels
and a matching human answer never supply ownership.

Conflicting text overrides, non-unit DIMLFAC, rounding, competing skins,
multiple contacted boundaries, clipped paths and scan truncation cannot select
a nominal boundary. Model dimensions must retain the same source/group/viewport
identity. The output preserves the contact skin, both raw lengths, normal
separation, dimension handles, segment bindings and undimensioned segments.
It remains REVIEW and does not fill a takeoff field: unknown units, physical
count, billable run and manufacturing bend allowance are separate questions.
