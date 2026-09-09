"""Expand paper-space viewports into virtual, evidence-addressable drawing panels."""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections import Counter, defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .classifier import classify_sheet
from .linking import extract_reference_codes, extract_structured_reference_callouts
from .models import CadEntity, Sheet
from .mt import detect_mt_occurrences

BBox = tuple[float, float, float, float]

_PLAN_VIEW_TITLE_RE = re.compile(
    r"平面(?:图|布置)?|(?:FLOOR|LAYOUT|FURNISHING|FIXTURE)\s+PLAN|\bPLAN\b",
    re.I,
)


def _stable_id(prefix: str, *parts: object) -> str:
    canonical = json.dumps(parts, ensure_ascii=False, sort_keys=True, default=str)
    return f"{prefix}:{hashlib.sha256(canonical.encode('utf-8')).hexdigest()[:24]}"


def _as_bbox(value: Any) -> BBox | None:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)) or len(value) != 4:
        return None
    try:
        x0, y0, x1, y1 = (float(part) for part in value)
    except (TypeError, ValueError):
        return None
    if x1 <= x0 or y1 <= y0:
        return None
    return x0, y0, x1, y1


def _structured_cross_sheet_plan_evidence(
    drawing_number: str | None,
    title_texts: Sequence[str],
    paper_entities: Sequence[CadEntity],
) -> list[str]:
    """Return explicit cross-sheet callouts that resolve a plan/ceiling tie.

    Many interior sheets intentionally place a reflected-ceiling view beside a
    floor-plan view and give both viewports the same model extents.  The shared
    title can therefore score as both ``plan`` and ``ceiling``.  A structured
    ``view number / target sheet`` callout inside one viewport is stronger
    evidence that this particular viewport is the plan/index half.

    The page's own title-block number is excluded: it is provenance for the
    sheet, not a cross-drawing relationship.
    """

    if not any(_PLAN_VIEW_TITLE_RE.search(value or "") for value in title_texts):
        return []
    own_codes = extract_reference_codes(drawing_number)
    return sorted(
        {
            callout.code
            for callout in extract_structured_reference_callouts(paper_entities)
            if callout.code not in own_codes
        }
    )


def _intersects(left: BBox | None, right: BBox) -> bool:
    if left is None:
        return False
    return (
        left[0] <= right[2] and left[2] >= right[0] and left[1] <= right[3] and left[3] >= right[1]
    )


def _point_inside(point: tuple[float, float] | None, box: BBox) -> bool:
    return bool(point and box[0] <= point[0] <= box[2] and box[1] <= point[1] <= box[3])


def _entity_in_box(entity: CadEntity, box: BBox) -> bool:
    return _intersects(entity.bbox, box) or _point_inside(entity.insert, box)


def _entity_center(entity: CadEntity) -> tuple[float, float] | None:
    if entity.bbox is not None:
        return (
            (entity.bbox[0] + entity.bbox[2]) / 2,
            (entity.bbox[1] + entity.bbox[3]) / 2,
        )
    return entity.insert


@dataclass(frozen=True, slots=True)
class _PaperToModelTransform:
    """Axis-aligned paper-to-model mapping for a 2D CAD viewport.

    A wrong transform is worse than a missing one, so rotated/3D/aspect-skewed
    viewports are rejected and their paper annotations remain on the original
    layout for review.
    """

    paper_box: BBox
    model_box: BBox
    scale_x: float
    scale_y: float

    @classmethod
    def build(
        cls,
        viewport: CadEntity,
        model_box: BBox,
    ) -> _PaperToModelTransform | None:
        paper_box = _as_bbox(viewport.bbox)
        if paper_box is None:
            return None
        direction = viewport.geometry.get("view_direction_vector")
        if isinstance(direction, Sequence) and not isinstance(direction, (str, bytes)):
            try:
                direction_values = [float(value) for value in direction[:3]]
            except (TypeError, ValueError):
                return None
            if len(direction_values) >= 3:
                magnitude = math.sqrt(sum(value * value for value in direction_values))
                if not math.isfinite(magnitude) or magnitude <= 1e-12:
                    return None
                normalized = [value / magnitude for value in direction_values]
                if (
                    abs(normalized[0]) > 1e-6
                    or abs(normalized[1]) > 1e-6
                    or abs(abs(normalized[2]) - 1.0) > 1e-6
                ):
                    return None
        try:
            twist = float(viewport.geometry.get("view_twist_angle") or 0.0)
        except (TypeError, ValueError):
            return None
        # Native DXF VIEWPORT group 51 is in degrees (ezdxf also converts it
        # with math.radians). A full turn is unrotated; 2*pi degrees is not.
        if not math.isfinite(twist) or abs(math.remainder(twist, 360.0)) > 1e-7:
            return None
        paper_width = paper_box[2] - paper_box[0]
        paper_height = paper_box[3] - paper_box[1]
        scale_x = (model_box[2] - model_box[0]) / paper_width
        scale_y = (model_box[3] - model_box[1]) / paper_height
        if not all(math.isfinite(value) and value > 0 for value in (scale_x, scale_y)):
            return None
        if abs(scale_x - scale_y) / max(scale_x, scale_y) > 1e-4:
            return None
        return cls(paper_box, model_box, scale_x, scale_y)

    def point(self, value: Sequence[Any] | None) -> list[float] | None:
        if not isinstance(value, Sequence) or isinstance(value, (str, bytes)) or len(value) < 2:
            return None
        try:
            x, y = float(value[0]), float(value[1])
        except (TypeError, ValueError):
            return None
        mapped = [
            self.model_box[0] + (x - self.paper_box[0]) * self.scale_x,
            self.model_box[1] + (y - self.paper_box[1]) * self.scale_y,
        ]
        if len(value) >= 3:
            try:
                mapped.append(float(value[2]))
            except (TypeError, ValueError):
                mapped.append(0.0)
        return mapped

    def bbox(self, value: BBox | None) -> BBox | None:
        if value is None:
            return None
        lower = self.point(value[:2])
        upper = self.point(value[2:])
        if lower is None or upper is None:
            return None
        return lower[0], lower[1], upper[0], upper[1]

    def geometry(self, value: Mapping[str, Any]) -> dict[str, Any]:
        output = dict(value)
        point_keys = {
            "defpoint",
            "defpoint2",
            "defpoint3",
            "defpoint4",
            "text_midpoint",
            "text_location",
            "text_point",
            "label_point",
            "landing",
            "landing_point",
            "start",
            "end",
            "center",
            "target",
            "leader_target",
            "arrowhead",
            "arrow_point",
            "arrow",
            "tip",
        }
        for key in point_keys:
            mapped = self.point(output.get(key))
            if mapped is not None:
                output[key] = mapped
        # All redundant leader representations must use the same coordinate
        # system. MT binding prefers leader_targets over leader_target/vertices;
        # projecting only the latter silently leaves its arrow in paper space.
        for key in (
            "vertices",
            "points",
            "leader_vertices",
            "line_points",
            "leader_targets",
            "landing_points",
            "dogleg_end_points",
            "annotation_boundary",
        ):
            points = output.get(key)
            if isinstance(points, Sequence) and not isinstance(points, (str, bytes)):
                mapped_points = [self.point(point) for point in points]
                output[key] = [point for point in mapped_points if point is not None]
        paths = output.get("leader_paths")
        if isinstance(paths, Sequence) and not isinstance(paths, (str, bytes)):
            # Recurse through the indexed path record, preserving leader/line
            # indices and copying rather than changing the raw source geometry.
            output["leader_paths"] = [
                self.geometry(path) if isinstance(path, Mapping) else path for path in paths
            ]
        try:
            height = float(output.get("height"))
        except (TypeError, ValueError):
            height = 0.0
        if math.isfinite(height) and height > 0:
            output["height"] = height * (self.scale_x + self.scale_y) / 2
        return output


@dataclass(frozen=True, slots=True)
class _ViewportPanelSpec:
    viewport: CadEntity
    model_box: BBox
    model_entity_count: int
    transform: _PaperToModelTransform | None


