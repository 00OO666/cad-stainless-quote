"""Shared data contracts for every pipeline stage.

Defaults are deliberately conservative: uncertain records start as REVIEW, never PASS.
"""

from __future__ import annotations

from enum import StrEnum
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ReviewStatus(StrEnum):
    PASS = "PASS"
    REVIEW = "REVIEW"
    BLOCK = "BLOCK"


class Severity(StrEnum):
    INFO = "INFO"
    WARNING = "WARNING"
    ERROR = "ERROR"
    BLOCK = "BLOCK"


class TextNormalizationMode(StrEnum):
    """Supported deterministic normalization modes for evaluation text fields."""

    STRICT = "strict"
    CANONICAL = "canonical"


class NumericZeroHandling(StrEnum):
    """How a relative-tolerance rule behaves when the gold value is zero."""

    EXACT = "exact"
    ABSOLUTE = "absolute"
    UNRESOLVED = "unresolved"


class UnfoldedSpecComparisonMode(StrEnum):
    """Ways to compare a folded/unfolded-width expression."""

    STRICT = "strict"
    EVALUATED_TOTAL = "evaluated-total"


class EvaluationTextFieldPolicy(StrictModel):
    enabled: bool = True
    required_in_gold: bool = True
    normalization: TextNormalizationMode = TextNormalizationMode.CANONICAL


class EvaluationNumericFieldPolicy(StrictModel):
    enabled: bool = True
    required_in_gold: bool = True
    # ``None`` deliberately means that the business tolerance is still pending.
    relative_tolerance: float | None = Field(default=None, ge=0)
    zero_handling: NumericZeroHandling = NumericZeroHandling.EXACT
    zero_absolute_tolerance: float | None = Field(default=None, ge=0)


class EvaluationUnfoldedSpecPolicy(StrictModel):
    enabled: bool = True
    required_in_gold: bool = True
    mode: UnfoldedSpecComparisonMode = UnfoldedSpecComparisonMode.STRICT
    normalization: TextNormalizationMode = TextNormalizationMode.CANONICAL


class EvaluationAmountPolicy(StrictModel):
    # Amount is outside the current quantity-takeoff acceptance scope unless a
    # reviewer explicitly enables the exact (0%) comparison.
    enabled: bool = False
    required_in_gold: bool = False
    exact: bool = True


class EvaluationPolicy(StrictModel):
    """Versioned business policy for project-level 95% replication testing.

    The default is intentionally non-final: the engineering-quantity tolerance is
    known to be 5%, while length and physical-count tolerances still require an
    explicit business decision.  A report produced with this default can diagnose
    rows but can never claim that the 95% gate passed.
    """

    schema_version: Literal["1.0"] = "1.0"
    policy_version: str = "draft-length-quantity-pending"
    target_accuracy: float = Field(default=0.95, gt=0, le=1)
    mt_code: EvaluationTextFieldPolicy = Field(default_factory=EvaluationTextFieldPolicy)
    name: EvaluationTextFieldPolicy = Field(default_factory=EvaluationTextFieldPolicy)
    material: EvaluationTextFieldPolicy = Field(
        default_factory=lambda: EvaluationTextFieldPolicy(
            enabled=False,
            required_in_gold=False,
        )
    )
    plan_location: EvaluationTextFieldPolicy = Field(default_factory=EvaluationTextFieldPolicy)
    elevation: EvaluationTextFieldPolicy = Field(default_factory=EvaluationTextFieldPolicy)
    detail: EvaluationTextFieldPolicy = Field(default_factory=EvaluationTextFieldPolicy)
    unfolded_spec: EvaluationUnfoldedSpecPolicy = Field(
        default_factory=EvaluationUnfoldedSpecPolicy
    )
    width_mm: EvaluationNumericFieldPolicy = Field(
        default_factory=lambda: EvaluationNumericFieldPolicy(
            enabled=False,
            required_in_gold=False,
        )
    )
    length_mm: EvaluationNumericFieldPolicy = Field(default_factory=EvaluationNumericFieldPolicy)
    quantity: EvaluationNumericFieldPolicy = Field(default_factory=EvaluationNumericFieldPolicy)
    engineering_quantity: EvaluationNumericFieldPolicy = Field(
        default_factory=lambda: EvaluationNumericFieldPolicy(relative_tolerance=0.05)
    )
    amount: EvaluationAmountPolicy = Field(default_factory=EvaluationAmountPolicy)


