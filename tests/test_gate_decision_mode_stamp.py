"""A gate_decision row must carry the session's real mode, whatever hook logged it.

log_gate_decision stamped the row from the bare shell variable
`${CURRENT_MODE:-${MODE:-}}`. Exactly ONE of the fourteen gate-emitting hooks ever
assigns it (validate-test-file.sh, whose rows measure 0% null across 321 rows); the
other thirteen never do, and their rows measured 100% null -- 4,672 of 4,993
gate_decision rows (93.6%) recorded `"mode": null` while the session cache said "work".
A null there is indistinguishable from an honest mode-unset session, so the audit
stream could not answer "which mode was this decision made under".

These tests drive REAL hooks from the defective thirteen, one per emit path, because
the two paths stamp the mode independently:

  * pre-validate-file.sh logs an "allow"  -> BUFFERED row (drained by writ-flush-events)
  * writ-state-write-gate.sh logs a "deny" -> SYNCHRONOUS row (_gd_emit_now)

The remaining tests pin the shape of the fix rather than the outcome: an explicitly set
variable still wins (so the one hook that had this right does not regress), an empty
session resolves to an empty mode without a lookup, and instrumentation alone never
triggers one -- a resolution in hook_instrument's exit trap would put a process back on
all fifteen hooks a single file write fires.

Isolation: WRIT_CACHE_DIR / WRIT_LOG_ROOT / WRIT_FRICTION_LOG all point at tmp_path, so
no test reads or writes live session state or the live logs.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
HOOKS = REPO / "hooks" / "scripts"
HELPER = REPO / "bin" / "lib" / "writ-session.py"
FLUSH = REPO / "bin" / "lib" / "writ-flush-events.py"
COMMON = REPO / "bin" / "lib" / "common.sh"


def _env(tmp_path: Path) -> dict:
    """A throwaway cache dir and log destination; nothing touches live state."""
    env = os.environ.copy()
    env["WRIT_CACHE_DIR"] = str(tmp_path / "cache")
    env["WRIT_LOG_ROOT"] = str(tmp_path / "logs")
    env["WRIT_FRICTION_LOG"] = str(tmp_path / "friction.log")
    env["WRIT_NO_AUTOSTART"] = "1"
    (tmp_path / "cache").mkdir(parents=True, exist_ok=True)
    return env


def _seed_mode(tmp_path: Path, sid: str, mode: str) -> None:
    """Set the session's mode exactly as production does (file-direct CLI)."""
    subprocess.run(
        [sys.executable, str(HELPER), "mode", "set", mode, sid],
        env=_env(tmp_path), check=True, capture_output=True, text=True, timeout=60,
    )


def _rows(tmp_path: Path, sid: str) -> list[dict]:
    """Every row on this test's log, buffered rows drained first.

    TWO SINKS, ONE COLLECTOR: an "allow" is buffered and anything else is emitted
    synchronously, so a test that read one sink would see one branch of the thing
    under test.
    """
    subprocess.run([sys.executable, str(FLUSH), sid], env=_env(tmp_path),
                   capture_output=True, text=True, timeout=60)
    log = Path(_env(tmp_path)["WRIT_FRICTION_LOG"])
    if not log.exists():
        return []
    return [json.loads(ln) for ln in log.read_text().splitlines() if ln.strip()]


def _gate_rows(tmp_path: Path, sid: str, gate: str) -> list[dict]:
    return [r for r in _rows(tmp_path, sid)
            if r.get("event") == "gate_decision" and r.get("gate") == gate]


def _run_hook(tmp_path: Path, hook: str, envelope: dict) -> subprocess.CompletedProcess:
    res = subprocess.run(
        ["bash", str(HOOKS / hook)], input=json.dumps(envelope),
        capture_output=True, text=True, env=_env(tmp_path), timeout=120,
    )
    assert res.returncode == 0, f"{hook} exited {res.returncode}: {res.stderr}"
    return res


def _probe(tmp_path: Path, *, sid: str, decision: str, preset: str = "") -> subprocess.CompletedProcess:
    """A minimal hook: source common.sh, instrument, log one decision.

    Used where a real hook cannot express the case (an empty session id, or a hook that
    sets the mode variable itself). It is the same code path the hooks take -- common.sh
    is the single consumer of the mode value.
    """
    script = tmp_path / "mode-stamp-probe.sh"
    script.write_text(
        "#!/usr/bin/env bash\n"
        "set -euo pipefail\n"
        f'source "{COMMON}"\n'
        'hook_instrument "mode-stamp-probe"\n'
        f'SESSION_ID="{sid}"\n'
        f"{preset}\n"
        f'log_gate_decision "probe-gate" "{decision}" "probe reason" "/tmp/probe-target"\n'
        "exit 0\n"
    )
    res = subprocess.run(["bash", str(script)], capture_output=True, text=True,
                         env=_env(tmp_path), timeout=120)
    assert res.returncode == 0, f"probe exited {res.returncode}: {res.stderr}"
    return res


