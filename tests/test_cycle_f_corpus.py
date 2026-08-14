"""Cycle F test skeletons: four new corpus nodes, one widened forbidden-
phrase list, and one corpus correction to a rule that overstated its own
enforcement.

Covers plan.md's "Cycle F (absorbed techniques + one corpus correction)"
analysis (F1-F6) and the cycle F lines in `## Capabilities`. Everything below
is RED until cycle F's corpus and code changes land:

  F1  `ENF-PROC-FIXLOOP-001` (Rule) does not exist yet: the five-round
      fix-loop cap, rounds 1-3 resume / rounds 4-5 fresh-implementer model
      escalation, three named adjudication dispositions. TEACHES-wired FROM
      `PBK-PROC-SDD-001` (mirroring the TEACHES it already carries to
      `ENF-PROC-SDD-001`), which also gains a body section naming the cap.
      Authored as a Rule specifically so it lands inside the Rule-to-Rule
      graph walk cycle E established (a Technique reaches the model only via
      pull keywords and an INVOKES edge).
  F2  `TEC-PROC-CONDITION-WAIT-001` (Technique) does not exist yet:
      condition-polling in place of a fixed sleep/retry. INVOKES-wired FROM
      `PBK-PROC-DIAGNOSE-HEISENBUG-001` and
      `PBK-PROC-DIAGNOSE-FAILING-TEST-001`, both of which forbid sleep/retry
      today with `edges: []` and no replacement pointer (confirmed on disk
      2026-08-13).
  F3  `TEC-PROC-DEFENSE-DEPTH-001` (Technique) does not exist yet: four
      validation layers applied AFTER a confirmed root cause. INVOKES-wired
      FROM `PBK-PROC-DEBUG-001` phase 4.
  F4  `TEC-PROC-TEST-POLLUTION-001` (Technique) does not exist yet: the
      bisection procedure for a full-suite-only failure. INVOKES-wired FROM
      `PBK-PROC-DIAGNOSE-HEISENBUG-001` ONLY -- the plan's own edges block
      for this node names that one playbook, not the failing-test playbook.
      `scripts/find-test-polluter.sh`, its driver script, does not exist
      either. This file pins its existence, executable bit, `bash -n`
      syntax, and `--victim`/`--artifact` usage/interface only -- it never
      invokes an actual bisection (that needs a real suite run and a
      deliberately planted polluter; the plan marks that operational).
  F5  `FRB-COMMS-001.forbidden_phrases` does not yet carry "Let me implement
      that now" (confirmed on disk: exactly the original six phrases).
  F6  `ENF-PROC-PLAN-001.enforcement` still claims a "Haiku judge" for Gate 5
      Tier 2 today (confirmed on disk, and expected live in the graph too --
      this node predates cycle F and the 7688 graph mirrors the
      post-cycle-E corpus, so it was never touched by any prior cycle). The
      same false claim is also live in `writ/session/cache.py`'s
      `_default_cache` (a plain source comment) and in the docstring of
      `writ/server/routes/session_state.py`'s `session_quality_judgment_set`
      (confirmed on disk 2026-08-13). The ONE Haiku mention the plan keeps
      on purpose lives in that same function's BODY (the out-of-process-
      judge latency note), not its docstring -- which is exactly why the
      two "must lose Haiku" pins below scope to the docstring / a
      Haiku-free function, never the whole file (a whole-file grep would
      false-positive on the surviving mention).

All corpus-data assertions run against the live 7688 test graph (read-only:
no `clear_all`, no `create_methodology_node`, no `create_edge` anywhere in
this file), mirroring `tests/test_dispatch_prose_parity.py` and
`tests/test_phase19_route_delivery_closure.py`'s `db_corpus` fixture. The
one exception is `TestF6ClaimVersusCodePin`, which reproduces plan.md's own
prescribed durable form for the claim-vs-code pin verbatim: a test that
reads the bible/ source file and the hook script directly, deliberately NOT
a graph read, because (per the plan's own reasoning) a wording pin on that
sentence would rot the first time someone improves it, so the durable form
pins the IMPLICATION instead. Guarded with `requires_bible` since bible/ is
an untracked, locally-regenerated export of the graph.

`floor_modes` note: `ENF-PROC-FIXLOOP-001` is a Rule, and Rule has no
`floor_modes` field at all (`_FLOOR_NODE_LABELS` in
writ/graph/integrity/_common.py excludes Rule; the Pydantic `Rule` class in
writ/graph/schema.py declares no such field) -- so "no floor" for F1 is
asserted as "not mandatory" / "no mechanical_enforcement_path" metadata, not
a `floor_modes` list. The three Techniques (F2-F4) DO carry `floor_modes`,
and the plan declares it on none of them.

SAFETY: every fixture here binds to bolt://localhost:7688 (forced by
tests/conftest.py's `apply_isolation_env` before any test module imports),
never to production (7687), and never touches session state.
"""

