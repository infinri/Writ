"""The writer half of the capture record schema (plan.md `dfacff61-23d5-474e-846c-2e2f0f0ea482`).

Covers `bin/lib/common.sh`'s side of the cycle: `blackbox_log` gains `event` and
`exit_code`, records the hook's OWN `$$` instead of the ephemeral encoder's pid, and
`_writ_hook_exit_trap` writes a third `direction: "exit"` row on a non-zero status.
The reader half (`writ/analysis/blackbox.py`) is covered in `test_blackbox_census.py`.

THE DEFECT THIS FILE EXISTS TO CATCH. `pid` is currently `os.getpid()` of the
ephemeral python encoder `blackbox_log` forks (common.sh:669), so an IN row and an
OUT row from ONE hook invocation carry DIFFERENT pids and the `(hook, pid, session)`
join in `build_census` can never match. The existing tests could not see this because
they hand-build IN and OUT rows sharing one literal pid. Every pid assertion below
therefore drives a REAL bash subprocess and anchors both rows to that process's OWN
`$$`, read back from the process's own stdout, never to each other alone: two
encoders could coincidentally agree with each other while both disagreeing with the
hook, so equality between the two rows is not the proof, equality with `$$` is.

SEAM CONTRACT for the implementer, none of which exists yet:

  bin/lib/common.sh::blackbox_log <direction> <hook> <session> [event] [exit_code]
      Gains two positional arguments. `event` is always written on a row this writer
      produces (a string when the caller passed one, JSON `null` when it did not),
      keeping "this row predates the schema" (key ABSENT) distinguishable from "this
      process observed no event" (key PRESENT, null). `exit_code` is written only for
      `direction: "exit"`, validated with the same `case "$x" in ''|*[!0-9]*)` idiom
      the size cap already uses, and the row is DROPPED when it is not numeric. `pid`
      is `$$` (the hook shell's own pid), encoded as a JSON int, with `os.getpid()`
      only as an encoder-internal fallback.

  bin/lib/common.sh::load_hook_env / emit_hook_reply
      Both now pass `${HOOK_EVENT:-}` through to `blackbox_log`, so IN and OUT rows
      carry the observed event at the record level too, not only inside their payload.

  bin/lib/common.sh::_writ_hook_exit_trap
      On a non-zero `$rc`, and only then, and only when `blackbox_enabled`, writes
      exactly one `direction: "exit"` row carrying that `$rc` as `exit_code` and
      `${HOOK_EVENT:-}` as `event`, reading stdin from `/dev/null` rather than a pipe.
      NOTHING happens on a zero exit: the row is additive, on top of the
      `hook_execution` telemetry row the same trap already appends.

  tests/_inventory.py::direct_blackbox_exit_calls() -> list[str]
      Mirrors `direct_blackbox_out_calls`: hook scripts under `hooks/scripts/` whose
      comment-stripped source still calls `blackbox_log exit` itself. Must be empty.

  tests/_inventory.py::nonzero_exit_scripts() -> list[str]
      `refusing_script_markers()` filtered on its own `nonzero_exit` marker. Must be
      non-empty, and every member must be auto-instrumented by `common.sh` (every
      script under `hooks/scripts/` except `writ-statusline.sh`).

Everything above is exercised exactly as named; a test that needs a symbol which does
not exist yet fails loudly (`pytest.fail` with "skeleton: ...") rather than skipping.

NON-NEGOTIABLES this file holds itself to:
  - every pid/exit-row claim is proved by a REAL bash subprocess, never a hand-built
    record (ENF-SYS-005: that is exactly how the pid defect survived);
  - `HOME`, `WRIT_CACHE_DIR`, `WRIT_LOG_ROOT` and `WRIT_PORT` are pinned into a tmp
    tree via `tests/firedrill/_harness.py`'s own isolation builder, never invented a
    second time, and the developer's real `~/.claude/writ-blackbox.jsonl` is asserted
    untouched;
  - any bash function this file calls directly is checked with `declare -F` first,
    separately from its exit status;
  - this file imports `tests.test_write_path_process_budget`'s existing budget
    constants rather than restating a number.
"""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from tests._strace import trace_execve_result
from tests.firedrill._harness import build_env, make_isolation, real_blackbox_snapshot

