"""Conservative component assembly and measurement candidate selection."""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, TypeVar

from .calculation import calculate_item, evaluate_numeric_expression, infer_unit
from .models import (
    CadEntity,
    ComponentInstance,
    EvidenceEdge,
    MaterialSpec,
    MeasurementCandidate,
    MtOccurrence,
    ReviewStatus,
    RunIssue,
    Severity,
    Sheet,
    TakeoffItem,
)

_UNFOLDED_RE = re.compile(r"(?<![\d.])\d+(?:\.\d+)?(?:\s*\+\s*\d+(?:\.\d+)?){1,10}(?![\d.])")
_EXPLICIT_UNFOLDED_RE = re.compile(
    r"(?:展开(?:宽度|规格)?|UNFOLDED(?:\s+WIDTH)?)\s*[:=：]?\s*"
    r"(?P<value>\d+(?:\.\d+)?)\s*(?:mm|毫米)",
    re.IGNORECASE,
)
_EXPLICIT_LENGTH_RE = re.compile(
    r"(?:长度|(?<![A-Z0-9])L(?:ENGTH)?)\s*[:=：]\s*"
    r"(?P<value>\d+(?:\.\d+)?)\s*(?:mm|毫米)",
    re.IGNORECASE,
)
_EXPLICIT_HEIGHT_RE = re.compile(
    r"(?:高度|(?<![A-Z0-9])H(?:EIGHT)?)\s*[:=：]\s*"
    r"(?P<value>\d+(?:\.\d+)?)\s*(?:mm|毫米)",
    re.IGNORECASE,
)
_QUANTITY_RE = re.compile(
    r"(?:[xX×*]\s*(?P<prefix>\d+(?:\.\d+)?)|"
    r"(?P<suffix>\d+(?:\.\d+)?)\s*(?:件|套|块|处|樘|个)|"
    r"(?:数量|QTY)\s*[:=：]?\s*(?P<label>\d+(?:\.\d+)?))",
    re.IGNORECASE,
)
_HEIGHT_RE = re.compile(r"(?:高度|高\s*[:=：]?|\bH\s*[:=])", re.IGNORECASE)
_DIMENSION_TYPES = {"DIMENSION", "ARC_DIMENSION", "LARGE_RADIAL_DIMENSION"}
_MAX_MEASUREMENT_CANDIDATES_PER_ROLE = 24

_T = TypeVar("_T")


def _dimension_is_millimetres(entity: CadEntity) -> bool:
    """Accept geometric DIMENSION values only with explicit millimetre metadata."""

    for key in ("units", "unit", "measurement_unit", "drawing_units", "units_code"):
        if key not in entity.geometry:
            continue
        raw = entity.geometry[key]
        if raw == 4:  # DXF $INSUNITS code for millimetres.
            return True
        normalized = str(raw).strip().casefold().replace(" ", "")
        return normalized in {
            "4",
            "mm",
            "millimeter",
            "millimeters",
            "millimetre",
            "millimetres",
            "毫米",
        }
    return False


def _safe_dimension_value(entity: CadEntity) -> tuple[str, float] | None:
    """Resolve a model-space millimetre DIMENSION, preferring display override.

    A displayed override is the quantity a human sees.  If it is present but is
    not a safe numeric expression, the candidate is discarded instead of falling
    back to conflicting hidden geometry.
    """

    if not _dimension_is_millimetres(entity):
        return None
    if entity.space.casefold().startswith("paper") and not isinstance(
        entity.geometry.get("display_measurement"), (int, float)
    ):
        return None
    if entity.text_override is not None:
        raw_value = entity.text_override.strip()
        expression = re.sub(r"(?:mm|毫米)\s*$", "", raw_value, flags=re.IGNORECASE).strip()
        try:
            value = float(evaluate_numeric_expression(expression))
        except (ValueError, OverflowError):
            return None
        if not math.isfinite(value) or value <= 0:
            return None
        return raw_value, value
    display_measurement = entity.geometry.get("display_measurement")
    numeric_value = (
        float(display_measurement)
        if isinstance(display_measurement, (int, float))
        else entity.value
    )
    if numeric_value is None or not math.isfinite(numeric_value) or numeric_value <= 0:
        return None
    return str(numeric_value), float(numeric_value)


def _stable_id(prefix: str, *parts: object) -> str:
    canonical = json.dumps(parts, ensure_ascii=False, sort_keys=True, default=str)
    return f"{prefix}:{hashlib.sha256(canonical.encode('utf-8')).hexdigest()[:24]}"


def _center(entity: CadEntity) -> tuple[float, float] | None:
    if entity.bbox is not None:
        return ((entity.bbox[0] + entity.bbox[2]) / 2, (entity.bbox[1] + entity.bbox[3]) / 2)
    return entity.insert


def _distance(
    left: tuple[float, float] | None,
    right: tuple[float, float] | None,
) -> float | None:
    if left is None or right is None:
        return None
    return math.hypot(left[0] - right[0], left[1] - right[1])


def _sheet_radius(sheet: Sheet | None) -> float:
    if sheet is None or sheet.bbox is None:
        return 1_500.0
    width = max(sheet.bbox[2] - sheet.bbox[0], 1.0)
    height = max(sheet.bbox[3] - sheet.bbox[1], 1.0)
    return max(200.0, min(5_000.0, math.hypot(width, height) * 0.12))


def _sheet_label(sheet: Sheet | None) -> str | None:
    if sheet is None:
        return None
    values = [value for value in (sheet.drawing_number, sheet.title) if value]
    return " / ".join(dict.fromkeys(values)) or sheet.layout


@dataclass(slots=True)
class TakeoffBuildResult:
    components: list[ComponentInstance] = field(default_factory=list)
    measurements: list[MeasurementCandidate] = field(default_factory=list)
    items: list[TakeoffItem] = field(default_factory=list)
    evidence_edges: list[EvidenceEdge] = field(default_factory=list)
    issues: list[RunIssue] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "components": [value.model_dump(mode="json") for value in self.components],
            "measurements": [value.model_dump(mode="json") for value in self.measurements],
            "items": [value.model_dump(mode="json") for value in self.items],
            "evidence_edges": [value.model_dump(mode="json") for value in self.evidence_edges],
            "issues": [value.model_dump(mode="json") for value in self.issues],
        }


@dataclass(frozen=True, slots=True)
class _MeasurementFact:
    """A component-independent measurement parsed once from a CAD entity."""

    entity: CadEntity
    role: str
    raw_value: str
    numeric_value: float
    unit: str
    basis_label: str
    confidence_base: float
    identity: tuple[str, str, str]

    def candidate_id(self, component_id: str) -> str:
        if self.basis_label == "cad_dimension":
            return _stable_id("measurement", component_id, self.role, self.entity.id)
        return _stable_id(
            "measurement",
            component_id,
            self.role,
            self.entity.id,
            self.raw_value,
        )


class _PointGrid:
    """Small uniform grid used for exact-radius neighborhood queries."""

    __slots__ = ("_cells", "cell_size")

    def __init__(self, cell_size: float) -> None:
        self.cell_size = max(float(cell_size), 1e-6)
        self._cells: dict[tuple[int, int], list[tuple[_T, tuple[float, float]]]] = defaultdict(list)

    def _cell(self, point: tuple[float, float]) -> tuple[int, int]:
        return (
            math.floor(point[0] / self.cell_size),
            math.floor(point[1] / self.cell_size),
        )

    def add(self, value: _T, point: tuple[float, float]) -> None:
        self._cells[self._cell(point)].append((value, point))

    def within(self, point: tuple[float, float], radius: float) -> list[_T]:
        radius = max(radius, 0.0)
        x, y = self._cell(point)
        reach = max(1, math.ceil(radius / self.cell_size))
        output: list[_T] = []
        for offset_x in range(-reach, reach + 1):
            for offset_y in range(-reach, reach + 1):
                for value, candidate_point in self._cells.get(
                    (x + offset_x, y + offset_y),
                    (),
                ):
                    if _distance(point, candidate_point) <= radius:
                        output.append(value)
        return output


