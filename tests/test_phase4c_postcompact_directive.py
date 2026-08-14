"""Phase 4c / Cycle G: verify-discipline directive delivery, redesigned.

PSR-004 finding: after /compact, the model treats recalled verification output
(e.g. "last run was 6 tests, 13 assertions, all passing") as fresh evidence.
The architectural defense is a directive that forces a re-verify mindset on
the first turn after a compaction.

Cycle G correction: a real `/compact` on 2026-08-14 showed Claude Code's
hook-output validator rejecting writ-postcompact.sh's
`{"hookSpecificOutput": {"hookEventName": "PostCompact", ...}}` reply with
"(root): Invalid input", because PostCompact is not an accepted
hookEventName variant. The whole reply was discarded, so this directive has
never reached the model since cycle B -- these tests used to pass by reading
the hook's OWN stdout, which proved the hook produced a string, not that
Claude Code accepted it (the anti-pattern ANT-PROC-VERIFY-001 names).

The redesign: PostCompact keeps its state duties (reset-after-compaction) and
gives up delivery entirely -- its stdout is now empty. It queues delivery by
setting `post_compact_pending` on the session cache. The next
UserPromptSubmit (writ-rag-inject.sh stdout, the channel confirmed to reach
the model on this build -- see writ.shared.delivery.STDOUT_TO_MODEL_EVENTS)
reads that flag, emits the state line + directive via the single source
`emit_post_compact_directive` in bin/lib/common.sh, and clears the flag.

All behavioral tests here are HERMETIC: WRIT_PORT points at an unreachable
port (19999) so /prompt-bundle, /recall and should-skip all fail open before
the pending-flag check runs (verified: it sits ahead of the /prompt-bundle
call that early-exits on a dead daemon), and WRIT_CACHE_DIR is a per-test
temp dir. No test here depends on a live writ-server or Neo4j.
"""
from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import uuid
from pathlib import Path

import pytest

WRIT_ROOT = Path(__file__).resolve().parent.parent
HOOK = WRIT_ROOT / "hooks" / "scripts" / "writ-postcompact.sh"
RAG_HOOK = WRIT_ROOT / "hooks" / "scripts" / "writ-rag-inject.sh"
COMMON_SH = WRIT_ROOT / "bin" / "lib" / "common.sh"
WRIT_SESSION_PY = str(WRIT_ROOT / "bin" / "lib" / "writ-session.py")

POSTCOMPACT_SRC = HOOK.read_text() if HOOK.exists() else ""
RAG_SRC = RAG_HOOK.read_text() if RAG_HOOK.exists() else ""
COMMON_SRC = COMMON_SH.read_text() if COMMON_SH.exists() else ""

# The directive's first line -- a stable, unique marker for "the directive text
# is embedded here" checks (source-shape) and "emitted exactly once" checks
# (behavioral).
DIRECTIVE_MARKER = "[Writ: context compacted]"
STATE_LINE_MARKER = "[Writ: post-compact workflow state]"


def _load_writ_session():
    spec = importlib.util.spec_from_file_location("writ_session_p4c", WRIT_SESSION_PY)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _run_postcompact(stdin_json: dict, cache_dir: str) -> subprocess.CompletedProcess:
    env = os.environ.copy()
    env["WRIT_CACHE_DIR"] = cache_dir
    env["WRIT_PORT"] = "19999"
    env["WRIT_HOST"] = "localhost"
    return subprocess.run(
        [str(HOOK)],
        input=json.dumps(stdin_json),
        capture_output=True, text=True,
        cwd=str(WRIT_ROOT), env=env, timeout=20,
    )


def _run_rag_inject(session_id: str, cache_dir: str,
                     prompt: str = "What decisions were made recently?") -> subprocess.CompletedProcess:
    """Hermetic UserPromptSubmit invocation. WRIT_PORT=19999 is unreachable, so
    /prompt-bundle, /recall and should-skip all fail open -- the pending-flag
    check (a pure read of the already-fetched $CACHE) runs regardless."""
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
        ["bash", str(RAG_HOOK)],
        input=envelope, capture_output=True, text=True,
        cwd=str(WRIT_ROOT), env=env, timeout=20,
    )


@pytest.fixture()
def seeded_session(tmp_path: Path):
    """(session_id, cache_dir, seed_fn). seed_fn writes only the cache fields a
    test actually needs (TEST-FIXTURE-002)."""
    mod = _load_writ_session()
    sid = f"test-p4c-{uuid.uuid4().hex[:8]}"
    cache_dir = str(tmp_path / "writ-cache")
    Path(cache_dir).mkdir(parents=True, exist_ok=True)
    orig = os.environ.get("WRIT_CACHE_DIR")
    os.environ["WRIT_CACHE_DIR"] = cache_dir

    def seed(**fields) -> None:
        cache = mod._read_cache(sid)
        cache.update(fields)
        mod._write_cache(sid, cache)

    yield sid, cache_dir, seed

    if orig is None:
        os.environ.pop("WRIT_CACHE_DIR", None)
    else:
        os.environ["WRIT_CACHE_DIR"] = orig