from __future__ import annotations

import inspect
import os
import re
import subprocess
from pathlib import Path

import pytest
import pytest_asyncio
import yaml

from tests._bible_guard import requires_bible
from writ.config import get_neo4j_password, get_neo4j_uri, get_neo4j_user
from writ.graph.db import Neo4jConnection

REPO_ROOT = Path(__file__).resolve().parent.parent
METHODOLOGY_DIR = REPO_ROOT / "bible" / "methodology"
HOOKS_DIR = REPO_ROOT / "hooks" / "scripts"
POLLUTER_SCRIPT = REPO_ROOT / "scripts" / "find-test-polluter.sh"

NEO4J_URI = get_neo4j_uri()
NEO4J_USER = get_neo4j_user()
NEO4J_PASSWORD = get_neo4j_password()


# ---------------------------------------------------------------------------
# Read-only live-corpus fixture -- never wipes, never mutates. Mirrors
# tests/test_dispatch_prose_parity.py's `db_corpus` fixture exactly.
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


def _neighbor(node: dict, target_id: str, edge_type: str, direction: str) -> bool:
    """True iff `node["neighbors"]` (from get_node_with_neighbors) contains a
    row matching (target_id, edge_type, direction) exactly. `direction` is
    'out' when the queried node is the edge's start, 'in' when it is the
    end -- per get_node_with_neighbors' own CASE expression."""
    return any(
        n["id"] == target_id and n["edge_type"] == edge_type and n["direction"] == direction
        for n in node["neighbors"]
    )


def _parse_frontmatter(path: Path) -> dict:
    """Extract and parse YAML frontmatter from a Markdown file. Local copy,
    matching the pattern every other test file in this suite that needs one
    defines for itself (e.g. tests/test_methodology_migration.py)."""
    text = path.read_text(encoding="utf-8")
    match = re.match(r"^---\n(.*?)\n---", text, re.DOTALL)
    if not match:
        return {}
    return yaml.safe_load(match.group(1)) or {}


# ---------------------------------------------------------------------------
# F1: ENF-PROC-FIXLOOP-001 -- the five-round fix-loop cap, authored as a
# Rule so it lands inside the Rule-to-Rule graph walk.
# ---------------------------------------------------------------------------