class _UnionFind:
    __slots__ = ("parents",)

    def __init__(self, size: int) -> None:
        self.parents = list(range(size))

    def find(self, index: int) -> int:
        while self.parents[index] != index:
            self.parents[index] = self.parents[self.parents[index]]
            index = self.parents[index]
        return index

    def union(self, left: int, right: int) -> None:
        left_root = self.find(left)
        right_root = self.find(right)
        if left_root != right_root:
            self.parents[right_root] = left_root


@dataclass(slots=True)
class _MeasurementIndex:
    """Per-run indexes that keep component extraction near O(entities + local hits)."""

    sheet_by_id: dict[str, Sheet]
    occurrence_by_id: dict[str, MtOccurrence]
    radius_by_sheet: dict[str, float]
    facts_by_sheet: dict[str, list[_MeasurementFact]]
    fact_grids: dict[str, _PointGrid]
    unlocated_facts: dict[str, list[_MeasurementFact]]

    @classmethod
    def build(
        cls,
        sheets: Sequence[Sheet],
        entities: Sequence[CadEntity],
        occurrences: Sequence[MtOccurrence],
    ) -> _MeasurementIndex:
        sheet_by_id = {sheet.id: sheet for sheet in sheets}
        occurrence_by_id = {occurrence.id: occurrence for occurrence in occurrences}
        radius_by_sheet = {
            sheet_id: _sheet_radius(sheet) for sheet_id, sheet in sheet_by_id.items()
        }
        text_by_sheet: dict[str, list[CadEntity]] = defaultdict(list)
        dimensions_by_sheet: dict[str, list[CadEntity]] = defaultdict(list)

        # This is the only full entity-table scan performed by build_takeoff().
        for entity in entities:
            if entity.sheet_id is None:
                continue
            if entity.text:
                text_by_sheet[entity.sheet_id].append(entity)
            if entity.entity_type in _DIMENSION_TYPES:
                dimensions_by_sheet[entity.sheet_id].append(entity)

        height_text_grids: dict[str, _PointGrid] = {}
        for sheet_id, text_entities in text_by_sheet.items():
            radius = radius_by_sheet.get(sheet_id, 1_500.0)
            grid = _PointGrid(max(radius * 0.25, 1.0))
            for entity in text_entities:
                point = _center(entity)
                if point is not None and _HEIGHT_RE.search(entity.text or ""):
                    grid.add(entity.id, point)
            height_text_grids[sheet_id] = grid

        facts_by_sheet: dict[str, list[_MeasurementFact]] = defaultdict(list)
        all_sheet_ids = set(text_by_sheet) | set(dimensions_by_sheet)
        for sheet_id in all_sheet_ids:
            radius = radius_by_sheet.get(sheet_id, 1_500.0)
            height_grid = height_text_grids.get(sheet_id)
            sheet = sheet_by_id.get(sheet_id)
            dimension_entities = (
                dimensions_by_sheet.get(sheet_id, ())
                if sheet is not None
                and sheet.kind in {"plan", "elevation_index", "elevation", "door"}
                else ()
            )
            for entity in dimension_entities:
                resolved_dimension = _safe_dimension_value(entity)
                if resolved_dimension is None:
                    continue
                raw_value, numeric_value = resolved_dimension
                point = _center(entity)
                is_height = bool(
                    point is not None
                    and height_grid is not None
                    and height_grid.within(point, radius * 0.25)
                )
                role = "height" if is_height else "length"
                facts_by_sheet[sheet_id].append(
                    _MeasurementFact(
                        entity=entity,
                        role=role,
                        raw_value=raw_value,
                        numeric_value=numeric_value,
                        unit="mm",
                        basis_label="cad_dimension",
                        confidence_base=0.58,
                        identity=(entity.id, role, raw_value),
                    )
                )

            for entity in text_by_sheet.get(sheet_id, ()):
                text = entity.text or ""
                for match in _UNFOLDED_RE.finditer(text):
                    expression = re.sub(r"\s+", "", match.group())
                    try:
                        value = evaluate_numeric_expression(expression)
                    except ValueError:
                        continue
                    facts_by_sheet[sheet_id].append(
                        _MeasurementFact(
                            entity=entity,
                            role="unfolded_spec",
                            raw_value=expression,
                            numeric_value=float(value),
                            unit="mm",
                            basis_label="plus_expression",
                            confidence_base=0.60,
                            identity=(entity.id, "unfolded_spec", expression),
                        )
                    )
                for role, pattern, basis_label in (
                    ("unfolded_spec", _EXPLICIT_UNFOLDED_RE, "explicit_unfolded_text"),
                    ("length", _EXPLICIT_LENGTH_RE, "explicit_length_text"),
                    ("height", _EXPLICIT_HEIGHT_RE, "explicit_height_text"),
                ):
                    seen_values: set[float] = set()
                    for match in pattern.finditer(text):
                        value = float(match.group("value"))
                        if not math.isfinite(value) or value <= 0 or value in seen_values:
                            continue
                        seen_values.add(value)
                        raw_value = match.group()
                        facts_by_sheet[sheet_id].append(
                            _MeasurementFact(
                                entity=entity,
                                role=role,
                                raw_value=raw_value,
                                numeric_value=value,
                                unit="mm",
                                basis_label=basis_label,
                                confidence_base=0.62,
                                identity=(entity.id, role, f"{value:g}mm"),
                            )
                        )
                seen_quantity_values: set[float] = set()
                for match in _QUANTITY_RE.finditer(text):
                    raw = (
                        match.group("prefix")
                        or match.group("suffix")
                        or match.group("label")
                    )
                    if raw is None:
                        continue
                    value = float(raw)
                    if value <= 0 or value > 100_000 or value in seen_quantity_values:
                        continue
                    seen_quantity_values.add(value)
                    matched_text = match.group()
                    facts_by_sheet[sheet_id].append(
                        _MeasurementFact(
                            entity=entity,
                            role="quantity",
                            raw_value=matched_text,
                            numeric_value=value,
                            unit="count",
                            basis_label="explicit_count_text",
                            confidence_base=0.62,
                            identity=(entity.id, "quantity", f"{value:g}"),
                        )
                    )

        fact_grids: dict[str, _PointGrid] = {}
        unlocated_facts: dict[str, list[_MeasurementFact]] = defaultdict(list)
        for sheet_id, facts in facts_by_sheet.items():
            grid = _PointGrid(radius_by_sheet.get(sheet_id, 1_500.0))
            for fact in facts:
                point = _center(fact.entity)
                if point is None:
                    unlocated_facts[sheet_id].append(fact)
                else:
                    grid.add(fact, point)
            fact_grids[sheet_id] = grid

        return cls(
            sheet_by_id=sheet_by_id,
            occurrence_by_id=occurrence_by_id,
            radius_by_sheet=radius_by_sheet,
            facts_by_sheet=dict(facts_by_sheet),
            fact_grids=fact_grids,
            unlocated_facts=dict(unlocated_facts),
        )

    def facts_near(
        self,
        sheet_id: str,
        points: Sequence[tuple[float, float]],
    ) -> list[_MeasurementFact]:
        if not points:
            return self.facts_by_sheet.get(sheet_id, [])
        radius = self.radius_by_sheet.get(sheet_id, 1_500.0)
        selected: dict[tuple[str, str, str], _MeasurementFact] = {
            fact.identity: fact for fact in self.unlocated_facts.get(sheet_id, ())
        }
        grid = self.fact_grids.get(sheet_id)
        if grid is not None:
            for point in points:
                for fact in grid.within(point, radius):
                    selected[fact.identity] = fact
        return list(selected.values())