# --------------------------------------------------------------------------- #
# PostCompact emits nothing: the rejected-payload bug this cycle fixes
# --------------------------------------------------------------------------- #
class TestPostCompactEmitsNothing:
    """CC's validator rejects hookSpecificOutput.hookEventName == "PostCompact";
    the fix is silence, not a different shape."""

    def test_stdout_is_empty_for_seeded_work_session(self, seeded_session) -> None:
        sid, cache_dir, seed = seeded_session
        seed(mode="work", current_phase="implementation")
        r = _run_postcompact({"session_id": sid, "event": "compact"}, cache_dir)
        assert r.returncode == 0, f"exit {r.returncode}; stderr={r.stderr[:300]!r}"
        assert r.stdout.strip() == "", (
            f"PostCompact must write nothing to stdout; got {r.stdout!r}"
        )

    def test_stdout_contains_no_hook_specific_output_key(self, seeded_session) -> None:
        sid, cache_dir, seed = seeded_session
        seed(mode="work", current_phase="implementation")
        r = _run_postcompact({"session_id": sid, "event": "compact"}, cache_dir)
        assert "hookSpecificOutput" not in r.stdout

    def test_stdout_contains_no_additional_context_key(self, seeded_session) -> None:
        sid, cache_dir, seed = seeded_session
        seed(mode="work", current_phase="implementation")
        r = _run_postcompact({"session_id": sid, "event": "compact"}, cache_dir)
        assert "additionalContext" not in r.stdout


class TestHookExecutability:
    """The hook is present, executable, and bash-syntax-valid."""

    def test_hook_exists(self) -> None:
        assert HOOK.exists(), f"{HOOK} does not exist"

    def test_hook_executable(self) -> None:
        assert os.access(HOOK, os.X_OK), f"{HOOK} is not executable"

    def test_hook_syntax(self) -> None:
        proc = subprocess.run(["bash", "-n", str(HOOK)], capture_output=True, text=True)
        assert proc.returncode == 0, f"Syntax error: {proc.stderr}"


class TestPostCompactExistingBehaviorPreserved:
    """Removing the rejected emission must not touch the hook's state duties."""

    def test_hook_does_not_throw_on_minimal_input(self) -> None:
        # No session_id in the payload: the hook must refuse to synthesize one
        # and still exit 0 (it cannot block compaction).
        r = subprocess.run(
            [str(HOOK)], input=json.dumps({}), capture_output=True, text=True,
            cwd=str(WRIT_ROOT), timeout=20,
        )
        assert r.returncode == 0, f"stderr={r.stderr!r}"
        assert r.stdout.strip() == ""

    def test_refuses_to_synthesize_a_session_id(self) -> None:
        r = subprocess.run(
            [str(HOOK)], input=json.dumps({}), capture_output=True, text=True,
            cwd=str(WRIT_ROOT), timeout=20,
        )
        assert "no session_id" in r.stderr.lower() or "WRIT CRITICAL" in r.stderr, (
            f"hook must record the missing-session-id refusal; stderr={r.stderr!r}"
        )

    def test_still_writes_reset_result_to_log(self, seeded_session) -> None:
        sid, cache_dir, seed = seeded_session
        seed(mode="work", current_phase="implementation")
        log_path = Path(f"/tmp/writ-postcompact-{sid}.log")
        log_path.unlink(missing_ok=True)
        r = _run_postcompact({"session_id": sid, "event": "compact"}, cache_dir)
        assert r.returncode == 0
        assert log_path.exists(), f"{log_path} must be written by the hook"
        contents = log_path.read_text()
        assert '"budget_reset": true' in contents or '"budget_reset":true' in contents

    def test_still_calls_reset_after_compaction(self, seeded_session) -> None:
        """The reset call is the one non-negotiable side effect: it is what
        lets the phase's rules re-inject and now also queues delivery."""
        mod = _load_writ_session()
        sid, cache_dir, seed = seeded_session
        seed(mode="work", current_phase="implementation",
             loaded_rule_ids_by_phase={"implementation": ["X-001"]})
        _run_postcompact({"session_id": sid, "event": "compact"}, cache_dir)
        updated = mod._read_cache(sid)
        assert updated["loaded_rule_ids_by_phase"]["implementation"] == []


