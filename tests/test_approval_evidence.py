"""tests/test_approval_evidence.py

Pins defect 2 (plan.md): an exact `approved` currently mints a token and advances
whatever gate is pending regardless of what the user was actually answering. This
cycle requires the immediately preceding assistant turn to have asked for THIS
approval before an exact `approved` mints or advances anything; absent that evidence
the hook takes the existing embedded-tier treatment (name the gate, ask, change
nothing). A fourth tier, the exact whole-prompt phrase `approved anyway`, overrides
a missing-evidence ask so an infrastructure failure (no transcript_path, an unreadable
file) cannot deadlock the user forever.

RED today: `bin/lib/approval_evidence.py` does not exist; `approval_match.py` has no
`OVERRIDE_PHRASE` / `is_override()` / `"override"` classify() tier; the hook's payload
parse has no fourth (transcript_path) line and its exact-tier arm mints and advances
unconditionally; no `approval_evidence_missing` / `approval_evidence_override` events
are emitted anywhere.

NOT STATED VERBATIM IN plan.md, committed to here as the narrowest reading of its
Analysis section (same disclosure convention as tests/test_gate_token_binding.py's
module docstring):
  * `bin/lib/approval_evidence.py` exposes one pure function,
    `request_was_asked(transcript_path: str) -> bool`, taking the SAME bounded
    400-line tail-scan approach as hooks/scripts/writ-comms-output-gate.sh, fail-closed
    (any read/parse error, or no assistant row at all, returns False).
  * The envelope's fourth field is the JSON key `transcript_path` (the universal hook
    field name Claude Code already uses, and the same key
    hooks/scripts/writ-comms-output-gate.sh reads), not a Writ-invented name.
  * `approval_match.OVERRIDE_PHRASE == "approved anyway"`, matched on WHOLE-PROMPT
    equality exactly as `REPLAN_PHRASE` is (same lowercase/strip/trailing-punctuation
    normalization `is_approval` and `is_replan_request` already share).

Per ENF-SYS-005 / TEST-ISOLATE-001: every hook invocation gets its own uuid-suffixed
session id and its own tmp WRIT_CACHE_DIR; every test that mints wraps the risky
section in `_mint_cleanup`, and the autouse `_no_leaked_gate_tokens` fixture is the
safety net (mirrors tests/test_gate_token_binding.py and
tests/test_replan_reopen_planning.py's identical convention).

Per the dispatch brief's harness traps: HOME is pinned to a tmp dir in every hook
subprocess (trap 2: blackbox capture is live and must never see this suite's
traffic), and the audit-stream assertions read back through
`writ.shared.logging.read_streams` against an isolated WRIT_LOG_ROOT/WRIT_LOG_PROJECT,
never a grepped file and never WRIT_FRICTION_LOG (trap 3: that variable collapses
every typed stream into one file, which would make "this event landed on audit"
vacuous).

Per TEST-TDD-001 / SKL-PROC-WRIT-FAILURE-001: skeletons approved before implementation.
"""

from __future__ import annotations

import contextlib
import glob
import json
import os
import shutil
import subprocess
import sys
import uuid
from pathlib import Path

import pytest

# This module asserts WHICH typed stream each audit row landed on, so it must opt out of
# the autouse WRIT_FRICTION_LOG redirect in tests/conftest.py. That variable collapses every
# stream into one file, and read_streams (what these tests read back with) does not honour
# it, so under the redirect the rows are written somewhere the assertions never look and the
# checks fail while the behaviour is correct. Same reason tests/firedrill/conftest.py applies
# this marker to its whole package. The isolation that replaces it is per-test:
# WRIT_LOG_ROOT plus WRIT_LOG_PROJECT into tmp_path.
pytestmark = pytest.mark.no_friction_isolation

SKILL_ROOT = Path(__file__).resolve().parent.parent
HOOK_PATH = SKILL_ROOT / "hooks" / "scripts" / "auto-approve-gate.sh"
APPROVAL_EVIDENCE_PY = SKILL_ROOT / "bin" / "lib" / "approval_evidence.py"