def _ranked_targets(
    edges: Sequence[EvidenceEdge],
    source_id: str,
    relation: str,
) -> list[EvidenceEdge]:
    return sorted(
        (edge for edge in edges if edge.source_id == source_id and edge.relation == relation),
        key=lambda edge: (-edge.confidence, edge.target_id, edge.id),
    )


def _dedupe_tolerance(sheet: Sheet | None) -> float:
    """Return an intentionally tiny drawing-unit tolerance for duplicate tags."""

    if sheet is None or sheet.bbox is None:
        return 0.1
    diagonal = math.hypot(
        sheet.bbox[2] - sheet.bbox[0],
        sheet.bbox[3] - sheet.bbox[1],
    )
    return min(0.1, max(1e-6, diagonal * 1e-8))


def _group_duplicate_occurrences(
    occurrences: Sequence[MtOccurrence],
    sheet_by_id: Mapping[str, Sheet],
) -> list[list[MtOccurrence]]:
    """Merge only duplicate evidence records, never infer physical quantity.

    The detector may emit the same annotation more than once (for example, a split
    MT label and its joined text). Records can be grouped only inside the same
    source/sheet/MT bucket and only when they share an original entity/leader or
    have effectively identical anchors/leader endpoints.
    """

    buckets: dict[tuple[str, str | None, str], list[MtOccurrence]] = defaultdict(list)
    for occurrence in occurrences:
        buckets[(occurrence.source_file_id, occurrence.sheet_id, occurrence.mt_code)].append(
            occurrence
        )

    output: list[list[MtOccurrence]] = []
    for bucket_key in sorted(buckets):
        bucket = sorted(buckets[bucket_key], key=lambda value: value.id)
        groups = _UnionFind(len(bucket))

        identity_owner: dict[tuple[str, str], int] = {}
        for index, occurrence in enumerate(bucket):
            identities = [("entity", value) for value in occurrence.entity_ids]
            if occurrence.leader_entity_id:
                identities.append(("leader", occurrence.leader_entity_id))
            for identity in identities:
                owner = identity_owner.setdefault(identity, index)
                groups.union(index, owner)

        tolerance = _dedupe_tolerance(sheet_by_id.get(bucket_key[1] or ""))
        target_grid = _PointGrid(tolerance)
        anchor_grid = _PointGrid(tolerance)
        for index, occurrence in enumerate(bucket):
            if occurrence.leader_target is not None:
                for neighbor in target_grid.within(occurrence.leader_target, tolerance):
                    groups.union(index, neighbor)
                target_grid.add(index, occurrence.leader_target)
            if occurrence.anchor is not None:
                for neighbor in anchor_grid.within(occurrence.anchor, tolerance):
                    groups.union(index, neighbor)
                anchor_grid.add(index, occurrence.anchor)

        grouped: dict[int, list[MtOccurrence]] = defaultdict(list)
        for index, occurrence in enumerate(bucket):
            grouped[groups.find(index)].append(occurrence)
        output.extend(
            sorted(
                (sorted(group, key=lambda value: value.id) for group in grouped.values()),
                key=lambda group: group[0].id,
            )
        )
    return output


def _consensus(values: Sequence[str | None]) -> str | None:
    distinct = {value.strip() for value in values if value and value.strip()}
    return next(iter(distinct)) if len(distinct) == 1 else None


def _semantic_key(value: str | None) -> str | None:
    if not value:
        return None
    normalized = re.sub(r"\s+", "", value).casefold()
    return normalized or None


def _unique_semantic_elevation_group(
    plan_group: Sequence[MtOccurrence],
    candidates: Sequence[MtOccurrence],
    sheet_by_id: Mapping[str, Sheet],
) -> list[MtOccurrence]:
    """Return one exact room/component match, never a sheet-wide MT fan-out."""

    plan_room = _semantic_key(_consensus([value.room for value in plan_group]))
    plan_hint = _semantic_key(_consensus([value.component_hint for value in plan_group]))
    if plan_room is None and plan_hint is None:
        return []
    matching_groups: list[list[MtOccurrence]] = []
    for group in _group_duplicate_occurrences(candidates, sheet_by_id):
        candidate_room = _semantic_key(_consensus([value.room for value in group]))
        candidate_hint = _semantic_key(_consensus([value.component_hint for value in group]))
        room_match = plan_room is not None and candidate_room == plan_room
        hint_match = plan_hint is not None and candidate_hint == plan_hint
        room_conflict = (
            plan_room is not None and candidate_room is not None and candidate_room != plan_room
        )
        hint_conflict = (
            plan_hint is not None and candidate_hint is not None and candidate_hint != plan_hint
        )
        if (room_match or hint_match) and not room_conflict and not hint_conflict:
            matching_groups.append(group)
    return matching_groups[0] if len(matching_groups) == 1 else []


def build_component_instances(
    sheets: Sequence[Sheet],
    occurrences: Sequence[MtOccurrence],
    edges: Sequence[EvidenceEdge],
    *,
    include_orphan_elevations: bool = True,
) -> list[ComponentInstance]:
    """Build plan-led and isolated elevation/door component candidates.

    Orphan candidates preserve otherwise-lost MT evidence, but remain REVIEW and
    never convert occurrence count into a physical quantity.
    """

    sheet_by_id = {sheet.id: sheet for sheet in sheets}
    unique_occurrences: list[MtOccurrence] = []
    seen_occurrence_ids: set[str] = set()
    for occurrence in occurrences:
        if occurrence.id not in seen_occurrence_ids:
            unique_occurrences.append(occurrence)
            seen_occurrence_ids.add(occurrence.id)

    by_sheet: dict[str, list[MtOccurrence]] = defaultdict(list)
    for occurrence in unique_occurrences:
        if occurrence.sheet_id:
            by_sheet[occurrence.sheet_id].append(occurrence)
    plan_occurrences = [
        occurrence
        for occurrence in unique_occurrences
        if occurrence.sheet_id
        and sheet_by_id.get(occurrence.sheet_id)
        and sheet_by_id[occurrence.sheet_id].kind in {"plan", "elevation_index"}
    ]
    elevation_or_door_occurrences = [
        occurrence
        for occurrence in unique_occurrences
        if occurrence.sheet_id
        and sheet_by_id.get(occurrence.sheet_id)
        and sheet_by_id[occurrence.sheet_id].kind in {"elevation", "door"}
    ]
    edges_by_relation_source: dict[tuple[str, str], list[EvidenceEdge]] = defaultdict(list)
    for edge in edges:
        edges_by_relation_source[(edge.relation, edge.source_id)].append(edge)
    for indexed_edges in edges_by_relation_source.values():
        indexed_edges.sort(key=lambda edge: (-edge.confidence, edge.target_id, edge.id))

    component_records: list[tuple[str, ComponentInstance]] = []
    claimed_elevation_ids: set[str] = set()
    for occurrence_group in _group_duplicate_occurrences(plan_occurrences, sheet_by_id):
        occurrence = occurrence_group[0]
        plan_ids = sorted(value.id for value in occurrence_group)
        source_sheet_id = occurrence.sheet_id
        if source_sheet_id is None:
            continue
        plan_edges = edges_by_relation_source.get(
            ("plan_to_elevation", source_sheet_id),
            [],
        )
        elevation_sheet_ids = {edge.target_id for edge in plan_edges}
        elevation_candidates = [
            item
            for elevation_sheet_id in elevation_sheet_ids
            for item in by_sheet.get(elevation_sheet_id, [])
            if item.mt_code == occurrence.mt_code
        ]
        semantic_group = _unique_semantic_elevation_group(
            occurrence_group,
            elevation_candidates,
            sheet_by_id,
        )
        unique_elevation_ids = sorted(value.id for value in semantic_group)
        claimed_elevation_ids.update(unique_elevation_ids)
        detail_ids = sorted(
            {
                edge.target_id
                for value in semantic_group
                if value.sheet_id
                for edge in edges_by_relation_source.get(
                    ("elevation_to_detail", value.sheet_id),
                    [],
                )
            }
        )
        component_id = _stable_id(
            "component",
            occurrence.mt_code,
            sorted(value.id for value in occurrence_group),
            unique_elevation_ids,
            detail_ids,
        )
        component_records.append(
            (
                occurrence.id,
                ComponentInstance(
                    id=component_id,
                    mt_code=occurrence.mt_code,
                    name=_consensus([value.component_hint for value in occurrence_group]),
                    room=_consensus([value.room for value in occurrence_group]),
                    plan_occurrence_ids=plan_ids,
                    elevation_occurrence_ids=unique_elevation_ids,
                    detail_sheet_ids=detail_ids,
                    status=ReviewStatus.REVIEW,
                ),
            )
        )

    if include_orphan_elevations:
        orphan_groups = _group_duplicate_occurrences(
            elevation_or_door_occurrences,
            sheet_by_id,
        )
        for occurrence_group in orphan_groups:
            group_ids = {value.id for value in occurrence_group}
            if group_ids & claimed_elevation_ids:
                continue
            occurrence = occurrence_group[0]
            source_sheet_id = occurrence.sheet_id
            if source_sheet_id is None:
                continue
            detail_edges = edges_by_relation_source.get(
                ("elevation_to_detail", source_sheet_id),
                [],
            )
            detail_ids = sorted({edge.target_id for edge in detail_edges})
            elevation_ids = sorted(group_ids)
            component_id = _stable_id(
                "component",
                "orphan_elevation",
                occurrence.mt_code,
                elevation_ids,
                detail_ids,
            )
            component_records.append(
                (
                    occurrence.id,
                    ComponentInstance(
                        id=component_id,
                        mt_code=occurrence.mt_code,
                        name=_consensus([value.component_hint for value in occurrence_group]),
                        room=_consensus([value.room for value in occurrence_group]),
                        plan_occurrence_ids=[],
                        elevation_occurrence_ids=elevation_ids,
                        detail_sheet_ids=detail_ids,
                        status=ReviewStatus.REVIEW,
                    ),
                )
            )
    return [component for _, component in sorted(component_records, key=lambda value: value[0])]


