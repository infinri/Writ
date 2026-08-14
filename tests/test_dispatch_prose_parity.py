"""Cycle E / E4: `detect_dispatch_prose_parity` -- the fourth rendering of
`dispatched_roles` that nothing checked before this check existed.

Per plan.md's E4 section: `dispatched_roles`, the DISPATCHES edge, and
`SubagentRole.dispatched_by` are three renderings of one authored fact,
already cross-checked against each other (`detect_dangling_dispatched_roles`,
`detect_dispatched_by_parity`). The FOURTH rendering is the playbook's own
prose (statement + body) -- the only one a model reading the corpus via
`traverse_neighbors` ever sees, because Playbook and SubagentRole sit outside
that walk entirely. This file exercises the new check directly against a
real graph (ENF-SYS-005: a mocked driver would only replay what the test
handed it, so it cannot prove anything about what a real Cypher MATCH
returns).

RED today: `IntegrityChecker` has no `detect_dispatch_prose_parity` method
(destined for writ/graph/integrity/structural_checks.py), so every clause
test below fails with an AttributeError. The live-corpus test additionally
encodes the plan's own measured pre-fix state: exactly two
`declared_but_unnamed` rows exist on the current corpus
(`PBK-PROC-AUDIT-FANOUT-001` / `ROL-EXPLORER-001` and `PBK-PROC-SDD-001` /
`ROL-REVIEWER-001`) and zero `named_but_undeclared`. That test asserts the
POST-fix expectation (`None`) directly, so it is RED now and turns GREEN
only once cycle E's corpus edits land -- it needs no edit at that point,
unlike a test that pinned the two current offenders by name.

Two clauses, one test class each, per the check's own docstring in plan.md:
  (1) DECLARED-BUT-UNNAMED: a DISPATCHES edge exists, but neither the role's
      `role_id` nor its `name` appears anywhere in the playbook's own
      statement + body.
  (2) NAMED-BUT-UNDECLARED: the playbook's text names a role by id or name,
      but no DISPATCHES edge targets it.

Matching is whole-identifier only (`(?<![\\w-])...(?![\\w-])`, not `\\b`,
because a hyphen is a non-word character): `writ-explorer-legacy` must not
satisfy a `writ-explorer` edge. `TestWholeIdentifierBoundary` pins that.
"""

from __future__ import annotations

import pytest
import pytest_asyncio

from writ.config import get_neo4j_password, get_neo4j_uri, get_neo4j_user
from writ.graph.db import Neo4jConnection
from writ.graph.integrity import IntegrityChecker

NEO4J_URI = get_neo4j_uri()
NEO4J_USER = get_neo4j_user()
NEO4J_PASSWORD = get_neo4j_password()


# ---------------------------------------------------------------------------
# Isolated fixture graph -- wiped before and after each test (TEST-ISOLATE-001).
# Mirrors tests/test_phase35b_field_edge_parity.py's `db` fixture: the
# established pattern for a checker method that needs constructed two-node
# scenarios per clause. Kept separate from the read-only live-corpus fixture
# below, which must never wipe or mutate anything.
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture()
async def db():
    conn = Neo4jConnection(NEO4J_URI, NEO4J_USER, NEO4J_PASSWORD)
    await conn.clear_all()
    yield conn
    await conn.clear_all()
    await conn.close()
    # Wipe-and-ABANDON was a live defect: this teardown used to leave the shared
    # 7688 graph empty, so any corpus-reading module collected after this one saw
    # nothing (found when cycle F ran the D/E suites in explicit order; only
    # alphabetical collection luck protected the full suite). ensure_corpus is
    # loop-safe by design (tests/_corpus._run_coro) and a no-op when complete.
    from tests._corpus import ensure_corpus
    ensure_corpus()


@pytest.fixture()
def checker(db: Neo4jConnection) -> IntegrityChecker:
    return IntegrityChecker(db._driver, db._database)


class TestCleanCase:
    """DISPATCHES edge + the role named in the playbook's own text -> None."""

    @pytest.mark.asyncio
    async def test_role_id_named_in_body_satisfies_the_edge(self, db, checker) -> None:
        await db.create_methodology_node("Playbook", {
            "playbook_id": "PBK-T-CLEAN-001",
            "statement": "Runs a fixed procedure.",
            "body": "Dispatch `writ-t-clean` (`ROL-T-CLEAN-001`) to do the work.",
        })
        await db.create_methodology_node("SubagentRole", {
            "role_id": "ROL-T-CLEAN-001", "name": "writ-t-clean",
        })
        await db.create_edge("DISPATCHES", "PBK-T-CLEAN-001", "ROL-T-CLEAN-001")
        assert await checker.detect_dispatch_prose_parity() is None

    @pytest.mark.asyncio
    async def test_agent_name_alone_in_statement_satisfies_the_edge(
        self, db, checker
    ) -> None:
        # The agent `name` (not the canonical role_id) appearing in
        # `statement` (not `body`) is sufficient: the check concatenates
        # statement + body before searching.
        await db.create_methodology_node("Playbook", {
            "playbook_id": "PBK-T-CLEAN-002",
            "statement": "Hands the region off to writ-t-clean-two.",
            "body": "",
        })
        await db.create_methodology_node("SubagentRole", {
            "role_id": "ROL-T-CLEAN-002", "name": "writ-t-clean-two",
        })
        await db.create_edge("DISPATCHES", "PBK-T-CLEAN-002", "ROL-T-CLEAN-002")
        assert await checker.detect_dispatch_prose_parity() is None

    @pytest.mark.asyncio
    async def test_playbook_dispatching_nobody_returns_none(self, db, checker) -> None:
        await db.create_methodology_node("Playbook", {
            "playbook_id": "PBK-T-CLEAN-003",
            "statement": "A single-session pipeline.",
            "body": "Does everything itself; dispatches nobody.",
        })
        assert await checker.detect_dispatch_prose_parity() is None


