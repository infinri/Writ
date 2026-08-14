"""Cycle A (item 1): the disposable-instance schema-readiness verdict.

Pins `classify_schema_readiness()` and `Neo4jConnection.schema_readiness()`
(plan.md ## Analysis "Cycle A (item 1)" / ## Capabilities, cycle A entries).
Neither symbol exists yet: `writ/graph/db/schema_store.py` has no
`classify_schema_readiness` function and no `schema_readiness` method on
`SchemaStoreMixin`. Every test below is RED today for exactly that reason --
each test imports its target symbol inside its own body (matching
tests/test_db_split_seam.py's convention), so a missing symbol fails that one
test with an ImportError instead of colliding collection for the whole
module, and a typo elsewhere in this file cannot be mistaken for that signal.

ENF-SYS-005 split, deliberate. `classify_schema_readiness` is a PURE function
over already-fetched SHOW-row dicts, so every TestClassify*/TestNo*/TestState*
class below constructs plain dict literals and calls it directly: no driver,
no fake, no monkeypatch, no I/O at all. A mocked driver would only replay
whatever the test handed it, which proves nothing about the classification
logic -- that split is why the pure function exists separately from the
method in the first place. Only `TestLiveSchemaReadiness` touches a real
graph, because "the live isolated instance actually reports ready" is a claim
no pure test can make.

GROUND TRUTH the dict literals below reproduce exactly (verified live against
Neo4j 5 on the isolated instance, 2026-08-13):
  SHOW INDEXES row keys:     entityType, id, indexProvider, labelsOrTypes,
                             lastRead, name, owningConstraint,
                             populationPercent, properties, readCount, state,
                             type. Observed `state`: "ONLINE". Observed
                             `type`: LOOKUP, RANGE.
  SHOW CONSTRAINTS row keys: entityType, id, labelsOrTypes, name, ownedIndex,
                             properties, propertyType, type. NO `state` key
                             at all -- constraint readiness can only be read
                             indirectly, through `ownedIndex`. A check that
                             asks a constraint row for `state` reads a key
                             that does not exist, which is the defect class
                             this whole cycle exists to prevent.
"""
from __future__ import annotations

import pytest


class TestEmptySchemaIsNotReady:
    """Zero index rows means no DDL has run at all, not that nothing is broken."""

    def test_no_indexes_and_no_constraints_reports_ready_false(self) -> None:
        from writ.graph.db.schema_store import classify_schema_readiness

        result = classify_schema_readiness([], [])

        assert result["ready"] is False, (
            "classify_schema_readiness([], []) must report ready=False: an empty "
            f"index list means no schema DDL has run yet, not that nothing is "
            f"broken, got {result}"
        )
        assert result["indexes"] == 0, (
            f"expected indexes=0 for an empty index list, got {result}"
        )
        assert result["not_online"] == [], (
            f"expected not_online=[] when there are no index rows at all, "
            f"got {result['not_online']}"
        )
        assert result["missing_owned"] == [], (
            f"expected missing_owned=[] when there are no constraint rows at all, "
            f"got {result['missing_owned']}"
        )


class TestNonOnlineIndexBlocksReadiness:
    """Any index row whose state is not ONLINE must fail the verdict and be named."""

    def test_populating_index_reports_ready_false_and_is_named_with_state_and_percent(
        self,
    ) -> None:
        from writ.graph.db.schema_store import classify_schema_readiness

        indexes = [{"name": "rule_domain", "state": "POPULATING", "populationPercent": 42.0}]

        result = classify_schema_readiness(indexes, [])

        assert result["ready"] is False, (
            f"a POPULATING index must make the verdict ready=False, got {result}"
        )
        assert result["not_online"] == ["rule_domain=POPULATING@42.0"], (
            "not_online must identify the non-ONLINE index by name, state and "
            f"populationPercent; got {result['not_online']}"
        )


class TestAllOnlineWithOwnedIndexesSatisfiedIsReady:
    """Every index ONLINE, with every constraint's ownedIndex among them, is ready."""

    def test_all_online_indexes_and_satisfied_owned_index_reports_ready_true(self) -> None:
        from writ.graph.db.schema_store import classify_schema_readiness

        indexes = [
            {"name": "project_name_unique", "state": "ONLINE", "populationPercent": 100.0},
            {"name": "rule_domain", "state": "ONLINE", "populationPercent": 100.0},
        ]
        constraints = [{"name": "project_name_unique", "ownedIndex": "project_name_unique"}]

        result = classify_schema_readiness(indexes, constraints)

        assert result["ready"] is True, (
            "all-ONLINE index rows with every constraint's ownedIndex present must "
            f"report ready=True, got {result}"
        )
        assert result["not_online"] == [], (
            f"expected no not_online entries when every index is ONLINE, got {result['not_online']}"
        )
        assert result["missing_owned"] == [], (
            f"expected no missing_owned entries when every ownedIndex is present, "
            f"got {result['missing_owned']}"
        )