sys.path.insert(0, str(SKILL_ROOT / "bin" / "lib"))
import approval_match  # noqa: E402
from approval_match import is_approval  # noqa: E402

from tests._strace import trace_execve  # noqa: E402
from tests.fixtures.session_state import (  # noqa: E402
    assistant_text_row,
    assistant_tool_call_row,
    sandbox_cwd,  # noqa: F401, autouse cwd pin, imported per this repo's convention
    user_row,
    write_transcript_jsonl,
)


def _sid(label: str) -> str:
    return f"evidence-{label}-{uuid.uuid4().hex[:8]}"


def _closed_port() -> int:
    import socket

    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]
    finally:
        s.close()


def _seed_pending_phase_a(cache_dir: Path, sid: str) -> None:
    cache_dir.mkdir(parents=True, exist_ok=True)
    (cache_dir / f"writ-session-{sid}.json").write_text(
        json.dumps({"mode": "work", "current_phase": "planning", "gates_approved": []})
    )


def _run_hook(
    payload: dict, cwd: Path, cache_dir: Path, fake_home: Path, extra_env: dict | None = None,
) -> subprocess.CompletedProcess:
    """Real subprocess, HOME pinned (trap 2: blackbox capture is live), WRIT_PORT a
    closed port (the daemon-unreachable arm), WRIT_CACHE_DIR the caller's own tmp
    dir. Argument is the payload dict; json.dumps happens here so no caller ever
    interpolates a path into a shell string (SEC-INJ-CMD-001, same discipline the
    hook itself uses for the prompt and the transcript path)."""
    fake_home.mkdir(parents=True, exist_ok=True)
    (fake_home / ".claude").mkdir(parents=True, exist_ok=True)
    env = {
        **os.environ,
        "WRIT_CACHE_DIR": str(cache_dir),
        "WRIT_HOST": "127.0.0.1",
        "WRIT_PORT": str(_closed_port()),
        "HOME": str(fake_home),
    }
    if extra_env:
        env.update(extra_env)
    return subprocess.run(
        ["bash", str(HOOK_PATH)], input=json.dumps(payload), cwd=str(cwd),
        capture_output=True, text=True, env=env, timeout=30,
    )


@contextlib.contextmanager
def _mint_cleanup(sid: str):
    from writ.session.gate_token import gate_token_path

    try:
        yield
    finally:
        try:
            os.remove(gate_token_path(sid))
        except OSError:
            pass


@pytest.fixture(autouse=True)
def _no_leaked_gate_tokens():
    before = set(glob.glob("/tmp/writ-gate-token-*"))
    yield
    after = set(glob.glob("/tmp/writ-gate-token-*"))
    leaked = after - before
    for path in leaked:
        try:
            os.remove(path)
        except OSError:
            pass
    assert not leaked, f"test leaked gate token file(s) (now removed): {sorted(leaked)}"


def _read_stream_rows(project: str, stream: str) -> list[dict]:
    from writ.shared.logging import read_streams

    return read_streams(project, [stream])


# ---------------------------------------------------------------------------
# Direct unit tests of bin/lib/approval_evidence.py's boundary cases (no hook
# subprocess: these pin the reader's own decisions, which the hook-level tests
# below then confirm are actually wired in).
# ---------------------------------------------------------------------------


def _evidence_reader():
    """Import bin/lib/approval_evidence.py by PATH, exactly as the hook loads it
    (its own docstring: 'no writ import, pure, fail-closed, path-loaded by the hook
    exactly as approval_match.py is'). Imported lazily inside each test so its
    absence today raises a clear ImportError scoped to this test, not a collection
    failure for the whole module."""
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "approval_evidence", str(APPROVAL_EVIDENCE_PY)
    )
    if spec is None or spec.loader is None:
        pytest.fail(f"bin/lib/approval_evidence.py does not exist yet at {APPROVAL_EVIDENCE_PY}")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class TestEvidenceReaderMarkerNormalization:
    """Capability: evidence survives markdown decoration."""

    def test_backtick_decorated_marker_counts(self, tmp_path):
        mod = _evidence_reader()
        tp = write_transcript_jsonl(tmp_path, [assistant_text_row("Say `approved` to proceed.")])
        assert mod.request_was_asked(tp) is True

    def test_asterisk_decorated_marker_counts(self, tmp_path):
        mod = _evidence_reader()
        tp = write_transcript_jsonl(tmp_path, [assistant_text_row("Reply **approved** when ready.")])
        assert mod.request_was_asked(tp) is True

    def test_a_turn_with_no_marker_at_all_is_not_evidence(self, tmp_path):
        mod = _evidence_reader()
        tp = write_transcript_jsonl(
            tmp_path, [assistant_text_row("I refactored the parser module.")]
        )
        assert mod.request_was_asked(tp) is False


