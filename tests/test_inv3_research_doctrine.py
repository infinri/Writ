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
via `writ import-markdown` + restart); they skip when the server is unreachable.
Mirrors tests/test_diagnose_playbooks.py and tests/test_debug_playbook_injection.py.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
import uuid
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from writ.shared.logging import read_streams, resolve_project  # noqa: E402

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

from tests._daemon import _port
from tests.conftest import writ_server_source


SKILL_DIR = Path(__file__).resolve().parent.parent
METHODOLOGY = SKILL_DIR / "bible" / "methodology"
RESEARCH_RULES = SKILL_DIR / "bible" / "research" / "rules.md"
HOOK = str(SKILL_DIR / "hooks" / "scripts" / "writ-rag-inject.sh")
# #8: the per-mode methodology query_source map (incl investigate ->
# investigation-doctrine) + the companion call moved from the hook into the warm
# /prompt-bundle endpoint; the hook delivers the rendered bundle via one curl.
SERVER = f"http://localhost:{_port()}"

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


def _server_up() -> bool:
    try:
        with urllib.request.urlopen(f"{SERVER}/health", timeout=2):
            return True
    except (urllib.error.URLError, OSError):
        return False


def _seed_cache(cache_dir: str, sid: str, mode: str) -> str:
    """Seed a non-orchestrator session cache in the server's cache dir."""
    path = os.path.join(cache_dir, f"writ-session-{sid}.json")
    with open(path, "w") as f:
        json.dump(
            {
                "mode": mode,
                "is_orchestrator": False,
                "is_subagent": False,
                "current_phase": None,
                "loaded_rule_ids": [],
                "loaded_rule_ids_by_phase": {},
                "remaining_budget": 8000,
                "context_percent": 0,
                "queries": 0,
                "files_written": [],
                "loaded_rules": [],
            },
            f,
        )
    return path


def _run_hook_events(tmp_path, sid: str, prompt: str) -> tuple[int, str, list[dict]]:
    """Run writ-rag-inject.sh against the live server in a tmp project cwd;
    return (returncode, stderr, parsed rag_query events from the P1 metrics
    stream). rag_query is a `metrics`-stream event; the project scope derives
    from the hook's cwd, resolved the same way the router does."""
    project_root = tmp_path / "proj"
    project_root.mkdir(exist_ok=True)
    (project_root / ".git").mkdir(exist_ok=True)  # marker for router project scope

    envelope = json.dumps({"session_id": sid, "prompt": prompt})
    result = subprocess.run(
        ["bash", HOOK],
        input=envelope,
        capture_output=True,
        text=True,
        cwd=str(project_root),
        timeout=15,
    )
    events = read_streams(resolve_project(str(project_root)), ["metrics"])
    return result.returncode, result.stderr, events


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

    def test_symptom_surfaces_research_playbook(self) -> None:
        if not _server_up():
            pytest.skip("Writ server unreachable")
        req = urllib.request.Request(
            f"{SERVER}/query",
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
    """Run the hook with a seeded investigate-mode cache; assert the friction log."""

    def test_investigate_mode_fires_doctrine_query(self, tmp_path) -> None:
        if not _server_up():
            pytest.skip("Writ server unreachable")
        sid = f"investigate-doctrine-e2e-{uuid.uuid4().hex[:8]}"
        cache_dir = os.environ.get("WRIT_CACHE_DIR", tempfile.gettempdir())
        cache_path = _seed_cache(cache_dir, sid, "investigate")
        try:
            rc, stderr, events = _run_hook_events(tmp_path, sid, RESEARCH_SYMPTOM)
        finally:
            try:
                os.unlink(cache_path)
            except FileNotFoundError:
                pass
        assert rc == 0, f"hook returned {rc}; stderr={stderr[:800]}"
        doctrine_q = [
            e for e in events
            if e.get("event") == "rag_query"
            and e.get("query_source") == "investigation-doctrine"
        ]
        assert doctrine_q, (
            "no rag_query with query_source=investigation-doctrine in investigate-mode run. "
            f"events:\n{json.dumps(events, indent=2)}"
        )

    def test_conversation_mode_does_not_fire_doctrine(self, tmp_path) -> None:
        """Mode-gating oracle: the arm must NOT fire outside investigate."""
        if not _server_up():
            pytest.skip("Writ server unreachable")
        sid = f"conv-doctrine-e2e-{uuid.uuid4().hex[:8]}"
        cache_dir = os.environ.get("WRIT_CACHE_DIR", tempfile.gettempdir())
        cache_path = _seed_cache(cache_dir, sid, "conversation")
        try:
            rc, stderr, events = _run_hook_events(tmp_path, sid, RESEARCH_SYMPTOM)
        finally:
            try:
                os.unlink(cache_path)
            except FileNotFoundError:
                pass
        assert rc == 0, f"hook returned {rc}; stderr={stderr[:800]}"
        doctrine_q = [
            e for e in events
            if e.get("event") == "rag_query"
            and e.get("query_source") == "investigation-doctrine"
        ]
        assert not doctrine_q, (
            f"investigation-doctrine must NOT fire in conversation mode; events:\n"
            f"{json.dumps(events, indent=2)}"
        )
