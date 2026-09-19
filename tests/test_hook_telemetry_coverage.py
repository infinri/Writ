"""Cycle I skeletons: telemetry coverage from LOCATION, not from remembering a call.

Maat's line is "a gate that has never refused anything has not been shown to be a gate".
Answering that needs the prior question, which hooks run at all, and Writ could not: 8 of
the 40 hooks registered in `hooks/hooks.json` emit no `hook_execution` row by any means.

WHY THIS FILE DOES NOT SCAN SOURCE TEXT. The first version of this cycle detected
instrumentation lexically, and the spelling list was wrong three times in one hour:
`^hook_instrument` reported 21 uninstrumented hooks; adding the guarded
`type hook_instrument && ...` form gave 12; adding `hook_timer_start`/`hook_timer_end`
(`writ-pre-write-dispatch.sh:29,324`) gave 10; adding
`log_friction_event ... "hook_execution"` (`writ-mark-pending-test.sh:39`) gave the true
answer, 8. A check that must enumerate every way to spell a thing is the same defect as the
comment that hid the gap, so these tests RUN hooks and count rows instead.

THE MECHANISM UNDER TEST. `common.sh` installs the telemetry EXIT trap when the script
sourcing it lives under `hooks/scripts/`. That is safe because all 40 registered hooks
already source it and NO script in `hooks/scripts/` or `bin/` installs its own EXIT, TERM,
INT or HUP trap, so nothing is clobbered in either direction. The path guard is what keeps
the five CLI scripts under `bin/` out.

TWO SINKS, AND A DOUBLE-EMIT TEST NEEDS BOTH. The trap APPENDS to
`writ-events-<session>.buf` with bash; `hook_timer_end` SPAWNS python and writes to the
friction log (`common.sh:1160`). A hook that keeps its explicit call after the trap goes
universal would emit one row to each, so the count below reads both.

Per ENF-GATE-007: skeletons written and approved before implementation.
Per TEST-ISOLATE-003: the synthetic scripts live under tmp_path with a parent directory
named to match or miss the guard, so no test writes into the repo's own hooks directory,
and every run gets its own `WRIT_CACHE_DIR` and `WRIT_FRICTION_LOG`.
"""
from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
HOOKS = REPO / "hooks" / "scripts"
HOOKS_JSON = REPO / "hooks" / "hooks.json"
COMMON_SH = REPO / "bin" / "lib" / "common.sh"

# hook_execution rows in the buffer: kind, hook, duration_ms, exit_code, mode, truncated.
_ROW_FIELDS = 6


def _require(module, *names) -> None:
    missing = [n for n in names if not hasattr(module, n)]
    if missing:
        pytest.fail(f"skeleton: {module.__name__} has no {', '.join(missing)} yet")


def _registered() -> set[str]:
    doc = json.loads(HOOKS_JSON.read_text())
    names: set[str] = set()
    for entries in doc.get("hooks", {}).values():
        for entry in entries:
            for hook in entry.get("hooks", []):
                for token in str(hook.get("command", "")).split():
                    if token.endswith(".sh"):
                        names.add(token.rsplit("/", 1)[-1][:-3])
    return names


def _buffer_rows(cache_dir: Path) -> list[list[str]]:
    """Every hook_execution row the bash trap appended, across all session buffers."""
    rows: list[list[str]] = []
    for buf in cache_dir.glob("writ-events-*.buf"):
        for record in buf.read_text(errors="replace").split("\x1e"):
            if not record.strip():
                continue
            fields = record.split("\x1f")
            if fields[0] == "hook_execution":
                rows.append(fields)
    return rows


def _friction_rows(log_path: Path) -> list[dict]:
    """Every hook_execution event on the python-written friction sink."""
    if not log_path.exists():
        return []
    out = []
    for line in log_path.read_text(errors="replace").splitlines():
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if row.get("event") == "hook_execution":
            out.append(row)
    return out


def _run(script: Path, *, cache: Path, friction: Path, stdin: str = "{}",
         extra_env: dict | None = None) -> subprocess.CompletedProcess:
    env = {
        **os.environ,
        "WRIT_CACHE_DIR": str(cache),
        "WRIT_FRICTION_LOG": str(friction),
        "WRIT_PORT": "59999",
        "WRIT_NO_AUTOSTART": "1",
        "WRIT_DIR": str(REPO),
        "SKILL_DIR": str(REPO),
        **(extra_env or {}),
    }
    cache.mkdir(parents=True, exist_ok=True)
    return subprocess.run(["bash", str(script)], input=stdin, capture_output=True,
                          text=True, env=env, timeout=120)


