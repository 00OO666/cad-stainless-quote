"""Validate view applicability against scorer-supplied, side-local CAD facts.

The context must be built independently from the current CAD index and reviewed
stage selections. It is not authenticated by a self-declared hash and must not
be copied from a gold row into a prediction. This module checks the contract; it
does not inspect CAD files, certify a reviewer, or promote commercial status.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from typing import Any, Literal

from pydantic import Field, ValidationError

from .models import NonEmptyString, Sha256String, StrictModel, TakeoffItem, ViewDisposition

VIEW_FIELDS = ("elevation", "detail")


class DispositionView(StrictModel):
    id: NonEmptyString
    component_id: NonEmptyString
    role: Literal["plan", "elevation", "section", "detail"]
    sheet_id: NonEmptyString
    source_id: NonEmptyString
    state: Literal["CONFIRMED", "REVIEW", "BLOCK"]
    evidence_ids: list[NonEmptyString] = Field(min_length=1)


class DispositionEntity(StrictModel):
    id: NonEmptyString
    component_id: NonEmptyString
    sheet_id: NonEmptyString
    source_id: NonEmptyString
    entity_type: NonEmptyString


class DispositionConnection(StrictModel):
    id: NonEmptyString
    component_id: NonEmptyString
    source_view_id: NonEmptyString
    target_view_id: NonEmptyString
    state: Literal["CONFIRMED", "REVIEW", "BLOCK"]
    evidence_ids: list[NonEmptyString] = Field(min_length=1)


class DispositionSearch(StrictModel):
    id: NonEmptyString
    component_id: NonEmptyString
    excluded_stage: Literal["elevation", "detail"]
    searched_sheet_ids: list[NonEmptyString] = Field(min_length=1)
    complete: bool = Field(strict=True)
    # No defaults: omission must not imply that a search found no conflicts.
    unresolved_reference_ids: list[NonEmptyString]
    conflicting_view_ids: list[NonEmptyString]


class ViewDispositionContext(StrictModel):
    schema_version: Literal["view-disposition-context/1"]
    side: Literal["predicted", "gold"]
    index_sha256: Sha256String
    source_sha256: dict[NonEmptyString, Sha256String] = Field(min_length=1)
    # These are the CURRENT SELECTED views, not an unfiltered candidate catalog.
    views: list[DispositionView]
    entities: list[DispositionEntity]
    connections: list[DispositionConnection]
    searches: list[DispositionSearch]


@dataclass(frozen=True)
class PreparedViewContext:
    context: ViewDispositionContext | None
    error: str | None = None


@dataclass(frozen=True)
class ViewApplicability:
    state: Literal["APPLICABLE", "NOT_APPLICABLE", "INVALID", "MISSING"]
    reason: str


def prepare_view_context(value: Any, side: str) -> PreparedViewContext:
    if value is None:
        return PreparedViewContext(None, "view_context_missing")
    try:
        # Revalidate instances too: model_copy/update is not a validation boundary.
        raw = value.model_dump(mode="json") if isinstance(value, StrictModel) else value
        context = ViewDispositionContext.model_validate(raw)
    except (ValidationError, TypeError, ValueError):
        return PreparedViewContext(None, "view_context_invalid")
    if context.side != side:
        return PreparedViewContext(None, "view_context_side_mismatch")
    for records in (context.views, context.entities, context.connections, context.searches):
        identifiers = [record.id for record in records]
        if len(set(identifiers)) != len(identifiers):
            return PreparedViewContext(None, "view_context_duplicate_identity")
    return PreparedViewContext(context)


def has_source_evidence(item: TakeoffItem) -> bool:
    return bool(item.evidence_ids) and all(
        isinstance(value, str) and bool(value.strip()) for value in item.evidence_ids
    )


def is_view_placeholder(value: Any) -> bool:
    """Sentinel text is never drawing evidence, including annotated N/A strings."""

    if not isinstance(value, str):
        return False
    text = unicodedata.normalize("NFKC", value).strip().casefold()
    if text in {"-", "—", "–", "none", "null", "nil", "无", "不适用", "未提供", "未知"}:
        return True
    return bool(
        re.match(r"^(?:n\s*/?\s*a|not[\s_-]*applicable)(?:$|\b|[:：—(-])", text)
        or re.match(r"^无(?:$|[\s(:：,，—-])", text)
        or re.match(r"^(?:无对应|无需|不适用|不需要|未找到|没有对应|无立面|无节点|无详图)", text)
    )


def resolve_view_applicability(
    item: TakeoffItem, field: str, prepared: PreparedViewContext
) -> ViewApplicability:
    """Resolve one side without observing the other row or its applicability."""

    def invalid(reason: str) -> ViewApplicability:
        return ViewApplicability("INVALID", reason)

    disposition = item.view_dispositions.get(field)
    value = getattr(item, field)
    if disposition is None:
        if value is None or not str(value).strip():
            return ViewApplicability("MISSING", "value_missing")
        if is_view_placeholder(value):
            return invalid("unaudited_view_placeholder")
        return ViewApplicability("APPLICABLE", "positive_view_reference")
    try:
        raw = disposition.model_dump(mode="json")
        disposition = ViewDisposition.model_validate(raw)
    except (AttributeError, ValidationError, TypeError, ValueError):
        return invalid("view_disposition_invalid")
    if len(item.view_dispositions) != 1:
        return invalid("multiple_not_applicable_stages")
    if value is not None and str(value).strip() and not is_view_placeholder(value):
        return invalid("positive_reference_conflicts_with_disposition")
    if not item.component_id or disposition.component_id != item.component_id:
        return invalid("disposition_component_mismatch")
    required = {
        "elevation": ("plan_section", "section"),
        "detail": ("plan_elevation_without_detail", "elevation"),
    }
    basis_kind, second_role = required[field]
    if disposition.basis_kind != basis_kind:
        return invalid("disposition_topology_mismatch")
    context = prepared.context
    if context is None:
        return invalid(prepared.error or "view_context_missing")
    if disposition.index_sha256.lower() != context.index_sha256.lower():
        return invalid("disposition_index_hash_mismatch")

    views = {view.id: view for view in context.views}
    support_ids = set(disposition.support_view_ids)
    if len(support_ids) != 2 or not support_ids.issubset(views):
        return invalid("support_view_missing_or_duplicate")
    support = [views[identifier] for identifier in disposition.support_view_ids]
    if any(view.component_id != item.component_id for view in support):
        return invalid("support_view_component_mismatch")
    selected = {view.id for view in context.views if view.component_id == item.component_id}
    if selected != support_ids:
        return invalid("support_views_differ_from_current_selection")
    if {view.role for view in support} != {"plan", second_role}:
        return invalid("support_view_roles_invalid")
    if any(view.state != "CONFIRMED" for view in support):
        return invalid("support_view_not_confirmed")
    if set(support[0].evidence_ids) & set(support[1].evidence_ids):
        return invalid("same_evidence_reused_across_support_views")
    sources = {view.source_id for view in support}
    if set(disposition.source_sha256) != sources or any(
        source not in context.source_sha256
        or disposition.source_sha256[source].lower() != context.source_sha256[source].lower()
        for source in sources
    ):
        return invalid("disposition_source_hash_mismatch")

    connections = {edge.id: edge for edge in context.connections}
    edge = connections.get(disposition.connection_id)
    role_views = {view.role: view for view in support}
    if (
        edge is None
        or edge.component_id != item.component_id
        or edge.state != "CONFIRMED"
        or edge.source_view_id != role_views["plan"].id
        or edge.target_view_id != role_views[second_role].id
    ):
        return invalid("support_views_disconnected")

    searches = {search.id: search for search in context.searches}
    search = searches.get(disposition.search_id)
    required_sheets = (
        {view.sheet_id for view in support}
        if field == "elevation"
        else {role_views["elevation"].sheet_id}
    )
    if (
        search is None
        or search.component_id != item.component_id
        or search.excluded_stage != field
        or not search.complete
        or search.unresolved_reference_ids
        or search.conflicting_view_ids
        or not required_sheets.issubset(search.searched_sheet_ids)
    ):
        return invalid("negative_search_invalid_or_incomplete")

    entities = {entity.id: entity for entity in context.entities}
    support_evidence = {identifier for view in support for identifier in view.evidence_ids}
    required_evidence = support_evidence | set(edge.evidence_ids)
    receipt_evidence = set(disposition.evidence_ids)
    if (
        len(receipt_evidence) != len(disposition.evidence_ids)
        or not required_evidence.issubset(receipt_evidence)
        or not receipt_evidence.issubset(item.evidence_ids)
        or not receipt_evidence.issubset(entities)
        or not has_source_evidence(item)
    ):
        return invalid("disposition_evidence_missing_or_unbound")
    scope = {(view.sheet_id, view.source_id) for view in support}
    if any(
        entities[identifier].component_id != item.component_id
        or (entities[identifier].sheet_id, entities[identifier].source_id) not in scope
        for identifier in receipt_evidence
    ):
        return invalid("disposition_evidence_outside_component_scope")
    if any(
        (entities[identifier].sheet_id, entities[identifier].source_id)
        != (view.sheet_id, view.source_id)
        for view in support
        for identifier in view.evidence_ids
    ):
        return invalid("support_view_evidence_scope_mismatch")
    governing_view = role_views[second_role]
    if not any(
        entities[identifier].entity_type.upper() == "DIMENSION"
        for identifier in governing_view.evidence_ids
    ):
        return invalid("governing_view_dimension_missing")
    return ViewApplicability("NOT_APPLICABLE", "audited_view_not_applicable")
