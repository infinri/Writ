"""POL-5d: PostCompact state-rehydration (A) + PreCompact re-document (C).

A: after /compact the next rag-inject only fires on the next user prompt, so an
   autonomously-resuming agent has no workflow bearings. cmd_reset_after_compaction
   now returns mode + phase.
C: writ-precompact.sh's "reduce footprint before compression" rationale is false
   (the session cache is a /tmp file, not part of the compacted context). Re-document;
   behavior unchanged.

Cycle G correction: the compact state line and the verify-discipline directive no
longer travel on writ-postcompact.sh's own stdout -- Claude Code's hook-output
validator rejects a PostCompact hookSpecificOutput payload outright ("(root):
Invalid input"), so that channel is dead. writ-postcompact.sh now only QUEUES
delivery (post_compact_pending=True via cmd_reset_after_compaction); the next
writ-rag-inject.sh UserPromptSubmit invocation is what actually emits the state
line + directive (via bin/lib/common.sh::emit_post_compact_directive) and clears
the flag. The state-line/directive assertions below are retargeted accordingly,
hermetically (WRIT_PORT=19999, no live daemon needed) rather than gated behind a
live-server marker.

RED until writ-session.py / writ-rag-inject.sh / bin/lib/common.sh / writ-precompact.sh
are updated.
"""

from __future__ import annotations

import importlib.util
import io
import json
import os
import subprocess
import tempfile
import uuid
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

import pytest

SKILL_DIR = Path(__file__).resolve().parent.parent
WRIT_SESSION_PY = str(SKILL_DIR / "bin" / "lib" / "writ-session.py")
PRECOMPACT = SKILL_DIR / "hooks" / "scripts" / "writ-precompact.sh"

PRECOMPACT_SRC = PRECOMPACT.read_text()
WRIT_SESSION_SRC = Path(WRIT_SESSION_PY).read_text()
# POL-6g-3: cmd_clear_rules_for_compaction moved to writ/session/session_lifecycle.py.
SESSION_LIFECYCLE_SRC = (SKILL_DIR / "writ" / "session" / "session_lifecycle.py").read_text()

MISLEADING = "footprint before compression"
CORRECTED = "not part of the compacted context"
BYTES_NOTE = "bytes_freed is cache-file bytes, not context tokens"


def _load_writ_session():
    spec = importlib.util.spec_from_file_location("writ_session_5d", WRIT_SESSION_PY)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _run_rag_inject(session_id: str, cache_dir: str,
                     prompt: str = "What decisions were made recently?") -> subprocess.CompletedProcess:
    """Hermetic UserPromptSubmit invocation of writ-rag-inject.sh: WRIT_PORT
    points at an unreachable port so /prompt-bundle, /recall and should-skip
    all fail open before the post_compact_pending check (a pure read of the
    already-fetched $CACHE) runs -- no live daemon required."""
    env = os.environ.copy()
    env["WRIT_CACHE_DIR"] = cache_dir
    env["WRIT_PORT"] = "19999"
    env["WRIT_HOST"] = "localhost"
    envelope = json.dumps({
        "session_id": session_id,
        "hook_event_name": "UserPromptSubmit",
        "prompt": prompt,
    })
    return subprocess.run(
        ["bash", str(SKILL_DIR / "hooks" / "scripts" / "writ-rag-inject.sh")],
        input=envelope, capture_output=True, text=True,
        cwd=str(SKILL_DIR), env=env, timeout=20,
    )


# --------------------------------------------------------------------------- #
# A. cmd_reset_after_compaction returns mode + phase (unit)
# --------------------------------------------------------------------------- #
class TestResetReturnsModeAndPhase:
    def setup_method(self) -> None:
        self.mod = _load_writ_session()
        self._tmp = tempfile.mkdtemp()
        self._env_patch = mock.patch.dict(os.environ, {"WRIT_CACHE_DIR": self._tmp})
        self._env_patch.start()

    def teardown_method(self) -> None:
        self._env_patch.stop()
        import shutil

        shutil.rmtree(self._tmp, ignore_errors=True)

    def _run(self, sid: str, cache: dict) -> dict:
        with open(self.mod._cache_path(sid), "w") as f:
            json.dump(cache, f)
        buf = io.StringIO()
        with redirect_stdout(buf):
            self.mod.cmd_reset_after_compaction(sid)
        return json.loads(buf.getvalue().strip())

    def test_result_includes_mode_and_phase(self) -> None:
        sid = "test-5d-unit"
        cache = {"mode": "work", "current_phase": "implementation",
                 "loaded_rule_ids_by_phase": {"implementation": ["X"]}, "remaining_budget": 500}
        result = self._run(sid, cache)
        assert result.get("mode") == "work", f"mode missing/wrong: {result}"
        assert result.get("phase") == "implementation", f"phase missing/wrong: {result}"

    def test_existing_keys_preserved(self) -> None:
        sid = "test-5d-unit2"
        cache = {"mode": "work", "current_phase": "implementation",
                 "loaded_rule_ids_by_phase": {"implementation": ["X"]}, "remaining_budget": 500}
        result = self._run(sid, cache)
        assert "rules_cleared" in result and result["budget_reset"] is True


