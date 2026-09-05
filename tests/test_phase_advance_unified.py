"""Unified test suite for the shared apply_phase_advance function.

Three test classes:

  TestApplyPhaseAdvance       -- hermetic, drives apply_phase_advance on a plain
                                  dict, one focused test per mutated field.

  TestCrossPathParity         -- the regression keystone: drives BOTH real advance
                                  callers (Path B = cmd_advance_phase, Path A = the
                                  live HTTP route session_advance_phase, driven
                                  in-process through httpx.AsyncClient over
                                  ASGITransport(app=app), no
                                  patch("writ.server.writ_session", ...)) through a
                                  full work cycle and asserts identical resulting
                                  cache state on the five fields, modulo Path A's
                                  extra confirmation_source and per-record ts.

  TestTerminalAndNonWorkNoAdvance
                              -- pin the caller-level guards: non-work and
                                  all-approved no-advance decisions (hermetic via
                                  _next_pending_gate), and advance-from-complete
                                  returning an error without mutating cache
                                  (integration against the live daemon, skips when
                                  unreachable).

RED phase: apply_phase_advance does not exist yet, so every test in
TestApplyPhaseAdvance and TestCrossPathParity raises ImportError. That is expected.
"""

from __future__ import annotations

import asyncio
import io
import json
import os
import secrets
import tempfile
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timezone

import pytest
from httpx import ASGITransport, AsyncClient

from tests._daemon import _port

# autouse: pins cwd to a sandbox so `mode set` cannot delete THIS repo's gate artifacts.
from tests.fixtures.session_state import sandbox_cwd, write_bound_gate_token  # noqa: F401

# The real FastAPI app: the module-level route driver below posts to it in-process
# through ASGITransport, exactly as tests/test_session_routes.py::TestSessionAdvancePhase
# does. No patch("writ.server.writ_session", ...) anywhere in this file -- the real
# writ.session cache module runs against whatever WRIT_CACHE_DIR a test's own fixture
# has pointed at.
from writ.server import app

# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

SERVER = f"http://localhost:{_port()}"

PLAN_CONTENT = """\
## Files
- service.py

## Analysis
Implement the thing with care and verify behavior.

## Rules Applied
- TEST-CI-001: all tests pass before merge.

## Capabilities
- [ ] the thing works
"""


def _server_up() -> bool:
    try:
        with urllib.request.urlopen(f"{SERVER}/health", timeout=2):
            return True
    except (urllib.error.URLError, OSError):
        return False


def _post_advance(session_id: str, body: dict) -> dict:
    req = urllib.request.Request(
        f"{SERVER}/session/{session_id}/advance-phase",
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=5) as resp:
        return json.loads(resp.read())


def _cache_path(session_id: str) -> str:
    return os.path.join(tempfile.gettempdir(), f"writ-session-{session_id}.json")


def _token_path(session_id: str) -> str:
    return os.path.join(tempfile.gettempdir(), f"writ-gate-token-{session_id}")


def _make_base_cache() -> dict:
    """Minimal cache that represents a fresh work session at the planning gate."""
    return {
        "mode": "work",
        "current_phase": "planning",
        "gates_approved": [],
        "denial_counts": {},
        "loaded_rule_ids_by_phase": {"planning": ["R1", "R2"]},
        "phase_transitions": [],
    }


async def _drive_advance_via_route(
    session_id: str, *, project_root: str, token: str, confirmation_source: str = "tool",
) -> dict:
    """POST /session/{session_id}/advance-phase against the REAL FastAPI app in-process.

    THE ONE ROUTE DRIVER IN THIS SUITE. TestCrossPathParity._run_path_a and every
    artifact test below call this same function rather than each hand-rolling a
    second copy of the ASGITransport dance -- a second copy is the duplication this
    repo's ## Files-parser and writ_gate_dir incidents both came from (plan.md,
    "Repairing the false parity guard").

    No patch("writ.server.writ_session", ...): the app's own writ_session facade
    (bin/lib/writ-session.py, loaded once at writ.server import time) reads and
    writes through the real writ.session.cache module, which re-resolves
    WRIT_CACHE_DIR from os.environ on every call -- so whatever tmp dir the caller
    already pointed WRIT_CACHE_DIR at is where this advance's cache mutation, the
    validator dispatch, the atomic token claim and (once it exists) the gate
    artifact all actually land. Every part of session_advance_phase runs for real;
    nothing here re-derives target_gate, new_phase or the artifact path by hand.
    """
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        response = await ac.post(
            f"/session/{session_id}/advance-phase",
            json={
                "confirmation_source": confirmation_source,
                "token": token,
                "project_root": project_root,
            },
        )
    return response.json()


def _friction_rows() -> list[dict]:
    """Read back every JSON row logged to WRIT_FRICTION_LOG.

    Mirrors `_friction_rows` in tests/test_project_boundary.py, which reads the same suite-wide
    autouse redirect (tests/conftest.py:91-117 -- _isolate_friction_log points
    WRIT_FRICTION_LOG at a per-test tmp_path unless a test opts out via the
    no_friction_isolation marker, which nothing in this file needs).
    """
    path = os.environ["WRIT_FRICTION_LOG"]
    if not os.path.exists(path):
        return []
    with open(path) as f:
        return [json.loads(line) for line in f if line.strip()]


# ---------------------------------------------------------------------------
# TestApplyPhaseAdvance -- one test per mutated field (hermetic)
# ---------------------------------------------------------------------------