class SourceFile(StrictModel):
    id: str
    relative_path: str
    absolute_path: str
    sha256: str
    bytes: int = Field(ge=0)
    suffix: str
    media_type: str | None = None
    archive_member: str | None = None
    status: ReviewStatus = ReviewStatus.REVIEW
    converted_paths: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)


class Sheet(StrictModel):
    id: str
    source_file_id: str
    drawing_number: str | None = None
    title: str | None = None
    kind: Literal[
        "cover",
        "catalog",
        "material",
        "plan",
        "elevation_index",
        "elevation",
        "detail",
        "door",
        "ceiling",
        "floor",
        "other",
        "unknown",
    ] = "unknown"
    layout: str | None = None
    viewport_handle: str | None = None
    bbox: tuple[float, float, float, float] | None = None
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    evidence: list[str] = Field(default_factory=list)


class CadEntity(StrictModel):
    id: str
    source_file_id: str
    sheet_id: str | None = None
    handle: str | None = None
    entity_type: str
    layer: str | None = None
    space: str
    text: str | None = None
    value: float | None = None
    text_override: str | None = None
    insert: tuple[float, float] | None = None
    bbox: tuple[float, float, float, float] | None = None
    geometry: dict[str, Any] = Field(default_factory=dict)


class MaterialSpec(StrictModel):
    id: str
    mt_code: str
    raw_material_code: str | None = None
    material_code_family: str | None = None
    name: str | None = None
    grade: str | None = None
    thickness_mm: float | None = Field(default=None, gt=0)
    finish: str | None = None
    process: str | None = None
    brand: str | None = None
    model: str | None = None
    source_file_id: str | None = None
    source_type: str | None = None
    source_sha256: str | None = None
    source_location: str | None = None
    source_evidence: list[str] = Field(default_factory=list)
    status: ReviewStatus = ReviewStatus.REVIEW
    conflicts: list[str] = Field(default_factory=list)


class MtOccurrence(StrictModel):
    id: str
    mt_code: str
    raw_material_code: str | None = None
    material_code_family: str | None = None
    source_file_id: str
    sheet_id: str | None = None
    entity_ids: list[str] = Field(default_factory=list)
    anchor: tuple[float, float] | None = None
    leader_entity_id: str | None = None
    leader_target: tuple[float, float] | None = None
    room: str | None = None
    component_hint: str | None = None
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    status: ReviewStatus = ReviewStatus.REVIEW


class MaterialMention(StrictModel):
    """Unnumbered stainless/metal component text retained for review, never as a fake MT."""

    id: str
    raw_text: str
    source_file_id: str
    sheet_id: str | None = None
    entity_ids: list[str] = Field(default_factory=list)
    anchor: tuple[float, float] | None = None
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    status: ReviewStatus = ReviewStatus.REVIEW
    reason: str = "stainless material description has no recognized material code"


class ComponentInstance(StrictModel):
    id: str
    mt_code: str
    raw_material_code: str | None = None
    material_code_family: str | None = None
    name: str | None = None
    room: str | None = None
    plan_occurrence_ids: list[str] = Field(default_factory=list)
    elevation_occurrence_ids: list[str] = Field(default_factory=list)
    detail_sheet_ids: list[str] = Field(default_factory=list)
    status: ReviewStatus = ReviewStatus.REVIEW


class EvidenceEdge(StrictModel):
    id: str
    relation: Literal[
        "plan_to_elevation",
        "elevation_to_detail",
        "material_mention_to_material",
        "occurrence_to_component",
        "component_to_dimension",
        "component_to_material",
        "component_to_engineering_quantity_evidence",
        "component_to_price",
    ]
    source_id: str
    target_id: str
    basis: list[str] = Field(default_factory=list)
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    status: ReviewStatus = ReviewStatus.REVIEW


