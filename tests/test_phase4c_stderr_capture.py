"""Phase 4c D1: stderr capture extension to Task / Subagent hooks.

PSR-004 surfaced PreToolUse:Agent hook tracebacks during sub-agent
dispatches. The Task-matcher hook (writ-dispatch-discipline.sh) and
SubagentStart/Stop hooks got the same diagnostic stderr-tee that
writ-pre-write-dispatch.sh got in Phase 4b.

The logging overhaul then GATED that tee behind WRIT_DEBUG. All four
hooks now use the shared idiom
    exec 2> >(tee -a "$(_writ_debug_enabled \\
        && echo "${WRIT_HOOK_LOG:-/tmp/writ-hook-debug.log}" \\
        || echo /dev/null)" >&2)
so the debug file is opened ONLY when WRIT_DEBUG=1; otherwise the tee
sink is /dev/null (quiet-by-default). These tests verify each hook
carries the gated idiom and that the idiom captures stderr to the debug
log when WRIT_DEBUG=1 and stays silent when it is unset.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
from pathlib import Path

import pytest

WRIT_ROOT = Path(__file__).resolve().parent.parent
HOOKS = WRIT_ROOT / "hooks" / "scripts"
DEBUG_LOG = Path("/tmp/writ-hook-debug.log")


HOOKS_TO_CHECK = [
    "writ-dispatch-discipline.sh",
    "writ-subagent-start.sh",
    "writ-subagent-stop.sh",
]


@pytest.fixture(autouse=True)
def truncate_debug_log():
    """Start each test with a clean debug log so writes are isolated."""
    if DEBUG_LOG.exists():
        DEBUG_LOG.unlink()
    yield


class TestStderrTeePresent:
    """Each target hook must contain the tee directive at the top."""

    @pytest.mark.parametrize("hook_name", HOOKS_TO_CHECK)
    def test_hook_redirects_stderr_to_debug_log(self, hook_name: str) -> None:
        """The hook source must carry the WRIT_DEBUG-gated exec 2> >(tee ...) line."""
        path = HOOKS / hook_name
        assert path.exists(), f"{hook_name} does not exist"
        content = path.read_text()
        # The gated idiom has three markers: the WRIT_DEBUG gate helper, the
        # tee append, and the /tmp/writ-hook-debug.log fallback path inside the
        # gated $(...). All three must be present (the bare literal
        # `tee -a /tmp/writ-hook-debug.log` no longer appears verbatim).
        has_gated_tee = (
            "_writ_debug_enabled" in content
            and "tee -a" in content
            and "/tmp/writ-hook-debug.log" in content
        )
        assert has_gated_tee, (
            f"{hook_name} must tee stderr to /tmp/writ-hook-debug.log only when "
            "WRIT_DEBUG=1 (gated idiom: _writ_debug_enabled + tee -a + the "
            "/tmp/writ-hook-debug.log fallback path) so tracebacks are diagnosable"
        )

    @pytest.mark.parametrize("hook_name", HOOKS_TO_CHECK)
    def test_hook_syntax_valid(self, hook_name: str) -> None:
        proc = subprocess.run(
            ["bash", "-n", str(HOOKS / hook_name)],
            capture_output=True, text=True,
        )
        assert proc.returncode == 0, f"{hook_name} syntax error: {proc.stderr}"


class TestStderrTeeIdiomWorks:
    """Verify the WRIT_DEBUG-gated bash idiom itself, not the production hook
    (which has relative-path dependencies that break under copy-and-modify).

    The hooks source bin/lib/common.sh (the single source of _writ_debug_enabled)
    and run
        exec 2> >(tee -a "$(_writ_debug_enabled \\
            && echo "${WRIT_HOOK_LOG:-/tmp/writ-hook-debug.log}" \\
            || echo /dev/null)" >&2)
    This test builds a minimal script that sources the REAL common.sh and runs
    the same line, then asserts BOTH gate states (TEST-EDGE-001):
      - WRIT_DEBUG=1: the marker is written to the debug log (and preserved on
        stderr).
      - WRIT_DEBUG unset: the sink resolves to /dev/null, so the marker reaches
        stderr but the debug log is NOT written (quiet-by-default -- the whole
        point of the gate).
    """

    def test_tee_idiom_captures_stderr(self, tmp_path: Path) -> None:
        import os

        marker = "PHASE4C_TEE_IDIOM_TEST_MARKER"
        common_sh = WRIT_ROOT / "bin" / "lib" / "common.sh"
        assert common_sh.exists(), f"common.sh not found at {common_sh}"

        script = tmp_path / "fake-hook.sh"
        # Sources the real common.sh so it binds the actual _writ_debug_enabled
        # helper (single source of truth), then runs the gated tee idiom and
        # writes the marker to stderr. WRIT_HOOK_LOG is popped from the child
        # env below so the fallback path resolves to /tmp/writ-hook-debug.log.
        script.write_text(f"""#!/usr/bin/env bash