class TestApplyPhaseAdvance:
    """Drive apply_phase_advance on a plain dict and assert each of the five
    cache mutations in isolation. All hermetic: no daemon, no network, no Neo4j."""

    # gates_approved ----------------------------------------------------------

    def test_gates_approved_adds_target_gate(self):
        from writ.session.approval_workflow import apply_phase_advance

        cache = {"phase_transitions": []}
        apply_phase_advance(
            cache, "phase-a", "planning", "testing",
            trigger="user-approved", mode="work",
        )
        assert "phase-a" in cache["gates_approved"]

    def test_gates_approved_preserves_prior_gate(self):
        """Seeding phase-a, then adding test-skeletons must keep both entries sorted."""
        from writ.session.approval_workflow import apply_phase_advance

        cache = {
            "gates_approved": ["phase-a"],
            "phase_transitions": [],
        }
        apply_phase_advance(
            cache, "test-skeletons", "testing", "implementation",
            trigger="user-approved", mode="work",
        )
        assert cache["gates_approved"] == ["phase-a", "test-skeletons"]

    def test_gates_approved_is_idempotent(self):
        """Applying the same gate twice must not duplicate the entry."""
        from writ.session.approval_workflow import apply_phase_advance

        cache = {"gates_approved": ["phase-a"], "phase_transitions": []}
        apply_phase_advance(
            cache, "phase-a", "testing", "testing",
            trigger="user-approved", mode="work",
        )
        assert cache["gates_approved"].count("phase-a") == 1

    def test_gates_approved_result_is_sorted(self):
        """sorted() ordering is part of the contract."""
        from writ.session.approval_workflow import apply_phase_advance

        cache = {"phase_transitions": []}
        apply_phase_advance(
            cache, "phase-a", "planning", "testing",
            trigger="user-approved", mode="work",
        )
        assert cache["gates_approved"] == sorted(cache["gates_approved"])

    # current_phase -----------------------------------------------------------

    def test_current_phase_set_to_new_phase(self):
        from writ.session.approval_workflow import apply_phase_advance

        cache = {"current_phase": "planning", "phase_transitions": []}
        apply_phase_advance(
            cache, "phase-a", "planning", "testing",
            trigger="user-approved", mode="work",
        )
        assert cache["current_phase"] == "testing"

    # denial_counts -----------------------------------------------------------

    def test_denial_counts_pops_target_gate(self):
        """The target gate's denial count is cleared; other gates are untouched."""
        from writ.session.approval_workflow import apply_phase_advance

        cache = {
            "denial_counts": {"phase-a": 3, "other": 1},
            "phase_transitions": [],
        }
        apply_phase_advance(
            cache, "phase-a", "planning", "testing",
            trigger="user-approved", mode="work",
        )
        assert "phase-a" not in cache["denial_counts"]
        assert cache["denial_counts"].get("other") == 1

    def test_denial_counts_absent_key_is_noop(self):
        """Popping a key that was never set must not raise."""
        from writ.session.approval_workflow import apply_phase_advance

        cache = {"denial_counts": {}, "phase_transitions": []}
        apply_phase_advance(
            cache, "phase-a", "planning", "testing",
            trigger="user-approved", mode="work",
        )
        assert "phase-a" not in cache.get("denial_counts", {})

    # loaded_rule_ids_by_phase ------------------------------------------------

    def test_rule_ids_old_phase_moved_to_historical(self):
        """IDs in the old phase must appear in _historical after advance."""
        from writ.session.approval_workflow import apply_phase_advance

        cache = {
            "loaded_rule_ids_by_phase": {"planning": ["R1", "R2"]},
            "phase_transitions": [],
        }
        apply_phase_advance(
            cache, "phase-a", "planning", "testing",
            trigger="user-approved", mode="work",
        )
        by_phase = cache["loaded_rule_ids_by_phase"]
        assert "R1" in by_phase["_historical"]
        assert "R2" in by_phase["_historical"]

    def test_rule_ids_old_phase_bucket_emptied(self):
        from writ.session.approval_workflow import apply_phase_advance

        cache = {
            "loaded_rule_ids_by_phase": {"planning": ["R1", "R2"]},
            "phase_transitions": [],
        }
        apply_phase_advance(
            cache, "phase-a", "planning", "testing",
            trigger="user-approved", mode="work",
        )
        assert cache["loaded_rule_ids_by_phase"]["planning"] == []

    def test_rule_ids_new_phase_bucket_seeded(self):
        """The new phase key must exist (empty list) after advance."""
        from writ.session.approval_workflow import apply_phase_advance

        cache = {
            "loaded_rule_ids_by_phase": {"planning": ["R1"]},
            "phase_transitions": [],
        }
        apply_phase_advance(
            cache, "phase-a", "planning", "testing",
            trigger="user-approved", mode="work",
        )
        assert "testing" in cache["loaded_rule_ids_by_phase"]
        assert cache["loaded_rule_ids_by_phase"]["testing"] == []

    def test_rule_ids_empty_old_phase_leaves_historical_unchanged(self):
        """When the old phase has no IDs, _historical must not grow."""
        from writ.session.approval_workflow import apply_phase_advance

        cache = {
            "loaded_rule_ids_by_phase": {"planning": []},
            "phase_transitions": [],
        }
        apply_phase_advance(
            cache, "phase-a", "planning", "testing",
            trigger="user-approved", mode="work",
        )
        by_phase = cache["loaded_rule_ids_by_phase"]
        assert "_historical" not in by_phase or by_phase.get("_historical") == []

    # phase_transitions -------------------------------------------------------

    def test_phase_transitions_exactly_one_record_appended(self):
        from writ.session.approval_workflow import apply_phase_advance

        cache = {"phase_transitions": []}
        apply_phase_advance(
            cache, "phase-a", "planning", "testing",
            trigger="user-approved", mode="work",
        )
        assert len(cache["phase_transitions"]) == 1

    def test_phase_transitions_record_full_key_set(self):
        """The record must carry from, to, ts, trigger, mode, gate, artifacts_validated."""
        from writ.session.approval_workflow import apply_phase_advance

        cache = {"phase_transitions": []}
        apply_phase_advance(
            cache, "phase-a", "planning", "testing",
            trigger="user-approved", mode="work",
            artifacts_validated=["plan.md"],
        )
        rec = cache["phase_transitions"][0]
        assert rec["from"] == "planning"
        assert rec["to"] == "testing"
        assert rec["trigger"] == "user-approved"
        assert rec["mode"] == "work"
        assert rec["gate"] == "phase-a"
        assert rec["artifacts_validated"] == ["plan.md"]
        assert "ts" in rec

    def test_phase_transitions_ts_is_utc_aware_and_round_trips(self):
        """ts must be a UTC-aware ISO string parseable by datetime.fromisoformat."""
        from writ.session.approval_workflow import apply_phase_advance

        cache = {"phase_transitions": []}
        apply_phase_advance(
            cache, "phase-a", "planning", "testing",
            trigger="user-approved", mode="work",
        )
        ts_str = cache["phase_transitions"][0]["ts"]
        parsed = datetime.fromisoformat(ts_str)
        assert parsed.tzinfo is not None, "ts must be timezone-aware"

    def test_phase_transitions_confirmation_source_absent_when_not_passed(self):
        """When confirmation_source kwarg is omitted (or None), the key must
        not appear in the transition record at all."""
        from writ.session.approval_workflow import apply_phase_advance

        cache = {"phase_transitions": []}
        apply_phase_advance(
            cache, "phase-a", "planning", "testing",
            trigger="user-approved", mode="work",
        )
        rec = cache["phase_transitions"][0]
        assert "confirmation_source" not in rec

    def test_phase_transitions_confirmation_source_present_when_passed(self):
        """When confirmation_source is given a value, it must appear in the record."""
        from writ.session.approval_workflow import apply_phase_advance

        cache = {"phase_transitions": []}
        apply_phase_advance(
            cache, "phase-a", "planning", "testing",
            trigger="user-approved", mode="work",
            confirmation_source="tool",
        )
        rec = cache["phase_transitions"][0]
        assert rec.get("confirmation_source") == "tool"

    def test_phase_transitions_artifacts_validated_defaults_to_empty_list(self):
        """Omitting artifacts_validated must produce [] in the record, not None."""
        from writ.session.approval_workflow import apply_phase_advance

        cache = {"phase_transitions": []}
        apply_phase_advance(
            cache, "phase-a", "planning", "testing",
            trigger="user-approved", mode="work",
        )
        rec = cache["phase_transitions"][0]
        assert rec["artifacts_validated"] == []