@dataclass(frozen=True, slots=True)
class _PaperPageReference:
    code: str | None
    point: tuple[float, float]
    entity_id: str
    space: str
    title_texts: tuple[str, ...]
    parent_handle: str | None = None
    raw_text: str = ""
    issue: str | None = None
    source_file_id: str | None = None
    frame_boxes: tuple[BBox, ...] = ()


@dataclass(frozen=True, slots=True)
class _PaperPageSelection:
    reference: _PaperPageReference | None
    basis: str
    blocks_fallback: bool = False
    candidate_ids: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class _PaperViewTitle:
    """One structured local view title recovered from an INSERT's ATTRIBs."""

    text: str
    point: tuple[float, float]
    entity_id: str
    parent_handle: str
    space: str
    strength: float


_CALLOUT_BLOCK_RE = re.compile(
    r"图号|索引|标注|引出|详图号|剖面号|ELEVATION|DETAIL|SECTION|CALLOUT|MARK",
    re.I,
)
_SHEET_TITLE_RE = re.compile(
    r"平面|立面|节点|大样|详图|剖面|天花|顶面|地面|门表|"
    r"PLAN|ELEVATION|DETAIL|SECTION|CEILING|FLOOR|DOOR",
    re.I,
)
_STRONG_VIEW_TITLE_TAG_RE = re.compile(
    r"SHEET[_ -]?TITLE|DRAWING[_ -]?TITLE|VIEW[_ -]?TITLE|TITLE[_ -]?NAME|"
    r"图名|标题|(?:平面|立面|剖面|节点|大样|详图).{0,12}(?:SCALE|比例)",
    re.I,
)
_GENERIC_VIEW_TITLE_RE = re.compile(
    r"^(?:PLAN|ELEVATION|DETAIL|SECTION|CEILING|FLOOR|DOOR|RCP|"
    r"平面|立面|节点|大样|详图|剖面)$",
    re.I,
)
_PAGE_NUMBER_TAG_RE = re.compile(
    r"^(?:(?:SHEET|DRAWING|DWG|PAGE)[_ -]?(?:NO|NUMBER|NUM)|图号|页码|图纸编号)$",
    re.I,
)


def _paper_page_references(paper_entities: Sequence[CadEntity]) -> list[_PaperPageReference]:
    """Find drawing numbers in title-block INSERT attributes.

    Interior drawings commonly place the page number outside every viewport.
    A material callout can therefore be inside a viewport while its human-used
    sheet number (for example ``1F-WE-01``) lives in a neighboring title block.
    We distinguish title blocks from local view-callout blocks by requiring a
    sibling sheet title and excluding explicitly named callout/index blocks.
    """

    by_handle = {
        (entity.source_file_id, entity.space, entity.handle): entity
        for entity in paper_entities
        if entity.handle
    }
    output: list[_PaperPageReference] = []
    for entity in paper_entities:
        if entity.entity_type != "ATTRIB" or entity.geometry.get("semantic_hidden"):
            continue
        # Retain an unparseable explicit page-number slot. Dropping it would
        # let the neighbouring page become the apparent nearest valid title.
        explicit_number_slot = bool(
            _PAGE_NUMBER_TAG_RE.fullmatch(str(entity.geometry.get("tag") or ""))
        )
        if not entity.text and not explicit_number_slot:
            continue
        extracted_codes = extract_reference_codes(entity.text)
        # Extraction already suppresses substrings of longer code tokens.
        # Distinct complete codes (including letter variants) are conflicts,
        # not alternative spellings to discard by length or prefix depth.
        codes = sorted(extracted_codes)
        if not codes and not explicit_number_slot:
            continue
        parent_handle = entity.geometry.get("parent_insert_handle")
        parent = (
            by_handle.get((entity.source_file_id, entity.space, str(parent_handle)))
            if parent_handle
            else None
        )
        if (
            parent is None
            or parent.entity_type != "INSERT"
            or parent.geometry.get("semantic_hidden")
        ):
            continue
        if (parent.source_file_id, parent.space) != (entity.source_file_id, entity.space):
            continue
        parent_name = str(parent.geometry.get("name") or "")
        if _CALLOUT_BLOCK_RE.search(parent_name):
            continue
        frame_boxes = tuple(_native_frame_boxes(parent.geometry))
        sibling_texts: list[str] = []
        framed_title_fields: list[str] = []
        for handle in parent.geometry.get("attribute_handles") or []:
            sibling = by_handle.get((entity.source_file_id, entity.space, str(handle)))
            if (
                sibling is not None
                and sibling.text
                and sibling.id != entity.id
                and not sibling.geometry.get("semantic_hidden")
                and sibling.geometry.get("parent_insert_handle") == parent.handle
                and (sibling.source_file_id, sibling.space) == (parent.source_file_id, parent.space)
            ):
                sibling_texts.append(sibling.text)
                # A reusable title-block template can label its title field
                # with drawing semantics even when the current text is e.g.
                # demolition dimensions or lighting controls. This helps find
                # its page identity only; the template tag is not the view kind.
                tag = str(sibling.geometry.get("tag") or "")
                if frame_boxes and (
                    _SHEET_TITLE_RE.search(tag) or _STRONG_VIEW_TITLE_TAG_RE.search(tag)
                ):
                    framed_title_fields.append(sibling.text)
        title_texts = tuple(
            dict.fromkeys(
                [
                    *(text for text in sibling_texts if _SHEET_TITLE_RE.search(text)),
                    *framed_title_fields,
                ]
            )
        )
        if not title_texts:
            continue
        point = _entity_center(entity)
        if point is None:
            continue
        output.append(
            _PaperPageReference(
                code=codes[0] if len(codes) == 1 else None,
                point=point,
                entity_id=entity.id,
                space=entity.space,
                title_texts=title_texts,
                parent_handle=parent.handle,
                raw_text=entity.text or "",
                issue=None if len(codes) == 1 else "UNPARSEABLE_OR_AMBIGUOUS_PAGE_NUMBER",
                source_file_id=entity.source_file_id,
                frame_boxes=frame_boxes,
            )
        )
    return sorted(output, key=lambda value: (value.space, value.code or "", value.entity_id))


def _native_frame_boxes(geometry: Mapping[str, Any]) -> list[BBox]:
    """Read explicit closed-frame geometry, never the INSERT insertion bbox."""
    scan = geometry.get("paper_frame_scan")
    if not isinstance(scan, Mapping) or scan.get("schema") != "native-paper-frame/1":
        return []
    if scan.get("complete") is not True or scan.get("issues"):
        return []
    rectangles = scan.get("rectangles")
    if not isinstance(rectangles, list):
        return []
    boxes = []
    for rectangle in rectangles:
        if not isinstance(rectangle, Mapping) or not rectangle.get("source_handles"):
            return []
        if rectangle.get("axis_aligned") is False:
            continue
        box = _as_bbox(rectangle.get("bbox"))
        if box is None or not all(math.isfinite(value) for value in box):
            return []
        boxes.append(box)
    return sorted(set(boxes))


def _frame_contains(outer: BBox, inner: BBox) -> bool:
    # Only numeric round-off tolerance, not a configurable near-frame radius.
    epsilon = max(outer[2] - outer[0], outer[3] - outer[1]) * 1e-8
    return (
        outer[0] - epsilon <= inner[0] <= inner[2] <= outer[2] + epsilon
        and outer[1] - epsilon <= inner[1] <= inner[3] <= outer[3] + epsilon
    )


def _nearest_page_reference(
    viewport: CadEntity,
    references: Sequence[_PaperPageReference],
) -> _PaperPageReference | None:
    """Compatibility wrapper; unresolved selections must not expose a page code."""
    return _select_page_reference(viewport, references).reference