def _synthetic(root: Path, relative: str, body: str) -> Path:
    """A script at a chosen path that sources the REAL common.sh. The parent directory is
    the whole point: the guard keys on where the sourcing script lives."""
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f'#!/usr/bin/env bash\nsource "{COMMON_SH}"\n{body}\n')
    path.chmod(0o755)
    return path


@pytest.fixture()
def sinks(tmp_path):
    cache = tmp_path / "cache"
    friction = tmp_path / "friction.jsonl"
    cache.mkdir(parents=True, exist_ok=True)
    return cache, friction


# --------------------------------------------------------------------------- #
# Capability 1, 2, 6: coverage follows location, and stops at the boundary
# --------------------------------------------------------------------------- #

class TestCoverageFollowsLocation:

    def test_a_script_under_hooks_scripts_emits_a_row_with_no_call_of_its_own(
        self, tmp_path, sinks
    ) -> None:
        """THE DEFECT, stated as the fix. Today a hook that never calls an instrumentation
        helper is invisible; 8 registered hooks are in that state."""
        cache, friction = sinks
        script = _synthetic(tmp_path, "hooks/scripts/probe-plain.sh",
                            'SESSION_ID="tel-1"\nexit 0')
        _run(script, cache=cache, friction=friction)
        rows = _buffer_rows(cache)
        assert len(rows) == 1, f"expected exactly one row, got {rows}"
        assert rows[0][1] == "probe-plain", rows[0]

    def test_a_script_under_bin_emits_nothing(self, tmp_path, sinks) -> None:
        """The guard's whole job. Five CLI scripts under bin/ source common.sh and must
        not start carrying an exit trap or emitting hook rows."""
        cache, friction = sinks
        script = _synthetic(tmp_path, "bin/probe-cli.sh", 'SESSION_ID="tel-2"\nexit 0')
        _run(script, cache=cache, friction=friction)
        assert _buffer_rows(cache) == [], "a bin/ script emitted a hook row"

    def test_a_script_elsewhere_emits_nothing(self, tmp_path, sinks) -> None:
        cache, friction = sinks
        script = _synthetic(tmp_path, "somewhere/else/probe.sh",
                            'SESSION_ID="tel-3"\nexit 0')
        _run(script, cache=cache, friction=friction)
        assert _buffer_rows(cache) == []

    def test_the_row_records_a_nonzero_exit_code(self, tmp_path, sinks) -> None:
        """A gate's refusal usually rides on stdout JSON, but the exit code is what the
        row carries, and a row that always says 0 would be useless for finding a hook
        that crashes."""
        cache, friction = sinks
        script = _synthetic(tmp_path, "hooks/scripts/probe-rc.sh",
                            'SESSION_ID="tel-4"\nexit 3')
        _run(script, cache=cache, friction=friction)
        rows = _buffer_rows(cache)
        assert rows and rows[0][3] == "3", rows

    def test_a_relative_invocation_is_still_instrumented(self, tmp_path, sinks) -> None:
        """The guard's first spelling was `*/hooks/scripts/*`, which needs a slash BEFORE
        `hooks` and so missed `bash hooks/scripts/x.sh`. Claude Code always passes an
        absolute path, so this would never have failed in production and would have dropped
        telemetry for every by-hand and by-test run. Found by probing a real hook."""
        cache, friction = sinks
        _synthetic(tmp_path, "hooks/scripts/probe-rel.sh", 'SESSION_ID="tel-rel"\nexit 0')
        env = {
            **os.environ,
            "WRIT_CACHE_DIR": str(cache),
            "WRIT_FRICTION_LOG": str(friction),
            "WRIT_PORT": "59999",
            "WRIT_NO_AUTOSTART": "1",
        }
        subprocess.run(["bash", "hooks/scripts/probe-rel.sh"], cwd=str(tmp_path),
                       input="{}", capture_output=True, text=True, env=env, timeout=60)
        rows = _buffer_rows(cache)
        assert rows and rows[0][1] == "probe-rel", (
            f"a relative invocation produced no row: {rows}"
        )

    def test_the_row_has_the_expected_field_count(self, tmp_path, sinks) -> None:
        """Anti-vacuity for the parser: a row shape change would otherwise make every
        assertion above pass on garbage."""
        cache, friction = sinks
        script = _synthetic(tmp_path, "hooks/scripts/probe-shape.sh",
                            'SESSION_ID="tel-5"\nexit 0')
        _run(script, cache=cache, friction=friction)
        rows = _buffer_rows(cache)
        assert rows and len(rows[0]) == _ROW_FIELDS, rows