class TestF1FixLoopRuleNode:
    """The new node itself: label, non-empty prose, `mandatory`/mechanical-
    enforcement-path metadata, trigger_keywords, and its own declared
    edges."""

    @pytest.mark.asyncio
    async def test_node_exists_as_a_rule_with_non_empty_trigger_and_statement(
        self, db_corpus: Neo4jConnection
    ) -> None:
        node = await db_corpus.get_node_with_neighbors("ENF-PROC-FIXLOOP-001")
        assert node is not None, "ENF-PROC-FIXLOOP-001 does not exist in the graph yet"
        assert node["type"] == "Rule"
        props = node["props"]
        assert props.get("trigger", "").strip()
        assert props.get("statement", "").strip()

    @pytest.mark.asyncio
    async def test_node_is_not_mandatory_and_declares_no_mechanical_enforcement_path(
        self, db_corpus: Neo4jConnection
    ) -> None:
        # Per the plan: no hook can count fix-loop rounds, so claiming a
        # mechanical enforcement path here would be F6's defect committed
        # in a brand-new node.
        node = await db_corpus.get_node_with_neighbors("ENF-PROC-FIXLOOP-001")
        assert node is not None
        props = node["props"]
        assert props.get("mandatory") is False
        assert not props.get("mechanical_enforcement_path")

    @pytest.mark.asyncio
    async def test_trigger_keywords_from_the_plan_are_present(
        self, db_corpus: Neo4jConnection
    ) -> None:
        node = await db_corpus.get_node_with_neighbors("ENF-PROC-FIXLOOP-001")
        assert node is not None
        keywords = set(node["props"].get("trigger_keywords") or [])
        expected = {"fix loop", "re-review", "review findings", "escalate", "adjudicate"}
        assert expected <= keywords, f"missing {expected - keywords}"

    @pytest.mark.asyncio
    async def test_states_the_five_round_cap_and_the_three_dispositions(
        self, db_corpus: Neo4jConnection
    ) -> None:
        node = await db_corpus.get_node_with_neighbors("ENF-PROC-FIXLOOP-001")
        assert node is not None
        prose = f"{node['props'].get('statement', '')} {node['props'].get('body', '')}".lower()
        assert re.search(r"five.?round|5.?round", prose), "no five-round cap language found"
        assert "1-3" in prose, "no rounds 1-3 resume language found"
        assert "4-5" in prose, "no rounds 4-5 escalation language found"
        assert "contestable" in prose
        assert "load-bearing" in prose

    @pytest.mark.asyncio
    async def test_distinguishes_itself_from_the_three_fix_rule(
        self, db_corpus: Neo4jConnection
    ) -> None:
        node = await db_corpus.get_node_with_neighbors("ENF-PROC-FIXLOOP-001")
        assert node is not None
        prose = f"{node['props'].get('statement', '')} {node['props'].get('body', '')}"
        assert "ANT-PROC-DEBUG-001" in prose

    @pytest.mark.asyncio
    async def test_declared_edges_resolve_and_match_the_plan(
        self, db_corpus: Neo4jConnection
    ) -> None:
        node = await db_corpus.get_node_with_neighbors("ENF-PROC-FIXLOOP-001")
        assert node is not None
        assert _neighbor(node, "PBK-PROC-SDD-001", "GATES", "out")
        assert _neighbor(node, "ENF-PROC-SDD-001", "RELATED_TO", "out")
        assert _neighbor(node, "ANT-PROC-DEBUG-001", "RELATED_TO", "out")


class TestF1SddPlaybookTeachesFixLoop:
    """`PBK-PROC-SDD-001` gains `{ target: ENF-PROC-FIXLOOP-001, type:
    TEACHES }` in its own edges (mirroring the TEACHES it already has to
    `ENF-PROC-SDD-001`), plus a `## The fix loop` body section citing the
    rule and naming the cap."""

    @pytest.mark.asyncio
    async def test_sdd_playbook_teaches_the_fixloop_rule(
        self, db_corpus: Neo4jConnection
    ) -> None:
        node = await db_corpus.get_node_with_neighbors("PBK-PROC-SDD-001")
        assert node is not None
        assert _neighbor(node, "ENF-PROC-FIXLOOP-001", "TEACHES", "out")

    @pytest.mark.asyncio
    async def test_sdd_playbook_body_names_the_five_round_cap(
        self, db_corpus: Neo4jConnection
    ) -> None:
        node = await db_corpus.get_node_with_neighbors("PBK-PROC-SDD-001")
        assert node is not None
        body = node["props"].get("body", "")
        assert "ENF-PROC-FIXLOOP-001" in body
        assert re.search(r"five.?round|5.?round", body.lower())


# ---------------------------------------------------------------------------
# F2: TEC-PROC-CONDITION-WAIT-001 -- condition-based waiting replaces the
# sleep/retry both diagnose playbooks already forbid with no replacement.
# ---------------------------------------------------------------------------


