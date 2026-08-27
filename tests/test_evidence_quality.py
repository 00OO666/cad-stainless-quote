import json
from pathlib import Path

from cadquote.evidence_quality import (
    EvidenceQualityThresholds,
    audit_evidence_quality,
)
from cadquote.models import ReviewStatus
from PIL import Image, ImageDraw


def _informative_image(path: Path, size: tuple[int, int] = (200, 100)) -> None:
    image = Image.new("RGB", size, "white")
    draw = ImageDraw.Draw(image)
    draw.rectangle((10, 10, size[0] - 10, size[1] - 10), fill="black")
    image.save(path)


def test_clean_row_produces_auditable_pass_json(tmp_path: Path):
    ai = tmp_path / "ai.png"
    human = tmp_path / "human.png"
    _informative_image(ai)
    _informative_image(human)

    report = audit_evidence_quality(
        [
            {
                "row_id": "row-1",
                "name": "脚线",
                "component_id": "component:1",
                "stage": "elevation",
                "occurrence_id": "occurrence:1",
                "human_paths": [human],
                "ai_paths": [
                    {
                        "path": ai,
                        "display_width_px": 160,
                        "display_height_px": 80,
                    }
                ],
            }
        ],
        thresholds={"require_human_image": True},
    )

    assert report.status == ReviewStatus.PASS
    assert report.summary.pass_row_count == 1
    assert report.summary.issue_count == 0
    assert report.images[1].display_scale == 0.8
    payload = report.model_dump(mode="json")
    json.dumps(payload, ensure_ascii=False)
    output = tmp_path / "quality.json"
    report.write_json(output)
    assert json.loads(output.read_text(encoding="utf-8"))["status"] == "PASS"


def test_missing_required_images_never_guess_correctness(tmp_path: Path):
    report = audit_evidence_quality(
        [{"row_id": "row-1", "name": "门套", "stage": "plan"}],
        thresholds={"require_human_image": True},
        base_dir=tmp_path,
    )

    assert report.status == ReviewStatus.BLOCK
    assert {issue.code for issue in report.issues} == {
        "AI_IMAGE_MISSING",
        "HUMAN_IMAGE_MISSING",
    }
    assert report.rows[0].status == ReviewStatus.BLOCK
    assert report.summary.missing_image_count == 2
    assert report.summary.pass_row_count == 0


def test_missing_and_unreadable_paths_are_separate_auditable_failures(tmp_path: Path):
    unreadable = tmp_path / "not-an-image.png"
    unreadable.write_text("synthetic non-image", encoding="utf-8")
    report = audit_evidence_quality(
        [
            {
                "row_id": "row-1",
                "name": "壁龛",
                "ai_paths": ["missing.png", unreadable],
            }
        ],
        base_dir=tmp_path,
    )

    assert report.status == ReviewStatus.BLOCK
    assert {issue.code for issue in report.issues} == {
        "AI_IMAGE_FILE_MISSING",
        "AI_IMAGE_UNREADABLE",
    }
    assert report.summary.missing_image_count == 1
    assert report.summary.unreadable_image_count == 1


def test_same_ai_content_reused_across_names_and_stages_blocks_both_rows(tmp_path: Path):
    shared = tmp_path / "shared.png"
    copied = tmp_path / "same-content-new-path.png"
    _informative_image(shared)
    copied.write_bytes(shared.read_bytes())

    report = audit_evidence_quality(
        [
            {
                "row_id": "row-1",
                "name": "脚线",
                "stage": "plan",
                "occurrence": "occurrence:1",
                "ai_path": shared,
            },
            {
                "row_id": "row-2",
                "name": "门套",
                "stage": "detail",
                "occurrence": "occurrence:2",
                "ai_path": copied,
            },
        ]
    )

    codes = {issue.code for issue in report.issues}
    assert "AI_IMAGE_REUSED_ACROSS_COMPONENT_NAMES" in codes
    assert "AI_IMAGE_REUSED_ACROSS_EVIDENCE_STAGES" in codes
    assert report.summary.unique_ai_image_count == 1
    assert report.summary.reused_across_component_name_group_count == 1
    assert report.summary.reused_across_stage_group_count == 1
    assert all(row.status == ReviewStatus.BLOCK for row in report.rows)


def test_near_white_and_small_embedding_are_review_with_configurable_thresholds(
    tmp_path: Path,
):
    image_path = tmp_path / "mostly-white.png"
    image = Image.new("RGB", (200, 100), "white")
    ImageDraw.Draw(image).rectangle((0, 0, 9, 99), fill="black")
    image.save(image_path)

    report = audit_evidence_quality(
        [
            {
                "row_id": "row-1",
                "name": "装饰条",
                "stage": "elevation",
                "ai_images": [
                    {
                        "path": image_path,
                        "display_width_px": 20,
                        "display_height_px": 10,
                    }
                ],
            }
        ],
        thresholds=EvidenceQualityThresholds(
            near_white_ratio_threshold=0.90,
            min_display_scale=0.25,
        ),
    )

    assert report.status == ReviewStatus.REVIEW
    assert {issue.code for issue in report.issues} == {
        "AI_IMAGE_NEAR_WHITE",
        "AI_IMAGE_DISPLAY_SCALE_TOO_SMALL",
    }
    assert report.images[0].near_white_ratio == 0.95
    assert report.images[0].display_scale == 0.1
    assert report.summary.blank_image_count == 1
    assert report.summary.small_display_count == 1


def test_threshold_failure_severities_cannot_be_pass():
    try:
        EvidenceQualityThresholds(near_white_status=ReviewStatus.PASS)
    except ValueError as exc:
        assert "must be REVIEW or BLOCK" in str(exc)
    else:
        raise AssertionError("PASS must not be accepted as a failure status")
