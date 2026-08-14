"""Phase 4: Integrity check tests.

Tests that IntegrityChecker detects known problems in crafted fixtures.
Requires Neo4j running with test data.
Each test is isolated (TEST-ISO-001).
"""

from __future__ import annotations

from datetime import date, timedelta

import pytest
import pytest_asyncio

from writ.config import get_neo4j_password, get_neo4j_uri, get_neo4j_user
from writ.graph.db import Neo4jConnection
from writ.graph.integrity import IntegrityChecker

NEO4J_URI = get_neo4j_uri()
NEO4J_USER = get_neo4j_user()
NEO4J_PASSWORD = get_neo4j_password()


def _make_rule(
    rule_id: str,
    mandatory: bool = False,
    trigger: str = "Default trigger",
    statement: str = "Default statement",
    last_validated: str | None = None,
    staleness_window: int = 365,
) -> dict:
    if last_validated is None:
        last_validated = date.today().isoformat()
    return {
        "rule_id": rule_id,
        "domain": "Test",
        "severity": "medium",
        "scope": "file",
        "trigger": trigger,
        "statement": statement,
        "violation": "Bad.",
        "pass_example": "Good.",
        "enforcement": "Review.",
        "rationale": "Testing.",
        "mandatory": mandatory,
        "confidence": "production-validated",
        "evidence": "doc:original-bible",
        "staleness_window": staleness_window,
        "last_validated": last_validated,
    }


@pytest_asyncio.fixture()
async def db():
    conn = Neo4jConnection(NEO4J_URI, NEO4J_USER, NEO4J_PASSWORD)
    await conn.clear_all()
    yield conn
    await conn.clear_all()
    await conn.close()


@pytest.fixture()
def checker(db: Neo4jConnection) -> IntegrityChecker:
    return IntegrityChecker(db._driver, db._database)


class TestConflictDetection:
    """CONFLICTS_WITH edge detection."""

    @pytest.mark.asyncio
    async def test_conflict_detected(self, db: Neo4jConnection, checker: IntegrityChecker) -> None:
        await db.create_rule(_make_rule("RULE-A-001"))
        await db.create_rule(_make_rule("RULE-B-001"))
        await db.create_edge("CONFLICTS_WITH", "RULE-A-001", "RULE-B-001")

        conflicts = await checker.detect_conflicts()
        assert len(conflicts) == 1
        pair = conflicts[0]
        assert pair["rule_a"] == "RULE-A-001"
        assert pair["rule_b"] == "RULE-B-001"

    @pytest.mark.asyncio
    async def test_no_false_positives(self, db: Neo4jConnection, checker: IntegrityChecker) -> None:
        await db.create_rule(_make_rule("RULE-A-001"))
        await db.create_rule(_make_rule("RULE-B-001"))
        await db.create_edge("DEPENDS_ON", "RULE-A-001", "RULE-B-001")

        conflicts = await checker.detect_conflicts()
        assert len(conflicts) == 0


class TestOrphanDetection:
    """Rules with zero edges."""

    @pytest.mark.asyncio
    async def test_orphan_flagged(self, db: Neo4jConnection, checker: IntegrityChecker) -> None:
        await db.create_rule(_make_rule("ORPHAN-RULE-001"))

        orphans = await checker.detect_orphans()
        assert "ORPHAN-RULE-001" in orphans

    @pytest.mark.asyncio
    async def test_connected_rule_not_orphan(self, db: Neo4jConnection, checker: IntegrityChecker) -> None:
        await db.create_rule(_make_rule("RULE-A-001"))
        await db.create_rule(_make_rule("RULE-B-001"))
        await db.create_edge("RELATED_TO", "RULE-A-001", "RULE-B-001")

        orphans = await checker.detect_orphans()
        assert "RULE-A-001" not in orphans
        assert "RULE-B-001" not in orphans