def _component_points(
    component: ComponentInstance,
    occurrences: Mapping[str, MtOccurrence],
) -> dict[str, list[tuple[float, float]]]:
    points: dict[str, list[tuple[float, float]]] = defaultdict(list)
    for occurrence_id in [
        *component.plan_occurrence_ids,
        *component.elevation_occurrence_ids,
    ]:
        occurrence = occurrences.get(occurrence_id)
        if occurrence is None or occurrence.sheet_id is None:
            continue
        point = occurrence.leader_target or occurrence.anchor
        if point is not None:
            points[occurrence.sheet_id].append(point)
    return points


def _proximity(
    entity: CadEntity,
    points: Sequence[tuple[float, float]],
) -> float | None:
    distances = [
        value
        for value in (_distance(_center(entity), point) for point in points)
        if value is not None
    ]
    return min(distances) if distances else None


def _candidate_confidence(distance: float | None, radius: float, base: float) -> float:
    if distance is None:
        return round(min(base, 0.45), 6)
    proximity_score = max(0.0, 1.0 - distance / max(radius, 1.0))
    return round(min(0.95, base + 0.25 * proximity_score), 6)


def collect_measurement_candidates(
    component: ComponentInstance,
    sheets: Sequence[Sheet],
    entities: Sequence[CadEntity],
    occurrences: Sequence[MtOccurrence],
    *,
    _measurement_index: _MeasurementIndex | None = None,
) -> list[MeasurementCandidate]:
    """Collect dimensions/specifications/counts; do not silently select ambiguous values."""

    measurement_index = _measurement_index or _MeasurementIndex.build(
        sheets,
        entities,
        occurrences,
    )
    sheet_by_id = measurement_index.sheet_by_id
    occurrence_by_id = measurement_index.occurrence_by_id
    points = _component_points(component, occurrence_by_id)
    elevation_sheet_ids = {
        occurrence_by_id[value].sheet_id
        for value in component.elevation_occurrence_ids
        if value in occurrence_by_id and occurrence_by_id[value].sheet_id
    }
    relevant_sheet_ids = set(elevation_sheet_ids) | set(component.detail_sheet_ids)
    output: list[MeasurementCandidate] = []

    for sheet_id in sorted(relevant_sheet_ids):
        sheet = sheet_by_id.get(sheet_id)
        radius = _sheet_radius(sheet)
        sheet_points = points.get(sheet_id, [])
        facts = measurement_index.facts_near(sheet_id, sheet_points)
        fallback_identities: set[tuple[str, str, str]] = set()
        expected_labeled_roles = (
            {"length", "height", "quantity"}
            if sheet is not None and sheet.kind in {"elevation", "door"}
            else {"unfolded_spec"}
            if sheet is not None and sheet.kind == "detail"
            else set()
        )
        missing_roles = expected_labeled_roles - {fact.role for fact in facts}
        if missing_roles and sheet_points:
            # A label can be intentionally placed outside the local MT callout.
            # Keep explicit, unit-bearing labels from the already-linked sheet
            # reviewable, but never promote them automatically. Raw CAD
            # dimensions remain proximity-gated to avoid a sheet-wide fan-out.
            fallback_by_role: dict[str, list[_MeasurementFact]] = defaultdict(list)
            for fact in measurement_index.facts_by_sheet.get(sheet_id, ()):
                if fact.role in missing_roles and fact.basis_label.startswith("explicit_"):
                    fallback_by_role[fact.role].append(fact)
            for role in sorted(fallback_by_role):
                ranked_fallbacks = sorted(
                    fallback_by_role[role],
                    key=lambda fact: (
                        _proximity(fact.entity, sheet_points) is None,
                        _proximity(fact.entity, sheet_points) or float("inf"),
                        fact.entity.id,
                    ),
                )[:_MAX_MEASUREMENT_CANDIDATES_PER_ROLE]
                facts.extend(ranked_fallbacks)
                fallback_identities.update(fact.identity for fact in ranked_fallbacks)
        selected_facts = {fact.identity: fact for fact in facts}
        for fact in selected_facts.values():
            entity = fact.entity
            distance = _proximity(entity, sheet_points)
            basis = [f"entity:{entity.id}", f"sheet:{sheet_id}"]
            if distance is not None:
                basis.append(f"anchor_distance:{distance:.3f}")
            is_fallback = fact.identity in fallback_identities
            if is_fallback:
                basis.append("linked_sheet_labeled_fallback")
            confidence = _candidate_confidence(
                distance,
                radius,
                fact.confidence_base,
            )
            if is_fallback:
                confidence = min(confidence, 0.52)
            output.append(
                MeasurementCandidate(
                    id=fact.candidate_id(component.id),
                    component_id=component.id,
                    role=fact.role,
                    raw_value=fact.raw_value,
                    numeric_value=fact.numeric_value,
                    unit=fact.unit,
                    source_file_id=entity.source_file_id,
                    sheet_id=sheet_id,
                    entity_ids=[entity.id],
                    distance=distance,
                    basis=[*basis, fact.basis_label],
                    confidence=confidence,
                    status=ReviewStatus.REVIEW,
                )
            )
    # A linked sheet can contain hundreds or thousands of unrelated dimensions.
    # Keeping every one for every MT occurrence creates a quadratic review pack
    # and makes a human choice less reliable.  Preserve the nearest candidates
    # per role, mark truncation explicitly, and never auto-PASS a truncated set.
    by_role: dict[str, list[MeasurementCandidate]] = defaultdict(list)
    for candidate in output:
        by_role[candidate.role].append(candidate)
    bounded: list[MeasurementCandidate] = []
    for role in sorted(by_role):
        ranked = sorted(
            by_role[role],
            key=lambda value: (
                value.distance is None,
                value.distance if value.distance is not None else float("inf"),
                -value.confidence,
                value.id,
            ),
        )
        total = len(ranked)
        for rank, candidate in enumerate(
            ranked[:_MAX_MEASUREMENT_CANDIDATES_PER_ROLE],
            start=1,
        ):
            basis = [*candidate.basis, f"candidate_rank:{rank}/{total}"]
            if total > _MAX_MEASUREMENT_CANDIDATES_PER_ROLE:
                basis.append(
                    "candidate_pool_truncated:"
                    f"{total}->{_MAX_MEASUREMENT_CANDIDATES_PER_ROLE}"
                )
            bounded.append(candidate.model_copy(update={"basis": basis}))
    return sorted(bounded, key=lambda value: (value.role, -value.confidence, value.id))