class TestF2ConditionWaitTechniqueNode:
    @pytest.mark.asyncio
    async def test_node_exists_as_a_technique_with_non_empty_trigger_and_statement(
        self, db_corpus: Neo4jConnection
    ) -> None:
        node = await db_corpus.get_node_with_neighbors("TEC-PROC-CONDITION-WAIT-001")
        assert node is not None, "TEC-PROC-CONDITION-WAIT-001 does not exist in the graph yet"
        assert node["type"] == "Technique"
        props = node["props"]
        assert props.get("trigger", "").strip()
        assert props.get("statement", "").strip()

    @pytest.mark.asyncio
    async def test_floor_modes_is_empty(self, db_corpus: Neo4jConnection) -> None:
        node = await db_corpus.get_node_with_neighbors("TEC-PROC-CONDITION-WAIT-001")
        assert node is not None
        assert not node["props"].get("floor_modes")

    @pytest.mark.asyncio
    async def test_trigger_keywords_from_the_plan_are_present(
        self, db_corpus: Neo4jConnection
    ) -> None:
        node = await db_corpus.get_node_with_neighbors("TEC-PROC-CONDITION-WAIT-001")
        assert node is not None
        keywords = set(node["props"].get("trigger_keywords") or [])
        expected = {"flaky", "sleep", "retry", "timeout", "polling", "condition"}
        assert expected <= keywords, f"missing {expected - keywords}"

    @pytest.mark.asyncio
    async def test_demonstrates_both_diagnose_playbooks(
        self, db_corpus: Neo4jConnection
    ) -> None:
        node = await db_corpus.get_node_with_neighbors("TEC-PROC-CONDITION-WAIT-001")
        assert node is not None
        assert _neighbor(node, "PBK-PROC-DIAGNOSE-HEISENBUG-001", "DEMONSTRATES", "out")
        assert _neighbor(node, "PBK-PROC-DIAGNOSE-FAILING-TEST-001", "DEMONSTRATES", "out")


class TestF2DiagnosePlaybooksInvokeConditionWait:
    """Both playbooks currently forbid sleep/retry with `edges: []` and no
    replacement pointer (confirmed on disk 2026-08-13: `bible/methodology/
    PBK-PROC-DIAGNOSE-HEISENBUG-001.md` and `...-FAILING-TEST-001.md` both
    carry `edges: []`). Each must gain an INVOKES edge to the new technique
    AND name it by id in its own prose -- the same spirit as cycle E's
    dispatch-prose-parity check (a graph edge with no matching prose is a
    rendering the model reading the corpus never sees)."""

    @pytest.mark.asyncio
    async def test_heisenbug_playbook_invokes_condition_wait(
        self, db_corpus: Neo4jConnection
    ) -> None:
        node = await db_corpus.get_node_with_neighbors("PBK-PROC-DIAGNOSE-HEISENBUG-001")
        assert node is not None
        assert _neighbor(node, "TEC-PROC-CONDITION-WAIT-001", "INVOKES", "out")
        prose = f"{node['props'].get('statement', '')} {node['props'].get('body', '')}"
        assert "TEC-PROC-CONDITION-WAIT-001" in prose

    @pytest.mark.asyncio
    async def test_failing_test_playbook_invokes_condition_wait(
        self, db_corpus: Neo4jConnection
    ) -> None:
        node = await db_corpus.get_node_with_neighbors("PBK-PROC-DIAGNOSE-FAILING-TEST-001")
        assert node is not None
        assert _neighbor(node, "TEC-PROC-CONDITION-WAIT-001", "INVOKES", "out")
        prose = f"{node['props'].get('statement', '')} {node['props'].get('body', '')}"
        assert "TEC-PROC-CONDITION-WAIT-001" in prose


# ---------------------------------------------------------------------------
# F3: TEC-PROC-DEFENSE-DEPTH-001 -- four validation layers after a confirmed
# root cause, wired to PBK-PROC-DEBUG-001 phase 4.
# ---------------------------------------------------------------------------


class TestF3DefenseDepthTechniqueNode:
    @pytest.mark.asyncio
    async def test_node_exists_as_a_technique_with_non_empty_trigger_and_statement(
        self, db_corpus: Neo4jConnection
    ) -> None:
        node = await db_corpus.get_node_with_neighbors("TEC-PROC-DEFENSE-DEPTH-001")
        assert node is not None, "TEC-PROC-DEFENSE-DEPTH-001 does not exist in the graph yet"
        assert node["type"] == "Technique"
        props = node["props"]
        assert props.get("trigger", "").strip()
        assert props.get("statement", "").strip()

    @pytest.mark.asyncio
    async def test_floor_modes_is_empty(self, db_corpus: Neo4jConnection) -> None:
        node = await db_corpus.get_node_with_neighbors("TEC-PROC-DEFENSE-DEPTH-001")
        assert node is not None
        assert not node["props"].get("floor_modes")

    @pytest.mark.asyncio
    async def test_trigger_keywords_from_the_plan_are_present(
        self, db_corpus: Neo4jConnection
    ) -> None:
        node = await db_corpus.get_node_with_neighbors("TEC-PROC-DEFENSE-DEPTH-001")
        assert node is not None
        keywords = set(node["props"].get("trigger_keywords") or [])
        expected = {"validation", "entry point", "invariant", "guard", "root cause"}
        assert expected <= keywords, f"missing {expected - keywords}"

    @pytest.mark.asyncio
    async def test_demonstrates_debug_playbook(self, db_corpus: Neo4jConnection) -> None:
        node = await db_corpus.get_node_with_neighbors("TEC-PROC-DEFENSE-DEPTH-001")
        assert node is not None
        assert _neighbor(node, "PBK-PROC-DEBUG-001", "DEMONSTRATES", "out")


