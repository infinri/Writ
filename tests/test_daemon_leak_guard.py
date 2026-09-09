"""The module-boundary leak guard: pins the pure half of `tests/_daemon_leak.py` (parse,
survivor, exclusion and report branches, all from literal `ps` fixtures with no daemon in
play) plus the derived hook population in `tests/_inventory.py::daemon_starting_hooks()`.

Traces plan.md 2412ba38-51e1-4b73-895b-7b240a3c21d3's capabilities 3, 4, 10, 11, 12, 13,
14, 15 and 16. Capabilities 5-9 (start_isolated_daemon returning real pids, and
stop_isolated_daemon's stopped-True / stopped-False / socket-unlink verdicts) are
DELIBERATELY NOT hermetic tests here: they can only be proven against a real daemon, which
is why the plan rides them on `tests/test_advance_phase_token_gate.py::own_daemon`, the
module that already owns one, rather than claiming them from fixtures below.

CORRECTLY RED as of the testing phase, for TWO separate reasons stacked in one collection
failure: `tests/_daemon_leak.py` does not exist at all yet (assigned to the implementation
phase), so the import below fails and this whole file fails to COLLECT. Once that module
lands, `daemon_starting_hooks()` (assigned to `tests/_inventory.py`, also not yet written)
will still be missing, and the two hook-population test classes near the bottom of this
file will fail to collect on their own import, independently of the first failure. Every
test below is written against the contract those functions must satisfy, following the
convention `tests/test_advance_phase_token_gate.py` already uses for
`start_isolated_daemon` / `stop_isolated_daemon`.

THE CONTRACT THIS FILE ASSUMES OF `tests/_daemon_leak.py` (choose names to match on
implementation):

    class UnmeasurableSnapshot(Exception): ...

    def _serve_pattern(port: int | str) -> str:
        # f"serve --port {port} --host" -- the substring every launch spelling shares.

    def parse_snapshot(ps_output: str | None) -> dict:
        # {"measured": bool, "processes": {pid: cmdline}}. measured is False when
        # ps_output is None or empty (ps missing, or returned nothing at all) --
        # never conflated with "measured, and happens to hold zero Writ processes".

    def find_survivors(baseline: dict, current: dict, *, suite_port: int) -> list[dict]:
        # Candidates (cmdline contains "serve --port" AND "writ") present in `current`
        # and absent from `baseline`, excluding `suite_port`. Each entry:
        # {"pid": int, "port": int, "cmdline": str}. Raises UnmeasurableSnapshot when
        # either snapshot's "measured" is False.

    def suite_port() -> int:
        # The suite's own daemon port, resolved LIVE from WRIT_PORT (mirrors
        # tests/_daemon.py::_port(), which this module does not import, so the two
        # stay independently testable).

    def format_report(survivor: dict, health: dict | None) -> str:
        # Names pid, port, cmdline, and either health's fields or "no /health answer".

`tests/_inventory.py::daemon_starting_hooks(*, scripts_dir=HOOK_SCRIPTS_DIR) -> dict[str,
bool]` follows this module's own established keyword-parameter convention
(`envelope_emitting_scripts(*, scripts_dir=)`, `style_swept_docs(*, repo=)`) so its
precision can be pinned against synthetic fixtures under `tmp_path`, never against the
real tree's current wording alone.
"""
from __future__ import annotations

import os
import subprocess
import time

import pytest

import tests._daemon as _daemon_module
import tests._daemon_leak as _leak_module
from tests._daemon import (
    _pids_serving_port,
    _wait_for_isolated_down,
    stop_isolated_daemon,
)
from tests._daemon_leak import (
    UnmeasurableSnapshot,
    _serve_pattern,
    confirm_survivors,
    find_survivors,
    format_report,
    live_snapshot,
    parse_snapshot,
    snapshot_text,
    suite_port,
)
from tests._inventory import daemon_starting_hooks

SUITE_PORT = 8799
OPERATOR_PORT = 8765


def _venv_spelling(port: int) -> str:
    """`<venv>/bin/writ serve` -- the ONLY spelling the old `pkill -f "writ serve
    --port N"` pattern ever matched."""
    return (
        "/home/lucio.saldivar/.claude/skills/writ/.venv/bin/python3 "
        f"/home/lucio.saldivar/.claude/skills/writ/.venv/bin/writ serve --port {port} --host localhost"
    )


def _module_spelling(port: int) -> str:
    """`<python> -m writ.cli serve` -- the shim spelling the old pattern could never
    match, because the literal substring "writ serve" never appears in it."""
    return (
        "/home/lucio.saldivar/.claude/skills/writ/.venv/bin/python3 -m writ.cli serve "
        f"--port {port} --host localhost"
    )


def _cache_venv_spelling(port: int) -> str:
    """`<cache venv>/bin/writ serve` -- the console-script variant under
    `~/.cache/writ/.venv`, the second live launcher on this machine."""
    return (
        "/home/lucio.saldivar/.cache/writ/.venv/bin/python3 "
        f"/home/lucio.saldivar/.cache/writ/.venv/bin/writ serve --port {port} --host localhost"
    )