class TestStalenessDetection:
    """Rules past staleness window."""

    @pytest.mark.asyncio
    async def test_stale_rule_flagged(self, db: Neo4jConnection, checker: IntegrityChecker) -> None:
        old_date = (date.today() - timedelta(days=400)).isoformat()
        await db.create_rule(_make_rule("STALE-RULE-001", last_validated=old_date, staleness_window=365))

        stale = await checker.detect_stale()
        stale_ids = [s["rule_id"] for s in stale]
        assert "STALE-RULE-001" in stale_ids

    @pytest.mark.asyncio
    async def test_fresh_rule_not_flagged(self, db: Neo4jConnection, checker: IntegrityChecker) -> None:
        today = date.today().isoformat()
        await db.create_rule(_make_rule("FRESH-RULE-001", last_validated=today))

        stale = await checker.detect_stale()
        stale_ids = [s["rule_id"] for s in stale]
        assert "FRESH-RULE-001" not in stale_ids


class TestStalenessAllLabels:
    """2.7b: detect_stale must scan ALL node types, not just :Rule, so a stale
    methodology node (Skill/Playbook/...) is flagged rather than read as fresh."""

    @pytest.mark.asyncio
    async def test_stale_methodology_node_flagged(self, db: Neo4jConnection, checker: IntegrityChecker) -> None:
        old_date = (date.today() - timedelta(days=400)).isoformat()
        async with db._driver.session(database=db._database) as s:
            await s.run(
                "CREATE (n:Skill {skill_id:$id, last_validated:$d, staleness_window:365})",
                id="STALE-SKILL-001", d=old_date,
            )
        stale = await checker.detect_stale()
        ids = [row["rule_id"] for row in stale]
        assert "STALE-SKILL-001" in ids, (
            f"detect_stale did not flag a stale non-Rule node (Rule-only bug): {stale}"
        )

    @pytest.mark.asyncio
    async def test_fresh_methodology_node_not_flagged(self, db: Neo4jConnection, checker: IntegrityChecker) -> None:
        today = date.today().isoformat()
        async with db._driver.session(database=db._database) as s:
            await s.run(
                "CREATE (n:Skill {skill_id:$id, last_validated:$d, staleness_window:365})",
                id="FRESH-SKILL-001", d=today,
            )
        stale = await checker.detect_stale()
        ids = [row["rule_id"] for row in stale]
        assert "FRESH-SKILL-001" not in ids


class TestDeleteRuleCount:
    """2.7a: delete_rule must report deletion accurately. count(r) after a
    DETACH DELETE is unreliable; the fix relies on the result summary counters."""

    @pytest.mark.asyncio
    async def test_delete_existing_returns_true_then_gone(self, db: Neo4jConnection) -> None:
        await db.create_rule(_make_rule("DEL-RULE-001"))
        assert await db.delete_rule("DEL-RULE-001") is True
        # Second delete finds nothing -> False, proving the first actually removed it.
        assert await db.delete_rule("DEL-RULE-001") is False

    @pytest.mark.asyncio
    async def test_delete_missing_returns_false(self, db: Neo4jConnection) -> None:
        assert await db.delete_rule("NOPE-RULE-999") is False