class TestRealHooksStampTheSessionMode:
    """The defect as measured: a hook that never assigns the variable logged null."""

    def test_buffered_allow_row_carries_the_session_mode(self, tmp_path) -> None:
        """pre-validate-file.sh logs `pre-write-validation allow` on every clean write
        and never assigns CURRENT_MODE/MODE. It is the highest-volume row of the 93.6%."""
        _seed_mode(tmp_path, "s-allow", "work")
        target = tmp_path / "probe_target.py"
        _run_hook(tmp_path, "pre-validate-file.sh", {
            "session_id": "s-allow",
            "hook_event_name": "PreToolUse",
            "tool_name": "Write",
            "tool_input": {"file_path": str(target), "content": "x = 1\n"},
        })
        rows = _gate_rows(tmp_path, "s-allow", "pre-write-validation")
        assert rows, "pre-validate-file.sh logged no gate_decision at all"
        assert [r.get("mode") for r in rows] == ["work"] * len(rows), (
            f"the gate ran in a work session but recorded {[r.get('mode') for r in rows]}; "
            f"a null mode is indistinguishable from an honest mode-unset session"
        )

    def test_synchronous_deny_row_carries_the_session_mode(self, tmp_path) -> None:
        """writ-state-write-gate.sh logs `state-write deny`, which bypasses the buffer
        and is emitted immediately -- the second, independent stamping site."""
        _seed_mode(tmp_path, "s-deny", "work")
        protected = Path(_env(tmp_path)["WRIT_CACHE_DIR"]) / "writ-session-s-deny.json"
        _run_hook(tmp_path, "writ-state-write-gate.sh", {
            "session_id": "s-deny",
            "hook_event_name": "PreToolUse",
            "tool_name": "Write",
            "tool_input": {"file_path": str(protected), "content": "{}"},
        })
        rows = _gate_rows(tmp_path, "s-deny", "state-write")
        assert rows, "writ-state-write-gate.sh logged no denial"
        assert rows[0].get("mode") == "work", (
            f"a DENIAL -- the record that proves a gate blocked something -- recorded "
            f"mode {rows[0].get('mode')!r} in a work session"
        )

    def test_the_session_really_was_in_that_mode(self, tmp_path) -> None:
        """Anti-vacuity: the assertions above would also pass if `mode set` silently
        wrote "work" nowhere and the stamp came from a default."""
        _seed_mode(tmp_path, "s-real", "investigate")
        target = tmp_path / "probe_target.py"
        _run_hook(tmp_path, "pre-validate-file.sh", {
            "session_id": "s-real",
            "hook_event_name": "PreToolUse",
            "tool_name": "Write",
            "tool_input": {"file_path": str(target), "content": "x = 1\n"},
        })
        rows = _gate_rows(tmp_path, "s-real", "pre-write-validation")
        assert rows and rows[0].get("mode") == "investigate", (
            f"expected the seeded mode to reach the row, got "
            f"{[r.get('mode') for r in rows]}"
        )

    def test_hook_execution_row_reuses_the_resolved_mode(self, tmp_path) -> None:
        """A hook that already resolved a mode for its decision stamps its telemetry row
        with the same value -- free, from the memo, no second lookup."""
        _seed_mode(tmp_path, "s-exec", "work")
        target = tmp_path / "probe_target.py"
        _run_hook(tmp_path, "pre-validate-file.sh", {
            "session_id": "s-exec",
            "hook_event_name": "PreToolUse",
            "tool_name": "Write",
            "tool_input": {"file_path": str(target), "content": "x = 1\n"},
        })
        execs = [r for r in _rows(tmp_path, "s-exec")
                 if r.get("event") == "hook_execution"]
        assert execs, "no hook_execution row was recorded"
        assert all(r.get("mode") == "work" for r in execs), (
            f"hook_execution rows recorded {[r.get('mode') for r in execs]}"
        )


class TestExplicitModeWins:
    """The fallback is a fallback. A hook that computed the mode itself is the
    authority on it, and the one hook that always got this right must not regress."""

    @pytest.mark.parametrize("var", ["CURRENT_MODE", "MODE"])
    def test_an_explicitly_set_variable_beats_the_session_cache(self, tmp_path, var) -> None:
        _seed_mode(tmp_path, "s-explicit", "work")
        _probe(tmp_path, sid="s-explicit", decision="deny", preset=f'{var}="review"')
        rows = _gate_rows(tmp_path, "s-explicit", "probe-gate")
        assert rows and rows[0].get("mode") == "review", (
            f"{var} was set to review; the row recorded {rows[0].get('mode')!r} "
            f"(the cache said work, so the fallback overrode the caller)"
        )

    def test_an_explicit_empty_string_is_not_an_answer(self, tmp_path) -> None:
        """`MODE=""` is what an unset variable looks like to every caller in the tree,
        so it falls through to the session -- otherwise the fix would be defeated by any
        hook that pre-declares the variable."""
        _seed_mode(tmp_path, "s-blank", "work")
        _probe(tmp_path, sid="s-blank", decision="deny", preset='MODE=""')
        rows = _gate_rows(tmp_path, "s-blank", "probe-gate")
        assert rows and rows[0].get("mode") == "work"