REPO = Path(__file__).resolve().parent.parent
COMMON_SH = REPO / "bin" / "lib" / "common.sh"
HOOKS_DIR = REPO / "hooks" / "scripts"


def _inventory():
    try:
        import tests._inventory as inventory
    except ImportError as exc:
        pytest.fail(f"skeleton: tests/_inventory.py failed to import: {exc}")
    return inventory


def _require(module, *names) -> None:
    missing = [n for n in names if not hasattr(module, n)]
    if missing:
        pytest.fail(f"skeleton: {module.__name__} has no {', '.join(missing)} yet")


def _run_probe(
    tmp_path: Path,
    body: str,
    *,
    capture: bool,
    stdin: str | None = None,
    extra_env: dict | None = None,
    session: str = "probe-sess",
    log_name: str = "capture.jsonl",
) -> tuple[subprocess.CompletedProcess, Path]:
    """Run one throwaway probe script that sources `common.sh`, fully isolated.

    Reuses `tests/firedrill/_harness.py`'s isolation builder for `HOME`,
    `WRIT_CACHE_DIR`, `WRIT_LOG_ROOT`, `WRIT_PORT` and `WRIT_NO_AUTOSTART`, rather
    than inventing a second isolation mechanism. Capture is layered on top through
    `WRIT_BLACKBOX` and `WRIT_BLACKBOX_LOG`, both pointed at this call's own
    `tmp_path`, never at a developer's real capture log.
    """
    iso = make_isolation(tmp_path, session_id=session)
    log_path = tmp_path / log_name
    env_extra: dict = dict(extra_env or {})
    env_extra["WRIT_BLACKBOX_LOG"] = str(log_path)
    if capture:
        env_extra["WRIT_BLACKBOX"] = "1"
    env = build_env(iso, extra=env_extra)
    if not capture:
        env.pop("WRIT_BLACKBOX", None)
    script = tmp_path / "probe.sh"
    script.write_text(
        "#!/usr/bin/env bash\n"
        "set -euo pipefail\n"
        f'source "{COMMON_SH}"\n'
        f"{body}\n"
    )
    proc = subprocess.run(
        ["bash", str(script)],
        input=stdin if stdin is not None else "",
        capture_output=True,
        text=True,
        env=env,
        timeout=60,
    )
    return proc, log_path


def _read_rows(log_path: Path) -> list[dict]:
    if not log_path.exists():
        return []
    return [json.loads(ln) for ln in log_path.read_text().splitlines() if ln.strip()]


def _auto_instrumented_scripts() -> set[str]:
    """Every hook script `common.sh` auto-instruments: all of `hooks/scripts/*.sh`
    except `writ-statusline.sh` (common.sh's own carve-out at the auto-instrument
    site). Names carry `.sh`, matching `refusing_script_markers()`'s own convention."""
    return {p.name for p in HOOKS_DIR.glob("*.sh") if p.name != "writ-statusline.sh"}


def _hook_scripts_calling_blackbox_log_exit_directly() -> list[str]:
    """Independent derivation (never imports the module under test): every hook
    script whose non-comment source contains the literal token `blackbox_log exit`."""
    found = []
    for path in sorted(HOOKS_DIR.glob("*.sh")):
        lines = [
            ln for ln in path.read_text(encoding="utf-8", errors="replace").splitlines()
            if not ln.lstrip().startswith("#")
        ]
        if any("blackbox_log exit" in ln for ln in lines):
            found.append(path.name)
    return found


# --------------------------------------------------------------------------------- #
# Capability: pid identity, proved against the shell's own $$
# --------------------------------------------------------------------------------- #