# ---------------------------------------------------------------------------
# TestCrossPathParity -- the regression keystone (hermetic: no live daemon, no
# network -- Path A drives the real FastAPI app in-process via
# httpx.ASGITransport, which never opens a socket)
#
# Drives BOTH REAL advance callers through a full work cycle (planning -> testing
# -> implementation) against separate but identically-seeded caches, then asserts
# the five mutated fields agree modulo Path A's additive confirmation_source
# and per-record ts timestamps.
#
# This test is the regression guard against field divergence reappearing between
# the two advance paths. Path A used to be an in-process simulation of the route
# (calling apply_phase_advance directly in a loop), which compared the real CLI
# against a hand-written model and was structurally blind to everything the route
# does or omits around the shared unit -- exactly how a missing filesystem side
# effect (the gate artifact) survived. Path A now drives the real route
# (session_advance_phase) through _drive_advance_via_route, so a future change
# that re-introduces a per-path mutation outside apply_phase_advance, or that
# omits a route-only side effect entirely, fails here before it reaches
# production.
# ---------------------------------------------------------------------------

class TestCrossPathParity:
    """Cross-path regression guard: both advance paths produce the same five
    cache fields after planning -> testing -> implementation. Path A drives the
    real HTTP route (POST /session/{id}/advance-phase) via
    _drive_advance_via_route (httpx.AsyncClient over ASGITransport(app=app), no
    patch("writ.server.writ_session", ...)); Path B drives the real CLI
    (cmd_advance_phase). Neither path is simulated."""

    @pytest.fixture()
    def cache_dir(self, tmp_path, monkeypatch):
        monkeypatch.setenv("WRIT_CACHE_DIR", str(tmp_path))
        return tmp_path

    @pytest.fixture()
    def project_root(self, tmp_path):
        root = tmp_path / "proj"
        root.mkdir()
        (root / ".git").mkdir()
        (root / ".claude" / "gates").mkdir(parents=True)
        (root / "plan.md").write_text(PLAN_CONTENT)
        (root / "tests").mkdir()
        (root / "tests" / "test_service.py").write_text("def test_service():\n    pass\n")
        return root

    def _make_seeded_cache(self) -> dict:
        """Produce an identical starting cache for both paths."""
        return {
            "mode": "work",
            "current_phase": "planning",
            "gates_approved": [],
            "denial_counts": {"phase-a": 0},
            "loaded_rule_ids_by_phase": {"planning": ["SEED-R1"]},
            "phase_transitions": [],
        }

    def _run_path_b(self, cache_dir, project_root, monkeypatch, capsys) -> tuple[dict, str]:
        """Advance Path B (cmd_advance_phase) through both gates and return
        (final cache state, session id). Mirrors TestGoldenWorkCycle in
        test_mode_engine.py. The session id travels back so artifact-path
        assertions (this session's own <root>/.claude/gates/<sid>/*.approved)
        can be built without re-deriving it."""
        import importlib.util

        helper_path = os.path.join(
            os.path.dirname(__file__), os.pardir, "bin", "lib", "writ-session.py"
        )
        spec = importlib.util.spec_from_file_location("writ_session_path_b", helper_path)
        ws = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(ws)

        session_id = f"parity-pathb-{uuid.uuid4().hex[:8]}"
        ws.cmd_mode(session_id, "set", "work")
        # Register the rule PLAN_CONTENT cites so the phase-a citation validator
        # (_validate_phase_a -> _validate_citations) sees it as loaded and does not
        # reject the advance as a hallucinated citation. Mirrors TestGoldenWorkCycle.
        ws.cmd_update(session_id, ["--add-rules", json.dumps(["TEST-CI-001"])])
        # Inject the seeded cache state onto the blank session.
        base = self._make_seeded_cache()
        existing = ws._read_cache(session_id)
        existing["denial_counts"] = base["denial_counts"]
        existing["loaded_rule_ids_by_phase"] = base["loaded_rule_ids_by_phase"]
        ws._write_cache(session_id, existing)

        def _advance_once():
            # A BOUND token (gate + plan fingerprint) derived from the cache as the
            # production mint derives it, re-read per advance so the second call binds
            # test-skeletons rather than the phase-a gate the first one spent.
            token = write_bound_gate_token(session_id, secrets.token_hex(16))
            capsys.readouterr()
            monkeypatch.setattr("sys.stdin", io.StringIO("approved"))
            ws.cmd_advance_phase(session_id, str(project_root), token)
            capsys.readouterr()  # drain output

        _advance_once()  # planning -> testing (phase-a gate)
        _advance_once()  # testing  -> implementation (test-skeletons gate)

        return ws._read_cache(session_id), session_id

    def _run_path_a(self, cache_dir, project_root) -> tuple[dict, str]:
        """Advance Path A -- the REAL HTTP route -- through both gates and return
        (final cache state, session id). Drives _drive_advance_via_route, the
        module-level ASGI helper, once per gate: the route's validator dispatch,
        plan-binding refusal, atomic token claim, apply_phase_advance under
        mutate_cache, and both telemetry emits all run for real. Nothing here
        re-derives target_gate or new_phase by hand -- that hand-derivation is
        exactly the in-process simulation this rewrite replaces."""
        import importlib.util

        helper_path = os.path.join(
            os.path.dirname(__file__), os.pardir, "bin", "lib", "writ-session.py"
        )
        spec = importlib.util.spec_from_file_location("writ_session_path_a", helper_path)
        ws = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(ws)

        session_id = f"parity-patha-{uuid.uuid4().hex[:8]}"
        ws.cmd_mode(session_id, "set", "work")
        # Register the rule PLAN_CONTENT cites, exactly as Path B does, so the
        # phase-a citation validator the route now dispatches through
        # _GATE_VALIDATORS sees it as loaded and does not reject the advance as a
        # hallucinated citation.
        ws.cmd_update(session_id, ["--add-rules", json.dumps(["TEST-CI-001"])])
        # Inject the seeded cache state onto the blank session -- identical setup
        # to Path B, so only the advance MECHANISM differs between the two paths.
        base = self._make_seeded_cache()
        existing = ws._read_cache(session_id)
        existing["denial_counts"] = base["denial_counts"]
        existing["loaded_rule_ids_by_phase"] = base["loaded_rule_ids_by_phase"]
        ws._write_cache(session_id, existing)

        def _advance_once() -> dict:
            # A fresh BOUND token per advance, derived from the cache exactly as
            # write_bound_gate_token derives it for Path B, so the second call
            # binds test-skeletons rather than the phase-a gate the first spent.
            token = write_bound_gate_token(session_id, secrets.token_hex(16))
            return asyncio.run(
                _drive_advance_via_route(
                    session_id,
                    project_root=str(project_root),
                    token=token,
                    confirmation_source="tool",
                )
            )

        _advance_once()  # planning -> testing (phase-a gate)
        _advance_once()  # testing  -> implementation (test-skeletons gate)

        return ws._read_cache(session_id), session_id

    def test_gates_approved_equal(self, cache_dir, project_root, monkeypatch, capsys):
        from writ.session.approval_workflow import apply_phase_advance  # noqa: F401 -- import check

        cache_b, _sid_b = self._run_path_b(cache_dir, project_root, monkeypatch, capsys)
        cache_a, _sid_a = self._run_path_a(cache_dir, project_root)
        assert cache_b["gates_approved"] == ["phase-a", "test-skeletons"]
        assert cache_a["gates_approved"] == ["phase-a", "test-skeletons"]

    def test_current_phase_equal(self, cache_dir, project_root, monkeypatch, capsys):
        from writ.session.approval_workflow import apply_phase_advance  # noqa: F401

        cache_b, _sid_b = self._run_path_b(cache_dir, project_root, monkeypatch, capsys)
        cache_a, _sid_a = self._run_path_a(cache_dir, project_root)
        assert cache_b["current_phase"] == "implementation"
        assert cache_a["current_phase"] == "implementation"

    def test_denial_counts_equal(self, cache_dir, project_root, monkeypatch, capsys):
        from writ.session.approval_workflow import apply_phase_advance  # noqa: F401

        cache_b, _sid_b = self._run_path_b(cache_dir, project_root, monkeypatch, capsys)
        cache_a, _sid_a = self._run_path_a(cache_dir, project_root)
        # Both paths clear phase-a; neither should retain it.
        assert "phase-a" not in cache_b.get("denial_counts", {})
        assert "phase-a" not in cache_a.get("denial_counts", {})
        assert "test-skeletons" not in cache_b.get("denial_counts", {})
        assert "test-skeletons" not in cache_a.get("denial_counts", {})

    def test_loaded_rule_ids_by_phase_equal(self, cache_dir, project_root, monkeypatch, capsys):
        from writ.session.approval_workflow import apply_phase_advance  # noqa: F401

        cache_b, _sid_b = self._run_path_b(cache_dir, project_root, monkeypatch, capsys)
        cache_a, _sid_a = self._run_path_a(cache_dir, project_root)

        for cache, label in [(cache_b, "Path B"), (cache_a, "Path A")]:
            by_phase = cache.get("loaded_rule_ids_by_phase", {})
            assert by_phase.get("planning") == [], f"{label}: planning bucket must be empty"
            assert "implementation" in by_phase, f"{label}: implementation bucket must be seeded"
            # SEED-R1 was in planning -> must be in _historical for Path A (Path B
            # starts with the same seed injected before cmd_advance_phase).
            # (Path B only has the seed if it was injected; relax to a subset check.)

    def test_phase_transitions_records_match_on_structural_fields(self, cache_dir, project_root, monkeypatch, capsys):
        """The transition records for both paths must agree on from/to/trigger/mode/gate/
        artifacts_validated. ts is excluded (timestamp differs). confirmation_source is
        allowed only on Path A records (additive, not a parity violation).

        Path A now runs a real `mode set` before its two advances (it drives the real
        route rather than simulating apply_phase_advance calls in a loop), so its
        phase_transitions list carries the mode-set record too, exactly as Path B's
        already does. Both sides' gate-advance records are therefore found with the
        SAME `r.get("gate") is not None` predicate rather than asserting a bare
        transition count, so a mode-set record on either side is never mistaken for a
        missing gate record.

        This assertion is the regression guard: any future change that re-routes a
        mutation outside apply_phase_advance will produce a record with a missing or
        wrong structural field and fail here.
        """
        from writ.session.approval_workflow import apply_phase_advance  # noqa: F401

        cache_b, _sid_b = self._run_path_b(cache_dir, project_root, monkeypatch, capsys)
        cache_a, _sid_a = self._run_path_a(cache_dir, project_root)

        transitions_b = cache_b.get("phase_transitions", [])
        transitions_a = cache_a.get("phase_transitions", [])

        STRUCTURAL_KEYS = ("from", "to", "trigger", "mode", "gate", "artifacts_validated")

        # Compare the two gate-advance records element-wise. Both paths may carry
        # extra, non-gate records (mode-set); the SAME predicate finds the
        # gate-advance ones on both sides.
        gate_records_b = [r for r in transitions_b if r.get("gate") is not None]
        gate_records_a = [r for r in transitions_a if r.get("gate") is not None]

        assert len(gate_records_b) == 2, f"Path B: expected 2 gate records, got {gate_records_b}"
        assert len(gate_records_a) == 2, f"Path A: expected 2 gate records, got {gate_records_a}"

        for i, (rec_b, rec_a) in enumerate(zip(gate_records_b, gate_records_a)):
            for key in STRUCTURAL_KEYS:
                assert rec_b.get(key) == rec_a.get(key), (
                    f"Transition record {i}: field '{key}' differs between paths. "
                    f"Path B={rec_b.get(key)!r}, Path A={rec_a.get(key)!r}"
                )
            # confirmation_source is allowed in Path A but must be ABSENT in Path B.
            assert "confirmation_source" not in rec_b, (
                f"Transition record {i}: Path B must not carry confirmation_source; got {rec_b}"
            )
            if "confirmation_source" in rec_a:
                assert rec_a["confirmation_source"] == "tool"

    # -- Gate artifact assertions (plan.md tests 1 and 2) --------------------

    def test_gate_artifact_written_by_both_paths(self, cache_dir, project_root, monkeypatch, capsys):
        """After a full two-gate cycle on each path, phase-a.approved and
        test-skeletons.approved exist under <project_root>/.claude/gates/<sid>/ for
        BOTH the route's session and the CLI's session, and each file's contents
        are its own session id plus a newline.

        Reddened on the route side by deleting the write_gate_artifact call from
        session_advance_phase, which is TODAY's code -- this assertion is RED
        before the change and GREEN after, rather than only mutation-proved.
        Reddened on the CLI side by deleting the call from cmd_advance_phase.
        Reddened on the contents half by writing an empty file or the gate name
        instead of the session id.
        """
        cache_b, session_b = self._run_path_b(cache_dir, project_root, monkeypatch, capsys)
        cache_a, session_a = self._run_path_a(cache_dir, project_root)

        for label, sid in (("Path B (CLI)", session_b), ("Path A (route)", session_a)):
            for gate in ("phase-a", "test-skeletons"):
                artifact = project_root / ".claude" / "gates" / sid / f"{gate}.approved"
                assert artifact.exists(), f"{label}: {artifact} must exist after a full advance cycle"
                contents = artifact.read_text()
                assert contents == f"{sid}\n", (
                    f"{label}: artifact contents must be the session id plus a "
                    f"newline, got {contents!r}"
                )

    def test_second_gate_does_not_remove_the_first(self, cache_dir, project_root, monkeypatch, capsys):
        """The same two-gate run asserts both artifacts are present TOGETHER after
        the second advance, for both paths.

        Reddened by making the writer clear its directory before writing
        (shutil.rmtree then makedirs) instead of makedirs(exist_ok=True).
        """
        cache_b, session_b = self._run_path_b(cache_dir, project_root, monkeypatch, capsys)
        cache_a, session_a = self._run_path_a(cache_dir, project_root)

        for label, sid in (("Path B (CLI)", session_b), ("Path A (route)", session_a)):
            gate_dir_path = project_root / ".claude" / "gates" / sid
            first = gate_dir_path / "phase-a.approved"
            second = gate_dir_path / "test-skeletons.approved"
            assert first.exists(), f"{label}: phase-a.approved must survive the second advance"
            assert second.exists(), f"{label}: test-skeletons.approved must exist after the second advance"


