"""INV-3: standards-before-investigation doctrine + investigate-mode injection.

The unified investigation engine (INV-1) gets its doctrine as first-class graph
content, injected the instant a session enters `investigate` mode -- standards
arrive BEFORE gathering, not after.

Authored:
- PBK-PROC-RESEARCH-001 (Playbook): the scope->gather->narrow->verify->synthesize
  spine with the source standards embedded inline; INVOKES the source-eval Technique.
- TEC-PROC-SOURCE-EVAL-001 (Technique): single-source credibility procedure;
  DEMONSTRATES the research Playbook.
- bible/research/rules.md: RESEARCH-SOURCE-001 / -CORROBORATE-001 / -CITE-001 /
  -STALENESS-001 (domain=research).
Wired:
- the prompt-bundle endpoint in writ/server/routes/query.py maps investigate mode to
  query_source "investigation-doctrine" within the methodology-companion channel,
  rendered under the shared "[Writ: methodology companion]" header. writ-rag-inject.sh
  has no per-mode investigate arm; it delivers the rendered bundle via one /prompt-bundle
  curl.

Parse/validity + edge + lint tests are hermetic. Retrieval/e2e tests require the
nodes ingested into the live graph + a warmed daemon (the implementation does that
via `writ import-markdown` + restart); they run against a per-module isolated
daemon (tests/_prompt_turn.py) rather than skipping on an unreachable shared
address. Mirrors tests/test_diagnose_playbooks.py and
tests/test_debug_playbook_injection.py: the live-hook tests here run the real
UserPromptSubmit hook and then the real Stop hook back-to-back from the same
project cwd, because production only ever drains a turn's buffered rag_query
rows onto the `metrics` stream from the Stop hook.
"""
from __future__ import annotations

import json
import os
import re
import urllib.request
import uuid
from pathlib import Path

import pytest

# Exercises the router's cwd-based project-scope resolution to a tmp subdir;
# opt out of the autouse WRIT_FRICTION_LOG redirect so rag_query telemetry
# routes to the split per-project streams under WRIT_LOG_ROOT (P1 router).
from tests._bible_guard import requires_bible

pytestmark = [requires_bible, pytest.mark.no_friction_isolation]

from writ.graph.ingest import (
    parse_edges_from_file,
    parse_nodes_from_file,
    validate_parsed_node,
)

from tests.conftest import writ_server_source
from tests._prompt_turn import run_prompt_turn, seed_session_cache
from tests._prompt_turn import isolated_prompt_daemon  # noqa: F401


SKILL_DIR = Path(__file__).resolve().parent.parent
METHODOLOGY = SKILL_DIR / "bible" / "methodology"
RESEARCH_RULES = SKILL_DIR / "bible" / "research" / "rules.md"
HOOK = str(SKILL_DIR / "hooks" / "scripts" / "writ-rag-inject.sh")

RESEARCH_PLAYBOOK = "PBK-PROC-RESEARCH-001"
SOURCE_EVAL_TECHNIQUE = "TEC-PROC-SOURCE-EVAL-001"
PLAYBOOK_PATH = METHODOLOGY / f"{RESEARCH_PLAYBOOK}.md"
TECHNIQUE_PATH = METHODOLOGY / f"{SOURCE_EVAL_TECHNIQUE}.md"

RESEARCH_RULE_IDS = {
    "RESEARCH-SOURCE-001",
    "RESEARCH-CORROBORATE-001",
    "RESEARCH-CITE-001",
    "RESEARCH-STALENESS-001",
}

RESEARCH_SYMPTOM = (
    "I need to research the current best practice for this online and gather "
    "authoritative sources before I summarize the findings."
)

MAGENTO_TOKENS = ["magento", "mview", "cron_schedule", "php-spx", "innodb"]