# --------------------------------------------------------------------------- #
# The reset queues delivery instead of emitting it
# --------------------------------------------------------------------------- #
class TestPostCompactHookQueuesPendingFlag:
    """cmd_reset_after_compaction (called by the hook) sets post_compact_pending
    so the next UserPromptSubmit knows to deliver. Unit-level coverage of
    cmd_reset_after_compaction itself lives in test_compaction_hooks.py; this
    is the hook-level wire-up."""

    def test_running_the_hook_sets_post_compact_pending_true(self, seeded_session) -> None:
        mod = _load_writ_session()
        sid, cache_dir, seed = seeded_session
        seed(mode="work", current_phase="implementation")
        r = _run_postcompact({"session_id": sid, "event": "compact"}, cache_dir)
        assert r.returncode == 0
        updated = mod._read_cache(sid)
        assert updated.get("post_compact_pending") is True, (
            f"post_compact_pending must be set True by the hook's reset call; "
            f"cache={updated.get('post_compact_pending')!r}"
        )


# --------------------------------------------------------------------------- #
# The directive text lives in exactly one place
# --------------------------------------------------------------------------- #
class TestSingleSourceOfDirectiveText:
    def test_common_sh_defines_emit_post_compact_directive(self) -> None:
        assert "emit_post_compact_directive" in COMMON_SRC, (
            "bin/lib/common.sh must define emit_post_compact_directive, "
            "modelled on emit_mode_directive"
        )

    def test_postcompact_hook_no_longer_embeds_the_directive_text(self) -> None:
        assert DIRECTIVE_MARKER not in POSTCOMPACT_SRC, (
            "writ-postcompact.sh must not carry its own copy of the directive "
            "text; it now lives solely in bin/lib/common.sh::emit_post_compact_directive"
        )

    def test_postcompact_hook_no_longer_claims_cycle_a_fallback(self) -> None:
        assert "Cycle A" not in POSTCOMPACT_SRC and "cycle A heuristic" not in POSTCOMPACT_SRC, (
            "writ-postcompact.sh's header must drop the false claim that a "
            "Cycle A fallback heuristic in writ-rag-inject.sh still exists"
        )


# --------------------------------------------------------------------------- #
# rag-inject.sh: source-shape guards for the new emission block
# --------------------------------------------------------------------------- #
class TestRagInjectPostCompactSourceShape:
    def test_hook_references_post_compact_pending_flag(self) -> None:
        assert "post_compact_pending" in RAG_SRC, (
            "writ-rag-inject.sh must reference post_compact_pending for the "
            "one-shot delivery guard"
        )

    def test_flag_is_read_off_the_already_fetched_cache_variable(self) -> None:
        # A literal parsed_bool "$CACHE" call is a PURE in-process string test
        # against the cache this hook already read at step 1c -- no new curl,
        # no new python3 spawn just to learn the flag's value.
        assert 'parsed_bool "$CACHE" "post_compact_pending"' in RAG_SRC, (
            "the pending check must read $CACHE (already fetched this "
            "invocation), not issue a fresh daemon call"
        )

    def test_clears_flag_via_the_new_update_handler(self) -> None:
        assert "--clear-post-compact-pending" in RAG_SRC, (
            "writ-rag-inject.sh must clear the flag with "
            "'update ... --clear-post-compact-pending' after emitting"
        )

    def test_emission_delegates_to_the_common_sh_function(self) -> None:
        assert "emit_post_compact_directive" in RAG_SRC, (
            "writ-rag-inject.sh must call emit_post_compact_directive rather "
            "than embedding its own copy of the state line / directive text"
        )


