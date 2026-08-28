from __future__ import annotations

import time
from collections.abc import Iterator, Sequence

import cadquote.takeoff as takeoff_module
from cadquote.models import CadEntity, EvidenceEdge, MaterialSpec, MtOccurrence, Sheet
from cadquote.takeoff import build_takeoff


class CountingEntities(Sequence[CadEntity]):
    def __init__(self, values: list[CadEntity]) -> None:
        self._values = values
        self.iterations = 0

    def __getitem__(self, index: int) -> CadEntity:
        return self._values[index]

    def __len__(self) -> int:
        return len(self._values)

    def __iter__(self) -> Iterator[CadEntity]:
        self.iterations += 1
        return iter(self._values)


def test_build_takeoff_indexes_large_entity_table_once():
    component_count = 200
    sheets: list[Sheet] = []
    occurrences: list[MtOccurrence] = []
    edges: list[EvidenceEdge] = []
    entities: list[CadEntity] = []

    for index in range(component_count):
        plan_id = f"plan-{index}"
        elevation_id = f"elevation-{index}"
        sheets.extend(
            [
                Sheet(
                    id=plan_id,
                    source_file_id="source",
                    kind="plan",
                    bbox=(0, 0, 1_000, 1_000),
                ),
                Sheet(
                    id=elevation_id,
                    source_file_id="source",
                    kind="elevation",
                    bbox=(0, 0, 1_000, 1_000),
                ),
            ]
        )
        occurrences.extend(
            [
                MtOccurrence(
                    id=f"plan-mt-{index}",
                    mt_code="MT-01",
                    source_file_id="source",
                    sheet_id=plan_id,
                    anchor=(100.0, 100.0),
                    room=f"room-{index}",
                ),
                MtOccurrence(
                    id=f"elevation-mt-{index}",
                    mt_code="MT-01",
                    source_file_id="source",
                    sheet_id=elevation_id,
                    anchor=(100.0, 100.0),
                    room=f"room-{index}",
                ),
            ]
        )
        edges.append(
            EvidenceEdge(
                id=f"edge-{index}",
                relation="plan_to_elevation",
                source_id=plan_id,
                target_id=elevation_id,
            )
        )
        entities.append(
            CadEntity(
                id=f"dimension-{index}",
                source_file_id="source",
                sheet_id=elevation_id,
                entity_type="DIMENSION",
                space="model",
                value=1_000 + index,
                insert=(100.0, 100.0),
                geometry={"units": "millimeters"},
            )
        )

    sheets.append(
        Sheet(
            id="irrelevant",
            source_file_id="source",
            kind="other",
            bbox=(0, 0, 10_000, 10_000),
        )
    )
    entities.extend(
        CadEntity(
            id=f"irrelevant-{index}",
            source_file_id="source",
            sheet_id="irrelevant",
            entity_type="INSERT",
            space="model",
            insert=(float(index), 0.0),
        )
        for index in range(20_000)
    )
    counted_entities = CountingEntities(entities)

    started = time.perf_counter()
    result = build_takeoff(sheets, counted_entities, occurrences, edges)
    elapsed = time.perf_counter() - started

    assert len(result.components) == component_count
    assert counted_entities.iterations == 1
    assert elapsed < 5.0


def test_composite_material_owner_is_resolved_once_per_evidence(monkeypatch):
    """Dense screen schedules must not rescan all components inside every row."""

    component_count = 120
    sheets = [
        Sheet(
            id=f"plan-{index}",
            source_file_id="source",
            kind="plan",
            bbox=(0, 0, 100, 100),
        )
        for index in range(component_count)
    ]
    occurrences = [
        MtOccurrence(
            id=f"mt-{index}",
            mt_code="MT-01",
            source_file_id="source",
            sheet_id=f"plan-{index}",
            anchor=(50, 50),
            room=f"room-{index}",
            component_hint="屏风",
        )
        for index in range(component_count)
    ]
    entities = [
        CadEntity(
            id=f"glass-{index}",
            source_file_id="source",
            sheet_id=f"plan-{index}",
            entity_type="TEXT",
            space="model",
            text="GC-GL-01 艺术玻璃",
            insert=(50, 50),
        )
        for index in range(component_count)
    ]
    materials = [
        MaterialSpec(
            id="glass-material",
            mt_code="GC-GL-01",
            material_code_family="GC-GL",
            name="艺术玻璃",
        )
    ]

    original = takeoff_module._MaterialEvidenceOwnerIndex.owner
    calls = 0

    def counted_owner(self, *args, **kwargs):
        nonlocal calls
        calls += 1
        return original(self, *args, **kwargs)

    monkeypatch.setattr(
        takeoff_module._MaterialEvidenceOwnerIndex,
        "owner",
        counted_owner,
    )
    result = build_takeoff(
        sheets,
        entities,
        occurrences,
        [],
        materials=materials,
    )

    assert len(result.components) == component_count
    assert calls == component_count


