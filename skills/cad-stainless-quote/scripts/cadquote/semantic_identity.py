"""Independent, source-bound semantic diagnostics; never a replacement 95% score.

File hashes prove byte identity/freshness, not semantic truth. Production callers
must explicitly provide a trusted replay callback, outside the supplied sidecars,
that reads approved independent CAD facts/reviews. The callback receives no desired
subject or expected page number. This module checks its actual returned subject.
An untrusted caller can also lie when configuring that trust boundary: this is not
reviewer authentication, CAD parsing, or automatic proof of human/model judgments.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, field_validator

Text = Annotated[str, StringConstraints(strict=True, strip_whitespace=True, min_length=1)]
Digest = Annotated[str, StringConstraints(strict=True, pattern=r"^[0-9a-f]{64}$")]
State = Literal["SUPPORTED", "CONFLICT", "UNRESOLVED"]
Side = Literal["predicted", "gold"]
Kind = Literal[
    "NATIVE_INSTANCE_IDENTITY",
    "NAME_CLASSIFICATION",
    "ROOM_LOCATION",
    "FIELD_INTERPRETATION",
    "SCOPED_ALIAS",
    "DIRECTION_FRAME",
    "NATIVE_ROOM_DIRECTION",
    "NATIVE_TITLE_VIEWPORT_OWNERSHIP",
    "NATIVE_WHOLE_PAGE_VIEW",
    "REVIEWED_NATIVE_GEOMETRY_CORRESPONDENCE",
    "DIRECT_NATIVE_COMPONENT_ROUTE",
    "VERIFIED_NATIVE_CUT_PATH",
]


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class ArtifactRef(Strict):
    path: Text
    sha256: Digest

    @field_validator("path")
    @classmethod
    def local_absolute_path(cls, value):
        if not Path(value).is_absolute() or value.startswith(("\\\\", "//")):
            raise ValueError("an absolute local file path is required; network paths are forbidden")
        return value


class Review(Strict):
    reviewer: Text
    reviewed_at: Text
    reason: Text

    @field_validator("reviewed_at")
    @classmethod
    def timezone_required(cls, value):
        stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if stamp.tzinfo is None or stamp.utcoffset() is None:
            raise ValueError("timezone-aware review time required")
        return value


class NativeInstance(Strict):
    source_sha256: Digest
    space: Text
    insert_chain: list[Text]
    entity_handle: Text
    minsert_cell: list[int] | None = Field(default=None, min_length=2, max_length=2)


class IdentityValue(Strict):
    project_id: Text
    instances: list[NativeInstance] = Field(min_length=1)
    quote_scope: Text


class NameValue(Strict):
    kind: Text
    variant: Text | None
    quote_scope: Text
    room_qualifier: Text | None = None


class LocationValue(Strict):
    project_id: Text
    building_id: Text
    level_id: Text
    room_id: Text
    plan_source_sha256: Digest
    plan_space: Text
    plan_page_code: Text
    plan_viewport_handle: Text
    instances: list[NativeInstance] = Field(min_length=1)
    # Values are canonical frame-qualified assertions, not free-form synonyms.
    direction_assertions: list[Text] = Field(default_factory=list)


class RoomDirection(Strict):
    source_sha256: Digest
    space: Text
    parent_handle: Text
    room_id: Text
    direction_token: Text
    page_code: Text


class ViewValue(Strict):
    role: Literal["ordinary_elevation", "product_elevation", "section", "detail"]
    source_sha256: Digest
    space: Text
    page_code: Text
    reference_kind: Literal["LOCAL_VIEW", "WHOLE_PAGE_VIEW"]
    local_view: Text | None
    viewport_handle: Text
    room_direction: RoomDirection | None = None


class ProofRef(Strict):
    kind: Kind
    artifact: ArtifactRef
    locator: Text


class Claim(Strict):
    raw_text: Text
    state: State = "UNRESOLVED"
    complete: bool = False
    candidates: int = Field(default=0, ge=0)
    review: Review
    proofs: list[ProofRef] = Field(default_factory=list)
    # An explicit registered alias is required when these terms are asserted.
    alias_ids: list[Text] = Field(default_factory=list)
    declared_conflicts: list[Text] = Field(default_factory=list)


class IdentityClaim(Claim):
    value: IdentityValue | None = None


class NameClaim(Claim):
    value: NameValue | None = None


class LocationClaim(Claim):
    value: LocationValue | None = None


class ViewClaim(Claim):
    value: ViewValue | None = None
    raw_reference_role: Literal["LOCAL_VIEW", "ROOM_DIRECTION", "PAGE_ONLY"] = "PAGE_ONLY"
    # These relations are not a ladder. Missing direct lead does not invalidate
    # separately proved room direction, target view, or geometry correspondence.
    direct_component_route: State = "UNRESOLVED"


class SemanticRow(Strict):
    row_id: Text  # Display/audit only; never a cross-side match key.
    identity: IdentityClaim
    name: NameClaim
    plan_location: LocationClaim
    elevation: ViewClaim
    detail: ViewClaim


class SemanticSidecar(Strict):
    schema_version: Literal["cad-native-semantic-claims/1"]
    side: Side
    original_artifact: ArtifactRef
    contract: ArtifactRef
    sources: list[ArtifactRef] = Field(min_length=1)
    rows: list[SemanticRow]


class ReplayResult(Strict):
    """Actual facts returned by the explicitly trusted, independent reader."""

    kind: Kind
    side: Side
    artifact_sha256: Digest
    original_artifact_sha256: Digest
    contract_sha256: Digest
    locator: Text
    source_sha256: list[Digest] = Field(min_length=1)
    subject: dict[str, Any]
    state: State = "UNRESOLVED"
    complete: bool = False
    candidates: int = Field(default=0, ge=0)
    review: Review


@dataclass(frozen=True)
class ReplayContext:
    side: str
    sources: tuple[ArtifactRef, ...]
    contract_sha256: str
    original_artifact: ArtifactRef


@dataclass(frozen=True)
class TrustedReplay:
    """Explicit application trust configuration, never read from a sidecar.

    Approved artifact hashes must be selected independently of the candidate.
    The replay reader must derive facts from its artifact/source, not echo the
    candidate. Registered review artifacts may contain trusted model judgments;
    the diagnostic explicitly reports that trust, not automatic authentication.
    """

    provider_id: str
    approved_artifacts: dict[str, frozenset[str]]
    replay: Callable[[ProofRef, ReplayContext], ReplayResult | dict]


def canonical_digest(value: Any) -> str:
    if isinstance(value, BaseModel):
        value = value.model_dump(mode="json")
    return hashlib.sha256(
        json.dumps(
            value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False
        ).encode("utf-8")
    ).hexdigest()


def _file_hash(path):
    result = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            result.update(chunk)
    return result.hexdigest()


def _asset_ok(asset):
    try:
        return Path(asset.path).is_file() and _file_hash(asset.path) == asset.sha256
    except OSError:
        return False


def _instance_set(instances):
    values = [canonical_digest(i) for i in instances]
    return tuple(sorted(values)) if len(values) == len(set(values)) else None


def _value_dict(value):
    output = value.model_dump(mode="json")
    if "instances" in output:
        output["instances"] = sorted(output["instances"], key=canonical_digest)
    if "direction_assertions" in output:
        output["direction_assertions"] = sorted(output["direction_assertions"])
    return output


def proof_subject(field: str, claim: Claim, kind: str, identity=None) -> dict:
    """Public schema helper, not a proof producer or trust decision."""
    value = _value_dict(claim.value) if claim.value is not None else None
    if kind == "NATIVE_ROOM_DIRECTION":
        value = value.get("room_direction") if value else None
    elif kind in {"NATIVE_TITLE_VIEWPORT_OWNERSHIP", "NATIVE_WHOLE_PAGE_VIEW"} and value:
        value.pop("room_direction", None)
    subject = {"field": field, "value": value}
    if kind not in {
        "NATIVE_ROOM_DIRECTION",
        "NATIVE_TITLE_VIEWPORT_OWNERSHIP",
        "NATIVE_WHOLE_PAGE_VIEW",
        "NATIVE_INSTANCE_IDENTITY",
    }:
        subject["identity"] = _value_dict(identity) if identity else None
    if kind == "NATIVE_WHOLE_PAGE_VIEW":
        subject["whole_page_conditions"] = {
            "complete_native_page_inventory": True,
            "content_viewport_count": 1,
            "local_subview_count": 0,
        }
    if kind == "FIELD_INTERPRETATION":
        subject.update(raw_text=claim.raw_text, alias_ids=claim.alias_ids)
        if isinstance(claim, ViewClaim):
            subject["raw_reference_role"] = claim.raw_reference_role
    if kind == "SCOPED_ALIAS":
        subject.update(raw_text=claim.raw_text, alias_ids=claim.alias_ids)
    return subject


def _source_hashes(value):
    output = set()
    if isinstance(value, BaseModel):
        value = value.model_dump(mode="json")
    if isinstance(value, dict):
        for key, child in value.items():
            if key in {"source_sha256", "plan_source_sha256"} and isinstance(child, str):
                output.add(child)
            else:
                output.update(_source_hashes(child))
    elif isinstance(value, list):
        for child in value:
            output.update(_source_hashes(child))
    return output


def _raw_view_conflicts(claim):
    """Recognize explicit references without silently deleting suffixes/notes.

    This catches narrow machine-verifiable contradictions only. All other raw
    text still needs a separately trusted FIELD_INTERPRETATION review.
    """
    if claim.value is None:
        return []
    value = claim.value
    text = claim.raw_text
    # No case, leading-zero or suffix deletion. Cosmetic punctuation can be
    # approved explicitly by the independent interpretation proof.
    page_pattern = r"(?<![A-Za-z0-9])(?:[A-Za-z0-9]+-)+[0-9]+[A-Za-z]*(?![A-Za-z0-9-])"
    pages = set(re.findall(page_pattern, text))
    issues = []
    allowed = {value.page_code}
    if value.room_direction:
        allowed.add(value.room_direction.page_code)
    if pages - allowed:
        issues.append("EXPLICIT_RAW_PAGE_CONFLICT")
    leading = re.match(r"\s*([0-9]+[A-Za-z]*)\s*/\s*", text)
    trailing = re.search(r"[（(]\s*([0-9]+[A-Za-z]*)\s*号\s*[）)]", text)
    tokens = {m.group(1) for m in (leading, trailing) if m}
    expected = (
        value.room_direction.direction_token
        if claim.raw_reference_role == "ROOM_DIRECTION" and value.room_direction
        else value.local_view
        if claim.raw_reference_role == "LOCAL_VIEW"
        else None
    )
    if tokens and (expected is None or tokens != {expected}):
        issues.append("EXPLICIT_RAW_REFERENCE_ROLE_OR_TOKEN_CONFLICT")
    return issues


def _required_proofs(field, claim):
    required = [{"FIELD_INTERPRETATION"}]
    if field == "identity":
        required.append({"NATIVE_INSTANCE_IDENTITY"})
    elif field == "name":
        required.append({"NAME_CLASSIFICATION"})
    elif field == "plan_location":
        required.append({"ROOM_LOCATION"})
        if claim.value and claim.value.direction_assertions:
            required.append({"DIRECTION_FRAME"})
    else:
        kind = (
            "NATIVE_WHOLE_PAGE_VIEW"
            if claim.value and claim.value.reference_kind == "WHOLE_PAGE_VIEW"
            else "NATIVE_TITLE_VIEWPORT_OWNERSHIP"
        )
        required.append({kind})
        if field == "detail":
            required.append({"VERIFIED_NATIVE_CUT_PATH"})
        else:
            required.append(
                {"DIRECT_NATIVE_COMPONENT_ROUTE", "REVIEWED_NATIVE_GEOMETRY_CORRESPONDENCE"}
            )
        if claim.value and claim.value.room_direction:
            required.append({"NATIVE_ROOM_DIRECTION"})
    if claim.alias_ids:
        required.append({"SCOPED_ALIAS"})
    return required


def _claim_check(field, claim, context, trusted, identity):
    conflicts = list(claim.declared_conflicts)
    issues = []
    if claim.state == "CONFLICT":
        conflicts.append("DECLARED_CONFLICT")
    if claim.state != "SUPPORTED" or not claim.complete or claim.candidates != 1:
        issues.append("CLAIM_NOT_UNIQUE_COMPLETE_SUPPORTED")
    if claim.value is None:
        issues.append("MISSING_STRUCTURED_VALUE")
    elif isinstance(claim.value, (IdentityValue, LocationValue)):
        if _instance_set(claim.value.instances) is None:
            issues.append("DUPLICATE_NATIVE_INSTANCE")
    if isinstance(claim, ViewClaim) and claim.value:
        conflicts.extend(_raw_view_conflicts(claim))
        value = claim.value
        if field == "elevation" and value.role not in {"ordinary_elevation", "product_elevation"}:
            conflicts.append("WRONG_VIEW_ROLE")
        if field == "detail" and value.role not in {"section", "detail"}:
            conflicts.append("WRONG_VIEW_ROLE")
        if value.reference_kind == "LOCAL_VIEW" and value.local_view is None:
            issues.append("LOCAL_VIEW_TOKEN_MISSING")
        if value.reference_kind == "WHOLE_PAGE_VIEW" and value.local_view is not None:
            conflicts.append("WHOLE_PAGE_CANNOT_DECLARE_LOCAL_TOKEN")
        if value.room_direction and value.room_direction.page_code != value.page_code:
            conflicts.append("ROOM_DIRECTION_TARGET_PAGE_CONFLICT")
    sources = {s.sha256 for s in context.sources}
    if not _source_hashes(claim.value).issubset(sources):
        issues.append("CLAIM_SOURCE_NOT_IN_CURRENT_SIDECAR")
    proofs = []
    successful = set()
    for proof in claim.proofs:
        expected = proof_subject(field, claim, proof.kind, identity)
        row = {
            "kind": proof.kind,
            "artifact": proof.artifact.model_dump(),
            "locator": proof.locator,
            "state": "UNRESOLVED",
        }
        proofs.append(row)
        if not _asset_ok(proof.artifact):
            row["reason"] = "PROOF_ARTIFACT_HASH_MISMATCH_OR_MISSING"
            issues.append(row["reason"])
            continue
        if (
            not trusted
            or not trusted.provider_id
            or proof.artifact.sha256
            not in (trusted.approved_artifacts.get(context.side, frozenset()))
        ):
            row["reason"] = "NO_EXPLICIT_TRUSTED_REPLAY_FOR_ARTIFACT"
            issues.append(row["reason"])
            continue
        try:
            actual = ReplayResult.model_validate(trusted.replay(proof, context))
        except Exception as exc:
            row["reason"] = f"REPLAY_FAILED:{type(exc).__name__}"
            issues.append(row["reason"])
            continue
        row["replay"] = actual.model_dump(mode="json")
        if (
            (actual.kind, actual.side, actual.artifact_sha256, actual.locator)
            != (proof.kind, context.side, proof.artifact.sha256, proof.locator)
            or not set(actual.source_sha256).issubset(sources)
            or (
                actual.original_artifact_sha256 != context.original_artifact.sha256
                or actual.contract_sha256 != context.contract_sha256
            )
        ):
            row["reason"] = "REPLAY_PROVENANCE_MISMATCH"
            issues.append(row["reason"])
        elif not _source_hashes(expected).issubset(set(actual.source_sha256)):
            row["reason"] = "REPLAY_DOES_NOT_COVER_CLAIM_SOURCES"
            issues.append(row["reason"])
        elif actual.state == "CONFLICT":
            row["state"] = "CONFLICT"
            conflicts.append("TRUSTED_REPLAY_CONFLICT")
        elif actual.state != "SUPPORTED" or not actual.complete or actual.candidates != 1:
            row["reason"] = "REPLAY_NOT_UNIQUE_COMPLETE_SUPPORTED"
            issues.append(row["reason"])
        elif canonical_digest(actual.subject) != canonical_digest(expected):
            row["state"] = "CONFLICT"
            conflicts.append("CLAIM_DIFFERS_FROM_TRUSTED_REPLAY")
        else:
            row["state"] = "SUPPORTED"
            successful.add(proof.kind)
    for alternatives in _required_proofs(field, claim):
        if not alternatives & successful:
            issues.append("MISSING_PROOF:" + "|".join(sorted(alternatives)))
    state = "CONFLICT" if conflicts else "UNRESOLVED" if issues else "SUPPORTED"
    return {
        "state": state,
        "issues": sorted(set(issues)),
        "conflicts": sorted(set(conflicts)),
        "proofs": proofs,
        "direct_component_route": getattr(claim, "direct_component_route", None),
    }


def validate_sidecar(value, *, expected_side, contract, trusted_replay=None):
    sidecar = SemanticSidecar.model_validate(value)
    contract = ArtifactRef.model_validate(contract)
    if sidecar.side != expected_side:
        raise ValueError("wrong semantic side")
    context = ReplayContext(
        sidecar.side, tuple(sidecar.sources), contract.sha256, sidecar.original_artifact
    )
    assets = [contract, *_sidecar_assets(sidecar)]
    envelope_issues = []
    if sidecar.contract.sha256 != contract.sha256:
        envelope_issues.append("CONTRACT_MISMATCH")
    if len({r.row_id for r in sidecar.rows}) != len(sidecar.rows):
        envelope_issues.append("DUPLICATE_LOCAL_ROW_ID")
    if len({s.sha256 for s in sidecar.sources}) != len(sidecar.sources):
        envelope_issues.append("DUPLICATE_SOURCE_REVISION")
    if any(not _asset_ok(asset) for asset in assets):
        envelope_issues.append("INPUT_ASSET_HASH_MISMATCH_OR_MISSING")
    rows = []
    for row in sidecar.rows:
        fields = {
            f: _claim_check(f, getattr(row, f), context, trusted_replay, row.identity.value)
            for f in _FIELDS
        }
        identity, name, location = row.identity.value, row.name.value, row.plan_location.value
        if (
            identity
            and location
            and (
                identity.project_id != location.project_id
                or _instance_set(identity.instances) != _instance_set(location.instances)
            )
        ):
            fields["plan_location"]["state"] = "CONFLICT"
            fields["plan_location"]["conflicts"].append("ROW_IDENTITY_LOCATION_CONFLICT")
        if identity and name and identity.quote_scope != name.quote_scope:
            fields["name"]["state"] = "CONFLICT"
            fields["name"]["conflicts"].append("ROW_QUOTE_SCOPE_CONFLICT")
        if name and location and name.room_qualifier not in {None, location.room_id}:
            fields["name"]["state"] = "CONFLICT"
            fields["name"]["conflicts"].append("NAME_ROOM_QUALIFIER_CONFLICT")
        for field in ("elevation", "detail"):
            value = getattr(row, field).value
            if value and value.room_direction and location:
                if value.room_direction.room_id != location.room_id:
                    fields[field]["state"] = "CONFLICT"
                    fields[field]["conflicts"].append("ROOM_DIRECTION_LOCATION_CONFLICT")
        rows.append({"row_id": row.row_id, "fields": fields})
    if any(not _asset_ok(asset) for asset in assets):
        envelope_issues.append("INPUT_ASSET_CHANGED_DURING_REPLAY")
    if envelope_issues:
        for row in rows:
            for field in row["fields"].values():
                field["state"] = "UNRESOLVED"
                field["issues"].extend(sorted(set(envelope_issues)))
    return {
        "schema_version": "cad-native-semantic-validation/1",
        "side": sidecar.side,
        "sidecar_sha256": canonical_digest(sidecar),
        "contract_sha256": contract.sha256,
        "trust_provider": trusted_replay.provider_id if trusted_replay else None,
        "trust_boundary": (
            "Explicit application-approved replay/review, not reviewer authentication"
        ),
        "envelope_issues": sorted(set(envelope_issues)),
        "rows": rows,
    }


_FIELDS = ("identity", "name", "plan_location", "elevation", "detail")


def _sidecar_assets(sidecar):
    assets = [sidecar.original_artifact, sidecar.contract, *sidecar.sources]
    assets.extend(p.artifact for r in sidecar.rows for f in _FIELDS for p in getattr(r, f).proofs)
    return assets


def compare_typed_values(left, right):
    """Exact typed diagnostic, not row pairing and not evidence validation."""
    if left is None or right is None:
        return "UNRESOLVED"
    if type(left) is not type(right):
        return "FAIL"
    if isinstance(left, ViewValue):
        a, b = _value_dict(left), _value_dict(right)
        a.pop("room_direction")
        b.pop("room_direction")
        if a["source_sha256"] != b["source_sha256"]:
            return "UNRESOLVED"
        return "PASS" if a == b else "FAIL"
    if _source_hashes(left) != _source_hashes(right):
        return "UNRESOLVED"  # Revision/file bridge is deliberately not inferred.
    if isinstance(left, NameValue):
        # Optional room prefix is checked against the independently validated
        # row location, not required to appear in both display names.
        a, b = _value_dict(left), _value_dict(right)
        a.pop("room_qualifier")
        b.pop("room_qualifier")
        return "PASS" if a == b else "FAIL"
    if isinstance(left, LocationValue):
        # Frame-qualified supplemental assertions require independent replay in
        # validate_sidecar; they are not mandatory display verbosity. A wrong
        # assertion conflicts with that replay, not fuzzy direction matching.
        a, b = _value_dict(left), _value_dict(right)
        a.pop("direction_assertions")
        b.pop("direction_assertions")
        return "PASS" if a == b else "FAIL"
    return "PASS" if _value_dict(left) == _value_dict(right) else "FAIL"


def compare_semantic_sidecars(predicted, gold, *, contract, trusted_replay=None):
    """Compare independently validated exact native identities, never amounts/order.

    No overall accuracy, whole-row acceptance, or stage-N/A decisions are made.
    Missing/incomplete gold rows remain visible in this separate diagnostic.
    """
    sides = {
        "predicted": SemanticSidecar.model_validate(predicted),
        "gold": SemanticSidecar.model_validate(gold),
    }
    audits = {
        side: validate_sidecar(
            value, expected_side=side, contract=contract, trusted_replay=trusted_replay
        )
        for side, value in sides.items()
    }
    # A callback for the second side must not mutate already-validated inputs
    # on the first side and retain their formerly successful proof status.
    for side, value in sides.items():
        if any(not _asset_ok(asset) for asset in _sidecar_assets(value)):
            issue = "INPUT_ASSET_CHANGED_DURING_COMPARISON"
            audits[side]["envelope_issues"].append(issue)
            for row in audits[side]["rows"]:
                for field in row["fields"].values():
                    field["state"] = "UNRESOLVED"
                    field["issues"].append(issue)
    groups = {}
    for side, value in sides.items():
        mapping = defaultdict(list)
        for index, row in enumerate(value.rows):
            if audits[side]["rows"][index]["fields"]["identity"]["state"] == "SUPPORTED":
                mapping[canonical_digest(_value_dict(row.identity.value))].append(index)
        groups[side] = mapping
    matches, matched = [], {"predicted": set(), "gold": set()}
    duplicates = []
    for key in sorted(set(groups["predicted"]) | set(groups["gold"])):
        indices = {side: groups[side].get(key, []) for side in sides}
        if any(len(values) > 1 for values in indices.values()):
            duplicates.append({"native_identity_digest": key, "indices": indices})
            continue
        if not all(len(values) == 1 for values in indices.values()):
            continue
        pi, gi = indices["predicted"][0], indices["gold"][0]
        fields = {}
        for field in _FIELDS[1:]:
            pa = audits["predicted"]["rows"][pi]["fields"][field]
            ga = audits["gold"]["rows"][gi]["fields"][field]
            states = {pa["state"], ga["state"]}
            status = (
                "FAIL"
                if "CONFLICT" in states
                else "UNRESOLVED"
                if states != {"SUPPORTED"}
                else compare_typed_values(
                    getattr(sides["predicted"].rows[pi], field).value,
                    getattr(sides["gold"].rows[gi], field).value,
                )
            )
            if field == "name":
                names = [sides["predicted"].rows[pi].name.value, sides["gold"].rows[gi].name.value]
                qualifiers = {name.room_qualifier for name in names if name and name.room_qualifier}
                if qualifiers:
                    locations = [
                        sides["predicted"].rows[pi].plan_location.value,
                        sides["gold"].rows[gi].plan_location.value,
                    ]
                    if any(loc and any(q != loc.room_id for q in qualifiers) for loc in locations):
                        status = "FAIL"
                    elif any(
                        audits[side]["rows"][i]["fields"]["plan_location"]["state"] != "SUPPORTED"
                        for side, i in (("predicted", pi), ("gold", gi))
                    ):
                        status = "UNRESOLVED" if status != "FAIL" else status
            fields[field] = {
                "status": status,
                "predicted_evidence_state": pa["state"],
                "gold_evidence_state": ga["state"],
            }
        matches.append(
            {
                "predicted_row": sides["predicted"].rows[pi].row_id,
                "gold_row": sides["gold"].rows[gi].row_id,
                "match_basis": "EXACT_VALIDATED_NATIVE_INSTANCE_SET_AND_QUOTE_SCOPE",
                "native_identity_digest": key,
                "fields": fields,
            }
        )
        matched["predicted"].add(pi)
        matched["gold"].add(gi)
    return {
        "schema_version": "cad-native-semantic-diagnostic/1",
        "accuracy_claim": None,
        "whole_row_acceptance": None,
        "legacy_policy_modified": False,
        "matching_uses_quantities_or_row_order": False,
        "validation": audits,
        "matches": matches,
        "duplicates": duplicates,
        "unmatched": {
            side: [r.row_id for i, r in enumerate(v.rows) if i not in matched[side]]
            for side, v in sides.items()
        },
        "limitations": [
            "Semantic review trust is explicitly supplied, not authenticated",
            "Exact native view identity is distinct from an individual direct route",
            "Numeric/spec rules, full-row scoring and full-project coverage are external",
        ],
    }