set -euo pipefail
source "{common_sh}"
exec 2> >(tee -a "$(_writ_debug_enabled && echo "${{WRIT_HOOK_LOG:-/tmp/writ-hook-debug.log}}" || echo /dev/null)" >&2)
printf '%s\\n' "{marker}" >&2
exit 0
""")
        script.chmod(0o755)

        # Child env with WRIT_HOOK_LOG removed so the fallback path is DEBUG_LOG.
        base_env = {k: v for k, v in os.environ.items() if k != "WRIT_HOOK_LOG"}

        # --- State 1: WRIT_DEBUG=1 -> marker is teed to the debug log. ---
        if DEBUG_LOG.exists():
            DEBUG_LOG.unlink()
        on_env = dict(base_env, WRIT_DEBUG="1")
        proc = subprocess.run(
            [str(script)], capture_output=True, text=True, timeout=5, env=on_env,
        )
        assert proc.returncode == 0, f"idiom script failed (WRIT_DEBUG=1): {proc.stderr}"
        assert marker in proc.stderr, "tee must preserve the marker on stderr"
        assert DEBUG_LOG.exists(), "debug log was not created with WRIT_DEBUG=1"
        assert marker in DEBUG_LOG.read_text(), (
            f"marker {marker!r} not found in {DEBUG_LOG} with WRIT_DEBUG=1"
        )

        # --- State 2: WRIT_DEBUG unset -> sink is /dev/null, log NOT written. ---
        if DEBUG_LOG.exists():
            DEBUG_LOG.unlink()
        off_env = dict(base_env)
        off_env.pop("WRIT_DEBUG", None)
        proc = subprocess.run(
            [str(script)], capture_output=True, text=True, timeout=5, env=off_env,
        )
        assert proc.returncode == 0, f"idiom script failed (WRIT_DEBUG unset): {proc.stderr}"
        assert marker in proc.stderr, "marker must still reach stderr when quiet"
        debug_has_marker = DEBUG_LOG.exists() and marker in DEBUG_LOG.read_text()
        assert not debug_has_marker, (
            "quiet-by-default: with WRIT_DEBUG unset the sink is /dev/null, so the "
            f"marker must NOT land in {DEBUG_LOG}"
        )


class TestPreWriteDispatchStillCovered:
    """Phase 4b tee on writ-pre-write-dispatch.sh is preserved."""

    def test_pre_write_dispatch_still_has_tee(self) -> None:
        content = (HOOKS / "writ-pre-write-dispatch.sh").read_text()
        # Same WRIT_DEBUG-gated idiom markers as the other hooks: the tee is now
        # opened only when WRIT_DEBUG=1 (the bare literal is gone).
        has_gated_tee = (
            "_writ_debug_enabled" in content
            and "tee -a" in content
            and "/tmp/writ-hook-debug.log" in content
        )
        assert has_gated_tee, (
            "Phase 4b tee on writ-pre-write-dispatch.sh must remain, now gated: "
            "it must tee stderr to /tmp/writ-hook-debug.log only when WRIT_DEBUG=1 "
            "(_writ_debug_enabled + tee -a + the /tmp/writ-hook-debug.log fallback path)"
        )


# The fork itself, not the source grep. This cycle's plan (write-path spawn
# reduction) changes line 27 from an UNCONDITIONAL `exec 2> >(tee -a "$(...)")`
# -- which forks tee on every run and only gates its DESTINATION file between
# the real log and /dev/null -- to one gated so the fork itself is skipped
# when WRIT_DEBUG is unset. TestPreWriteDispatchStillCovered above greps for
# three substrings that are identical in both spellings, so it passes either
# way and is not a test of this change. This class runs the REAL hook under
# strace and counts.
_TEE_EXECVE = re.compile(r'execve\("[^"]*/tee"')


class TestPreWriteDispatchForkBothStates:
    """Both states of the fork, proven by running the real hook, not a
    synthetic minimal script. `writ_critical` (bin/lib/common.sh:1233) prints
    a deterministic `[WRIT CRITICAL] writ-pre-write-dispatch: ...` line
    straight to stderr whenever the hook's own no-session-id branch fires
    (hooks/scripts/writ-pre-write-dispatch.sh:119-122), so an envelope with no
    session_id and no agent_id gives this test a REAL, controllable breadcrumb
    to look for in the debug log, instead of inventing one.
    """

    ENVELOPE_NO_SESSION = json.dumps({
        "tool_name": "Write",
        "tool_input": {"file_path": "/tmp/writ-tee-fork-probe.py", "content": "x = 1\n"},
    })
    BREADCRUMB = "[WRIT CRITICAL] writ-pre-write-dispatch:"

    def _run_traced(self, *, writ_debug: bool):
        from tests._strace import trace_execve_result

        # Same base-env shape as TestStderrTeeIdiomWorks above: WRIT_HOOK_LOG
        # popped so the fallback path resolves to DEBUG_LOG.
        env = {k: v for k, v in os.environ.items() if k != "WRIT_HOOK_LOG"}
        if writ_debug:
            env["WRIT_DEBUG"] = "1"
        else:
            env.pop("WRIT_DEBUG", None)
        return trace_execve_result(
            ["bash", str(HOOKS / "writ-pre-write-dispatch.sh")],
            input=self.ENVELOPE_NO_SESSION, timeout=180, env=env,
        )

    def test_writ_debug_unset_spawns_no_tee_process(self) -> None:
        if DEBUG_LOG.exists():
            DEBUG_LOG.unlink()
        proc, text = self._run_traced(writ_debug=False)
        assert "execve(" in text, "the tracer recorded no execve at all; the counter is broken"
        tee_lines = [ln for ln in text.splitlines() if _TEE_EXECVE.search(ln)]
        assert tee_lines == [], (
            "writ-pre-write-dispatch.sh spawned a tee process with WRIT_DEBUG "
            f"unset: {tee_lines!r}. The stderr tee at line 27 must be gated so "
            "the FORK itself is skipped when debug is off, not just its "
            "destination file (today's unconditional `exec 2> >(tee -a "
            "\"$(... || echo /dev/null)\")` forks tee either way)."
        )
        assert proc.returncode == 0, f"hook exited {proc.returncode}: {proc.stderr[:300]}"
        debug_has_breadcrumb = DEBUG_LOG.exists() and self.BREADCRUMB in DEBUG_LOG.read_text()
        assert not debug_has_breadcrumb, (
            f"quiet-by-default: with WRIT_DEBUG unset the breadcrumb must not "
            f"reach {DEBUG_LOG}"
        )

    def test_writ_debug_on_spawns_tee_and_writes_the_breadcrumb(self) -> None:
        if DEBUG_LOG.exists():
            DEBUG_LOG.unlink()
        proc, text = self._run_traced(writ_debug=True)
        assert "execve(" in text, "the tracer recorded no execve at all; the counter is broken"
        tee_lines = [ln for ln in text.splitlines() if _TEE_EXECVE.search(ln)]
        assert tee_lines != [], (
            "writ-pre-write-dispatch.sh did not spawn tee with WRIT_DEBUG=1; "
            "the debug capture is broken, not just quiet-by-default"
        )
        assert proc.returncode == 0, f"hook exited {proc.returncode}: {proc.stderr[:300]}"
        assert DEBUG_LOG.exists(), f"{DEBUG_LOG} was not created with WRIT_DEBUG=1"
        assert self.BREADCRUMB in DEBUG_LOG.read_text(), (
            f"the writ_critical breadcrumb did not reach {DEBUG_LOG} with WRIT_DEBUG=1"
        )

    def test_the_fork_count_actually_differs_between_the_two_states(self) -> None:
        """Anti-vacuity: proves the two tests above are not both passing by
        accident (e.g. a strace invocation that never sees any tee, in which
        case 'no tee lines found' would be true in both states for the wrong
        reason).
        """
        if DEBUG_LOG.exists():
            DEBUG_LOG.unlink()
        _, off_text = self._run_traced(writ_debug=False)
        if DEBUG_LOG.exists():
            DEBUG_LOG.unlink()
        _, on_text = self._run_traced(writ_debug=True)
        off_execs = off_text.count("execve(")
        on_execs = on_text.count("execve(")
        assert on_execs > off_execs, (
            f"expected WRIT_DEBUG=1 to spawn strictly more processes than "
            f"WRIT_DEBUG unset (the tee fork); off={off_execs} on={on_execs}"
        )
