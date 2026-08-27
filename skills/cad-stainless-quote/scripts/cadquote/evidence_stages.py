"""Canonical drawing-kind to evidence-stage mapping.

Sheet classification and the three-stage quotation evidence model are related
but not identical.  This module is the single conversion boundary so unknown
or administrative sheet kinds cannot silently become elevation evidence.
"""

from __future__ import annotations

from typing import Literal

EvidenceStage = Literal["plan", "elevation", "detail"]

_KIND_TO_STAGE: dict[str, EvidenceStage] = {
    "plan": "plan",
    "elevation_index": "plan",
    "elevation": "elevation",
    "detail": "detail",
    "door": "detail",
    "ceiling": "detail",
    "floor": "detail",
}


def canonical_stage_for_kind(kind: object) -> EvidenceStage | None:
    """Return the quotation evidence stage for a classified sheet kind.

    ``None`` is intentional for unknown, other, cover, catalog, material, and
    any future unrecognised kind.  Callers must keep those sheets outside the
    plan/elevation/detail chain until classification is explicitly resolved.
    """

    normalized = str(kind or "").strip().casefold().replace("-", "_").replace(" ", "_")
    return _KIND_TO_STAGE.get(normalized)


__all__ = ["EvidenceStage", "canonical_stage_for_kind"]