_SPELLINGS = {
    "venv_console_script": _venv_spelling,
    "module_invocation": _module_spelling,
    "cache_venv_console_script": _cache_venv_spelling,
}


def _ps_snapshot(*entries: tuple[int, str]) -> str:
    """Literal `ps -eo pid=,args=` text: one "<pid> <cmdline>" line per entry."""
    return "\n".join(f"{pid} {cmdline}" for pid, cmdline in entries)


# --------------------------------------------------------------------------- #
# Capability 3: the recognizer matches all three launch spellings.
# --------------------------------------------------------------------------- #


class TestServePatternMatchesAllThreeLaunchSpellings:
    """The three shapes given verbatim from this machine, because the defect this
    cycle fixes is that only the first was ever matched."""

    VENV_CMDLINE = (
        "/home/lucio.saldivar/.claude/skills/writ/.venv/bin/python3 "
        "/home/lucio.saldivar/.claude/skills/writ/.venv/bin/writ serve --port 19998 --host localhost"
    )
    MODULE_CMDLINE = (
        "/home/lucio.saldivar/.claude/skills/writ/.venv/bin/python3 -m writ.cli serve "
        "--port 59015 --host localhost"
    )
    CACHE_VENV_CMDLINE = (
        "/home/lucio.saldivar/.cache/writ/.venv/bin/python3 "
        "/home/lucio.saldivar/.cache/writ/.venv/bin/writ serve --port 50385 --host localhost"
    )

    def test_the_venv_console_script_spelling_matches_its_own_port(self) -> None:
        assert _serve_pattern(19998) in self.VENV_CMDLINE

    def test_the_module_invocation_spelling_matches_its_own_port(self) -> None:
        """Reddened by reverting the recognizer to the old `writ serve --port N`
        substring (mutation 2 in the plan's own list): `-m writ.cli serve` never
        contains the literal words "writ serve" together, so that substring cannot
        match this spelling no matter what port is named."""
        assert _serve_pattern(59015) in self.MODULE_CMDLINE

    def test_the_cache_venv_console_script_spelling_matches_its_own_port(self) -> None:
        assert _serve_pattern(50385) in self.CACHE_VENV_CMDLINE


# --------------------------------------------------------------------------- #
# Capability 4: the recognizer excludes the two protected ports.
# --------------------------------------------------------------------------- #


class TestServePatternExcludesProtectedPorts:
    """A recognizer that matches everything passes every positive test above; these
    pin that a pattern built for a throwaway port never matches the operator's 8765
    or the suite's 8799, in any of the three spellings."""

    THROWAWAY_PORT = 19998

    @pytest.mark.parametrize("protected_port", [OPERATOR_PORT, SUITE_PORT])
    @pytest.mark.parametrize("spelling_name", sorted(_SPELLINGS))
    def test_a_protected_ports_cmdline_never_matches_a_different_ports_pattern(
        self, spelling_name: str, protected_port: int,
    ) -> None:
        """Reddened by a recognizer keyed on the port's digits alone, without the
        surrounding "serve --port ... --host" shape: a bare digit match could
        coincidentally substring-match an unrelated number elsewhere in a longer
        cmdline."""
        cmdline = _SPELLINGS[spelling_name](protected_port)
        assert _serve_pattern(self.THROWAWAY_PORT) not in cmdline

    @pytest.mark.parametrize("spelling_name", sorted(_SPELLINGS))
    def test_the_protected_ports_own_pattern_does_match_their_own_cmdline(
        self, spelling_name: str,
    ) -> None:
        """Anti-vacuity for the test above: proves the exclusion is about the PORT
        NUMBER, not about the protected ports being unmatchable by construction."""
        cmdline = _SPELLINGS[spelling_name](self.THROWAWAY_PORT)
        assert _serve_pattern(self.THROWAWAY_PORT) in cmdline


# --------------------------------------------------------------------------- #
# Capability 10: a survivor is reported with pid, port, cmdline and /health.
# --------------------------------------------------------------------------- #


class TestFindSurvivorsReportsANewCandidate:
    """A process that appeared during a module and survives its end is reported with
    pid, port, cmdline, and its /health destinations or an explicit absence."""

    def test_a_new_writ_serve_process_is_reported(self) -> None:
        baseline = parse_snapshot(_ps_snapshot((1, "/sbin/init")))
        current = parse_snapshot(
            _ps_snapshot((1, "/sbin/init"), (52806, _venv_spelling(19998)))
        )
        survivors = find_survivors(baseline, current, suite_port=SUITE_PORT)
        assert len(survivors) == 1
        survivor = survivors[0]
        assert survivor["pid"] == 52806
        assert survivor["port"] == 19998
        assert survivor["cmdline"] == _venv_spelling(19998)

    def test_the_report_names_pid_port_cmdline_and_no_health_answer(self) -> None:
        """Reddened by a formatter that drops any one of pid/port/cmdline, or that
        reports a clean state instead of naming the absence of a /health reply."""
        survivor = {"pid": 52806, "port": 19998, "cmdline": _venv_spelling(19998)}
        report = format_report(survivor, None)
        assert "52806" in report
        assert "19998" in report
        assert _venv_spelling(19998) in report
        assert "no /health answer" in report

    def test_the_report_names_healths_destinations_when_present(self) -> None:
        """The same evidence shape the diagnosis used: audit_log, friction_log,
        cache_dir and startup_time."""
        survivor = {"pid": 52806, "port": 19998, "cmdline": _venv_spelling(19998)}
        health = {
            "audit_log": "/skill/var/logs/github.com/infinri/Writ/audit.jsonl",
            "friction_log": "/skill/var/logs/github.com/infinri/Writ/friction.jsonl",
            "cache_dir": "/tmp/tmp.2AgLz4jWBt/c",
            "startup_time": "2026-09-09T08:29:17",
        }
        report = format_report(survivor, health)
        for value in health.values():
            assert value in report, f"{value!r} missing from report: {report!r}"