# --------------------------------------------------------------------------- #
# Capability 3, 4: the real hooks, and exactly one row each
# --------------------------------------------------------------------------- #

class TestTheRealHooksEmitExactlyOnce:
    """Real scripts, because the synthetic ones cannot show that a hook's own code path
    still reaches exit, and because the double-emit risk is specific to real callers.
    """

    # One of the 8 that emitted nothing, and one of the 9 that emitted through python.
    _PREVIOUSLY_SILENT = "writ-pressure-audit"
    _FORMER_TIMER_CALLER = "writ-cwd-changed"

    def _count(self, name: str, cache: Path, friction: Path, stdin: str) -> int:
        _run(HOOKS / f"{name}.sh", cache=cache, friction=friction, stdin=stdin)
        buffered = [r for r in _buffer_rows(cache) if r[1] == name]
        spawned = [r for r in _friction_rows(friction) if r.get("hook_name") == name]
        return len(buffered) + len(spawned)

    def test_a_previously_silent_hook_now_emits(self, sinks) -> None:
        cache, friction = sinks
        count = self._count(self._PREVIOUSLY_SILENT, cache, friction,
                            json.dumps({"session_id": "tel-real-1"}))
        assert count >= 1, f"{self._PREVIOUSLY_SILENT} still emits no execution row"

    def test_a_former_timer_caller_emits_exactly_one(self, sinks) -> None:
        """THE REGRESSION THIS CYCLE COULD INTRODUCE. Keeping the explicit
        `hook_timer_end` call alongside the universal trap double-counts every duration
        in the friction analysis."""
        cache, friction = sinks
        count = self._count(self._FORMER_TIMER_CALLER, cache, friction,
                            json.dumps({"session_id": "tel-real-2", "cwd": str(REPO)}))
        assert count == 1, (
            f"{self._FORMER_TIMER_CALLER} emitted {count} rows; the trap and the explicit "
            "timer call are both firing"
        )

    def test_no_registered_hook_keeps_an_explicit_timer_call(self) -> None:
        """The deletion, asserted structurally. This is a scan, and it is allowed to be
        one: it looks for a call that must be ABSENT, so a spelling it misses cannot make
        a broken hook look fine, only an already-clean one."""
        offenders = sorted(
            name for name in _registered()
            if "hook_timer_end" in "\n".join(
                line for line in (HOOKS / f"{name}.sh").read_text().splitlines()
                if not line.lstrip().startswith("#")
            )
        )
        assert not offenders, (
            "these hooks still call hook_timer_end, which now duplicates the trap's row "
            f"and spawns python to do it: {offenders}"
        )

    def test_an_explicit_hook_instrument_name_still_wins(self, tmp_path, sinks) -> None:
        """Must-not-regress: ~20 hooks name themselves explicitly and those names are what
        the existing metrics are keyed on."""
        cache, friction = sinks
        script = _synthetic(tmp_path, "hooks/scripts/probe-named.sh",
                            'hook_instrument "chosen-name"\nSESSION_ID="tel-6"\nexit 0')
        _run(script, cache=cache, friction=friction)
        rows = _buffer_rows(cache)
        assert rows and rows[0][1] == "chosen-name", rows


# --------------------------------------------------------------------------- #
# The drainer must include itself
# --------------------------------------------------------------------------- #

