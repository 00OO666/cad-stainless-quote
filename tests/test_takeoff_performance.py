from __future__ import annotations

import time
from collections.abc import Iterator, Sequence

from cadquote.models import CadEntity, EvidenceEdge, MtOccurrence, Sheet
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