# --------------------------------------------------------------------------- #
# Capability 11: a process already in the baseline is never a survivor.
# --------------------------------------------------------------------------- #


class TestFindSurvivorsExcludesTheBaselineProcess:
    def test_a_writ_serve_process_already_in_the_baseline_is_not_reported(self) -> None:
        """Reddened by dropping the "not in baseline" filter (mutation 1 in the
        plan's own list): the operator's already-running daemon, present at the
        first module boundary, would then read as a leak on every boundary."""
        cmdline = _venv_spelling(19998)
        baseline = parse_snapshot(_ps_snapshot((52806, cmdline)))
        current = parse_snapshot(_ps_snapshot((52806, cmdline)))
        assert find_survivors(baseline, current, suite_port=SUITE_PORT) == []


# --------------------------------------------------------------------------- #
# Capability 12: the suite's own daemon port is excluded, resolved live.
# --------------------------------------------------------------------------- #


class TestFindSurvivorsExcludesTheSuitesOwnPort:
    def test_a_new_process_on_the_suite_port_is_not_reported(self) -> None:
        baseline = parse_snapshot(_ps_snapshot((1, "/sbin/init")))
        current = parse_snapshot(
            _ps_snapshot((1, "/sbin/init"), (999, _venv_spelling(SUITE_PORT)))
        )
        assert find_survivors(baseline, current, suite_port=SUITE_PORT) == []

    def test_suite_port_is_resolved_live_from_writ_port(self, monkeypatch) -> None:
        """Reddened by hardcoding 8799 instead of reading WRIT_PORT live: a suite
        invoked under a different WRIT_PORT would then falsely report its OWN
        daemon as a leak. Mirrors tests/_daemon.py::_port(), independently."""
        monkeypatch.setenv("WRIT_PORT", "47001")
        assert suite_port() == 47001


# --------------------------------------------------------------------------- #
# Capability 13: a non-Writ process naming "serve --port" is never reported.
# --------------------------------------------------------------------------- #


class TestFindSurvivorsExcludesNonWritProcesses:
    def test_an_unrelated_esbuild_serve_process_is_not_reported(self) -> None:
        """Reddened by matching on "serve --port" alone, without also requiring a
        "writ" token in the cmdline: a developer's dev-server, started mid-suite on
        an unrelated port, must not be reported."""
        baseline = parse_snapshot(_ps_snapshot((1, "/sbin/init")))
        current = parse_snapshot(
            _ps_snapshot(
                (1, "/sbin/init"),
                (4242, "node /usr/local/bin/esbuild serve --port 3000"),
            )
        )
        assert find_survivors(baseline, current, suite_port=SUITE_PORT) == []


# --------------------------------------------------------------------------- #
# Capability 14: an unmeasurable snapshot fails the NEXT boundary loudly.
# --------------------------------------------------------------------------- #


class TestUnmeasurableSnapshotFailsLoud:
    """`ps` missing or returning nothing at all must make the next boundary FAIL
    with that reason, never read as a clean, empty snapshot. Absence is not a
    policy."""

    def test_empty_ps_output_is_recorded_as_unmeasured(self) -> None:
        assert parse_snapshot("")["measured"] is False

    def test_missing_ps_is_recorded_as_unmeasured(self) -> None:
        assert parse_snapshot(None)["measured"] is False

    def test_an_unmeasured_current_snapshot_raises_rather_than_reading_clean(self) -> None:
        """Reddened by "return a clean verdict for an empty ps snapshot" (mutation 3
        in the plan's own list): a guard that reads green because it could not look
        is the failure mode this repo has already paid for."""
        baseline = parse_snapshot(_ps_snapshot((1, "/sbin/init")))
        current = parse_snapshot("")
        with pytest.raises(UnmeasurableSnapshot):
            find_survivors(baseline, current, suite_port=SUITE_PORT)

    def test_an_unmeasured_baseline_also_raises(self) -> None:
        baseline = parse_snapshot(None)
        current = parse_snapshot(_ps_snapshot((1, "/sbin/init")))
        with pytest.raises(UnmeasurableSnapshot):
            find_survivors(baseline, current, suite_port=SUITE_PORT)

    def test_a_normal_boundary_with_no_candidates_is_measured_and_clean(self) -> None:
        """Anti-vacuity for the two tests above: proves "measured, zero candidates"
        and "unmeasurable" are genuinely distinguished outcomes, not the same code
        path answering to two different names."""
        baseline = parse_snapshot(_ps_snapshot((1, "/sbin/init")))
        current = parse_snapshot(_ps_snapshot((1, "/sbin/init"), (2, "/usr/bin/bash")))
        assert current["measured"] is True
        assert find_survivors(baseline, current, suite_port=SUITE_PORT) == []


