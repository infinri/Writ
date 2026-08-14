"""Phase 6h + 6i: methodology retrieval verification + playbook event wiring.

6h verification (Stage 4 traversal against live graph):
  After Phase 6e/f/g promoted the methodology corpus to bible/methodology
  and migration created 120 edges, Stage 4 graph traversal in
  writ/retrieval/traversal.py must expand a Rule's bundle to include
  linked Skill/Playbook/AntiPattern nodes via the new methodology edge
  types (TEACHES, GATES, COUNTERS, DEMONSTRATES, DISPATCHES,
  PRESSURE_TESTS, CONTAINS, ATTACHED_TO).

6i wiring (server-side playbook_step_complete emission):
  The /session/{sid}/advance-phase endpoint now emits a
  playbook_step_complete friction event after every phase advance,
  parallel to the existing phase_advance event. This gives Phase 5's
  --playbook-compliance analyzer real signal to work with: each
  workflow phase advance is one playbook step.
"""
from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from writ.analysis.friction import (
    analyze_playbook_compliance,
    parse_log,
)
from writ.server import app

WRIT_ROOT = Path(__file__).resolve().parent.parent


# ============================================================================
# 6h -- Stage 4 traversal verification (live graph)
# ============================================================================


class TestPhase6hStage4Surfaces:
    """Stage 4 graph traversal exposes methodology nodes via the new
    edge types (TEACHES, GATES, COUNTERS, etc.).

    Uses a synthetic AdjacencyCache populated to mirror the edge
    patterns the live methodology corpus produces. Live-graph
    integration testing belongs to PSR-style end-to-end runs (6j) --
    keeping the unit-level traversal contract test free of database
    fixture state means it can't flake when a peer test wipes the
    graph mid-suite (which conftest.pytest_sessionfinish then fails
    to restore because it only refills when count_rules() == 0).
    """

    @staticmethod
    def _cache_with_edges(edges: list[tuple[str, str, str]]):
        """Build a synthetic AdjacencyCache from (src, tgt, type) tuples.

        Mirrors the bidirectional storage that build_from_db uses
        (entries written for both src->tgt and tgt->src).
        """
        from writ.retrieval.traversal import AdjacencyCache
        cache = AdjacencyCache()
        for src, tgt, edge_type in edges:
            cache._neighbors.setdefault(src, []).append(
                {"rule_id": tgt, "edge_type": edge_type, "direction": "outgoing"}
            )
            cache._neighbors.setdefault(tgt, []).append(
                {"rule_id": src, "edge_type": edge_type, "direction": "incoming"}
            )
        return cache

    def test_cache_includes_methodology_edges(self) -> None:
        """A cache populated from methodology edges (TEACHES, GATES,
        COUNTERS, CONTAINS) stores neighbors keyed on methodology
        node IDs (SKL-, PBK-, ANT-, PHA-)."""
        cache = self._cache_with_edges([
            ("ANT-PROC-PLAN-001", "SKL-PROC-PLAN-001", "COUNTERS"),
            ("PBK-PROC-PLAN-001", "ENF-PROC-PLAN-001", "GATES"),
            ("PBK-PROC-PLAN-001", "PHA-PLAN-001", "CONTAINS"),
        ])
        for mid in (
            "ANT-PROC-PLAN-001", "SKL-PROC-PLAN-001",
            "PBK-PROC-PLAN-001", "ENF-PROC-PLAN-001",
            "PHA-PLAN-001",
        ):
            assert cache.get_neighbors(mid), (
                f"Cache missing neighbors for {mid}; Stage 4 cannot expand."
            )

    def test_neighbors_include_methodology_via_counters(self) -> None:
        """Starting from an AntiPattern that COUNTERS a Skill, Stage 4
        get_neighbors reaches the Skill (a direct neighbor)."""
        cache = self._cache_with_edges([
            ("ANT-PROC-PLAN-001", "SKL-PROC-PLAN-001", "COUNTERS"),
            ("ANT-PROC-PLAN-001", "ENF-PROC-PLAN-001", "COUNTERS"),
        ])
        neighbor_ids = [n["rule_id"] for n in cache.get_neighbors("ANT-PROC-PLAN-001")]
        assert "SKL-PROC-PLAN-001" in neighbor_ids
        assert "ENF-PROC-PLAN-001" in neighbor_ids

    def test_neighbors_work_for_skill_seed(self) -> None:
        """Stage 4 traversal is symmetric: starting from a Skill
        reaches the AntiPattern via the inverse-direction entry."""
        cache = self._cache_with_edges([
            ("ANT-PROC-PLAN-001", "SKL-PROC-PLAN-001", "COUNTERS"),
            ("PBK-PROC-PLAN-001", "SKL-PROC-PLAN-001", "TEACHES"),
        ])
        neighbor_ids = [n["rule_id"] for n in cache.get_neighbors("SKL-PROC-PLAN-001")]
        assert "ANT-PROC-PLAN-001" in neighbor_ids, (
            "Inverse-direction lookup failed; Stage 4 not symmetric."
        )
        assert "PBK-PROC-PLAN-001" in neighbor_ids