def _select_page_reference(
    viewport: CadEntity,
    references: Sequence[_PaperPageReference],
) -> _PaperPageSelection:
    """Bounded title association, not verified page-frame or component ownership.

    Indexed INSERT boxes may be insertion points, not native paper frames. Use
    local directional candidates conservatively and preserve missing/ambiguous
    number slots so a parser failure cannot silently borrow a different page.
    """
    point = _entity_center(viewport)
    if point is None:
        return _PaperPageSelection(None, "MISSING_VIEWPORT_LOCATION", True)
    candidates = [
        value
        for value in references
        if value.space == viewport.space
        and (value.source_file_id is None or value.source_file_id == viewport.source_file_id)
    ]
    if not candidates:
        return _PaperPageSelection(None, "NO_STRUCTURED_PAGE_SLOT")
    all_local_layout = candidates
    paper_box = _as_bbox(viewport.bbox)
    if paper_box is not None and all(math.isfinite(v) for v in paper_box):
        contained = [
            value
            for value in candidates
            if any(_frame_contains(box, paper_box) for box in value.frame_boxes)
        ]
        if contained:
            # A local view title can carry a reciprocal code, but its small
            # annotation frame cannot own the whole physical drawing viewport.
            # No lexical or area preference may hide conflicting owner frames.
            results = [
                _page_slot_result(v, candidates, "NATIVE_CLOSED_FRAME_CONTAINS_VIEWPORT")
                for v in contained
            ]
            if (
                any(result.blocks_fallback for result in results)
                or len({v.code for v in contained}) != 1
            ):
                return _PaperPageSelection(
                    None,
                    "CONFLICTING_OR_UNPARSEABLE_NATIVE_PAGE_FRAMES",
                    True,
                    tuple(sorted({i for result in results for i in result.candidate_ids})),
                )
            winner = min(contained, key=lambda value: value.entity_id)
            return _PaperPageSelection(
                winner,
                "NATIVE_CLOSED_FRAME_CONTAINS_VIEWPORT",
                False,
                tuple(sorted(v.entity_id for v in contained)),
            )
        width = viewport.bbox[2] - viewport.bbox[0]
        height = viewport.bbox[3] - viewport.bbox[1]
        candidates = [
            value
            for value in candidates
            if viewport.bbox[0] - max(5.0, width * 0.08)
            <= value.point[0]
            <= viewport.bbox[2] + max(5.0, width)
            and viewport.bbox[1] - max(5.0, height)
            <= value.point[1]
            <= viewport.bbox[3] + max(5.0, height * 0.20)
        ]
        if not candidates:
            return _PaperPageSelection(
                None,
                "NO_LOCAL_PAGE_SLOT",
                True,
                tuple(sorted(v.entity_id for v in all_local_layout)),
            )
        # Some authoring files place many complete paper sheets side-by-side
        # under one very wide viewport.  Their title blocks form a horizontal
        # sequence just below the viewport.  The panel begins with the
        # left-most page, not the title block nearest the viewport centre or
        # lower-right corner.
        horizontal_band = [
            value
            for value in candidates
            if viewport.bbox[0] <= value.point[0] <= viewport.bbox[2] + max(5.0, width * 0.03)
            and viewport.bbox[1] - max(5.0, height * 0.75)
            <= value.point[1]
            <= viewport.bbox[3] + max(30.0, height * 0.20)
        ]
        # Count positioned title slots, including unparseable ones. Requiring
        # two *valid codes* skips the first missing page and borrows the next.
        distinct_slots = {
            (value.parent_handle or value.entity_id, value.point) for value in horizontal_band
        }
        if width >= height * 2.0 and len(distinct_slots) >= 2:
            x_values = [value.point[0] for value in horizontal_band]
            if max(x_values) - min(x_values) >= max(5.0, width * 0.10):
                winner = min(
                    horizontal_band,
                    key=lambda value: (
                        value.point[0],
                        value.point[1],
                        value.code or "",
                        value.entity_id,
                    ),
                )
                return _page_slot_result(winner, all_local_layout, "WIDE_PAGE_BAND_CANDIDATE")
        # Conventional title blocks sit at the lower-right of the represented
        # drawing area. Prefer that directional relationship before raw nearest
        # distance; otherwise a viewport near a page boundary can be assigned
        # to the title block above/left on the neighboring sheet.
        directional = [
            value
            for value in candidates
            if value.point[0] >= viewport.bbox[2] - max(5.0, width * 0.08)
            and value.point[1] <= point[1] + max(5.0, height * 0.10)
        ]
        if directional:
            candidates = directional
    else:
        return _PaperPageSelection(None, "MISSING_VIEWPORT_EXTENT", True)
    ordered = sorted(
        candidates,
        key=lambda value: (
            math.hypot(value.point[0] - point[0], value.point[1] - point[1]),
            value.code or "",
            value.entity_id,
        ),
    )
    winner = ordered[0]
    slot_result = _page_slot_result(winner, all_local_layout, "BOUNDED_DIRECTIONAL_PAGE_CANDIDATE")
    if slot_result.blocks_fallback:
        return slot_result
    distance = math.hypot(winner.point[0] - point[0], winner.point[1] - point[1])
    near_ties = [
        v
        for v in ordered
        if abs(math.hypot(v.point[0] - point[0], v.point[1] - point[1]) - distance)
        <= max(1e-6, math.hypot(width, height) * 0.01)
    ]
    if len({v.code for v in near_ties}) > 1:
        return _PaperPageSelection(
            None, "AMBIGUOUS_LOCAL_PAGE_SLOTS", True, tuple(sorted(v.entity_id for v in near_ties))
        )
    return slot_result


def _page_slot_result(
    winner: _PaperPageReference,
    layout_references: Sequence[_PaperPageReference],
    basis: str,
) -> _PaperPageSelection:
    siblings = [
        v
        for v in layout_references
        if winner.parent_handle and v.parent_handle == winner.parent_handle
    ] or [winner]
    ids = tuple(sorted(v.entity_id for v in siblings))
    if not winner.code or winner.issue:
        return _PaperPageSelection(None, winner.issue or "MISSING_PAGE_NUMBER", True, ids)
    if any(not v.code or v.issue for v in siblings) or len({v.code for v in siblings}) != 1:
        return _PaperPageSelection(None, "CONFLICTING_TITLE_BLOCK_PAGE_NUMBERS", True, ids)
    return _PaperPageSelection(winner, basis, False, ids)


def _paper_entity_owner(
    entity: CadEntity,
    specs: Sequence[_ViewportPanelSpec],
) -> str | None:
    """Assign an annotation to one most-specific non-default viewport."""

    points: list[tuple[float, float]] = []
    if entity.entity_type in {"LEADER", "MLEADER", "MULTILEADER"}:
        raw_vertices = entity.geometry.get("vertices") or entity.geometry.get("points")
        if isinstance(raw_vertices, Sequence) and not isinstance(raw_vertices, (str, bytes)):
            for raw in raw_vertices:
                if (
                    isinstance(raw, Sequence)
                    and not isinstance(raw, (str, bytes))
                    and len(raw) >= 2
                ):
                    try:
                        points.append((float(raw[0]), float(raw[1])))
                    except (TypeError, ValueError):
                        continue
    center = _entity_center(entity)
    if center is not None:
        points.append(center)
    for point in points:
        candidates = [
            spec
            for spec in specs
            if entity.space == spec.viewport.space
            and spec.viewport.bbox is not None
            and _point_inside(point, spec.viewport.bbox)
        ]
        if not candidates:
            continue
        winner = min(
            candidates,
            key=lambda spec: (
                (spec.viewport.bbox[2] - spec.viewport.bbox[0])
                * (spec.viewport.bbox[3] - spec.viewport.bbox[1]),  # type: ignore[index]
                spec.viewport.id,
            ),
        )
        return winner.viewport.id
    return None


def _point_viewport_owner(
    point: tuple[float, float] | None,
    space: str,
    specs: Sequence[_ViewportPanelSpec],
) -> str | None:
    if point is None:
        return None
    candidates = [
        spec
        for spec in specs
        if space == spec.viewport.space
        and spec.viewport.bbox is not None
        and _point_inside(point, spec.viewport.bbox)
    ]
    if not candidates:
        return None
    winner = min(
        candidates,
        key=lambda spec: (
            (spec.viewport.bbox[2] - spec.viewport.bbox[0])
            * (spec.viewport.bbox[3] - spec.viewport.bbox[1]),  # type: ignore[index]
            spec.viewport.id,
        ),
    )
    return winner.viewport.id