class TestEvidenceReaderTrailingToolCallDoesNotErase:
    def test_trailing_tool_call_only_row_keeps_the_earlier_texts_evidence(self, tmp_path):
        mod = _evidence_reader()
        tp = write_transcript_jsonl(tmp_path, [
            assistant_text_row("Say approved to proceed."),
            assistant_tool_call_row(),
        ])
        assert mod.request_was_asked(tp) is True

    def test_a_lone_tool_call_only_row_with_no_preceding_text_is_not_evidence(self, tmp_path):
        """Anti-vacuity for the guard above: a tool-call row must never BY ITSELF
        read as a request; the fixture above only proves it does not erase, this
        proves it does not manufacture."""
        mod = _evidence_reader()
        tp = write_transcript_jsonl(tmp_path, [assistant_tool_call_row()])
        assert mod.request_was_asked(tp) is False


class TestEvidenceReaderUserRowTolerance:
    """At most ONE user row may follow the evidence turn; two or more mean the
    request belongs to an earlier exchange and does not count."""

    def test_no_trailing_user_row_still_counts(self, tmp_path):
        mod = _evidence_reader()
        tp = write_transcript_jsonl(tmp_path, [assistant_text_row("Say approved to proceed.")])
        assert mod.request_was_asked(tp) is True

    def test_exactly_one_trailing_user_row_still_counts(self, tmp_path):
        mod = _evidence_reader()
        tp = write_transcript_jsonl(
            tmp_path, [assistant_text_row("Say approved to proceed."), user_row("approved")]
        )
        assert mod.request_was_asked(tp) is True

    def test_two_trailing_user_rows_do_not_count(self, tmp_path):
        mod = _evidence_reader()
        tp = write_transcript_jsonl(tmp_path, [
            assistant_text_row("Say approved to proceed."),
            user_row("wait, one more question"),
            user_row("approved"),
        ])
        assert mod.request_was_asked(tp) is False


class TestEvidenceReaderMissingOrUnreadableTranscript:
    def test_a_missing_file_is_not_evidence(self, tmp_path):
        mod = _evidence_reader()
        assert mod.request_was_asked(str(tmp_path / "does-not-exist.jsonl")) is False

    def test_a_directory_path_unreadable_as_a_file_is_not_evidence(self, tmp_path):
        """A portable stand-in for 'present but unreadable' that needs no chmod
        (permission bits are meaningless when the test runs as root): opening a
        directory as a file always raises."""
        mod = _evidence_reader()
        a_directory = tmp_path / "not-a-file"
        a_directory.mkdir()
        assert mod.request_was_asked(str(a_directory)) is False

    def test_a_garbled_line_does_not_crash_and_is_not_evidence(self, tmp_path):
        mod = _evidence_reader()
        tp = write_transcript_jsonl(tmp_path, ["{not valid json at all"])
        assert mod.request_was_asked(tp) is False

    def test_no_assistant_row_at_all_is_not_evidence(self, tmp_path):
        """The session's first turn: a transcript with only a user row."""
        mod = _evidence_reader()
        tp = write_transcript_jsonl(tmp_path, [user_row("hello")])
        assert mod.request_was_asked(tp) is False

    def test_an_empty_transcript_is_not_evidence(self, tmp_path):
        mod = _evidence_reader()
        tp = write_transcript_jsonl(tmp_path, [])
        assert mod.request_was_asked(tp) is False


# ---------------------------------------------------------------------------
# Capability 1/2: exact `approved`, no evidence -> no mint, no advance, ask directive
# ---------------------------------------------------------------------------