class TestDeclaredButUnnamed:
    """Clause (1): p-[:DISPATCHES]->r, but r is never named in p's own text."""

    @pytest.mark.asyncio
    async def test_edge_with_no_mention_anywhere_in_text_is_reported(
        self, db, checker
    ) -> None:
        await db.create_methodology_node("Playbook", {
            "playbook_id": "PBK-T-DBUP-001",
            "statement": "Does some work.",
            "body": "No worker is named anywhere in this text.",
        })
        await db.create_methodology_node("SubagentRole", {
            "role_id": "ROL-T-DBUP-001", "name": "writ-t-dbup",
        })
        await db.create_edge("DISPATCHES", "PBK-T-DBUP-001", "ROL-T-DBUP-001")
        result = await checker.detect_dispatch_prose_parity()
        assert result is not None
        assert result["declared_but_unnamed"] == [
            {"playbook": "PBK-T-DBUP-001", "role": "ROL-T-DBUP-001"}
        ]
        assert result["named_but_undeclared"] == []

    @pytest.mark.asyncio
    async def test_multiple_offenders_sorted_by_playbook_then_role(
        self, db, checker
    ) -> None:
        await db.create_methodology_node("Playbook", {
            "playbook_id": "PBK-T-DBUP-002", "statement": "s", "body": "b",
        })
        await db.create_methodology_node("Playbook", {
            "playbook_id": "PBK-T-DBUP-001", "statement": "s", "body": "b",
        })
        await db.create_methodology_node("SubagentRole", {
            "role_id": "ROL-T-DBUP-001", "name": "writ-t-dbup-1",
        })
        await db.create_edge("DISPATCHES", "PBK-T-DBUP-002", "ROL-T-DBUP-001")
        await db.create_edge("DISPATCHES", "PBK-T-DBUP-001", "ROL-T-DBUP-001")
        result = await checker.detect_dispatch_prose_parity()
        assert result is not None
        assert result["declared_but_unnamed"] == [
            {"playbook": "PBK-T-DBUP-001", "role": "ROL-T-DBUP-001"},
            {"playbook": "PBK-T-DBUP-002", "role": "ROL-T-DBUP-001"},
        ]


class TestNamedButUndeclared:
    """Clause (2): p's text names r by id or name, with no DISPATCHES edge."""

    @pytest.mark.asyncio
    async def test_role_id_named_with_no_edge_is_reported(self, db, checker) -> None:
        await db.create_methodology_node("Playbook", {
            "playbook_id": "PBK-T-NBUD-001",
            "statement": "Dispatches `writ-t-nbud` (`ROL-T-NBUD-001`).",
            "body": "",
        })
        await db.create_methodology_node("SubagentRole", {
            "role_id": "ROL-T-NBUD-001", "name": "writ-t-nbud",
        })
        result = await checker.detect_dispatch_prose_parity()
        assert result is not None
        assert result["named_but_undeclared"] == [
            {"playbook": "PBK-T-NBUD-001", "role": "ROL-T-NBUD-001"}
        ]
        assert result["declared_but_unnamed"] == []

    @pytest.mark.asyncio
    async def test_agent_name_named_with_no_edge_is_reported(self, db, checker) -> None:
        await db.create_methodology_node("Playbook", {
            "playbook_id": "PBK-T-NBUD-002",
            "statement": "s",
            "body": "Fresh `writ-t-nbud-two` handles this region.",
        })
        await db.create_methodology_node("SubagentRole", {
            "role_id": "ROL-T-NBUD-002", "name": "writ-t-nbud-two",
        })
        result = await checker.detect_dispatch_prose_parity()
        assert result is not None
        assert result["named_but_undeclared"] == [
            {"playbook": "PBK-T-NBUD-002", "role": "ROL-T-NBUD-002"}
        ]


