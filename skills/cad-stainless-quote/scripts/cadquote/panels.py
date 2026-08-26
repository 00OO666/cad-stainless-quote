"""Expand paper-space viewports into virtual, evidence-addressable drawing panels."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .classifier import classify_sheet
from .models import CadEntity, Sheet

BBox = tuple[float, float, float, float]


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


def _intersects(left: BBox | None, right: BBox) -> bool:
    if left is None:
        return False
    return (
        left[0] <= right[2]
        and left[2] >= right[0]
        and left[1] <= right[3]
        and left[3] >= right[1]
    )


def _point_inside(point: tuple[float, float] | None, box: BBox) -> bool:
    return bool(point and box[0] <= point[0] <= box[2] and box[1] <= point[1] <= box[3])


def _entity_in_box(entity: CadEntity, box: BBox) -> bool:
    return _intersects(entity.bbox, box) or _point_inside(entity.insert, box)


def _paper_title_texts(viewport: CadEntity, paper_entities: Sequence[CadEntity]) -> list[str]:
    if viewport.bbox is None:
        return []
    x0, y0, x1, _ = viewport.bbox
    height = max(viewport.bbox[3] - viewport.bbox[1], 1.0)
    search = (x0, y0 - min(300.0, height * 0.4), x1, y0 + height * 0.12)
    candidates: list[tuple[float, str]] = []
    for entity in paper_entities:
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


def expand_viewport_panels(
    sheets: Sequence[Sheet],
    entities: Sequence[CadEntity],
    *,
    source_names: Mapping[str, str] | None = None,
    minimum_entity_count: int = 1,
) -> PanelExpansion:
    """Build virtual sheets for non-default paper-space viewports.

    Model entities are cloned with a panel-specific ID and sheet ID, but retain
    their original CAD handle. This makes downstream evidence unambiguous while
    leaving the normalized source index unchanged.
    """

    source_names = source_names or {}
    output = PanelExpansion()
    sources = sorted({sheet.source_file_id for sheet in sheets})
    for source_id in sources:
        source_entities = [entity for entity in entities if entity.source_file_id == source_id]
        model_entities = [entity for entity in source_entities if entity.space == "model"]
        paper_entities = [entity for entity in source_entities if entity.space.startswith("paper:")]
        viewports = [entity for entity in paper_entities if entity.entity_type == "VIEWPORT"]
        seen_boxes: set[tuple[str, tuple[float, ...]]] = set()
        panel_count = 0
        for viewport in sorted(viewports, key=lambda entity: (entity.space, entity.id)):
            viewport_id = viewport.geometry.get("viewport_id")
            if viewport_id is not None and int(viewport_id) <= 1:
                continue
            model_box, entity_count = _best_model_bbox(viewport, model_entities)
            if model_box is None or entity_count < minimum_entity_count:
                output.warnings.append(f"{viewport.id}: no usable model-space region")
                continue
            dedupe_key = (viewport.space, tuple(round(value, 5) for value in model_box))
            if dedupe_key in seen_boxes:
                continue
            seen_boxes.add(dedupe_key)
            paper_layout = viewport.space.removeprefix("paper:")
            title_texts = _paper_title_texts(viewport, paper_entities)
            selected = [entity for entity in model_entities if _entity_in_box(entity, model_box)]
            semantic_texts = [entity.text for entity in selected if entity.text]
            filename = Path(source_names.get(source_id, source_id)).name
            classification = classify_sheet(
                filename,
                [*title_texts, *semantic_texts],
                layout_name=paper_layout,
            )
            sheet_id = _stable_id("panel", source_id, paper_layout, viewport.handle, model_box)
            title = title_texts[0] if title_texts else classification.title
            panel = Sheet(
                id=sheet_id,
                source_file_id=source_id,
                drawing_number=classification.drawing_number,
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
                ],
            )
            output.sheets.append(panel)
            for entity in selected:
                clone_id = _stable_id("panel_entity", sheet_id, entity.id)
                output.entities.append(
                    entity.model_copy(
                        update={
                            "id": clone_id,
                            "sheet_id": sheet_id,
                            "space": f"model@{paper_layout}#{viewport.handle or viewport.id}",
                            "geometry": {
                                **entity.geometry,
                                "original_entity_id": entity.id,
                                "panel_viewport_handle": viewport.handle,
                            },
                        }
                    )
                )
            panel_count += 1
        output.source_panel_counts[source_id] = panel_count
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
        or (
            entity.id not in represented_original_ids
            and entity.entity_type != "VIEWPORT"
        )
    ]
    required_sheet_ids = {entity.sheet_id for entity in fallback_entities if entity.sheet_id}
    sheets = list(expansion.sheets)
    sheets.extend(sheet for sheet in original_sheets if sheet.id in required_sheet_ids)
    entities = [*expansion.entities, *fallback_entities]
    return sheets, entities