class TestExactApprovedWithNoEvidenceAsksInsteadOfActing:
    def test_no_token_is_minted(self, tmp_path):
        from writ.session.gate_token import gate_token_path

        sid = _sid("noevidence-notoken")
        cache_dir = tmp_path / "cache"
        _seed_pending_phase_a(cache_dir, sid)
        tp = write_transcript_jsonl(tmp_path, [assistant_text_row("I refactored the parser.")])
        with _mint_cleanup(sid):
            _run_hook(
                {"session_id": sid, "prompt": "approved", "transcript_path": tp},
                cwd=tmp_path, cache_dir=cache_dir, fake_home=tmp_path / "home",
            )
            assert not os.path.exists(gate_token_path(sid)), (
                "an exact approval with no evidence must mint no token"
            )

    def test_gates_approved_does_not_change(self, tmp_path):
        sid = _sid("noevidence-noadvance")
        cache_dir = tmp_path / "cache"
        _seed_pending_phase_a(cache_dir, sid)
        tp = write_transcript_jsonl(tmp_path, [assistant_text_row("I refactored the parser.")])
        with _mint_cleanup(sid):
            _run_hook(
                {"session_id": sid, "prompt": "approved", "transcript_path": tp},
                cwd=tmp_path, cache_dir=cache_dir, fake_home=tmp_path / "home",
            )
        after = json.loads((cache_dir / f"writ-session-{sid}.json").read_text())
        assert after.get("gates_approved") == []

    def test_the_directive_names_the_pending_gate_and_the_override_phrase_verbatim(self, tmp_path):
        sid = _sid("noevidence-directive")
        cache_dir = tmp_path / "cache"
        _seed_pending_phase_a(cache_dir, sid)
        tp = write_transcript_jsonl(tmp_path, [assistant_text_row("I refactored the parser.")])
        with _mint_cleanup(sid):
            r = _run_hook(
                {"session_id": sid, "prompt": "approved", "transcript_path": tp},
                cwd=tmp_path, cache_dir=cache_dir, fake_home=tmp_path / "home",
            )
        assert "phase-a" in r.stdout, f"directive must name the pending gate: {r.stdout!r}"
        assert approval_match.OVERRIDE_PHRASE in r.stdout, (
            f"directive must quote the override phrase verbatim: {r.stdout!r}"
        )


class TestExactApprovedWithEvidenceMintsAndAdvancesAsToday:
    def test_a_marker_phrase_in_the_preceding_turn_mints_a_bound_token(self, tmp_path):
        from writ.session.gate_token import gate_token_path

        sid = _sid("evidence-mints")
        cache_dir = tmp_path / "cache"
        _seed_pending_phase_a(cache_dir, sid)
        tp = write_transcript_jsonl(tmp_path, [assistant_text_row("Say approved to proceed.")])
        with _mint_cleanup(sid):
            r = _run_hook(
                {"session_id": sid, "prompt": "approved", "transcript_path": tp},
                cwd=tmp_path, cache_dir=cache_dir, fake_home=tmp_path / "home",
            )
            assert os.path.exists(gate_token_path(sid)), (
                f"an exact approval WITH evidence must mint a token; stdout={r.stdout!r}"
            )
            with open(gate_token_path(sid)) as f:
                lines = f.read().split("\n")
            assert len(lines) >= 2 and lines[1] == "phase-a", (
                f"the mint must bind to the pending gate; lines={lines!r}"
            )


# ---------------------------------------------------------------------------
# Capabilities 7-9: absent / missing / no-assistant-row transcript_path, at the hook
# ---------------------------------------------------------------------------