# ---------------------------------------------------------------------------
# TestGateArtifactResolvedRoot -- plan.md test 3
# ---------------------------------------------------------------------------

class TestGateArtifactResolvedRoot:
    """The artifact write must land under the root the LIVE ROUTE resolved from the
    request (project_root/cwd), never under cache['project_root'] -- a value stamped
    once at mode-set time from the process cwd and not re-derived per advance."""

    def test_route_writes_under_resolved_root_not_cache_root(self, tmp_path, monkeypatch):
        """Two roots. The cache is seeded with project_root=root_C (which carries
        its own plan.md, so the fingerprint the mint and the claim both derive from
        it is a real digest rather than the empty-string degenerate case), while
        the request body sends project_root=root_R, the root that carries the
        plan.md and test file the validators actually judge. The artifact must
        exist under root_R/.claude/gates/<sid>/ and must NOT exist anywhere under
        root_C.

        Reddened by passing cache.get("project_root") to the writer in gate.py
        instead of the resolved project_root; both halves flip.
        """
        import importlib.util

        monkeypatch.setenv("WRIT_CACHE_DIR", str(tmp_path / "cache"))

        root_c = tmp_path / "root_c"
        root_c.mkdir()
        (root_c / ".git").mkdir()
        (root_c / "plan.md").write_text(PLAN_CONTENT)

        root_r = tmp_path / "root_r"
        root_r.mkdir()
        (root_r / ".git").mkdir()
        (root_r / "plan.md").write_text(PLAN_CONTENT)
        (root_r / "tests").mkdir()
        (root_r / "tests" / "test_service.py").write_text("def test_service():\n    pass\n")

        helper_path = os.path.join(
            os.path.dirname(__file__), os.pardir, "bin", "lib", "writ-session.py"
        )
        spec = importlib.util.spec_from_file_location("writ_session_root_mismatch", helper_path)
        ws = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(ws)

        session_id = f"root-mismatch-{uuid.uuid4().hex[:8]}"
        ws.cmd_mode(session_id, "set", "work")
        ws.cmd_update(session_id, ["--add-rules", json.dumps(["TEST-CI-001"])])
        # Override the cache's project_root (stamped from cwd by cmd_mode) to
        # root_C, the value the mint and the claim will fingerprint.
        cache = ws._read_cache(session_id)
        cache["project_root"] = str(root_c)
        ws._write_cache(session_id, cache)

        token = write_bound_gate_token(session_id, secrets.token_hex(16))
        result = asyncio.run(
            _drive_advance_via_route(
                session_id,
                project_root=str(root_r),
                token=token,
                confirmation_source="tool",
            )
        )
        assert result.get("phase") == "testing", f"the advance did not go through: {result}"

        artifact_r = root_r / ".claude" / "gates" / session_id / "phase-a.approved"
        artifact_c = root_c / ".claude" / "gates" / session_id / "phase-a.approved"
        assert artifact_r.exists(), (
            f"artifact must exist under the resolved root {root_r}, not {root_c}"
        )
        assert not artifact_c.exists(), (
            f"artifact must NOT be written under cache['project_root'] ({root_c}) "
            "when it differs from the root the route resolved"
        )