def _paper_annotation_radius(entities: Sequence[CadEntity]) -> dict[str, float]:
    heights_by_space: dict[str, list[float]] = defaultdict(list)
    for entity in entities:
        if entity.entity_type not in {"TEXT", "MTEXT", "ATTRIB", "ATTDEF"}:
            continue
        height = 0.0
        if entity.bbox is not None:
            height = abs(entity.bbox[3] - entity.bbox[1])
        try:
            height = max(height, float(entity.geometry.get("height") or 0.0))
        except (TypeError, ValueError):
            pass
        if math.isfinite(height) and height > 0:
            heights_by_space[entity.space].append(height)
    output: dict[str, float] = {}
    for space, heights in heights_by_space.items():
        ordered = sorted(heights)
        median = ordered[len(ordered) // 2]
        output[space] = max(1.0, min(80.0, median * 8.0))
    return output


def _leader_label_point(entity: CadEntity) -> tuple[float, float] | None:
    for key in ("label_point", "landing", "landing_point", "text_location", "text_point"):
        raw = entity.geometry.get(key)
        if isinstance(raw, Sequence) and not isinstance(raw, (str, bytes)) and len(raw) >= 2:
            try:
                return float(raw[0]), float(raw[1])
            except (TypeError, ValueError):
                pass
    raw_vertices = entity.geometry.get("vertices") or entity.geometry.get("points")
    if isinstance(raw_vertices, Sequence) and not isinstance(raw_vertices, (str, bytes)):
        for raw in reversed(raw_vertices):
            if isinstance(raw, Sequence) and not isinstance(raw, (str, bytes)) and len(raw) >= 2:
                try:
                    return float(raw[0]), float(raw[1])
                except (TypeError, ValueError):
                    continue
    return _entity_center(entity)


def _leader_arrow_point(entity: CadEntity) -> tuple[float, float] | None:
    for key in ("leader_target", "target", "arrowhead", "arrow_point", "arrow", "tip"):
        raw = entity.geometry.get(key)
        if isinstance(raw, Sequence) and not isinstance(raw, (str, bytes)) and len(raw) >= 2:
            try:
                return float(raw[0]), float(raw[1])
            except (TypeError, ValueError):
                pass
    raw_vertices = entity.geometry.get("vertices") or entity.geometry.get("points")
    if isinstance(raw_vertices, Sequence) and not isinstance(raw_vertices, (str, bytes)):
        for raw in raw_vertices:
            if isinstance(raw, Sequence) and not isinstance(raw, (str, bytes)) and len(raw) >= 2:
                try:
                    return float(raw[0]), float(raw[1])
                except (TypeError, ValueError):
                    continue
    return entity.insert


def _paper_entity_owners(
    paper_entities: Sequence[CadEntity],
    specs: Sequence[_ViewportPanelSpec],
) -> tuple[dict[str, str], list[str]]:
    """Move a callout atomically even when its label lands outside a viewport.

    A CAD LEADER arrow may be inside the viewport while its ATTRIB label sits in
    the margin. Assigning each entity independently splits the callout and drops
    its evidence. This routine first assigns geometric owners, then propagates a
    leader's owner to its annotation and the annotation's parent INSERT group.
    """

    warnings: list[str] = []
    owners = {
        entity.id: owner
        for entity in paper_entities
        if entity.entity_type != "VIEWPORT"
        if (owner := _paper_entity_owner(entity, specs)) is not None
    }
    by_handle = {entity.handle: entity for entity in paper_entities if entity.handle}
    parent_members: dict[str, list[CadEntity]] = defaultdict(list)
    for entity in paper_entities:
        parent_handle = entity.geometry.get("parent_insert_handle")
        if parent_handle:
            parent_members[str(parent_handle)].append(entity)
        if entity.entity_type == "INSERT" and entity.handle:
            parent_members[entity.handle].append(entity)

    def assign_parent_group(entity: CadEntity, owner: str) -> None:
        parent_handle = entity.geometry.get("parent_insert_handle")
        if parent_handle:
            for member in parent_members.get(str(parent_handle), ()):
                owners[member.id] = owner
        elif entity.entity_type == "INSERT" and entity.handle:
            for member in parent_members.get(entity.handle, ()):
                owners[member.id] = owner
        owners[entity.id] = owner

    # A member already inside a viewport brings its entire INSERT/ATTRIB group.
    for members in parent_members.values():
        member_owners = [owners[member.id] for member in members if member.id in owners]
        if not member_owners:
            continue
        owner = Counter(member_owners).most_common(1)[0][0]
        for member in members:
            owners[member.id] = owner

    # MT callouts receive the strongest, occurrence-level assignment. The
    # detector already binds split attributes, descriptors, and bare LEADERs;
    # the leader arrow target then places that entire evidence group in exactly
    # one viewport. This avoids the partial-migration failure where a leader was
    # projected but the MT label remained on the source layout.
    entity_by_id = {entity.id: entity for entity in paper_entities}
    atomic_claims: dict[str, set[str]] = defaultdict(set)
    atomic_group_by_occurrence: list[tuple[str, set[str], str]] = []
    atomic_leader_ids: set[str] = set()
    for occurrence in detect_mt_occurrences(paper_entities):
        if occurrence.leader_target is None or not occurrence.leader_entity_id:
            continue
        leader = entity_by_id.get(occurrence.leader_entity_id)
        if leader is None:
            continue
        owner = _point_viewport_owner(occurrence.leader_target, leader.space, specs)
        if owner is None:
            continue
        member_ids = set(occurrence.entity_ids)
        member_ids.add(occurrence.leader_entity_id)
        expanded_ids = set(member_ids)
        for entity_id in member_ids:
            entity = entity_by_id.get(entity_id)
            if entity is None:
                continue
            parent_handle = entity.geometry.get("parent_insert_handle")
            if parent_handle:
                expanded_ids.update(
                    member.id for member in parent_members.get(str(parent_handle), ())
                )
        atomic_group_by_occurrence.append((occurrence.id, expanded_ids, owner))
        atomic_leader_ids.add(occurrence.leader_entity_id)
        for entity_id in expanded_ids:
            atomic_claims[entity_id].add(owner)
    conflicting_ids = {
        entity_id for entity_id, claimed_owners in atomic_claims.items() if len(claimed_owners) > 1
    }
    for occurrence_id, member_ids, owner in atomic_group_by_occurrence:
        if member_ids & conflicting_ids:
            for entity_id in member_ids:
                owners.pop(entity_id, None)
            warnings.append(
                f"{occurrence_id}: conflicting viewport claims; retained atomic MT callout "
                "on original layout"
            )
            continue
        for entity_id in member_ids:
            owners[entity_id] = owner

    radii = _paper_annotation_radius(paper_entities)
    text_entities_by_space: dict[str, list[CadEntity]] = defaultdict(list)
    for entity in paper_entities:
        if entity.entity_type in {"TEXT", "MTEXT", "ATTRIB", "ATTDEF"}:
            text_entities_by_space[entity.space].append(entity)
    leaders = [
        entity
        for entity in paper_entities
        if entity.entity_type in {"LEADER", "MLEADER", "MULTILEADER"}
        and entity.id in owners
        and entity.id not in atomic_leader_ids
    ]
    for leader in leaders:
        owner = owners[leader.id]
        annotation_handle = leader.geometry.get("annotation_handle")
        annotation = by_handle.get(str(annotation_handle)) if annotation_handle else None
        if annotation is None:
            label_point = _leader_label_point(leader)
            radius = radii.get(leader.space, 80.0)
            candidates: list[tuple[float, str, CadEntity]] = []
            for entity in text_entities_by_space.get(leader.space, ()):
                point = _entity_center(entity)
                if point is None or label_point is None:
                    continue
                distance = math.hypot(point[0] - label_point[0], point[1] - label_point[1])
                if distance <= radius:
                    candidates.append((distance, entity.id, entity))
            if candidates:
                annotation = min(candidates)[2]
        if annotation is not None:
            assign_parent_group(annotation, owner)
    return owners, warnings


def _clone_paper_entity(
    entity: CadEntity,
    *,
    sheet_id: str,
    panel_space: str,
    viewport: CadEntity,
    transform: _PaperToModelTransform,
) -> CadEntity:
    insert = transform.point(entity.insert)
    geometry = transform.geometry(entity.geometry)
    geometry.update(
        {
            "original_entity_id": entity.id,
            "original_space": entity.space,
            "original_paper_insert": list(entity.insert) if entity.insert else None,
            "original_paper_bbox": list(entity.bbox) if entity.bbox else None,
            "panel_viewport_handle": viewport.handle,
            "paper_to_model_transform": {
                "paper_bbox": list(transform.paper_box),
                "model_bbox": list(transform.model_box),
                "scale_x": transform.scale_x,
                "scale_y": transform.scale_y,
            },
        }
    )
    return entity.model_copy(
        update={
            "id": _stable_id("panel_paper_entity", sheet_id, entity.id),
            "sheet_id": sheet_id,
            "space": panel_space,
            "insert": tuple(insert[:2]) if insert is not None else None,
            "bbox": transform.bbox(entity.bbox),
            "geometry": geometry,
        }
    )


def _paper_title_texts(viewport: CadEntity, paper_entities: Sequence[CadEntity]) -> list[str]:
    if viewport.bbox is None:
        return []
    x0, y0, x1, _ = viewport.bbox
    width = max(x1 - x0, 1.0)
    height = max(viewport.bbox[3] - viewport.bbox[1], 1.0)
    # Local title blocks regularly extend a little beyond the right edge of the
    # represented viewport.  A narrow asymmetric overhang recovers them without
    # using a broad expanded viewport that could absorb an adjacent drawing.
    search = (
        x0 - width * 0.08,
        y0 - min(300.0, height * 0.4),
        x1 + width * 0.25,
        y0 + height * 0.12,
    )
    candidates: list[tuple[float, str]] = []
    for entity in paper_entities:
        if (entity.source_file_id, entity.space) != (viewport.source_file_id, viewport.space):
            continue
        if entity.geometry.get("semantic_hidden"):
            continue
        if not entity.text or not _entity_in_box(entity, search):
            continue
        center_y = (
            (entity.bbox[1] + entity.bbox[3]) / 2
            if entity.bbox is not None
            else entity.insert[1]
            if entity.insert
            else y0
        )
        candidates.append((abs(y0 - center_y), entity.text))
    return [text for _, text in sorted(candidates)[:12]]


def _paper_primary_view_titles(
    viewport: CadEntity,
    paper_entities: Sequence[CadEntity],
) -> list[str]:
    """Return structured human-readable local titles near a viewport bottom.

    Many title blocks contain both a fixed ATTRIB value such as ``DETAIL`` and
    the actual title (for example ``服务台A正立面图``).  The ATTRIB tag of
    the latter normally names a title/scale field, so it is stronger than the
    fixed glyph.  Grouping by parent INSERT also prevents a nearby sheet title
    from winning solely because it is a few units closer.
    """

    if viewport.bbox is None:
        return []
    x0, y0, x1, y1 = viewport.bbox
    width = max(x1 - x0, 1.0)
    height = max(y1 - y0, 1.0)
    search = (
        x0 - width * 0.08,
        y0 - min(300.0, height * 0.4),
        x1 + width * 0.25,
        y0 + height * 0.18,
    )
    candidates: list[tuple[float, float, str, str]] = []
    for entity in paper_entities:
        if (entity.source_file_id, entity.space) != (viewport.source_file_id, viewport.space):
            continue
        if entity.geometry.get("semantic_hidden"):
            continue
        if entity.entity_type != "ATTRIB" or not entity.text:
            continue
        if not _entity_in_box(entity, search) or not _SHEET_TITLE_RE.search(entity.text):
            continue
        text = entity.text.strip()
        if not text or _GENERIC_VIEW_TITLE_RE.fullmatch(text):
            continue
        tag = str(entity.geometry.get("tag") or "")
        strong_tag = bool(_STRONG_VIEW_TITLE_TAG_RE.search(tag))
        descriptive = len(re.sub(r"\s+", "", text)) >= 4
        if not strong_tag and not descriptive:
            continue
        point = _entity_center(entity)
        if point is None:
            continue
        outside_x = max(x0 - point[0], 0.0, point[0] - x1)
        distance = abs(point[1] - y0) + outside_x * 0.35
        strength = 3.0 if strong_tag else 1.5
        parent = str(entity.geometry.get("parent_insert_handle") or entity.id)
        candidates.append((-strength, distance, parent, text))
    if not candidates:
        return []
    candidates.sort()
    winning_parent = candidates[0][2]
    return list(
        dict.fromkeys(text for _, _, parent, text in candidates if parent == winning_parent)
    )[:4]


def _best_model_bbox(
    viewport: CadEntity,
    model_entities: Sequence[CadEntity],
) -> tuple[BBox | None, int]:
    candidates = [
        _as_bbox(viewport.geometry.get("model_bbox_target_shifted")),
        _as_bbox(viewport.geometry.get("model_bbox")),
    ]
    scored: list[tuple[int, BBox]] = []
    for candidate in candidates:
        if candidate is None:
            continue
        count = sum(_entity_in_box(entity, candidate) for entity in model_entities)
        scored.append((count, candidate))
    if not scored:
        return None, 0
    count, box = max(scored, key=lambda item: (item[0], item[1]))
    return box, count


@dataclass(slots=True)
class PanelExpansion:
    sheets: list[Sheet] = field(default_factory=list)
    entities: list[CadEntity] = field(default_factory=list)
    source_panel_counts: dict[str, int] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "panel_count": len(self.sheets),
            "entity_count": len(self.entities),
            "source_panel_counts": self.source_panel_counts,
            "warnings": self.warnings,
            "sheets": [sheet.model_dump(mode="json") for sheet in self.sheets],
            "entities": [entity.model_dump(mode="json") for entity in self.entities],
        }