def _select_candidate(
    candidates: Sequence[MeasurementCandidate],
    role: str,
    confirmed: Mapping[str, str],
) -> tuple[MeasurementCandidate | None, str | None]:
    values = [candidate for candidate in candidates if candidate.role == role]
    explicit_id = confirmed.get(role)
    if explicit_id:
        selected = next((candidate for candidate in values if candidate.id == explicit_id), None)
        if selected is None:
            return None, f"confirmed {role} candidate does not exist: {explicit_id}"
        return selected.model_copy(update={"status": ReviewStatus.PASS}), None
    if not values:
        return None, f"missing {role} evidence"
    ranked = sorted(
        values,
        key=lambda value: (
            -value.confidence,
            value.distance or float("inf"),
            value.id,
        ),
    )
    selected = ranked[0]
    if len(ranked) > 1:
        second = ranked[1]
        different = second.unit != selected.unit or not math.isclose(
            second.numeric_value,
            selected.numeric_value,
            rel_tol=1e-9,
            abs_tol=1e-9,
        )
        if different and selected.confidence - second.confidence < 0.12:
            return None, f"ambiguous {role}: {selected.raw_value} vs {second.raw_value}"
    return selected, f"unconfirmed {role} candidate: {selected.id}"


def _chain_is_pass(
    component: ComponentInstance,
    occurrence_by_id: Mapping[str, MtOccurrence],
    edges: Sequence[EvidenceEdge],
    pass_edge_keys: set[tuple[str, str, str]] | None = None,
) -> bool:
    if not component.plan_occurrence_ids or not component.elevation_occurrence_ids:
        return False
    plan_sheet_ids = {
        occurrence_by_id[value].sheet_id
        for value in component.plan_occurrence_ids
        if value in occurrence_by_id
    }
    elevation_sheet_ids = {
        occurrence_by_id[value].sheet_id
        for value in component.elevation_occurrence_ids
        if value in occurrence_by_id
    }
    if pass_edge_keys is None:
        pass_edge_keys = {
            (edge.relation, edge.source_id, edge.target_id)
            for edge in edges
            if edge.status == ReviewStatus.PASS
        }
    plan_ok = any(
        ("plan_to_elevation", plan_sheet_id, elevation_sheet_id) in pass_edge_keys
        for plan_sheet_id in plan_sheet_ids
        for elevation_sheet_id in elevation_sheet_ids
    )
    detail_ok = any(
        ("elevation_to_detail", elevation_sheet_id, detail_sheet_id) in pass_edge_keys
        for elevation_sheet_id in elevation_sheet_ids
        for detail_sheet_id in component.detail_sheet_ids
    )
    return plan_ok and detail_ok


_ALLOWED_UNITS = {"m", "㎡", "件", "套"}
_ROLES_BY_UNIT: dict[str, tuple[str, ...]] = {
    "㎡": ("unfolded_spec", "length", "quantity"),
    "m": ("length", "quantity"),
    "件": ("quantity",),
    "套": ("quantity",),
}


def _parse_occurrence_confirmation(
    confirmed: Mapping[str, Any],
) -> tuple[list[str] | None, list[str]]:
    supplied = [
        key for key in ("elevation_occurrence", "elevation_occurrence_ids") if key in confirmed
    ]
    if not supplied:
        return None, []

    def parse(raw: Any) -> list[str]:
        if isinstance(raw, str):
            return [value.strip() for value in re.split(r"[,，;；]", raw) if value.strip()]
        if isinstance(raw, Sequence) and not isinstance(raw, (str, bytes)):
            return [str(value).strip() for value in raw if str(value).strip()]
        return []

    parsed = [parse(confirmed[key]) for key in supplied]
    if any(not values for values in parsed):
        return None, ["confirmed elevation occurrence selection is empty or invalid"]
    normalized = [sorted(set(values)) for values in parsed]
    if len(normalized) == 2 and normalized[0] != normalized[1]:
        return None, ["elevation_occurrence and elevation_occurrence_ids conflict"]
    return normalized[0], []


def _parse_merge_component_ids(
    confirmed: Mapping[str, Any],
) -> tuple[list[str] | None, list[str]]:
    if "merge_component_ids" not in confirmed:
        return None, []
    raw = confirmed["merge_component_ids"]
    if isinstance(raw, str):
        values = [value.strip() for value in re.split(r"[,，;；]", raw) if value.strip()]
    elif isinstance(raw, Sequence) and not isinstance(raw, (str, bytes)):
        values = [str(value).strip() for value in raw if str(value).strip()]
    else:
        values = []
    if not values:
        return None, ["confirmed merge_component_ids is empty or invalid"]
    return sorted(set(values)), []