class TestPidIsTheHooksOwnDollarDollar:
    """An IN row and an OUT row from ONE real hook invocation must carry the SAME
    pid, and that pid must be the hook shell's own `$$`, never an encoder's.

    Anchoring both rows to a THIRD, independently-read value (`$$`, printed by the
    probe script itself) is load-bearing: two encoders could coincidentally agree
    with each other while both disagreeing with the hook, so row-to-row equality
    alone would not prove this.
    """

    def test_an_in_row_and_an_out_row_from_the_same_invocation_share_the_shells_pid(
        self, tmp_path
    ) -> None:
        body = (
            'hook_instrument "pid-probe"\n'
            "printf '%s' '{\"hook_event_name\":\"PreToolUse\"}' "
            '| blackbox_log in "pid-probe" "sess-1"\n'
            "printf '%s' '{\"hookSpecificOutput\":{\"hookEventName\":\"PreToolUse\"}}' "
            '| blackbox_log out "pid-probe" "sess-1"\n'
            'printf "SHELL_PID=%s\\n" "$$"\n'
        )
        proc, log_path = _run_probe(tmp_path, body, capture=True)
        assert proc.returncode == 0, proc.stderr

        marker_lines = [ln for ln in proc.stdout.splitlines() if ln.startswith("SHELL_PID=")]
        assert marker_lines, f"probe did not print its own $$: stdout={proc.stdout!r}"
        shell_pid = int(marker_lines[0].split("=", 1)[1])
        assert shell_pid > 0, "a real bash pid must be a positive integer"

        rows = _read_rows(log_path)
        assert len(rows) == 2, (
            f"expected exactly one IN row and one OUT row, got {len(rows)}: {rows!r}"
        )
        in_rows = [r for r in rows if r.get("direction") == "in"]
        out_rows = [r for r in rows if r.get("direction") == "out"]
        assert len(in_rows) == 1 and len(out_rows) == 1, rows

        in_pid = in_rows[0].get("pid")
        out_pid = out_rows[0].get("pid")
        assert in_pid is not None and out_pid is not None, rows

        assert in_pid == shell_pid, (
            f"the IN row's pid ({in_pid!r}) is not the hook shell's own $$ "
            f"({shell_pid}); it looks like the ephemeral encoder's pid instead"
        )
        assert out_pid == shell_pid, (
            f"the OUT row's pid ({out_pid!r}) is not the hook shell's own $$ "
            f"({shell_pid}); it looks like the ephemeral encoder's pid instead"
        )
        assert isinstance(in_pid, int) and isinstance(out_pid, int), (
            "pid must be encoded as a JSON int, not a str, or the (hook, pid, "
            f"session) join in build_census silently never matches: in_pid={in_pid!r} "
            f"({type(in_pid)}), out_pid={out_pid!r} ({type(out_pid)})"
        )


class TestRealCaptureLogNeverTouchedByTheseProbes:
    """Standing check (firedrill harness trap 3), repeated here because this file
    turns capture ON deliberately: even so, every row lands in this test's own
    tmp_path, never the developer's real ~/.claude/writ-blackbox.jsonl."""

    def test_a_capture_on_probe_never_appends_to_the_developers_real_log(
        self, tmp_path
    ) -> None:
        before = real_blackbox_snapshot()
        body = 'hook_instrument "real-log-guard"\nexit 0\n'
        _run_probe(tmp_path, body, capture=True)
        after = real_blackbox_snapshot()
        assert after == before, (
            f"a probe with capture on touched the developer's real capture log: "
            f"before={before}, after={after}"
        )


# --------------------------------------------------------------------------------- #
# Capability: the exit row exists only on a non-zero status, and carries an int code
# --------------------------------------------------------------------------------- #