class MeasurementCandidate(StrictModel):
    id: str
    component_id: str
    role: Literal["length", "height", "width", "unfolded_spec", "quantity"]
    raw_value: str
    numeric_value: float | None = None
    unit: str | None = None
    source_file_id: str
    sheet_id: str | None = None
    source_sheet_ids: list[str] = Field(default_factory=list)
    entity_ids: list[str] = Field(default_factory=list)
    source_candidate_ids: list[str] = Field(default_factory=list)
    derived_expression: str | None = None
    value_expression: str | None = None
    distance: float | None = Field(default=None, ge=0)
    basis: list[str] = Field(default_factory=list)
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    status: ReviewStatus = ReviewStatus.REVIEW


class ComponentMaterialRef(StrictModel):
    """An included material that belongs to the same physical billable assembly."""

    role: Literal["glass_infill", "included_other"]
    material_spec_id: str
    material_code: str
    material_name: str
    evidence_ids: list[str] = Field(default_factory=list)
    status: ReviewStatus = ReviewStatus.REVIEW


class CompositeAssembly(StrictModel):
    """A multi-material physical assembly emitted as exactly one quotation row."""

    kind: Literal["single_line_composite"] = "single_line_composite"
    assembly_type: Literal["screen_with_glass"]
    billing_basis: Literal["whole_elevation_projection"]
    required_material_roles: list[Literal["glass_infill", "included_other"]]
    included_materials: list[ComponentMaterialRef] = Field(default_factory=list)
    basis: str
    evidence_ids: list[str] = Field(default_factory=list)
    projection_width_candidate_id: str | None = None
    projection_length_candidate_id: str | None = None
    projection_component_entity_id: str | None = None
    projection_axis_basis: str | None = None
    projection_axis_evidence_ids: list[str] = Field(default_factory=list)


class PriceEntry(StrictModel):
    id: str
    version: str
    approved: bool = False
    mt_code: str
    material: str | None = None
    grade: str | None = None
    thickness_mm: float | None = Field(default=None, gt=0)
    finish: str | None = None
    process: str | None = None
    pricing_method: str
    unit: Literal["m", "㎡", "件", "套"]
    unit_price: float = Field(ge=0)
    currency: str = "CNY"
    tax_included: bool | None = None
    valid_from: str | None = None
    valid_to: str | None = None
    source: str
    note: str | None = None
    included_material_codes: list[str] = Field(default_factory=list)
    composite_billing_basis: Literal["whole_elevation_projection"] | None = None


class PriceBook(StrictModel):
    version: str
    approved: bool = False
    source: str
    entries: list[PriceEntry] = Field(default_factory=list)


class TakeoffItem(StrictModel):
    sequence: int = Field(ge=1)
    name: str
    mt_code: str
    material: str | None = None
    plan_location: str | None = None
    elevation: str | None = None
    detail: str | None = None
    unfolded_spec: str | None = None
    width_mm: float | None = Field(default=None, ge=0)
    length_mm: float | None = Field(default=None, ge=0)
    quantity: float | None = Field(default=None, ge=0)
    engineering_quantity: float | None = Field(default=None, ge=0)
    engineering_quantity_expression: str | None = None
    engineering_quantity_basis: str | None = None
    engineering_quantity_evidence_ids: list[str] = Field(default_factory=list)
    unit: Literal["m", "㎡", "件", "套"] | None = None
    pricing_method: str | None = None
    unit_price: float | None = Field(default=None, ge=0)
    price_entry_id: str | None = None
    amount: float | None = Field(default=None, ge=0)
    note: str | None = None
    composite_assembly: CompositeAssembly | None = None
    component_id: str | None = None
    evidence_ids: list[str] = Field(default_factory=list)
    status: ReviewStatus = ReviewStatus.REVIEW
    block_reason: str | None = None


class RunIssue(StrictModel):
    stage: str
    severity: Severity
    code: str
    message: str
    source_id: str | None = None
    evidence: list[str] = Field(default_factory=list)
    suggested_action: str | None = None


class ProjectManifest(StrictModel):
    schema_version: str = "1.0"
    project_id: str
    project_name: str
    input_path: str
    run_dir: str
    created_at: str
    files: list[SourceFile] = Field(default_factory=list)
    sheets: list[Sheet] = Field(default_factory=list)
    issues: list[RunIssue] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)

    @property
    def run_path(self) -> Path:
        return Path(self.run_dir)