def _apply_component_merges(
    components: Sequence[ComponentInstance],
    confirmations: Mapping[str, Mapping[str, Any]],
) -> tuple[list[ComponentInstance], dict[str, list[str]], list[EvidenceEdge]]:
    """Apply atomic, explicitly reviewed physical-component merges.

    Nested merge graphs are deliberately rejected.  Reviewers can flatten such
    a graph into one target confirmation, which keeps ownership and suppression
    unambiguous.
    """

    component_by_id = {component.id: component for component in components}
    requests: dict[str, list[str]] = {}
    errors: dict[str, list[str]] = defaultdict(list)
    for target in components:
        raw_confirmed = confirmations.get(target.id, {})
        if not isinstance(raw_confirmed, Mapping):
            continue
        source_ids, parse_errors = _parse_merge_component_ids(raw_confirmed)
        if parse_errors:
            errors[target.id].extend(parse_errors)
        if source_ids is not None:
            requests[target.id] = source_ids

    claimants_by_source: dict[str, set[str]] = defaultdict(set)
    for target_id, source_ids in requests.items():
        target = component_by_id[target_id]
        for source_id in source_ids:
            source = component_by_id.get(source_id)
            if source is None:
                errors[target_id].append(f"confirmed merge component does not exist: {source_id}")
                continue
            if source_id == target_id:
                errors[target_id].append("component cannot merge itself")
                continue
            if source.mt_code != target.mt_code:
                errors[target_id].append(
                    f"merge component MT mismatch: {source_id} ({source.mt_code})"
                )
                continue
            claimants_by_source[source_id].add(target_id)

    for source_id, target_ids in claimants_by_source.items():
        if len(target_ids) <= 1:
            continue
        rendered = ", ".join(sorted(target_ids))
        for target_id in target_ids:
            errors[target_id].append(
                f"merge source {source_id} is claimed by multiple targets: {rendered}"
            )

    requested_targets = set(requests)
    for target_id, source_ids in requests.items():
        nested_sources = sorted(set(source_ids) & requested_targets)
        if not nested_sources:
            continue
        rendered = ", ".join(nested_sources)
        errors[target_id].append(
            f"nested component merge is not allowed; flatten sources: {rendered}"
        )
        for nested_id in nested_sources:
            errors[nested_id].append(f"component is both merge target and source of {target_id}")

    def conflict_reason(
        target_id: str,
        source_ids: Sequence[str],
        field_name: str,
    ) -> str | None:
        values = [
            getattr(component_by_id[component_id], field_name)
            for component_id in (target_id, *source_ids)
            if component_id in component_by_id
        ]
        distinct = {
            normalized for value in values if (normalized := _semantic_key(value)) is not None
        }
        if len(distinct) > 1:
            return f"merge {field_name} conflict: {' | '.join(sorted(distinct))}"
        return None

    for target_id, source_ids in requests.items():
        if errors.get(target_id):
            continue
        for field_name in ("room", "name"):
            reason = conflict_reason(target_id, source_ids, field_name)
            if reason:
                errors[target_id].append(reason)

    valid_requests = {
        target_id: source_ids
        for target_id, source_ids in requests.items()
        if not errors.get(target_id)
    }
    suppressed_ids = {
        source_id for source_ids in valid_requests.values() for source_id in source_ids
    }
    merged_by_target: dict[str, ComponentInstance] = {}
    merge_edges: list[EvidenceEdge] = []
    for target_id, source_ids in valid_requests.items():
        target = component_by_id[target_id]
        sources = [component_by_id[source_id] for source_id in source_ids]
        members = [target, *sources]

        def preserved_value(
            field_name: str,
            current_members: Sequence[ComponentInstance] = members,
        ) -> str | None:
            return next(
                (
                    value.strip()
                    for member in current_members
                    if (value := getattr(member, field_name)) and value.strip()
                ),
                None,
            )

        merged_by_target[target_id] = target.model_copy(
            update={
                "name": preserved_value("name"),
                "room": preserved_value("room"),
                "plan_occurrence_ids": sorted(
                    {
                        occurrence_id
                        for value in members
                        for occurrence_id in value.plan_occurrence_ids
                    }
                ),
                "elevation_occurrence_ids": sorted(
                    {
                        occurrence_id
                        for value in members
                        for occurrence_id in value.elevation_occurrence_ids
                    }
                ),
                "detail_sheet_ids": sorted(
                    {sheet_id for value in members for sheet_id in value.detail_sheet_ids}
                ),
            }
        )
        for source in sources:
            occurrence_ids = {
                *source.plan_occurrence_ids,
                *source.elevation_occurrence_ids,
            }
            merge_edges.extend(
                EvidenceEdge(
                    id=_stable_id(
                        "edge",
                        "merge_component",
                        occurrence_id,
                        target_id,
                        source.id,
                    ),
                    relation="occurrence_to_component",
                    source_id=occurrence_id,
                    target_id=target_id,
                    basis=[
                        "confirmation:merge_component_ids",
                        f"source_component:{source.id}",
                    ],
                    confidence=1.0,
                    status=ReviewStatus.PASS,
                )
                for occurrence_id in sorted(occurrence_ids)
            )

    merged_components = [
        merged_by_target.get(component.id, component)
        for component in components
        if component.id not in suppressed_ids
    ]
    return merged_components, dict(errors), merge_edges


def _apply_elevation_confirmation(
    component: ComponentInstance,
    occurrence_by_id: Mapping[str, MtOccurrence],
    edges: Sequence[EvidenceEdge],
    confirmed: Mapping[str, Any],
) -> tuple[ComponentInstance, list[str], set[str]]:
    selected_ids, reasons = _parse_occurrence_confirmation(confirmed)
    if selected_ids is None:
        return component, reasons, set()
    if not component.plan_occurrence_ids:
        return component, ["orphan component cannot claim an elevation occurrence"], set()

    plan_sheet_ids = {
        occurrence_by_id[value].sheet_id
        for value in component.plan_occurrence_ids
        if value in occurrence_by_id and occurrence_by_id[value].sheet_id
    }
    target_sheet_ids = {
        edge.target_id
        for edge in edges
        if edge.relation == "plan_to_elevation" and edge.source_id in plan_sheet_ids
    }
    selected: list[MtOccurrence] = []
    for occurrence_id in selected_ids:
        occurrence = occurrence_by_id.get(occurrence_id)
        if occurrence is None:
            reasons.append(f"confirmed elevation occurrence does not exist: {occurrence_id}")
            continue
        if occurrence.mt_code != component.mt_code:
            reasons.append(f"confirmed elevation occurrence MT mismatch: {occurrence_id}")
            continue
        if occurrence.sheet_id not in target_sheet_ids:
            reasons.append(
                f"confirmed elevation occurrence is outside linked target sheets: {occurrence_id}"
            )
            continue
        selected.append(occurrence)
    if reasons:
        return component, reasons, set()

    detail_ids = sorted(
        {
            edge.target_id
            for occurrence in selected
            for edge in edges
            if edge.relation == "elevation_to_detail" and edge.source_id == occurrence.sheet_id
        }
    )
    effective = component.model_copy(
        update={
            "elevation_occurrence_ids": sorted(value.id for value in selected),
            "detail_sheet_ids": detail_ids,
        }
    )
    return effective, [], {value.id for value in selected}


def _resolve_unit_and_pricing_method(
    confirmed: Mapping[str, Any],
) -> tuple[str | None, str | None, list[str]]:
    """Resolve explicitly reviewed commercial semantics without guessing a unit."""

    reasons: list[str] = []
    raw_unit = confirmed.get("unit")
    raw_method = confirmed.get("pricing_method")
    unit = raw_unit.strip() if isinstance(raw_unit, str) else None
    pricing_method = raw_method.strip() if isinstance(raw_method, str) else None

    if raw_unit is not None and (not unit or unit not in _ALLOWED_UNITS):
        reasons.append(f"invalid confirmed unit: {raw_unit!r}")
        unit = None
    if raw_method is not None and not pricing_method:
        reasons.append("confirmed pricing_method is empty")
        pricing_method = None
    if pricing_method is None:
        reasons.append("missing confirmed pricing_method")

    inferred = infer_unit(pricing_method)
    if unit is None and raw_unit is None and inferred in _ALLOWED_UNITS:
        unit = inferred
    elif unit is not None and inferred is not None and inferred != unit:
        reasons.append(f"confirmed unit {unit} conflicts with pricing_method unit {inferred}")
        unit = None
    if unit is None and not any(value.startswith("invalid confirmed unit") for value in reasons):
        reasons.append("missing confirmed unit")
    return unit, pricing_method, reasons