def _triage_message(
    expected_source: str, events: list[dict], stdout: str, queries_delta: int,
) -> str:
    """One failure message that carries every triage rung, so a reader tells a
    DRAIN failure from a RETRIEVAL failure without re-running anything.

    The discriminator comes from the mechanism, not from a guess: a drain
    problem cannot be selective, because it takes the whole list (every
    query_source, plus the `hook_execution` rows), and its symptom is
    `events == []`. A retrieval problem is ONE source missing from a NON-EMPTY
    list. The `queries` delta then splits the empty case in two: zero means the
    turn never reached this daemon at all (transport), positive means the daemon
    served it and nothing landed (drain)."""
    rag_events = [e for e in events if e.get("event") == "rag_query"]
    sources = sorted({e.get("query_source") for e in rag_events})
    return (
        f"no rag_query with query_source={expected_source!r} in the drained "
        f"turn.\n"
        f"rung 1 (transport vs drain): events empty={not events}, "
        f"daemon queries delta={queries_delta}\n"
        f"rung 2 ('broad' present -- retrieval + drain both worked): "
        f"{'broad' in sources}\n"
        f"rung 3 (companion header rendered on stdout): "
        f"{'[Writ: methodology companion]' in stdout}\n"
        f"query_source values seen: {sources}\n"
        f"events:\n{json.dumps(events, indent=2)}\n"
        f"stdout:\n{stdout}"
    )


class TestResearchNodesParse:
    """Parse-level validation of the two methodology nodes -- hermetic, no DB."""

    def test_research_playbook_is_valid_playbook(self) -> None:
        assert PLAYBOOK_PATH.exists(), f"{PLAYBOOK_PATH} does not exist yet"
        nodes = parse_nodes_from_file(PLAYBOOK_PATH)
        assert len(nodes) == 1, f"expected one node in {PLAYBOOK_PATH}, got {len(nodes)}"
        node = nodes[0]
        assert node.get("node_type") == "Playbook", (
            f"{RESEARCH_PLAYBOOK} must be node_type=Playbook, got {node.get('node_type')}"
        )
        assert node.get("playbook_id") == RESEARCH_PLAYBOOK
        validate_parsed_node(node)  # raises on invalid front-matter

    def test_source_eval_is_valid_technique(self) -> None:
        assert TECHNIQUE_PATH.exists(), f"{TECHNIQUE_PATH} does not exist yet"
        nodes = parse_nodes_from_file(TECHNIQUE_PATH)
        assert len(nodes) == 1
        node = nodes[0]
        assert node.get("node_type") == "Technique", (
            f"{SOURCE_EVAL_TECHNIQUE} must be node_type=Technique, got {node.get('node_type')}"
        )
        assert node.get("technique_id") == SOURCE_EVAL_TECHNIQUE
        validate_parsed_node(node)

    def test_doctrine_is_stack_agnostic(self) -> None:
        for path in (PLAYBOOK_PATH, TECHNIQUE_PATH):
            assert path.exists(), f"{path} does not exist yet"
            body = path.read_text(encoding="utf-8").lower()
            leaked = [t for t in MAGENTO_TOKENS if t in body]
            assert not leaked, f"{path.name} leaks stack-specific tokens {leaked}"


class TestDoctrineEmbedsStandards:
    """The Playbook must carry the source standards inline so the doctrine
    travels even if the companion Rules do not rank in a given query."""

    def test_playbook_states_five_phases_and_standards(self) -> None:
        assert PLAYBOOK_PATH.exists(), f"{PLAYBOOK_PATH} does not exist yet"
        body = PLAYBOOK_PATH.read_text(encoding="utf-8").lower()
        # The unified investigation phases.
        for phase in ("scope", "gather", "narrow", "verif", "synthi"):
            assert phase[:5] in body, f"playbook must name the '{phase}' phase"
        # The source standards (corroboration, citation, staleness, authority).
        for standard in ("corroborat", "cit", "stale", "primary"):
            assert standard in body, f"playbook must embed the '{standard}' standard"

    def test_playbook_states_presence_not_truth_ceiling(self) -> None:
        """FRB-COMMS-002 honesty: the doctrine claims capture, never truth."""
        assert PLAYBOOK_PATH.exists(), f"{PLAYBOOK_PATH} does not exist yet"
        body = PLAYBOOK_PATH.read_text(encoding="utf-8").lower()
        assert "truth" in body and "presence" in body, (
            "playbook must state the presence-not-truth ceiling"
        )