class TestExitRowWrittenOnlyOnNonZeroStatus:
    def _exit_rows(self, tmp_path: Path, code: int) -> tuple[subprocess.CompletedProcess, list[dict]]:
        body = f'hook_instrument "exit-row-probe"\nexit {code}\n'
        proc, log_path = _run_probe(tmp_path, body, capture=True)
        rows = [r for r in _read_rows(log_path) if r.get("direction") == "exit"]
        return proc, rows

    def test_a_nonzero_exit_writes_exactly_one_exit_row_with_the_code_as_an_integer(
        self, tmp_path
    ) -> None:
        proc, exit_rows = self._exit_rows(tmp_path, 2)
        assert proc.returncode == 2, (
            "the trap must not alter the hook's own exit status while recording it"
        )
        assert len(exit_rows) == 1, (
            f"expected exactly one direction=exit row for a hook that exited 2, got "
            f"{len(exit_rows)}: {exit_rows!r}"
        )
        row = exit_rows[0]
        assert row.get("exit_code") == 2, row
        assert isinstance(row["exit_code"], int), (
            f"exit_code must be a JSON int, not {type(row['exit_code'])}: {row!r}"
        )

    def test_a_zero_exit_writes_no_exit_row_given_the_mechanism_is_proven_live(
        self, tmp_path
    ) -> None:
        """Precondition run FIRST, in this same test: without proving the mechanism
        can produce a row at all, "zero rows for exit 0" would be indistinguishable
        from a feature that does not exist (the exact anti-vacuity trap NON-NEGOTIABLE
        1 names). Two separate subdirectories so the two runs never share one log."""
        nonzero_dir = tmp_path / "nonzero"
        nonzero_dir.mkdir()
        _, nonzero_exit_rows = self._exit_rows(nonzero_dir, 2)
        assert len(nonzero_exit_rows) == 1, (
            "precondition failed: a non-zero exit produced no exit row on this tree, "
            "so the absence checked below would prove nothing about the guard"
        )

        zero_dir = tmp_path / "zero"
        zero_dir.mkdir()
        proc, zero_exit_rows = self._exit_rows(zero_dir, 0)
        assert proc.returncode == 0, proc.stderr
        assert zero_exit_rows == [], (
            f"a hook that exited 0 must write no exit row: {zero_exit_rows!r}"
        )


# --------------------------------------------------------------------------------- #
# Capability: capture off adds neither a row nor a process for a non-zero exit
# --------------------------------------------------------------------------------- #


class TestCaptureOffAddsNoRowAndNoProcessForANonzeroExit:
    def test_capture_off_writes_no_row_and_leaves_the_hooks_status_alone(
        self, tmp_path
    ) -> None:
        body = 'hook_instrument "capture-off-exit-probe"\nexit 2\n'
        proc, log_path = _run_probe(tmp_path, body, capture=False)
        assert proc.returncode == 2, (
            "capture being off must not change the hook's own exit status"
        )
        assert _read_rows(log_path) == [], (
            f"capture is OFF but a row was written anyway: {_read_rows(log_path)!r}"
        )

    def test_the_exit_trap_forks_nothing_extra_when_capture_is_off(self, tmp_path) -> None:
        """Differential strace measurement (style of
        test_hook_out_capture.py::TestEmitHookReplyCaptureOff): the trap's added cost
        with capture off is `[ rc -ne 0 ]` plus `blackbox_enabled`, both builtins, so
        a hook that exits non-zero must fork exactly as many processes as one that
        exits zero, under the identical harness and env. Differential, not an
        absolute floor of zero: `strace` and common.sh's own source-time fork never
        trace to 0 on this machine.
        """
        if shutil.which("strace") is None:
            pytest.skip("strace unavailable; cannot count execve")

        def _traced(code: int) -> tuple[subprocess.CompletedProcess, int]:
            iso_dir = tmp_path / f"code-{code}"
            iso_dir.mkdir()
            iso = make_isolation(iso_dir, session_id=f"trace-{code}")
            env = build_env(iso)
            env.pop("WRIT_BLACKBOX", None)
            script = iso_dir / "probe.sh"
            script.write_text(
                "#!/usr/bin/env bash\n"
                "set -euo pipefail\n"
                f'source "{COMMON_SH}"\n'
                'hook_instrument "trace-exit-probe"\n'
                f"exit {code}\n"
            )
            result, text = trace_execve_result(
                ["bash", str(script)], env=env, timeout=60,
            )
            return result, text.count("execve(")

        baseline_result, baseline_count = _traced(0)
        measured_result, measured_count = _traced(2)

        assert baseline_result.returncode == 0, baseline_result.stderr
        assert measured_result.returncode == 2, (
            "the probe must actually exit 2, or this comparison proves nothing "
            f"about the non-zero path: {measured_result.stderr!r}"
        )
        assert baseline_count > 0, (
            "baseline execve count is zero; strace measured nothing"
        )
        assert measured_count == baseline_count, (
            f"the exit trap forked something extra on a non-zero exit even with "
            f"capture off: baseline={baseline_count}, exit-2={measured_count}"
        )

    def test_the_standing_write_path_process_budgets_still_hold(self) -> None:
        """The second half of the capability, satisfied by importing the existing
        ratchet's own constants and measurement functions, never restating a number
        here (PERF-QBUDGET-001, and the plan's explicit non-negotiable on it)."""
        if shutil.which("strace") is None:
            pytest.skip("strace unavailable; cannot count execve")
        from tests.test_write_path_process_budget import (
            HOOKS as WRITE_PATH_HOOKS_DIR,
            PYTHON_BUDGET,
            REAL_PROCESS_BUDGET,
            TOTAL_BUDGET,
            WRITE_PATH_HOOKS,
            _counts,
            _real_processes,
        )

        python_total = overall_total = real_total = 0
        for hook in WRITE_PATH_HOOKS:
            if not (WRITE_PATH_HOOKS_DIR / hook).exists():
                continue
            python, total = _counts(hook)
            python_total += python
            overall_total += total
            real_total += _real_processes(hook)

        assert python_total <= PYTHON_BUDGET, (
            f"{python_total} python interpreters exceeds the existing ratchet of "
            f"{PYTHON_BUDGET} after adding the exit-row trap"
        )
        assert overall_total <= TOTAL_BUDGET, (
            f"{overall_total} execve attempts exceeds the existing ratchet of "
            f"{TOTAL_BUDGET} after adding the exit-row trap"
        )
        assert real_total <= REAL_PROCESS_BUDGET, (
            f"{real_total} real processes exceeds the existing ratchet of "
            f"{REAL_PROCESS_BUDGET} after adding the exit-row trap"
        )