class TestADrainerIncludesItself:
    """Instrumenting the hooks that DRAIN the buffer created a new defect: each appended
    its own row at exit, AFTER the drain, re-creating the file it had just removed.
    Reproduced by seeding one row and running the hook: the buffer survived, holding
    `hook_execution|friction-logger|73|0||0`.

    Three hooks drain (`friction-logger.sh:64`, `writ-subagent-stop.sh:73`,
    `writ-session-end.sh:41`), so every turn left a one-row orphan and a session's final
    row waited for a turn that might never come.

    The rows are read from WRIT_FRICTION_LOG, which is where `writ-flush-events.py` sends
    them; that is the same sink `tests/test_event_buffer_durability.py` reads.
    """

    HOOK = HOOKS / "friction-logger.sh"
    SID = "drainer-probe"
    SEEDED = "seeded-hook"

    def _seed(self, cache: Path) -> Path:
        cache.mkdir(parents=True, exist_ok=True)
        buf = cache / f"writ-events-{self.SID}.buf"
        buf.write_text(f"hook_execution\x1f{self.SEEDED}\x1f5\x1f0\x1fwork\x1f0\x1e")
        return buf

    def _drain(self, cache: Path, friction: Path) -> None:
        env = {
            **os.environ,
            "WRIT_CACHE_DIR": str(cache),
            "WRIT_FRICTION_LOG": str(friction),
            "WRIT_LOG_ROOT": str(cache.parent / "logroot"),
            "WRIT_PORT": "59999",
            "WRIT_NO_AUTOSTART": "1",
            "WRIT_DIR": str(REPO),
            "SKILL_DIR": str(REPO),
        }
        subprocess.run(["bash", str(self.HOOK)],
                       input=json.dumps({"session_id": self.SID}),
                       capture_output=True, text=True, env=env, timeout=120)

    def test_the_buffer_is_gone_after_the_drain(self, sinks) -> None:
        """THE DEFECT. The drain unlinks the buffer, then the drainer's own exit row
        re-creates it."""
        cache, friction = sinks
        buf = self._seed(cache)
        self._drain(cache, friction)
        assert not buf.exists(), (
            "the drainer left a buffer behind, holding: "
            + repr(buf.read_text(errors="replace"))
        )

    def test_the_drainers_own_row_reaches_the_log(self, sinks) -> None:
        """The row must be in the buffer BEFORE the drain runs, or it is stranded."""
        cache, friction = sinks
        self._seed(cache)
        self._drain(cache, friction)
        names = [r.get("hook_name") for r in _friction_rows(friction)]
        assert "friction-logger" in names, (
            f"the drainer's own row never reached the log: {names}"
        )

    def test_the_seeded_row_still_reaches_the_log(self, sinks) -> None:
        """MUST-NOT-REGRESS: whatever the ordering, the drain still has to drain."""
        cache, friction = sinks
        self._seed(cache)
        self._drain(cache, friction)
        names = [r.get("hook_name") for r in _friction_rows(friction)]
        assert self.SEEDED in names, f"the drain lost the row it was there to flush: {names}"


# --------------------------------------------------------------------------- #
# Capability 5: the mode survives losing the explicit call
# --------------------------------------------------------------------------- #

class TestTheRowKeepsItsMode:

    def test_the_hooks_own_mode_variable_reaches_the_row(self, tmp_path, sinks) -> None:
        """`writ-pre-write-dispatch` passes the mode from the /pre-write-check envelope to
        hook_timer_end to avoid a second `mode get` spawn. Deleting that call must not
        lose it, and the seam is the EXISTING convention rather than a new one:
        `_writ_row_mode_cached` (`common.sh:118-120`) reads `CURRENT_MODE` then `MODE`,
        both of which those hooks already hold. An earlier draft of this test asserted a
        preset `_WRIT_ROW_MODE`, which that function overwrites."""
        cache, friction = sinks
        script = _synthetic(tmp_path, "hooks/scripts/probe-mode.sh",
                            'SESSION_ID="tel-7"\nMODE="work"\nexit 0')
        _run(script, cache=cache, friction=friction)
        rows = _buffer_rows(cache)
        assert rows and rows[0][4] == "work", rows

    def test_current_mode_also_reaches_the_row(self, tmp_path, sinks) -> None:
        """The other spelling in the convention, used by `writ-cwd-changed` and
        `writ-dispatch-discipline`."""
        cache, friction = sinks
        script = _synthetic(tmp_path, "hooks/scripts/probe-cmode.sh",
                            'SESSION_ID="tel-10"\nCURRENT_MODE="debug"\nexit 0')
        _run(script, cache=cache, friction=friction)
        rows = _buffer_rows(cache)
        assert rows and rows[0][4] == "debug", rows


# --------------------------------------------------------------------------- #
# Capability 8, 9: the doctor check, structural then observational
# --------------------------------------------------------------------------- #

def _fake_install(root: Path, *, hooks: dict[str, bool]) -> Path:
    """`hooks` maps hook name to whether its script sources common.sh."""
    (root / "hooks" / "scripts").mkdir(parents=True, exist_ok=True)
    registered = []
    for name, sources in hooks.items():
        body = "#!/usr/bin/env bash\n"
        body += 'source "$SKILL_DIR/bin/lib/common.sh"\n' if sources else "true\n"
        (root / "hooks" / "scripts" / f"{name}.sh").write_text(body)
        registered.append({"hooks": [{"type": "command",
                                      "command": "bash ${CLAUDE_PLUGIN_ROOT}/hooks/scripts/"
                                                 + name + ".sh"}]})
    (root / "hooks" / "hooks.json").write_text(
        json.dumps({"hooks": {"PreToolUse": registered}})
    )
    return root