class TestDoctrineEdges:
    """INVOKES / DEMONSTRATES edges are declared and resolve (no dangling)."""

    def test_playbook_invokes_source_eval(self) -> None:
        # 1.3b: SOURCE-EVAL is a Technique (not a SubagentRole), so the spine
        # INVOKES it (applies inline) rather than DISPATCHES.
        assert PLAYBOOK_PATH.exists(), f"{PLAYBOOK_PATH} does not exist yet"
        edges = parse_edges_from_file(PLAYBOOK_PATH)
        invoke = {e["target"] for e in edges if e.get("type") == "INVOKES"}
        assert SOURCE_EVAL_TECHNIQUE in invoke, (
            f"{RESEARCH_PLAYBOOK} must INVOKES {SOURCE_EVAL_TECHNIQUE}; got {sorted(invoke)}"
        )

    def test_dispatch_target_resolves(self) -> None:
        assert TECHNIQUE_PATH.exists(), f"{TECHNIQUE_PATH} does not exist yet (would dangle)"
        nodes = parse_nodes_from_file(TECHNIQUE_PATH)
        assert nodes and nodes[0].get("technique_id") == SOURCE_EVAL_TECHNIQUE

    def test_technique_demonstrates_playbook(self) -> None:
        assert TECHNIQUE_PATH.exists(), f"{TECHNIQUE_PATH} does not exist yet"
        edges = parse_edges_from_file(TECHNIQUE_PATH)
        demo = {e["target"] for e in edges if e.get("type") == "DEMONSTRATES"}
        assert RESEARCH_PLAYBOOK in demo, (
            f"{SOURCE_EVAL_TECHNIQUE} must DEMONSTRATES {RESEARCH_PLAYBOOK}; got {sorted(demo)}"
        )


class TestResearchRules:
    """bible/research/rules.md holds the four source-standard Rules."""

    def test_rules_file_exists(self) -> None:
        assert RESEARCH_RULES.exists(), f"{RESEARCH_RULES} does not exist yet"

    def test_all_four_rules_present_and_valid(self) -> None:
        assert RESEARCH_RULES.exists(), f"{RESEARCH_RULES} does not exist yet"
        nodes = parse_nodes_from_file(RESEARCH_RULES)
        ids = {n.get("rule_id") for n in nodes}
        assert RESEARCH_RULE_IDS <= ids, (
            f"missing research rules: {RESEARCH_RULE_IDS - ids}; found {sorted(ids)}"
        )
        for n in nodes:
            if n.get("rule_id") in RESEARCH_RULE_IDS:
                assert n.get("node_type") == "Rule"
                assert (n.get("domain") or "").lower() == "research", (
                    f"{n.get('rule_id')} must be domain=research, got {n.get('domain')}"
                )
                assert n.get("statement"), f"{n.get('rule_id')} must have a Statement"
                assert n.get("trigger"), f"{n.get('rule_id')} must have a Trigger"
                validate_parsed_node(n)

    def test_cite_rule_ties_to_citation_log(self) -> None:
        """RESEARCH-CITE-001 must reference the INV-2 citation ledger concept."""
        assert RESEARCH_RULES.exists(), f"{RESEARCH_RULES} does not exist yet"
        body = RESEARCH_RULES.read_text(encoding="utf-8").lower()
        assert "citation" in body, "RESEARCH-CITE-001 must reference citations"


class TestInvestigateArmStructural:
    """Lint-level guard: the methodology block must carry the investigate arm so
    a future regression that drops it is caught without a live server."""

    def test_hook_references_investigation_doctrine(self) -> None:
        # #8: the query_source map moved into the /prompt-bundle endpoint.
        server = writ_server_source()
        assert "investigation-doctrine" in server, (
            "the /prompt-bundle endpoint must map investigate -> 'investigation-doctrine'"
        )

    def test_hook_has_investigate_case_via_companion(self) -> None:
        # 1.7 CUTOVER: methodology is delivered by /methodology-companion (the
        # companion serves the investigate FLOOR -- RESEARCH/AUDIT-FANOUT/SOURCE-EVAL
        # /INVESTIGATE -- from node-declared floor_modes), not a mode->node_type
        # /query. Lint guards: the investigate case exists and the hook posts to
        # the companion (behavioral firing proven by the e2e test below).
        # #8: the investigate query_source case + the companion call live in the
        # /prompt-bundle endpoint now; the hook delivers the rendered bundle.
        server = writ_server_source()
        assert re.search(r'"investigate":\s*"investigation-doctrine"', server), (
            "the /prompt-bundle endpoint must keep an investigate-mode query_source case"
        )
        assert "methodology_companion" in server, (
            "the endpoint must deliver methodology via the methodology_companion handler"
        )
        with open(HOOK) as f:
            body = f.read()
        assert "/prompt-bundle" in body, (
            "the hook must deliver the methodology bundle via /prompt-bundle"
        )


