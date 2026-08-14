"""Phase-machine defensive contracts for fresh-task transitions.

Two issues hit during the Phase 6j session:

1. When a new task starts, current_phase often carries over from the
   prior task (e.g. `implementation` or `complete`). The agent calls
   /advance-phase on the user's "approved" and inadvertently advances
   the prior task's phase instead of starting a fresh planning cycle.

2. Advancing from `complete` is currently a silent no-op (the phase
   machine clamps at the last index). Callers get no signal that they
   need to reset.

This module pins:
- `/advance-phase` from `complete` must return an explicit error so the
  caller knows to reset.
- `_mode_set` continues to reset current_phase to the initial phase for
  the mode (already-existing contract; pinned here so it doesn't
  regress).
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile

import pytest
from pathlib import Path

from tests._daemon import _port

# autouse: pins cwd to a sandbox so `mode set` cannot delete THIS repo's gate artifacts.
from tests.fixtures.session_state import sandbox_cwd  # noqa: F401

SKILL_DIR = str(Path(__file__).resolve().parent.parent)
WRIT_SESSION_PY = f"{SKILL_DIR}/bin/lib/writ-session.py"


def _seed_cache(cache_dir: str, session_id: str, phase: str) -> str:
    payload = {
        "loaded_rule_ids": [],
        "loaded_rules": [],
        "remaining_budget": 8000,
        "context_percent": 0,
        "queries": 0,
        "mode": "work",
        "is_subagent": False,
        "files_written": [],
        "loaded_rule_ids_by_phase": {},
        "current_phase": phase,
        "gates_approved": [],
        "phase_transitions": [],
    }
    path = os.path.join(cache_dir, f"writ-session-{session_id}.json")
    with open(path, "w") as f:
        json.dump(payload, f)
    return path


def _run_session(cache_dir: str, *args: str) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env["WRIT_CACHE_DIR"] = cache_dir
    return subprocess.run(
        [sys.executable, WRIT_SESSION_PY, *args],
        capture_output=True,
        text=True,
        env=env,
    )


class TestModeSetResetsPhase:
    """Pin the existing contract: `mode set work` against any prior
    phase resets current_phase to `planning` and clears gates_approved.
    """

    @pytest.mark.parametrize("prior_phase", ["complete", "implementation", "testing"])
    def test_mode_set_work_resets_to_planning(
        self, tmp_path, prior_phase
    ) -> None:
        sid = f"reset-{prior_phase}"
        _seed_cache(str(tmp_path), sid, prior_phase)

        r = _run_session(str(tmp_path), "mode", "set", "work", sid)
        assert r.returncode == 0, r.stderr

        read = _run_session(str(tmp_path), "read", sid)
        cache = json.loads(read.stdout)
        assert cache["current_phase"] == "planning", (
            f"mode set work failed to reset {prior_phase} -> planning"
        )
        assert cache["gates_approved"] == [], (
            "gates_approved must clear on mode set"
        )


class TestAdvanceFromCompleteRejects:
    """When current_phase=complete, /advance-phase should refuse rather
    than silently no-op. Caller must explicitly reset via mode set work
    before starting a new task. This catches the pattern where the agent
    advances on user "approved" without realizing the prior task ended.

    The /advance-phase endpoint lives in writ/server.py; this test
    targets the same logical predicate via the underlying _advance helper
    or by exercising the friction-log/phase_transitions invariant.
    Pure-unit form: assert that the cache after advancing from complete
    is unchanged AND that the response carries an error signal.
    """

    def test_advance_from_complete_does_not_advance_silently(
        self, tmp_path
    ) -> None:
        """Hit the live server (started independently) by exercising the
        endpoint via the running uvicorn instance. Avoids in-process
        TestClient pollution of writ_session module state."""
        import urllib.request
        import urllib.error

        sid = "advance-from-complete-live"
        # Seed the cache file in the LIVE server's cache dir (not tmp).
        # This is the only path that exercises the real predicate
        # without rebuilding the pipeline.
        # Resolve the cache dir exactly as the server does (writ-session.py:58),
        # so the seed lands where the live daemon actually reads it. Hardcoding
        # "/tmp" missed the server's dir when TMPDIR is set (e.g. /tmp/claude-1001).
        live_cache_dir = os.environ.get("WRIT_CACHE_DIR", tempfile.gettempdir())
        _seed_cache(live_cache_dir, sid, "complete")

        try:
            req = urllib.request.Request(
                f"http://localhost:{_port()}/session/{sid}/advance-phase",
                data=json.dumps({"confirmation_source": "tool"}).encode(),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=2) as resp:
                body = json.loads(resp.read())
        except urllib.error.URLError:
            pytest.skip("Writ server not running; skip live endpoint check")

        assert "error" in body, (
            f"advance from complete returned silent no-op: {body!r}"
        )