class TestTheDoctorReportsTelemetryCoverage:

    def _run_check(self, monkeypatch, root: Path, observed: set[str]):
        from writ.session import doctor

        _require(doctor, "check_hook_telemetry_coverage", "_observed_hook_names")
        monkeypatch.setattr(doctor, "_PACKAGE_ROOT", root)
        monkeypatch.setattr(doctor, "_observed_hook_names", lambda: set(observed))
        return doctor.check_hook_telemetry_coverage(doctor.DoctorOptions())

    def test_a_hook_that_does_not_source_common_fails(self, tmp_path, monkeypatch) -> None:
        root = _fake_install(tmp_path, hooks={"writ-a": True, "writ-b": False})
        result = self._run_check(monkeypatch, root, observed={"writ-a", "writ-b"})
        assert result.status == "fail", result
        assert "writ-b" in result.detail, result.detail

    def test_an_unobserved_hook_only_warns(self, tmp_path, monkeypatch) -> None:
        """Rarity is not death: `writ-cwd-changed` is instrumented today and has no rows
        because the working directory rarely changes."""
        root = _fake_install(tmp_path, hooks={"writ-a": True, "writ-rare": True})
        result = self._run_check(monkeypatch, root, observed={"writ-a"})
        assert result.status == "warn", result
        assert "writ-rare" in result.detail, result.detail

    def test_a_structural_gap_outranks_an_observational_one(self, tmp_path, monkeypatch) -> None:
        root = _fake_install(tmp_path, hooks={"writ-a": False, "writ-rare": True})
        result = self._run_check(monkeypatch, root, observed=set())
        assert result.status == "fail", result

    def test_full_coverage_passes(self, tmp_path, monkeypatch) -> None:
        root = _fake_install(tmp_path, hooks={"writ-a": True, "writ-b": True})
        result = self._run_check(monkeypatch, root, observed={"writ-a", "writ-b"})
        assert result.status == "ok", result

    def test_no_readable_metrics_is_not_an_accusation(self, tmp_path, monkeypatch) -> None:
        """Found by the doctor's own all-ok stub: with no metrics stream the first draft
        warned about all 40 hooks at once. An empty observation set means the log could not
        be read (fresh install, redirected log, pruned directory), not that every hook is
        dead, and a check that cannot tell those apart must not accuse."""
        root = _fake_install(tmp_path, hooks={"writ-a": True, "writ-b": True})
        result = self._run_check(monkeypatch, root, observed=set())
        assert result.status == "ok", result
        assert "could not be observed" in result.detail, result.detail

    def test_the_check_does_not_fail_on_this_repo(self) -> None:
        from writ.session import doctor

        _require(doctor, "check_hook_telemetry_coverage")
        result = doctor.check_hook_telemetry_coverage(doctor.DoctorOptions())
        assert result.status != "fail", result.detail

    def test_the_check_is_registered(self) -> None:
        from writ.session import doctor

        names = [name for name, _fn in doctor._CHECKS]
        assert "hook-telemetry-coverage" in names, names

    def test_every_registered_hook_sources_common_sh(self) -> None:
        """The structural precondition the whole design rests on, asserted directly so it
        cannot quietly stop being true."""
        missing = sorted(
            name for name in _registered()
            if "common.sh" not in (HOOKS / f"{name}.sh").read_text()
        )
        assert not missing, f"these registered hooks do not source common.sh: {missing}"


# --------------------------------------------------------------------------- #
# Capability 10: stdout stays clean
# --------------------------------------------------------------------------- #

class TestStdoutStaysClean:
    """`writ-rag-inject` is one of the 8 and its stdout IS the injected rule block, so a
    single stray byte from the telemetry path corrupts every prompt's context.
    """

    def test_sourcing_and_exiting_writes_nothing_to_stdout(self, tmp_path, sinks) -> None:
        cache, friction = sinks
        script = _synthetic(tmp_path, "hooks/scripts/probe-silent.sh",
                            'SESSION_ID="tel-8"\nexit 0')
        proc = _run(script, cache=cache, friction=friction)
        assert proc.stdout == "", f"telemetry wrote to stdout: {proc.stdout!r}"

    def test_a_hooks_own_stdout_is_preserved(self, tmp_path, sinks) -> None:
        cache, friction = sinks
        script = _synthetic(tmp_path, "hooks/scripts/probe-echo.sh",
                            'SESSION_ID="tel-9"\nprintf \'{"ok":true}\'\nexit 0')
        proc = _run(script, cache=cache, friction=friction)
        assert proc.stdout == '{"ok":true}', proc.stdout
