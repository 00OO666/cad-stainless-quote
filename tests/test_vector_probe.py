from __future__ import annotations

from pathlib import Path

import ezdxf
from cadquote.models import MtOccurrence, Sheet
from cadquote.vector_probe import probe_repeated_vectors


def _drawing(path: Path, *, ambiguous: bool = False) -> None:
    document = ezdxf.new("R2013")
    modelspace = document.modelspace()
    modelspace.add_lwpolyline([(0, 0), (2400, 0)], dxfattribs={"layer": "METAL"})
    modelspace.add_lwpolyline([(0, -300), (2400, -300)], dxfattribs={"layer": "METAL"})
    if ambiguous:
        modelspace.add_lwpolyline([(1000, -100), (1000, 100)], dxfattribs={"layer": "TRIM"})
        modelspace.add_lwpolyline([(1200, -100), (1200, 100)], dxfattribs={"layer": "TRIM"})
    document.saveas(path)


def _sheet() -> Sheet:
    return Sheet(
        id="sheet:elevation",
        source_file_id="file:synthetic",
        drawing_number="E-01",
        title="Synthetic elevation",
        kind="elevation",
        layout="Model",
        bbox=(-500, -500, 3_000, 500),
        confidence=1.0,
    )


def _occurrence(target: tuple[float, float] | None = (1000, 0)) -> MtOccurrence:
    return MtOccurrence(
        id="mt:synthetic",
        mt_code="MT-01",
        source_file_id="file:synthetic",
        sheet_id="sheet:elevation",
        leader_target=target,
        confidence=0.8,
    )


def test_repeated_polyline_group_is_review_only_quantity_candidate(tmp_path: Path) -> None:
    drawing = tmp_path / "repeated.dxf"
    _drawing(drawing)

    result = probe_repeated_vectors(
        {"file:synthetic": drawing},
        [_sheet()],
        [_occurrence()],
        radius=500,
    )

    assert result["summary"]["review_candidate_count"] == 1
    probe = result["probes"][0]
    assert probe["recommended_quantity"] == 2
    assert probe["status"] == "REVIEW"
    assert probe["confidence"] < 0.5
    assert probe["groups"][0]["instance_count"] == 2
    assert len(probe["groups"][0]["handles"]) == 2
    assert "review_only_not_billable_quantity" in probe["basis"]


def test_multiple_anchored_repeated_shapes_are_ambiguous(tmp_path: Path) -> None:
    drawing = tmp_path / "ambiguous.dxf"
    _drawing(drawing, ambiguous=True)

    result = probe_repeated_vectors(
        {"file:synthetic": drawing},
        [_sheet()],
        [_occurrence()],
        radius=500,
    )

    probe = result["probes"][0]
    assert probe["recommended_quantity"] is None
    assert probe["status"] == "BLOCK"
    assert probe["repeated_group_count"] == 2
    assert "multiple_anchored_repeated_groups_ambiguous" in probe["basis"]


def test_truncated_vector_scan_never_recommends_quantity(tmp_path: Path) -> None:
    drawing = tmp_path / "truncated.dxf"
    _drawing(drawing)

    result = probe_repeated_vectors(
        {"file:synthetic": drawing},
        [_sheet()],
        [_occurrence()],
        radius=500,
        max_primitives=1,
    )

    probe = result["probes"][0]
    assert result["scans"][0]["truncated"] is True
    assert probe["recommended_quantity"] is None


def test_occurrence_without_leader_target_is_not_probed(tmp_path: Path) -> None:
    drawing = tmp_path / "unused.dxf"
    _drawing(drawing)

    result = probe_repeated_vectors(
        {"file:synthetic": drawing},
        [_sheet()],
        [_occurrence(None)],
    )

    assert result["probes"] == []
    assert result["summary"]["skipped_without_leader_target_count"] == 1
    assert result["skipped_without_leader_target_ids"] == ["mt:synthetic"]