class TestWholeIdentifierBoundary:
    """Matching excludes both word chars and hyphens on either side, so a
    hyphenated extension of a real identifier neither satisfies an edge to
    the shorter name nor falsely names an unrelated one."""

    @pytest.mark.asyncio
    async def test_hyphenated_extension_does_not_satisfy_a_dispatches_edge(
        self, db, checker
    ) -> None:
        # An edge to `writ-t-bound` exists, but the text only contains
        # `writ-t-bound-legacy` -- that must NOT count as naming
        # `writ-t-bound`, so the edge is still reported declared_but_unnamed.
        await db.create_methodology_node("Playbook", {
            "playbook_id": "PBK-T-BOUND-001",
            "statement": "s",
            "body": "Dispatch writ-t-bound-legacy for this step.",
        })
        await db.create_methodology_node("SubagentRole", {
            "role_id": "ROL-T-BOUND-001", "name": "writ-t-bound",
        })
        await db.create_edge("DISPATCHES", "PBK-T-BOUND-001", "ROL-T-BOUND-001")
        result = await checker.detect_dispatch_prose_parity()
        assert result is not None
        assert result["declared_but_unnamed"] == [
            {"playbook": "PBK-T-BOUND-001", "role": "ROL-T-BOUND-001"}
        ]

    @pytest.mark.asyncio
    async def test_hyphenated_extension_does_not_falsely_satisfy_named_but_undeclared(
        self, db, checker
    ) -> None:
        # The text contains `writ-t-bound-legacy` and there is genuinely no
        # edge to `writ-t-bound`: that substring must not be read as naming
        # `writ-t-bound`, so no named_but_undeclared finding is produced.
        await db.create_methodology_node("Playbook", {
            "playbook_id": "PBK-T-BOUND-002",
            "statement": "s",
            "body": "Dispatch writ-t-bound-legacy for this step.",
        })
        await db.create_methodology_node("SubagentRole", {
            "role_id": "ROL-T-BOUND-002", "name": "writ-t-bound",
        })
        result = await checker.detect_dispatch_prose_parity()
        assert result is None


class TestWiredIntoRunAllChecks:
    """`run_all_checks` must surface the new key (E4: 'wired ... immediately
    after the dispatch_invokes line'), and a finding must flip exit_code."""

    @pytest.mark.asyncio
    async def test_finding_key_present_and_gates(self, db, checker) -> None:
        await db.create_methodology_node("Playbook", {
            "playbook_id": "PBK-T-WIRE-001", "statement": "s", "body": "b",
        })
        await db.create_methodology_node("SubagentRole", {
            "role_id": "ROL-T-WIRE-001", "name": "writ-t-wire",
        })
        await db.create_edge("DISPATCHES", "PBK-T-WIRE-001", "ROL-T-WIRE-001")
        findings = await checker.run_all_checks(skip_redundancy=True)
        assert "dispatch_prose_parity" in findings
        assert findings["dispatch_prose_parity"]
        assert findings["exit_code"] == 1

    @pytest.mark.asyncio
    async def test_clean_state_finding_key_present_but_falsy(self, db, checker) -> None:
        findings = await checker.run_all_checks(skip_redundancy=True)
        assert "dispatch_prose_parity" in findings
        assert not findings["dispatch_prose_parity"]


# ---------------------------------------------------------------------------
# Read-only live-corpus fixture -- never wipes, never mutates. Mirrors
# tests/test_phase19_route_delivery_closure.py's `db_corpus` fixture.
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture()
async def db_corpus():
    # Self-heal before reading: a predecessor module may have wiped the shared
    # graph and abandoned it (the order-pollution class found at the cycle F
    # commit gate). ensure_corpus is loop-safe and a no-op when already complete,
    # so a corpus-READING fixture heals rather than depending on collection order.
    from tests._corpus import ensure_corpus
    ensure_corpus()
    conn = Neo4jConnection(NEO4J_URI, NEO4J_USER, NEO4J_PASSWORD)
    try:
        async with conn._driver.session(database=conn._database) as s:
            await (await s.run("RETURN 1 AS ok")).consume()
    except Exception:
        await conn.close()
        pytest.skip("Neo4j unreachable")
    yield conn
    await conn.close()


class TestLiveCorpusDispatchProseParity:
    """Read-only against the real corpus graph: never clear_all, never
    create or edit a node here. Target state is zero findings once cycle
    E's corpus edits land.

    RED today: plan.md's own measurement is exactly two declared_but_unnamed
    rows (PBK-PROC-AUDIT-FANOUT-001/ROL-EXPLORER-001,
    PBK-PROC-SDD-001/ROL-REVIEWER-001) and zero named_but_undeclared, on the
    corpus as it stands before cycle E's prose edits land. Asserting the
    POST-fix expectation directly (rather than pinning the two current
    offenders) means this test needs no edit when cycle E lands -- it just
    turns green.
    """

    @pytest.mark.asyncio
    async def test_returns_none_once_cycle_e_prose_edits_have_landed(
        self, db_corpus: Neo4jConnection
    ) -> None:
        checker = IntegrityChecker(db_corpus._driver, db_corpus._database)
        result = await checker.detect_dispatch_prose_parity()
        assert result is None, (
            "dispatch prose parity must be clean after cycle E's corpus "
            f"edits land; got {result}. Before cycle E this fails with "
            "exactly two declared_but_unnamed rows per plan.md's own "
            "measurement: PBK-PROC-AUDIT-FANOUT-001/ROL-EXPLORER-001 and "
            "PBK-PROC-SDD-001/ROL-REVIEWER-001."
        )