# --------------------------------------------------------------------------------- #
# Capability: the exit row's event, by key membership, from load_hook_env or absent
# --------------------------------------------------------------------------------- #


class TestExitRowEventComesFromLoadHookEnvOrIsNull:
    def test_an_exit_row_from_a_hook_that_ran_load_hook_env_carries_the_envelope_event(
        self, tmp_path
    ) -> None:
        body = (
            'hook_instrument "event-probe-with-envelope"\n'
            "load_hook_env\n"
            "exit 3\n"
        )
        envelope = json.dumps({"session_id": "sess-ev", "hook_event_name": "Stop"})
        proc, log_path = _run_probe(tmp_path, body, capture=True, stdin=envelope)
        assert proc.returncode == 3, proc.stderr
        exit_rows = [r for r in _read_rows(log_path) if r.get("direction") == "exit"]
        assert len(exit_rows) == 1, exit_rows
        row = exit_rows[0]
        assert "event" in row, f"the event key must be PRESENT, not just correct: {row!r}"
        assert row["event"] == "Stop", row

    def test_an_exit_row_from_a_hook_that_never_ran_load_hook_env_carries_a_present_but_null_event(
        self, tmp_path
    ) -> None:
        body = 'hook_instrument "event-probe-without-envelope"\nexit 4\n'
        proc, log_path = _run_probe(tmp_path, body, capture=True)
        assert proc.returncode == 4, proc.stderr
        exit_rows = [r for r in _read_rows(log_path) if r.get("direction") == "exit"]
        assert len(exit_rows) == 1, exit_rows
        row = exit_rows[0]
        assert "event" in row, (
            f"the event key must be PRESENT even when nothing was observed, so a "
            f"pre-schema row (key ABSENT) stays distinguishable from this one: {row!r}"
        )
        assert row["event"] is None, (
            f"a hook that never ran load_hook_env has no event to report; the key "
            f"must be null, not a placeholder string: {row!r}"
        )


# --------------------------------------------------------------------------------- #
# Capability: blackbox_log drops an exit row whose code is not numeric
# --------------------------------------------------------------------------------- #


