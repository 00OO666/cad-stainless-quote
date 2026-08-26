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

from .materials import find_mt_codes, normalize_mt_code, normalize_text
from .models import CadEntity, MaterialSpec, MtOccurrence, ReviewStatus

_TEXT_TYPES = {"TEXT", "MTEXT", "ATTRIB", "ATTDEF", "MULTILEADER", "MLEADER"}
_LEADER_TYPES = {"LEADER", "MLEADER", "MULTILEADER"}
_DETACHED_MT_RE = re.compile(r"^\s*M\s*T\s*[-—–－_:/／\\]?\s*$", re.I)
_NUMBER_RE = re.compile(r"^\s*0*(\d{1,3})\s*$")
_MATERIAL_KEYWORD_RE = re.compile(
    r"不锈钢|钢板|金属|铝板|铝合金|铜板|铁板|钛金|玫瑰金|镜面|拉丝|喷砂|"
    r"苹果砂|古铜|烤漆|镀色|雕花|蚀刻"
)
_ROOM_RE = re.compile(
    r"(?:房|厅|室|区|走廊|过道|前厅|大堂|会所|售楼部|卫生间|洗手间|茶室|"
    r"瑜伽|水吧|书吧|接待|洽谈|会议|办公室|门厅)"
)
_COMPONENT_RE = re.compile(
    r"踢脚|脚线|顶线|线条|门套|窗套|包板|收口|嵌条|墙面|顶面|天花|屏风|柜|台|"
    r"造型|设备带|壁炉|层架|挂衣杆|栏杆|压条|雕花"
)
_DRAWING_TITLE_RE = re.compile(
    r"(?:平面|立面|剖面|大样|节点|示意|索引|放样|天花)[^,，。；;]{0,12}图|图纸目录"
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
    entities: tuple[CadEntity, ...]
    confidence: float
    method: str


def clean_cad_text(value: Any) -> str:
    """Remove common MTEXT control codes while preserving visible content."""

    text = normalize_text(value)
    text = text.replace("\\P", " ").replace("\\p", " ").replace("\\~", " ")
    text = re.sub(r"\\[AaCcFfHhLlOoQqTtWw][^;]*;", "", text)
    text = text.replace("{", "").replace("}", "")
    return re.sub(r"\s+", " ", text).strip()


def contains_material_keyword(value: Any) -> bool:
    return bool(_MATERIAL_KEYWORD_RE.search(clean_cad_text(value)))


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
        entity
        for entity in entities
        if entity.text and entity.entity_type.upper() in _TEXT_TYPES
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


def _detached_seeds(group: Sequence[CadEntity], radius: float) -> list[_Seed]:
    mt_entities = [
        entity
        for entity in group
        if _DETACHED_MT_RE.fullmatch(clean_cad_text(entity.text))
    ]
    number_entities = [
        entity for entity in group if _NUMBER_RE.fullmatch(clean_cad_text(entity.text))
    ]
    candidates: list[tuple[float, str, str, CadEntity, CadEntity]] = []
    for mt_entity in mt_entities:
        mt_point = entity_center(mt_entity)
        for number_entity in number_entities:
            number_point = entity_center(number_entity)
            distance = _distance(mt_point, number_point)
            if distance > radius:
                continue
            if mt_point is not None and number_point is not None:
                dx = number_point[0] - mt_point[0]
                dy = abs(number_point[1] - mt_point[1])
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
                    mt_entity.id,
                    number_entity.id,
                    mt_entity,
                    number_entity,
                )
            )

    used_mt: set[str] = set()
    used_numbers: set[str] = set()
    seeds: list[_Seed] = []
    for _, _, _, mt_entity, number_entity in sorted(candidates):
        if mt_entity.id in used_mt or number_entity.id in used_numbers:
            continue
        match = _NUMBER_RE.fullmatch(clean_cad_text(number_entity.text))
        assert match is not None
        width = max(2, len(match.group(1)))
        code = f"MT-{int(match.group(1)):0{width}d}"
        seeds.append(_Seed(code, (mt_entity, number_entity), 0.80, "detached_text"))
        used_mt.add(mt_entity.id)
        used_numbers.add(number_entity.id)
    return seeds


def _material_phrases(materials: Sequence[MaterialSpec]) -> dict[str, set[str]]:
    phrases: dict[str, set[str]] = defaultdict(set)
    for material in materials:
        for value in (material.name, material.finish):
            text = clean_cad_text(value)
            compact = re.sub(r"[\s,，。()（）/]+", "", text).casefold()
            if len(compact) >= 3 and contains_material_keyword(compact):
                phrases[compact].add(material.mt_code)
    return phrases