class TestHookLevelFailClosedTranscriptCases:
    def test_transcript_path_absent_from_payload_mints_nothing(self, tmp_path):
        from writ.session.gate_token import gate_token_path

        sid = _sid("hook-absent")
        cache_dir = tmp_path / "cache"
        _seed_pending_phase_a(cache_dir, sid)
        with _mint_cleanup(sid):
            r = _run_hook(
                {"session_id": sid, "prompt": "approved"},  # no transcript_path key at all
                cwd=tmp_path, cache_dir=cache_dir, fake_home=tmp_path / "home",
            )
            assert not os.path.exists(gate_token_path(sid)), (
                f"an absent transcript_path must mint nothing; stdout={r.stdout!r}"
            )
            assert "phase-a" in r.stdout

    def test_transcript_path_naming_a_missing_file_mints_nothing(self, tmp_path):
        from writ.session.gate_token import gate_token_path

        sid = _sid("hook-missingfile")
        cache_dir = tmp_path / "cache"
        _seed_pending_phase_a(cache_dir, sid)
        with _mint_cleanup(sid):
            r = _run_hook(
                {
                    "session_id": sid, "prompt": "approved",
                    "transcript_path": str(tmp_path / "no-such-transcript.jsonl"),
                },
                cwd=tmp_path, cache_dir=cache_dir, fake_home=tmp_path / "home",
            )
            assert not os.path.exists(gate_token_path(sid)), (
                f"a missing transcript file must mint nothing; stdout={r.stdout!r}"
            )
            assert "phase-a" in r.stdout

    def test_transcript_with_no_assistant_row_mints_nothing(self, tmp_path):
        """The session's first turn: nothing to have asked with yet."""
        from writ.session.gate_token import gate_token_path

        sid = _sid("hook-noassistant")
        cache_dir = tmp_path / "cache"
        _seed_pending_phase_a(cache_dir, sid)
        tp = write_transcript_jsonl(tmp_path, [user_row("approved")])
        with _mint_cleanup(sid):
            r = _run_hook(
                {"session_id": sid, "prompt": "approved", "transcript_path": tp},
                cwd=tmp_path, cache_dir=cache_dir, fake_home=tmp_path / "home",
            )
            assert not os.path.exists(gate_token_path(sid)), (
                f"no assistant row must mint nothing; stdout={r.stdout!r}"
            )


# ---------------------------------------------------------------------------
# Capabilities 10-13: the override tier
# ---------------------------------------------------------------------------


class TestOverridePhraseClassification:
    def test_classify_returns_override_for_the_whole_prompt_phrase_only(self):
        from approval_match import classify

        assert classify("approved anyway") == "override"
        assert classify("APPROVED ANYWAY.") == "override", "case/trailing-punct must normalize like is_approval"
        assert classify("  approved anyway  ") == "override"

    def test_a_qualified_phrase_does_not_classify_as_override(self):
        from approval_match import classify

        assert classify("approved anyway because the tests pass") != "override"

    def test_is_approval_stays_false_for_the_override_phrase(self):
        assert is_approval("approved anyway") is False, (
            "the override phrase must not also satisfy the ordinary exact-approval predicate"
        )


class TestOverridePhraseHookIntegration:
    def test_approved_anyway_mints_and_advances_with_no_transcript_evidence_at_all(self, tmp_path):
        from writ.session.gate_token import gate_token_path

        sid = _sid("override-mints")
        cache_dir = tmp_path / "cache"
        _seed_pending_phase_a(cache_dir, sid)
        with _mint_cleanup(sid):
            r = _run_hook(
                # No transcript_path key at all: the override tier waives evidence entirely.
                {"session_id": sid, "prompt": "approved anyway"},
                cwd=tmp_path, cache_dir=cache_dir, fake_home=tmp_path / "home",
            )
            assert os.path.exists(gate_token_path(sid)), (
                f"'approved anyway' must mint despite having no transcript evidence; "
                f"stdout={r.stdout!r}"
            )
            with open(gate_token_path(sid)) as f:
                lines = f.read().split("\n")
            assert len(lines) >= 2 and lines[1] == "phase-a", lines

    def test_the_qualified_phrase_does_not_take_the_override_path_it_asks(self, tmp_path):
        from writ.session.gate_token import gate_token_path

        sid = _sid("override-qualified")
        cache_dir = tmp_path / "cache"
        _seed_pending_phase_a(cache_dir, sid)
        with _mint_cleanup(sid):
            r = _run_hook(
                {"session_id": sid, "prompt": "approved anyway because the tests pass"},
                cwd=tmp_path, cache_dir=cache_dir, fake_home=tmp_path / "home",
            )
            assert not os.path.exists(gate_token_path(sid)), (
                f"a qualified override phrase must not mint; stdout={r.stdout!r}"
            )


