"""Spatial MT occurrence detection for normalized CAD entities.

This stage finds *annotations*, not physical quantities.  Ten ``MT-01`` labels
are ten occurrences until later evidence proves how many physical components
they describe.  All detections therefore start in REVIEW.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from statistics import median
from typing import Any

from .materials import (
    DEFAULT_REVIEW_CODE_FAMILIES,
    DEFAULT_STAINLESS_CODE_FAMILIES,
    find_material_codes,
    material_code_disposition,
    normalize_material_code_family,
    normalize_mt_code,
    normalize_text,
)
from .models import (
    CadEntity,
    EvidenceEdge,
    MaterialMention,
    MaterialSpec,
    MtOccurrence,
    ReviewStatus,
)

_TEXT_TYPES = {"TEXT", "MTEXT", "ATTRIB", "ATTDEF", "MULTILEADER", "MLEADER"}
_LEADER_TYPES = {"LEADER", "MLEADER", "MULTILEADER"}
_NUMBER_RE = re.compile(r"^\s*0*(\d{1,3})\s*$")
_STAINLESS_RE = re.compile(r"不锈钢", re.I)
_UNNUMBERED_METAL_RE = re.compile(r"金属|铁艺|铝合金|铝板|铜板|铁板|钢板|钛金", re.I)
_MATERIAL_KEYWORD_RE = re.compile(
    r"不锈钢|钢板|金属|铝板|铝合金|铜板|铁板|钛金|玫瑰金|镜面|拉丝|喷砂|"
    r"苹果砂|古铜|烤漆|镀色|雕花|蚀刻|铁艺|扁铁"
)
_GENERIC_MATERIAL_ALIAS_TERMS = {
    "不锈钢",
    "钢板",
    "金属",
    "铝板",
    "铝合金",
    "铜板",
    "铁板",
    "镜面",
    "拉丝",
    "喷砂",
    "烤漆",
    "镀色",
    "雕花",
    "蚀刻",
}
_ROOM_RE = re.compile(
    r"(?:房|厅|室|区|走廊|过道|前厅|大堂|会所|售楼部|卫生间|洗手间|茶室|"
    r"瑜伽|水吧|书吧|接待|洽谈|会议|办公室|门厅)"
)
_COMPONENT_RE = re.compile(
    r"踢脚|脚线|顶线|线条|门套|窗套|包板|收口|嵌条|墙面|顶面|天花|屏风|柜|台|"
    r"造型|设备带|壁炉|壁龛|灯槽|镜框|吊架|挂架|按钮板|旋转门|推拉门|门扇|"
    r"背景板|背景墙|背板|侧板|层板|酒柜|隔断|包边|扶手|层架|挂衣杆|栏杆|"
    r"栏板|压条|雕花|镜子|银镜|玻璃"
)
_DRAWING_TITLE_RE = re.compile(
    r"(?:平面|立面|剖面|大样|节点|示意|索引|放样|天花)[^,，。；;]{0,12}图|图纸目录"
)
_DRAWING_TITLE_SUFFIX_RE = re.compile(
    r"(?:正|背|侧|左|右)?(?:平面|立面|剖面|大样|节点|示意|索引|放样|天花)"
    r"[^,，。；;]{0,12}图$"
)


@dataclass(frozen=True)
class TextCluster:
    """A deterministic group of spatially adjacent text entities."""

    entity_ids: tuple[str, ...]
    text: str
    center: tuple[float, float] | None


@dataclass(frozen=True)
class _Seed:
    code: str
    raw_code: str
    family: str
    disposition: str
    entities: tuple[CadEntity, ...]
    confidence: float
    method: str


@dataclass(frozen=True)
class _MaterialPhraseIndex:
    exact: Mapping[str, set[str]]
    aliases: Mapping[str, set[str]]


def clean_cad_text(value: Any) -> str:
    """Remove common MTEXT control codes while preserving visible content."""

    text = normalize_text(value)
    text = text.replace("\\P", " ").replace("\\p", " ").replace("\\~", " ")
    text = re.sub(r"\\[AaCcFfHhLlOoQqTtWw][^;]*;", "", text)
    text = text.replace("{", "").replace("}", "")
    return re.sub(r"\s+", " ", text).strip()


def contains_material_keyword(value: Any) -> bool:
    return bool(_MATERIAL_KEYWORD_RE.search(clean_cad_text(value)))


def _component_label(value: Any) -> str | None:
    """Return a component phrase, including the semantic part of a view title."""

    text = clean_cad_text(value)
    if not text or len(text) > 36:
        return None
    text = re.sub(
        r"\s*SCALE\s*[:：]?\s*1\s*[/：:]\s*\d+\s*$",
        "",
        text,
        flags=re.I,
    ).strip()
    if _DRAWING_TITLE_RE.search(text):
        text = _DRAWING_TITLE_SUFFIX_RE.sub("", text).strip()
    if not text or not _COMPONENT_RE.search(text):
        return None
    return text


def _point(value: Any) -> tuple[float, float] | None:
    if isinstance(value, Mapping):
        if "x" in value and "y" in value:
            try:
                return float(value["x"]), float(value["y"])
            except (TypeError, ValueError):
                return None
        return None
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)) and len(value) >= 2:
        try:
            return float(value[0]), float(value[1])
        except (TypeError, ValueError):
            return None
    return None


def entity_center(entity: CadEntity) -> tuple[float, float] | None:
    if entity.insert is not None:
        return float(entity.insert[0]), float(entity.insert[1])
    if entity.bbox is not None:
        x1, y1, x2, y2 = entity.bbox
        return (float(x1 + x2) / 2.0, float(y1 + y2) / 2.0)
    for key in ("text_location", "label_point", "insert", "position", "location"):
        point = _point(entity.geometry.get(key))
        if point is not None:
            return point
    return None


def _distance(left: tuple[float, float] | None, right: tuple[float, float] | None) -> float:
    if left is None or right is None:
        return math.inf
    return math.hypot(left[0] - right[0], left[1] - right[1])


def _stable_id(prefix: str, payload: Mapping[str, Any]) -> str:
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return f"{prefix}:{hashlib.sha256(encoded.encode('utf-8')).hexdigest()[:24]}"


def _inferred_distance(entities: Sequence[CadEntity], fallback: float = 80.0) -> float:
    heights: list[float] = []
    for entity in entities:
        if entity.bbox:
            height = abs(entity.bbox[3] - entity.bbox[1])
            if height > 0:
                heights.append(float(height))
        for key in ("height", "text_height", "char_height"):
            try:
                height = float(entity.geometry.get(key, 0))
            except (TypeError, ValueError):
                height = 0
            if height > 0:
                heights.append(height)
    if heights:
        return max(1.0, min(fallback, median(heights) * 8.0))
    return fallback


def cluster_nearby_text(
    entities: Iterable[CadEntity], *, max_distance: float | None = None
) -> list[TextCluster]:
    """Cluster adjacent text within each source/sheet/space.

    This utility is useful for reconstructing labels split across TEXT and
    ATTRIB entities.  The detector below remains seed-centric so that two close
    but distinct MT callouts are not accidentally collapsed.
    """

    text_entities = [
        entity for entity in entities if entity.text and entity.entity_type.upper() in _TEXT_TYPES
    ]
    by_scope: dict[tuple[str, str | None, str], list[CadEntity]] = defaultdict(list)
    for entity in text_entities:
        by_scope[(entity.source_file_id, entity.sheet_id, entity.space)].append(entity)

    clusters: list[TextCluster] = []
    for scope in sorted(by_scope):
        group = sorted(by_scope[scope], key=lambda entity: entity.id)
        radius = max_distance if max_distance is not None else _inferred_distance(group)
        parents = list(range(len(group)))

        def find(index: int, parent_nodes: list[int] = parents) -> int:
            while parent_nodes[index] != index:
                parent_nodes[index] = parent_nodes[parent_nodes[index]]
                index = parent_nodes[index]
            return index

        def union(
            left: int,
            right: int,
            parent_nodes: list[int] = parents,
            find_node: Any = find,
        ) -> None:
            left_root, right_root = find_node(left), find_node(right)
            if left_root != right_root:
                parent_nodes[max(left_root, right_root)] = min(left_root, right_root)

        for left in range(len(group)):
            for right in range(left + 1, len(group)):
                if _distance(entity_center(group[left]), entity_center(group[right])) <= radius:
                    union(left, right)

        members: dict[int, list[CadEntity]] = defaultdict(list)
        for index, entity in enumerate(group):
            members[find(index)].append(entity)
        for member_group in members.values():
            ordered = sorted(
                member_group,
                key=lambda entity: (
                    -(entity_center(entity) or (0.0, 0.0))[1],
                    (entity_center(entity) or (0.0, 0.0))[0],
                    entity.id,
                ),
            )
            points = [point for entity in ordered if (point := entity_center(entity)) is not None]
            center = None
            if points:
                center = (
                    sum(point[0] for point in points) / len(points),
                    sum(point[1] for point in points) / len(points),
                )
            clusters.append(
                TextCluster(
                    entity_ids=tuple(sorted(entity.id for entity in member_group)),
                    text=" ".join(clean_cad_text(entity.text) for entity in ordered),
                    center=center,
                )
            )
    return sorted(clusters, key=lambda cluster: cluster.entity_ids)


def _detached_seeds(
    group: Sequence[CadEntity],
    radius: float,
    *,
    stainless_families: Iterable[str],
    review_families: Iterable[str],
) -> list[_Seed]:
    family_entities: list[tuple[CadEntity, str, str]] = []
    for entity in group:
        family = normalize_material_code_family(clean_cad_text(entity.text))
        disposition = material_code_disposition(
            family,
            stainless_families=stainless_families,
            review_families=review_families,
        )
        if family and disposition:
            family_entities.append((entity, family, disposition))
    number_entities = [
        entity for entity in group if _NUMBER_RE.fullmatch(clean_cad_text(entity.text))
    ]
    candidates: list[tuple[float, str, str, CadEntity, str, str, CadEntity]] = []
    for family_entity, family, disposition in family_entities:
        family_point = entity_center(family_entity)
        for number_entity in number_entities:
            number_point = entity_center(number_entity)
            distance = _distance(family_point, number_point)
            if distance > radius:
                continue
            if family_point is not None and number_point is not None:
                dx = number_point[0] - family_point[0]
                dy = abs(number_point[1] - family_point[1])
                # Callout codes are normally on the same baseline, with the
                # number to the right.  Below/right is tolerated for blocks.
                if dy > radius * 0.65 or dx < -radius * 0.35:
                    continue
                orientation_penalty = dy * 0.5 + (abs(dx) * 0.05 if dx >= 0 else radius)
            else:
                orientation_penalty = 0.0
            candidates.append(
                (
                    distance + orientation_penalty,
                    family_entity.id,
                    number_entity.id,
                    family_entity,
                    family,
                    disposition,
                    number_entity,
                )
            )

    used_families: set[str] = set()
    used_numbers: set[str] = set()
    seeds: list[_Seed] = []
    for _, _, _, family_entity, family, disposition, number_entity in sorted(candidates):
        if family_entity.id in used_families or number_entity.id in used_numbers:
            continue
        match = _NUMBER_RE.fullmatch(clean_cad_text(number_entity.text))
        assert match is not None
        width = max(2, len(match.group(1)))
        code = f"{family}-{int(match.group(1)):0{width}d}"
        confidence = 0.80 if disposition == "stainless" else 0.56
        seeds.append(
            _Seed(
                code=code,
                raw_code=f"{clean_cad_text(family_entity.text)} "
                f"{clean_cad_text(number_entity.text)}",
                family=family,
                disposition=disposition,
                entities=(family_entity, number_entity),
                confidence=confidence,
                method="detached_text",
            )
        )
        used_families.add(family_entity.id)
        used_numbers.add(number_entity.id)
    return seeds


def _material_phrases(materials: Sequence[MaterialSpec]) -> _MaterialPhraseIndex:
    phrases: dict[str, set[str]] = defaultdict(set)
    aliases: dict[str, set[str]] = defaultdict(set)
    for material in materials:
        for value in (material.name, material.finish):
            text = clean_cad_text(value)
            compact = re.sub(r"[\s,，。()（）/]+", "", text).casefold()
            if len(compact) >= 3 and contains_material_keyword(compact):
                phrases[compact].add(material.mt_code)
            for term in _MATERIAL_KEYWORD_RE.findall(text):
                alias = term.casefold()
                if alias not in _GENERIC_MATERIAL_ALIAS_TERMS:
                    aliases[alias].add(material.mt_code)
    return _MaterialPhraseIndex(exact=phrases, aliases=aliases)


def _match_material_code(text: str, phrases: _MaterialPhraseIndex) -> str | None:
    compact = re.sub(r"[\s,，。()（）/]+", "", clean_cad_text(text)).casefold()
    matches: set[str] = set()
    for phrase, codes in phrases.exact.items():
        if phrase in compact or compact in phrase:
            matches.update(codes)
    if len(matches) == 1:
        return next(iter(matches))
    if matches:
        return None

    alias_matches: set[str] = set()
    for alias, codes in phrases.aliases.items():
        if alias in compact and len(codes) == 1:
            alias_matches.update(codes)
    return next(iter(alias_matches)) if len(alias_matches) == 1 else None


def _nearest_descriptor(
    anchor: tuple[float, float] | None,
    text_entities: Sequence[CadEntity],
    radius: float,
) -> CadEntity | None:
    candidates = [
        (_distance(anchor, entity_center(entity)), entity.id, entity)
        for entity in text_entities
        if contains_material_keyword(entity.text)
        and _distance(anchor, entity_center(entity)) <= radius
    ]
    return min(candidates)[2] if candidates else None


def _same_callout_component_descriptor(
    seed_entities: Sequence[CadEntity],
    text_entities: Sequence[CadEntity],
    material_family: str,
) -> CadEntity | None:
    """Return a component description stored in the same attributed callout block.

    CAD callout blocks often place the material code and its visible description
    in sibling ``ATTRIB`` entities.  Their drawing coordinates can be much farther
    apart than the normal text-clustering radius after viewport projection, while
    ``parent_insert_handle`` is an exact annotation identity.  Prefer this
    structural binding, but still require a component noun so a finish such as
    ``古铜色不锈钢`` is not mislabeled as the physical object.
    """

    parent_handles = {
        str(entity.geometry.get("parent_insert_handle") or "").strip()
        for entity in seed_entities
        if str(entity.geometry.get("parent_insert_handle") or "").strip()
    }
    if not parent_handles:
        return None
    seed_ids = {entity.id for entity in seed_entities}
    candidates: list[tuple[str, CadEntity]] = []
    for entity in text_entities:
        if entity.id in seed_ids:
            continue
        parent = str(entity.geometry.get("parent_insert_handle") or "").strip()
        label = _component_label(entity.text)
        # Attributed annotation blocks are often copied without updating every
        # default field.  A bare mirror/glass description is strong for its own
        # material family but is unsafe for a stainless-steel callout; phrases
        # such as 镜框 or 玻璃包边 remain valid because they name the metal part.
        incompatible_bare_finish = (
            material_family in DEFAULT_STAINLESS_CODE_FAMILIES
            and label is not None
            and re.fullmatch(r"(?:灰色)?镜子(?:\(加防爆膜\))?|(?:渐变|长虹|艺术)*玻璃", label)
            is not None
        )
        if parent in parent_handles and label is not None and not incompatible_bare_finish:
            candidates.append((entity.id, entity))
    return min(candidates)[1] if candidates else None


def _nearest_room(
    anchor: tuple[float, float] | None,
    text_entities: Sequence[CadEntity],
    radius: float,
) -> str | None:
    candidates: list[tuple[float, str, str]] = []
    for entity in text_entities:
        text = clean_cad_text(entity.text)
        if not text or contains_material_keyword(text) or not _ROOM_RE.search(text):
            continue
        distance = _distance(anchor, entity_center(entity))
        if distance <= radius:
            candidates.append((distance, entity.id, text))
    return min(candidates)[2] if candidates else None


def _extract_leader_points(
    entity: CadEntity,
) -> tuple[tuple[float, float] | None, tuple[float, float] | None, bool]:
    geometry = entity.geometry
    target = None
    label = None
    raw_targets = geometry.get("leader_targets")
    explicit_targets: list[tuple[float, float]] = []
    if isinstance(raw_targets, Sequence) and not isinstance(raw_targets, (str, bytes)):
        for raw_target in raw_targets:
            parsed = _point(raw_target)
            if parsed is not None and parsed not in explicit_targets:
                explicit_targets.append(parsed)
    target_is_ambiguous = len(explicit_targets) > 1
    if len(explicit_targets) == 1:
        target = explicit_targets[0]
    elif not target_is_ambiguous:
        for key in ("leader_target", "target", "arrowhead", "arrow_point", "arrow", "tip"):
            target = _point(geometry.get(key))
            if target is not None:
                break
    for key in ("label_point", "landing", "landing_point", "text_location", "text_point"):
        label = _point(geometry.get(key))
        if label is not None:
            break
    vertices: list[tuple[float, float]] = []
    for key in ("vertices", "points", "leader_vertices", "line_points"):
        raw = geometry.get(key)
        if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes)):
            continue
        vertices = [point for item in raw if (point := _point(item)) is not None]
        if vertices:
            break
    if vertices:
        if target is None and not target_is_ambiguous:
            target = vertices[0]
        if label is None:
            label = vertices[-1]
    elif label is None:
        label = entity_center(entity)
    return label, target, target_is_ambiguous


def _bind_leader(
    seed_entities: Sequence[CadEntity],
    seed_code: str,
    seed_family: str,
    anchor: tuple[float, float] | None,
    leaders: Sequence[CadEntity],
    radius: float,
) -> tuple[CadEntity | None, tuple[float, float] | None]:
    seed_ids = {entity.id for entity in seed_entities}
    candidates: list[tuple[float, str, CadEntity, tuple[float, float] | None]] = []
    for leader in leaders:
        label, target, target_is_ambiguous = _extract_leader_points(leader)
        own_annotation = leader.id in seed_ids or bool(
            seed_code
            in {
                match.normalized_code
                for match in find_material_codes(
                    leader.text or "",
                    stainless_families={seed_family},
                    review_families=(),
                )
            }
        )
        distance = 0.0 if own_annotation else _distance(anchor, label)
        if distance <= radius:
            # For bare LEADER polylines, the endpoint nearest the annotation is
            # the landing; the opposite endpoint is the arrow target.
            vertices = leader.geometry.get("vertices") or leader.geometry.get("points")
            if (
                not own_annotation
                and not target_is_ambiguous
                and isinstance(vertices, Sequence)
                and len(vertices) >= 2
            ):
                points = [point for item in vertices if (point := _point(item)) is not None]
                if len(points) >= 2 and anchor is not None:
                    target = max(points[0], points[-1], key=lambda point: _distance(anchor, point))
            candidates.append((distance, leader.id, leader, target))
    if not candidates:
        return None, None
    _, _, leader, target = min(candidates)
    return leader, target


def _occurrence_from_seed(
    seed: _Seed,
    *,
    group: Sequence[CadEntity],
    radius: float,
    room_radius: float,
    leader_radius: float,
) -> MtOccurrence:
    text_entities = [entity for entity in group if entity.text]
    points = [point for entity in seed.entities if (point := entity_center(entity)) is not None]
    anchor = None
    if points:
        anchor = (
            sum(point[0] for point in points) / len(points),
            sum(point[1] for point in points) / len(points),
        )
    descriptor = _nearest_descriptor(anchor, text_entities, radius)
    entities = list(seed.entities)
    if descriptor is not None and descriptor.id not in {entity.id for entity in entities}:
        entities.append(descriptor)
    callout_component = _same_callout_component_descriptor(
        seed.entities,
        text_entities,
        seed.family,
    )
    leaders = [entity for entity in group if entity.entity_type.upper() in _LEADER_TYPES]
    leader, target = _bind_leader(
        entities,
        seed.code,
        seed.family,
        anchor,
        leaders,
        leader_radius,
    )
    if leader is not None and leader.id not in {entity.id for entity in entities}:
        entities.append(leader)

    room = _nearest_room(anchor, text_entities, room_radius)
    # A plain material phrase (for example "古铜色不锈钢") describes the MT
    # specification, not the physical component name. Prefer nearby text that
    # actually names a component; retain the material descriptor only when it
    # also contains a component noun such as 踢脚/门套/收口.
    component_candidates: list[tuple[float, str, str]] = []
    if callout_component is not None:
        callout_label = _component_label(callout_component.text)
        if callout_label is not None:
            component_candidates.append((-1.0, callout_component.id, callout_label))
    # The arrow endpoint is often closer to the physical component label than
    # the MT text itself. Search it first, then the annotation anchor. A wider
    # but still local radius recovers labels such as “社区门厅顶线” without
    # promoting a drawing title like “社区门厅立面图” to a component name.
    for priority, search_point in enumerate((target, anchor)):
        if search_point is None:
            continue
        for entity in text_entities:
            text = clean_cad_text(entity.text)
            component_label = _component_label(text)
            distance = _distance(search_point, entity_center(entity))
            if (
                component_label is None
                or distance > radius * 6.0
            ):
                continue
            component_candidates.append(
                (distance + priority * radius * 0.15, entity.id, component_label)
            )
    component_hint = min(component_candidates)[2] if component_candidates else None
    if (
        component_hint is None
        and descriptor is not None
        and _component_label(descriptor.text) is not None
    ):
        component_hint = _component_label(descriptor.text)

    confidence = seed.confidence
    if descriptor is not None:
        confidence += 0.04
    if leader is not None and target is not None:
        confidence += 0.05
    confidence = min(0.98, confidence)

    source = seed.entities[0]
    entity_ids = sorted({entity.id for entity in entities})
    rounded_anchor = tuple(round(value, 6) for value in anchor) if anchor is not None else None
    rounded_target = tuple(round(value, 6) for value in target) if target is not None else None
    payload = {
        "mt_code": seed.code,
        "source_file_id": source.source_file_id,
        "sheet_id": source.sheet_id,
        "entity_ids": entity_ids,
        "anchor": rounded_anchor,
        "leader_entity_id": leader.id if leader else None,
        "leader_target": rounded_target,
    }
    return MtOccurrence(
        id=_stable_id("mt", payload),
        mt_code=seed.code,
        raw_material_code=seed.raw_code,
        material_code_family=seed.family,
        source_file_id=source.source_file_id,
        sheet_id=source.sheet_id,
        entity_ids=entity_ids,
        anchor=rounded_anchor,
        leader_entity_id=leader.id if leader else None,
        leader_target=rounded_target,
        room=room,
        component_hint=component_hint,
        confidence=confidence,
        status=ReviewStatus.REVIEW,
    )


def deduplicate_occurrences(
    occurrences: Iterable[MtOccurrence], *, anchor_tolerance: float = 1e-6
) -> list[MtOccurrence]:
    """Merge only records that share source evidence or an identical anchor.

    Spatially close labels are intentionally retained: they may identify two
    different physical components even when they carry the same MT code.
    """

    output: list[MtOccurrence] = []
    for occurrence in sorted(occurrences, key=lambda item: item.id):
        match_index = None
        for index, existing in enumerate(output):
            if (
                existing.mt_code != occurrence.mt_code
                or existing.source_file_id != occurrence.source_file_id
                or existing.sheet_id != occurrence.sheet_id
            ):
                continue
            # A nearby descriptor can legitimately be shared by two separate
            # callouts.  Merge evidence only when the complete evidence set is
            # identical; a partial overlap is not proof of duplication.
            shared_evidence = set(existing.entity_ids) == set(occurrence.entity_ids)
            shared_leader = bool(
                existing.leader_entity_id
                and existing.leader_entity_id == occurrence.leader_entity_id
            )
            same_anchor = (
                existing.anchor is not None
                and occurrence.anchor is not None
                and _distance(existing.anchor, occurrence.anchor) <= anchor_tolerance
            )
            if shared_evidence or shared_leader or same_anchor:
                match_index = index
                break
        if match_index is None:
            output.append(occurrence)
            continue
        existing = output[match_index]
        entity_ids = sorted(set(existing.entity_ids) | set(occurrence.entity_ids))
        winner = max((existing, occurrence), key=lambda item: (item.confidence, item.id))
        payload = {
            "mt_code": winner.mt_code,
            "source_file_id": winner.source_file_id,
            "sheet_id": winner.sheet_id,
            "entity_ids": entity_ids,
            "anchor": winner.anchor,
            "leader_entity_id": winner.leader_entity_id,
            "leader_target": winner.leader_target,
        }
        output[match_index] = winner.model_copy(
            update={"id": _stable_id("mt", payload), "entity_ids": entity_ids}
        )
    return sorted(output, key=lambda item: (item.source_file_id, item.sheet_id or "", item.id))


def detect_material_mentions(
    entities: Iterable[CadEntity],
    *,
    occurrences: Iterable[MtOccurrence] = (),
) -> list[MaterialMention]:
    """Retain unnumbered stainless/metal component text, never fabricate MT rows."""

    used_entity_ids = {
        entity_id for occurrence in occurrences for entity_id in occurrence.entity_ids
    }
    mentions: list[MaterialMention] = []
    seen_source_entities: set[tuple[str, str]] = set()
    for entity in sorted(entities, key=lambda value: value.id):
        if (
            entity.id in used_entity_ids
            or not entity.text
            or entity.entity_type.upper() not in _TEXT_TYPES
        ):
            continue
        text = clean_cad_text(entity.text)
        is_stainless = bool(_STAINLESS_RE.search(text))
        is_uncoded_metal_component = bool(
            _UNNUMBERED_METAL_RE.search(text) and _COMPONENT_RE.search(text)
        )
        if (not is_stainless and not is_uncoded_metal_component) or find_material_codes(text):
            continue
        original_id = str(entity.geometry.get("original_entity_id") or entity.id)
        identity = (entity.source_file_id, original_id)
        if identity in seen_source_entities:
            continue
        seen_source_entities.add(identity)
        anchor = entity_center(entity)
        rounded_anchor = tuple(round(value, 6) for value in anchor) if anchor is not None else None
        payload = {
            "raw_text": text,
            "source_file_id": entity.source_file_id,
            "sheet_id": entity.sheet_id,
            "entity_ids": [entity.id],
            "anchor": rounded_anchor,
        }
        mentions.append(
            MaterialMention(
                id=_stable_id("material-mention", payload),
                raw_text=text,
                source_file_id=entity.source_file_id,
                sheet_id=entity.sheet_id,
                entity_ids=[entity.id],
                anchor=rounded_anchor,
                confidence=(
                    0.38
                    if is_stainless and _COMPONENT_RE.search(text)
                    else 0.36 if is_uncoded_metal_component else 0.32
                ),
                status=ReviewStatus.REVIEW,
                reason=(
                    "metal component description has no recognized material code"
                    if is_uncoded_metal_component and not is_stainless
                    else "stainless material description has no recognized material code"
                ),
            )
        )
    return mentions


def link_docx_material_mentions(
    mentions: Iterable[MaterialMention],
    materials: Iterable[MaterialSpec],
    *,
    stainless_code_families: Iterable[str] | None = None,
) -> tuple[list[EvidenceEdge], list[MtOccurrence]]:
    """Create auditable REVIEW candidates from DOCX descriptions to CAD mentions.

    Only records explicitly parsed from a DOCX material book participate. A
    normalized description must resolve to exactly one configured stainless code.
    Ambiguous descriptions produce BLOCK candidate edges and never occurrences.
    """

    stainless_families = tuple(
        DEFAULT_STAINLESS_CODE_FAMILIES
        if stainless_code_families is None
        else stainless_code_families
    )
    phrase_specs: dict[str, list[MaterialSpec]] = defaultdict(list)
    for material in materials:
        family = material.material_code_family or material.mt_code.rsplit("-", 1)[0]
        if (
            material.source_type != "docx_material_book"
            or material_code_disposition(
                family,
                stainless_families=stainless_families,
                review_families=(),
            )
            != "stainless"
        ):
            continue
        phrase = re.sub(
            r"[\s,，。()（）/|｜:：;；._\-—–]+",
            "",
            clean_cad_text(material.name),
        ).casefold()
        if len(phrase) >= 3:
            phrase_specs[phrase].append(material)

    edges: list[EvidenceEdge] = []
    occurrences: list[MtOccurrence] = []
    for mention in sorted(mentions, key=lambda value: value.id):
        compact = re.sub(
            r"[\s,，。()（）/|｜:：;；._\-—–]+",
            "",
            clean_cad_text(mention.raw_text),
        ).casefold()
        matched_specs = {
            material.id: material
            for phrase, values in phrase_specs.items()
            if phrase in compact or compact in phrase
            for material in values
        }
        by_code: dict[str, list[MaterialSpec]] = defaultdict(list)
        for material in matched_specs.values():
            by_code[material.mt_code].append(material)
        if not by_code:
            continue

        candidate_codes = sorted(by_code)
        unique_code = candidate_codes[0] if len(candidate_codes) == 1 else None
        blocked_definition = bool(
            unique_code
            and any(
                material.status == ReviewStatus.BLOCK or material.conflicts
                for material in by_code[unique_code]
            )
        )
        relation_status = (
            ReviewStatus.REVIEW
            if unique_code is not None and not blocked_definition
            else ReviewStatus.BLOCK
        )
        codes_for_edges = [unique_code] if unique_code is not None else candidate_codes
        for code in codes_for_edges:
            assert code is not None
            candidates = sorted(by_code[code], key=lambda value: value.id)
            target = candidates[0]
            payload = {
                "relation": "material_mention_to_material",
                "mention_id": mention.id,
                "material_code": code,
                "material_spec_ids": [value.id for value in candidates],
                "candidate_codes": candidate_codes,
            }
            basis = [
                "source_type:docx_material_book",
                f"material_code:{code}",
                (
                    "description_normalized_unique"
                    if unique_code is not None
                    else "description_normalized_ambiguous"
                ),
                f"candidate_codes:{','.join(candidate_codes)}",
                *(
                    f"document_sha256:{value.source_sha256}"
                    for value in candidates
                    if value.source_sha256
                ),
                *(
                    f"source_location:{value.source_location}"
                    for value in candidates
                    if value.source_location
                ),
            ]
            edges.append(
                EvidenceEdge(
                    id=_stable_id("edge", payload),
                    relation="material_mention_to_material",
                    source_id=mention.id,
                    target_id=target.id,
                    basis=list(dict.fromkeys(basis)),
                    confidence=0.46 if relation_status == ReviewStatus.REVIEW else 0.28,
                    status=relation_status,
                )
            )

        if unique_code is None or blocked_definition:
            continue
        representative = sorted(by_code[unique_code], key=lambda value: value.id)[0]
        family = representative.material_code_family or unique_code.rsplit("-", 1)[0]
        occurrence_payload = {
            "material_mention_id": mention.id,
            "mt_code": unique_code,
            "material_spec_ids": sorted(value.id for value in by_code[unique_code]),
        }
        occurrences.append(
            MtOccurrence(
                id=_stable_id("mt-docx-candidate", occurrence_payload),
                mt_code=unique_code,
                raw_material_code=representative.raw_material_code or unique_code,
                material_code_family=family,
                source_file_id=mention.source_file_id,
                sheet_id=mention.sheet_id,
                entity_ids=list(mention.entity_ids),
                anchor=mention.anchor,
                component_hint=(
                    mention.raw_text if _COMPONENT_RE.search(mention.raw_text) else None
                ),
                confidence=0.44,
                status=ReviewStatus.REVIEW,
            )
        )
    return (
        sorted(edges, key=lambda value: value.id),
        deduplicate_occurrences(occurrences),
    )


def detect_mt_occurrences(
    entities: Iterable[CadEntity],
    *,
    materials: Iterable[MaterialSpec] = (),
    cluster_distance: float | None = None,
    leader_bind_distance: float | None = None,
    room_search_distance: float | None = None,
    stainless_code_families: Iterable[str] | None = None,
    review_code_families: Iterable[str] | None = None,
) -> list[MtOccurrence]:
    """Detect configured stainless/review material-code annotations.

    ``MT`` and ``GC-SS`` are stainless families by default. ``GC-MT`` remains
    a lower-confidence review candidate unless callers explicitly move it into
    ``stainless_code_families``. Other families are ignored unless configured.
    """

    all_entities = list(entities)
    material_list = list(materials)
    stainless_families = tuple(
        DEFAULT_STAINLESS_CODE_FAMILIES
        if stainless_code_families is None
        else stainless_code_families
    )
    review_families = tuple(
        DEFAULT_REVIEW_CODE_FAMILIES if review_code_families is None else review_code_families
    )
    by_scope: dict[tuple[str, str | None, str], list[CadEntity]] = defaultdict(list)
    for entity in all_entities:
        by_scope[(entity.source_file_id, entity.sheet_id, entity.space)].append(entity)

    detections: list[MtOccurrence] = []
    for scope in sorted(by_scope):
        group = sorted(by_scope[scope], key=lambda entity: entity.id)
        radius = cluster_distance if cluster_distance is not None else _inferred_distance(group)
        leader_radius = leader_bind_distance if leader_bind_distance is not None else radius * 2.0
        room_radius = room_search_distance if room_search_distance is not None else radius * 20.0
        seeds: list[_Seed] = []
        used_descriptor_ids: set[str] = set()

        for entity in group:
            if not entity.text or entity.entity_type.upper() not in _TEXT_TYPES:
                continue
            text = clean_cad_text(entity.text)
            for code_match in find_material_codes(
                text,
                stainless_families=stainless_families,
                review_families=review_families,
            ):
                seeds.append(
                    _Seed(
                        code=code_match.normalized_code,
                        raw_code=code_match.raw_code,
                        family=code_match.family,
                        disposition=code_match.disposition,
                        entities=(entity,),
                        confidence=(0.88 if code_match.disposition == "stainless" else 0.58),
                        method="explicit_text",
                    )
                )
        detached = _detached_seeds(
            group,
            radius,
            stainless_families=stainless_families,
            review_families=review_families,
        )
        seeds.extend(detached)
        detached_entity_ids = {entity.id for seed in detached for entity in seed.entities}
        # Some callout blocks store only a numeric value in an ATTRIB whose tag
        # carries the MT meaning.  This is explicit structured evidence even if
        # the static "MT" glyph was omitted from the exported DXF.
        for entity in group:
            if entity.id in detached_entity_ids or entity.entity_type.upper() != "ATTRIB":
                continue
            tag = clean_cad_text(entity.geometry.get("tag")).upper()
            match = _NUMBER_RE.fullmatch(clean_cad_text(entity.text))
            family: str | None = None
            disposition: str | None = None
            tag_compact = re.sub(r"[^A-Z0-9\u4e00-\u9fff]", "", tag)
            for configured_family in sorted(
                {*stainless_families, *review_families},
                key=lambda value: (-len(value), value),
            ):
                normalized_family = normalize_material_code_family(configured_family)
                if normalized_family and normalized_family.replace("-", "") in tag_compact:
                    family = normalized_family
                    disposition = material_code_disposition(
                        family,
                        stainless_families=stainless_families,
                        review_families=review_families,
                    )
                    break
            if family is None and re.search(r"材料.*编号|物料.*编号", tag):
                family = "MT"
                disposition = material_code_disposition(
                    family,
                    stainless_families=stainless_families,
                    review_families=review_families,
                )
            if match and family and disposition:
                width = max(2, len(match.group(1)))
                code = f"{family}-{int(match.group(1)):0{width}d}"
                seeds.append(
                    _Seed(
                        code=code,
                        raw_code=f"{family} {clean_cad_text(entity.text)}",
                        family=family,
                        disposition=disposition,
                        entities=(entity,),
                        confidence=0.84 if disposition == "stainless" else 0.54,
                        method="attribute_tag",
                    )
                )

        for seed in seeds:
            occurrence = _occurrence_from_seed(
                seed,
                group=group,
                radius=radius,
                room_radius=room_radius,
                leader_radius=leader_radius,
            )
            detections.append(occurrence)
            for entity_id in occurrence.entity_ids:
                entity = next((item for item in group if item.id == entity_id), None)
                if entity is not None and contains_material_keyword(entity.text):
                    used_descriptor_ids.add(entity_id)

        # Material-name-only labels are useful when a drawing omits the MT code,
        # but only when the project material registry maps the phrase uniquely.
        phrases = _material_phrases(material_list)
        for entity in group:
            if (
                entity.id in used_descriptor_ids
                or not entity.text
                or not contains_material_keyword(entity.text)
            ):
                continue
            code = _match_material_code(entity.text, phrases)
            if code is None:
                continue
            material = next(
                (value for value in material_list if value.mt_code == code),
                None,
            )
            family = (
                material.material_code_family
                if material and material.material_code_family
                else code.rsplit("-", 1)[0]
            )
            disposition = material_code_disposition(
                family,
                stainless_families=stainless_families,
                review_families=review_families,
            )
            if disposition is None:
                continue
            detections.append(
                _occurrence_from_seed(
                    _Seed(
                        code=code,
                        raw_code=(material.raw_material_code if material else None) or code,
                        family=family,
                        disposition=disposition,
                        entities=(entity,),
                        confidence=0.65 if disposition == "stainless" else 0.48,
                        method="material_name",
                    ),
                    group=group,
                    radius=radius,
                    room_radius=room_radius,
                    leader_radius=leader_radius,
                )
            )

    return deduplicate_occurrences(detections)


__all__ = [
    "TextCluster",
    "clean_cad_text",
    "cluster_nearby_text",
    "contains_material_keyword",
    "deduplicate_occurrences",
    "detect_material_mentions",
    "detect_mt_occurrences",
    "entity_center",
    "link_docx_material_mentions",
    "normalize_mt_code",
]