# ---------------------------------------------------------------------------
# TestWriteGateArtifactHermeticRefusals -- plan.md tests 4, 5, 6
# ---------------------------------------------------------------------------

class TestWriteGateArtifactHermeticRefusals:
    """Direct, hermetic calls to write_gate_artifact -- no daemon, no CLI, no
    session cache. write_gate_artifact does not exist yet (plan.md's Files
    section: approval_workflow.py adds this writer), so every test here fails on
    ImportError/AttributeError today, which is the correct RED state."""

    def test_invalid_session_component_writes_nothing(self, tmp_path):
        """A hermetic direct call with a traversal session id returns False and
        leaves no new path anywhere under the root or its parent.

        Reddened by removing the `if session_gate_dir and gate_file:` guard so the
        writer joins and creates the traversal path.
        """
        from writ.session.approval_workflow import write_gate_artifact

        root = tmp_path / "project"
        root.mkdir()

        result = write_gate_artifact(str(root), "../evil", "phase-a", mode="work")

        assert result is False
        assert list(root.iterdir()) == [], (
            "an invalid session component must create nothing under the root"
        )
        assert not (tmp_path / "evil").exists(), (
            "a traversal component must not escape to the parent either"
        )

    def test_invalid_session_component_logs_refusal(self, tmp_path):
        """The same call emits exactly one gate_artifact_refused row carrying
        gate="phase-a" and had_root=true, read from the WRIT_FRICTION_LOG file the
        suite's autouse redirect points at.

        Reddened by deleting the else-branch emit.
        """
        from writ.session.approval_workflow import write_gate_artifact

        root = tmp_path / "project"
        root.mkdir()

        write_gate_artifact(str(root), "../evil", "phase-a", mode="work")

        refusals = [r for r in _friction_rows() if r.get("event") == "gate_artifact_refused"]
        assert len(refusals) == 1, f"expected exactly one refusal row, got {refusals}"
        assert refusals[0].get("gate") == "phase-a"
        assert refusals[0].get("had_root") is True

    def test_empty_project_root_writes_nothing_and_records_had_root_false(self, tmp_path):
        """The boundary the route cannot reach today: an empty project_root
        returns False and its refusal row carries had_root=false.

        Reddened by hardcoding had_root=True or by dropping the field.
        """
        from writ.session.approval_workflow import write_gate_artifact

        result = write_gate_artifact("", "some-session", "phase-a", mode="work")

        assert result is False
        refusals = [r for r in _friction_rows() if r.get("event") == "gate_artifact_refused"]
        assert len(refusals) == 1, f"expected exactly one refusal row, got {refusals}"
        assert refusals[0].get("had_root") is False