# --------------------------------------------------------------------------- #
# Capability 15: the hook population is derived, not a hardcoded list.
# --------------------------------------------------------------------------- #


class TestDaemonStartingHooksIsDerivedNotHardcoded:
    """The population must come from scanning `hooks/scripts/`, never from a fixed
    list of names, so a new autostarting hook is covered with no edit and a
    detector that quietly returns `{}` cannot pass silently."""

    def test_the_real_scripts_dir_yields_a_non_empty_map(self) -> None:
        """Reddened by a `daemon_starting_hooks` that returns `{}` unconditionally,
        the vacuous-detector failure this repo's own `tests/_inventory.py` module
        docstring says it has hit three times: every other assertion in this class
        and the next would then pass vacuously."""
        population = daemon_starting_hooks()
        assert isinstance(population, dict)
        assert population, (
            "daemon_starting_hooks() returned an empty map against the real "
            "hooks/scripts/ tree"
        )

    def test_a_synthetic_guarded_writ_ensure_server_call_is_found_and_marked_true(
        self, tmp_path,
    ) -> None:
        """Reddened by a derivation that only recognizes a hardcoded filename: a
        brand-new hook this test invents, under a `scripts_dir` the real tree never
        had, must still appear in the map."""
        script = tmp_path / "made-up-hook.sh"
        script.write_text(
            "#!/usr/bin/env bash\n"
            'if [ -z "${WRIT_NO_AUTOSTART:-}" ]; then\n'
            "    writ_ensure_server\n"
            "fi\n"
        )
        population = daemon_starting_hooks(scripts_dir=tmp_path)
        assert population.get("made-up-hook.sh") is True, population

    def test_a_synthetic_unguarded_writ_ensure_server_call_is_marked_false(
        self, tmp_path,
    ) -> None:
        """Reddened by a derivation that assumes every launch it finds is guarded,
        instead of checking for the guard: this hook calls the launch with no
        conditional anywhere in the file."""
        script = tmp_path / "made-up-hook.sh"
        script.write_text("#!/usr/bin/env bash\nwrit_ensure_server\n")
        population = daemon_starting_hooks(scripts_dir=tmp_path)
        assert population.get("made-up-hook.sh") is False, population

    def test_a_synthetic_bare_nohup_serve_launch_is_found(self, tmp_path) -> None:
        """Reddened by a derivation that recognizes only the `writ_ensure_server`
        call and misses the second launch shape the plan names: a bare
        `nohup ... serve`."""
        script = tmp_path / "made-up-hook.sh"
        script.write_text(
            "#!/usr/bin/env bash\n"
            'nohup "$VENV_DIR/bin/writ" serve --port "$WRIT_PORT" --host "$WRIT_HOST" &\n'
        )
        population = daemon_starting_hooks(scripts_dir=tmp_path)
        assert "made-up-hook.sh" in population, population

    def test_a_script_with_no_launch_reachability_is_absent_from_the_map(
        self, tmp_path,
    ) -> None:
        """Reddened by a derivation that includes every script under `scripts_dir`
        regardless of content, which would make "the population is the process
        table" (for the sibling leak guard) or "launch reachability" (here)
        meaningless."""
        script = tmp_path / "unrelated-hook.sh"
        script.write_text("#!/usr/bin/env bash\necho hello\n")
        population = daemon_starting_hooks(scripts_dir=tmp_path)
        assert "unrelated-hook.sh" not in population, population


# --------------------------------------------------------------------------- #
# Capability 16: every hook in the real map consults the guard.
# --------------------------------------------------------------------------- #


class TestDaemonStartingHooksAllConsultTheGuard:
    """RED today, and stated as such rather than silently expected to pass:
    `hooks/scripts/session-start-bootstrap.sh` calls `writ_ensure_server` at line
    107 with no guard, so its value in the real map is False until the
    implementation wraps that call in a WRIT_NO_AUTOSTART check."""

    def test_every_hook_in_the_real_population_consults_the_guard(self) -> None:
        """Reddened (as of the testing phase, ALREADY red) by
        `hooks/scripts/session-start-bootstrap.sh`'s unguarded `writ_ensure_server`
        call; goes green only once that call is wrapped in the same
        `[ -z "${WRIT_NO_AUTOSTART:-}" ]` check `writ-rag-inject.sh:48` already
        uses. Holding the population as a MAP (not a boolean AND of the whole
        tree) means a hook that later DECAYS -- keeps its launch, loses its guard
        -- fails this same assertion too, rather than silently dropping out."""
        population = daemon_starting_hooks()
        unguarded = sorted(name for name, guarded in population.items() if not guarded)
        assert unguarded == [], (
            f"hooks that launch a daemon without consulting WRIT_NO_AUTOSTART: {unguarded}"
        )