class TestRedundancyDetection:
    """Near-identical rule content detection."""

    @pytest.mark.asyncio
    async def test_near_identical_flagged(self, db: Neo4jConnection, checker: IntegrityChecker) -> None:
        pytest.importorskip(
            "sentence_transformers",
            reason="real redundancy embedding needs the [fallback] extra; the mocked-absence tests below still run",
        )
        await db.create_rule(_make_rule(
            "DUP-A-001",
            trigger="Controller must not contain SQL queries directly",
            statement="All data access must go through repository layer",
        ))
        await db.create_rule(_make_rule(
            "DUP-B-001",
            trigger="Controller must not contain SQL queries directly",
            statement="All data access must go through repository layer",
        ))

        redundant = await checker.detect_redundant()
        assert len(redundant) >= 1
        pair = redundant[0]
        ids = {pair["rule_a"], pair["rule_b"]}
        assert ids == {"DUP-A-001", "DUP-B-001"}

    @pytest.mark.asyncio
    async def test_different_rules_clean(self, db: Neo4jConnection, checker: IntegrityChecker) -> None:
        pytest.importorskip(
            "sentence_transformers",
            reason="real redundancy embedding needs the [fallback] extra; the mocked-absence tests below still run",
        )
        await db.create_rule(_make_rule(
            "DIFF-A-001",
            trigger="SQL query uses positional placeholders",
            statement="Use named bind parameters instead of positional",
        ))
        await db.create_rule(_make_rule(
            "DIFF-B-001",
            trigger="Class hierarchy exceeds 2 levels",
            statement="Refactor to use composition via constructor injection",
        ))

        redundant = await checker.detect_redundant()
        assert len(redundant) == 0

    @pytest.mark.asyncio
    async def test_detect_redundant_raises_when_sentence_transformers_missing(
        self,
        db: Neo4jConnection,
        checker: IntegrityChecker,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """detect_redundant() must raise RuntimeError when
        sentence_transformers cannot be imported, not silently return [].

        Same bug class as the silent ONNX fallback fixed in commit
        dae679a: an empty list when the dependency is missing is
        wire-format-identical to "no redundancies found", and the
        caller (`writ validate`) cannot distinguish the two cases.
        The new contract surfaces the missing-dependency state
        explicitly via a RuntimeError that names the [fallback] install
        command and the skip_redundancy=True opt-out for callers that
        intentionally exclude this check.
        """
        import sys

        # Need at least 2 rules so detect_redundant does not early-return
        # on len(rules) < 2 before reaching the sentence_transformers
        # import.
        await db.create_rule(_make_rule("RULE-A-001"))
        await db.create_rule(_make_rule("RULE-B-001"))

        monkeypatch.setitem(sys.modules, "sentence_transformers", None)

        with pytest.raises(RuntimeError) as excinfo:
            await checker.detect_redundant()

        msg = str(excinfo.value)
        assert "sentence" in msg.lower(), (
            f"RuntimeError must name sentence-transformers; got: {msg!r}"
        )
        assert "fallback" in msg, (
            f"RuntimeError must name the [fallback] extras group; got: {msg!r}"
        )
        assert "pip install" in msg, (
            f"RuntimeError must name the pip install verb; got: {msg!r}"
        )
        assert "skip_redundancy" in msg, (
            f"RuntimeError must name the skip_redundancy=True opt-out so "
            f"callers reading the error see the supported intentional-"
            f"exclusion path; got: {msg!r}"
        )


class TestRunAllChecks:
    """Orchestrator behavior."""

    @pytest.mark.asyncio
    async def test_clean_graph_returns_zero(self, db: Neo4jConnection, checker: IntegrityChecker) -> None:
        await db.create_rule(_make_rule("RULE-A-001"))
        await db.create_rule(_make_rule("RULE-B-001"))
        await db.create_edge("RELATED_TO", "RULE-A-001", "RULE-B-001")

        findings = await checker.run_all_checks(skip_redundancy=True)
        assert findings["exit_code"] == 0

    @pytest.mark.asyncio
    async def test_any_finding_returns_nonzero(self, db: Neo4jConnection, checker: IntegrityChecker) -> None:
        await db.create_rule(_make_rule("RULE-A-001"))
        await db.create_rule(_make_rule("RULE-B-001"))
        await db.create_edge("CONFLICTS_WITH", "RULE-A-001", "RULE-B-001")

        findings = await checker.run_all_checks(skip_redundancy=True)
        assert findings["exit_code"] == 1
        assert len(findings["conflicts"]) == 1

    @pytest.mark.asyncio
    async def test_run_all_checks_sets_redundancy_unavailable_when_library_missing(
        self,
        db: Neo4jConnection,
        checker: IntegrityChecker,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """run_all_checks(skip_redundancy=False) must catch the
        RuntimeError from detect_redundant() and surface the missing-
        dependency state via findings['redundancy_unavailable'] rather
        than killing the entire integrity scan.

        Degrade-loud-but-continue: one of five checks losing its
        dependency does not stop the other four (conflicts, orphans,
        stale, confidence-defaults) from reporting. The redundancy_
        unavailable state is informational; it does not by itself
        drive exit_code (exit_code reflects "we ran a check and found
        problems", not "we could not run a check").
        """
        import sys

        await db.create_rule(_make_rule("RULE-A-001"))
        await db.create_rule(_make_rule("RULE-B-001"))
        await db.create_edge("CONFLICTS_WITH", "RULE-A-001", "RULE-B-001")

        monkeypatch.setitem(sys.modules, "sentence_transformers", None)

        findings = await checker.run_all_checks(skip_redundancy=False)

        # Redundancy was attempted but the library was missing.
        assert "redundancy_unavailable" in findings, (
            "run_all_checks must surface the missing-dep state via "
            "the redundancy_unavailable key; got keys: "
            f"{sorted(findings.keys())}"
        )
        assert findings["redundancy_unavailable"], (
            "redundancy_unavailable must contain a non-empty message "
            "so the caller can print an actionable line; got: "
            f"{findings['redundancy_unavailable']!r}"
        )
        assert findings["redundant"] == [], (
            "redundant must remain an empty list when the check could "
            "not run; got: {findings['redundant']!r}"
        )

        # The other checks still ran. The crafted conflict surfaces.
        assert len(findings["conflicts"]) == 1, (
            f"conflicts check must still run; got: {findings['conflicts']!r}"
        )

        # Exit code is non-zero because of the real conflict, not
        # because redundancy was unavailable. The redundancy_unavailable
        # state is informational; it must not by itself drive exit_code.
        assert findings["exit_code"] == 1


# ---------------------------------------------------------------------------
# T0.10/0.11 -- Phase 0 additions: parity violations + category reachability
# ---------------------------------------------------------------------------


class TestParityViolations:
    """detect_parity_violations compares graph nodes against bible markdown files.

    A node is a "parity violation" when it exists in the graph but does not
    appear in any *.md file under the given bible_dir. These tests are RED
    until IntegrityChecker gains detect_parity_violations(bible_dir).
    """

    @pytest.mark.asyncio
    async def test_graph_only_node_is_flagged(self, tmp_path: "pathlib.Path") -> None:
        """A node present in the graph but absent from every bible *.md is returned.

        Setup: checker whose get_all_nodes is stubbed to return one node
        (ORPHAN-001) and a tmp bible_dir that contains no file mentioning
        ORPHAN-001.  detect_parity_violations must include ORPHAN-001 in
        the result list.
        """
        from unittest.mock import AsyncMock, patch

        checker = IntegrityChecker(None, None)

        graph_nodes = [{"type": "Rule", "id": "ORPHAN-001"}]

        # bible_dir is empty -- no markdown files mention ORPHAN-001
        bible_dir = tmp_path / "bible"
        bible_dir.mkdir()

        with patch.object(checker, "get_all_nodes", AsyncMock(return_value=graph_nodes)):
            violations = await checker.detect_parity_violations(bible_dir)

        ids = [v["id"] for v in violations]
        assert "ORPHAN-001" in ids, (
            f"graph-only node ORPHAN-001 must appear in parity violations; got {ids!r}"
        )

    @pytest.mark.asyncio
    async def test_present_node_not_returned(self, tmp_path: "pathlib.Path") -> None:
        """A node that appears in at least one bible *.md is NOT a parity violation."""
        from unittest.mock import AsyncMock, patch

        checker = IntegrityChecker(None, None)

        graph_nodes = [{"type": "Rule", "id": "PRESENT-001"}]

        bible_dir = tmp_path / "bible"
        bible_dir.mkdir()
        # Write a markdown file that mentions PRESENT-001
        (bible_dir / "rules.md").write_text("rule_id: PRESENT-001\nstatement: ok\n")

        with patch.object(checker, "get_all_nodes", AsyncMock(return_value=graph_nodes)):
            violations = await checker.detect_parity_violations(bible_dir)

        ids = [v["id"] for v in violations]
        assert "PRESENT-001" not in ids, (
            f"node present in bible must NOT appear in violations; got {ids!r}"
        )

    @pytest.mark.asyncio
    async def test_all_nodes_in_markdown_returns_empty(self, tmp_path: "pathlib.Path") -> None:
        """When every graph node appears in at least one *.md, result is empty."""
        from unittest.mock import AsyncMock, patch

        checker = IntegrityChecker(None, None)

        graph_nodes = [
            {"type": "Rule", "id": "RULE-A-001"},
            {"type": "Skill", "id": "SKL-PROC-BRAIN-001"},
        ]

        bible_dir = tmp_path / "bible"
        bible_dir.mkdir()
        (bible_dir / "rules.md").write_text(
            "rule_id: RULE-A-001\nstatement: ok\n\nskill_id: SKL-PROC-BRAIN-001\n"
        )

        with patch.object(checker, "get_all_nodes", AsyncMock(return_value=graph_nodes)):
            violations = await checker.detect_parity_violations(bible_dir)

        assert violations == [], (
            f"all nodes present in bible must yield empty violations; got {violations!r}"
        )


class TestCategoryReachability:
    """detect_category_reachability checks that every node has a BELONGS_TO category edge.

    These tests are RED until IntegrityChecker gains detect_category_reachability().
    """

    @pytest.mark.asyncio
    async def test_skipped_when_no_categories(self) -> None:
        """When no Category nodes exist the check is skipped with an explanatory reason.

        The return value must be a dict with skipped=True and a non-empty 'reason'
        string so the caller (writ validate) can print an informational line rather
        than a false alarm.
        """
        from unittest.mock import AsyncMock, patch

        checker = IntegrityChecker(None, None)

        # Stub: graph has nodes but zero categories
        with patch.object(checker, "get_all_nodes", AsyncMock(return_value=[
            {"type": "Rule", "id": "RULE-A-001"},
        ])):
            with patch.object(checker, "get_category_count", AsyncMock(return_value=0)):
                result = await checker.detect_category_reachability()

        assert result.get("skipped") is True, (
            f"skipped must be True when no categories exist; got {result!r}"
        )
        assert result.get("reason"), (
            f"reason must be a non-empty string; got {result!r}"
        )

    @pytest.mark.asyncio
    async def test_node_without_belongs_to_flagged(self) -> None:
        """A non-Category node lacking any BELONGS_TO edge is in nodes_without_category.

        When at least one Category node exists the check runs. A node that has
        no BELONGS_TO edge to any category must appear in the
        'nodes_without_category' list and skipped must be False.
        """
        from unittest.mock import AsyncMock, patch

        checker = IntegrityChecker(None, None)

        all_nodes = [{"type": "Rule", "id": "RULE-UNASSIGNED-001"}]
        # Simulate: this node has no BELONGS_TO edges
        nodes_without_category = [{"type": "Rule", "id": "RULE-UNASSIGNED-001"}]

        with patch.object(checker, "get_category_count", AsyncMock(return_value=3)):
            with patch.object(
                checker,
                "_get_nodes_without_belongs_to",
                AsyncMock(return_value=nodes_without_category),
            ):
                result = await checker.detect_category_reachability()

        assert result.get("skipped") is False, (
            f"skipped must be False when categories exist; got {result!r}"
        )
        flagged_ids = [n["id"] for n in result.get("nodes_without_category", [])]
        assert "RULE-UNASSIGNED-001" in flagged_ids, (
            f"node without BELONGS_TO must be in nodes_without_category; got {flagged_ids!r}"
        )


class TestRunAllChecksPhase0:
    """Phase 0 additions: parity and reachability wire into run_all_checks exit code.

    These tests are RED until run_all_checks is extended to call
    detect_parity_violations and detect_category_reachability and fold their
    results into exit_code.
    """

    @pytest.mark.asyncio
    async def test_parity_violation_flips_exit_code(self, tmp_path) -> None:
        """Non-empty detect_parity_violations must set exit_code=1; empty keeps 0.

        Uses AsyncMock stubs for all detectors so no Neo4j is needed.

        Cycle 7: this test needs a USABLE oracle dir, and passing bible_dir=None
        would silently stop testing what it means to test. run_all_checks now
        resolves the parity oracle once and, when it is unusable, sets the four
        parity keys to their falsy defaults WITHOUT calling the detectors. Under
        the old bible_dir=None the patched detect_parity_violations would never
        run, parity_violations would be [] no matter what the mock returned, and
        case 1 would assert exit_code 1 against a check that never executed.

        That is the exact failure this cycle exists to fix, one layer up: a
        mocked detector plus an oracle that cannot resolve is indistinguishable
        from a passing check. A tmp dir holding one .md is the smallest thing
        that resolves, so the wiring this test pins actually executes.
        """
        from unittest.mock import AsyncMock, patch

        oracle_dir = tmp_path / "bible"
        oracle_dir.mkdir()
        (oracle_dir / "seed.md").write_text(
            "---\nrule_id: SEED-001\n---\n", encoding="utf-8"
        )

        checker = IntegrityChecker(None, None)

        base_patches = {
            "detect_conflicts": AsyncMock(return_value=[]),
            "detect_orphans": AsyncMock(return_value=[]),
            "detect_stale": AsyncMock(return_value=[]),
            "detect_redundant": AsyncMock(return_value=[]),
            "check_unreviewed_count": AsyncMock(return_value=None),
            "detect_frequency_stale": AsyncMock(return_value=[]),
            "detect_graduation_flags": AsyncMock(return_value=[]),
            "detect_dangling_dispatched_roles": AsyncMock(return_value=[]),
            "detect_orphans_all_labels": AsyncMock(return_value=([], {})),
            "detect_category_reachability": AsyncMock(
                return_value={"skipped": True, "reason": "no categories"}
            ),
        }

        # Case 1: parity violations present -> exit_code 1
        violation_patches = dict(base_patches)
        violation_patches["detect_parity_violations"] = AsyncMock(
            return_value=[{"type": "Rule", "id": "ORPHAN-001"}]
        )
        cms = [patch.object(checker, name, mock) for name, mock in violation_patches.items()]
        for cm in cms:
            cm.start()
        try:
            findings = await checker.run_all_checks(
                skip_redundancy=True, bible_dir=oracle_dir
            )
        finally:
            for cm in cms:
                cm.stop()

        assert findings["exit_code"] == 1, (
            f"non-empty parity violations must set exit_code=1; got {findings['exit_code']}"
        )

        # Case 2: no violations -> exit_code 0
        clean_patches = dict(base_patches)
        clean_patches["detect_parity_violations"] = AsyncMock(return_value=[])
        cms = [patch.object(checker, name, mock) for name, mock in clean_patches.items()]
        for cm in cms:
            cm.start()
        try:
            findings = await checker.run_all_checks(
                skip_redundancy=True, bible_dir=oracle_dir
            )
        finally:
            for cm in cms:
                cm.stop()

        assert findings["exit_code"] == 0, (
            f"empty parity violations must leave exit_code=0; got {findings['exit_code']}"
        )

    @pytest.mark.asyncio
    async def test_reachability_failure_flips_exit_code(self) -> None:
        """Non-empty nodes_without_category must set exit_code=1.

        Uses AsyncMock stubs; the reachability result has skipped=False and a
        non-empty nodes_without_category list.
        """
        from unittest.mock import AsyncMock, patch

        checker = IntegrityChecker(None, None)

        base_patches = {
            "detect_conflicts": AsyncMock(return_value=[]),
            "detect_orphans": AsyncMock(return_value=[]),
            "detect_stale": AsyncMock(return_value=[]),
            "detect_redundant": AsyncMock(return_value=[]),
            "check_unreviewed_count": AsyncMock(return_value=None),
            "detect_frequency_stale": AsyncMock(return_value=[]),
            "detect_graduation_flags": AsyncMock(return_value=[]),
            "detect_dangling_dispatched_roles": AsyncMock(return_value=[]),
            "detect_orphans_all_labels": AsyncMock(return_value=([], {})),
            "detect_parity_violations": AsyncMock(return_value=[]),
            "detect_category_reachability": AsyncMock(
                return_value={
                    "skipped": False,
                    "nodes_without_category": [{"type": "Rule", "id": "RULE-UNCAT-001"}],
                }
            ),
        }

        cms = [patch.object(checker, name, mock) for name, mock in base_patches.items()]
        for cm in cms:
            cm.start()
        try:
            findings = await checker.run_all_checks(
                skip_redundancy=True, bible_dir=None
            )
        finally:
            for cm in cms:
                cm.stop()

        assert findings["exit_code"] == 1, (
            "nodes_without_category non-empty must set exit_code=1; "
            f"got exit_code={findings['exit_code']}"
        )
