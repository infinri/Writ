"""Guard for the Wave 3 integrity _run() Cypher-helper dedup.

The check mixins repeated `async with self._driver.session(...) as session: result =
await session.run(Q, **p); [... async for record in result]`. The dedup extracts the
single-query-collect boilerplate into `_QueryMixin._run(self, query, **params)` (new
writ/graph/integrity/_query.py, composed into IntegrityChecker) and migrates the 15
sole-run comprehension sites to `self._run(...)`.

RED today: writ.graph.integrity._query does not exist (behavior/MRO tests fail on import)
and the mixins still contain the 31 inline `async with self._driver.session` blocks
(structural test fails on its counts).
"""
from __future__ import annotations

import asyncio
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
INTEG = REPO / "writ" / "graph" / "integrity"
MIXIN_FILES = [
    "structural_checks.py", "frequency_checks.py", "parity_checks.py", "routing_checks.py",
    "content_checks.py", "edge_contract_checks.py", "artifact_checks.py",
]


class _FakeRecord(dict):
    def data(self):
        return dict(self)


class _FakeResult:
    def __init__(self, records):
        self._records = records

    async def __aiter__(self):  # pragma: no cover - exercised via async-for
        for r in self._records:
            yield r


class _FakeSession:
    def __init__(self, records, calls):
        self._records = records
        self._calls = calls
        self.closed = False

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        self.closed = True
        return False

    async def run(self, query, **params):
        self._calls.append((query, params))
        return _FakeResult(self._records)


class _FakeDriver:
    def __init__(self, records):
        self._records = records
        self.calls = []
        self.last_session = None

    def session(self, database=None):
        self.last_session = _FakeSession(self._records, self.calls)
        return self.last_session


def _checker(records):
    from writ.graph.integrity import IntegrityChecker  # RED today via _query import chain

    return IntegrityChecker(_FakeDriver(records))


class TestRunHelper:
    def test_run_returns_materialized_records_usable_after_close(self) -> None:
        recs_in = [_FakeRecord({"rule_id": "A"}), _FakeRecord({"rule_id": "B"})]
        checker = _checker(recs_in)
        out = asyncio.run(checker._run("MATCH (n) RETURN n"))
        assert len(out) == 2
        assert checker._driver.last_session.closed, "session must be closed after _run returns"
        # records are materialized values: index + .data() work AFTER the session closed
        assert out[0]["rule_id"] == "A"
        assert out[1].data() == {"rule_id": "B"}

    def test_run_passes_params_through(self) -> None:
        checker = _checker([])
        asyncio.run(checker._run("Q", project="p", ids=[1, 2]))
        assert checker._driver.calls[-1] == ("Q", {"project": "p", "ids": [1, 2]})

    def test_query_mixin_in_mro_and_run_callable(self) -> None:
        from writ.graph.integrity import IntegrityChecker
        from writ.graph.integrity._query import _QueryMixin

        assert _QueryMixin in IntegrityChecker.__mro__
        assert callable(IntegrityChecker(None)._run)


class TestSitesMigrated:
    def test_sole_run_blocks_migrated_to_helper(self) -> None:
        async_with = 0
        self_run = 0
        for name in MIXIN_FILES:
            src = (INTEG / name).read_text()
            async_with += src.count("async with self._driver.session")
            self_run += src.count("await self._run(")
        # 31 blocks total; 6 AST-verified clean single-line-run + single-line-comprehension sites
        # migrate -> 25 async-with left in the mixins, and 6 new self._run call sites. (The other 9
        # planned sites use multi-line inline f-string queries / multi-line collects and are excluded;
        # they keep their explicit `async with`.)
        #
        # 25 -> 28 as three detectors were added, each with a multi-line query, which is the
        # documented reason a site keeps its explicit `async with` rather than migrating:
        #   +2 cycle 6a, routing_checks.py: detect_route_implementation_closure and
        #      detect_delivery_orphans, the two checks that caught the dead-route class.
        #   +1 cycle 7, artifact_checks.py: detect_artifact_abstracts_parity.
        # self._run is unchanged at 6, which is the real invariant here: the migrated sites
        # stay migrated. This count is a ratchet on NEW raw sessions, so it is expected to
        # move when a detector lands, and each move should name what added it.
        assert async_with == 28, f"expected 28 async-with blocks left in mixins; found {async_with}"
        assert self_run == 6, f"expected 6 self._run call sites; found {self_run}"
