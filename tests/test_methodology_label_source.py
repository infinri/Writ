"""Program item 1e: one source for the methodology label lists.

The ranked candidate pool (pipeline._load_candidates) loads five methodology labels and the
Channel 2 trigger index (MethodologyTriggerIndex.build_from_db) loads four. The difference
is DELIBERATE: every ForbiddenResponse is injected by /always-on each turn and ranked in
Channel 1 (the 1.7 cutover note in writ/server/routes/query.py), so the trigger index
leaving it out is what stops a second delivery through the methodology companion. These
tests pin both lists to writ/graph/schema.py and the difference to exactly that one label.
"""
from __future__ import annotations

import asyncio
import re
from pathlib import Path

from writ.graph import schema


class _EmptyResult:
    def __aiter__(self):
        return self

    async def __anext__(self):
        raise StopAsyncIteration


class _RecordingSession:
    def __init__(self, seen: list[str]) -> None:
        self._seen = seen

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc) -> bool:
        return False

    async def run(self, query: str, **params):
        self._seen.append(query)
        return _EmptyResult()


class _RecordingDriver:
    def __init__(self) -> None:
        self.seen: list[str] = []

    def session(self, database=None):
        return _RecordingSession(self.seen)


class _RecordingDB:
    def __init__(self) -> None:
        self._driver = _RecordingDriver()
        self._database = "neo4j"


def _labels_queried(queries: list[str]) -> list[str]:
    return re.findall(r"MATCH \(n:(\w+)\)", "\n".join(queries))


class TestSchemaOwnsBothLists:
    def test_ranked_labels_pinned_in_build_order(self) -> None:
        assert schema.RANKED_METHODOLOGY_LABELS == (
            "Skill", "Playbook", "Technique", "AntiPattern", "ForbiddenResponse",
        )

    def test_trigger_index_labels_pinned(self) -> None:
        assert schema.TRIGGER_INDEX_METHODOLOGY_LABELS == (
            "Skill", "Playbook", "Technique", "AntiPattern",
        )

    def test_the_only_intended_difference_is_forbidden_response(self) -> None:
        assert schema.CHANNEL1_ONLY_METHODOLOGY_LABELS == frozenset({"ForbiddenResponse"})
        difference = set(schema.RANKED_METHODOLOGY_LABELS) - set(schema.TRIGGER_INDEX_METHODOLOGY_LABELS)
        assert difference == set(schema.CHANNEL1_ONLY_METHODOLOGY_LABELS)

    def test_every_label_has_an_id_field(self) -> None:
        for label in schema.RANKED_METHODOLOGY_LABELS:
            assert label in schema.NODE_ID_FIELDS


class TestConsumersReadTheSchema:
    def test_trigger_index_constant_is_the_schema_tuple(self) -> None:
        from writ.retrieval import trigger_index

        assert trigger_index.RETRIEVABLE_METHODOLOGY_LABELS is schema.TRIGGER_INDEX_METHODOLOGY_LABELS

    def test_pipeline_loads_exactly_the_ranked_labels_in_order(self) -> None:
        from writ.retrieval.pipeline import _load_candidates

        db = _RecordingDB()
        asyncio.run(_load_candidates(db))
        assert _labels_queried(db._driver.seen) == list(schema.RANKED_METHODOLOGY_LABELS)

    def test_trigger_index_loads_exactly_the_trigger_labels_in_order(self) -> None:
        from writ.retrieval.trigger_index import MethodologyTriggerIndex

        db = _RecordingDB()
        asyncio.run(MethodologyTriggerIndex.build_from_db(db))
        assert _labels_queried(db._driver.seen) == list(schema.TRIGGER_INDEX_METHODOLOGY_LABELS)

    def test_integrity_floor_labels_are_rendered_from_the_schema(self) -> None:
        from writ.graph.integrity import _common

        assert _common._FLOOR_NODE_LABELS == "['Skill','Playbook','Technique','AntiPattern']"
        assert "TRIGGER_INDEX_METHODOLOGY_LABELS" in Path(_common.__file__).read_text()
