"""Deterministic quality checks for row-bound spreadsheet evidence images.

The checks in this module are intentionally negative-only.  They can prove that
evidence is missing, suspiciously reused, visually near blank, or embedded too
small to review.  They never infer that an image depicts the correct component.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from PIL import Image, ImageOps
from pydantic import Field, field_validator

from .io import sha256_file, write_json_atomic
from .models import ReviewStatus, StrictModel

ImageKind = Literal["human", "ai"]


class EvidenceQualityThresholds(StrictModel):
    """Versioned, serializable thresholds for evidence-image QA."""

    near_white_channel_min: int = Field(default=248, ge=0, le=255)
    near_white_ratio_threshold: float = Field(default=0.90, ge=0, le=1)
    min_display_scale: float = Field(default=0.20, gt=0, le=1)
    max_analysis_pixels: int = Field(default=1_000_000, ge=10_000)
    require_ai_image: bool = True
    require_human_image: bool = False
    check_human_blank: bool = False
    check_human_display_scale: bool = False
    missing_ai_status: ReviewStatus = ReviewStatus.BLOCK
    missing_human_status: ReviewStatus = ReviewStatus.REVIEW
    unreadable_ai_status: ReviewStatus = ReviewStatus.BLOCK
    unreadable_human_status: ReviewStatus = ReviewStatus.REVIEW
    reuse_name_status: ReviewStatus = ReviewStatus.BLOCK
    reuse_stage_status: ReviewStatus = ReviewStatus.BLOCK
    near_white_status: ReviewStatus = ReviewStatus.REVIEW
    small_display_status: ReviewStatus = ReviewStatus.REVIEW

    @field_validator(
        "missing_ai_status",
        "missing_human_status",
        "unreadable_ai_status",
        "unreadable_human_status",
        "reuse_name_status",
        "reuse_stage_status",
        "near_white_status",
        "small_display_status",
    )
    @classmethod
    def _failure_status_must_not_pass(cls, value: ReviewStatus) -> ReviewStatus:
        if value == ReviewStatus.PASS:
            raise ValueError("a failed evidence-quality check must be REVIEW or BLOCK")
        return value


class EvidenceImageMetric(StrictModel):
    """Auditable metrics for one row/image use, not merely one unique file."""

    id: str
    row_id: str
    kind: ImageKind
    component_name: str | None = None
    component_id: str | None = None
    stage: str | None = None
    occurrence_ids: list[str] = Field(default_factory=list)
    evidence_role: str | None = None
    source_path: str
    resolved_path: str
    exists: bool
    readable: bool
    sha256: str | None = None
    pixel_width: int | None = Field(default=None, gt=0)
    pixel_height: int | None = Field(default=None, gt=0)
    sampled_pixel_width: int | None = Field(default=None, gt=0)
    sampled_pixel_height: int | None = Field(default=None, gt=0)
    near_white_ratio: float | None = Field(default=None, ge=0, le=1)
    display_width_px: float | None = Field(default=None, gt=0)
    display_height_px: float | None = Field(default=None, gt=0)
    display_scale: float | None = Field(default=None, gt=0)
    status: ReviewStatus = ReviewStatus.PASS
    issue_codes: list[str] = Field(default_factory=list)


class EvidenceQualityIssue(StrictModel):
    id: str
    code: str
    status: ReviewStatus
    message: str
    row_ids: list[str] = Field(default_factory=list)
    image_metric_ids: list[str] = Field(default_factory=list)
    evidence: dict[str, Any] = Field(default_factory=dict)

    @field_validator("status")
    @classmethod
    def _issue_status_must_not_pass(cls, value: ReviewStatus) -> ReviewStatus:
        if value == ReviewStatus.PASS:
            raise ValueError("an evidence-quality issue must be REVIEW or BLOCK")
        return value


class EvidenceQualityRowMetric(StrictModel):
    row_id: str
    record_index: int = Field(ge=1)
    component_name: str | None = None
    component_id: str | None = None
    stage: str | None = None
    occurrence_ids: list[str] = Field(default_factory=list)
    human_image_count: int = Field(default=0, ge=0)
    ai_image_count: int = Field(default=0, ge=0)
    image_metric_ids: list[str] = Field(default_factory=list)
    status: ReviewStatus = ReviewStatus.PASS
    issue_codes: list[str] = Field(default_factory=list)


class EvidenceQualitySummary(StrictModel):
    row_count: int = Field(ge=0)
    image_use_count: int = Field(ge=0)
    unique_ai_image_count: int = Field(ge=0)
    missing_image_count: int = Field(ge=0)
    unreadable_image_count: int = Field(ge=0)
    blank_image_count: int = Field(ge=0)
    small_display_count: int = Field(ge=0)
    reused_across_component_name_group_count: int = Field(ge=0)
    reused_across_stage_group_count: int = Field(ge=0)
    issue_count: int = Field(ge=0)
    pass_row_count: int = Field(ge=0)
    review_row_count: int = Field(ge=0)
    block_row_count: int = Field(ge=0)


class EvidenceQualityReport(StrictModel):
    schema_version: Literal["1.0"] = "1.0"
    status: ReviewStatus
    thresholds: EvidenceQualityThresholds
    summary: EvidenceQualitySummary
    rows: list[EvidenceQualityRowMetric]
    images: list[EvidenceImageMetric]
    issues: list[EvidenceQualityIssue]

    def write_json(self, path: str | Path) -> None:
        write_json_atomic(Path(path), self.model_dump(mode="json"))


@dataclass(slots=True)
class _ImageInput:
    kind: ImageKind
    path: str
    stage: str | None
    evidence_role: str | None
    display_width_px: float | None
    display_height_px: float | None


@dataclass(slots=True)
class _FileFacts:
    exists: bool
    readable: bool
    sha256: str | None = None
    pixel_width: int | None = None
    pixel_height: int | None = None
    sampled_pixel_width: int | None = None
    sampled_pixel_height: int | None = None
    near_white_ratio: float | None = None


_STATUS_RANK = {
    ReviewStatus.PASS: 0,
    ReviewStatus.REVIEW: 1,
    ReviewStatus.BLOCK: 2,
}


def _max_status(*values: ReviewStatus) -> ReviewStatus:
    return max(values, key=_STATUS_RANK.__getitem__)


def _stable_id(prefix: str, payload: object) -> str:
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return f"{prefix}:{hashlib.sha256(encoded).hexdigest()[:24]}"


def _text(value: object) -> str | None:
    if value is None:
        return None
    normalized = re.sub(r"\s+", " ", str(value)).strip()
    return normalized or None


def _first(record: Mapping[str, Any], *keys: str) -> object:
    for key in keys:
        if key in record and record[key] is not None:
            return record[key]
    return None


def _string_list(value: object) -> list[str]:
    if value is None:
        return []
    if isinstance(value, (str, Path)):
        text = _text(value)
        return [text] if text else []
    if isinstance(value, Sequence) and not isinstance(value, (bytes, bytearray)):
        output: list[str] = []
        for item in value:
            text = _text(item)
            if text and text not in output:
                output.append(text)
        return output
    text = _text(value)
    return [text] if text else []


def _positive_float(value: object) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) and result > 0 else None


def _as_mapping(record: object) -> Mapping[str, Any]:
    if isinstance(record, Mapping):
        return record
    model_dump = getattr(record, "model_dump", None)
    if callable(model_dump):
        payload = model_dump(mode="python")
        if isinstance(payload, Mapping):
            return payload
    raise TypeError("each evidence-quality input record must be a mapping or Pydantic model")


def _raw_image_values(
    record: Mapping[str, Any],
    kind: ImageKind,
) -> list[tuple[object, str | None]]:
    if kind == "human":
        aliases = (
            ("human_paths", None),
            ("human_path", None),
            ("human_evidence_paths", None),
            ("human_evidence_path", None),
            ("human_images", None),
        )
    else:
        aliases = (
            ("ai_paths", None),
            ("ai_path", None),
            ("ai_evidence_paths", None),
            ("ai_evidence_path", None),
            ("ai_images", None),
            ("context_image", "locator"),
            ("locator_path", "locator"),
            ("detail_image", "closeup"),
            ("closeup_path", "closeup"),
        )
    output: list[tuple[object, str | None]] = []
    for key, role in aliases:
        if key not in record or record[key] is None:
            continue
        value = record[key]
        if isinstance(value, Sequence) and not isinstance(
            value, (str, bytes, bytearray, Path, Mapping)
        ):
            output.extend((item, role) for item in value)
        else:
            output.append((value, role))
    return output


def _normalize_images(
    record: Mapping[str, Any],
    kind: ImageKind,
    row_stage: str | None,
) -> list[_ImageInput]:
    row_width = _positive_float(record.get(f"{kind}_display_width_px"))
    row_height = _positive_float(record.get(f"{kind}_display_height_px"))
    output: list[_ImageInput] = []
    seen: set[tuple[str, str | None, str | None, float | None, float | None]] = set()
    for raw, alias_role in _raw_image_values(record, kind):
        if isinstance(raw, Mapping):
            path = _text(_first(raw, "path", "image_path", "file", "source_path"))
            stage = _text(raw.get("stage")) or row_stage
            role = _text(_first(raw, "evidence_role", "role", "image_role")) or alias_role
            width = _positive_float(
                _first(raw, "display_width_px", "embedded_width_px", "width_px")
            )
            height = _positive_float(
                _first(raw, "display_height_px", "embedded_height_px", "height_px")
            )
        else:
            path = _text(raw)
            stage = row_stage
            role = alias_role
            width = None
            height = None
        if not path:
            continue
        width = width or row_width
        height = height or row_height
        key = path, stage, role, width, height
        if key in seen:
            continue
        seen.add(key)
        output.append(
            _ImageInput(
                kind=kind,
                path=path,
                stage=stage,
                evidence_role=role,
                display_width_px=width,
                display_height_px=height,
            )
        )
    return output


def _resolve_path(path: str, base_dir: Path | None) -> Path:
    candidate = Path(path)
    if not candidate.is_absolute() and base_dir is not None:
        candidate = base_dir / candidate
    return candidate.resolve(strict=False)


def _near_white_ratio(image: Image.Image, channel_min: int) -> float:
    rgba = image.convert("RGBA")
    background = Image.new("RGBA", rgba.size, (255, 255, 255, 255))
    background.alpha_composite(rgba)
    rgb = background.convert("RGB")
    pixel_count = rgb.width * rgb.height
    if pixel_count <= 0:
        return 1.0
    get_pixels = getattr(rgb, "get_flattened_data", None)
    pixels = get_pixels() if callable(get_pixels) else rgb.getdata()
    white_count = sum(
        1
        for red, green, blue in pixels
        if red >= channel_min and green >= channel_min and blue >= channel_min
    )
    return white_count / pixel_count


def _inspect_file(path: Path, thresholds: EvidenceQualityThresholds) -> _FileFacts:
    if not path.is_file():
        return _FileFacts(exists=False, readable=False)
    digest = sha256_file(path)
    try:
        with Image.open(path) as source:
            source.load()
            image = ImageOps.exif_transpose(source)
            width, height = image.size
            sample = image.copy()
            if sample.width * sample.height > thresholds.max_analysis_pixels:
                side = max(1, int(math.sqrt(thresholds.max_analysis_pixels)))
                sample.thumbnail((side, side), Image.Resampling.BOX)
            ratio = _near_white_ratio(sample, thresholds.near_white_channel_min)
            return _FileFacts(
                exists=True,
                readable=True,
                sha256=digest,
                pixel_width=width,
                pixel_height=height,
                sampled_pixel_width=sample.width,
                sampled_pixel_height=sample.height,
                near_white_ratio=ratio,
            )
    except (OSError, ValueError):
        return _FileFacts(exists=True, readable=False, sha256=digest)


def _display_scale(
    display_width_px: float | None,
    display_height_px: float | None,
    pixel_width: int | None,
    pixel_height: int | None,
) -> float | None:
    ratios: list[float] = []
    if display_width_px is not None and pixel_width:
        ratios.append(display_width_px / pixel_width)
    if display_height_px is not None and pixel_height:
        ratios.append(display_height_px / pixel_height)
    return min(ratios) if ratios else None


def audit_evidence_quality(
    records: Sequence[Mapping[str, Any] | object],
    *,
    thresholds: EvidenceQualityThresholds | Mapping[str, Any] | None = None,
    base_dir: str | Path | None = None,
) -> EvidenceQualityReport:
    """Audit row-level human/AI evidence records without judging correctness.

    Image fields may be strings, lists of strings, or mappings containing
    ``path``, optional ``stage``, and optional embedded ``display_*_px`` values.
    Supported aliases include ``human_paths``, ``ai_paths``, ``context_image``,
    and ``detail_image``.
    """

    policy = (
        thresholds
        if isinstance(thresholds, EvidenceQualityThresholds)
        else EvidenceQualityThresholds.model_validate(thresholds or {})
    )
    root = Path(base_dir).resolve(strict=False) if base_dir is not None else None
    rows: list[EvidenceQualityRowMetric] = []
    images: list[EvidenceImageMetric] = []
    issues: list[EvidenceQualityIssue] = []
    file_cache: dict[str, _FileFacts] = {}
    row_by_index: dict[int, EvidenceQualityRowMetric] = {}
    image_by_id: dict[str, EvidenceImageMetric] = {}

    def add_issue(
        code: str,
        status: ReviewStatus,
        message: str,
        *,
        affected_rows: Sequence[EvidenceQualityRowMetric],
        affected_images: Sequence[EvidenceImageMetric] = (),
        evidence: Mapping[str, Any] | None = None,
    ) -> None:
        row_ids = sorted({row.row_id for row in affected_rows})
        image_ids = sorted({image.id for image in affected_images})
        issue = EvidenceQualityIssue(
            id=_stable_id(
                "evidence-quality-issue",
                {"code": code, "rows": row_ids, "images": image_ids},
            ),
            code=code,
            status=status,
            message=message,
            row_ids=row_ids,
            image_metric_ids=image_ids,
            evidence=dict(evidence or {}),
        )
        issues.append(issue)
        for row in affected_rows:
            row.status = _max_status(row.status, status)
            if code not in row.issue_codes:
                row.issue_codes.append(code)
        for image in affected_images:
            image.status = _max_status(image.status, status)
            if code not in image.issue_codes:
                image.issue_codes.append(code)

    for record_index, raw_record in enumerate(records, start=1):
        record = _as_mapping(raw_record)
        row_id = _text(_first(record, "row_id", "id", "sequence")) or str(record_index)
        name = _text(_first(record, "name", "component_name"))
        component_id = _text(record.get("component_id"))
        stage = _text(_first(record, "stage", "evidence_stage"))
        occurrence_ids = _string_list(
            _first(record, "occurrence_ids", "occurrence_id", "occurrence")
        )
        human_inputs = _normalize_images(record, "human", stage)
        ai_inputs = _normalize_images(record, "ai", stage)
        row = EvidenceQualityRowMetric(
            row_id=row_id,
            record_index=record_index,
            component_name=name,
            component_id=component_id,
            stage=stage,
            occurrence_ids=occurrence_ids,
            human_image_count=len(human_inputs),
            ai_image_count=len(ai_inputs),
        )
        rows.append(row)
        row_by_index[record_index] = row

        if policy.require_ai_image and not ai_inputs:
            add_issue(
                "AI_IMAGE_MISSING",
                policy.missing_ai_status,
                "该行缺少必需的 AI 截图证据。",
                affected_rows=[row],
                evidence={"record_index": record_index},
            )
        if policy.require_human_image and not human_inputs:
            add_issue(
                "HUMAN_IMAGE_MISSING",
                policy.missing_human_status,
                "该行缺少要求提供的人工参考截图。",
                affected_rows=[row],
                evidence={"record_index": record_index},
            )

        for image_index, image_input in enumerate(human_inputs + ai_inputs, start=1):
            resolved = _resolve_path(image_input.path, root)
            cache_key = str(resolved)
            facts = file_cache.get(cache_key)
            if facts is None:
                facts = _inspect_file(resolved, policy)
                file_cache[cache_key] = facts
            scale = _display_scale(
                image_input.display_width_px,
                image_input.display_height_px,
                facts.pixel_width,
                facts.pixel_height,
            )
            metric = EvidenceImageMetric(
                id=_stable_id(
                    "evidence-image-use",
                    {
                        "record_index": record_index,
                        "row_id": row_id,
                        "kind": image_input.kind,
                        "image_index": image_index,
                        "path": image_input.path,
                        "stage": image_input.stage,
                    },
                ),
                row_id=row_id,
                kind=image_input.kind,
                component_name=name,
                component_id=component_id,
                stage=image_input.stage,
                occurrence_ids=occurrence_ids,
                evidence_role=image_input.evidence_role,
                source_path=image_input.path,
                resolved_path=cache_key,
                exists=facts.exists,
                readable=facts.readable,
                sha256=facts.sha256,
                pixel_width=facts.pixel_width,
                pixel_height=facts.pixel_height,
                sampled_pixel_width=facts.sampled_pixel_width,
                sampled_pixel_height=facts.sampled_pixel_height,
                near_white_ratio=facts.near_white_ratio,
                display_width_px=image_input.display_width_px,
                display_height_px=image_input.display_height_px,
                display_scale=scale,
            )
            images.append(metric)
            image_by_id[metric.id] = metric
            row.image_metric_ids.append(metric.id)

            if not facts.exists:
                status = (
                    policy.missing_ai_status
                    if image_input.kind == "ai"
                    else policy.missing_human_status
                )
                add_issue(
                    f"{image_input.kind.upper()}_IMAGE_FILE_MISSING",
                    status,
                    "截图路径不存在或不是文件。",
                    affected_rows=[row],
                    affected_images=[metric],
                    evidence={"source_path": image_input.path},
                )
                continue
            if not facts.readable:
                status = (
                    policy.unreadable_ai_status
                    if image_input.kind == "ai"
                    else policy.unreadable_human_status
                )
                add_issue(
                    f"{image_input.kind.upper()}_IMAGE_UNREADABLE",
                    status,
                    "截图文件存在，但无法作为图像解码。",
                    affected_rows=[row],
                    affected_images=[metric],
                    evidence={"sha256": facts.sha256},
                )
                continue

            check_blank = image_input.kind == "ai" or policy.check_human_blank
            if (
                check_blank
                and facts.near_white_ratio is not None
                and facts.near_white_ratio >= policy.near_white_ratio_threshold
            ):
                add_issue(
                    f"{image_input.kind.upper()}_IMAGE_NEAR_WHITE",
                    policy.near_white_status,
                    "截图的近白像素占比过高，可能为空白或信息不足。",
                    affected_rows=[row],
                    affected_images=[metric],
                    evidence={
                        "near_white_ratio": facts.near_white_ratio,
                        "threshold": policy.near_white_ratio_threshold,
                    },
                )

            check_scale = image_input.kind == "ai" or policy.check_human_display_scale
            if check_scale and scale is not None and scale < policy.min_display_scale:
                add_issue(
                    f"{image_input.kind.upper()}_IMAGE_DISPLAY_SCALE_TOO_SMALL",
                    policy.small_display_status,
                    "截图嵌入显示尺寸相对原图过小，可能无法人工复核。",
                    affected_rows=[row],
                    affected_images=[metric],
                    evidence={
                        "display_scale": scale,
                        "threshold": policy.min_display_scale,
                        "natural_pixel_size": [facts.pixel_width, facts.pixel_height],
                        "display_pixel_size": [
                            image_input.display_width_px,
                            image_input.display_height_px,
                        ],
                    },
                )

    ai_by_identity: dict[str, list[EvidenceImageMetric]] = defaultdict(list)
    for metric in images:
        if metric.kind != "ai" or not metric.exists:
            continue
        identity = f"sha256:{metric.sha256}" if metric.sha256 else f"path:{metric.resolved_path}"
        ai_by_identity[identity].append(metric)

    reused_name_groups = 0
    reused_stage_groups = 0
    for identity, group in sorted(ai_by_identity.items()):
        names = {
            re.sub(r"\s+", " ", metric.component_name).strip().casefold()
            for metric in group
            if metric.component_name
        }
        display_names = sorted(
            {metric.component_name for metric in group if metric.component_name}
        )
        affected_rows = [row_by_index[index] for index in sorted({
            next(
                row.record_index
                for row in rows
                if metric.id in row.image_metric_ids
            )
            for metric in group
        })]
        if len(names) > 1:
            reused_name_groups += 1
            add_issue(
                "AI_IMAGE_REUSED_ACROSS_COMPONENT_NAMES",
                policy.reuse_name_status,
                "同一 AI 图片内容被不同构件名称复用，证据绑定不可信。",
                affected_rows=affected_rows,
                affected_images=group,
                evidence={"image_identity": identity, "component_names": display_names},
            )
        stages = {
            metric.stage.strip().casefold()
            for metric in group
            if metric.stage and metric.stage.strip().casefold() not in {"unknown", "missing"}
        }
        display_stages = sorted({metric.stage for metric in group if metric.stage})
        if len(stages) > 1:
            reused_stage_groups += 1
            add_issue(
                "AI_IMAGE_REUSED_ACROSS_EVIDENCE_STAGES",
                policy.reuse_stage_status,
                "同一 AI 图片内容被用于不同证据阶段，不能证明完整证据链。",
                affected_rows=affected_rows,
                affected_images=group,
                evidence={"image_identity": identity, "stages": display_stages},
            )

    report_status = ReviewStatus.PASS
    for row in rows:
        report_status = _max_status(report_status, row.status)
    missing_codes = {
        "AI_IMAGE_MISSING",
        "HUMAN_IMAGE_MISSING",
        "AI_IMAGE_FILE_MISSING",
        "HUMAN_IMAGE_FILE_MISSING",
    }
    summary = EvidenceQualitySummary(
        row_count=len(rows),
        image_use_count=len(images),
        unique_ai_image_count=len(ai_by_identity),
        missing_image_count=sum(issue.code in missing_codes for issue in issues),
        unreadable_image_count=sum(
            image.exists and not image.readable for image in images
        ),
        blank_image_count=sum(
            "AI_IMAGE_NEAR_WHITE" in image.issue_codes
            or "HUMAN_IMAGE_NEAR_WHITE" in image.issue_codes
            for image in images
        ),
        small_display_count=sum(
            "AI_IMAGE_DISPLAY_SCALE_TOO_SMALL" in image.issue_codes
            or "HUMAN_IMAGE_DISPLAY_SCALE_TOO_SMALL" in image.issue_codes
            for image in images
        ),
        reused_across_component_name_group_count=reused_name_groups,
        reused_across_stage_group_count=reused_stage_groups,
        issue_count=len(issues),
        pass_row_count=sum(row.status == ReviewStatus.PASS for row in rows),
        review_row_count=sum(row.status == ReviewStatus.REVIEW for row in rows),
        block_row_count=sum(row.status == ReviewStatus.BLOCK for row in rows),
    )
    return EvidenceQualityReport(
        status=report_status,
        thresholds=policy,
        summary=summary,
        rows=rows,
        images=images,
        issues=issues,
    )