# ---------------------------------------------------------------------------
# TestGateArtifactWriteFailure -- review finding, not in the original plan
#
# The plan's refusal tests all exercise the LOCATOR refusal (an empty gate_dir or
# gate_artifact_path), which returns before any I/O. Nothing covered the write
# itself failing. That matters more on the route than it did on the CLI: the
# advance is durably committed and the token already spent by the time the writer
# runs, so an exception escaping _apply would fail the request for an advance that
# actually happened, and would skip the phase_advance, decision-capture and
# playbook_step_complete emits that follow it. The route's own sibling side effect
# (capture_decision_at_approve) already catches and logs for this reason.
#
# The fault here is REAL, not mocked: .claude/gates is created as a FILE, so
# os.makedirs raises NotADirectoryError/FileExistsError the way a read-only or
# permission-restricted project directory would.
# ---------------------------------------------------------------------------

class TestGateArtifactWriteFailure:
    """A failed write is a refusal, not an exception, on both paths."""

    def test_write_failure_returns_false_and_does_not_raise(self, tmp_path):
        """makedirs fails because .claude/gates is a file. The writer returns
        False rather than propagating OSError.

        Reddened by removing the try/except around the makedirs and write.
        """
        from writ.session.approval_workflow import write_gate_artifact

        root = tmp_path / "project"
        (root / ".claude").mkdir(parents=True)
        (root / ".claude" / "gates").write_text("not a directory\n")

        result = write_gate_artifact(str(root), "some-session", "phase-a", mode="work")

        assert result is False
        assert (root / ".claude" / "gates").is_file(), (
            "the blocking file must be left as it was, never replaced"
        )

    def test_write_failure_logs_a_distinguishable_refusal(self, tmp_path):
        """The row separates an I/O fault from a bad path component, so a reader
        of the log can tell "this session id was rejected" from "this disk would
        not take the write".

        Reddened by reusing reason="invalid_session_or_gate_path_component" for
        the I/O branch, or by dropping the emit.
        """
        from writ.session.approval_workflow import write_gate_artifact

        root = tmp_path / "project"
        (root / ".claude").mkdir(parents=True)
        (root / ".claude" / "gates").write_text("not a directory\n")

        write_gate_artifact(str(root), "some-session", "phase-a", mode="work")

        refusals = [r for r in _friction_rows() if r.get("event") == "gate_artifact_refused"]
        assert len(refusals) == 1, f"expected exactly one refusal row, got {refusals}"
        assert refusals[0].get("reason") == "write_failed", (
            "an I/O fault must not be logged as an invalid path component"
        )
        assert refusals[0].get("gate") == "phase-a"

    def test_route_advance_survives_a_failed_artifact_write(self, tmp_path, monkeypatch):
        """The invariant the plan states, proved against the mechanism it did not
        enumerate: the route's advance still succeeds and the cache still records
        the gate when the artifact write itself fails.

        Reddened by removing the try/except: the OSError escapes _apply through
        asyncio.to_thread and the advance the caller sees fails.
        """
        import importlib.util

        monkeypatch.setenv("WRIT_CACHE_DIR", str(tmp_path / "cache"))

        root = tmp_path / "proj"
        root.mkdir()
        (root / ".git").mkdir()
        (root / "plan.md").write_text(PLAN_CONTENT)
        (root / "tests").mkdir()
        (root / "tests" / "test_service.py").write_text("def test_service():\n    pass\n")
        (root / ".claude").mkdir()
        (root / ".claude" / "gates").write_text("not a directory\n")

        helper_path = os.path.join(
            os.path.dirname(__file__), os.pardir, "bin", "lib", "writ-session.py"
        )
        spec = importlib.util.spec_from_file_location("writ_session_write_fail", helper_path)
        ws = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(ws)

        session_id = f"write-fail-{uuid.uuid4().hex[:8]}"
        ws.cmd_mode(session_id, "set", "work")
        ws.cmd_update(session_id, ["--add-rules", json.dumps(["TEST-CI-001"])])

        token = write_bound_gate_token(session_id, secrets.token_hex(16))
        result = asyncio.run(
            _drive_advance_via_route(
                session_id, project_root=str(root), token=token, confirmation_source="tool",
            )
        )

        assert result.get("phase") == "testing", (
            f"a failed artifact write must not fail the advance; got {result}"
        )
        assert "phase-a" in ws._read_cache(session_id).get("gates_approved", []), (
            "the committed advance must still be recorded in the cache"
        )