# --------------------------------------------------------------------------- #
# Added after review: the derivation's REAL heredoc behaviour, pinned as a
# documented property rather than left as a future mystery red.
# --------------------------------------------------------------------------- #


class TestDaemonStartingHooksReadsIntoHeredocBodies:
    """A launch token inside a QUOTED heredoc body is classified as a real launch.

    This is the direction the derivation actually fails in, MEASURED against a
    synthetic script rather than reasoned from the code: `_emission_end` folds a
    heredoc body into the same scanned block as the command that opens it, so an
    inert `nohup ... serve` living in a Python string inside `<<'PY'` is matched.
    An earlier revision of `_dsh_guard_verdict`'s docstring claimed the opposite
    ("skipped whole, so a launch inside one is not seen at all"), which was wrong
    in the more dangerous direction: it promised a MISS where the code produces a
    FALSE POSITIVE.

    The real population is unaffected today (both entries carry a genuine launch
    and both read True), so this pins behaviour, not a defect. It is a test
    rather than a comment because the next person to add a hook with an
    interpreter heredoc will meet a False in the map, and this is where the
    answer is.
    """

    # BOTH launch alternatives, because the docstring in `_dsh_guard_verdict` claims the
    # measurement for both and prose is not a pin. The bodies are inert either way: a
    # quoted heredoc delimiter stops bash expanding anything inside it, and these are
    # python string literals.
    HEREDOC_BODIES = {
        "nohup_serve": 'TEXT = "nohup something serve --port 1 &"',
        "writ_ensure_server": 'TEXT = "writ_ensure_server"',
    }

    def _heredoc_hook(self, body: str) -> str:
        return (
            "#!/usr/bin/env bash\n"
            "python3 <<'PY'\n"
            f"{body}\n"
            "print(TEXT)\n"
            "PY\n"
        )

    @pytest.mark.parametrize("launch", sorted(HEREDOC_BODIES))
    def test_an_inert_launch_inside_a_quoted_heredoc_body_is_still_classified(
        self, launch: str, tmp_path,
    ) -> None:
        """Reddened by a scanner that genuinely skipped heredoc bodies, which is a
        change this cycle deliberately did NOT make: reworking the block walk is
        outside its plan, and a wrong docstring is the part that misleads."""
        script = tmp_path / "heredoc-hook.sh"
        script.write_text(self._heredoc_hook(self.HEREDOC_BODIES[launch]))
        population = daemon_starting_hooks(scripts_dir=tmp_path)
        assert population.get("heredoc-hook.sh") is False, population


# --------------------------------------------------------------------------- #
# Added after review: the snapshot must prove it can SEE, not merely that it
# came back non-empty. Regression cover for the truncation defect this cycle
# found in its own guard.
# --------------------------------------------------------------------------- #


