"""One owner of the EXIT trap, enforced.

bash allows exactly ONE EXIT trap. `hook_instrument` installs it to record each hook's
telemetry row, so a hook that later runs `trap cleanup EXIT` REPLACES it and silently
stops recording. Two hooks did exactly that (pre-validate-file.sh, inject-tier-workflow.sh)
and nobody noticed, because nothing fails when a metrics row is never written.

That is the bug class this file exists for. Converting the two known hooks fixes today;
the scan below fixes tomorrow, which is the part that matters.

WHY THE OBVIOUS FIX WAS FIRST REJECTED, AND WHY THAT WAS WRONG. I argued that running
the telemetry after a hook's own handler would report the HANDLER's `$?` rather than the
hook's, and that in a PreToolUse gate `$?` is the allow/deny signal. Review checked and
refuted it: both hooks exit 0 on every path and carry their decision in stdout JSON.
Separately, the rc-preserving form is exact for every exit shape anyway, which the
behavioural tests here pin. A tradeoff argued from an unverified constraint is not a
tradeoff, it is a guess.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
HOOKS = REPO / "hooks" / "scripts"
COMMON_SH = REPO / "bin" / "lib" / "common.sh"

# `trap <something> <SIG>` for every signal hook_instrument owns, ignoring a trap that
# CLEARS (`trap - EXIT`) since that installs no competing handler.
#
# TERM/INT/HUP are covered as well as EXIT, and the signal case is the more dangerous
# one. hook_instrument converts each signal to an explicit `exit` so the trap records the
# real status. A hook that installs its own `trap my_cleanup TERM` (a natural thing to
# write when you want cleanup-on-kill) replaces that conversion, and bash RESUMES normal
# execution after a trap handler that does not itself exit: the hook absorbs SIGTERM and
# keeps running. Reproduced by review, which needed SIGKILL to stop the probe. That is
# not lost telemetry, it is a hook that cannot be terminated gracefully.
_OWNED_SIGNALS = ("EXIT", "TERM", "INT", "HUP")
TRAP_EXIT = re.compile(
    r"^\s*trap\s+(?!-\s+(?:" + "|".join(_OWNED_SIGNALS) + r")\b)\S.*\b(?:"
    + "|".join(_OWNED_SIGNALS) + r")\b",
    re.MULTILINE,
)


def _instrumented_hooks() -> list[Path]:
    return sorted(p for p in HOOKS.glob("*.sh") if "hook_instrument" in p.read_text())


class TestNoHookStealsTheTrap:
    def test_no_instrumented_hook_installs_its_own_exit_trap(self) -> None:
        offenders = {}
        for path in _instrumented_hooks():
            hits = TRAP_EXIT.findall(path.read_text())
            if hits:
                offenders[path.name] = hits
        assert offenders == {}, (
            "these hooks call hook_instrument and then take the EXIT trap, which "
            "replaces the telemetry trap and silently stops their instrumentation. "
            f"Use `writ_on_exit <fn>` instead: {offenders}"
        )

    def test_the_scan_sees_a_planted_trap(self) -> None:
        """Anti-vacuity: a regex that matches nothing would pass on any tree."""
        assert TRAP_EXIT.findall("hook_instrument 'x'\ntrap cleanup EXIT\n")

    @pytest.mark.parametrize("sig", ["TERM", "INT", "HUP"])
    def test_the_scan_sees_a_planted_signal_trap(self, sig: str) -> None:
        """The signal case is why this scan covers more than EXIT: a hook that takes
        TERM does not merely lose its exit code, it stops dying on SIGTERM, because bash
        resumes execution after a handler that does not exit."""
        assert TRAP_EXIT.findall(f"hook_instrument 'x'\ntrap my_cleanup {sig}\n")

    @pytest.mark.parametrize("sig", ["EXIT", "TERM", "INT", "HUP"])
    def test_the_scan_ignores_clearing_any_owned_signal(self, sig: str) -> None:
        assert TRAP_EXIT.findall(f"trap - {sig}\n") == []

    def test_the_scan_ignores_a_trap_that_clears(self) -> None:
        """`trap - EXIT` removes a handler rather than installing a rival, and flagging
        it would push authors toward worse workarounds."""
        assert TRAP_EXIT.findall("trap - EXIT\n") == []

    def test_there_are_instrumented_hooks_to_scan(self) -> None:
        """Anti-vacuity for the whole class: an empty file list passes everything."""
        assert len(_instrumented_hooks()) > 10


class TestTheRegistryPreservesExitStatus:
    """The behavioural half. The scan proves nobody steals the trap; these prove the
    replacement is worth having."""

    def _run(self, body: str, tmp_path: Path):
        script = tmp_path / "probe.sh"
        script.write_text(
            "#!/usr/bin/env bash\n"
            "set -euo pipefail\n"
            f'source "{COMMON_SH}"\n'
            'hook_instrument "trap-probe"\n'
            'marker() { echo "HANDLER saw $?" >&2; }\n'
            "writ_on_exit marker\n"
            f"{body}\n"
        )
        env = os.environ.copy()
        env["WRIT_CACHE_DIR"] = str(tmp_path / "cache")
        env["WRIT_LOG_ROOT"] = str(tmp_path / "logs")
        env["WRIT_PORT"] = "19999"
        env.pop("WRIT_FRICTION_LOG", None)
        (tmp_path / "cache").mkdir(parents=True, exist_ok=True)
        return subprocess.run(["bash", str(script)], capture_output=True, text=True,
                              env=env, timeout=60)

    @pytest.mark.parametrize("body,expected", [
        ("exit 0", 0), ("exit 2", 2), ("exit 7", 7), ("true", 0), ("false", 1),
    ])
    def test_the_hooks_own_exit_status_is_unchanged(self, body, expected, tmp_path) -> None:
        """`exit 2` is a PreToolUse deny. If the trap altered it, a gate would flip."""
        assert self._run(body, tmp_path).returncode == expected

    @pytest.mark.parametrize("body,expected", [
        ("exit 0", 0), ("exit 2", 2), ("exit 7", 7), ("false", 1),
    ])
    def test_the_handler_runs_and_sees_the_real_status(self, body, expected, tmp_path) -> None:
        """The registered handler must run on EVERY exit shape, and observe the hook's
        status rather than the trap's bookkeeping.

        The non-zero cases are the ones that caught a real defect: with errexit left on
        inside the trap, `( exit "$rc" )` aborted it, so neither the handler nor the
        telemetry ran for `exit 2` or a failed command, while the exit status still
        looked correct.
        """
        proc = self._run(body, tmp_path)
        assert f"HANDLER saw {expected}" in proc.stderr, (
            f"handler did not run or saw the wrong status for {body!r}: "
            f"{proc.stderr[-300:]!r}"
        )

    def test_a_signal_kill_records_its_real_status(self) -> None:
        """A killed hook used to record exit_code 0.

        The shell runs the EXIT trap with `$?` still 0 when a signal ends the script,
        because no command set it, so the RECORD said success for a hook that was
        killed. (The process's reported status was always correct, so enforcement never
        depended on this.) Converting each signal into an explicit exit gives the trap
        the true status by the shell's 128+signal convention. Verified live for TERM
        (143) and HUP (129); asserted structurally here because bash defers a trap until
        the running command returns, so a live probe costs 30 seconds per signal.
        """
        src = COMMON_SH.read_text()
        start = src.index("hook_instrument() {")
        body = src[start:src.index("\n}\n", start)]
        for sig, code in (("HUP", 129), ("INT", 130), ("TERM", 143)):
            assert f"trap 'exit {code}' {sig}" in body, (
                f"{sig} is not converted to exit {code}, so a hook killed by it records "
                f"exit_code 0"
            )

    def test_a_telemetry_row_is_buffered_on_a_nonzero_exit(self, tmp_path) -> None:
        """The whole point: instrumentation survives the shapes it used to miss."""
        self._run("exit 2", tmp_path)
        bufs = list((tmp_path / "cache").glob("writ-events-*.buf"))
        assert bufs, "no telemetry row was buffered for a hook that exited 2"
        assert "trap-probe" in bufs[0].read_text()


class TestTheConvertedHooksRecordAgain:
    @pytest.mark.parametrize("hook,envelope", [
        ("pre-validate-file.sh", {"tool_name": "Write",
                                  "tool_input": {"file_path": "/tmp/x.md", "content": "hi\n"}}),
        ("inject-tier-workflow.sh", {"tool_name": "Write",
                                     "tool_input": {"file_path": "/tmp/x.md", "content": "hi\n"}}),
    ])
    def test_the_hook_buffers_a_row(self, hook: str, envelope: dict, tmp_path) -> None:
        env = os.environ.copy()
        env["WRIT_CACHE_DIR"] = str(tmp_path / "cache")
        env["WRIT_LOG_ROOT"] = str(tmp_path / "logs")
        env["WRIT_PORT"] = "19999"
        env.pop("WRIT_FRICTION_LOG", None)
        (tmp_path / "cache").mkdir(parents=True, exist_ok=True)
        payload = {"session_id": "trap-conv", "hook_event_name": "PreToolUse", **envelope}
        subprocess.run(["bash", str(HOOKS / hook)], input=json.dumps(payload),
                       capture_output=True, text=True, env=env, timeout=120)
        bufs = list((tmp_path / "cache").glob("writ-events-*.buf"))
        assert bufs, f"{hook} recorded no telemetry row; it used to steal the EXIT trap"
