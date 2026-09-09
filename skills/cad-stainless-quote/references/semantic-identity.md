# Native view identity and semantic comparison

Use these standalone helpers when different descriptions may identify the same
physical item. They do not replace `evaluate`, create quotation rows, or prove
whole-project completeness.

## Discover a target view's native identity

`cadquote.native_view_identity.verify_native_view_identity(source_path,
layout=..., viewport_handle=..., expected_role=None)` reopens the original DXF.
The caller selects the viewport, not the desired page or local number. The
producer verifies its closed native paper frame and attached page identity;
local identity needs a same-parent numbered title, native circle/rule geometry,
and a unique bounded viewport owner. A source direction marker and a target
title number are different facts, even when their printed numbers coincide.

Use the viewport's actual native paper layout, not a render manifest's model
space. Keep original suffixes and raw text. Unknown, overlapping, hidden or
overflowing ownership stays REVIEW; do not enlarge a viewport or drop a suffix
to obtain acceptance. Inspect `field_states`, `proof`, `reason_codes` and source
hash as well as `state`. A whole-page caption may support some fields without
establishing a local number. It is not permission to invent one from a page tail.

VERIFIED here proves only the supported native view identity. It does not prove
that a particular component belongs to that view, that a cut crosses it, or that
its dimensions, material and quantity are correct.

## Compare independent structured claims

`cadquote.semantic_identity` defines `SemanticSidecar` and the diagnostic
`compare_semantic_sidecars`. Each side preserves its original artifact, raw
field text, source revisions, structured claims and review provenance. Match
rows by a unique validated native physical-instance set and quotation scope,
never row order, quantity similarity or arbitrary shared component labels.

Names distinguish kind, variant and billed subassembly; locations distinguish
source, building/floor partition, room, plan viewport and physical instances.
View identity distinguishes owning page, local view, viewport and role. Room
direction, native target title, physical geometry correspondence and a direct
cut/leader are separate relations. Missing individual direct-leader evidence
must not be described as proved by a room marker. Optional descriptive qualifiers
remain constraints: conflicting or unreviewed extra wording cannot be discarded.

The application must independently configure `TrustedReplay` with approved
per-side artifact hashes and a reader that derives facts from current native
evidence or explicit trusted reviews. The callback receives no desired subject
or expected page. No provider means unresolved. A candidate's own claim/hash is
not an approval source. Hashes prove freshness, not semantics or reviewer
authentication; document any model/human judgment trusted by the application.

Each claim needs an independent field-interpretation review plus its typed
evidence. Native title identity alone cannot supply component geometry or cut
ownership. Source-scoped aliases require explicit review; avoid global room
synonyms. A whole-page view requires complete native inventory proving a unique
content viewport and no local subviews, not merely a missing local number.

This diagnostic deliberately returns no whole-row acceptance or accuracy.
Retain the old policy and scores. Any later full-row assessment must separately
apply all enabled numeric/specification fields, validate the CAD evidence chain,
retain missing/extra rows and the entire project denominator, and label
post-freeze known-project normalization as development rather than unseen
validation. Freeze each new rule and sidecar before comparing it.