@dataclass(frozen=True, slots=True)
class _LocalViewAnchor:
    code: str
    x: float
    title: str | None
    entity_ids: tuple[str, ...]
    basis: str = "local_title"


_LOCAL_ELEVATION_CODE_RE = re.compile(
    r"^(?P<prefix>.*(?:^|[-_])(?:EL|E)[-_])(?P<number>\d+)(?P<suffix>[A-Z]?)$",
    re.I,
)


def _median(values: Sequence[float]) -> float:
    ordered = sorted(values)
    midpoint = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[midpoint]
    return (ordered[midpoint - 1] + ordered[midpoint]) / 2


def _local_view_anchors(
    panel: Sheet,
    entities: Sequence[CadEntity],
) -> list[_LocalViewAnchor]:
    """Recover repeated local sheet titles embedded in one oversized viewport."""

    if panel.kind != "elevation" or panel.bbox is None:
        return []
    x0, y0, x1, y1 = panel.bbox
    width = max(x1 - x0, 1.0)
    height = max(y1 - y0, 1.0)
    title_entities = [
        entity
        for entity in entities
        if entity.text
        and _SHEET_TITLE_RE.search(entity.text)
        and _entity_center(entity) is not None
    ]
    titles_by_parent: dict[str, list[CadEntity]] = defaultdict(list)
    for entity in title_entities:
        parent = entity.geometry.get("parent_insert_handle")
        if parent:
            titles_by_parent[str(parent)].append(entity)

    candidates: dict[str, list[tuple[float, str | None, str]]] = defaultdict(list)
    for entity in entities:
        if not entity.text:
            continue
        codes = [
            code
            for code in extract_reference_codes(entity.text)
            if _LOCAL_ELEVATION_CODE_RE.fullmatch(code) is not None
        ]
        if not codes:
            continue
        specificity = max((code.count("-"), len(code)) for code in codes)
        specific_codes = sorted(
            {code for code in codes if (code.count("-"), len(code)) == specificity}
        )
        if len(specific_codes) != 1:
            continue
        code = specific_codes[0]
        point = _entity_center(entity)
        if point is None:
            continue
        x_value, y_value = point
        if not (x0 - width * 0.06 <= x_value <= x1 + width * 0.06):
            continue
        if not (y0 - height * 0.25 <= y_value <= y0 + height * 0.40):
            continue
        parent = entity.geometry.get("parent_insert_handle")
        sibling_titles = titles_by_parent.get(str(parent), []) if parent else []
        nearby_titles = [
            title
            for title in title_entities
            if (
                (title_point := _entity_center(title)) is not None
                and abs(title_point[0] - x_value) <= max(2_500.0, min(6_000.0, width * 0.03))
                and abs(title_point[1] - y_value) <= max(1_200.0, height * 0.20)
            )
        ]
        matched_titles = sibling_titles or nearby_titles
        if not matched_titles:
            continue
        title_entity = min(
            matched_titles,
            key=lambda value: (
                math.hypot(
                    _entity_center(value)[0] - x_value,  # type: ignore[index]
                    _entity_center(value)[1] - y_value,  # type: ignore[index]
                ),
                value.id,
            ),
        )
        candidates[code].append((x_value, title_entity.text, entity.id))

    grouped_by_prefix: dict[str, list[_LocalViewAnchor]] = defaultdict(list)
    for code, values in candidates.items():
        match = _LOCAL_ELEVATION_CODE_RE.fullmatch(code)
        assert match is not None
        titles = [title for _, title, _ in values if title]
        title = Counter(titles).most_common(1)[0][0] if titles else None
        grouped_by_prefix[match.group("prefix").upper()].append(
            _LocalViewAnchor(
                code=code,
                x=_median([x_value for x_value, _, _ in values]),
                title=title,
                entity_ids=tuple(sorted(entity_id for _, _, entity_id in values)),
            )
        )
    if not grouped_by_prefix:
        return []
    anchors = max(
        grouped_by_prefix.values(),
        key=lambda values: (
            len(values),
            max(value.x for value in values) - min(value.x for value in values),
        ),
    )
    anchors.sort(key=lambda value: (value.x, value.code))
    if len(anchors) < 2 or anchors[-1].x - anchors[0].x < width * 0.12:
        return []

    return anchors