def _match_material_code(text: str, phrases: Mapping[str, set[str]]) -> str | None:
    compact = re.sub(r"[\s,，。()（）/]+", "", clean_cad_text(text)).casefold()
    matches: set[str] = set()
    for phrase, codes in phrases.items():
        if phrase in compact or compact in phrase:
            matches.update(codes)
    return next(iter(matches)) if len(matches) == 1 else None


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
) -> tuple[tuple[float, float] | None, tuple[float, float] | None]:
    geometry = entity.geometry
    target = None
    label = None
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
        if target is None:
            target = vertices[0]
        if label is None:
            label = vertices[-1]
    elif label is None:
        label = entity_center(entity)
    return label, target


def _bind_leader(
    seed_entities: Sequence[CadEntity],
    anchor: tuple[float, float] | None,
    leaders: Sequence[CadEntity],
    radius: float,
) -> tuple[CadEntity | None, tuple[float, float] | None]:
    seed_ids = {entity.id for entity in seed_entities}
    candidates: list[tuple[float, str, CadEntity, tuple[float, float] | None]] = []
    for leader in leaders:
        label, target = _extract_leader_points(leader)
        own_annotation = leader.id in seed_ids or bool(
            set(find_mt_codes(leader.text or ""))
            & {code for entity in seed_entities for code in find_mt_codes(entity.text or "")}
        )
        distance = 0.0 if own_annotation else _distance(anchor, label)
        if distance <= radius:
            # For bare LEADER polylines, the endpoint nearest the annotation is
            # the landing; the opposite endpoint is the arrow target.
            vertices = leader.geometry.get("vertices") or leader.geometry.get("points")
            if not own_annotation and isinstance(vertices, Sequence) and len(vertices) >= 2:
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
    leaders = [entity for entity in group if entity.entity_type.upper() in _LEADER_TYPES]
    leader, target = _bind_leader(entities, anchor, leaders, leader_radius)
    if leader is not None and leader.id not in {entity.id for entity in entities}:
        entities.append(leader)

    room = _nearest_room(anchor, text_entities, room_radius)
    # A plain material phrase (for example "古铜色不锈钢") describes the MT
    # specification, not the physical component name. Prefer nearby text that
    # actually names a component; retain the material descriptor only when it
    # also contains a component noun such as 踢脚/门套/收口.
    component_candidates: list[tuple[float, str, str]] = []
    # The arrow endpoint is often closer to the physical component label than
    # the MT text itself. Search it first, then the annotation anchor. A wider
    # but still local radius recovers labels such as “社区门厅顶线” without
    # promoting a drawing title like “社区门厅立面图” to a component name.
    for priority, search_point in enumerate((target, anchor)):
        if search_point is None:
            continue
        for entity in text_entities:
            text = clean_cad_text(entity.text)
            distance = _distance(search_point, entity_center(entity))
            if (
                not text
                or len(text) > 36
                or not _COMPONENT_RE.search(text)
                or _DRAWING_TITLE_RE.search(text)
                or distance > radius * 6.0
            ):
                continue
            component_candidates.append(
                (distance + priority * radius * 0.15, entity.id, text)
            )
    component_hint = min(component_candidates)[2] if component_candidates else None
    if (
        component_hint is None
        and descriptor is not None
        and _COMPONENT_RE.search(clean_cad_text(descriptor.text))
    ):
        component_hint = clean_cad_text(descriptor.text)

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


def detect_mt_occurrences(
    entities: Iterable[CadEntity],
    *,
    materials: Iterable[MaterialSpec] = (),
    cluster_distance: float | None = None,
    leader_bind_distance: float | None = None,
    room_search_distance: float | None = None,
) -> list[MtOccurrence]:
    """Detect explicit, detached, and uniquely material-resolved MT labels."""

    all_entities = list(entities)
    material_list = list(materials)
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
            for code in find_mt_codes(text):
                seeds.append(_Seed(code, (entity,), 0.88, "explicit_text"))
        detached = _detached_seeds(group, radius)
        seeds.extend(detached)
        detached_entity_ids = {
            entity.id for seed in detached for entity in seed.entities
        }
        # Some callout blocks store only a numeric value in an ATTRIB whose tag
        # carries the MT meaning.  This is explicit structured evidence even if
        # the static "MT" glyph was omitted from the exported DXF.
        for entity in group:
            if entity.id in detached_entity_ids or entity.entity_type.upper() != "ATTRIB":
                continue
            tag = clean_cad_text(entity.geometry.get("tag")).upper()
            match = _NUMBER_RE.fullmatch(clean_cad_text(entity.text))
            if match and re.search(r"(?:^|[_-])MT(?:$|[_-])|材料.*编号|物料.*编号", tag):
                width = max(2, len(match.group(1)))
                code = f"MT-{int(match.group(1)):0{width}d}"
                seeds.append(_Seed(code, (entity,), 0.84, "attribute_tag"))

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
            detections.append(
                _occurrence_from_seed(
                    _Seed(code, (entity,), 0.65, "material_name"),
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
    "detect_mt_occurrences",
    "entity_center",
    "normalize_mt_code",
]
