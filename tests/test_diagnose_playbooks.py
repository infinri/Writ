"""Increment 3: diagnose-* classifier playbooks.

Three symptom-class debugging playbooks that the general spine
(PBK-PROC-DEBUG-001) DISPATCHES to, giving debug mode a Gate-0 classifier.

Contract pinned here:
1. Each new file is a valid Playbook node with a PBK-PROC-DIAGNOSE-* id.
2. PBK-PROC-DEBUG-001 declares a DISPATCHES edge to each of the three (targets
   resolve -- no dangling).
3. Each playbook names Falsification (the universal mandatory step).
4. The playbooks are stack-agnostic (no Magento/PHP-specific tokens).
5. /query for each symptom surfaces the matching node (integration, live server
   -- requires the nodes ingested + a re-warmed daemon, which the implementation
   does via `writ import-markdown` + restart).
"""
from __future__ import annotations

import json
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from writ.graph.ingest import (
    parse_edges_from_file,
    parse_nodes_from_file,
    validate_parsed_node,
)

from tests._daemon import _port

from tests._bible_guard import requires_bible

pytestmark = requires_bible


SKILL_DIR = Path(__file__).resolve().parent.parent
METHODOLOGY = SKILL_DIR / "bible" / "methodology"
SPINE = METHODOLOGY / "PBK-PROC-DEBUG-001.md"
SERVER = f"http://localhost:{_port()}"

# node_id -> (filename, representative symptom query)
DIAGNOSE = {
    "PBK-PROC-DIAGNOSE-CRASH-STACKTRACE-001": (
        "PBK-PROC-DIAGNOSE-CRASH-STACKTRACE-001.md",
        "the program crashed with an exception and a stack trace pointing at a line",
    ),
    "PBK-PROC-DIAGNOSE-FAILING-TEST-001": (
        "PBK-PROC-DIAGNOSE-FAILING-TEST-001.md",
        "a unit test is failing unexpectedly after my change",
    ),
    "PBK-PROC-DIAGNOSE-HEISENBUG-001": (
        "PBK-PROC-DIAGNOSE-HEISENBUG-001.md",
        "an intermittent race condition that will not reproduce reliably",
    ),
}

MAGENTO_TOKENS = ["magento", "mview", "cron_schedule", "php-spx", "innodb"]


def _path(node_id: str) -> Path:
    return METHODOLOGY / DIAGNOSE[node_id][0]


def _server_up() -> bool:
    try:
        with urllib.request.urlopen(f"{SERVER}/health", timeout=2):
            return True
    except (urllib.error.URLError, OSError):
        return False


class TestDiagnoseNodesParse:
    """Parse-level validation -- hermetic, no DB."""

    @pytest.mark.parametrize("node_id", list(DIAGNOSE))
    def test_node_is_valid_playbook(self, node_id: str) -> None:
        path = _path(node_id)
        assert path.exists(), f"{path} does not exist yet"
        nodes = parse_nodes_from_file(path)
        assert len(nodes) == 1, f"expected exactly one node in {path}, got {len(nodes)}"
        node = nodes[0]
        assert node.get("node_type") == "Playbook", (
            f"{node_id} must be node_type=Playbook, got {node.get('node_type')}"
        )
        assert node.get("playbook_id") == node_id, (
            f"playbook_id must be {node_id}, got {node.get('playbook_id')}"
        )
        # Must pass schema validation (raises on invalid front-matter).
        validate_parsed_node(node)

    @pytest.mark.parametrize("node_id", list(DIAGNOSE))
    def test_node_names_falsification(self, node_id: str) -> None:
        path = _path(node_id)
        assert path.exists(), f"{path} does not exist yet"
        body = path.read_text(encoding="utf-8").lower()
        assert "falsif" in body, (
            f"{node_id} must name Falsification (the universal mandatory step)"
        )

    @pytest.mark.parametrize("node_id", list(DIAGNOSE))
    def test_node_is_stack_agnostic(self, node_id: str) -> None:
        path = _path(node_id)
        assert path.exists(), f"{path} does not exist yet"
        body = path.read_text(encoding="utf-8").lower()
        leaked = [t for t in MAGENTO_TOKENS if t in body]
        assert not leaked, (
            f"{node_id} contains Magento/PHP-specific tokens {leaked}; "
            "diagnose-* playbooks must be stack-agnostic"
        )


class TestSpineDispatchesDiagnose:
    """The spine must connect to each diagnose-* node, with no dangling target.

    1.3b: the diagnose-* targets are Playbooks (not SubagentRoles), so the spine
    INVOKES them (applies inline, one level) rather than DISPATCHES (which is
    reserved for spawning a role).
    """

    def test_spine_invokes_all_three(self) -> None:
        assert SPINE.exists(), f"{SPINE} missing"
        edges = parse_edges_from_file(SPINE)
        invoke_targets = {
            e["target"] for e in edges if e.get("type") == "INVOKES"
        }
        for node_id in DIAGNOSE:
            assert node_id in invoke_targets, (
                f"PBK-PROC-DEBUG-001 must INVOKES {node_id}; "
                f"invoke targets present: {sorted(invoke_targets)}"
            )

    def test_invoke_targets_resolve(self) -> None:
        """Each INVOKES target to a diagnose-* node is a real, parseable node
        (guards against a dangling edge from a typo in the id)."""
        for node_id in DIAGNOSE:
            path = _path(node_id)
            assert path.exists(), f"{path} does not exist yet (would dangle)"
            nodes = parse_nodes_from_file(path)
            assert nodes and nodes[0].get("playbook_id") == node_id, (
                f"DISPATCHES target {node_id} does not resolve to a node with that id"
            )


class TestDiagnoseRetrieval:
    """Integration: each symptom surfaces its diagnose-* node via /query."""

    @pytest.mark.parametrize("node_id", list(DIAGNOSE))
    def test_symptom_surfaces_node(self, node_id: str) -> None:
        if not _server_up():
            pytest.skip("Writ server unreachable")
        symptom = DIAGNOSE[node_id][1]
        req = urllib.request.Request(
            f"{SERVER}/query",
            data=json.dumps({
                "query": symptom,
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
        assert node_id in ids, (
            f"symptom {symptom!r} must surface {node_id}; got {ids}"
        )