class TestTheLiveSnapshotProvesItCanSee:
    """The emptiness check cannot catch a full-but-blind snapshot.

    MEASURED, and this is the defect being covered: inside a pytest process
    `ps -eo pid=,args=` returned 697 lines with the widest at 79 characters,
    because procps cuts the args column to COLUMNS-1 and something sets
    COLUMNS=80 at the C level where `os.environ` never sees it. Every daemon line
    lost its `serve --port N` tail, so a table holding four live daemons reported
    ZERO candidates while `measured` stayed True. `-ww` fixes the read; these
    tests are what keeps it fixed, and they pin the PROPERTY (the snapshot can
    still see this process's own whole command line) rather than the argv
    spelling that currently delivers it.
    """

    # The measured procps cut: COLUMNS=80 leaves 79 characters of text.
    PROCPS_WIDTH = 80

    def _cut_live_table(self) -> tuple[str, str, int]:
        """One real `ps` read, plus the same text with every line cut short.

        ONE read, not two, so the uncut and cut counts compared below cannot
        differ because a process started or exited between them.
        """
        text = snapshot_text()
        assert text, "ps returned nothing, so this test cannot build a cut table"
        own = next(
            (ln for ln in text.splitlines() if ln.strip().startswith(f"{os.getpid()} ")),
            None,
        )
        assert own is not None, (
            f"the real table does not contain this process (pid {os.getpid()}), so the "
            f"self-check being tested here has nothing to look for"
        )
        # 80 is the measured width; the min keeps the cut REAL on a machine whose own
        # command line is shorter than that, which is also the case where the live
        # defect could not fire.
        width = min(self.PROCPS_WIDTH, len(own.rstrip()) - 1)
        cut = "\n".join(line[:width] for line in text.splitlines())
        assert len(own.rstrip()) > width, "the cut did not shorten this process's own line"
        return text, cut, width

    def test_a_table_whose_lines_were_cut_reads_as_unmeasured(self) -> None:
        """Reddened by reverting `-eww` to `-eo` in the snapshot argv, or by
        dropping the self-check: either way a cut table reads as a clean
        measurement, which is exactly the state this cycle shipped and found."""
        _text, cut, _width = self._cut_live_table()
        snapshot = parse_snapshot(cut, self_check=True)
        assert snapshot["measured"] is False
        assert "TRUNCATED" in (snapshot["reason"] or ""), snapshot["reason"]

    def test_the_cut_table_holds_every_process_the_uncut_one_did(self) -> None:
        """Anti-vacuity, and the load-bearing half: it proves EMPTINESS is not what
        caught the cut table. Same line count, same pids, no `serve --port` tails."""
        text, cut, _width = self._cut_live_table()
        assert len(parse_snapshot(cut)["processes"]) == len(
            parse_snapshot(text)["processes"]
        )
        assert parse_snapshot(cut)["processes"], "the cut table parsed to nothing"

    def test_the_live_snapshot_passes_the_self_check_today(self) -> None:
        """The positive signal. Reddened by reverting `-eww` to `-eo`, under the
        documented invocation (`.venv/bin/python -m pytest <paths>`, about 105
        characters against a 79 character cut)."""
        snapshot = live_snapshot()
        assert snapshot["measured"] is True, snapshot["reason"]
        assert snapshot["reason"] is None

    def test_a_table_without_this_process_is_unmeasured_and_names_the_pid(self) -> None:
        """The other way a snapshot fails to be a view of this machine: our own pid
        missing. Refuses in the same direction as a cut line rather than treating
        an impossible table as clean."""
        snapshot = parse_snapshot(_ps_snapshot((1, "/sbin/init")), self_check=True)
        assert snapshot["measured"] is False
        assert str(os.getpid()) in (snapshot["reason"] or "")

    def test_the_default_parse_does_not_demand_this_process(self) -> None:
        """Anti-vacuity for the keyword's default: literal fixtures must stay usable,
        or every other test in this module would have to carry the pytest process's
        own line. Reddened by turning the self-check on by default."""
        snapshot = parse_snapshot(_ps_snapshot((1, "/sbin/init")))
        assert snapshot["measured"] is True
        assert snapshot["reason"] is None

    def test_a_cut_snapshot_makes_the_boundary_refuse_with_truncation_named(self) -> None:
        """The end of the chain: an uncertifiable snapshot reaches the module
        boundary as a REFUSAL carrying its reason, not as an empty survivor list."""
        _text, cut, _width = self._cut_live_table()
        baseline = parse_snapshot(_ps_snapshot((1, "/sbin/init")))
        with pytest.raises(UnmeasurableSnapshot) as excinfo:
            find_survivors(
                baseline, parse_snapshot(cut, self_check=True), suite_port=SUITE_PORT
            )
        assert "TRUNCATED" in str(excinfo.value), str(excinfo.value)


# --------------------------------------------------------------------------- #
# Added after review: a dying process is not a leak. Regression cover for a
# false positive this guard produced against a CORRECT teardown.
# --------------------------------------------------------------------------- #


class TestConfirmSurvivorsRechecksBeforeFailing:
    """The boundary snapshot is taken the instant a module's teardown returns, and a
    correctly stopped daemon is still listed for 0.05 to 0.10 seconds after its /health
    goes silent (three measured runs). During a 2056-test smoke run this guard reported
    one such process as a leak on port 49231, and the process was gone by the time the
    run finished. A guard that fails a correct teardown is worse than no guard, so the
    report is re-checked against the same table rather than exempting anything.
    """

    def test_a_process_still_in_the_table_is_still_reported(self) -> None:
        """The positive arm, using the one process guaranteed to be present: this one.
        Reddened by a re-check that drops every survivor, which would silence the guard
        completely."""
        own_cmdline = live_snapshot()["processes"][os.getpid()]
        survivor = {"pid": os.getpid(), "port": 1, "cmdline": own_cmdline}
        assert confirm_survivors([survivor], grace=0.3) == [survivor]

    def test_a_process_that_has_left_the_table_is_dropped(self) -> None:
        """The arm that fixes the false positive: a real pid, from a real process this
        test ran and reaped, so "gone" is a fact rather than a guessed number."""
        finished = subprocess.Popen(["true"])
        finished.wait(timeout=10)
        survivor = {
            "pid": finished.pid,
            "port": 1,
            "cmdline": "/x/writ serve --port 1 --host localhost",
        }
        assert confirm_survivors([survivor], grace=0.3) == []

    def test_a_reused_pid_with_another_command_line_is_dropped(self) -> None:
        """Matching pid AND cmdline, so a pid the kernel handed to something else in the
        meantime cannot pass for the daemon that had it. Reddened by a re-check that
        compares pids alone."""
        survivor = {"pid": os.getpid(), "port": 1, "cmdline": "some other command line"}
        assert confirm_survivors([survivor], grace=0.3) == []

    def test_no_survivors_costs_no_waiting(self) -> None:
        """Anti-vacuity for the grace window: it is paid only on a hit, so a clean
        boundary (every boundary, on a healthy run) costs nothing. Reddened by a
        re-check that sleeps before looking at its input."""
        started = time.monotonic()
        assert confirm_survivors([], grace=5.0) == []
        assert time.monotonic() - started < 1.0

    def test_an_uncertifiable_recheck_drops_nothing(self, monkeypatch) -> None:
        """The one branch in `confirm_survivors` that decides what BLINDNESS means.

        Reddened by deleting the `if not recheck["measured"]` guard: the filter below
        it compares full cmdlines, and a cut table holds a truncated one, so a REAL
        survivor would be discarded for being invisible. Verified by mutation, not
        argued: with the guard removed and the same stub in place, this returns [].
        """
        survivor = {"pid": 52806, "port": 19998, "cmdline": _venv_spelling(19998)}
        monkeypatch.setattr(
            _leak_module,
            "live_snapshot",
            lambda: {
                "measured": False,
                "processes": {52806: _venv_spelling(19998)[:80]},
                "reason": "cut at 80 columns",
            },
        )
        assert confirm_survivors([survivor], grace=0.3) == [survivor]