def _prepend_parent_page_anchor(
    panel: Sheet,
    anchors: Sequence[_LocalViewAnchor],
) -> list[_LocalViewAnchor]:
    """Recover the explicit title-block page immediately before local anchors.

    A long paper-space viewport may start on the page named by its neighboring
    title block and then contain additional repeated page titles in the same
    model strip.  In that layout the local detector can see ``B2-E-11`` and
    later pages while the parent panel already carries the explicit
    ``paper_page_reference:B2-E-10`` evidence.  Dropping the parent reference
    silently folds page 04 into page 05.

    Only the immediately preceding code is inferred, and only when the parent
    code is backed by a native paper-page reference.  Its x position is one
    median inter-page pitch before the first local anchor; implausible geometry
    fails closed and leaves the original anchors unchanged.
    """

    ordered = sorted(anchors, key=lambda value: (value.x, value.code))
    if len(ordered) < 2 or panel.bbox is None or not panel.drawing_number:
        return ordered

    parent_code = panel.drawing_number.strip().upper().replace("_", "-")
    if any(anchor.code.upper().replace("_", "-") == parent_code for anchor in ordered):
        return ordered

    parent_match = _LOCAL_ELEVATION_CODE_RE.fullmatch(parent_code)
    first_match = _LOCAL_ELEVATION_CODE_RE.fullmatch(ordered[0].code.upper().replace("_", "-"))
    if parent_match is None or first_match is None:
        return ordered
    # Lettered pages are explicit identities, not a numeric predecessor rule.
    # Preserve their native anchors; do not infer a missing lettered/base page.
    if parent_match.group("suffix") or any(
        re.search(r"\d[A-Z]$", anchor.code, re.I) for anchor in ordered
    ):
        return ordered
    parent_prefix = parent_match.group("prefix").upper().replace("_", "-")
    first_prefix = first_match.group("prefix").upper().replace("_", "-")
    if parent_prefix != first_prefix:
        return ordered
    if int(first_match.group("number")) != int(parent_match.group("number")) + 1:
        return ordered

    reference_entity_ids: list[str] = []
    marker = "paper_page_reference:"
    for evidence in panel.evidence:
        if not evidence.startswith(marker):
            continue
        payload = evidence[len(marker) :]
        code, separator, entity_id = payload.partition("@")
        if separator and code.strip().upper().replace("_", "-") == parent_code and entity_id:
            reference_entity_ids.append(entity_id)
    if not reference_entity_ids:
        return ordered

    gaps = [
        right.x - left.x
        for left, right in zip(ordered, ordered[1:], strict=False)
        if right.x > left.x
    ]
    if not gaps:
        return ordered
    page_pitch = _median(gaps)
    inferred_x = ordered[0].x - page_pitch
    x0, _, x1, _ = panel.bbox
    minimum_margin = max((x1 - x0) * 0.002, page_pitch * 0.08)
    if not (x0 + minimum_margin < inferred_x < ordered[0].x - minimum_margin):
        return ordered

    return [
        _LocalViewAnchor(
            code=parent_code,
            x=inferred_x,
            title=panel.title,
            entity_ids=tuple(sorted(set(reference_entity_ids))),
            basis="paper_page_reference_predecessor",
        ),
        *ordered,
    ]


def _layout_family(layout: str | None) -> str:
    return (layout or "").split("#viewport:", 1)[0]


def _vertical_gap(left: BBox, right: BBox) -> float:
    if left[3] < right[1]:
        return right[1] - left[3]
    if right[3] < left[1]:
        return left[1] - right[3]
    return 0.0


def _inherit_orphan_panel_page_codes(sheets: Sequence[Sheet]) -> list[Sheet]:
    """Attach a small unnumbered detail viewport to its explicit page band."""

    local_pages = [
        sheet
        for sheet in sheets
        if sheet.drawing_number
        and sheet.bbox is not None
        and any(value.startswith("local_subview_parent:") for value in sheet.evidence)
    ]
    output: list[Sheet] = []
    for sheet in sheets:
        if (
            sheet.drawing_number
            or sheet.bbox is None
            or sheet.kind != "elevation"
            or any(value.startswith("paper_page_reference_unresolved:") for value in sheet.evidence)
        ):
            output.append(sheet)
            continue
        center_x = (sheet.bbox[0] + sheet.bbox[2]) / 2
        candidates = [
            candidate
            for candidate in local_pages
            if candidate.source_file_id == sheet.source_file_id
            and _layout_family(candidate.layout) == _layout_family(sheet.layout)
            and candidate.bbox is not None
            and candidate.bbox[0] <= center_x <= candidate.bbox[2]
            and _vertical_gap(sheet.bbox, candidate.bbox)
            <= max(5_000.0, (candidate.bbox[3] - candidate.bbox[1]) * 2.0)
        ]
        if not candidates:
            output.append(sheet)
            continue
        winner = min(
            candidates,
            key=lambda candidate: (
                _vertical_gap(sheet.bbox, candidate.bbox),  # type: ignore[arg-type]
                abs(
                    center_x - (candidate.bbox[0] + candidate.bbox[2]) / 2  # type: ignore[index]
                ),
                candidate.id,
            ),
        )
        output.append(
            sheet.model_copy(
                update={
                    "drawing_number": winner.drawing_number,
                    "confidence": min(sheet.confidence, winner.confidence, 0.72),
                    "evidence": [
                        *sheet.evidence,
                        f"inherited_local_page_code:{winner.drawing_number}@{winner.id}",
                    ],
                }
            )
        )
    return output


