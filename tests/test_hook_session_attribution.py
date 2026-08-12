"""Every hook that buffers telemetry must carry the session identity that
telemetry is keyed on, or say by name why it does not.

THE DEFECT THIS GUARDS. Buffered rows (hook_execution via hook_instrument's exit
trap, gate_decision via log_gate_decision) key on
`${SESSION_ID:-${HOOK_SESSION_ID:-}}` (bin/lib/common.sh:1208-1211, :1284,
:333-335). A hook that calls either helper without ever setting SESSION_ID or
HOOK_SESSION_ID files its rows under the literal session id "unknown" --
unattributable to the session that produced them. Measured 2026-08-11: 445 such
rows existed, 372 from writ-comms-output-gate.sh and 17 from
session-start-bootstrap.sh. writ-debug-code-gate.sh had the identical bug, was
fixed on 2026-08-08 (hooks/scripts/writ-debug-code-gate.sh:32-44 documents the
fix and the pattern), and its rows stop there. That fix's own code comment did
NOT stop the same bug living on in sibling hooks -- which is why this must be a
test, enumerated from the directory rather than a fixed list, so the next
uninstrumented hook trips it instead of silently joining the "unknown" pile.

TWO KINDS OF TEST HERE:

  1. TestEveryTelemetryCallerCarriesAnIdentity -- a forward-guard sweep. Every
     hooks/scripts/*.sh that calls hook_instrument or log_gate_decision must
     either assign SESSION_ID / HOOK_SESSION_ID, call load_hook_env (which sets
     HOOK_SESSION_ID for its caller), or be named in EXEMPT_HOOKS below with a
     one-line reason. The hook list is derived by globbing the directory AT
     TEST TIME (module load), never hardcoded -- a hardcoded list is exactly
     the failure mode being guarded against, since it silently stops covering
     the next new hook.

  2. TestCommsOutputGateAttributesItsTelemetry -- behavioral. Drives
     writ-comms-output-gate.sh as a real subprocess with a synthetic Stop
     payload, isolated via WRIT_CACHE_DIR / WRIT_LOG_ROOT pointed at tmp_path,
     and inspects the session-scoped BUFFER FILE the hook itself appends to
     (writ_event_buffer_append / log_gate_decision's fast path). That buffer is
     the artifact this hook directly writes; the DRAINED log row is produced by
     a *different* Stop hook (friction-logger.sh / writ-session-end.sh call
     writ_event_buffer_flush -- this hook never does), so asserting against a
     drained log here would test code this file does not own and that is
     concurrently owned by another agent (writ/shared/logging.py). Isolated:
     no real WRIT_CACHE_DIR, no real WRIT_LOG_ROOT, no daemon
     (WRIT_NO_AUTOSTART=1), nothing under this hook's control touches real
     session state.

     session-start-bootstrap.sh is NOT driven behaviorally here: it bootstraps
     real sessions (starts the daemon, probes Neo4j, rotates session state),
     and the task that produced this file explicitly forbids driving it
     unisolated. Its coverage here is the static sweep only.

Per TEST-TDD-001: skeletons approved before implementation. Written while the
comms-output-gate / session-start-bootstrap fix is landing concurrently on this
branch, so the sweep may already read GREEN for those two files by the time
this runs -- see the report for the actually-observed result.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
HOOKS_DIR = REPO / "hooks" / "scripts"

# ---------------------------------------------------------------------------
# Comment stripping, matching the discipline tests/test_session_identity_no_
# fallback.py already established: scan the CODE, not prose ABOUT the code. A
# hook's own postmortem comment quotes "hook_instrument" and "SESSION_ID" by
# name (see writ-debug-code-gate.sh's own header), and matching those would make
# the only passing move "stop documenting the fix", which is backwards.
# ---------------------------------------------------------------------------
_FULL_LINE_COMMENT = re.compile(r"^[ \t]*#.*$", re.M)
_TRAILING_COMMENT = re.compile(r"[ \t]#[ \t].*$", re.M)


def _strip_comments(src: str) -> str:
    return _TRAILING_COMMENT.sub("", _FULL_LINE_COMMENT.sub("", src))


# A call to either telemetry helper. Neither is ever DEFINED under hooks/scripts
# (only bin/lib/common.sh defines them), so a word-boundary match after comment
# stripping is a call, not a definition or a stray mention.
CALLS_TELEMETRY = re.compile(r"\b(?:hook_instrument|log_gate_decision)\b")

# An assignment to either variable the telemetry helpers fall back to, or a call
# to the one function (load_hook_env) that sets HOOK_SESSION_ID on the caller's
# behalf. `\b` before SESSION_ID / HOOK_SESSION_ID means a variable like
# PARENT_SESSION_ID= does NOT satisfy this -- the fallback in common.sh reads
# exactly these two names and nothing else.
HAS_SESSION_IDENTITY = re.compile(
    r"\b(?:SESSION_ID|HOOK_SESSION_ID)\s*=|\bload_hook_env\b"
)

# THE EXEMPTIONS, by name, each with a one-line reason -- modeled on
# tests/test_session_identity_no_fallback.py's POINTER_READ_EXEMPT. An allowlist
# nobody re-checks is a hole, so TestExemptionsStayJustified below holds this
# set to exactly the reason each entry was granted for, and
# test_only_the_named_hooks_are_exempt pins the set itself so a THIRD exemption
# cannot be added without a conscious edit here.
EXEMPT_HOOKS: dict[str, str] = {
    "writ-blackbox-capture.sh": (
        "hook_instrument is only reached past the WRIT_BLACKBOX opt-in gate "
        "(line 9: exits before sourcing common.sh unless WRIT_BLACKBOX=1 or the "
        "~/.claude/writ-blackbox.on sentinel exists); blackbox capture is opt-in "
        "and was measured OFF in production, so this call contributes zero rows "
        "to the measured writ-events-unknown.buf despite a naive grep flagging it."
    ),
    "writ-subagent-start.sh": (
        "log_gate_decision fires only on the narrow manual-test-grant inherit "
        "success path (PARENT_SESSION set, PARENT_SESSION != AGENT_ID, and the "
        "grant python call exits 0); the hook's own comment says plainly 'This "
        "hook has no SESSION_ID' (its identity is AGENT_ID, read via a different "
        "path entirely), and it measured zero rows in the unknown-session buffer "
        "despite a naive grep flagging it."
    ),
}


def _all_hook_scripts() -> list[Path]:
    """Every hooks/scripts/*.sh file, globbed fresh every time this runs."""
    return sorted(HOOKS_DIR.glob("*.sh"))


def _telemetry_callers() -> list[Path]:
    """Every hook that calls hook_instrument or log_gate_decision, derived from
    the directory at test time -- never a hardcoded filename list."""
    return [
        p
        for p in _all_hook_scripts()
        if CALLS_TELEMETRY.search(_strip_comments(p.read_text(errors="replace")))
    ]


# Computed once at module load (i.e. at pytest collection / "test time"), so the
# parametrization below always reflects whatever is on disk right now.
_ALL_HOOKS = _all_hook_scripts()
_CALLERS = _telemetry_callers()


# ---------------------------------------------------------------------------
# Anti-vacuity for the enumeration itself. A broken glob (wrong directory, wrong
# suffix, a typo'd extension) would make every assertion below pass by scanning
# nothing, which is indistinguishable from "every hook is fixed".
# ---------------------------------------------------------------------------
class TestTheEnumerationIsNotVacuous:
    def test_the_directory_holds_a_plausible_number_of_shell_hooks(self) -> None:
        assert len(_ALL_HOOKS) > 20, (
            f"expected hooks/scripts/*.sh to hold more than 20 files; found "
            f"{len(_ALL_HOOKS)}: {[p.name for p in _ALL_HOOKS]}. HOOKS_DIR may be "
            f"pointed at the wrong directory -- every other assertion in this "
            f"file depends on this glob actually reaching the hooks."
        )

    def test_a_plausible_number_of_hooks_call_the_telemetry_helpers(self) -> None:
        assert len(_CALLERS) >= 15, (
            f"expected at least 15 hooks/scripts/*.sh files to call "
            f"hook_instrument or log_gate_decision; found {len(_CALLERS)}: "
            f"{[p.name for p in _CALLERS]}. If this count dropped, the CALLS_"
            f"TELEMETRY regex or the comment-stripping is broken, not the hooks."
        )

    def test_not_every_hook_calls_the_telemetry_helpers(self) -> None:
        """The reverse direction: a regex that matches every file (e.g. an
        unescaped wildcard) would also pass the count check above for the wrong
        reason. Some hooks in this directory call neither helper."""
        assert len(_CALLERS) < len(_ALL_HOOKS)


# ---------------------------------------------------------------------------
# THE SWEEP. One parametrized case per caller, so a single offending hook fails
# on its own line naming itself, rather than disappearing into an aggregate.
# ---------------------------------------------------------------------------
class TestEveryTelemetryCallerCarriesAnIdentity:
    @pytest.mark.parametrize("path", _CALLERS, ids=[p.name for p in _CALLERS])
    def test_hook_assigns_an_identity_or_is_named_exempt(self, path: Path) -> None:
        if path.name in EXEMPT_HOOKS:
            pytest.skip(f"{path.name} is exempt: {EXEMPT_HOOKS[path.name]}")

        src = _strip_comments(path.read_text(errors="replace"))
        assert HAS_SESSION_IDENTITY.search(src), (
            f"{path.name} calls hook_instrument or log_gate_decision, whose "
            f"telemetry keys on ${{SESSION_ID:-${{HOOK_SESSION_ID:-}}}} "
            f"(bin/lib/common.sh:1208-1211, :1284), but this file never assigns "
            f"SESSION_ID, never assigns HOOK_SESSION_ID, and never calls "
            f"load_hook_env. Every row it produces lands under the literal "
            f"session id 'unknown', unattributable to the session that produced "
            f"it (see bin/lib/common.sh:333-335). TO FIX, add ONE of: "
            f"(1) extract the identity from this hook's own payload -- agent_id "
            f"first, then session_id, so a sub-agent's rows are not filed under "
            f"its parent -- and assign it to SESSION_ID before the first "
            f"hook_instrument/log_gate_decision call (see the pattern at "
            f"hooks/scripts/writ-debug-code-gate.sh:23-30); "
            f"(2) call load_hook_env, if this hook has not already consumed "
            f"stdin itself; or "
            f"(3) if -- and only if -- {path.name} is proven to produce zero "
            f"rows in the measured unknown-session buffer, add '{path.name}' to "
            f"EXEMPT_HOOKS in tests/test_hook_session_attribution.py with a "
            f"one-line reason, and add coverage in "
            f"TestExemptionsStayJustified pinning that reason."
        )


# ---------------------------------------------------------------------------
# Anti-vacuity for the two regexes the sweep depends on: they must have real
# teeth against the exact shape of the historical bug, not just look plausible.
# ---------------------------------------------------------------------------
class TestTheDetectionHasTeeth:
    def test_a_bare_telemetry_call_with_no_identity_is_flagged(self) -> None:
        broken = 'source common.sh\nhook_instrument "fake-hook"\nexit 0\n'
        stripped = _strip_comments(broken)
        assert CALLS_TELEMETRY.search(stripped)
        assert not HAS_SESSION_IDENTITY.search(stripped)

    def test_a_bare_log_gate_decision_call_with_no_identity_is_flagged(self) -> None:
        broken = 'log_gate_decision "some-gate" "allow" "reason" "target"\n'
        stripped = _strip_comments(broken)
        assert CALLS_TELEMETRY.search(stripped)
        assert not HAS_SESSION_IDENTITY.search(stripped)

    def test_assigning_session_id_before_the_call_clears_it(self) -> None:
        fixed = 'SESSION_ID="$SID"\nhook_instrument "fake-hook"\n'
        assert HAS_SESSION_IDENTITY.search(_strip_comments(fixed))

    def test_assigning_hook_session_id_clears_it(self) -> None:
        fixed = 'HOOK_SESSION_ID=$(detect_session_id "$STDIN_JSON")\nlog_gate_decision "g" "allow" "r" "t"\n'
        assert HAS_SESSION_IDENTITY.search(_strip_comments(fixed))

    def test_calling_load_hook_env_clears_it(self) -> None:
        fixed = 'load_hook_env\nhook_instrument "fake-hook"\n'
        assert HAS_SESSION_IDENTITY.search(_strip_comments(fixed))

    def test_a_mention_only_in_a_comment_does_not_clear_it(self) -> None:
        """The exact shape of the miss the identity-fallback sweep in
        test_session_identity_no_fallback.py was retrofitted for: a hook that
        only TALKS about setting SESSION_ID in a comment, with no live
        assignment, must still be flagged."""
        broken = (
            '# SESSION_ID is set somewhere else already, trust me\n'
            'hook_instrument "fake-hook"\n'
        )
        stripped = _strip_comments(broken)
        assert CALLS_TELEMETRY.search(stripped)
        assert not HAS_SESSION_IDENTITY.search(stripped)

    def test_a_variable_that_merely_contains_session_id_as_a_suffix_does_not_clear_it(
        self,
    ) -> None:
        """PARENT_SESSION_ID is not SESSION_ID: common.sh's fallback reads
        exactly SESSION_ID / HOOK_SESSION_ID, so a differently-named variable
        gives the caller nothing."""
        broken = 'PARENT_SESSION_ID="whatever"\nhook_instrument "fake-hook"\n'
        assert not HAS_SESSION_IDENTITY.search(_strip_comments(broken))


# ---------------------------------------------------------------------------
# The exemption list held to the same standard test_session_identity_no_
# fallback.py holds POINTER_READ_EXEMPT to: named, reasoned, and re-verified
# rather than trusted forever.
# ---------------------------------------------------------------------------
class TestExemptionsStayJustified:
    def test_only_the_named_hooks_are_exempt(self) -> None:
        """Pins the set itself, so a third exemption cannot be added silently --
        adding one here is a conscious edit to this test, not a side effect of
        editing the dict above."""
        assert set(EXEMPT_HOOKS) == {
            "writ-blackbox-capture.sh",
            "writ-subagent-start.sh",
        }, (
            "the exemption set changed. That may be correct, but review the new "
            "entry's reason on its own merits -- this pin exists so growing the "
            "set is a deliberate edit, not a silent one."
        )

    def test_every_exemption_names_a_hook_that_still_exists(self) -> None:
        for name in EXEMPT_HOOKS:
            assert (HOOKS_DIR / name).exists(), (
                f"{name} is exempted but no longer exists under {HOOKS_DIR}; "
                f"delete the stale exemption entry."
            )

    def test_every_exemption_still_calls_a_telemetry_helper(self) -> None:
        """An exemption that no longer matches anything it was granted for is
        dead weight that could hide a REAL future offender behind a name that
        no longer means what it once meant."""
        callers = {p.name for p in _CALLERS}
        for name in EXEMPT_HOOKS:
            assert name in callers, (
                f"{name} is exempted from carrying a session identity because "
                f"it was said to call hook_instrument or log_gate_decision, but "
                f"it no longer does. Drop the exemption -- it covers nothing."
            )

    def test_every_exemption_has_a_substantive_reason(self) -> None:
        for name, reason in EXEMPT_HOOKS.items():
            assert len(reason.strip()) > 40, (
                f"{name}'s exemption reason is too short to be checkable: "
                f"{reason!r}"
            )

    def test_blackbox_capture_reason_still_matches_its_opt_in_gate(self) -> None:
        """Re-verifies the reason against the live file, not just the dict:
        if writ-blackbox-capture.sh's opt-in gate is ever removed, this
        exemption's justification disappears with it."""
        src = (HOOKS_DIR / "writ-blackbox-capture.sh").read_text()
        assert "WRIT_BLACKBOX" in src and "hook_instrument" in src

    def test_subagent_start_reason_still_matches_its_no_session_id_comment(self) -> None:
        src = (HOOKS_DIR / "writ-subagent-start.sh").read_text()
        assert "This hook has no SESSION_ID" in src, (
            "writ-subagent-start.sh's exemption is grounded in this hook "
            "deliberately carrying no SESSION_ID; if that comment/behavior is "
            "gone, re-derive whether the exemption still holds"
        )


# ---------------------------------------------------------------------------
# BEHAVIORAL: drive the real hook as a subprocess and inspect the buffer file
# it appends to directly. See the module docstring for why the buffer (and not
# a drained log row) is the right artifact to assert against here.
# ---------------------------------------------------------------------------
class TestCommsOutputGateAttributesItsTelemetry:
    HOOK = HOOKS_DIR / "writ-comms-output-gate.sh"

    def _write_transcript(self, tmp_path: Path, text: str) -> Path:
        entry = {
            "type": "assistant",
            "message": {"content": [{"type": "text", "text": text}]},
        }
        transcript = tmp_path / "transcript.jsonl"
        transcript.write_text(json.dumps(entry) + "\n", encoding="utf-8")
        return transcript

    def _run(self, tmp_path: Path, payload: dict) -> tuple[subprocess.CompletedProcess, Path]:
        cache_dir = tmp_path / "cache"
        log_dir = tmp_path / "logs"
        cache_dir.mkdir()
        log_dir.mkdir()
        proc = subprocess.run(
            ["bash", str(self.HOOK)],
            input=json.dumps(payload),
            text=True,
            capture_output=True,
            timeout=30,
            env={
                **os.environ,
                "WRIT_CACHE_DIR": str(cache_dir),
                "WRIT_LOG_ROOT": str(log_dir),
                "WRIT_NO_AUTOSTART": "1",
            },
        )
        return proc, cache_dir

    def test_a_distinctive_session_id_is_attributed_to_its_own_buffer(
        self, tmp_path: Path
    ) -> None:
        transcript = self._write_transcript(tmp_path, "Clean reply, no forbidden punctuation.")
        sid = "distinctive-session-c9f3a1"
        proc, cache_dir = self._run(
            tmp_path,
            {
                "session_id": sid,
                "stop_hook_active": False,
                "transcript_path": str(transcript),
            },
        )
        assert proc.returncode == 0, (
            f"hook did not exit cleanly on a clean transcript: "
            f"rc={proc.returncode} stderr={proc.stderr!r}"
        )
        own_buffer = cache_dir / f"writ-events-{sid}.buf"
        assert own_buffer.exists(), (
            f"expected the hook's own telemetry buffer at {own_buffer.name}; "
            f"found instead {[p.name for p in cache_dir.iterdir()]}. The row was "
            f"filed under a different key than the payload's session_id."
        )
        assert not (cache_dir / "writ-events-unknown.buf").exists(), (
            "a session id WAS present in the payload, and the row still landed "
            "under the literal 'unknown' buffer -- exactly the defect this test "
            "guards (bin/lib/common.sh:1284, :333-335)."
        )

    def test_agent_id_wins_over_session_id_for_a_subagent(self, tmp_path: Path) -> None:
        """Matches the resolution order the fixed hook documents and the
        canonical pattern at writ-debug-code-gate.sh:23-30: agent_id first, so a
        sub-agent's rows are never filed under its parent's session."""
        transcript = self._write_transcript(tmp_path, "Clean reply.")
        proc, cache_dir = self._run(
            tmp_path,
            {
                "session_id": "parent-session",
                "agent_id": "child-agent-77",
                "stop_hook_active": False,
                "transcript_path": str(transcript),
            },
        )
        assert proc.returncode == 0, proc.stderr
        assert (cache_dir / "writ-events-child-agent-77.buf").exists(), (
            f"expected the sub-agent's own id to win; found "
            f"{[p.name for p in cache_dir.iterdir()]}"
        )
        assert not (cache_dir / "writ-events-parent-session.buf").exists(), (
            "the row was filed under the PARENT's session id instead of the "
            "sub-agent's agent_id"
        )

    def test_the_buffered_row_is_the_real_gate_decision_not_an_empty_file(
        self, tmp_path: Path
    ) -> None:
        """The attribution tests above only check the FILENAME. Without this,
        a hook that created an empty file under the right name would pass them
        for the wrong reason."""
        transcript = self._write_transcript(tmp_path, "Clean reply.")
        sid = "row-content-check"
        proc, cache_dir = self._run(
            tmp_path,
            {
                "session_id": sid,
                "stop_hook_active": False,
                "transcript_path": str(transcript),
            },
        )
        assert proc.returncode == 0, proc.stderr
        raw = (cache_dir / f"writ-events-{sid}.buf").read_text()
        assert "gate_decision" in raw, f"no gate_decision row in the buffer: {raw!r}"
        assert "comms-output" in raw, f"gate_decision row does not name its own gate: {raw!r}"
        assert "allow" in raw, f"a clean transcript must record an 'allow' decision: {raw!r}"

    def test_no_session_id_anywhere_stays_unattributed_not_invented(
        self, tmp_path: Path
    ) -> None:
        """The failure must stay VISIBLE. A payload that names no session must
        never resolve to a real-looking id -- no PID, no md5(cwd:user), nothing
        synthesized. Filing under the literal 'unknown' sentinel is a visible
        gap; inventing a plausible-looking id would be a silently WRONG record,
        which is the worse outcome (see writ-debug-code-gate.sh:41-43's own
        framing of the same choice)."""
        transcript = self._write_transcript(tmp_path, "Clean reply.")
        proc, cache_dir = self._run(
            tmp_path,
            {
                "stop_hook_active": False,
                "transcript_path": str(transcript),
            },
        )
        assert proc.returncode == 0, proc.stderr
        names = sorted(p.name for p in cache_dir.iterdir())
        assert names == ["writ-events-unknown.buf"], (
            f"expected exactly one buffer, the literal 'unknown' sentinel, when "
            f"the payload names no session; found {names}. Any OTHER name would "
            f"mean an id was invented from something other than the payload."
        )

    def test_stop_hook_active_continuation_is_still_attributed(self, tmp_path: Path) -> None:
        """The identity is keyed BEFORE the stop_hook_active guard exits
        (writ-comms-output-gate.sh's own comment: keying it after would leave
        every continuation-Stop row unattributed). A continuation Stop must
        still land under the real session, not under 'unknown'."""
        transcript = self._write_transcript(tmp_path, "irrelevant, guard exits first")
        sid = "continuation-session-42"
        proc, cache_dir = self._run(
            tmp_path,
            {
                "session_id": sid,
                "stop_hook_active": True,
                "transcript_path": str(transcript),
            },
        )
        assert proc.returncode == 0, proc.stderr
        assert (cache_dir / f"writ-events-{sid}.buf").exists(), (
            f"a stop_hook_active continuation must still attribute its "
            f"hook_execution row to the real session; found "
            f"{[p.name for p in cache_dir.iterdir()]}"
        )
        assert not (cache_dir / "writ-events-unknown.buf").exists()


# ---------------------------------------------------------------------------
# PART 6b: resolve_project() must not mislabel an ungitted cwd as "writ".
#
# THE DEFECT. A cwd outside any git repo makes derive_project_identity raise
# NotInRepoError, and resolve_project() fell back to the literal string
# "writ" -- the Writ skill's OWN log scope. That project's mode-change (and
# every other) row was filed where its actual owner could never find it, and
# it read indistinguishably from a genuine Writ-repo event. The fix trades a
# wrong label for an honest one, not for silence: the row must still land
# somewhere, under a marker that is explicitly NOT a real project name.
# ---------------------------------------------------------------------------
class TestResolveProjectDoesNotFallBackToWrit:
    """`resolve_project()` for a cwd with no `.git` anywhere above it must
    return an explicit unresolved marker, never the literal 'writ'."""

    def test_ungitted_cwd_does_not_resolve_to_the_literal_writ(self, tmp_path) -> None:
        from writ.shared.logging import resolve_project

        result = resolve_project(cwd=str(tmp_path))

        assert result != "writ", (
            f"resolve_project() returned the literal 'writ' for {tmp_path}, a "
            f"cwd with no .git anywhere above it. That string names the Writ "
            f"skill's OWN log scope, so this project's row would be mislabelled "
            f"into it -- exactly the defect this test guards."
        )

    def test_ungitted_cwd_still_resolves_to_a_non_empty_marker(self, tmp_path) -> None:
        """The row must stay recorded, not dropped: the fix trades a wrong
        label for an honest one, not for silence."""
        from writ.shared.logging import resolve_project

        result = resolve_project(cwd=str(tmp_path))

        assert isinstance(result, str) and result.strip(), (
            f"resolve_project() must return a non-empty marker for an ungitted "
            f"cwd so the row is still recorded; got {result!r}"
        )

    def test_ungitted_cwd_marker_is_stable_across_calls(self, tmp_path) -> None:
        """A fixed sentinel, not something derived per-call: two rows from the
        same ungitted project must land together, not scatter across
        different log scopes."""
        from writ.shared.logging import resolve_project

        first = resolve_project(cwd=str(tmp_path))
        second = resolve_project(cwd=str(tmp_path))

        assert first == second

    def test_ungitted_cwd_row_is_recorded_under_the_marker_not_under_writ(
        self, tmp_path, monkeypatch
    ) -> None:
        """End-to-end through `emit`: a row from an ungitted cwd must land on
        disk under whatever resolve_project() now returns, not under 'writ'
        and not nowhere.

        DETERMINISM NOTE. This test was previously reported flaky (FileNotFoundError
        from iterdir(), once in seven runs): emit()'s `if friction_log:` branch
        (writ/shared/logging.py:422-428) takes priority over the per-project router
        this test exercises, so ANY leaked WRIT_FRICTION_LOG value at call time makes
        emit() append to that single file instead of ever creating `writ_root` --
        and iterdir() on a directory that was never created raises FileNotFoundError
        rather than returning empty, which is a confusing way to learn the real
        precondition was violated. The delenv below already clears the variable and
        nothing runs between it and the `emit()` call that could reintroduce it, but
        the assertion immediately before the call pins that precondition explicitly
        at its point of use: if it is EVER violated again (a sibling fixture change,
        an inherited shell export, a future edit that inserts a call in between), this
        fails on its own named line instead of resurfacing as an opaque iterdir()
        crash three lines later.
        """
        from writ.shared import logging as writ_logging
        from writ.shared.logging import UNRESOLVED_PROJECT

        # The suite's autouse _isolate_friction_log fixture (tests/conftest.py)
        # points WRIT_FRICTION_LOG at one collapsed file by default, which would
        # short-circuit emit() before it ever reaches resolve_project() / the
        # per-project stream router this test exercises. Opt out locally rather
        # than a module-level `no_friction_isolation` mark, since every other
        # test in this file is unaffected by the router and should keep the
        # default isolation.
        monkeypatch.delenv("WRIT_FRICTION_LOG", raising=False)
        monkeypatch.setenv("WRIT_LOG_ROOT", str(tmp_path / "logs"))
        monkeypatch.delenv("WRIT_LOG_PROJECT", raising=False)
        monkeypatch.chdir(tmp_path)

        assert "WRIT_FRICTION_LOG" not in os.environ, (
            "WRIT_FRICTION_LOG is still set immediately before emit() despite the "
            "delenv above; emit() would take its single-file early-return branch "
            "(writ/shared/logging.py:422-428) instead of exercising the per-project "
            "router this test exists to check -- this is the exact precondition "
            "whose violation previously surfaced as a flaky FileNotFoundError from "
            "iterdir() on a `logs` dir that was never created."
        )

        writ_logging.emit(None, "mode_change", "sid-ungitted", "work", change_type="set")

        writ_root = tmp_path / "logs"
        assert not (writ_root / "writ").exists(), (
            "the row landed under the 'writ' project scope; an ungitted "
            "project's row must never be filed into the Writ skill's own log "
            "space"
        )
        assert writ_root.is_dir(), (
            f"{writ_root} was never created; the row must still be recorded, not "
            f"silently dropped (emit() likely took the WRIT_FRICTION_LOG "
            f"single-file branch instead of the per-project router)"
        )
        project_dirs = {p.name for p in writ_root.iterdir() if p.is_dir()}
        assert project_dirs == {UNRESOLVED_PROJECT}, (
            f"expected exactly the {UNRESOLVED_PROJECT!r} project directory under "
            f"{writ_root}; found {sorted(project_dirs)}"
        )