# --------------------------------------------------------------------------- #
# A. writ-rag-inject.sh emits the state line + directive (behavioral, hermetic)
# --------------------------------------------------------------------------- #
class TestRagInjectPostCompactStateRehydration:
    """Cycle G retarget: writ-postcompact.sh no longer emits anything (its
    hookSpecificOutput shape is rejected by CC's validator on PostCompact).
    It only sets post_compact_pending=True. The state line + directive move to
    the next writ-rag-inject.sh UserPromptSubmit invocation, tested here
    hermetically (WRIT_PORT=19999 unreachable, no live daemon needed) rather
    than behind the @requires_server marker the old postcompact-stdout version
    needed."""

    @pytest.fixture()
    def seeded(self, tmp_path: Path):
        mod = _load_writ_session()
        sid = f"test-5d-{uuid.uuid4().hex[:8]}"
        cache_dir = str(tmp_path / "writ-cache")
        Path(cache_dir).mkdir(parents=True, exist_ok=True)
        orig = os.environ.get("WRIT_CACHE_DIR")
        os.environ["WRIT_CACHE_DIR"] = cache_dir

        def seed(**fields):
            cache = mod._read_cache(sid)
            cache.update(fields)
            mod._write_cache(sid, cache)
            return sid

        yield sid, cache_dir, seed

        if orig is None:
            os.environ.pop("WRIT_CACHE_DIR", None)
        else:
            os.environ["WRIT_CACHE_DIR"] = orig

    def test_state_line_emitted_for_work_session(self, seeded) -> None:
        sid, cache_dir, seed = seeded
        seed(mode="work", current_phase="implementation", post_compact_pending=True)
        r = _run_rag_inject(sid, cache_dir)
        assert r.returncode == 0, f"exit {r.returncode}; stderr={r.stderr[:300]!r}"
        assert "post-compact workflow state" in r.stdout, f"state line missing: {r.stdout[:400]!r}"
        assert "mode=work" in r.stdout
        assert "implementation" in r.stdout

    def test_verification_directive_still_emitted(self, seeded) -> None:
        sid, cache_dir, seed = seeded
        seed(mode="work", current_phase="implementation", post_compact_pending=True)
        r = _run_rag_inject(sid, cache_dir)
        assert r.returncode == 0, f"exit {r.returncode}; stderr={r.stderr[:300]!r}"
        assert "fresh evidence" in r.stdout.lower()
        assert "STOP" in r.stdout

    def test_no_state_line_when_mode_unset(self, seeded) -> None:
        # mode left unset, but post_compact_pending IS set: the state line is
        # omitted (nothing to report) while the directive still emits (the
        # no-mode boundary case this file exists to pin).
        sid, cache_dir, seed = seeded
        seed(post_compact_pending=True)
        r = _run_rag_inject(sid, cache_dir)
        assert r.returncode == 0, f"exit {r.returncode}; stderr={r.stderr[:300]!r}"
        assert "post-compact workflow state" not in r.stdout, (
            f"state line must not emit without a mode: {r.stdout[:300]!r}"
        )
        assert "fresh evidence" in r.stdout.lower(), "directive must still emit"


# --------------------------------------------------------------------------- #
# C. PreCompact re-documented (source-shape + behavioral)
# --------------------------------------------------------------------------- #
class TestPreCompactRedocumented:
    def test_misleading_footprint_claim_gone(self) -> None:
        assert MISLEADING not in PRECOMPACT_SRC, (
            "writ-precompact.sh must drop the false 'reduce footprint before "
            "compression' rationale (the cache is not in the compacted context)"
        )

    def test_corrected_statement_present(self) -> None:
        assert CORRECTED in PRECOMPACT_SRC, (
            "writ-precompact.sh must state that the session cache is not part of "
            "the compacted context"
        )

    def test_no_longer_claims_postcompact_reaches_next_turn_via_additional_context(self) -> None:
        # Cycle G: this is the exact false belief the cycle disproves. CC's
        # validator rejects PostCompact's hookSpecificOutput outright, so
        # writ-postcompact.sh's own output never reached the next turn via
        # additionalContext -- delivery now queues instead (post_compact_pending)
        # and the next writ-rag-inject.sh UserPromptSubmit does the emitting.
        assert "reaches the next turn" not in PRECOMPACT_SRC, (
            "writ-precompact.sh must drop the false claim that PostCompact's "
            "own output reaches the next turn via additionalContext"
        )

    def test_clear_rules_docstring_corrected(self) -> None:
        # the cmd carries a note that bytes_freed is cache bytes, not context tokens
        assert BYTES_NOTE in SESSION_LIFECYCLE_SRC, (
            "cmd_clear_rules_for_compaction must note bytes_freed is cache-file bytes, "
            "not context tokens"
        )

    def test_precompact_hook_still_exits_zero(self) -> None:
        r = subprocess.run(
            ["bash", str(PRECOMPACT)], input="", capture_output=True, text=True,
            cwd=str(SKILL_DIR), timeout=15,
        )
        assert r.returncode == 0, f"precompact must still exit 0; stderr={r.stderr[:200]!r}"