def _component_chain_edges(
    component: ComponentInstance,
    occurrence_by_id: Mapping[str, MtOccurrence],
    edges: Sequence[EvidenceEdge],
    confirmed: Mapping[str, Any],
) -> tuple[
    EvidenceEdge | None,
    EvidenceEdge | None,
    bool,
    list[str],
    list[str],
]:
    """Choose one coherent plan→elevation→detail path.

    Returned reason lists are respectively review-only and blocking failures.  An
    explicit edge ID always takes precedence: an invalid ID cannot silently fall
    back to another PASS edge.
    """

    plan_sheet_ids = {
        occurrence_by_id[value].sheet_id
        for value in component.plan_occurrence_ids
        if value in occurrence_by_id and occurrence_by_id[value].sheet_id
    }
    elevation_sheet_ids = {
        occurrence_by_id[value].sheet_id
        for value in component.elevation_occurrence_ids
        if value in occurrence_by_id and occurrence_by_id[value].sheet_id
    }
    detail_sheet_ids = set(component.detail_sheet_ids)
    valid_plan_edges = sorted(
        (
            edge
            for edge in edges
            if edge.relation == "plan_to_elevation"
            and edge.source_id in plan_sheet_ids
            and edge.target_id in elevation_sheet_ids
        ),
        key=lambda value: (-value.confidence, value.id),
    )
    valid_detail_edges = sorted(
        (
            edge
            for edge in edges
            if edge.relation == "elevation_to_detail"
            and edge.source_id in elevation_sheet_ids
            and edge.target_id in detail_sheet_ids
        ),
        key=lambda value: (-value.confidence, value.id),
    )
    review_reasons: list[str] = []
    block_reasons: list[str] = []

    def explicit_edge(key: str, valid: Sequence[EvidenceEdge]) -> EvidenceEdge | None:
        if key not in confirmed:
            return None
        edge_id = confirmed.get(key)
        if not isinstance(edge_id, str) or not edge_id.strip():
            block_reasons.append(f"confirmed {key} is empty or invalid")
            return None
        edge_id = edge_id.strip()
        selected = next((edge for edge in valid if edge.id == edge_id), None)
        if selected is None:
            block_reasons.append(f"confirmed {key} does not exist in component chain: {edge_id}")
        return selected

    plan_was_explicit = "plan_to_elevation_edge" in confirmed
    detail_was_explicit = "elevation_to_detail_edge" in confirmed
    plan_edge = explicit_edge("plan_to_elevation_edge", valid_plan_edges)
    detail_edge = explicit_edge("elevation_to_detail_edge", valid_detail_edges)
    if block_reasons:
        return plan_edge, detail_edge, False, review_reasons, block_reasons

    coherent_pairs = [
        (plan, detail)
        for plan in valid_plan_edges
        for detail in valid_detail_edges
        if plan.target_id == detail.source_id
        and (plan_edge is None or plan.id == plan_edge.id)
        and (detail_edge is None or detail.id == detail_edge.id)
    ]
    if not coherent_pairs:
        if not valid_plan_edges:
            block_reasons.append("missing plan_to_elevation edge in component chain")
        if not valid_detail_edges:
            block_reasons.append("missing elevation_to_detail edge in component chain")
        if valid_plan_edges and valid_detail_edges:
            block_reasons.append("selected relation edges do not form one coherent chain")
        return plan_edge, detail_edge, False, review_reasons, block_reasons

    if plan_edge is None or detail_edge is None:
        plan_edge, detail_edge = coherent_pairs[0]

    if len(coherent_pairs) > 1:
        if not plan_was_explicit:
            review_reasons.append(
                f"ambiguous plan_to_elevation edges: {len(coherent_pairs)} coherent paths"
            )
        if not detail_was_explicit:
            review_reasons.append(
                f"ambiguous elevation_to_detail edges: {len(coherent_pairs)} coherent paths"
            )

    plan_ok = plan_was_explicit or (
        len(coherent_pairs) == 1 and plan_edge.status == ReviewStatus.PASS
    )
    detail_ok = detail_was_explicit or (
        len(coherent_pairs) == 1 and detail_edge.status == ReviewStatus.PASS
    )
    if not plan_ok:
        review_reasons.append(f"unconfirmed plan_to_elevation edge: {plan_edge.id}")
    if not detail_ok:
        review_reasons.append(f"unconfirmed elevation_to_detail edge: {detail_edge.id}")
    return plan_edge, detail_edge, plan_ok and detail_ok, review_reasons, block_reasons


def _material_for_component(
    mt_code: str,
    materials_by_code: Mapping[str, Sequence[MaterialSpec]],
) -> tuple[MaterialSpec | None, list[str]]:
    candidates = sorted(materials_by_code.get(mt_code, ()), key=lambda value: value.id)
    if not candidates:
        return None, [f"missing material specification for {mt_code}"]
    material = candidates[0]
    conflicts = sorted(
        {conflict for candidate in candidates for conflict in candidate.conflicts if conflict}
    )
    reasons: list[str] = []
    if conflicts:
        reasons.append(f"material conflicts for {mt_code}: {' | '.join(conflicts)}")
    if any(candidate.status == ReviewStatus.BLOCK for candidate in candidates):
        reasons.append(f"blocked material specification for {mt_code}")
    return material, reasons