class TestInvestigateDoctrineRetrieval:
    """Integration: a research symptom surfaces the doctrine via /query."""

    def test_symptom_surfaces_research_playbook(self, isolated_prompt_daemon) -> None:
        """Reddened by the research symptom no longer ranking
        PBK-PROC-RESEARCH-001 into the top-k Playbook/Technique hits for
        domain=process -- a content or ranking regression in the graph, not a
        harness defect."""
        req = urllib.request.Request(
            f"{isolated_prompt_daemon['base_url']}/query",
            data=json.dumps({
                "query": RESEARCH_SYMPTOM,
                "node_types": ["Playbook", "Technique"],
                "domain": "process",
                "budget_tokens": 2000,
                "top_k": 6,
            }).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            body = json.loads(resp.read())
        ids = [r.get("rule_id") for r in body.get("rules", [])]
        assert RESEARCH_PLAYBOOK in ids, (
            f"research symptom must surface {RESEARCH_PLAYBOOK}; got {ids}"
        )


class TestInvestigateArmEndToEnd:
    """Run the real two-hook turn with a seeded mode; assert the drained stream."""

    def test_investigate_mode_fires_doctrine_query(
        self, isolated_prompt_daemon, tmp_path,
    ) -> None:
        """Reddened by the Stop hook draining a buffer under a different
        WRIT_CACHE_DIR or a different cwd than the UserPromptSubmit hook used
        (the drain then finds nothing), or by removing the Stop-hook run from
        the turn entirely (the drain never happens at all, events == [])."""
        daemon = isolated_prompt_daemon
        sid = f"investigate-doctrine-e2e-{uuid.uuid4().hex[:8]}"
        cache_path = seed_session_cache(
            daemon["health"]["cache_dir"], sid, "investigate",
        )
        try:
            events, stdout, queries_delta = run_prompt_turn(
                daemon, tmp_path, sid, RESEARCH_SYMPTOM,
            )
        finally:
            try:
                os.unlink(cache_path)
            except FileNotFoundError:
                pass
        doctrine_q = [
            e for e in events
            if e.get("event") == "rag_query"
            and e.get("query_source") == "investigation-doctrine"
        ]
        assert doctrine_q, _triage_message(
            "investigation-doctrine", events, stdout, queries_delta,
        )

    def test_conversation_mode_does_not_fire_doctrine(
        self, isolated_prompt_daemon, tmp_path,
    ) -> None:
        """Mode-gating oracle: the arm must NOT fire outside investigate. The
        absence is asserted alongside its companion methodology-conversation
        row from the SAME channel, one dict key apart
        (writ/server/routes/query.py:349-353), so it cannot be a dead channel
        wearing a correct decline's face. Reddened by either: (a) a regression
        that makes the doctrine arm fire outside investigate mode, or (b) a
        channel-3 failure (mode unresolved, the remaining_budget > 600 gate, or
        a companion error) that would ALSO remove the companion row this test
        requires to be present."""
        daemon = isolated_prompt_daemon
        sid = f"conv-doctrine-e2e-{uuid.uuid4().hex[:8]}"
        cache_path = seed_session_cache(
            daemon["health"]["cache_dir"], sid, "conversation",
        )
        try:
            events, stdout, queries_delta = run_prompt_turn(
                daemon, tmp_path, sid, RESEARCH_SYMPTOM,
            )
        finally:
            try:
                os.unlink(cache_path)
            except FileNotFoundError:
                pass
        doctrine_q = [
            e for e in events
            if e.get("event") == "rag_query"
            and e.get("query_source") == "investigation-doctrine"
        ]
        companion_q = [
            e for e in events
            if e.get("event") == "rag_query"
            and e.get("query_source") == "methodology-conversation"
        ]
        assert not doctrine_q, (
            f"investigation-doctrine must NOT fire in conversation mode; events:\n"
            f"{json.dumps(events, indent=2)}"
        )
        assert companion_q, (
            "conversation mode must still fire its own methodology-conversation "
            "companion row; without it, this absence is indistinguishable from "
            "channel 3 having died entirely.\n"
            + _triage_message("methodology-conversation", events, stdout, queries_delta)
        )
