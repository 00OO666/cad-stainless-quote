"""Build and render item-bound CAD evidence images for spreadsheet export.

The binding in this module is deliberately component-first.  An item can only
claim occurrences and measurements that belong to its exact ``component_id``;
an MT code is descriptive metadata and is never used as a join key.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from PIL import Image
from pydantic import Field

from .io import write_json_atomic
from .models import (
    CadEntity,
    ComponentInstance,
    EvidenceEdge,
    MeasurementCandidate,
    MtOccurrence,
    ReviewStatus,
    Sheet,
    StrictModel,
    TakeoffItem,
)
from .render import render_regions

EvidenceStage = Literal["plan", "elevation", "detail", "other", "missing"]
EvidenceTargetState = Literal["READY", "MISSING"]
EvidenceRenderState = Literal["RENDERED", "MISSING", "FAILED"]
EvidenceLifecycleState = Literal["CANDIDATE", "CONFIRMED", "MISSING"]
EvidenceRole = Literal[
    "location",
    "length",
    "height",
    "width",
    "unfolded_spec",
    "quantity",
]
BBox = tuple[float, float, float, float]
Point = tuple[float, float]


class EvidenceTarget(StrictModel):
    """A reproducible, item-bound request for one pair of CAD evidence images."""

    id: str
    sequence: int = Field(ge=1)
    component_id: str | None = None
    mt_code: str
    stage: EvidenceStage
    roles: list[EvidenceRole] = Field(default_factory=list)
    source_file_id: str | None = None
    sheet_id: str | None = None
    drawing_number: str | None = None
    sheet_title: str | None = None
    layout: str | None = None
    occurrence_ids: list[str] = Field(default_factory=list)
    measurement_ids: list[str] = Field(default_factory=list)
    entity_ids: list[str] = Field(default_factory=list)
    entity_handles: list[str] = Field(default_factory=list)
    anchor_points: list[Point] = Field(default_factory=list)
    focus_bbox: BBox | None = None
    detail_bbox: BBox | None = None
    context_bbox: BBox | None = None
    status: ReviewStatus = ReviewStatus.REVIEW
    state: EvidenceTargetState = "MISSING"
    evidence_state: EvidenceLifecycleState = "MISSING"
    reason: str | None = None


class EvidenceRecord(EvidenceTarget):
    """Rendered result for an :class:`EvidenceTarget`, including file integrity."""

    render_state: EvidenceRenderState = "MISSING"
    source_sha256: str | None = None
    context_image: str | None = None
    detail_image: str | None = None
    context_target_px: int | None = Field(default=None, gt=0)
    detail_target_px: int | None = Field(default=None, gt=0)
    context_pixel_size: tuple[int, int] | None = None
    detail_pixel_size: tuple[int, int] | None = None
    context_aspect_ratio: float | None = Field(default=None, gt=0)
    detail_aspect_ratio: float | None = Field(default=None, gt=0)
    context_sha256: str | None = None
    detail_sha256: str | None = None
    context_backend: str | None = None
    detail_backend: str | None = None
    render_reason: str | None = None


@dataclass(slots=True)
class _TargetDraft:
    sequence: int
    component_id: str
    mt_code: str
    stage: EvidenceStage
    source_file_id: str | None
    sheet_id: str | None
    status: ReviewStatus
    roles: set[EvidenceRole] = field(default_factory=set)
    occurrence_ids: set[str] = field(default_factory=set)
    measurement_ids: set[str] = field(default_factory=set)
    entity_ids: set[str] = field(default_factory=set)
    anchor_points: list[Point] = field(default_factory=list)
    missing_anchor_occurrence_ids: set[str] = field(default_factory=set)
    reasons: list[str] = field(default_factory=list)


def _stable_id(prefix: str, payload: object) -> str:
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return f"{prefix}:{hashlib.sha256(encoded).hexdigest()[:24]}"


def _safe_label(value: str) -> str:
    label = re.sub(r"[^0-9A-Za-z._\u4e00-\u9fff-]+", "_", value).strip(" ._")
    return label[:72] or "evidence"


def _sheet_stage(sheet: Sheet | None) -> EvidenceStage:
    if sheet is None:
        return "other"
    if sheet.kind in {"plan", "elevation_index"}:
        return "plan"
    if sheet.kind == "elevation":
        return "elevation"
    if sheet.kind in {"detail", "door", "ceiling", "floor"}:
        return "detail"
    return "other"


def _valid_point(value: Sequence[float] | None) -> Point | None:
    if value is None or len(value) < 2:
        return None
    try:
        point = float(value[0]), float(value[1])
    except (TypeError, ValueError):
        return None
    return point if all(math.isfinite(part) for part in point) else None


def _valid_bbox(value: Sequence[float] | None) -> BBox | None:
    if value is None or len(value) != 4:
        return None
    try:
        bbox = tuple(float(part) for part in value)
    except (TypeError, ValueError):
        return None
    if not all(math.isfinite(part) for part in bbox):
        return None
    x0, y0, x1, y1 = bbox
    if x1 < x0 or y1 < y0:
        return None
    return x0, y0, x1, y1


def _focus_bbox(
    entity_ids: Sequence[str],
    anchors: Sequence[Point],
    entity_by_id: Mapping[str, CadEntity],
) -> BBox | None:
    x_values: list[float] = []
    y_values: list[float] = []
    for entity_id in entity_ids:
        entity = entity_by_id.get(entity_id)
        if entity is None:
            continue
        bbox = _valid_bbox(entity.bbox)
        if bbox is not None:
            x_values.extend((bbox[0], bbox[2]))
            y_values.extend((bbox[1], bbox[3]))
            continue
        point = _valid_point(entity.insert)
        if point is not None:
            x_values.append(point[0])
            y_values.append(point[1])
    for point in anchors:
        valid = _valid_point(point)
        if valid is not None:
            x_values.append(valid[0])
            y_values.append(valid[1])
    if not x_values or not y_values:
        return None
    return min(x_values), min(y_values), max(x_values), max(y_values)


def _expanded_boxes(focus: BBox, sheet_bbox: BBox | None) -> tuple[BBox, BBox, BBox]:
    x0, y0, x1, y1 = focus
    width = max(0.0, x1 - x0)
    height = max(0.0, y1 - y0)
    sheet_diagonal = 0.0
    if sheet_bbox is not None:
        sheet_diagonal = math.hypot(
            sheet_bbox[2] - sheet_bbox[0],
            sheet_bbox[3] - sheet_bbox[1],
        )
    focus_diagonal = math.hypot(width, height)
    base = max(1.0, sheet_diagonal * 0.008, focus_diagonal * 0.1)
    center_x = (x0 + x1) / 2
    center_y = (y0 + y1) / 2

    focus_half_width = max(width / 2, base * 0.25)
    focus_half_height = max(height / 2, base * 0.25)
    normalized_focus = (
        center_x - focus_half_width,
        center_y - focus_half_height,
        center_x + focus_half_width,
        center_y + focus_half_height,
    )

    detail_half_width = max(focus_half_width * 1.65, base)
    detail_half_height = max(focus_half_height * 1.65, base)
    detail = (
        center_x - detail_half_width,
        center_y - detail_half_height,
        center_x + detail_half_width,
        center_y + detail_half_height,
    )

    context_half_width = max(detail_half_width * 2.75, detail_half_width + base)
    context_half_height = max(detail_half_height * 2.75, detail_half_height + base)
    context = (
        center_x - context_half_width,
        center_y - context_half_height,
        center_x + context_half_width,
        center_y + context_half_height,
    )
    return normalized_focus, detail, context


def _missing_target(item: TakeoffItem, reason: str) -> EvidenceTarget:
    payload = {
        "sequence": item.sequence,
        "component_id": item.component_id,
        "stage": "missing",
        "reason": reason,
    }
    return EvidenceTarget(
        id=_stable_id("excel-evidence", payload),
        sequence=item.sequence,
        component_id=item.component_id,
        mt_code=item.mt_code,
        stage="missing",
        status=ReviewStatus.BLOCK,
        state="MISSING",
        evidence_state="MISSING",
        reason=reason,
    )


def _selected_measurements(
    component_id: str,
    measurements: Sequence[MeasurementCandidate],
    edges: Sequence[EvidenceEdge],
) -> list[MeasurementCandidate]:
    candidates = [value for value in measurements if value.component_id == component_id]
    if not candidates:
        return []
    edges_by_target: dict[str, list[EvidenceEdge]] = defaultdict(list)
    for edge in edges:
        if edge.relation == "component_to_dimension" and edge.source_id == component_id:
            edges_by_target[edge.target_id].append(edge)
    if edges_by_target:
        candidates = [value for value in candidates if value.id in edges_by_target]

    by_role: dict[str, list[MeasurementCandidate]] = defaultdict(list)
    for candidate in candidates:
        by_role[candidate.role].append(candidate)
    selected: list[MeasurementCandidate] = []
    for role in sorted(by_role):
        ranked = sorted(
            by_role[role],
            key=lambda value: (-value.confidence, value.id),
        )
        passed = [
            candidate
            for candidate in ranked
            if any(
                edge.status == ReviewStatus.PASS
                for edge in edges_by_target.get(candidate.id, ())
            )
        ]
        if passed:
            selected.extend(passed)
            continue
        unblocked = [candidate for candidate in ranked if candidate.status != ReviewStatus.BLOCK]
        if unblocked:
            selected.append(unblocked[0])
        elif ranked:
            selected.append(ranked[0])
    return selected


def _draft_for(
    drafts: dict[tuple[EvidenceStage, str | None, str | None], _TargetDraft],
    item: TakeoffItem,
    component: ComponentInstance,
    stage: EvidenceStage,
    source_file_id: str | None,
    sheet_id: str | None,
) -> _TargetDraft:
    key = stage, sheet_id, source_file_id
    if key not in drafts:
        drafts[key] = _TargetDraft(
            sequence=item.sequence,
            component_id=component.id,
            mt_code=item.mt_code,
            stage=stage,
            source_file_id=source_file_id,
            sheet_id=sheet_id,
            status=item.status,
        )
    return drafts[key]


def _finalize_draft(
    draft: _TargetDraft,
    sheet_by_id: Mapping[str, Sheet],
    entity_by_id: Mapping[str, CadEntity],
) -> EvidenceTarget:
    sheet = sheet_by_id.get(draft.sheet_id or "")
    entity_ids = sorted(draft.entity_ids)
    missing_entity_ids = [value for value in entity_ids if value not in entity_by_id]
    reasons = list(draft.reasons)
    if missing_entity_ids:
        reasons.append(f"找不到实体：{','.join(missing_entity_ids[:8])}")
    handles = sorted(
        {
            entity.handle
            for entity_id in entity_ids
            if (entity := entity_by_id.get(entity_id)) is not None and entity.handle
        }
    )
    focus = _focus_bbox(entity_ids, draft.anchor_points, entity_by_id)
    normalized_focus: BBox | None = None
    detail: BBox | None = None
    context: BBox | None = None
    state: EvidenceTargetState = "READY"
    status = draft.status
    if draft.missing_anchor_occurrence_ids:
        state = "MISSING"
        status = ReviewStatus.BLOCK
    if focus is None:
        state = "MISSING"
        status = ReviewStatus.BLOCK
        reasons.append("缺少可定位的实体 bbox、插入点或 MT/引线锚点")
    else:
        normalized_focus, detail, context = _expanded_boxes(
            focus,
            _valid_bbox(sheet.bbox) if sheet is not None else None,
        )
    if draft.sheet_id is None:
        state = "MISSING"
        status = ReviewStatus.BLOCK
        reasons.append("缺少图纸 ID")
    if draft.source_file_id is None:
        state = "MISSING"
        status = ReviewStatus.BLOCK
        reasons.append("缺少来源文件 ID")
    reason = "；".join(dict.fromkeys(value for value in reasons if value)) or None
    identity = {
        "sequence": draft.sequence,
        "component_id": draft.component_id,
        "stage": draft.stage,
        "source_file_id": draft.source_file_id,
        "sheet_id": draft.sheet_id,
        "occurrence_ids": sorted(draft.occurrence_ids),
        "measurement_ids": sorted(draft.measurement_ids),
    }
    return EvidenceTarget(
        id=_stable_id("excel-evidence", identity),
        sequence=draft.sequence,
        component_id=draft.component_id,
        mt_code=draft.mt_code,
        stage=draft.stage,
        roles=sorted(draft.roles),
        source_file_id=draft.source_file_id,
        sheet_id=draft.sheet_id,
        drawing_number=sheet.drawing_number if sheet is not None else None,
        sheet_title=sheet.title if sheet is not None else None,
        layout=sheet.layout if sheet is not None else None,
        occurrence_ids=sorted(draft.occurrence_ids),
        measurement_ids=sorted(draft.measurement_ids),
        entity_ids=entity_ids,
        entity_handles=handles,
        anchor_points=sorted(set(draft.anchor_points)),
        focus_bbox=normalized_focus,
        detail_bbox=detail,
        context_bbox=context,
        status=status,
        state=state,
        evidence_state=(
            "MISSING"
            if state == "MISSING"
            else "CONFIRMED"
            if status == ReviewStatus.PASS
            else "CANDIDATE"
        ),
        reason=reason,
    )


def build_excel_evidence_targets(
    items: Sequence[TakeoffItem],
    components: Sequence[ComponentInstance],
    occurrences: Sequence[MtOccurrence],
    measurements: Sequence[MeasurementCandidate],
    edges: Sequence[EvidenceEdge],
    sheets: Sequence[Sheet],
    entities: Sequence[CadEntity],
) -> list[EvidenceTarget]:
    """Build render targets using exact component membership, never MT similarity.

    Targets are grouped by component, evidence stage, source file and sheet.  A
    missing component, occurrence anchor or entity location remains present as
    an explicit ``MISSING`` target so spreadsheet export can fail closed.
    """

    component_by_id = {value.id: value for value in components}
    occurrence_by_id = {value.id: value for value in occurrences}
    sheet_by_id = {value.id: value for value in sheets}
    entity_by_id = {value.id: value for value in entities}
    output: list[EvidenceTarget] = []

    for item in sorted(items, key=lambda value: (value.sequence, value.component_id or "")):
        if item.component_id is None:
            output.append(_missing_target(item, "报价项缺少 component_id，禁止按 MT 编号猜测"))
            continue
        component = component_by_id.get(item.component_id)
        if component is None:
            output.append(
                _missing_target(item, f"找不到精确构件 {item.component_id}，禁止按 MT 编号猜测")
            )
            continue

        drafts: dict[tuple[EvidenceStage, str | None, str | None], _TargetDraft] = {}
        occurrence_stages = (
            ("plan", component.plan_occurrence_ids),
            ("elevation", component.elevation_occurrence_ids),
        )
        for stage, occurrence_ids in occurrence_stages:
            for occurrence_id in occurrence_ids:
                occurrence = occurrence_by_id.get(occurrence_id)
                if occurrence is None:
                    draft = _draft_for(drafts, item, component, stage, None, None)
                    draft.occurrence_ids.add(occurrence_id)
                    draft.roles.add("location")
                    draft.reasons.append(f"找不到 occurrence：{occurrence_id}")
                    continue
                draft = _draft_for(
                    drafts,
                    item,
                    component,
                    stage,
                    occurrence.source_file_id,
                    occurrence.sheet_id,
                )
                draft.occurrence_ids.add(occurrence.id)
                draft.roles.add("location")
                draft.entity_ids.update(occurrence.entity_ids)
                if occurrence.leader_entity_id:
                    draft.entity_ids.add(occurrence.leader_entity_id)
                point = _valid_point(occurrence.leader_target or occurrence.anchor)
                if point is not None:
                    draft.anchor_points.append(point)
                else:
                    draft.missing_anchor_occurrence_ids.add(occurrence.id)
                    draft.reasons.append(f"{occurrence.id} 缺少 MT/引线锚点")

        for candidate in _selected_measurements(component.id, measurements, edges):
            sheet = sheet_by_id.get(candidate.sheet_id or "")
            stage = _sheet_stage(sheet)
            draft = _draft_for(
                drafts,
                item,
                component,
                stage,
                candidate.source_file_id,
                candidate.sheet_id,
            )
            draft.measurement_ids.add(candidate.id)
            draft.roles.add(candidate.role)
            draft.entity_ids.update(candidate.entity_ids)

        # Keep the three required evidence-chain stages visible even when a
        # drawing association or a render anchor is missing.  This makes the
        # spreadsheet fail closed with an explicit red ``缺图`` row instead of
        # silently omitting the absent plan/elevation/detail evidence.
        if not any(draft.stage == "plan" for draft in drafts.values()):
            draft = _draft_for(drafts, item, component, "plan", None, None)
            draft.roles.add("location")
            draft.reasons.append("构件未关联平面 MT 位置")
        if not any(draft.stage == "elevation" for draft in drafts.values()):
            draft = _draft_for(drafts, item, component, "elevation", None, None)
            draft.roles.add("location")
            draft.reasons.append("构件未关联立面 MT 位置")
        if not any(draft.stage == "detail" for draft in drafts.values()):
            if component.detail_sheet_ids:
                for detail_sheet_id in component.detail_sheet_ids:
                    detail_sheet = sheet_by_id.get(detail_sheet_id)
                    draft = _draft_for(
                        drafts,
                        item,
                        component,
                        "detail",
                        detail_sheet.source_file_id if detail_sheet is not None else None,
                        detail_sheet_id,
                    )
                    draft.reasons.append("节点/大样页缺少构件级尺寸实体或局部锚点")
            else:
                draft = _draft_for(drafts, item, component, "detail", None, None)
                draft.reasons.append("构件未关联节点/大样页")

        if not drafts:
            output.append(_missing_target(item, "构件没有可绑定的 occurrence 或 measurement"))
            continue
        output.extend(
            _finalize_draft(draft, sheet_by_id, entity_by_id)
            for _, draft in sorted(
                drafts.items(),
                key=lambda pair: (
                    pair[0][0],
                    pair[0][1] or "",
                    pair[0][2] or "",
                ),
            )
        )

    return sorted(
        output,
        key=lambda value: (value.sequence, value.stage, value.sheet_id or "", value.id),
    )


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _image_metadata(path: Path, root: Path) -> tuple[str, tuple[int, int], str]:
    with Image.open(path) as image:
        pixel_size = int(image.width), int(image.height)
    relative = str(path.relative_to(root)).replace("\\", "/")
    return relative, pixel_size, _sha256_file(path)


def _render_one(
    source: Path,
    target: EvidenceTarget,
    region: BBox,
    output_dir: Path,
    *,
    target_px: int,
) -> tuple[Path, str]:
    layout = (
        target.layout
        if target.layout and target.layout != "Model" and "#viewport:" not in target.layout
        else "Model"
    )
    result = render_regions(
        source,
        {target.id: region},
        output_dir,
        layout=layout,
        margin_ratio=0.0,
        target_px=target_px,
        mark_center=True,
    )
    record = result.get("regions", {}).get(target.id)
    if not isinstance(record, Mapping) or not record.get("file"):
        raise RuntimeError("渲染器未输出图片")
    path = output_dir / str(record["file"])
    if not path.is_file() or path.stat().st_size == 0:
        raise RuntimeError("渲染器返回的图片不存在或为空")
    return path, str(record.get("backend") or "unknown")


def render_excel_evidence(
    targets: Sequence[EvidenceTarget],
    source_dxfs: Mapping[str, Path | str],
    output_dir: Path | str,
    *,
    context_target_px: int = 2_800,
    detail_target_px: int = 2_200,
) -> list[EvidenceRecord]:
    """Render a context/detail pair per target and write ``index.json``.

    Every input target produces exactly one output record.  Missing sources,
    invalid coordinates and renderer failures are recorded as ``MISSING`` or
    ``FAILED`` and never disappear from the result.
    """

    destination = Path(output_dir).expanduser().resolve()
    destination.mkdir(parents=True, exist_ok=True)
    records: list[EvidenceRecord] = []
    source_hashes: dict[Path, str] = {}
    for target in targets:
        payload = target.model_dump()
        source: Path | None = None
        source_sha256: str | None = None
        source_problem: str | None = None
        if target.source_file_id is not None:
            source_value = source_dxfs.get(target.source_file_id)
            if source_value is None:
                source_problem = f"找不到 {target.source_file_id} 的 DXF 路径"
            else:
                source = Path(source_value).expanduser().resolve()
                if not source.is_file():
                    source_problem = f"DXF 文件不存在：{source}"
                else:
                    try:
                        if source not in source_hashes:
                            source_hashes[source] = _sha256_file(source)
                        source_sha256 = source_hashes[source]
                    except OSError as exc:
                        source_problem = f"DXF 哈希读取失败：{type(exc).__name__}: {exc}"
        if (
            target.state == "MISSING"
            or target.context_bbox is None
            or target.detail_bbox is None
            or target.source_file_id is None
        ):
            records.append(
                EvidenceRecord(
                    **{**payload, "evidence_state": "MISSING"},
                    render_state="MISSING",
                    source_sha256=source_sha256,
                    context_target_px=context_target_px,
                    detail_target_px=detail_target_px,
                    render_reason="；".join(
                        value
                        for value in (
                            target.reason or "证据目标缺少渲染条件",
                            source_problem,
                        )
                        if value
                    ),
                )
            )
            continue

        if source is None or source_problem is not None:
            records.append(
                EvidenceRecord(
                    **{**payload, "evidence_state": "MISSING"},
                    render_state="FAILED",
                    source_sha256=source_sha256,
                    context_target_px=context_target_px,
                    detail_target_px=detail_target_px,
                    render_reason=source_problem or "来源 DXF 不可用",
                )
            )
            continue
        target_root = destination / (
            f"{_safe_label(target.id)}-{hashlib.sha256(target.id.encode()).hexdigest()[:8]}"
        )
        context_image: str | None = None
        detail_image: str | None = None
        context_pixel_size: tuple[int, int] | None = None
        detail_pixel_size: tuple[int, int] | None = None
        context_sha256: str | None = None
        detail_sha256: str | None = None
        context_backend: str | None = None
        detail_backend: str | None = None
        failures: list[str] = []

        try:
            path, context_backend = _render_one(
                source,
                target,
                target.context_bbox,
                target_root / "context",
                target_px=context_target_px,
            )
            context_image, context_pixel_size, context_sha256 = _image_metadata(
                path,
                destination,
            )
        except Exception as exc:
            failures.append(f"定位图失败：{type(exc).__name__}: {exc}")
        try:
            path, detail_backend = _render_one(
                source,
                target,
                target.detail_bbox,
                target_root / "detail",
                target_px=detail_target_px,
            )
            detail_image, detail_pixel_size, detail_sha256 = _image_metadata(
                path,
                destination,
            )
        except Exception as exc:
            failures.append(f"放大图失败：{type(exc).__name__}: {exc}")

        records.append(
            EvidenceRecord(
                **{
                    **payload,
                    "evidence_state": "MISSING" if failures else target.evidence_state,
                },
                render_state="FAILED" if failures else "RENDERED",
                source_sha256=source_sha256,
                context_image=context_image,
                detail_image=detail_image,
                context_target_px=context_target_px,
                detail_target_px=detail_target_px,
                context_pixel_size=context_pixel_size,
                detail_pixel_size=detail_pixel_size,
                context_aspect_ratio=(
                    context_pixel_size[0] / context_pixel_size[1]
                    if context_pixel_size is not None
                    else None
                ),
                detail_aspect_ratio=(
                    detail_pixel_size[0] / detail_pixel_size[1]
                    if detail_pixel_size is not None
                    else None
                ),
                context_sha256=context_sha256,
                detail_sha256=detail_sha256,
                context_backend=context_backend,
                detail_backend=detail_backend,
                render_reason="；".join(failures) or None,
            )
        )

    write_json_atomic(
        destination / "index.json",
        {
            "schema_version": "1.0",
            "target_count": len(targets),
            "record_count": len(records),
            "rendered_count": sum(value.render_state == "RENDERED" for value in records),
            "missing_count": sum(value.render_state == "MISSING" for value in records),
            "failed_count": sum(value.render_state == "FAILED" for value in records),
            "records": [value.model_dump(mode="json") for value in records],
        },
    )
    return records


__all__ = [
    "EvidenceRecord",
    "EvidenceTarget",
    "build_excel_evidence_targets",
    "render_excel_evidence",
]