class TestBlackboxLogDropsANonNumericExitCode:
    def test_blackbox_log_is_defined_before_relying_on_its_exit_status(
        self, tmp_path
    ) -> None:
        proc, _ = _run_probe(tmp_path, "declare -F blackbox_log", capture=True)
        assert proc.returncode == 0, (
            f"blackbox_log is not defined (declare -F failed): {proc.stderr!r}"
        )

    def test_a_non_numeric_exit_code_produces_no_row_at_all(self, tmp_path) -> None:
        body = "printf '' | blackbox_log exit somehook sess-1 Stop abc\n"
        proc, log_path = _run_probe(tmp_path, body, capture=True)
        assert proc.returncode == 0, proc.stderr
        assert _read_rows(log_path) == [], (
            f"blackbox_log wrote a row for a non-numeric exit code, so an exit row "
            f"without a valid code can exist: {_read_rows(log_path)!r}"
        )

    def test_a_numeric_exit_code_still_produces_a_row_so_the_drop_is_specific(
        self, tmp_path
    ) -> None:
        """Companion/precondition: without this, a blackbox_log that writes nothing
        for ANY exit call at all would pass the negative test above for the wrong
        reason."""
        body = "printf '' | blackbox_log exit somehook sess-1 Stop 7\n"
        proc, log_path = _run_probe(tmp_path, body, capture=True)
        assert proc.returncode == 0, proc.stderr
        rows = _read_rows(log_path)
        assert len(rows) == 1, (
            f"a valid numeric exit code must still produce exactly one row: {rows!r}"
        )
        assert rows[0].get("exit_code") == 7, rows[0]


# --------------------------------------------------------------------------------- #
# Capability: coverage held by derivation (tests/_inventory.py), not by vigilance
# --------------------------------------------------------------------------------- #


class TestNoHookCallsBlackboxLogExitDirectly:
    def test_direct_blackbox_exit_calls_agrees_with_an_independent_scan(self) -> None:
        inventory = _inventory()
        _require(inventory, "direct_blackbox_exit_calls")
        expected = _hook_scripts_calling_blackbox_log_exit_directly()
        offenders = inventory.direct_blackbox_exit_calls()
        assert offenders == expected == [], (
            f"tests/_inventory.py::direct_blackbox_exit_calls() disagrees with an "
            f"independent scan of hooks/scripts/, or found a real offender: "
            f"function={offenders!r} independent={expected!r}"
        )


class TestNonzeroExitScriptsPopulationIsDerivedAndBounded:
    def test_the_population_is_non_empty(self) -> None:
        inventory = _inventory()
        _require(inventory, "nonzero_exit_scripts")
        scripts = inventory.nonzero_exit_scripts()
        assert scripts, (
            "nonzero_exit_scripts() derived nothing; a silently-empty derivation "
            "would make the subset check below pass on any tree"
        )

    def test_every_member_is_auto_instrumented_by_common_sh(self) -> None:
        inventory = _inventory()
        _require(inventory, "nonzero_exit_scripts")
        scripts = set(inventory.nonzero_exit_scripts())
        assert scripts, "precondition: see the non-emptiness test above"
        auto_instrumented = _auto_instrumented_scripts()
        assert auto_instrumented, "no auto-instrumented scripts derived; vacuous precondition"
        stray = scripts - auto_instrumented
        assert stray == set(), (
            f"these scripts have a non-zero exit site but are NOT auto-instrumented "
            f"by common.sh, so their exit could never reach the shared trap: {stray!r}"
        )

    def test_the_population_agrees_with_refusing_script_markers_nonzero_exit_filter(
        self,
    ) -> None:
        """Independent derivation: filters the EXISTING refusing_script_markers()
        population on its own nonzero_exit marker, exactly as the plan specifies,
        rather than trusting nonzero_exit_scripts() to have implemented that filter
        correctly on its own."""
        inventory = _inventory()
        _require(inventory, "nonzero_exit_scripts", "refusing_script_markers")
        expected = sorted(
            name for name, markers in inventory.refusing_script_markers().items()
            if "nonzero_exit" in markers
        )
        assert expected, "precondition: refusing_script_markers() found no nonzero_exit script"
        got = sorted(inventory.nonzero_exit_scripts())
        assert got == expected, (
            f"nonzero_exit_scripts() disagrees with filtering refusing_script_markers() "
            f"on its own nonzero_exit marker: got={got!r} expected={expected!r}"
        )