class TestMissingOwnedIndexBlocksReadiness:
    """A constraint whose ownedIndex is absent from the ONLINE set is not enforceable."""

    def test_owned_index_absent_from_online_set_reports_ready_false_and_lists_it(self) -> None:
        from writ.graph.db.schema_store import classify_schema_readiness

        indexes = [{"name": "rule_domain", "state": "ONLINE", "populationPercent": 100.0}]
        constraints = [{"name": "project_name_unique", "ownedIndex": "project_name_unique"}]

        result = classify_schema_readiness(indexes, constraints)

        assert result["ready"] is False, (
            "a constraint whose ownedIndex names an index absent from the ONLINE "
            f"set must report ready=False, got {result}"
        )
        assert result["missing_owned"] == ["project_name_unique"], (
            f"missing_owned must list the absent ownedIndex name, got {result['missing_owned']}"
        )


class TestStateReadOnlyFromIndexRows:
    """SHOW CONSTRAINTS rows carry no `state` key; the verdict must never read one there."""

    def test_constraint_row_with_no_state_key_does_not_raise_and_is_not_treated_as_not_online(
        self,
    ) -> None:
        from writ.graph.db.schema_store import classify_schema_readiness

        indexes = [{"name": "rule_domain", "state": "ONLINE", "populationPercent": 100.0}]
        # Real SHOW CONSTRAINTS rows have exactly entityType, id, labelsOrTypes,
        # name, ownedIndex, properties, propertyType, type -- there is no "state"
        # key at all. This literal omits it on purpose (the real driver shape),
        # to prove the classifier never reads state off a constraint row.
        constraints = [{"name": "project_name_unique", "ownedIndex": "rule_domain"}]

        result = classify_schema_readiness(indexes, constraints)  # must not raise KeyError

        assert result["ready"] is True, (
            "a constraint row with no state key must not make the verdict raise "
            f"or silently read None as not-online; its owned index IS online, "
            f"got {result}"
        )
        assert result["missing_owned"] == [], (
            f"the constraint's ownedIndex is ONLINE, so missing_owned must be "
            f"empty, got {result['missing_owned']}"
        )


class TestNullOwnedIndexExcludedFromMissingOwned:
    """A constraint with ownedIndex=None has no index to wait on."""

    def test_constraint_with_null_owned_index_is_not_listed_as_missing(self) -> None:
        from writ.graph.db.schema_store import classify_schema_readiness

        constraints = [{"name": "some_constraint", "ownedIndex": None}]

        result = classify_schema_readiness([], constraints)

        assert result["missing_owned"] == [], (
            "a constraint whose ownedIndex is null has no index to wait on, so "
            f"it must never appear in missing_owned; got {result['missing_owned']}"
        )


class TestLiveSchemaReadiness:
    """The one real-graph claim: the isolated instance actually reports ready.

    Uses tests/_graph.py's `connection()` factory, which resolves through
    writ.config and is forced onto bolt://localhost:7688 (never 7687,
    production) by tests/conftest.py at import, plus tests/conftest.py's
    `corpus_ready` fixture, which skips this test outright when Neo4j is
    unreachable rather than letting an empty result read as passing.
    """

    @pytest.mark.asyncio
    async def test_schema_readiness_reports_ready_with_nonzero_indexes_on_isolated_instance(
        self, corpus_ready
    ) -> None:
        from tests._graph import connection

        db = connection()
        try:
            await db.apply_constraints()
            result = await db.schema_readiness()
        finally:
            await db.close()

        assert result["ready"] is True, (
            "schema_readiness() against the isolated instance must report "
            f"ready=True after apply_constraints, got {result}"
        )
        assert result["indexes"] > 0, (
            f"expected a non-zero index count on the isolated instance, got {result['indexes']}"
        )
        assert result["not_online"] == [], (
            f"expected no non-ONLINE indexes on the isolated instance, got {result['not_online']}"
        )