class TestF3DebugPlaybookInvokesDefenseDepth:
    @pytest.mark.asyncio
    async def test_debug_playbook_invokes_defense_depth(
        self, db_corpus: Neo4jConnection
    ) -> None:
        node = await db_corpus.get_node_with_neighbors("PBK-PROC-DEBUG-001")
        assert node is not None
        assert _neighbor(node, "TEC-PROC-DEFENSE-DEPTH-001", "INVOKES", "out")

    @pytest.mark.asyncio
    async def test_debug_playbook_phase_4_names_defense_depth(
        self, db_corpus: Neo4jConnection
    ) -> None:
        node = await db_corpus.get_node_with_neighbors("PBK-PROC-DEBUG-001")
        assert node is not None
        body = node["props"].get("body", "")
        assert "TEC-PROC-DEFENSE-DEPTH-001" in body


# ---------------------------------------------------------------------------
# F4: TEC-PROC-TEST-POLLUTION-001 -- bisection for full-suite-only failures,
# INVOKES-wired from the heisenbug playbook only. scripts/find-test-
# polluter.sh is its driver script (interface pinned, never run).
# ---------------------------------------------------------------------------


class TestF4TestPollutionTechniqueNode:
    @pytest.mark.asyncio
    async def test_node_exists_as_a_technique_with_non_empty_trigger_and_statement(
        self, db_corpus: Neo4jConnection
    ) -> None:
        node = await db_corpus.get_node_with_neighbors("TEC-PROC-TEST-POLLUTION-001")
        assert node is not None, "TEC-PROC-TEST-POLLUTION-001 does not exist in the graph yet"
        assert node["type"] == "Technique"
        props = node["props"]
        assert props.get("trigger", "").strip()
        assert props.get("statement", "").strip()

    @pytest.mark.asyncio
    async def test_floor_modes_is_empty(self, db_corpus: Neo4jConnection) -> None:
        node = await db_corpus.get_node_with_neighbors("TEC-PROC-TEST-POLLUTION-001")
        assert node is not None
        assert not node["props"].get("floor_modes")

    @pytest.mark.asyncio
    async def test_trigger_keywords_from_the_plan_are_present(
        self, db_corpus: Neo4jConnection
    ) -> None:
        node = await db_corpus.get_node_with_neighbors("TEC-PROC-TEST-POLLUTION-001")
        assert node is not None
        keywords = set(node["props"].get("trigger_keywords") or [])
        expected = {
            "pollution", "passes in isolation", "bisect", "test order",
            "shared state", "full suite",
        }
        assert expected <= keywords, f"missing {expected - keywords}"

    @pytest.mark.asyncio
    async def test_demonstrates_heisenbug_playbook_only(
        self, db_corpus: Neo4jConnection
    ) -> None:
        node = await db_corpus.get_node_with_neighbors("TEC-PROC-TEST-POLLUTION-001")
        assert node is not None
        assert _neighbor(node, "PBK-PROC-DIAGNOSE-HEISENBUG-001", "DEMONSTRATES", "out")


class TestF4HeisenbugPlaybookInvokesTestPollution:
    @pytest.mark.asyncio
    async def test_heisenbug_playbook_invokes_test_pollution(
        self, db_corpus: Neo4jConnection
    ) -> None:
        node = await db_corpus.get_node_with_neighbors("PBK-PROC-DIAGNOSE-HEISENBUG-001")
        assert node is not None
        assert _neighbor(node, "TEC-PROC-TEST-POLLUTION-001", "INVOKES", "out")

    @pytest.mark.asyncio
    async def test_failing_test_playbook_does_not_gain_this_edge(
        self, db_corpus: Neo4jConnection
    ) -> None:
        # The plan's F4 edges block targets ONLY the heisenbug playbook, not
        # the failing-test one. This is a guard against an over-broad
        # implementation wiring the pollution technique to both diagnose
        # playbooks; it holds true before AND after cycle F lands, so it is
        # not expected to be part of the RED signal.
        node = await db_corpus.get_node_with_neighbors("PBK-PROC-DIAGNOSE-FAILING-TEST-001")
        assert node is not None
        assert not _neighbor(node, "TEC-PROC-TEST-POLLUTION-001", "INVOKES", "out")