# --------------------------------------------------------------------------- #
# rag-inject.sh: behavioral, hermetic (WRIT_PORT=19999)
# --------------------------------------------------------------------------- #
class TestRagInjectPostCompactEmission:
    def test_state_line_and_directive_emitted_when_pending(self, seeded_session) -> None:
        sid, cache_dir, seed = seeded_session
        seed(mode="work", current_phase="implementation", post_compact_pending=True)
        r = _run_rag_inject(sid, cache_dir)
        assert r.returncode == 0, f"exit {r.returncode}; stderr={r.stderr[:300]!r}"
        assert STATE_LINE_MARKER in r.stdout, f"state line missing: {r.stdout[:400]!r}"
        assert "mode=work" in r.stdout
        assert "implementation" in r.stdout
        assert DIRECTIVE_MARKER in r.stdout

    def test_silent_when_pending_unset(self, seeded_session) -> None:
        sid, cache_dir, seed = seeded_session
        seed(mode="work", current_phase="implementation")  # post_compact_pending absent/False
        r = _run_rag_inject(sid, cache_dir)
        assert r.returncode == 0
        assert STATE_LINE_MARKER not in r.stdout
        assert DIRECTIVE_MARKER not in r.stdout

    def test_clears_flag_after_emitting(self, seeded_session) -> None:
        mod = _load_writ_session()
        sid, cache_dir, seed = seeded_session
        seed(mode="work", current_phase="implementation", post_compact_pending=True)
        r = _run_rag_inject(sid, cache_dir)
        assert r.returncode == 0
        updated = mod._read_cache(sid)
        assert updated.get("post_compact_pending") is False, (
            f"the flag must be cleared after the first emission; "
            f"got {updated.get('post_compact_pending')!r}"
        )

    def test_second_prompt_in_same_session_emits_neither(self, seeded_session) -> None:
        sid, cache_dir, seed = seeded_session
        seed(mode="work", current_phase="implementation", post_compact_pending=True)
        first = _run_rag_inject(sid, cache_dir)
        assert first.returncode == 0
        assert DIRECTIVE_MARKER in first.stdout, "sanity: first prompt must emit"

        second = _run_rag_inject(sid, cache_dir, prompt="a second, unrelated prompt")
        assert second.returncode == 0
        assert STATE_LINE_MARKER not in second.stdout
        assert DIRECTIVE_MARKER not in second.stdout


# --------------------------------------------------------------------------- #
# The nine PSR-004 content assertions, retargeted to the new emission point
# --------------------------------------------------------------------------- #
class TestPostCompactDirectiveContent:
    """Each test seeds only post_compact_pending=True (TEST-FIXTURE-002): the
    directive itself does not require a mode (see the no-mode boundary case
    in test_pol5d_postcompact_state.py)."""

    def _stdout(self, seeded_session) -> str:
        sid, cache_dir, seed = seeded_session
        seed(post_compact_pending=True)
        r = _run_rag_inject(sid, cache_dir)
        assert r.returncode == 0, f"exit {r.returncode}; stderr={r.stderr[:300]!r}"
        return r.stdout

    def test_directive_present_and_nonempty(self, seeded_session) -> None:
        out = self._stdout(seeded_session)
        assert DIRECTIVE_MARKER in out, "directive must be emitted on the pending path"

    def test_directive_mentions_compaction(self, seeded_session) -> None:
        out = self._stdout(seeded_session).lower()
        assert "compact" in out, (
            "Directive must reference the compaction event so the model "
            "knows why this directive is firing"
        )

    def test_directive_mentions_recalled_or_second_hand_evidence(self, seeded_session) -> None:
        out = self._stdout(seeded_session).lower()
        signals = ["recalled", "second-hand", "second hand", "remembered", "pre-compact"]
        assert any(s in out for s in signals), (
            f"Directive must signal that pre-compact memory is now second-hand "
            f"evidence (one of {signals!r})"
        )

    def test_directive_instructs_reverify(self, seeded_session) -> None:
        out = self._stdout(seeded_session).lower()
        signals = ["re-run", "rerun", "re-verify", "reverify", "verify"]
        assert any(s in out for s in signals), (
            f"Directive must instruct re-verification (one of {signals!r})"
        )

    def test_directive_handles_blocked_reverification(self, seeded_session) -> None:
        out = self._stdout(seeded_session).lower()
        assert "blocked" in out, (
            "Directive must address the blocked/rejected re-verification "
            "case explicitly (PSR-004b regression)"
        )

    def test_directive_uses_stop_language(self, seeded_session) -> None:
        out = self._stdout(seeded_session)
        assert "STOP" in out, (
            "Directive must include STOP language for the blocked case to "
            "interrupt the rejection-as-confirmation reflex"
        )

    def test_directive_forbids_yes_without_evidence(self, seeded_session) -> None:
        out = self._stdout(seeded_session).lower()
        assert "forbidden" in out, (
            "Directive must use 'forbidden' framing so the model recognizes "
            "answering 'yes' without re-verify as a hard rule, not advice"
        )

    def test_directive_mentions_fresh_evidence(self, seeded_session) -> None:
        out = self._stdout(seeded_session).lower()
        assert "fresh evidence" in out, (
            "Directive must contrast recalled output with 'fresh evidence' so "
            "the model knows what counts as a valid affirmative"
        )

    def test_directive_appears_exactly_once_not_duplicated(self, seeded_session) -> None:
        out = self._stdout(seeded_session)
        assert out.count(DIRECTIVE_MARKER) == 1, (
            f"the directive must be emitted exactly once per prompt, found "
            f"{out.count(DIRECTIVE_MARKER)} occurrences"
        )