# ============================================================================
# 6i -- /advance-phase emits playbook_step_complete
# ============================================================================


@pytest.fixture
def client() -> TestClient:
    return TestClient(app)


@pytest.fixture
def tmp_log(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    p = tmp_path / "workflow-friction.log"
    monkeypatch.setenv("WRIT_FRICTION_LOG", str(p))
    cache_dir = tmp_path / "writ-cache"
    cache_dir.mkdir()
    monkeypatch.setenv("WRIT_CACHE_DIR", str(cache_dir))
    from writ.server import writ_session
    monkeypatch.setenv("WRIT_CACHE_DIR", str(cache_dir))
    return p


def _read_events(log: Path, event: str) -> list[dict]:
    if not log.exists():
        return []
    return [e.model_dump() for e in parse_log(log) if e.event == event]


def _advance_with_token(client: TestClient, sid: str, source: str = "tool"):
    """Advance the phase, writing the gate token the route now requires + consumes
    (audit P0 self-approval fix). The token is consumed per advance, so callers that
    advance repeatedly must call this each time (it re-writes the token)."""
    # Mint via the PRODUCTION writer, not an open() at tempfile.gettempdir():
    # gate_token_path hardcodes /tmp on purpose so the bash writer and the python reader
    # can never disagree, and pytest sets $TMPDIR -- so gettempdir() put the token where
    # the route never looks. The advance was then refused as self-approval, the route
    # still returned HTTP 200 with advanced=False, and the status-only assertion
    # below used to pass while no event was ever emitted.
    from tests.fixtures.session_state import write_bound_gate_token

    # Seed a work-mode session with a pending gate AND a project root holding a
    # gate-valid plan.md. Without the session, the route answers "No pending gate to
    # advance"; without the project root and plan, the phase-a gate refuses with
    # "project-root detection failed". Neither path emits an event, so the event
    # assertions below would fail for a reason that has nothing to do with wiring.
    import tempfile

    from writ.server import writ_session

    root = tempfile.mkdtemp(prefix=f"writ-6i-{sid}-")
    (Path(root) / "plan.md").write_text(
        "# Plan\n\n"
        "## Files\n\n- `src/foo.py` (modify) -- wire the thing\n\n"
        "## Analysis\n\nSeed plan for the advance-phase wiring test.\n\n"
        "## Rules Applied\n\nNo matching rules\n\n"
        "## Capabilities\n\n- [ ] the advance emits its events\n"
    )
    # A test skeleton as well as the plan: the route now runs the TARGET gate's
    # validator, so the SECOND advance in a two-advance sequence (testing ->
    # implementation) checks for test skeletons. It previously validated phase-a only,
    # which let the test-skeletons gate advance with no artifact at all.
    tests_dir = Path(root) / "tests"
    tests_dir.mkdir(exist_ok=True)
    (tests_dir / "test_seed.py").write_text("def test_seed():\n    assert True\n")
    with writ_session.mutate_cache(sid) as c:
        if not c.get("mode"):
            c["mode"] = "work"
            c["current_phase"] = "planning"
            c["gates_approved"] = []

    # The token is BOUND to the gate it authorizes and to the plan fingerprint, and the
    # route claims through claim_gate_token with no unbound fallback, so a bare one-line
    # secret is refused before the route reaches any gate logic. The binding is derived
    # from the session cache seeded just above, which is what the production mint does --
    # and it is re-derived per call, so the second advance in a sequence binds
    # test-skeletons rather than the phase-a gate the first one spent.
    tok = write_bound_gate_token(sid, "test-6i-gate-token")
    # project_root travels in the REQUEST body (gate.py reads req.project_root),
    # not the session cache, and the phase-a gate fails closed without it.
    resp = client.post(
        f"/session/{sid}/advance-phase",
        json={"confirmation_source": source, "token": tok, "project_root": root},
    )
    # A refusal is HTTP 200 with advanced=False, so assert the advance actually
    # happened; otherwise every downstream event assertion fails misleadingly.
    assert resp.status_code == 200, resp.text
    assert resp.json().get("advanced") is not False, (
        f"advance was refused, not performed: {resp.json()}"
    )
    return resp


class TestPhase6iAdvancePhaseFiresPlaybookStep:
    """Every /advance-phase call must emit BOTH a phase_advance and
    a playbook_step_complete event. The latter is what unlocks Phase
    5's --playbook-compliance analyzer."""

    def test_first_advance_fires_playbook_step_complete(
        self, client: TestClient, tmp_log: Path
    ) -> None:
        resp = _advance_with_token(client, "test-6i-1")
        assert resp.status_code == 200

        # Phase 1.2: phase_advance now ALSO routes through log_friction_event
        # (honors WRIT_FRICTION_LOG) instead of the old cwd-walked writer that
        # leaked into the repo log -- so both events land in tmp_log now.
        advances = _read_events(tmp_log, "phase_advance")
        assert len(advances) == 1, f"expected one phase_advance event; got {advances}"

        steps = _read_events(tmp_log, "playbook_step_complete")
        assert len(steps) == 1, (
            f"Expected one playbook_step_complete event after first "
            f"advance; got {len(steps)}: {steps}"
        )
        ev = steps[0]
        assert ev["playbook_id"] == "PBK-PROC-SDD-001"
        # planning -> testing is the first advance (planning is the
        # initial state).
        assert ev["step_id"] == "testing"
        assert ev["step_index"] == 1  # testing is index 1 in [planning, testing, implementation]
        assert ev["total_steps"] == 3

    def test_step_index_increments_across_advances(
        self, client: TestClient, tmp_log: Path
    ) -> None:
        for _ in range(2):
            _advance_with_token(client, "test-6i-2")
        steps = _read_events(tmp_log, "playbook_step_complete")
        # TWO advances, not three: work mode defines two gates (phase-a and
        # test-skeletons), so planning->testing and testing->implementation are the
        # only gated transitions. A third call is refused with "No pending gate to
        # advance" -- the loop used to run 3 times and silently absorb that refusal,
        # because the helper only checked the HTTP status and a refusal is still 200.
        assert len(steps) == 2
        indices = [s["step_index"] for s in steps]
        step_ids = [s["step_id"] for s in steps]
        assert indices == [1, 2], f"step_index sequence wrong: {indices}"
        assert step_ids == ["testing", "implementation"], (
            f"step_id sequence wrong: {step_ids}"
        )

    def test_compliance_analyzer_consumes_emitted_events(
        self, client: TestClient, tmp_log: Path
    ) -> None:
        """End-to-end: emit events via /advance-phase, feed them
        to analyze_playbook_compliance, expect a non-empty result row
        for PBK-PROC-SDD-001."""
        for _ in range(2):  # two gates in work mode; see the note above
            _advance_with_token(client, "test-6i-3")
        events = parse_log(tmp_log)
        rows = analyze_playbook_compliance(events, since_days=30)
        sdd_rows = [r for r in rows if r.playbook_id == "PBK-PROC-SDD-001"]
        assert sdd_rows, (
            f"--playbook-compliance analyzer found no rows for "
            f"PBK-PROC-SDD-001 after 2 advances. All rows: "
            f"{[(r.playbook_id, r.runs) for r in rows]}"
        )
        row = sdd_rows[0]
        assert row.runs >= 1
        # The 2 advances should be a contiguous step sequence (1, 2),
        # which the analyzer scores by step_index ordering. Whether
        # this counts as "compliant" depends on the analyzer's
        # definition; either way runs > 0 is the minimum we assert.

    def test_event_carries_session_and_mode(
        self, client: TestClient, tmp_log: Path
    ) -> None:
        _advance_with_token(client, "test-6i-4")
        steps = _read_events(tmp_log, "playbook_step_complete")
        assert len(steps) == 1
        # The event carries session_id under the standard 'session' key
        # (FrictionEvent's model field; see writ/analysis/friction.py).
        assert steps[0].get("session") == "test-6i-4"
