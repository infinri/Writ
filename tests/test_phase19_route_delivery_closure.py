"""1.9: route implementation closure + delivery orphans.

The defect these two checks close, measured against the live corpus before
authoring this file: `RouteValue` includes `ride_along`; exactly one Category
(`CAT-DISC-001`) declared `routes: ['ride_along']`; and no delivery channel
(Stage-1 ranked pool, floor injection, action push, or pull-by-keyword)
branches on that string. Its 26 members -- 14 of them AntiPatterns -- were
therefore undeliverable with zero failing check. `detect_route_implementation_
closure` catches the declared-but-unwired route; `detect_delivery_orphans`
catches the resulting unreachable members directly, as a conjunction over
every channel a methodology node could use.

CORPUS REPAIR (written to bible/ 2026-08-12): CAT-DISC-001 now declares
`routes: [pull]` (a WIRED_ROUTES member), all 14 of its AntiPattern members
carry three `trigger_keywords` each (an independent pull-by-keyword channel on
top of the now-wired category route), ANT-PROC-VERIFY-001 additionally floors
in all five modes, ANT-PROC-FINISH-001 carries `action_triggers: ["finish"]`,
and ANT-PROC-WORKTREE-001 carries `action_triggers: ["worktree"]`. The same
repair also strips the dead `scoped` route from the four
CAT-CODE-{FW-MAGENTO,LANG-PHP,LANG-PYTHON,LANG-SQL}-001 categories. Both
halves are ONE pending import, not an already-landed change plus a
still-pending one: running this file's real-corpus tests against the live
graph (verified while retargeting this file, 2026-08-12) shows all four
CAT-CODE-* categories still reporting `scoped` today, right alongside
CAT-DISC-001's `ride_along`. CAT-DISC-001 was the last unwired-route category
the bible/ files declare, so once the whole repair reaches the live graph,
`detect_route_implementation_closure` returns None corpus-wide and
`detect_delivery_orphans` returns zero orphans: neither detector has a known
real violation left to report.

That clean result is indistinguishable from a detector that is simply broken
and always returns None. So every test below that asserts the clean state
against the real corpus is paired with a sibling test that constructs a
genuine violation (via the fake-driver doubles in this file) and confirms the
same detector still flags it, keeping the proof that the detector can fire at
all independent of the corpus staying broken.

TIMING: this repair is written to the bible/ source files but, as of this
retargeting, has not yet been imported into the live (production, port 7687)
graph, for EITHER half (CAT-DISC-001 or the four CAT-CODE-* categories). That
import is deliberately out of scope for this file (see GRAPH SAFETY below)
and runs separately. Until it does, every test below that asserts the clean
state against the real corpus (via the `db_corpus` fixture) will legitimately
fail, reporting the pre-repair state; that is not a defect in the test, it is
the live graph not yet reflecting the files on disk. Those tests are expected
to pass once the import runs.

WIRED_ROUTES CONTRACT (finalized 2026-08-12): WIRED_ROUTES is a HAND-LISTED
set of the routes with a verified delivery mechanism ({action, always_on,
pull, semantic, state}), not derived from VALID_ROUTES by subtraction (an
earlier draft of this contract, since retracted: subtraction makes a newly
added RouteValue member wired by default, which is how `ride_along` survived
unimplemented for months in the first place). It excludes BOTH `ride_along`
AND `scoped` (the latter discovered independently unimplemented, on
CAT-CODE-FW-MAGENTO-001 and CAT-CODE-LANG-{PHP,PYTHON,SQL}-001, while
verifying the hand-list). The bible/ corpus repair above rewrites those four
categories' `routes` off `scoped` and rewrites CAT-DISC-001's `routes` off
`ride_along`, but WIRED_ROUTES itself did not change: `ride_along` and
`scoped` both remain genuinely unwired, which is exactly what the
constructed-violation tests below exercise. See
tests/test_category_schema.py::TestRouteValueEnum for the schema-level
contract tests (the exact hand-list, the strict-subset relationship, and the
anti-drift subset guard).
TestWiredRoutesIndependenceFromValidRoutesGrowth below is the regression
guard for the hand-list redesign itself: a hypothetical new RouteValue member
must not silently become wired, proven by constructing the scenario against
the real detector rather than reasoning about it.

GRAPH SAFETY: this file runs against the SAME live (production, port 7687)
graph the rest of the integrity suite targets. No clear_all, no reingest, no
delete-by-label, anywhere in this file. Tests that read the corpus' current
state do so read-only, never mutating a live Category's `routes` (an earlier
version of this file did a scoped snapshot/mutate/restore round-trip on the
live graph to manufacture a clean state; that is no longer necessary now that
the repaired corpus is clean on its own, and it is deliberately not reused
below). Tests that need a node or Category that does not exist in the real
corpus (an empty graph, or a genuinely unwired route now that the corpus is
clean) use a fake driver double instead of touching Neo4j.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
import pytest_asyncio

from writ.config import get_neo4j_password, get_neo4j_uri, get_neo4j_user
from writ.graph.db import Neo4jConnection
from writ.graph.integrity import IntegrityChecker

# ---------------------------------------------------------------------------
# REAL-GRAPH fixture -- connects to the live corpus, skips if unreachable.
# No clear_all, no ingest_path: this file never wipes or rebuilds the corpus.
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture()
async def db_corpus():
    db = Neo4jConnection(get_neo4j_uri(), get_neo4j_user(), get_neo4j_password())
    try:
        async with db._driver.session(database=db._database) as s:
            await (await s.run("RETURN 1 AS ok")).consume()
    except Exception:
        await db.close()
        pytest.skip("Neo4j unreachable")
    yield db
    await db.close()


# ---------------------------------------------------------------------------
# Fake driver double for the "graph has zero Category nodes" guard tests.
# The live corpus always has Category nodes and GRAPH SAFETY forbids deleting
# them to construct a real empty state, so this double stands in. It answers
# the guard's `MATCH (c:Category) RETURN count(c) AS c` -> `.single()["c"]`
# idiom (confirmed against the shipped detect_route_implementation_closure /
# detect_delivery_orphans source) with a zero count, and answers any row
# iteration with nothing, for any query text.
# ---------------------------------------------------------------------------


class _EmptyResult:
    async def single(self):
        return {"c": 0, "count": 0}

    def __aiter__(self):
        return self

    async def __anext__(self):
        raise StopAsyncIteration


class _EmptySession:
    async def run(self, query, **params):
        return _EmptyResult()

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc_info):
        return False


class _EmptyDriver:
    """Stands in for a connected driver over a graph with zero Category nodes."""

    def session(self, database=None):
        return _EmptySession()


class _CategoryRowsResult:
    """Like _EmptyResult but parameterized: answers `.single()` with a
    non-zero count (so the presence guard proceeds past the skip branch) and
    answers row iteration with the exact (category_id, routes) rows given,
    for any query text."""

    def __init__(self, rows: list[tuple[str, list[str]]]) -> None:
        self._rows = rows

    async def single(self):
        return {"c": len(self._rows), "count": len(self._rows)}

    def __aiter__(self):
        self._iter = iter(self._rows)
        return self

    async def __anext__(self):
        try:
            cat, routes = next(self._iter)
        except StopIteration:
            raise StopAsyncIteration
        return {"cat": cat, "routes": routes}


class _CategoryRowsSession:
    def __init__(self, rows: list[tuple[str, list[str]]]) -> None:
        self._rows = rows

    async def run(self, query, **params):
        return _CategoryRowsResult(self._rows)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc_info):
        return False


class _CategoryRowsDriver:
    """Stands in for a connected driver over a graph containing exactly the
    given (category_id, routes) rows. Used to construct a scenario (a
    hypothetical unimplemented route) that cannot be built on the live
    corpus without inventing a real RouteValue member, which would be a
    production-code change."""

    def __init__(self, rows: list[tuple[str, list[str]]]) -> None:
        self._rows = rows

    def session(self, database=None):
        return _CategoryRowsSession(self._rows)


class _NodeRow(dict):
    """A dict that also answers `.data()`, matching the neo4j Record interface
    detect_delivery_orphans relies on (`rows = [r.data() async for r in
    result]`); a plain dict has no `.data()` method so `_CategoryRowsResult`'s
    rows (consumed via `r["cat"]` directly) cannot stand in for this query."""

    def data(self) -> dict:
        return dict(self)


class _DeliveryRowsResult:
    """Answers detect_delivery_orphans' two queries against a fake driver: the
    corpus-presence guard's `.single()` (a non-zero count, so the guard
    proceeds past the skip branch) and the main query's row iteration, which
    yields exactly the given node rows (id, label, cat, routes, floor_modes,
    action_triggers, trigger_keywords) for any query text."""

    def __init__(self, rows: list[dict]) -> None:
        self._rows = rows

    async def single(self):
        count = max(len(self._rows), 1)
        return {"c": count, "count": count}

    def __aiter__(self):
        self._iter = iter(self._rows)
        return self

    async def __anext__(self):
        try:
            row = next(self._iter)
        except StopIteration:
            raise StopAsyncIteration
        return _NodeRow(row)


class _DeliveryRowsSession:
    def __init__(self, rows: list[dict]) -> None:
        self._rows = rows

    async def run(self, query, **params):
        return _DeliveryRowsResult(self._rows)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc_info):
        return False


class _DeliveryRowsDriver:
    """Stands in for a connected driver over a graph containing exactly the
    given node rows. Used to construct a node meeting detect_delivery_orphans'
    full five-way conjunction (no floor_modes, no action_triggers, no
    trigger_keywords, and a category routing to neither semantic nor pull)
    now that the repaired live corpus no longer has one."""

    def __init__(self, rows: list[dict]) -> None:
        self._rows = rows

    def session(self, database=None):
        return _DeliveryRowsSession(self._rows)


class TestRouteImplementationClosureReportsViolation:
    """detect_route_implementation_closure against the untouched live corpus:
    the specific CAT-DISC-001/ride_along violation this file was originally
    authored against is gone now that CAT-DISC-001 declares `routes: [pull]`,
    a WIRED_ROUTES member. This class asserts that absence on the real corpus
    and, separately, proves via a fake driver that the detector would still
    catch that exact pattern (category declares only `ride_along`) if it ever
    recurred -- the repair changed the DATA, not the detector or WIRED_ROUTES."""

    @pytest.mark.asyncio
    async def test_cat_disc_001_ride_along_no_longer_reported(
        self, db_corpus: Neo4jConnection
    ) -> None:
        checker = IntegrityChecker(db_corpus._driver, db_corpus._database)
        result = await checker.detect_route_implementation_closure()
        assert "CAT-DISC-001" not in (result or {}), (
            "CAT-DISC-001 now declares routes: ['pull'], a WIRED_ROUTES "
            f"member; it must no longer be reported. Got "
            f"{(result or {}).get('CAT-DISC-001')!r} (full result: {result}). "
            "If this is failing because the corpus repair has not been "
            "imported into the live graph yet, that is expected: this test "
            "passes once the import runs."
        )

    @pytest.mark.asyncio
    async def test_detector_still_reports_that_exact_pattern_when_constructed(
        self,
    ) -> None:
        checker = IntegrityChecker(
            _CategoryRowsDriver([("CAT-DISC-001", ["ride_along"])]), "neo4j"
        )
        result = await checker.detect_route_implementation_closure()
        assert result == {"CAT-DISC-001": ["ride_along"]}, (
            "a category declaring routes: ['ride_along'] must still be "
            f"reported as unwired; got {result}"
        )


class TestRouteImplementationClosureCleanState:
    """The non-vacuity witness at corpus scope: prove the detector recognizes
    the WHOLE repaired corpus as clean (not just CAT-DISC-001), and separately
    prove it is not simply a no-op detector by constructing a fresh violation
    via a fake driver and confirming it still fires."""

    @pytest.mark.asyncio
    async def test_returns_none_on_the_repaired_corpus(
        self, db_corpus: Neo4jConnection
    ) -> None:
        checker = IntegrityChecker(db_corpus._driver, db_corpus._database)
        result = await checker.detect_route_implementation_closure()
        assert result is None, (
            "every Category should now declare only WIRED_ROUTES members "
            f"(CAT-DISC-001 was the last unwired-route holdout); got "
            f"{result}. If this is failing because the corpus repair has not "
            "been imported into the live graph yet, that is expected: this "
            "test passes once the import runs."
        )

    @pytest.mark.asyncio
    async def test_detector_still_fires_on_a_constructed_violation(self) -> None:
        checker = IntegrityChecker(
            _CategoryRowsDriver([("CAT-CONSTRUCTED-001", ["scoped", "semantic"])]),
            "neo4j",
        )
        result = await checker.detect_route_implementation_closure()
        assert result == {"CAT-CONSTRUCTED-001": ["scoped"]}, (
            "a category mixing a wired route (semantic) with a genuinely "
            f"unwired one (scoped) must still be reported for the unwired "
            f"member only; got {result}"
        )


class TestWiredRoutesIndependenceFromValidRoutesGrowth:
    """The regression guard for the WIRED_ROUTES redesign (hand-list, not a
    VALID_ROUTES subtraction): adding a member to RouteValue must NOT
    silently make it wired. If WIRED_ROUTES were still `VALID_ROUTES -
    {ride_along}`, growing VALID_ROUTES would grow WIRED_ROUTES automatically
    -- exactly the failure mode that let `ride_along` go undetected.

    Constructed, not reasoned about: a hypothetical 7th route is added to
    VALID_ROUTES (simulating RouteValue gaining a member), the real
    WIRED_ROUTES is left untouched, and a category declaring only that route
    is fed through the REAL detect_route_implementation_closure via a fake
    driver (the live corpus has no such category, and inventing a real
    RouteValue member to test with would be a production-code change)."""

    @pytest.mark.asyncio
    async def test_new_route_value_is_not_silently_wired(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import writ.graph.schema as schema_module

        hypothetical_route = "quantum_route"
        monkeypatch.setattr(
            schema_module,
            "VALID_ROUTES",
            schema_module.VALID_ROUTES | {hypothetical_route},
        )
        assert hypothetical_route not in schema_module.WIRED_ROUTES, (
            f"WIRED_ROUTES must stay independent of VALID_ROUTES growth; "
            f"after VALID_ROUTES gained {hypothetical_route!r}, WIRED_ROUTES "
            f"is still {sorted(schema_module.WIRED_ROUTES)} (unchanged, as "
            f"expected) -- if this assertion fails, {hypothetical_route!r} "
            f"leaked into WIRED_ROUTES somehow"
        )

        checker = IntegrityChecker(
            _CategoryRowsDriver([("CAT-HYPOTHETICAL-001", [hypothetical_route])]),
            "neo4j",
        )
        result = await checker.detect_route_implementation_closure()
        expected = {"CAT-HYPOTHETICAL-001": [hypothetical_route]}
        assert result == expected, (
            f"a category declaring a newly-added-but-unimplemented route must "
            f"be reported, not silently treated as wired; expected {expected}, "
            f"got {result}"
        )


class TestDeliveryOrphansFlagsViolation:
    """detect_delivery_orphans against the untouched live corpus: the specific
    ANT-PROC-DEBUG-001 orphan this file was originally authored against is
    gone now that its category (CAT-DISC-001) routes to `pull` (a selecting
    route) and ANT-PROC-DEBUG-001 additionally carries its own
    trigger_keywords. This class asserts the real corpus now reports zero
    orphans and, separately, proves via a fake driver that the detector still
    flags a node meeting the full five-way conjunction (no floor_modes, no
    action_triggers, no trigger_keywords, and a category routing to neither
    semantic nor pull)."""

    @pytest.mark.asyncio
    async def test_real_corpus_has_no_orphans(
        self, db_corpus: Neo4jConnection
    ) -> None:
        checker = IntegrityChecker(db_corpus._driver, db_corpus._database)
        result = await checker.detect_delivery_orphans()
        assert result is None, (
            "CAT-DISC-001 now routes to pull (a selecting route) and each of "
            "its AntiPattern members carries its own trigger_keywords, so no "
            f"methodology node should remain undeliverable; got {result}. If "
            "this is failing because the corpus repair has not been imported "
            "into the live graph yet, that is expected: this test passes "
            "once the import runs."
        )

    @pytest.mark.asyncio
    async def test_detector_still_flags_a_constructed_orphan(self) -> None:
        rows = [
            {
                "id": "ANT-TEST-CONSTRUCTED-001",
                "label": "AntiPattern",
                "cat": "CAT-TEST-CONSTRUCTED-001",
                "routes": ["action"],
                "floor_modes": None,
                "action_triggers": None,
                "trigger_keywords": None,
            }
        ]
        checker = IntegrityChecker(_DeliveryRowsDriver(rows), "neo4j")
        result = await checker.detect_delivery_orphans()
        assert result == {
            "ANT-TEST-CONSTRUCTED-001": {
                "label": "AntiPattern",
                "category": "CAT-TEST-CONSTRUCTED-001",
                "routes": ["action"],
            }
        }, (
            "a node with no floor_modes, no action_triggers, no "
            "trigger_keywords, and a category routing only to 'action' "
            f"(neither semantic nor pull) must still be flagged as an "
            f"orphan; got {result}"
        )


class TestDeliveryOrphansOverFlagGuard:
    """THE OVER-FLAG GUARD -- the most important tests in this file. A node
    reachable by exactly ONE channel must never be flagged: flagging either of
    these makes the check noise and gets it switched off, wasting the cycle.
    Covered as two separate tests, one per named node, one channel each."""

    @pytest.mark.asyncio
    async def test_floor_only_node_not_flagged(self, db_corpus: Neo4jConnection) -> None:
        # SKL-PROC-MODE-001: floor_modes=['conversation','work'] -- reachable
        # via the floor channel alone. Its category (CAT-PROC-001) also routes
        # 'pull', a second independent reason it must stay silent here.
        checker = IntegrityChecker(db_corpus._driver, db_corpus._database)
        result = await checker.detect_delivery_orphans()
        flagged = result or {}
        assert "SKL-PROC-MODE-001" not in flagged, (
            "SKL-PROC-MODE-001 is floor-reachable; flagging it is a false "
            f"positive: {flagged.get('SKL-PROC-MODE-001')}"
        )

    @pytest.mark.asyncio
    async def test_action_only_node_not_flagged(self, db_corpus: Neo4jConnection) -> None:
        # SKL-PROC-REVRECV-001: action_triggers=['review-feedback'] -- reachable
        # via the action channel alone. Its category (CAT-COMM-001) routes
        # [always_on, action] -- NEITHER semantic NOR pull -- so the category
        # channel cannot save it; only its own action_triggers key does.
        checker = IntegrityChecker(db_corpus._driver, db_corpus._database)
        result = await checker.detect_delivery_orphans()
        flagged = result or {}
        assert "SKL-PROC-REVRECV-001" not in flagged, (
            "SKL-PROC-REVRECV-001 is action-reachable; flagging it is a false "
            f"positive: {flagged.get('SKL-PROC-REVRECV-001')}"
        )


class TestReturnsNoneWithoutDriver:
    """Both detectors: no driver -> None. No live graph touched."""

    @pytest.mark.asyncio
    async def test_closure_returns_none_without_driver(self) -> None:
        checker = IntegrityChecker(None, None)
        assert await checker.detect_route_implementation_closure() is None

    @pytest.mark.asyncio
    async def test_orphans_returns_none_without_driver(self) -> None:
        checker = IntegrityChecker(None, None)
        assert await checker.detect_delivery_orphans() is None


class TestSkipsWhenNoCategoryNodes:
    """Both detectors must treat an absent Category corpus as a skip (None),
    matching detect_floor_completeness's existing corpus-presence guard.

    Why: without any Category to compare against, EVERY methodology node
    would look route-less, and a detector that reports "the whole corpus is
    unwired/orphaned" against a graph that simply has not been populated yet
    (e.g. a crafted test graph of a few rules) is worse than no detector --
    it trains the reader to ignore the check. Skipping (None) when the
    corpus genuinely has zero categories keeps the check silent until there
    is something meaningful to say.

    Uses the fake driver double (never the live corpus): production always
    has Category nodes, and GRAPH SAFETY forbids deleting them to construct
    a real empty state.
    """

    @pytest.mark.asyncio
    async def test_closure_skips_when_no_categories(self) -> None:
        checker = IntegrityChecker(_EmptyDriver(), "neo4j")
        result = await checker.detect_route_implementation_closure()
        assert result is None

    @pytest.mark.asyncio
    async def test_orphans_skips_when_no_categories(self) -> None:
        checker = IntegrityChecker(_EmptyDriver(), "neo4j")
        result = await checker.detect_delivery_orphans()
        assert result is None


# ---------------------------------------------------------------------------
# run_all_checks wiring. Pure mock unit tests (no Neo4j). The baseline patch
# set below is NOT optional padding: detect_conflicts/detect_orphans/
# detect_stale/detect_redundant/check_unreviewed_count/detect_frequency_stale/
# detect_graduation_flags/detect_dangling_dispatched_roles/
# detect_orphans_all_labels (the pre-Phase-0 structural/frequency checks) have
# no `if self._driver is None: return None` guard and raise AttributeError on
# a None driver, unlike the newer routing_checks.py additions -- this exact
# set is copied from test_validate_exit_orphans.py's `_checker_with_findings`,
# which stubs precisely these for the same reason.
# ---------------------------------------------------------------------------


def _baseline_patches() -> dict:
    return {
        "detect_conflicts": AsyncMock(return_value=[]),
        "detect_orphans": AsyncMock(return_value=[]),
        "detect_stale": AsyncMock(return_value=[]),
        "detect_redundant": AsyncMock(return_value=[]),
        "check_unreviewed_count": AsyncMock(return_value=None),
        "detect_frequency_stale": AsyncMock(return_value=[]),
        "detect_graduation_flags": AsyncMock(return_value=[]),
        "detect_dangling_dispatched_roles": AsyncMock(return_value=[]),
        "detect_orphans_all_labels": AsyncMock(return_value=([], {})),
    }


async def _run_with_patches(checker: IntegrityChecker, patches: dict) -> dict:
    cms = [patch.object(checker, name, mock) for name, mock in patches.items()]
    for cm in cms:
        cm.start()
    try:
        return await checker.run_all_checks(skip_redundancy=True)
    finally:
        for cm in cms:
            cm.stop()


class TestRunAllChecksWiresRouteAndDeliveryClosure:
    """run_all_checks must surface both under their own findings keys and
    fold either into a non-zero exit_code."""

    @pytest.mark.asyncio
    async def test_route_implementation_closure_flips_exit_code(self) -> None:
        checker = IntegrityChecker(None, None)
        patches = _baseline_patches()
        patches["detect_route_implementation_closure"] = AsyncMock(
            return_value={"CAT-DISC-001": ["ride_along"]}
        )
        patches["detect_delivery_orphans"] = AsyncMock(return_value=None)
        findings = await _run_with_patches(checker, patches)
        assert findings.get("route_implementation_closure") == {
            "CAT-DISC-001": ["ride_along"]
        }
        assert findings["exit_code"] == 1

    @pytest.mark.asyncio
    async def test_delivery_orphans_flips_exit_code(self) -> None:
        checker = IntegrityChecker(None, None)
        patches = _baseline_patches()
        patches["detect_route_implementation_closure"] = AsyncMock(return_value=None)
        patches["detect_delivery_orphans"] = AsyncMock(
            return_value={
                "ANT-PROC-DEBUG-001": {
                    "label": "AntiPattern",
                    "category": "CAT-DISC-001",
                    "routes": ["ride_along"],
                }
            }
        )
        findings = await _run_with_patches(checker, patches)
        assert "ANT-PROC-DEBUG-001" in findings.get("delivery_orphans", {})
        assert findings["exit_code"] == 1

    @pytest.mark.asyncio
    async def test_both_clean_does_not_force_nonzero(self) -> None:
        checker = IntegrityChecker(None, None)
        patches = _baseline_patches()
        patches["detect_route_implementation_closure"] = AsyncMock(return_value=None)
        patches["detect_delivery_orphans"] = AsyncMock(return_value=None)
        findings = await _run_with_patches(checker, patches)
        assert findings["exit_code"] == 0