# ---------------------------------------------------------------------------
# TestRefusedArtifactDoesNotFailAdvance -- plan.md test 7 (capability 9)
# ---------------------------------------------------------------------------

class TestRefusedArtifactDoesNotFailAdvance:
    """Capability 9, corrected from the plan's hedge between the route and the
    CLI: an invalid GATE-or-SESSION path component is not reachable through the
    live route (both work-mode gates in _GATE_VALIDATORS have names that always
    pass is_valid_session_component), so this is proved at the CLI level, where
    the session id used for the WHOLE advance is the component that fails
    validation -- the CLI never validates session_id shape for its cache or token
    file paths, only gate_dir/gate_artifact_path do, at the last step."""

    @pytest.fixture()
    def cache_dir(self, tmp_path, monkeypatch):
        monkeypatch.setenv("WRIT_CACHE_DIR", str(tmp_path))
        return tmp_path

    @pytest.fixture()
    def project_root(self, tmp_path):
        root = tmp_path / "proj"
        root.mkdir()
        (root / ".git").mkdir()
        (root / ".claude" / "gates").mkdir(parents=True)
        (root / "plan.md").write_text(PLAN_CONTENT)
        (root / "tests").mkdir()
        (root / "tests" / "test_service.py").write_text("def test_service():\n    pass\n")
        return root

    def test_refused_artifact_does_not_fail_the_advance(self, cache_dir, project_root, monkeypatch, capsys):
        """Drive cmd_advance_phase with a session id of ".." (a valid filename
        component embedded in "writ-session-...json", so every cache/token file
        read and write succeeds normally; it is rejected ONLY by
        is_valid_session_component, which is the one check gate_dir/
        gate_artifact_path apply before write_gate_artifact ever joins a path).
        The advance JSON must still report advanced: true, and the cache must
        still list the gate in gates_approved.

        Reddened by making the writer raise instead of returning False.
        """
        import importlib.util

        helper_path = os.path.join(
            os.path.dirname(__file__), os.pardir, "bin", "lib", "writ-session.py"
        )
        spec = importlib.util.spec_from_file_location(
            "writ_session_refused_artifact", helper_path
        )
        ws = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(ws)

        session_id = ".."
        ws.cmd_mode(session_id, "set", "work")
        ws.cmd_update(session_id, ["--add-rules", json.dumps(["TEST-CI-001"])])

        token = write_bound_gate_token(session_id, secrets.token_hex(16))
        capsys.readouterr()
        monkeypatch.setattr("sys.stdin", io.StringIO("approved"))
        ws.cmd_advance_phase(session_id, str(project_root), token)
        out = capsys.readouterr().out.strip()
        result = json.loads(out)

        assert result.get("advanced") is True, (
            f"a refused artifact write must not fail the advance itself: {result}"
        )
        cache = ws._read_cache(session_id)
        assert "phase-a" in cache.get("gates_approved", []), (
            "the gate must still be recorded as approved even though the artifact "
            "write was refused"
        )


