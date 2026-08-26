"""Rank conservative cross-sheet evidence candidates.

The output is a shortlist for semantic/visual verification, not an assertion
that two sheets describe the same physical component.  Candidate edges default
to REVIEW.  They can be promoted only when the caller opts in and an explicit
drawing reference (or separately confirmed pair) exists.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import unicodedata
from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from difflib import SequenceMatcher
from statistics import median
from typing import Any

from .materials import normalize_text
from .models import CadEntity, EvidenceEdge, MtOccurrence, ReviewStatus, Sheet
from .mt import entity_center

_REF_RE = re.compile(
    r"(?<![A-Z0-9])(?:[A-Z]{1,4}-){1,2}\d{1,4}(?![A-Z0-9])",
    re.I,
)
_COMPACT_REF_RE = re.compile(
    r"(?<![A-Z0-9])(?:(?P<section>[AB])(?P<kind>[ED])|(?P<prefix>EL|DS|DT|FD|TD|CD|P|M))"
    r"[- ]?(?P<number>\d{1,3})(?![A-Z0-9])",
    re.I,
)
_RANGE_RE = re.compile(
    r"(?P<left>(?:[A-Z]{1,4}-){1,2}\d{1,4})\s*[~～至]\s*"
    r"(?P<right>(?:(?:[A-Z]{1,4}-){1,2})?\d{1,4})",
    re.I,
)
_DETACHED_REF_PREFIX_RE = re.compile(r"^(?:[AB]-[ED]|EL|DS|DT|FD|TD|CD|P|M)-?$", re.I)
_DETACHED_REF_NUMBER_RE = re.compile(r"^0*(\d{1,3})$")
_GENERIC_TITLE_RE = re.compile(
    r"施工图|平面(?:布置|尺寸|索引)?图?|立面(?:索引)?图?|剖面图?|节点图?|大样图?|详图|"
    r"天花|地花|墙身|门表|会所|售楼部|SCALE|比例",
    re.I,
)


def _stable_id(prefix: str, payload: Mapping[str, Any]) -> str:
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return f"{prefix}:{hashlib.sha256(encoded.encode('utf-8')).hexdigest()[:24]}"


def normalize_reference_code(value: Any) -> str | None:
    """Normalize common interior drawing codes (``Ａ－Ｅ－１`` → ``A-E-01``)."""

    text = unicodedata.normalize("NFKC", normalize_text(value)).upper()
    text = re.sub(r"[—–－_:/／\\\s]+", "-", text).strip("-")
    match = re.fullmatch(r"((?:[A-Z]{1,4}-){1,2})(\d{1,4})", text)
    if not match:
        compact = _COMPACT_REF_RE.fullmatch(text.replace("-", ""))
        if not compact:
            return None
        if compact.group("section"):
            prefix = f"{compact.group('section')}-{compact.group('kind')}"
        else:
            prefix = compact.group("prefix") or ""
        number = compact.group("number")
        return f"{prefix.upper()}-{int(number):0{max(2, len(number))}d}"
    prefix, number = match.groups()
    return f"{prefix.rstrip('-')}-{int(number):0{max(2, len(number))}d}"


def _expand_range(left: str, right: str) -> set[str]:
    left_code = normalize_reference_code(left)
    if left_code is None:
        return set()
    left_prefix, left_number = left_code.rsplit("-", 1)
    if re.search(r"[A-Z]", right, re.I):
        right_code = normalize_reference_code(right)
    else:
        right_code = normalize_reference_code(f"{left_prefix}-{right}")
    if right_code is None:
        return {left_code}
    right_prefix, right_number = right_code.rsplit("-", 1)
    if left_prefix != right_prefix:
        return {left_code, right_code}
    start, end = int(left_number), int(right_number)
    if abs(end - start) > 200:
        return {left_code, right_code}
    low, high = sorted((start, end))
    return {f"{left_prefix}-{number:02d}" for number in range(low, high + 1)}


def extract_reference_codes(value: Any) -> set[str]:
    """Extract and expand drawing references such as ``A-E-01~A-E-16``."""

    text = unicodedata.normalize("NFKC", normalize_text(value)).upper()
    text = text.replace("—", "-").replace("–", "-").replace("－", "-")
    result: set[str] = set()
    for match in _RANGE_RE.finditer(text):
        result.update(_expand_range(match.group("left"), match.group("right")))
    for match in _REF_RE.finditer(text):
        code = normalize_reference_code(match.group())
        if code:
            result.add(code)
    for match in _COMPACT_REF_RE.finditer(text):
        code = normalize_reference_code(match.group())
        if code:
            result.add(code)
    return result


def _sheet_aliases(sheet: Sheet) -> set[str]:
    aliases: set[str] = set()
    aliases.update(extract_reference_codes(sheet.drawing_number))
    aliases.update(extract_reference_codes(sheet.title))
    return aliases


def _title_key(sheet: Sheet) -> str:
    title = unicodedata.normalize("NFKC", normalize_text(sheet.title)).upper()
    title = _RANGE_RE.sub(" ", title)
    title = _REF_RE.sub(" ", title)
    title = _COMPACT_REF_RE.sub(" ", title)
    title = _GENERIC_TITLE_RE.sub(" ", title)
    return re.sub(r"[^0-9A-Z\u4e00-\u9fff]+", "", title)


def _similarity(left: str, right: str) -> float:
    if not left or not right:
        return 0.0
    if left == right:
        return 1.0
    if left in right or right in left:
        return min(len(left), len(right)) / max(len(left), len(right))
    return SequenceMatcher(None, left, right).ratio()


def _occurrence_metadata(
    occurrences: Sequence[MtOccurrence],
) -> tuple[dict[str, set[str]], dict[str, set[str]]]:
    mt_by_sheet: dict[str, set[str]] = defaultdict(set)
    rooms_by_sheet: dict[str, set[str]] = defaultdict(set)
    for occurrence in occurrences:
        if not occurrence.sheet_id:
            continue
        mt_by_sheet[occurrence.sheet_id].add(occurrence.mt_code)
        if occurrence.room:
            rooms_by_sheet[occurrence.sheet_id].add(normalize_text(occurrence.room).casefold())
    return mt_by_sheet, rooms_by_sheet


def _reference_pair_radius(entities: Sequence[CadEntity]) -> float:
    heights: list[float] = []
    for entity in entities:
        if entity.bbox:
            height = abs(entity.bbox[3] - entity.bbox[1])
            if height > 0:
                heights.append(height)
        try:
            height = float(entity.geometry.get("height") or 0)
        except (TypeError, ValueError):
            height = 0
        if height > 0:
            heights.append(height)
    return max(1.0, min(80.0, median(heights) * 8.0)) if heights else 80.0


def _detached_reference_pairs(
    entities: Sequence[CadEntity],
) -> list[tuple[str, tuple[str, str]]]:
    """Reconstruct ``A-E`` + ``03`` reference blocks conservatively."""

    by_scope: dict[tuple[str, str | None, str], list[CadEntity]] = defaultdict(list)
    for entity in entities:
        if entity.text:
            by_scope[(entity.source_file_id, entity.sheet_id, entity.space)].append(entity)
    result: list[tuple[str, tuple[str, str]]] = []
    for group in by_scope.values():
        radius = _reference_pair_radius(group)
        prefixes = [
            entity
            for entity in group
            if _DETACHED_REF_PREFIX_RE.fullmatch(normalize_text(entity.text).upper())
        ]
        numbers = [
            entity
            for entity in group
            if _DETACHED_REF_NUMBER_RE.fullmatch(normalize_text(entity.text))
        ]
        candidates: list[tuple[float, str, str, CadEntity, CadEntity]] = []
        for prefix in prefixes:
            prefix_point = entity_center(prefix)
            for number in numbers:
                number_point = entity_center(number)
                if prefix_point is None or number_point is None:
                    continue
                dx = number_point[0] - prefix_point[0]
                dy = abs(number_point[1] - prefix_point[1])
                distance = math.hypot(dx, dy)
                if distance > radius or dy > radius * 0.65 or dx < -radius * 0.35:
                    continue
                candidates.append((distance + dy * 0.5, prefix.id, number.id, prefix, number))
        used_prefixes: set[str] = set()
        used_numbers: set[str] = set()
        for _, _, _, prefix, number in sorted(candidates):
            if prefix.id in used_prefixes or number.id in used_numbers:
                continue
            code = normalize_reference_code(f"{normalize_text(prefix.text)}-{number.text}")
            if code:
                result.append((code, (prefix.id, number.id)))
                used_prefixes.add(prefix.id)
                used_numbers.add(number.id)
    return result


def _confirmed_map(
    confirmed_links: Iterable[tuple[str, str] | Mapping[str, Any]] | Mapping[Any, Any] | None,
) -> dict[tuple[str, str], list[str]]:
    result: dict[tuple[str, str], list[str]] = {}
    if confirmed_links is None:
        return result
    if isinstance(confirmed_links, Mapping):
        iterator: Iterable[Any] = confirmed_links.items()
        for key, value in iterator:
            if isinstance(key, Sequence) and not isinstance(key, (str, bytes)) and len(key) == 2:
                basis = (
                    value
                    if isinstance(value, Sequence) and not isinstance(value, str)
                    else [value]
                )
                result[(str(key[0]), str(key[1]))] = [str(item) for item in basis if item]
        return result
    for item in confirmed_links:
        if isinstance(item, Mapping):
            source = item.get("source_id")
            target = item.get("target_id")
            if source and target:
                basis = item.get("basis") or ["confirmed_link"]
                if isinstance(basis, str):
                    basis = [basis]
                result[(str(source), str(target))] = [str(value) for value in basis]
        elif isinstance(item, Sequence) and not isinstance(item, (str, bytes)) and len(item) == 2:
            result[(str(item[0]), str(item[1]))] = ["confirmed_link"]
    return result


def _rank_relation(
    relation: str,
    source_sheets: Sequence[Sheet],
    target_sheets: Sequence[Sheet],
    *,
    source_entity_refs: Mapping[str, Mapping[str, set[str]]],
    mt_by_sheet: Mapping[str, set[str]],
    rooms_by_sheet: Mapping[str, set[str]],
    confirmed: Mapping[tuple[str, str], list[str]],
    promote_explicit: bool,
    top_k: int,
    minimum_confidence: float,
) -> list[EvidenceEdge]:
    result: list[EvidenceEdge] = []
    for source in sorted(source_sheets, key=lambda sheet: sheet.id):
        ranked: list[EvidenceEdge] = []
        source_mt = mt_by_sheet.get(source.id, set())
        source_rooms = rooms_by_sheet.get(source.id, set())
        source_refs = source_entity_refs.get(source.id, {})
        for target in sorted(target_sheets, key=lambda sheet: sheet.id):
            if source.id == target.id:
                continue
            target_aliases = _sheet_aliases(target)
            explicit_codes = sorted(set(source_refs) & target_aliases)
            pair = (source.id, target.id)
            is_confirmed = pair in confirmed
            basis: list[str] = []
            score = 0.02  # direction/type prior only

            if is_confirmed:
                score = 1.0
                basis.extend(f"confirmed:{item}" for item in confirmed[pair])
            elif explicit_codes:
                score += 0.72
                for code in explicit_codes:
                    handles = ",".join(sorted(source_refs[code]))
                    basis.append(f"explicit_reference:{code}@{handles}")

            common_mt = sorted(source_mt & mt_by_sheet.get(target.id, set()))
            if common_mt:
                union = source_mt | mt_by_sheet.get(target.id, set())
                score += 0.10 + 0.08 * (len(common_mt) / len(union))
                basis.append(f"same_mt:{','.join(common_mt)}")

            target_rooms = rooms_by_sheet.get(target.id, set())
            common_rooms = sorted(source_rooms & target_rooms)
            if common_rooms:
                score += 0.15
                basis.append(f"same_room:{'|'.join(common_rooms)}")
            elif source_rooms and target_rooms:
                best_room = max(
                    (
                        (_similarity(left, right), left, right)
                        for left in source_rooms
                        for right in target_rooms
                    ),
                    default=(0.0, "", ""),
                )
                if best_room[0] >= 0.65:
                    score += 0.08 * best_room[0]
                    basis.append(f"similar_room:{best_room[1]}~{best_room[2]}")

            title_similarity = _similarity(_title_key(source), _title_key(target))
            if title_similarity >= 0.25:
                score += 0.10 * title_similarity
                basis.append(f"title_similarity:{title_similarity:.3f}")

            score = min(1.0, round(score, 6))
            if score < minimum_confidence:
                continue
            # Caller-supplied confirmations are authoritative. Native drawing
            # references remain REVIEW until we know the source has exactly one
            # explicit target; a plan index commonly lists several elevations.
            status = ReviewStatus.PASS if is_confirmed else ReviewStatus.REVIEW
            if not basis:
                basis.append("sheet_type_candidate")
            payload = {
                "relation": relation,
                "source_id": source.id,
                "target_id": target.id,
                "basis": sorted(basis),
                "confidence": score,
            }
            ranked.append(
                EvidenceEdge(
                    id=_stable_id("edge", payload),
                    relation=relation,  # type: ignore[arg-type]
                    source_id=source.id,
                    target_id=target.id,
                    basis=sorted(basis),
                    confidence=score,
                    status=status,
                )
            )
        ranked.sort(key=lambda edge: (-edge.confidence, edge.target_id, edge.id))
        if promote_explicit and not any(edge.status == ReviewStatus.PASS for edge in ranked):
            explicit_candidates = [
                edge
                for edge in ranked
                if any(value.startswith("explicit_reference:") for value in edge.basis)
            ]
            if len(explicit_candidates) == 1:
                explicit_id = explicit_candidates[0].id
                ranked = [
                    edge.model_copy(update={"status": ReviewStatus.PASS})
                    if edge.id == explicit_id
                    else edge
                    for edge in ranked
                ]
        result.extend(ranked[:top_k])
    return result


def rank_evidence_edges(
    sheets: Iterable[Sheet],
    occurrences: Iterable[MtOccurrence] = (),
    entities: Iterable[CadEntity] = (),
    *,
    top_k: int = 5,
    minimum_confidence: float = 0.05,
    promote_explicit: bool = False,
    confirmed_links: (
        Iterable[tuple[str, str] | Mapping[str, Any]] | Mapping[Any, Any] | None
    ) = None,
) -> list[EvidenceEdge]:
    """Rank plan→elevation and elevation→detail candidates.

    ``promote_explicit`` never promotes a similarity-only edge.  PASS requires
    an exact reference code backed by source entity IDs, or a caller-supplied
    confirmed pair.
    """

    if top_k < 1:
        raise ValueError("top_k must be at least 1")
    sheet_list = list(sheets)
    occurrence_list = list(occurrences)
    mt_by_sheet, rooms_by_sheet = _occurrence_metadata(occurrence_list)

    source_entity_refs: dict[str, dict[str, set[str]]] = defaultdict(lambda: defaultdict(set))
    entity_list = list(entities)
    for entity in entity_list:
        if not entity.sheet_id or not entity.text:
            continue
        for code in extract_reference_codes(entity.text):
            source_entity_refs[entity.sheet_id][code].add(entity.id)
    entity_by_id = {entity.id: entity for entity in entity_list}
    for code, entity_ids in _detached_reference_pairs(entity_list):
        sheet_id = entity_by_id[entity_ids[0]].sheet_id
        if sheet_id:
            source_entity_refs[sheet_id][code].update(entity_ids)

    confirmed = _confirmed_map(confirmed_links)
    plans = [sheet for sheet in sheet_list if sheet.kind in {"plan", "elevation_index"}]
    elevations = [sheet for sheet in sheet_list if sheet.kind == "elevation"]
    details = [
        sheet
        for sheet in sheet_list
        if sheet.kind in {"detail", "door", "ceiling", "floor"}
    ]

    edges = _rank_relation(
        "plan_to_elevation",
        plans,
        elevations,
        source_entity_refs=source_entity_refs,
        mt_by_sheet=mt_by_sheet,
        rooms_by_sheet=rooms_by_sheet,
        confirmed=confirmed,
        promote_explicit=promote_explicit,
        top_k=top_k,
        minimum_confidence=minimum_confidence,
    )
    edges.extend(
        _rank_relation(
            "elevation_to_detail",
            elevations,
            details,
            source_entity_refs=source_entity_refs,
            mt_by_sheet=mt_by_sheet,
            rooms_by_sheet=rooms_by_sheet,
            confirmed=confirmed,
            promote_explicit=promote_explicit,
            top_k=top_k,
            minimum_confidence=minimum_confidence,
        )
    )
    return sorted(
        edges,
        key=lambda edge: (edge.relation, edge.source_id, -edge.confidence, edge.target_id),
    )


def rank_plan_to_elevation_edges(
    sheets: Iterable[Sheet],
    occurrences: Iterable[MtOccurrence] = (),
    entities: Iterable[CadEntity] = (),
    **kwargs: Any,
) -> list[EvidenceEdge]:
    return [
        edge
        for edge in rank_evidence_edges(sheets, occurrences, entities, **kwargs)
        if edge.relation == "plan_to_elevation"
    ]


def rank_elevation_to_detail_edges(
    sheets: Iterable[Sheet],
    occurrences: Iterable[MtOccurrence] = (),
    entities: Iterable[CadEntity] = (),
    **kwargs: Any,
) -> list[EvidenceEdge]:
    return [
        edge
        for edge in rank_evidence_edges(sheets, occurrences, entities, **kwargs)
        if edge.relation == "elevation_to_detail"
    ]


__all__ = [
    "extract_reference_codes",
    "normalize_reference_code",
    "rank_elevation_to_detail_edges",
    "rank_evidence_edges",
    "rank_plan_to_elevation_edges",
]
