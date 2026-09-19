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

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from writ.graph.ingest import (
    parse_edges_from_file,
    parse_nodes_from_file,
    validate_parsed_node,
)

from tests._bible_guard import requires_bible
from tests.fixtures.server_routes import route_pipeline  # noqa: F401

pytestmark = requires_bible


SKILL_DIR = Path(__file__).resolve().parent.parent
METHODOLOGY = SKILL_DIR / "bible" / "methodology"
SPINE = METHODOLOGY / "PBK-PROC-DEBUG-001.md"

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


@pytest.fixture(scope="module")
def _diagnose_nodes_present() -> int:
    """The corpus PRECONDITION for TestDiagnoseRetrieval, DERIVED FROM THE
    PROPERTY rather than a generic corpus-completeness check: the three
    PBK-PROC-DIAGNOSE-* ids this module's own DIAGNOSE dict names must be
    present in the graph. Runs BEFORE route_pipeline/live_pipeline is built
    (requested first in the test's fixture list), because the pipeline indexes
    the graph ONCE at build time and a repair afterwards is invisible to it.
    FAILS, quoting the count it saw, rather than skipping: a route that
    answers against an empty precondition is a corpus problem, never a
    reason to weaken the retrieval assertion below.
    """
    from tests._corpus import require_population

    ids_literal = ", ".join(f'"{node_id}"' for node_id in DIAGNOSE)
    return require_population(
        f"MATCH (r:Playbook) WHERE r.playbook_id IN [{ids_literal}] RETURN count(r)",
        "the three PBK-PROC-DIAGNOSE-* nodes the diagnose-* symptom tests select",
    )


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
    """ROUTE (Decision 4, plan.md 2412ba38-51e1-4b73-895b-7b240a3c21d3): each
    symptom surfaces its diagnose-* node via /query, driven over the route
    against route_pipeline (conftest.py's session-scoped live_pipeline
    installed at writ.server._pipeline). No daemon, no socket.
    """

    @pytest.mark.parametrize("node_id", list(DIAGNOSE))
    def test_symptom_surfaces_node(
        self, node_id: str, _diagnose_nodes_present, route_pipeline
    ) -> None:
        from writ.server import app as server_app

        symptom = DIAGNOSE[node_id][1]
        client = TestClient(server_app)
        resp = client.post("/query", json={
            "query": symptom,
            "node_types": ["Playbook", "Technique"],
            "domain": "process",
            "budget_tokens": 2000,
            "top_k": 6,
        })
        assert resp.status_code == 200, (
            f"POST /query for {node_id!r} returned {resp.status_code}, not 200: "
            f"{resp.text}"
        )
        body = resp.json()
        ids = [r.get("rule_id") for r in body.get("rules", [])]
        assert node_id in ids, (
            f"symptom {symptom!r} must surface {node_id} (present in the graph "
            f"per the _diagnose_nodes_present precondition above); got {ids}. "
            f"RANKING is not under this module's control, which is why the "
            f"assertion is membership in the top k rather than exact order."
        )