# --------------------------------------------------------------------------- #
# Added after re-review: the pid lookup must not answer "absent" from a table
# it could not certify. Same defect class as the truncation, one level lower.
# --------------------------------------------------------------------------- #


class TestThePidLookupCannotMistakeBlindnessForAbsence:
    """`_pids_serving_port` reads through the self-checking snapshot and returns
    (measured, pids), because an earlier revision returned the list alone and threw the
    check away: on a cut table it answered "no pid for that port", which is the
    read-green-because-it-could-not-look failure one level below the guard built to stop
    it. All three consumers are pinned here, hermetically, with no daemon in play.

    The fixtures below use a REAL daemon command line cut at 80 columns, the measured
    procps width, rather than an invented short string.
    """

    PID = 52806
    PORT = 19998

    def _stub_snapshot(self, *, measured: bool, cut: bool) -> dict:
        line = _venv_spelling(self.PORT)
        return {
            "measured": measured,
            "processes": {self.PID: line[:80] if cut else line},
            "reason": None if measured else "cut at 80 columns",
        }

    def _patch_table(self, monkeypatch, *, measured: bool, cut: bool) -> None:
        monkeypatch.setattr(
            _daemon_module, "live_snapshot", lambda: self._stub_snapshot(
                measured=measured, cut=cut
            )
        )

    def test_an_unmeasured_table_reports_that_it_could_not_see(self, monkeypatch) -> None:
        """Reddened by returning the pid list alone: the caller then receives [] and has
        no way to tell it from "this port has no process"."""
        self._patch_table(monkeypatch, measured=False, cut=True)
        measured, pids = _pids_serving_port(self.PORT)
        assert measured is False
        assert pids == []

    def test_a_measured_table_reports_the_pid_it_found(self, monkeypatch) -> None:
        """Anti-vacuity for the test above: proves the lookup still WORKS, so the pair
        is a real distinction rather than a function that always says it cannot see."""
        self._patch_table(monkeypatch, measured=True, cut=False)
        assert _pids_serving_port(self.PORT) == (True, [self.PID])

    def test_a_measured_table_without_the_port_reports_a_certified_absence(
        self, monkeypatch,
    ) -> None:
        """The third of the three answers: seen, and nothing there."""
        monkeypatch.setattr(
            _daemon_module,
            "live_snapshot",
            lambda: {"measured": True, "processes": {1: "/sbin/init"}, "reason": None},
        )
        assert _pids_serving_port(self.PORT) == (True, [])

    def test_the_down_check_will_not_certify_an_unmeasured_table(self, monkeypatch) -> None:
        """The consumer where this matters most. Reddened by treating an empty pid list
        as absence regardless of `measured`: with /health already silent, an
        uncertifiable table would then make `_wait_for_isolated_down` return True and a
        LIVE daemon would be reported `stopped: True`."""
        monkeypatch.setattr(_daemon_module, "_isolated_health", lambda port, timeout=2.0: None)
        self._patch_table(monkeypatch, measured=False, cut=True)
        assert _wait_for_isolated_down(self.PORT, attempts=1) is False

    def test_the_down_check_still_certifies_a_measured_absence(self, monkeypatch) -> None:
        """Anti-vacuity for the test above, and the normal path: /health silent plus a
        table that was read and holds nothing on that port is DOWN."""
        monkeypatch.setattr(_daemon_module, "_isolated_health", lambda port, timeout=2.0: None)
        monkeypatch.setattr(
            _daemon_module,
            "live_snapshot",
            lambda: {"measured": True, "processes": {1: "/sbin/init"}, "reason": None},
        )
        assert _wait_for_isolated_down(self.PORT, attempts=1) is True

    def test_the_stop_verdict_says_when_the_table_could_not_be_read(
        self, monkeypatch, tmp_path,
    ) -> None:
        """The verdict carries the distinction instead of collapsing it: `survivors: []`
        alongside `pids_measured: False` cannot be read as "nothing survived". The
        teardown still completes: it escalates to the force kill and it still unlinks the
        socket, because a blind read is not a reason to leave a stale socket behind.

        No signal is sent: the kill is stubbed and recorded, which is also how the
        escalation is asserted.
        """
        killed: list[tuple[int, bool]] = []
        monkeypatch.setattr(
            _daemon_module,
            "_kill_writ_serve_on_port",
            lambda port, force=False: killed.append((port, force)),
        )
        monkeypatch.setattr(_daemon_module, "_isolated_health", lambda port, timeout=2.0: None)
        self._patch_table(monkeypatch, measured=False, cut=True)
        real_wait = _daemon_module._wait_for_isolated_down
        monkeypatch.setattr(
            _daemon_module,
            "_wait_for_isolated_down",
            lambda port, attempts=1: real_wait(port, attempts=1),
        )
        socket_file = tmp_path / "w.sock"
        socket_file.write_text("")

        verdict = stop_isolated_daemon(
            {"port": self.PORT, "socket_path": str(socket_file)}
        )

        assert verdict["stopped"] is False
        assert verdict["pids_measured"] is False
        assert verdict["survivors"] == []
        assert killed == [(self.PORT, False), (self.PORT, True)], killed
        assert not socket_file.exists(), (
            "the teardown skipped its socket unlink on the blind path, which is the "
            "one-defect-into-two shape stop_isolated_daemon exists to avoid"
        )

    def test_a_readable_table_leaves_the_verdict_saying_so(
        self, monkeypatch, tmp_path,
    ) -> None:
        """Anti-vacuity for the flag: it must not read False on every teardown."""
        monkeypatch.setattr(
            _daemon_module, "_kill_writ_serve_on_port", lambda port, force=False: None
        )
        monkeypatch.setattr(_daemon_module, "_isolated_health", lambda port, timeout=2.0: None)
        monkeypatch.setattr(
            _daemon_module,
            "live_snapshot",
            lambda: {"measured": True, "processes": {1: "/sbin/init"}, "reason": None},
        )
        socket_file = tmp_path / "w.sock"
        socket_file.write_text("")
        verdict = stop_isolated_daemon(
            {"port": self.PORT, "socket_path": str(socket_file)}
        )
        assert verdict["stopped"] is True
        assert verdict["pids_measured"] is True


