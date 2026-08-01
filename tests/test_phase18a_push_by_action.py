"""1.8a: push-by-action substrate -- action_triggers DATA + two invariants.

Push-by-action was BUILT server-side at the 1.6/1.7 cutover (match() does
push = action in node.action_triggers, trigger_index.py:111-114; the endpoint
plumbs `action` through) but is INERT: no node declares action_triggers and no
caller sets `action`. 1.8a fills the corpus/validator half of the pipe:

(1) DATA: 8 methodology nodes declare action_triggers, one per wired action.
    The mapping closes every action-routed Category (a Category whose routes
    include 'action'): CAT-COMM-001 <- REVRECV, CAT-META-001 <- PBK-AUTHOR-001,
    CAT-PROC-001 <- WRIT-FAILURE/WORKTREE/FINISH/VERIFY, CAT-PROC-DISPATCH-001
    <- DISPATCH. (META-AUTH-001/002 are Rules -> action_triggers there is a dead
    tag the index can't push; caught by detect_push_reachability.dead_action_tags.)
(2) detect_push_reachability: the push analog of detect_trigger_keyword_invariant's
    pull-reachability. An action-routed Category with NO methodology member
    declaring action_triggers is an empty_action_route (the route is advertised
    but unreachable by push); a methodology node under an action-routed Category
    with no floor_modes, no trigger_keywords AND no action_triggers is a
    push_orphan. RED on today's committed graph: all 4 action-routed categories
    are empty (no node has action_triggers yet).
(3) detect_action_vocabulary_closure: the action analog of EXPECTED_FLOORS'
    unknown-mode clause. Every action_triggers value must be a member of
    KNOWN_ACTIONS; a typo'd/invented action tags a node the push path can never
    reach (a silent no-op, the 29-stranded-mandatory failure class).
(4) run_all_checks wires both into exit_code; cli renders them.

The seed/mutation tests are the non-vacuity witnesses (a clean variant must also
return None so a checker that trivially returns None cannot pass both halves).
Module-scoped corpus (built once); mutation tests snapshot -> mutate -> restore
so the shared graph stays canonical without a per-test re-ingest.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import pytest_asyncio

from writ.config import get_neo4j_password, get_neo4j_uri, get_neo4j_user
from writ.graph.db import Neo4jConnection
from writ.graph.integrity import IntegrityChecker
from writ.graph.methodology_ingest import ingest_path

BIBLE = Path(__file__).resolve().parent.parent / "bible"

# The action -> node mapping 1.8a authors. One row per (node_id, expected_action).
# Covers all 4 action-routed categories. 'dispatch' is authored here (closing
# CAT-PROC-DISPATCH-001) even though unifying its hook through the companion is
# deferred to 1.8b. 'plan' is deliberately ABSENT (PLAN nodes are always_on-floored
# in every mode -> a plan push is a pure no-op; D-B dropped it).
ACTION_TRIGGER_DATA = [
    ("SKL-PROC-REVRECV-001", "review-feedback"),   # CAT-COMM-001
    ("PBK-AUTHOR-001", "bible-authoring"),          # CAT-META-001 (the Playbook)
    ("SKL-PROC-WRIT-FAILURE-001", "gate-denial"),   # CAT-PROC-001
    ("SKL-PROC-WORKTREE-001", "worktree"),          # CAT-PROC-001
    ("TEC-PROC-WORKTREE-001", "worktree"),          # CAT-PROC-001
    ("PBK-PROC-FINISH-001", "finish"),              # CAT-PROC-001
    ("SKL-PROC-DISPATCH-001", "dispatch"),          # CAT-PROC-DISPATCH-001
]
# SKL-PROC-VERIFY-001's "Stop" trigger was removed: the node is always_on +
# work-floored (already injected every turn), so the Stop re-push was redundant
# and current Claude Code treated a Stop additionalContext emit as a turn-block
# loop. "Stop" dropped from KNOWN_ACTIONS too. Verification stays enforced by the
# PreToolUse TodoWrite gate in writ-verify-before-claim.sh.
# NOTE: META-AUTH-001/002 were initially authored for bible-authoring but are
# RULE nodes -- the trigger index only loads methodology labels, so action_triggers
# on a Rule is a DEAD tag (caught now by detect_push_reachability.dead_action_tags).
# bible-authoring pushes PBK-AUTHOR-001 (the Playbook); the META-AUTH guardrails
# reach the agent via CHANNEL-1 (rules), not the methodology companion.

ACTION_ROUTED_CATEGORIES = [
    "CAT-COMM-001", "CAT-META-001", "CAT-PROC-001", "CAT-PROC-DISPATCH-001",
]

# The vocabulary the live push path can emit (the wired actions). Mirrors the
# KNOWN_ACTIONS constant the invariant will read from integrity.py.
EXPECTED_KNOWN_ACTIONS = {
    "dispatch", "gate-denial", "review-feedback", "worktree",
    "bible-authoring", "finish",
}

_COALESCE = "coalesce(n.skill_id,n.playbook_id,n.technique_id,n.antipattern_id,n.rule_id)"


@pytest_asyncio.fixture(scope="module", loop_scope="module")
async def db_corpus():
    db = Neo4jConnection(get_neo4j_uri(), get_neo4j_user(), get_neo4j_password())
    try:
        async with db._driver.session(database=db._database) as s:
            await (await s.run("RETURN 1 AS ok")).consume()
    except Exception:
        await db.close()
        pytest.skip("Neo4j unreachable")
    if not BIBLE.exists():
        await db.close()
        pytest.skip("requires the untracked bible/ source tree (regenerate with `writ export`)")
    await db.clear_all()
    await ingest_path(BIBLE, db)
    yield db
    # Leave the shared graph canonical for the rest of the suite.
    await db.clear_all()
    await ingest_path(BIBLE, db)
    await db.close()


async def _get_action_triggers(db: Neo4jConnection, node_id: str) -> list[str]:
    async with db._driver.session(database=db._database) as s:
        r = await s.run(
            f"MATCH (n) WHERE {_COALESCE} = $id RETURN n.action_triggers AS a", id=node_id
        )
        rec = await r.single()
        return (rec["a"] or []) if rec else []


async def _snapshot(db: Neo4jConnection, node_id: str, *keys: str) -> dict:
    cols = ", ".join(f"n.{k} AS {k}" for k in keys)
    async with db._driver.session(database=db._database) as s:
        r = await s.run(f"MATCH (n) WHERE {_COALESCE} = $id RETURN {cols}", id=node_id)
        rec = await r.single()
        return {k: rec[k] for k in keys}


async def _restore(db: Neo4jConnection, node_id: str, snap: dict) -> None:
    # SET prop = null removes it in Neo4j, so a None snapshot (absent prop)
    # restores exactly to absent.
    sets = ", ".join(f"n.{k} = ${k}" for k in snap)
    async with db._driver.session(database=db._database) as s:
        await s.run(f"MATCH (n) WHERE {_COALESCE} = $id SET {sets}", id=node_id, **snap)


async def _set_props(db: Neo4jConnection, node_id: str, **props) -> None:
    sets = ", ".join(f"n.{k} = ${k}" for k in props)
    async with db._driver.session(database=db._database) as s:
        await s.run(f"MATCH (n) WHERE {_COALESCE} = $id SET {sets}", id=node_id, **props)


async def _snapshot_category_action_triggers(db: Neo4jConnection, cat_id: str) -> list[tuple[str, object]]:
    async with db._driver.session(database=db._database) as s:
        r = await s.run(
            f"MATCH (n)-[:BELONGS_TO]->(c:Category {{category_id:$cat}}) "
            f"RETURN {_COALESCE} AS id, n.action_triggers AS a",
            cat=cat_id,
        )
        return [(rec["id"], rec["a"]) async for rec in r]


async def _clear_category_action_triggers(db: Neo4jConnection, cat_id: str) -> None:
    async with db._driver.session(database=db._database) as s:
        await s.run(
            "MATCH (n)-[:BELONGS_TO]->(c:Category {category_id:$cat}) SET n.action_triggers = []",
            cat=cat_id,
        )


async def _restore_category_action_triggers(
    db: Neo4jConnection, snap: list[tuple[str, object]]
) -> None:
    async with db._driver.session(database=db._database) as s:
        for nid, val in snap:
            await s.run(
                f"MATCH (n) WHERE {_COALESCE} = $id SET n.action_triggers = $val",
                id=nid, val=val,
            )


# --------------------------------------------------------------------------- #
# (1) DATA: the 8 target methodology nodes declare action_triggers.            #
# --------------------------------------------------------------------------- #
class TestActionTriggerData:
    pytestmark = pytest.mark.asyncio(loop_scope="module")

    async def test_target_nodes_declare_action_triggers(self, db_corpus: Neo4jConnection) -> None:
        wrong = {}
        for node_id, action in ACTION_TRIGGER_DATA:
            got = await _get_action_triggers(db_corpus, node_id)
            if got != [action]:
                wrong[node_id] = (got, [action])
        assert not wrong, f"action_triggers mismatches (got, expected): {wrong}"


# --------------------------------------------------------------------------- #
# (2) detect_push_reachability.                                                #
# --------------------------------------------------------------------------- #
class TestPushReachability:
    pytestmark = pytest.mark.asyncio(loop_scope="module")

    async def test_corpus_clean(self, db_corpus: Neo4jConnection) -> None:
        # After authoring, every action-routed category has >=1 member with
        # action_triggers and no methodology node is a push_orphan.
        checker = IntegrityChecker(db_corpus._driver, db_corpus._database)
        result = await checker.detect_push_reachability()
        assert result is None, f"push-reachability violations: {result}"

    async def test_detects_empty_action_route(self, db_corpus: Neo4jConnection) -> None:
        # Non-vacuity (empty route): strip action_triggers off every member of an
        # action-routed category -> the route is advertised but unreachable.
        snap = await _snapshot_category_action_triggers(db_corpus, "CAT-META-001")
        try:
            await _clear_category_action_triggers(db_corpus, "CAT-META-001")
            checker = IntegrityChecker(db_corpus._driver, db_corpus._database)
            result = await checker.detect_push_reachability()
            assert result is not None
            assert "CAT-META-001" in result["empty_action_routes"]
        finally:
            await _restore_category_action_triggers(db_corpus, snap)

    async def test_detects_push_orphan(self, db_corpus: Neo4jConnection) -> None:
        # Non-vacuity (orphan): SKL-PROC-REVRECV-001 is under CAT-COMM-001, which
        # routes [always_on, action] -- action but NOT pull, so pull_orphan cannot
        # cover it. Stripped of all three surfaces it is unreachable by the
        # companion: push_reachability must catch it.
        snap = await _snapshot(
            db_corpus, "SKL-PROC-REVRECV-001",
            "floor_modes", "trigger_keywords", "action_triggers",
        )
        try:
            await _set_props(
                db_corpus, "SKL-PROC-REVRECV-001",
                floor_modes=[], trigger_keywords=[], action_triggers=[],
            )
            checker = IntegrityChecker(db_corpus._driver, db_corpus._database)
            result = await checker.detect_push_reachability()
            assert result is not None
            assert "SKL-PROC-REVRECV-001" in result["push_orphans"]
        finally:
            await _restore(db_corpus, "SKL-PROC-REVRECV-001", snap)

    async def test_detects_dead_action_tag(self, db_corpus: Neo4jConnection) -> None:
        # Non-vacuity (dead tag): action_triggers on a Rule (a label the trigger
        # index never loads) can never push. ENF-META-CONCISE-001 is a Rule under
        # CAT-META-001; tagging it must be flagged as a dead tag.
        snap = await _snapshot(db_corpus, "ENF-META-CONCISE-001", "action_triggers")
        try:
            await _set_props(db_corpus, "ENF-META-CONCISE-001", action_triggers=["bible-authoring"])
            checker = IntegrityChecker(db_corpus._driver, db_corpus._database)
            result = await checker.detect_push_reachability()
            assert result is not None
            assert "ENF-META-CONCISE-001" in result["dead_action_tags"]
        finally:
            await _restore(db_corpus, "ENF-META-CONCISE-001", snap)


# --------------------------------------------------------------------------- #
# (3) detect_action_vocabulary_closure.                                        #
# --------------------------------------------------------------------------- #
class TestActionVocabularyClosure:
    pytestmark = pytest.mark.asyncio(loop_scope="module")

    async def test_known_actions_constant(self) -> None:
        # The vocabulary is the wired action set; lock it so a drop/typo is loud.
        from writ.graph.integrity import KNOWN_ACTIONS

        assert set(KNOWN_ACTIONS) == EXPECTED_KNOWN_ACTIONS

    async def test_corpus_clean(self, db_corpus: Neo4jConnection) -> None:
        # Every authored action_triggers value is in KNOWN_ACTIONS.
        checker = IntegrityChecker(db_corpus._driver, db_corpus._database)
        result = await checker.detect_action_vocabulary_closure()
        assert result is None, f"unknown actions: {result}"

    async def test_detects_unknown_action(self, db_corpus: Neo4jConnection) -> None:
        # Non-vacuity: an invented action is a tag the push path can never emit.
        snap = await _snapshot(db_corpus, "SKL-PROC-WORKTREE-001", "action_triggers")
        try:
            await _set_props(db_corpus, "SKL-PROC-WORKTREE-001", action_triggers=["bogus-action"])
            checker = IntegrityChecker(db_corpus._driver, db_corpus._database)
            result = await checker.detect_action_vocabulary_closure()
            assert result is not None
            assert result.get("SKL-PROC-WORKTREE-001") == ["bogus-action"]
        finally:
            await _restore(db_corpus, "SKL-PROC-WORKTREE-001", snap)

    async def test_known_action_passes(self, db_corpus: Neo4jConnection) -> None:
        # Paired clean: an in-vocabulary action does NOT trip the check (proves the
        # detector is not trivially returning a finding).
        snap = await _snapshot(db_corpus, "SKL-PROC-WORKTREE-001", "action_triggers")
        try:
            await _set_props(db_corpus, "SKL-PROC-WORKTREE-001", action_triggers=["gate-denial"])
            checker = IntegrityChecker(db_corpus._driver, db_corpus._database)
            result = await checker.detect_action_vocabulary_closure()
            assert result is None
        finally:
            await _restore(db_corpus, "SKL-PROC-WORKTREE-001", snap)


# --------------------------------------------------------------------------- #
# (4) run_all_checks wiring -> exit_code.                                       #
# --------------------------------------------------------------------------- #
class TestRunAllChecksWiresPushByAction:
    pytestmark = pytest.mark.asyncio(loop_scope="module")

    async def test_findings_keys_present_and_clean(self, db_corpus: Neo4jConnection) -> None:
        checker = IntegrityChecker(db_corpus._driver, db_corpus._database)
        findings = await checker.run_all_checks(skip_redundancy=True)
        assert "push_reachability" in findings
        assert "action_vocabulary" in findings
        assert findings["push_reachability"] is None
        assert findings["action_vocabulary"] is None
        assert findings["exit_code"] == 0

    async def test_empty_action_route_fails_validate(self, db_corpus: Neo4jConnection) -> None:
        snap = await _snapshot_category_action_triggers(db_corpus, "CAT-META-001")
        try:
            await _clear_category_action_triggers(db_corpus, "CAT-META-001")
            checker = IntegrityChecker(db_corpus._driver, db_corpus._database)
            findings = await checker.run_all_checks(skip_redundancy=True)
            assert findings["push_reachability"] is not None
            assert findings["exit_code"] == 1
        finally:
            await _restore_category_action_triggers(db_corpus, snap)

    async def test_unknown_action_fails_validate(self, db_corpus: Neo4jConnection) -> None:
        snap = await _snapshot(db_corpus, "SKL-PROC-WORKTREE-001", "action_triggers")
        try:
            await _set_props(db_corpus, "SKL-PROC-WORKTREE-001", action_triggers=["bogus-action"])
            checker = IntegrityChecker(db_corpus._driver, db_corpus._database)
            findings = await checker.run_all_checks(skip_redundancy=True)
            assert findings["action_vocabulary"] is not None
            assert findings["exit_code"] == 1
        finally:
            await _restore(db_corpus, "SKL-PROC-WORKTREE-001", snap)