def split_local_drawing_panels(expansion: PanelExpansion) -> PanelExpansion:
    """Split oversized elevation panels into one virtual sheet per local title.

    Some CAD files place twenty or more complete drawing sheets side-by-side in
    model space and expose them through only one paper viewport. Treating that
    viewport as a single sheet collapses distinct codes such as ``B2-E-03`` and
    ``B2-E-09`` into the title-block code nearest the viewport. This pass uses
    repeated local sheet-title geometry to recover the nested sheets.
    """

    entities_by_sheet: dict[str, list[CadEntity]] = defaultdict(list)
    for entity in expansion.entities:
        if entity.sheet_id:
            entities_by_sheet[entity.sheet_id].append(entity)

    output_sheets: list[Sheet] = []
    output_entities: list[CadEntity] = []
    for panel in expansion.sheets:
        panel_entities = entities_by_sheet.get(panel.id, [])
        anchors = _prepend_parent_page_anchor(
            panel,
            _local_view_anchors(panel, panel_entities),
        )
        if len(anchors) < 2 or panel.bbox is None:
            output_sheets.append(panel)
            output_entities.extend(panel_entities)
            continue

        owner_by_entity: dict[str, int] = {}

        local_anchors = tuple(anchors)

        def owner_for_x(
            x_value: float,
            anchor_values: tuple[_LocalViewAnchor, ...] = local_anchors,
        ) -> int:
            return min(
                range(len(anchor_values)),
                key=lambda candidate_index: (
                    abs(anchor_values[candidate_index].x - x_value),
                    candidate_index,
                ),
            )

        for entity in panel_entities:
            point = _entity_center(entity)
            if point is not None:
                owner_by_entity[entity.id] = owner_for_x(point[0])

        # Keep block INSERTs and their ATTRIBs on one recovered sheet.
        parent_groups: dict[str, list[str]] = defaultdict(list)
        for entity in panel_entities:
            parent = entity.geometry.get("parent_insert_handle")
            if parent:
                parent_groups[str(parent)].append(entity.id)
            if entity.entity_type == "INSERT" and entity.handle:
                parent_groups[entity.handle].append(entity.id)
        for entity_ids in parent_groups.values():
            votes = [owner_by_entity[value] for value in entity_ids if value in owner_by_entity]
            if not votes:
                continue
            winner = Counter(votes).most_common(1)[0][0]
            for entity_id in entity_ids:
                owner_by_entity[entity_id] = winner

        # MT labels, their leaders, and arrow targets are an atomic evidence unit.
        for occurrence in detect_mt_occurrences(panel_entities):
            point = occurrence.leader_target or occurrence.anchor
            if point is None:
                continue
            winner = owner_for_x(point[0])
            for entity_id in [*occurrence.entity_ids, occurrence.leader_entity_id]:
                if entity_id:
                    owner_by_entity[entity_id] = winner

        # A leader and its annotation remain one evidence unit even when the
        # annotation is a material/component phrase without an explicit MT
        # code.  The arrow target owns the unit; otherwise a label just left of
        # a recovered-sheet boundary can be assigned to the neighbouring page
        # while pointing into the correct page.
        entity_by_handle = {entity.handle: entity for entity in panel_entities if entity.handle}
        text_by_space: dict[str, list[CadEntity]] = defaultdict(list)
        for entity in panel_entities:
            if entity.entity_type in {"TEXT", "MTEXT", "ATTRIB", "ATTDEF"} and entity.text:
                text_by_space[entity.space].append(entity)
        radii = _paper_annotation_radius(panel_entities)
        for leader in panel_entities:
            if leader.entity_type not in {"LEADER", "MLEADER", "MULTILEADER"}:
                continue
            target = _leader_arrow_point(leader)
            if target is None:
                continue
            winner = owner_for_x(target[0])
            owner_by_entity[leader.id] = winner
            annotation_handle = leader.geometry.get("annotation_handle")
            annotation = entity_by_handle.get(str(annotation_handle)) if annotation_handle else None
            if annotation is None:
                label_point = _leader_label_point(leader)
                radius = radii.get(leader.space, 80.0)
                candidates: list[tuple[float, str, CadEntity]] = []
                for candidate in text_by_space.get(leader.space, ()):
                    point = candidate.insert or _entity_center(candidate)
                    if point is None or label_point is None:
                        continue
                    distance = math.hypot(point[0] - label_point[0], point[1] - label_point[1])
                    if distance <= radius:
                        candidates.append((distance, candidate.id, candidate))
                if candidates:
                    annotation = min(candidates)[2]
            if annotation is None:
                continue
            owner_by_entity[annotation.id] = winner
            parent = annotation.geometry.get("parent_insert_handle")
            if parent:
                for entity_id in parent_groups.get(str(parent), ()):
                    owner_by_entity[entity_id] = winner

        x0, y0, x1, y1 = panel.bbox
        boundaries = [x0]
        boundaries.extend(
            (left.x + right.x) / 2 for left, right in zip(anchors, anchors[1:], strict=False)
        )
        boundaries.append(x1)
        for index, anchor in enumerate(anchors):
            child_id = _stable_id("subview", panel.id, anchor.code, round(anchor.x, 6))
            child_entities = [
                entity for entity in panel_entities if owner_by_entity.get(entity.id) == index
            ]
            child = panel.model_copy(
                update={
                    "id": child_id,
                    "drawing_number": anchor.code,
                    "title": anchor.title or panel.title,
                    "layout": f"{panel.layout or ''}#subview:{anchor.code}",
                    "bbox": (boundaries[index], y0, boundaries[index + 1], y1),
                    "confidence": max(panel.confidence, 0.90),
                    "evidence": [
                        *panel.evidence,
                        f"local_subview_parent:{panel.id}",
                        f"local_page_code:{anchor.code}",
                        f"local_anchor_x:{anchor.x:.6f}",
                        f"local_anchor_basis:{anchor.basis}",
                        f"local_subview_entities:{len(child_entities)}",
                    ],
                }
            )
            output_sheets.append(child)
            parent_space = child_entities[0].space if child_entities else "model"
            child_space = f"{parent_space}#subview:{anchor.code}"
            for entity in child_entities:
                output_entities.append(
                    entity.model_copy(
                        update={
                            "id": _stable_id("subview_entity", child_id, entity.id),
                            "sheet_id": child_id,
                            "space": child_space,
                            "geometry": {
                                **entity.geometry,
                                "parent_panel_entity_id": entity.id,
                                "parent_panel_sheet_id": panel.id,
                                "local_subview_code": anchor.code,
                            },
                        }
                    )
                )

    expansion.sheets = _inherit_orphan_panel_page_codes(output_sheets)
    expansion.entities = output_entities
    expansion.source_panel_counts = dict(Counter(sheet.source_file_id for sheet in output_sheets))
    return expansion


