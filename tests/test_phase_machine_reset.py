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

import asyncio
import json
import os
import subprocess
import sys
import uuid

import pytest
from pathlib import Path

# ruff: noqa: F811 -- shared fixtures are consumed as test-method parameters.
# autouse: pins cwd to a sandbox so `mode set` cannot delete THIS repo's gate artifacts.
from tests.fixtures.session_state import sandbox_cwd, write_bound_gate_token  # noqa: F401
from tests.test_gate_token_binding import _mint_cleanup, _no_leaked_gate_tokens  # noqa: F401
from tests.test_phase_advance_unified import _drive_advance_via_route
from writ.session.gate_token import gate_token_path

SKILL_DIR = str(Path(__file__).resolve().parent.parent)
WRIT_SESSION_PY = f"{SKILL_DIR}/bin/lib/writ-session.py"


def _seed_cache(
    cache_dir: str, session_id: str, phase: str, *, gates_approved: list[str] | None = None,
) -> str:
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
        "gates_approved": gates_approved if gates_approved is not None else [],
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
    """When current_phase=complete, /advance-phase must refuse rather than
    silently no-op or fall through to a neighbouring arm. There is no
    production `_advance` helper the route delegates to: every `_advance` in
    this tree (e.g. tests/test_advance_gate_validation_parity.py:92) is a
    per-test-module helper, and the guard is inline in the route at
    writ/server/routes/gate.py:118-127. The test below drives the real
    FastAPI app in-process with a VALID token through
    `_drive_advance_via_route`, identifies the terminal arm by the keys its
    two refusing neighbours do NOT carry, and proves the refusal returns
    before `claim_gate_token`, so it does not spend the human's approval.
    """

    def test_advance_from_complete_is_refused_and_does_not_spend_the_approval(
        self, tmp_path, monkeypatch, sandbox_cwd
    ) -> None:
        """A `complete` session presenting a valid token is refused, not
        silently no-opped, and the refusal spends nothing.

        Three edits redden this test, each reverted before the next:
          M1 (delete the `if old_phase == "complete":` block,
          gate.py:118-127) reddens A1, A2 and A3: control falls to the
          no-pending-gate arm, which shares `phase`/`from`/
          `confirmation_source` with the terminal arm and consumes nothing,
          so A4 through A7 stay green.
          M2 (a `consume_gate_token` call inserted before the terminal
          return, gate.py:119) reddens A6 alone: the refusal would then
          spend the approval it must leave untouched.
          Arm probe (same body, `token=""`, not a mutation and not
          committed) reddens A4 and A5: the token-invalid arm answers
          instead while A1 stays green, which is the measurement that
          showed the old test's `"error" in body` assertion passed for the
          wrong reason.
        """
        monkeypatch.setenv("WRIT_CACHE_DIR", str(tmp_path))
        sid = f"advance-from-complete-{uuid.uuid4().hex[:8]}"
        cache_path = _seed_cache(
            str(tmp_path), sid, "complete", gates_approved=["phase-a", "test-skeletons"],
        )
        token = write_bound_gate_token(sid)
        token_path = gate_token_path(sid)
        minted = Path(token_path).read_bytes()

        with _mint_cleanup(sid):
            body = asyncio.run(
                _drive_advance_via_route(
                    sid,
                    project_root=str(sandbox_cwd),
                    token=token,
                    confirmation_source="tool",
                )
            )

            # A1: the old property, kept.
            assert body.get("error"), f"advance from complete returned no error: {body!r}"
            # A2: excludes the token-invalid arm (gate.py:91-98), which also carries "advanced".
            assert "advanced" not in body
            # A3: excludes the no-pending-gate arm (gate.py:135-141), which also carries "reason".
            assert "reason" not in body
            # A4: positively identifies the terminal arm.
            assert body.get("phase") == "complete"
            assert body.get("from") == "complete"
            # A5: the refusal echoes the caller's claimed authorization.
            assert body.get("confirmation_source") == "tool"
            # A6: the refused advance does not spend the approval.
            assert Path(token_path).exists()
            assert Path(token_path).read_bytes() == minted
            # A7: the refusal mutates nothing.
            cache = json.loads(Path(cache_path).read_text())
            assert cache["current_phase"] == "complete"
            assert cache["phase_transitions"] == []