def build_takeoff(
    sheets: Sequence[Sheet],
    entities: Sequence[CadEntity],
    occurrences: Sequence[MtOccurrence],
    relation_edges: Sequence[EvidenceEdge],
    *,
    materials: Sequence[MaterialSpec] = (),
    confirmations: Mapping[str, Mapping[str, Any]] | None = None,
) -> TakeoffBuildResult:
    """Assemble auditable rows with an explicit, unit-aware confirmation loop."""

    confirmations = confirmations or {}
    sheet_by_id = {sheet.id: sheet for sheet in sheets}
    occurrence_by_id = {occurrence.id: occurrence for occurrence in occurrences}
    materials_by_code: dict[str, list[MaterialSpec]] = defaultdict(list)
    for material in materials:
        materials_by_code[material.mt_code].append(material)
    base_components = build_component_instances(sheets, occurrences, relation_edges)
    occurrence_confirmation_errors: dict[str, list[str]] = {}
    explicitly_claimed_occurrences: set[str] = set()
    effective_components: list[ComponentInstance] = []
    for component in base_components:
        raw_component_confirmations = confirmations.get(component.id, {})
        safe_confirmations = (
            raw_component_confirmations if isinstance(raw_component_confirmations, Mapping) else {}
        )
        effective, errors, claimed = _apply_elevation_confirmation(
            component,
            occurrence_by_id,
            relation_edges,
            safe_confirmations,
        )
        if errors:
            occurrence_confirmation_errors[component.id] = errors
        explicitly_claimed_occurrences.update(claimed)
        effective_components.append(effective)
    elevation_resolved_components = [
        component
        for component in effective_components
        if component.plan_occurrence_ids
        or not (set(component.elevation_occurrence_ids) & explicitly_claimed_occurrences)
    ]
    components, merge_confirmation_errors, merge_evidence_edges = _apply_component_merges(
        elevation_resolved_components, confirmations
    )
    measurement_index = _MeasurementIndex.build(sheets, entities, occurrences)
    result = TakeoffBuildResult(
        components=components,
        evidence_edges=[*relation_edges, *merge_evidence_edges],
    )
    explicit_merge_pairs = {(edge.source_id, edge.target_id) for edge in merge_evidence_edges}
    for component in components:
        for occurrence_id in [
            *component.plan_occurrence_ids,
            *component.elevation_occurrence_ids,
        ]:
            if (occurrence_id, component.id) in explicit_merge_pairs:
                continue
            result.evidence_edges.append(
                EvidenceEdge(
                    id=_stable_id("edge", occurrence_id, component.id),
                    relation="occurrence_to_component",
                    source_id=occurrence_id,
                    target_id=component.id,
                    basis=[
                        "explicit_reviewer_selection"
                        if occurrence_id in explicitly_claimed_occurrences
                        else "conservative_component_group"
                    ],
                    confidence=1.0 if occurrence_id in explicitly_claimed_occurrences else 0.7,
                    status=(
                        ReviewStatus.PASS
                        if occurrence_id in explicitly_claimed_occurrences
                        else ReviewStatus.REVIEW
                    ),
                )
            )
    confirmed_relation_edge_ids: set[str] = set()
    for sequence, component in enumerate(components, start=1):
        measurements = collect_measurement_candidates(
            component,
            sheets,
            entities,
            occurrences,
            _measurement_index=measurement_index,
        )
        result.measurements.extend(measurements)
        truncated_markers = sorted(
            {
                basis
                for candidate in measurements
                for basis in candidate.basis
                if basis.startswith("candidate_pool_truncated:")
            }
        )
        if truncated_markers:
            result.issues.append(
                RunIssue(
                    stage="takeoff",
                    severity=Severity.WARNING,
                    code="MEASUREMENT_CANDIDATES_TRUNCATED",
                    message=(
                        f"{component.mt_code}: 尺寸候选过多，仅保留每类最近的"
                        f"{_MAX_MEASUREMENT_CANDIDATES_PER_ROLE}条；"
                        "未确认前不得作为工程量。"
                    ),
                    source_id=component.id,
                    evidence=truncated_markers,
                    suggested_action="核对证据图；若正确尺寸未入选，补充构件锚点或缩小图纸范围后重跑",
                )
            )
        selected: dict[str, MeasurementCandidate | None] = {}
        review_reasons: list[str] = []
        block_reasons: list[str] = [
            *occurrence_confirmation_errors.get(component.id, ()),
            *merge_confirmation_errors.get(component.id, ()),
        ]
        raw_confirmations = confirmations.get(component.id, {})
        component_confirmations = (
            raw_confirmations if isinstance(raw_confirmations, Mapping) else {}
        )
        if raw_confirmations and not isinstance(raw_confirmations, Mapping):
            block_reasons.append("component confirmation must be an object")

        unit, pricing_method, commercial_reasons = _resolve_unit_and_pricing_method(
            component_confirmations
        )
        block_reasons.extend(commercial_reasons)
        required_roles = _ROLES_BY_UNIT.get(unit, ())
        roles_to_select = set(required_roles) | {
            role
            for role in ("unfolded_spec", "length", "quantity", "height", "width")
            if role in component_confirmations
        }
        for role in sorted(roles_to_select):
            candidate, reason = _select_candidate(
                measurements,
                role,
                component_confirmations,
            )
            selected[role] = candidate
            if reason:
                if reason.startswith("unconfirmed "):
                    review_reasons.append(reason)
                else:
                    block_reasons.append(reason)

        plan_edge, detail_edge, chain_pass, chain_review, chain_block = _component_chain_edges(
            component,
            occurrence_by_id,
            relation_edges,
            component_confirmations,
        )
        review_reasons.extend(chain_review)
        block_reasons.extend(chain_block)
        if plan_edge is not None and detail_edge is not None:
            expected_sheet_by_role = {
                "length": plan_edge.target_id,
                "height": plan_edge.target_id,
                "quantity": plan_edge.target_id,
                "unfolded_spec": detail_edge.target_id,
                "width": detail_edge.target_id,
            }
            for role, candidate in selected.items():
                expected_sheet = expected_sheet_by_role.get(role)
                if (
                    candidate is not None
                    and expected_sheet is not None
                    and candidate.sheet_id != expected_sheet
                ):
                    block_reasons.append(
                        f"confirmed {role} candidate is outside selected chain: "
                        f"{candidate.id} ({candidate.sheet_id} != {expected_sheet})"
                    )
        if plan_edge is not None and "plan_to_elevation_edge" in component_confirmations:
            confirmed_relation_edge_ids.add(plan_edge.id)
        if detail_edge is not None and "elevation_to_detail_edge" in component_confirmations:
            confirmed_relation_edge_ids.add(detail_edge.id)

        plan_occurrence = next(
            (
                occurrence_by_id[value]
                for value in component.plan_occurrence_ids
                if value in occurrence_by_id
                and (plan_edge is None or occurrence_by_id[value].sheet_id == plan_edge.source_id)
            ),
            None,
        )
        elevation_occurrence = next(
            (
                occurrence_by_id[value]
                for value in component.elevation_occurrence_ids
                if value in occurrence_by_id
                and (plan_edge is None or occurrence_by_id[value].sheet_id == plan_edge.target_id)
            ),
            None,
        )
        plan_sheet = sheet_by_id.get(plan_occurrence.sheet_id) if plan_occurrence else None
        elevation_sheet = (
            sheet_by_id.get(elevation_occurrence.sheet_id) if elevation_occurrence else None
        )
        detail_sheet = (
            sheet_by_id.get(detail_edge.target_id)
            if detail_edge is not None
            else next(
                (
                    sheet_by_id[value]
                    for value in component.detail_sheet_ids
                    if value in sheet_by_id
                ),
                None,
            )
        )
        material, material_reasons = _material_for_component(component.mt_code, materials_by_code)
        block_reasons.extend(material_reasons)
        unfolded = selected.get("unfolded_spec")
        length = selected.get("length")
        quantity = selected.get("quantity")
        confirmed_roles = all(
            selected.get(role) is not None and selected[role].status == ReviewStatus.PASS
            for role in required_roles
        )
        status = (
            ReviewStatus.PASS
            if confirmed_roles and chain_pass and not review_reasons and not block_reasons
            else ReviewStatus.REVIEW
        )
        if block_reasons:
            status = ReviewStatus.BLOCK
        reasons = [*block_reasons, *review_reasons]
        selected_candidates = [
            candidate for candidate in selected.values() if candidate is not None
        ]
        evidence_ids = sorted(
            {entity_id for candidate in selected_candidates for entity_id in candidate.entity_ids}
        )
        item = TakeoffItem(
            sequence=sequence,
            name=component.name or (material.name if material else None) or "不锈钢构件",
            mt_code=component.mt_code,
            material=material.name if material else None,
            plan_location=" / ".join(
                value for value in (component.room, _sheet_label(plan_sheet)) if value
            )
            or None,
            elevation=_sheet_label(elevation_sheet),
            detail=_sheet_label(detail_sheet),
            unfolded_spec=unfolded.raw_value if unfolded else None,
            width_mm=unfolded.numeric_value if unfolded else None,
            length_mm=length.numeric_value if length else None,
            quantity=quantity.numeric_value if quantity else None,
            unit=unit,
            pricing_method=pricing_method,
            note="；".join(reasons) or None,
            component_id=component.id,
            evidence_ids=evidence_ids,
            status=status,
            block_reason="；".join(reasons) if status == ReviewStatus.BLOCK else None,
        )
        calculated = calculate_item(item)
        if status == ReviewStatus.BLOCK:
            calculated = calculated.model_copy(
                update={
                    "status": ReviewStatus.BLOCK,
                    "block_reason": "；".join(block_reasons),
                }
            )
        result.items.append(calculated)
        confirmed_measurement_ids = {
            candidate.id
            for candidate in selected_candidates
            if candidate.status == ReviewStatus.PASS
        }
        for candidate in measurements:
            edge_status = (
                ReviewStatus.PASS
                if candidate.id in confirmed_measurement_ids
                else ReviewStatus.REVIEW
            )
            result.evidence_edges.append(
                EvidenceEdge(
                    id=_stable_id("edge", component.id, candidate.id),
                    relation="component_to_dimension",
                    source_id=component.id,
                    target_id=candidate.id,
                    basis=candidate.basis,
                    confidence=candidate.confidence,
                    status=edge_status,
                )
            )
        if reasons:
            result.issues.append(
                RunIssue(
                    stage="takeoff",
                    severity=(Severity.BLOCK if block_reasons else Severity.WARNING),
                    code="MEASUREMENT_REVIEW_REQUIRED",
                    message=f"{component.mt_code}: {'; '.join(reasons)}",
                    source_id=component.id,
                    evidence=evidence_ids,
                    suggested_action="核对局部证据图并在 confirmations.json 选择候选ID",
                )
            )
    if confirmed_relation_edge_ids:
        result.evidence_edges = [
            edge.model_copy(update={"status": ReviewStatus.PASS})
            if edge.id in confirmed_relation_edge_ids
            else edge
            for edge in result.evidence_edges
        ]
    return result