# --------------------------------------------------------------------------- #
# Added after review: the no-daemon verdict contract. Hermetic, so it lives
# here rather than riding the module that owns a real daemon.
# --------------------------------------------------------------------------- #


class TestStopIsolatedDaemonWithNothingToStop:
    """`stop_isolated_daemon` returns a VERDICT rather than None, including on its
    two early returns, and those two paths need no daemon at all.

    They are covered here because they are the paths NO live caller reaches:
    `test_advance_phase_token_gate.py::own_daemon` skips before teardown when the
    start fails, so without these tests the new contract would be exercised by
    nothing and could change silently. This module is the cycle's own hermetic
    test file, and the plan's file list holds no other hermetic home for it.
    """

    # `pids_measured` joined the verdict when `_pids_serving_port` stopped collapsing
    # "could not read the table" into an empty list.
    KEYS = {"port", "pids", "stopped", "survivors", "health", "pids_measured"}

    @pytest.mark.parametrize("payload", [None, {}])
    def test_an_absent_daemon_returns_a_no_daemon_verdict(self, payload) -> None:
        """Reddened by restoring the old `return None`: the owning fixture reads
        `verdict["stopped"]`, and None raises a TypeError there instead of
        answering, which is the pre-cycle behaviour this replaced."""
        verdict = stop_isolated_daemon(payload)
        assert set(verdict) == self.KEYS
        assert verdict["port"] is None
        assert verdict["pids"] == []
        assert verdict["survivors"] == []
        assert verdict["health"] is None
        # Nothing was read because nothing needed reading, and that is not blindness:
        # this path never consults the process table at all.
        assert verdict["pids_measured"] is True

    def test_the_no_daemon_verdict_reads_stopped_because_nothing_runs(self) -> None:
        """`stopped: True` with `port: None` is the pair that says "nothing to stop",
        and `port` is what tells it apart from a verified teardown. Reddened by a
        verdict that reported `stopped: False` for an absent daemon, which would fail
        the owning fixture for a skip that never started anything."""
        verdict = stop_isolated_daemon(None)
        assert verdict["stopped"] is True
        assert verdict["port"] is None

    def test_a_payload_with_no_port_signals_nothing_and_unlinks_nothing(
        self, tmp_path,
    ) -> None:
        """The second early return, proven by a side effect rather than by a return
        value: a payload naming a file but no port must not reach the kill or the
        unlink, because there is no port to match a process against."""
        socket_file = tmp_path / "not-a-socket"
        socket_file.write_text("")
        verdict = stop_isolated_daemon({"socket_path": str(socket_file)})
        assert verdict["port"] is None
        assert verdict["stopped"] is True
        assert socket_file.exists(), (
            "the no-port path unlinked a file it never bound; only a teardown that "
            "identified a port may remove a socket"
        )