class TestNoSessionNoLookup:
    def test_an_empty_session_yields_an_empty_mode(self, tmp_path) -> None:
        """No session id means no session to read: the row records null, and the hook
        neither errors nor pays for a lookup that cannot succeed."""
        res = _probe(tmp_path, sid="", decision="deny")
        assert res.stderr.strip() == "", f"the empty-session path wrote to stderr: {res.stderr}"
        rows = [r for r in _rows(tmp_path, "")
                if r.get("event") == "gate_decision" and r.get("gate") == "probe-gate"]
        assert rows, "the decision was not recorded at all"
        assert rows[0].get("mode") is None, (
            f"an unknown session must record a null mode, got {rows[0].get('mode')!r}"
        )


@pytest.mark.skipif(shutil.which("strace") is None,
                    reason="strace unavailable; cannot count execve")
class TestResolutionIsLazyAndMemoized:
    """The cost side of the fix, guarded because it is invisible in the output.

    A file write fires fifteen hooks and the process budget (test_write_path_process_
    budget.py) has single-digit headroom, so where the resolution happens is not a
    style question: in hook_instrument's exit trap it would run on every instrumented
    hook, including the thirteen that never log a decision.
    """

    def _trace(self, tmp_path: Path, body: str) -> list[str]:
        script = tmp_path / "trace-probe.sh"
        script.write_text(
            "#!/usr/bin/env bash\n"
            "set -euo pipefail\n"
            f'source "{COMMON}"\n'
            'hook_instrument "trace-probe"\n'
            f"{body}\n"
            "exit 0\n"
        )
        trace = tmp_path / "trace.txt"
        subprocess.run(
            ["strace", "-f", "-qq", "-e", "trace=execve", "-o", str(trace),
             "bash", str(script)],
            capture_output=True, text=True, timeout=180, env=_env(tmp_path),
        )
        if not trace.exists():
            pytest.skip("strace produced no trace")
        return trace.read_text(errors="replace").splitlines()

    def _lookups(self, lines: list[str]) -> list[str]:
        """execve of the readers writ_session_mode_direct can use (jq or python3)."""
        return [ln for ln in lines if re.search(r'"[^"]*/(jq|python3)"', ln)]

    def test_instrumentation_alone_resolves_nothing(self, tmp_path) -> None:
        _seed_mode(tmp_path, "s-lazy", "work")
        lines = self._trace(tmp_path, 'SESSION_ID="s-lazy"')
        assert self._lookups(lines) == [], (
            "a hook that logs no decision paid for a mode lookup; multiplied by the "
            f"fifteen hooks a write fires: {self._lookups(lines)}"
        )

    def test_an_empty_session_resolves_without_a_lookup(self, tmp_path) -> None:
        """The other half of "an empty session yields an empty mode": it must cost
        nothing to learn that. An unguarded fallback would read writ-session-.json, a
        path that cannot exist, and pay a process per decision to be told so."""
        lines = self._trace(
            tmp_path,
            'SESSION_ID=""\nlog_gate_decision "probe-gate" "allow" "reason" "/tmp/t"',
        )
        assert self._lookups(lines) == [], (
            f"a session-less decision paid for a cache read: {self._lookups(lines)}"
        )

    def test_many_decisions_resolve_once(self, tmp_path) -> None:
        """writ-bash-write-gate.sh has eleven log_gate_decision call sites. The memo is
        what keeps that one file read rather than eleven."""
        _seed_mode(tmp_path, "s-memo", "work")
        body = 'SESSION_ID="s-memo"\n' + "\n".join(
            f'log_gate_decision "probe-gate" "allow" "reason {i}" "/tmp/t{i}"'
            for i in range(5)
        )
        lines = self._trace(tmp_path, body)
        assert len(self._lookups(lines)) <= 2, (
            f"five decisions triggered {len(self._lookups(lines))} reader processes; "
            f"the per-process memo is not holding: {self._lookups(lines)}"
        )

    def test_the_tracer_can_see_processes(self, tmp_path) -> None:
        """Anti-vacuity: a trace that captured nothing passes both assertions above."""
        lines = self._trace(tmp_path, 'SESSION_ID="s-see"\ntrue')
        assert any("execve(" in ln for ln in lines), "the tracer recorded no execve at all"