class TestF4PolluterScriptInterface:
    """File-existence/executability/syntax/interface pins only. Never
    invokes an actual bisection -- that needs a real suite run and a
    deliberately planted polluter, which the plan marks operational."""

    def test_script_exists(self) -> None:
        assert POLLUTER_SCRIPT.exists(), f"{POLLUTER_SCRIPT} does not exist yet"

    def test_script_is_executable(self) -> None:
        assert POLLUTER_SCRIPT.exists(), f"{POLLUTER_SCRIPT} does not exist yet"
        assert os.access(POLLUTER_SCRIPT, os.X_OK), f"{POLLUTER_SCRIPT} is not executable"

    def test_script_passes_bash_syntax_check(self) -> None:
        assert POLLUTER_SCRIPT.exists(), f"{POLLUTER_SCRIPT} does not exist yet"
        r = subprocess.run(
            ["bash", "-n", str(POLLUTER_SCRIPT)], capture_output=True, text=True
        )
        assert r.returncode == 0, f"bash -n failed:\n{r.stderr}"

    def test_help_flag_names_victim_and_artifact(self) -> None:
        assert POLLUTER_SCRIPT.exists(), f"{POLLUTER_SCRIPT} does not exist yet"
        r = subprocess.run(
            ["bash", str(POLLUTER_SCRIPT), "--help"], capture_output=True, text=True
        )
        assert r.returncode != 0
        assert "--victim" in r.stderr
        assert "--artifact" in r.stderr

    def test_no_args_prints_the_same_usage(self) -> None:
        assert POLLUTER_SCRIPT.exists(), f"{POLLUTER_SCRIPT} does not exist yet"
        r = subprocess.run(["bash", str(POLLUTER_SCRIPT)], capture_output=True, text=True)
        assert r.returncode != 0
        assert "--victim" in r.stderr
        assert "--artifact" in r.stderr


# ---------------------------------------------------------------------------
# F5: FRB-COMMS-001 gains a seventh forbidden phrase. Widening, not
# replacing: the six existing phrases must survive untouched (confirmed on
# disk today: exactly six, none of them the new one).
# ---------------------------------------------------------------------------


class TestF5ForbiddenPhraseWidened:
    @pytest.mark.asyncio
    async def test_new_phrase_present(self, db_corpus: Neo4jConnection) -> None:
        node = await db_corpus.get_node_with_neighbors("FRB-COMMS-001")
        assert node is not None
        phrases = node["props"].get("forbidden_phrases") or []
        assert "Let me implement that now" in phrases

    @pytest.mark.asyncio
    async def test_all_six_existing_phrases_survive(
        self, db_corpus: Neo4jConnection
    ) -> None:
        node = await db_corpus.get_node_with_neighbors("FRB-COMMS-001")
        assert node is not None
        phrases = set(node["props"].get("forbidden_phrases") or [])
        existing = {
            "You're absolutely right", "Great point", "Excellent feedback",
            "Thanks for the review", "Good catch", "That makes a lot of sense",
        }
        assert existing <= phrases, f"missing {existing - phrases}"


# ---------------------------------------------------------------------------
# F6: ENF-PROC-PLAN-001's enforcement field claims a "Haiku judge" for Gate 5
# Tier 2 that hooks/scripts/writ-quality-judge.sh does not run. Assertions
# here run against the graph node's `enforcement` property -- corpus data,
# never docs/reference/http-api.md prose (out of scope: not a test surface).
# ---------------------------------------------------------------------------