# ---------------------------------------------------------------------------
# Capability 14: the replan tier requires no evidence
# ---------------------------------------------------------------------------


class TestReplanTierRequiresNoEvidence:
    def test_replan_approved_resets_a_deadlocked_session_with_no_transcript_path_at_all(
        self, tmp_path,
    ):
        from writ.session.locators import plan_md_hash

        sid = _sid("replan-noevidence")
        cache_dir = tmp_path / "cache"
        cache_dir.mkdir(parents=True, exist_ok=True)
        plan_dir = tmp_path / ".claude" / "plans" / sid
        plan_dir.mkdir(parents=True, exist_ok=True)
        (plan_dir / "plan.md").write_text(
            "# Plan\n## Files\n- `a.py` (modify) -- x\n## Analysis\nx\n"
            "## Rules Applied\nNo matching rules.\n## Capabilities\n- [ ] x\n"
        )
        h = plan_md_hash(str(tmp_path), sid)
        (cache_dir / f"writ-session-{sid}.json").write_text(json.dumps({
            "mode": "work", "current_phase": "implementation",
            "gates_approved": ["phase-a", "test-skeletons"],
            "gates_approved_plan": {"phase-a": h, "test-skeletons": h},
            "project_root": str(tmp_path), "denial_counts": {},
        }))
        with _mint_cleanup(sid):
            _run_hook(
                # Deliberately NO transcript_path key: this is the regression pin for
                # "the replan tier requires no evidence".
                {"session_id": sid, "prompt": "replan approved"},
                cwd=tmp_path, cache_dir=cache_dir, fake_home=tmp_path / "home",
            )
        after = json.loads((cache_dir / f"writ-session-{sid}.json").read_text())
        assert after.get("current_phase") == "planning", (
            f"a genuine replan approval must still reset a deadlocked session with no "
            f"transcript evidence at all: {after}"
        )
        assert after.get("gates_approved") == []


# ---------------------------------------------------------------------------
# Capabilities 15-16: the audit trail
# ---------------------------------------------------------------------------


class TestAuditTrailForEvidence:
    def test_evidence_missing_writes_exactly_one_row_naming_the_pending_gate(self, tmp_path):
        sid = _sid("audit-missing")
        cache_dir = tmp_path / "cache"
        _seed_pending_phase_a(cache_dir, sid)
        tp = write_transcript_jsonl(tmp_path, [assistant_text_row("no request here")])
        with _mint_cleanup(sid):
            _run_hook(
                {"session_id": sid, "prompt": "approved", "transcript_path": tp},
                cwd=tmp_path, cache_dir=cache_dir, fake_home=tmp_path / "home",
                extra_env={"WRIT_LOG_ROOT": str(tmp_path / "logs"), "WRIT_LOG_PROJECT": "evidence-missing-project"},
            )
        rows = _read_stream_rows("evidence-missing-project", "audit")
        matches = [r for r in rows if r.get("event") == "approval_evidence_missing"]
        assert len(matches) == 1, f"expected exactly one approval_evidence_missing row, got {rows}"
        assert matches[0].get("gate") == "phase-a", matches[0]

    def test_evidence_present_writes_no_evidence_missing_row(self, tmp_path):
        sid = _sid("audit-present")
        cache_dir = tmp_path / "cache"
        _seed_pending_phase_a(cache_dir, sid)
        tp = write_transcript_jsonl(tmp_path, [assistant_text_row("Say approved to proceed.")])
        with _mint_cleanup(sid):
            _run_hook(
                {"session_id": sid, "prompt": "approved", "transcript_path": tp},
                cwd=tmp_path, cache_dir=cache_dir, fake_home=tmp_path / "home",
                extra_env={"WRIT_LOG_ROOT": str(tmp_path / "logs"), "WRIT_LOG_PROJECT": "evidence-present-project"},
            )
        rows = _read_stream_rows("evidence-present-project", "audit")
        matches = [r for r in rows if r.get("event") == "approval_evidence_missing"]
        assert matches == [], f"evidence was present; expected zero approval_evidence_missing rows, got {matches}"

    def test_override_turn_writes_approval_evidence_override(self, tmp_path):
        sid = _sid("audit-override")
        cache_dir = tmp_path / "cache"
        _seed_pending_phase_a(cache_dir, sid)
        with _mint_cleanup(sid):
            _run_hook(
                {"session_id": sid, "prompt": "approved anyway"},
                cwd=tmp_path, cache_dir=cache_dir, fake_home=tmp_path / "home",
                extra_env={"WRIT_LOG_ROOT": str(tmp_path / "logs"), "WRIT_LOG_PROJECT": "evidence-override-project"},
            )
        rows = _read_stream_rows("evidence-override-project", "audit")
        matches = [r for r in rows if r.get("event") == "approval_evidence_override"]
        assert len(matches) == 1, f"expected exactly one approval_evidence_override row, got {rows}"