def test_dense_same_sheet_glass_ownership_uses_spatial_index(monkeypatch):
    component_count = 200
    sheet = Sheet(
        id="plan",
        source_file_id="source",
        kind="plan",
        bbox=(0, 0, component_count * 1000, 1000),
    )
    occurrences = [
        MtOccurrence(
            id=f"mt-{index}",
            mt_code="MT-01",
            source_file_id="source",
            sheet_id=sheet.id,
            anchor=(index * 1000 + 100, 500),
            room=f"room-{index}",
            component_hint="屏风",
        )
        for index in range(component_count)
    ]
    entities = [
        CadEntity(
            id=f"glass-{index}",
            source_file_id="source",
            sheet_id=sheet.id,
            entity_type="TEXT",
            space="model",
            text="GC-GL-01 艺术玻璃",
            insert=(index * 1000 + 100, 500),
        )
        for index in range(component_count)
    ]
    material = MaterialSpec(
        id="glass-material",
        mt_code="GC-GL-01",
        material_code_family="GC-GL",
        name="艺术玻璃",
    )
    score_calls = 0
    original_score = takeoff_module._component_material_evidence_score

    def counted_score(*args, **kwargs):
        nonlocal score_calls
        score_calls += 1
        return original_score(*args, **kwargs)

    monkeypatch.setattr(
        takeoff_module,
        "_component_material_evidence_score",
        counted_score,
    )
    started = time.perf_counter()
    result = build_takeoff(
        [sheet],
        entities,
        occurrences,
        [],
        materials=[material],
    )
    elapsed = time.perf_counter() - started

    assert len(result.components) == component_count
    assert all(item.composite_assembly is not None for item in result.items)
    # One resolved local-material verification per row is expected; the owner
    # lookup itself must not add component_count scans per evidence entity.
    assert score_calls == component_count
    assert elapsed < 3.0


def test_dense_measurement_facts_are_truncated_before_model_construction(monkeypatch):
    component_count = 100
    sheet = Sheet(
        id="elevation",
        source_file_id="source",
        kind="elevation",
        drawing_number="E-01",
        bbox=(0, 0, (component_count + 1) * 100, 2000),
    )
    occurrences: list[MtOccurrence] = []
    entities: list[CadEntity] = []
    for index in range(component_count):
        x = 50 + 100 * index
        frame_id = f"frame-{index}"
        occurrences.append(
            MtOccurrence(
                id=f"mt-{index}",
                mt_code="MT-01",
                source_file_id="source",
                sheet_id=sheet.id,
                anchor=(x, 500),
                leader_target=(x, 500),
                component_hint=f"屏风{index}",
                entity_ids=[frame_id],
            )
        )
        entities.extend(
            [
                CadEntity(
                    id=frame_id,
                    source_file_id="source",
                    sheet_id=sheet.id,
                    entity_type="INSERT",
                    space="model",
                    insert=(x, 500),
                    bbox=(x - 40, 0, x + 40, 1000),
                ),
                CadEntity(
                    id=f"glass-{index}",
                    source_file_id="source",
                    sheet_id=sheet.id,
                    entity_type="TEXT",
                    space="model",
                    text="GC-GL-01 艺术玻璃",
                    insert=(x + 1, 500),
                ),
                CadEntity(
                    id=f"width-{index}",
                    source_file_id="source",
                    sheet_id=sheet.id,
                    entity_type="DIMENSION",
                    space="model",
                    value=80,
                    insert=(x, 500),
                    geometry={
                        "units": "millimeters",
                        "defpoint2": [x - 40, 500, 0],
                        "defpoint3": [x + 40, 500, 0],
                    },
                ),
                CadEntity(
                    id=f"length-{index}",
                    source_file_id="source",
                    sheet_id=sheet.id,
                    entity_type="DIMENSION",
                    space="model",
                    value=1000,
                    insert=(x, 500),
                    geometry={
                        "units": "millimeters",
                        "defpoint2": [x, 0, 0],
                        "defpoint3": [x, 1000, 0],
                    },
                ),
                CadEntity(
                    id=f"quantity-{index}",
                    source_file_id="source",
                    sheet_id=sheet.id,
                    entity_type="TEXT",
                    space="model",
                    text="×1",
                    insert=(x, 500),
                ),
            ]
        )
    material = MaterialSpec(
        id="glass-material",
        mt_code="GC-GL-01",
        material_code_family="GC-GL",
        name="艺术玻璃",
    )
    original_candidate = takeoff_module.MeasurementCandidate
    constructor_calls = 0

    def counted_candidate(*args, **kwargs):
        nonlocal constructor_calls
        constructor_calls += 1
        return original_candidate(*args, **kwargs)

    monkeypatch.setattr(takeoff_module, "MeasurementCandidate", counted_candidate)
    result = build_takeoff(
        [sheet],
        entities,
        occurrences,
        [],
        materials=[material],
    )

    assert len(result.components) == component_count
    assert len(result.measurements) > component_count
    assert constructor_calls == len(result.measurements)