class TestF6PlanRuleEnforcementCorpus:
    @pytest.mark.asyncio
    async def test_enforcement_names_no_external_model(
        self, db_corpus: Neo4jConnection
    ) -> None:
        node = await db_corpus.get_node_with_neighbors("ENF-PROC-PLAN-001")
        assert node is not None
        enforcement = (node["props"].get("enforcement") or "").lower()
        assert "haiku" not in enforcement

    @pytest.mark.asyncio
    async def test_enforcement_names_the_real_self_review_mechanism(
        self, db_corpus: Neo4jConnection
    ) -> None:
        node = await db_corpus.get_node_with_neighbors("ENF-PROC-PLAN-001")
        assert node is not None
        enforcement = (node["props"].get("enforcement") or "").lower()
        assert "self-scored" in enforcement or "self-review" in enforcement
        assert "writ-quality-judge.sh" in enforcement
        assert "quality-judgment" in enforcement


class TestF6ClaimVersusCodePin:
    """Reproduces plan.md's own prescribed durable form for the F6
    claim-vs-code pin. Deliberately NOT a wording pin: it asserts the
    IMPLICATION (a model-vendor token named in `enforcement` /
    `mechanical_enforcement_path` obligates the named hook to contain that
    same token), because a wording pin on this sentence would rot the first
    time someone improves it. Reads bible/ (untracked, locally regenerated)
    and hooks/scripts/ directly rather than the graph, matching the plan's
    own text for this specific pin."""

    pytestmark = requires_bible

    _MODEL_TOKENS = ("haiku", "anthropic", "claude-")

    def test_plan_rule_enforcement_claim_matches_the_hook(self) -> None:
        fm = _parse_frontmatter(METHODOLOGY_DIR / "ENF-PROC-PLAN-001.md")
        claim = f"{fm.get('enforcement') or ''} {fm.get('mechanical_enforcement_path') or ''}".lower()
        hook = (HOOKS_DIR / "writ-quality-judge.sh").read_text(encoding="utf-8").lower()
        for token in self._MODEL_TOKENS:
            if token in claim:
                assert token in hook, (
                    f"ENF-PROC-PLAN-001 enforcement claims '{token}' but "
                    f"writ-quality-judge.sh never calls it"
                )

    def test_plan_rule_enforcement_paths_exist(self) -> None:
        fm = _parse_frontmatter(METHODOLOGY_DIR / "ENF-PROC-PLAN-001.md")
        claim = f"{fm.get('enforcement') or ''} {fm.get('mechanical_enforcement_path') or ''}"
        for path in re.findall(r"hooks/scripts/[\w.-]+\.sh", claim):
            assert (REPO_ROOT / path).exists(), (
                f"ENF-PROC-PLAN-001 names {path}, which does not exist"
            )


class TestF6CodeCommentPins:
    """The same false claim propagated into two code comments/docstrings
    outside the corpus (confirmed on disk 2026-08-13). Both pins are scoped
    so the SURVIVING mention (session_state.py's out-of-process-judge
    latency note, which the plan keeps on purpose because it is true) does
    not false-positive the check: `_default_cache`'s only Haiku mention is a
    plain comment with no legitimate survivor to protect, so its whole
    source is checked; `session_quality_judgment_set`'s legitimate survivor
    lives in the function BODY, so only its `__doc__` is checked."""

    def test_default_cache_comment_drops_haiku(self) -> None:
        from writ.session.cache import _default_cache

        source = inspect.getsource(_default_cache)
        assert "haiku" not in source.lower(), (
            "writ/session/cache.py's _default_cache still names a Haiku "
            "judge for Gate 5 Tier 2"
        )

    def test_quality_judgment_endpoint_docstring_drops_haiku(self) -> None:
        from writ.server.routes.session_state import session_quality_judgment_set

        docstring = session_quality_judgment_set.__doc__ or ""
        assert "haiku" not in docstring.lower(), (
            "session_quality_judgment_set's docstring still names a Haiku "
            "judge for Gate 5 Tier 2"
        )

    def test_surviving_out_of_process_judge_note_is_not_deleted(self) -> None:
        # Regression guard for the opposite mistake: a sweep that ALSO
        # deletes the one Haiku mention the plan keeps on purpose. Scoped to
        # the function's full source (unlike the docstring-only test
        # above), so it stays green whether or not the docstring fix has
        # landed yet.
        from writ.server.routes.session_state import session_quality_judgment_set

        source = inspect.getsource(session_quality_judgment_set)
        assert "haiku" in source.lower(), (
            "the out-of-process-judge latency note (kept on purpose) "
            "appears to have been deleted from session_quality_judgment_set"
        )