# ---------------------------------------------------------------------------
# Capability 17: the process budget, a "none"-tier prompt never reads evidence
# ---------------------------------------------------------------------------


class TestNoneTierNeverInvokesTheEvidenceReader:
    """PERF-QBUDGET-001, applied per plan.md: the evidence read sits INSIDE the
    exact/override arm, after the tier gate; the 'none' tier (every turn but one)
    is untouched. Pinned by INVOCATION (does the interpreter that reads the
    transcript ever start for a plain instruction), not by re-deriving the
    aggregate PYTHON_BUDGET tests/test_prompt_path_process_budget.py already owns.
    """

    NON_APPROVAL_PROMPT = "add retry logic and error handling to the payment client"

    def test_a_non_approval_prompt_never_starts_the_evidence_reader(self, tmp_path):
        if shutil.which("strace") is None:
            pytest.skip("strace unavailable; cannot trace execve")
        sid = _sid("budget-none")
        cache_dir = tmp_path / "cache"
        _seed_pending_phase_a(cache_dir, sid)
        fake_home = tmp_path / "home"
        fake_home.mkdir(parents=True, exist_ok=True)
        (fake_home / ".claude").mkdir(parents=True, exist_ok=True)
        env = {
            **os.environ, "WRIT_CACHE_DIR": str(cache_dir), "HOME": str(fake_home),
            "WRIT_HOST": "127.0.0.1", "WRIT_PORT": str(_closed_port()),
        }
        text = trace_execve(
            ["bash", str(HOOK_PATH)],
            input=json.dumps({"session_id": sid, "prompt": self.NON_APPROVAL_PROMPT}),
            cwd=str(tmp_path), env=env, timeout=60, str_limit=500,
        )
        assert "approval_evidence" not in text, (
            f"a non-approval prompt must never invoke the evidence reader:\n{text}"
        )

    def test_the_exact_tier_does_invoke_the_evidence_reader(self, tmp_path):
        """Anti-vacuity for the guard above: an exact-tier approval with no
        transcript_path DOES attempt to read evidence (and fails closed for lack of
        a path), so 'not found above' is a real discriminator, not strace seeing
        nothing at all."""
        if shutil.which("strace") is None:
            pytest.skip("strace unavailable; cannot trace execve")
        sid = _sid("budget-exact")
        cache_dir = tmp_path / "cache"
        _seed_pending_phase_a(cache_dir, sid)
        fake_home = tmp_path / "home"
        fake_home.mkdir(parents=True, exist_ok=True)
        (fake_home / ".claude").mkdir(parents=True, exist_ok=True)
        env = {
            **os.environ, "WRIT_CACHE_DIR": str(cache_dir), "HOME": str(fake_home),
            "WRIT_HOST": "127.0.0.1", "WRIT_PORT": str(_closed_port()),
        }
        with _mint_cleanup(sid):
            text = trace_execve(
                ["bash", str(HOOK_PATH)],
                input=json.dumps({"session_id": sid, "prompt": "approved"}),
                cwd=str(tmp_path), env=env, timeout=60, str_limit=500,
            )
        assert "approval_evidence" in text, (
            f"an exact-tier approval must invoke the evidence reader:\n{text}"
        )