# ---------------------------------------------------------------------------
# TestTerminalAndNonWorkNoAdvance
# ---------------------------------------------------------------------------

class TestTerminalAndNonWorkNoAdvance:
    """Pin the caller-level no-advance guards.

    The decision NOT to call apply_phase_advance lives in the callers, not in the
    function itself. These tests validate the decision mechanism at the level where
    it actually exists.
    """

    # Hermetic: _next_pending_gate returns None (the no-advance signal) ----------

    def test_next_pending_gate_returns_none_for_non_work_mode(self):
        """_next_pending_gate returning None is the signal both callers use to skip
        the advance. Non-work modes (conversation, debug, review) always produce None."""
        from writ.session.mode_engine import _next_pending_gate

        for mode in ("conversation", "debug", "review"):
            cache = {"mode": mode, "gates_approved": []}
            assert _next_pending_gate(cache) is None, (
                f"_next_pending_gate must return None for mode={mode!r}"
            )

    def test_next_pending_gate_returns_none_for_none_mode(self):
        from writ.session.mode_engine import _next_pending_gate

        cache = {"gates_approved": []}
        assert _next_pending_gate(cache) is None

    def test_next_pending_gate_returns_none_when_all_gates_approved(self):
        """When both work-mode gates are approved, _next_pending_gate returns None --
        the signal for both callers to return a no-advance result rather than call
        apply_phase_advance and wrongly advance to complete."""
        from writ.session.mode_engine import _next_pending_gate

        cache = {
            "mode": "work",
            "gates_approved": ["phase-a", "test-skeletons"],
        }
        assert _next_pending_gate(cache) is None

    def test_next_pending_gate_returns_phase_a_for_fresh_work_session(self):
        """Positive control: a fresh work session returns 'phase-a'."""
        from writ.session.mode_engine import _next_pending_gate

        cache = {"mode": "work", "gates_approved": []}
        assert _next_pending_gate(cache) == "phase-a"

    def test_next_pending_gate_returns_test_skeletons_after_phase_a(self):
        """After phase-a is approved, the next pending gate is test-skeletons."""
        from writ.session.mode_engine import _next_pending_gate

        cache = {"mode": "work", "gates_approved": ["phase-a"]}
        assert _next_pending_gate(cache) == "test-skeletons"

    # Integration: advance-from-complete via the live daemon -------------------

    def test_advance_from_complete_returns_error_and_does_not_mutate_cache(self):
        """The route-level terminal guard: a session with current_phase='complete'
        must return an error signal without mutating the cache. Skips when the
        daemon is unreachable. This mirrors test_phase_machine_reset.py's live case.
        """
        if not _server_up():
            pytest.skip("Writ server unreachable")

        sid = f"terminal-guard-{uuid.uuid4().hex[:8]}"
        token = uuid.uuid4().hex

        # Seed a throwaway session cache at the complete phase.
        cache_seed = {
            "mode": "work",
            "current_phase": "complete",
            "gates_approved": ["phase-a", "test-skeletons"],
            "denial_counts": {},
            "phase_transitions": [],
        }
        with open(_cache_path(sid), "w") as f:
            json.dump(cache_seed, f)
        with open(_token_path(sid), "w") as f:
            f.write(token)

        try:
            result = _post_advance(sid, {"confirmation_source": "tool", "token": token})

            # The response must signal an error or a complete state, not a real advance.
            is_error = "error" in result
            is_complete_signal = result.get("phase") == "complete" or result.get("from") == "complete"
            assert is_error or is_complete_signal, (
                f"advance-from-complete must return an error or complete signal; got {result}"
            )
            # The cache must not have advanced past complete.
            with open(_cache_path(sid)) as f:
                after = json.load(f)
            assert after.get("current_phase") == "complete", (
                f"cache current_phase must remain 'complete' after a refused terminal advance; "
                f"got {after.get('current_phase')!r}"
            )
        finally:
            for p in (_cache_path(sid), _token_path(sid)):
                try:
                    os.remove(p)
                except OSError:
                    pass
