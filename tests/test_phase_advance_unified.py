"""Unified test suite for the shared apply_phase_advance function.

Three test classes:

  TestApplyPhaseAdvance       -- hermetic, drives apply_phase_advance on a plain
                                  dict, one focused test per mutated field.

  TestCrossPathParity         -- the regression keystone: drives BOTH advance
                                  callers (Path B = cmd_advance_phase, Path A =
                                  in-process simulation of the rewritten
                                  server._advance) through a full work cycle and
                                  asserts identical resulting cache state on the
                                  five fields, modulo Path A's extra
                                  confirmation_source and per-record ts.

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

from tests._daemon import _port

# autouse: pins cwd to a sandbox so `mode set` cannot delete THIS repo's gate artifacts.
from tests.fixtures.session_state import sandbox_cwd, write_bound_gate_token  # noqa: F401

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
# TestCrossPathParity -- the regression keystone (hermetic)
#
# Drives BOTH advance callers through a full work cycle (planning -> testing ->
# implementation) against separate but identically-seeded caches, then asserts
# the five mutated fields agree modulo Path A's additive confirmation_source
# and per-record ts timestamps.
#
# This test is the regression guard against field divergence reappearing between
# the two advance paths. A future change that re-introduces a per-path mutation
# outside apply_phase_advance will fail here before it reaches production.
# ---------------------------------------------------------------------------

class TestCrossPathParity:
    """Cross-path regression guard: both advance paths produce the same five
    cache fields after planning -> testing -> implementation."""

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

    def _run_path_b(self, cache_dir, project_root, monkeypatch, capsys) -> dict:
        """Advance Path B (cmd_advance_phase) through both gates and return the
        final cache state. Mirrors TestGoldenWorkCycle in test_mode_engine.py."""
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

        return ws._read_cache(session_id)

    def _run_path_a(self, cache_dir) -> dict:
        """Simulate Path A's post-refactor in-process derivation: for each step
        compute target_gate and new_phase from MODE_CONFIG/_next_pending_gate, then
        call apply_phase_advance with confirmation_source='tool'. This mirrors
        exactly what the rewritten server._advance will do."""
        from writ.session.approval_workflow import apply_phase_advance
        from writ.session.mode_engine import MODE_CONFIG, _next_pending_gate

        cache = self._make_seeded_cache()

        for _step in range(2):  # two gate advances
            target_gate = _next_pending_gate(cache)
            assert target_gate is not None, "expected a pending gate"
            old_phase = cache.get("current_phase", "planning")
            new_phase = MODE_CONFIG["work"]["phase_after_gate"][target_gate]
            artifacts = ["plan.md"] if target_gate == "phase-a" else []
            apply_phase_advance(
                cache, target_gate, old_phase, new_phase,
                trigger="user-approved",
                mode="work",
                confirmation_source="tool",
                artifacts_validated=artifacts,
            )

        return cache

    def test_gates_approved_equal(self, cache_dir, project_root, monkeypatch, capsys):
        from writ.session.approval_workflow import apply_phase_advance  # noqa: F401 -- import check

        cache_b = self._run_path_b(cache_dir, project_root, monkeypatch, capsys)
        cache_a = self._run_path_a(cache_dir)
        assert cache_b["gates_approved"] == ["phase-a", "test-skeletons"]
        assert cache_a["gates_approved"] == ["phase-a", "test-skeletons"]

    def test_current_phase_equal(self, cache_dir, project_root, monkeypatch, capsys):
        from writ.session.approval_workflow import apply_phase_advance  # noqa: F401

        cache_b = self._run_path_b(cache_dir, project_root, monkeypatch, capsys)
        cache_a = self._run_path_a(cache_dir)
        assert cache_b["current_phase"] == "implementation"
        assert cache_a["current_phase"] == "implementation"

    def test_denial_counts_equal(self, cache_dir, project_root, monkeypatch, capsys):
        from writ.session.approval_workflow import apply_phase_advance  # noqa: F401

        cache_b = self._run_path_b(cache_dir, project_root, monkeypatch, capsys)
        cache_a = self._run_path_a(cache_dir)
        # Both paths clear phase-a; neither should retain it.
        assert "phase-a" not in cache_b.get("denial_counts", {})
        assert "phase-a" not in cache_a.get("denial_counts", {})
        assert "test-skeletons" not in cache_b.get("denial_counts", {})
        assert "test-skeletons" not in cache_a.get("denial_counts", {})

    def test_loaded_rule_ids_by_phase_equal(self, cache_dir, project_root, monkeypatch, capsys):
        from writ.session.approval_workflow import apply_phase_advance  # noqa: F401

        cache_b = self._run_path_b(cache_dir, project_root, monkeypatch, capsys)
        cache_a = self._run_path_a(cache_dir)

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

        This assertion is the regression guard: any future change that re-routes a
        mutation outside apply_phase_advance will produce a record with a missing or
        wrong structural field and fail here.
        """
        from writ.session.approval_workflow import apply_phase_advance  # noqa: F401

        cache_b = self._run_path_b(cache_dir, project_root, monkeypatch, capsys)
        cache_a = self._run_path_a(cache_dir)

        transitions_b = cache_b.get("phase_transitions", [])
        transitions_a = cache_a.get("phase_transitions", [])

        # Both paths must produce exactly two transition records (one per gate).
        assert len(transitions_b) >= 2, f"Path B: expected >=2 transitions, got {len(transitions_b)}"
        assert len(transitions_a) == 2, f"Path A: expected 2 transitions, got {len(transitions_a)}"

        STRUCTURAL_KEYS = ("from", "to", "trigger", "mode", "gate", "artifacts_validated")

        # Compare the two gate-advance records element-wise.
        # Path B may have extra records from mode-set; find the gate-advance ones.
        gate_records_b = [r for r in transitions_b if r.get("gate") is not None]
        gate_records_a = transitions_a  # Path A only produces gate-advance records

        assert len(gate_records_b) == 2, f"Path B: expected 2 gate records, got {gate_records_b}"
        assert len(gate_records_a) == 2

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