def expand_viewport_panels(
    sheets: Sequence[Sheet],
    entities: Sequence[CadEntity],
    *,
    source_names: Mapping[str, str] | None = None,
    minimum_entity_count: int = 1,
) -> PanelExpansion:
    """Build virtual sheets for non-default paper-space viewports.

    Model entities and paper-space annotations inside a viewport are cloned with
    panel-specific IDs. Paper annotations are projected into model coordinates
    so an MT callout, its leader target, dimensions, and drawing geometry share
    one evidence space. Original CAD handles and coordinates remain in geometry.
    """

    source_names = source_names or {}
    output = PanelExpansion()
    sources = sorted({sheet.source_file_id for sheet in sheets})
    for source_id in sources:
        source_entities = [entity for entity in entities if entity.source_file_id == source_id]
        model_entities = [entity for entity in source_entities if entity.space == "model"]
        paper_entities = [entity for entity in source_entities if entity.space.startswith("paper:")]
        viewports = [entity for entity in paper_entities if entity.entity_type == "VIEWPORT"]
        page_references = _paper_page_references(paper_entities)
        # A plan view and a reflected-ceiling view may deliberately show the
        # same model region in two side-by-side paper viewports.  Deduplicating
        # by model bbox alone discards one viewport and its distinct paper
        # annotations (often the only elevation-index callouts).  Only collapse
        # viewports that overlap in both paper and model coordinates.
        seen_views: set[tuple[str, tuple[float, ...], tuple[float, ...] | tuple[str]]] = set()
        specs: list[_ViewportPanelSpec] = []
        for viewport in sorted(viewports, key=lambda entity: (entity.space, entity.id)):
            viewport_id = viewport.geometry.get("viewport_id")
            if viewport_id is not None and int(viewport_id) <= 1:
                continue
            model_box, entity_count = _best_model_bbox(viewport, model_entities)
            if model_box is None or entity_count < minimum_entity_count:
                output.warnings.append(f"{viewport.id}: no usable model-space region")
                continue
            paper_box = _as_bbox(viewport.bbox)
            paper_identity: tuple[float, ...] | tuple[str]
            if paper_box is not None:
                paper_identity = tuple(round(value, 5) for value in paper_box)
            else:
                # Without a paper bbox there is no safe geometric basis for
                # collapsing two viewport records.
                paper_identity = (viewport.id,)
            dedupe_key = (
                viewport.space,
                tuple(round(value, 5) for value in model_box),
                paper_identity,
            )
            if dedupe_key in seen_views:
                continue
            seen_views.add(dedupe_key)
            specs.append(
                _ViewportPanelSpec(
                    viewport=viewport,
                    model_box=model_box,
                    model_entity_count=entity_count,
                    transform=_PaperToModelTransform.build(viewport, model_box),
                )
            )
        paper_owners, owner_warnings = _paper_entity_owners(paper_entities, specs)
        output.warnings.extend(owner_warnings)
        panel_count = 0
        for spec in specs:
            viewport = spec.viewport
            model_box = spec.model_box
            paper_layout = viewport.space.removeprefix("paper:")
            page_selection = _select_page_reference(viewport, page_references)
            page_reference = page_selection.reference
            if page_selection.blocks_fallback:
                output.warnings.append(
                    f"{viewport.id}: unresolved paper page number ({page_selection.basis})"
                )
            title_texts = _paper_title_texts(viewport, paper_entities)
            primary_title_texts = _paper_primary_view_titles(viewport, paper_entities)
            if page_reference is not None:
                title_texts.extend(page_reference.title_texts)
            selected = [entity for entity in model_entities if _entity_in_box(entity, model_box)]
            selected_paper = [
                entity
                for entity in paper_entities
                if entity.entity_type != "VIEWPORT" and paper_owners.get(entity.id) == viewport.id
            ]
            semantic_texts = [entity.text for entity in [*selected, *selected_paper] if entity.text]
            filename = Path(source_names.get(source_id, source_id)).name
            classification = classify_sheet(
                filename,
                [*title_texts, *semantic_texts],
                layout_name=paper_layout,
                primary_title_texts=primary_title_texts,
            )
            page_code = page_reference.code if page_reference is not None else None
            structured_plan_codes = _structured_cross_sheet_plan_evidence(
                page_code,
                [*primary_title_texts, *title_texts, *semantic_texts],
                selected_paper,
            )
            if classification.kind in {"unknown", "ceiling"} and structured_plan_codes:
                classification.kind = "plan"
                classification.confidence = max(classification.confidence, 0.68)
                classification.evidence = [
                    *classification.evidence,
                    "structured_plan_references:" + ",".join(structured_plan_codes),
                ]
            sheet_id = _stable_id("panel", source_id, paper_layout, viewport.handle, model_box)
            title = (
                primary_title_texts[0]
                if primary_title_texts
                else title_texts[0]
                if title_texts
                else classification.title
            )
            panel = Sheet(
                id=sheet_id,
                source_file_id=source_id,
                drawing_number=(
                    page_reference.code
                    if page_reference is not None
                    else None
                    if page_selection.blocks_fallback
                    else classification.drawing_number
                ),
                title=title,
                kind=classification.kind,  # type: ignore[arg-type]
                layout=f"{paper_layout}#viewport:{viewport.handle or viewport.id}",
                viewport_handle=viewport.handle,
                bbox=model_box,
                confidence=classification.confidence,
                evidence=[
                    *classification.evidence,
                    f"virtual_panel:{viewport.id}",
                    f"selected_entities:{len(selected)}",
                    f"selected_paper_entities:{len(selected_paper)}",
                    f"paper_page_association_basis:{page_selection.basis}",
                    *(
                        [
                            "paper_page_reference_unresolved:"
                            + page_selection.basis
                            + "@"
                            + ",".join(page_selection.candidate_ids)
                        ]
                        if page_selection.blocks_fallback
                        else []
                    ),
                    *(
                        [f"paper_page_reference:{page_reference.code}@{page_reference.entity_id}"]
                        if page_reference is not None
                        else []
                    ),
                    *(
                        [
                            f"paper_page_closed_frame:{','.join(map(str, box))}"
                            f"@{page_reference.parent_handle}"
                            for box in page_reference.frame_boxes
                            if _frame_contains(box, spec.transform.paper_box)
                        ]
                        if page_reference is not None
                        and spec.transform is not None
                        and page_selection.basis == "NATIVE_CLOSED_FRAME_CONTAINS_VIEWPORT"
                        else []
                    ),
                ],
            )
            output.sheets.append(panel)
            panel_space = f"model@{paper_layout}#{viewport.handle or viewport.id}"
            for entity in selected:
                clone_id = _stable_id("panel_entity", sheet_id, entity.id)
                output.entities.append(
                    entity.model_copy(
                        update={
                            "id": clone_id,
                            "sheet_id": sheet_id,
                            "space": panel_space,
                            "geometry": {
                                **entity.geometry,
                                "original_entity_id": entity.id,
                                "panel_viewport_handle": viewport.handle,
                            },
                        }
                    )
                )
            if selected_paper and spec.transform is None:
                output.warnings.append(
                    f"{viewport.id}: unsupported paper-to-model transform; "
                    f"retained {len(selected_paper)} annotations on original layout"
                )
            elif spec.transform is not None:
                output.entities.extend(
                    _clone_paper_entity(
                        entity,
                        sheet_id=sheet_id,
                        panel_space=panel_space,
                        viewport=viewport,
                        transform=spec.transform,
                    )
                    for entity in selected_paper
                )
            panel_count += 1
        output.source_panel_counts[source_id] = panel_count
    split_local_drawing_panels(output)
    output.sheets.sort(key=lambda sheet: (sheet.source_file_id, sheet.layout or "", sheet.id))
    output.entities.sort(
        key=lambda entity: (entity.source_file_id, entity.sheet_id or "", entity.id)
    )
    return output


def choose_analysis_view(
    original_sheets: Sequence[Sheet],
    original_entities: Sequence[CadEntity],
    expansion: PanelExpansion,
) -> tuple[list[Sheet], list[CadEntity]]:
    """Use panels plus unrepresented model entities, never dropping source semantics.

    Viewport geometry in vendor drawings is sometimes incomplete or shifted. A
    source-wide fallback is therefore retained for every semantic entity that
    was not assigned to any virtual panel. Represented originals are omitted to
    avoid duplicating the same CAD handle at both panel and source level.
    """

    panel_sources = {
        source_id for source_id, count in expansion.source_panel_counts.items() if count > 0
    }
    represented_original_ids = {
        str(entity.geometry["original_entity_id"])
        for entity in expansion.entities
        if entity.geometry.get("original_entity_id")
    }
    fallback_entities = [
        entity
        for entity in original_entities
        if entity.source_file_id not in panel_sources
        or (entity.id not in represented_original_ids and entity.entity_type != "VIEWPORT")
    ]
    required_sheet_ids = {entity.sheet_id for entity in fallback_entities if entity.sheet_id}
    sheets = list(expansion.sheets)
    sheets.extend(sheet for sheet in original_sheets if sheet.id in required_sheet_ids)
    entities = [*expansion.entities, *fallback_entities]
    return sheets, entities
