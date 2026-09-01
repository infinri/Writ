"""A file write must not spawn processes back into the hook path.

Pins the capabilities.md section "The process budget is pinned, so spawns cannot
creep back".

Why this exists: the cost of a write is almost entirely process startup, and it
accumulated invisibly because nobody counted. Measured baseline 2026-08-07, summed
across the 15 hooks a single `Write` fires:

    total processes         349
    python interpreter      46   (24 inline `-c`, 13 stdin parses, 5 mode reads,
                                  2 friction appends, 2 other)
    git                    160   (measured cost: zero, inside the noise)
    total wall time      1,535ms

This file is the ratchet. It counts what a write actually spawns and fails if the
count climbs, so a future convenience `python3 -c` costs a red test instead of 18
silent milliseconds on every write forever.

It counts PROCESSES, not milliseconds, on purpose: process counts are stable on a
loaded machine while timings are not, which is the whole reason the timing gates
moved behind the `perf` marker.

Per TEST-TDD-001: skeletons approved before implementation.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import tempfile
from pathlib import Path

import pytest

from tests._strace import trace_execve

REPO = Path(__file__).resolve().parent.parent
HOOKS = REPO / "hooks" / "scripts"

# The hooks a single Write actually reaches, from hooks.json matchers
# (Write|Edit|NotebookEdit and Write) plus the PostToolUse chain.
WRITE_PATH_HOOKS = (
    "writ-state-write-gate.sh", "writ-pre-write-dispatch.sh", "pre-validate-file.sh",
    "validate-test-file.sh", "validate-design-doc.sh", "writ-memory-policy-guard.sh",
    "validate-file.sh", "validate-rules.sh", "writ-posttool-rag.sh",
    "writ-mark-pending-test.sh", "writ-quality-judge.sh", "writ-memory-capture.sh",
    "writ-bible-authoring-push.sh", "inject-tier-workflow.sh", "validate-handoff.sh",
)

# ENVIRONMENT NOTE, or these numbers cannot be compared with each other. The figures
# in the docstring above were measured on the real hook path with the daemon UP. This
# test measures inside pytest, where tests/conftest.py:17 points WRIT_PORT at the test
# daemon port with nothing listening, so every hook takes its daemon-down branch. That
# path is a few spawns cheaper, which is why the same write counts 33 outside pytest
# and 32 here. Every constant below is measured IN THIS ENVIRONMENT so the ratchet
# compares like with like.
BASELINE_PYTHON = 46   # before any conversion
BASELINE_TOTAL = 349

# The ratchet: measured now, so a new spawn turns this red immediately. NOT the target.
# Tightened 46 -> 32 -> 31 -> 27 -> 17 as conversions landed; lower it with the next one.
#
# The 27 -> 17 step also took TOTAL processes from 336 to 203, far more than 10 python
# starts can account for. The reason is worth keeping: 128 of those were GIT processes
# spawned as children of the per-hook `emit` call, which shells out to resolve the
# project root for log routing. Buffering the telemetry row removed the python starts
# and their git children together. It also explains an earlier measurement that made no
# sense at the time: removing git alone moved the total by nothing, because the python
# that spawned it simply did the work another way.
# The python figure is exact because it is branch-deterministic here: conftest points
# WRIT_PORT at a dead port, so every hook takes the same daemon-down path every run. The
# total carries a few processes of margin because it also counts conditional git and
# grep forks that depend on repo state.
# 17 python / 210 total, measured, WITH audit logging restored.
#
# The path here is worth recording, because the middle step looked like a regression and
# was not. pre-validate-file.sh and inject-tier-workflow.sh installed their own `trap ...
# EXIT`, which REPLACED the instrumentation trap. That lost more than two metrics rows:
# pre-validate-file.sh calls log_gate_decision, so its AUDIT records (365-day retention)
# were being discarded with the trap. Registering via writ_on_exit brought them back and
# cost a python start plus its git children, taking a write from 722ms to 925ms.
#
# Paying 203ms per write for one governance record was the wrong trade when the buffer
# already existed, so gate_decision now goes through the SAME append-and-drain path as
# hook_execution: 15 python starts, 195 processes, 16 git in production measurement,
# with every audit record intact. The exit trap spawns nothing at all now.
#
# 215 -> 200 after removing a DEAD `mktemp` from hook_instrument. It created a per-hook
# scratch buffer for log_gate_decision that stopped being read when both row kinds moved
# to the session buffer, but the call outlived its reader and kept spawning: 2 execve
# (the mktemp plus the command substitution's subshell) x 10 instrumented write-path
# hooks = 20 processes per write, in the cycle whose whole purpose was removing exactly
# those. Measured 194 after, so the old figure was 214 and this ratchet was ONE process
# from failing while the waste was invisible. Found by strace, not by reading, because
# the variable was still assigned and still cleaned up.
#
# 200 -> 180 after replacing `date +%s%N` with bash 5's EPOCHREALTIME. hook_timer_start
# and the exit trap each called it, so it ran twice on every instrumented hook: 20 of the
# 24 date processes on a write. Measured 194 -> 171 attempts, 163 -> 143 real processes.
#
# WHAT THIS NUMBER ACTUALLY COUNTS, because the name is misleading and cost us a wrong
# figure in three commit messages. It counts execve ATTEMPTS, and a binary not found in
# the first PATH entry produces one failed execve per directory tried, all inside the SAME
# forked child. Those misses are cheap syscalls, NOT processes. On this machine `git`
# resolves 8th, so 4 real git calls appeared as 32. Attempts are still the right thing to
# ratchet (they are deterministic and strictly ordered by real cost), but never quote them
# as a process count: REAL_PROCESS_BUDGET below is the honest figure.
#
# WRITE-PATH SPAWN-REDUCTION CYCLE. Two independent changes landed, and they are
# recorded as two numbers on purpose: measured together, either one would be credited
# with the other's win.
#
#   HALF A, writ-pre-write-dispatch.sh's own per-write spawns: -14 processes. Eleven of
#   them were the two positional line splits (one `head`, EIGHT `sed`, one `tail`) plus
#   the `tr` whitespace strip, all replaced by two `mapfile -t` reads and parameter
#   expansions. The other three are the bootstrap `dirname`, the black-box capture drain
#   (`blackbox_log`'s disabled arm forks a `cat` to drain the pipe it was handed, so the
#   fix is to not create the pipe), and the stderr `tee`, which forked on EVERY run and
#   only gated its destination file. Measured by binary name in the hook's own trace:
#   head 1->0, sed 8->0, tail 1->0, tr 1->0, dirname 2->1 (the survivor is
#   bin/lib/common.sh:10, out of this cycle's scope), cat 3->2, tee 1->0.
#
#   HALF B, the three unconditional `mkdir -p` calls in bin/lib/common.sh: -16
#   processes. Measured on the FOURTEEN write-path hooks half A does not touch, so the
#   two halves cannot be confused: their summed execve attempts went 111 -> 96, exactly
#   the 15 `mkdir` processes they were paying, plus 1 more in the dispatch hook.
#
# Whole path, measured outside pytest (WRIT_FRICTION_LOG unset, so the git forks for log
# routing are counted): 155 -> 125 execve attempts, 135 -> 109 real processes, 16 -> 0
# mkdir. The constants below are the IN-PYTEST figures, which are lower because
# conftest's autouse friction isolation sets WRIT_FRICTION_LOG and removes those git
# forks. Same reason the docstring's 349 does not match anything measured here. Measured
# in pytest after this cycle: 16 python, 109 attempts, 107 real, stable over three runs.
#
# THE RATCHET MUST NOT BE RAISED, so the pre-cycle values are pinned beside the new ones
# and TestBudgetConstantsOnlyRatchetDown asserts the direction.
PREVIOUS_PYTHON_BUDGET = 17
PREVIOUS_TOTAL_BUDGET = 180
PREVIOUS_REAL_PROCESS_BUDGET = 155

PYTHON_BUDGET = 16
TOTAL_BUDGET = 112
# Successful execve only: one per process actually created.
#
# CARRIES A MARGIN, and the reason is a property of the COUNTER rather than of the code.
# strace -f interleaves output from concurrent children, so a line can be split into
# `execve(... <unfinished ...>` and a later `resumed`. `_EXECVE_OK` matches `= 0` at end
# of line, so a split line is not counted, and the count therefore moves by a process or
# two between runs depending on scheduling. Observed range over seven whole-path runs
# after this cycle: 104 to 109. The attempts figure above has no such noise (125 on every
# one of those runs), which is why it is pinned tight and this one is not.
REAL_PROCESS_BUDGET = 110

# Where this is going, and what closes the gap. Each item is a measured count of python
# starts that actually EXECUTE on a file write, not a grep of the hook source:
#   -8  the hook_execution exit trap in common.sh, paid by 8 write-path hooks. Needs a
#       daemon route (no /events route exists) and a decision about audit durability
#       when the daemon is down, so it is its own cycle, not a slip-in here.
#   DONE, -4: the mode reads. is_work_mode now reads the cache FILE instead of asking
#       the daemon. The "curl fast path" it replaced was not fast: measured, a daemon
#       round trip is 13-17ms and a python start 13ms, against ~2ms for a jq file read.
#   -3  the remaining inline JSON snippets in writ-pre-write-dispatch.sh
#   -4  pre-validate-file.sh: two of these are detect_project_root, a marker walk up the
#       directory tree that bash does with zero processes, called twice per write
#   -1  writ-posttool-rag.sh's `should-skip`, a daemon call made via python not curl
# Converted so far: 15 (13 stdin parses, the write-gate parse, and posttool-rag's
# session-id read); plus the response relevance check and the additionalContext
# envelope, which fire on the RAG path rather than on every write.
# validate-rules.sh has 10 inline snippets and is deliberately NOT on this list: it
# executes ZERO of them on a file write (measured), so converting it changes the cost
# of writing a RULE file, not the number this test guards.
PYTHON_TARGET = 15

ENVELOPE = json.dumps({
    "session_id": "process-budget-probe",
    "tool_name": "Write",
    "hook_event_name": "PreToolUse",
    "tool_input": {"file_path": "/tmp/process_budget_probe.py", "content": "x = 1\n"},
})

pytestmark = pytest.mark.skipif(
    shutil.which("strace") is None, reason="strace unavailable; cannot count execve"
)


def _isolated_env() -> dict:
    """The ambient environment with HOME redirected to an empty throwaway directory.

    HOME is load-bearing for THIS measurement, not hygiene. `blackbox_log` in
    bin/lib/common.sh enables itself from the sentinel file `$HOME/.claude/writ-blackbox.on`
    and then spawns one python per hook, so with capture switched on for a real session
    every count here rises by one per write-path hook and the ratchet fails for a reason
    that has nothing to do with the write path. Measured 2026-08-28: 31 python startups
    with capture on versus the ratchet, and the diff under test had added no process at
    all. A budget whose value depends on a developer's ambient debug switch is not a
    budget, so the switch is excluded here rather than the number being raised.
    """
    env = os.environ.copy()
    env["HOME"] = tempfile.mkdtemp(prefix="writ-budget-home-")
    return env


def _counts(hook: str) -> tuple[int, int]:
    """(python_startups, total_processes) for one hook run on the write envelope."""
    text = trace_execve(
        ["bash", str(HOOKS / hook)],
        input=ENVELOPE, timeout=180, env=_isolated_env(),
    )
    total = text.count("execve(")
    python = sum(1 for ln in text.splitlines() if 'python3"' in ln)
    return python, total


_EXECVE_OK = re.compile(r"\)\s+= 0$")


def _real_processes(hook: str) -> int:
    """Successful execve only. See the note at REAL_PROCESS_BUDGET: a PATH miss is a
    failed execve inside an already-forked child, not a new process."""
    text = trace_execve(
        ["bash", str(HOOKS / hook)],
        input=ENVELOPE, timeout=180, env=_isolated_env(),
    )
    return sum(1 for ln in text.splitlines() if "execve(" in ln and _EXECVE_OK.search(ln))


@pytest.fixture(scope="module")
def real_total() -> int:
    return sum(
        _real_processes(hook) for hook in WRITE_PATH_HOOKS if (HOOKS / hook).exists()
    )


@pytest.fixture(scope="module")
def totals() -> tuple[int, int]:
    py = tot = 0
    for hook in WRITE_PATH_HOOKS:
        if not (HOOKS / hook).exists():
            continue
        p, t = _counts(hook)
        py += p
        tot += t
    return py, tot


class TestProcessBudget:
    def test_python_startups_within_budget(self, totals) -> None:
        python, _ = totals
        assert python <= PYTHON_BUDGET, (
            f"{python} python interpreter startups per write exceeds the ratchet of "
            f"{PYTHON_BUDGET} (baseline before any conversion: {BASELINE_PYTHON}). "
            f"Each one costs ~15ms on every file write. If this is a deliberate "
            f"addition, say why in the constant, do not just raise the number."
        )

    def test_python_startups_actually_improved(self, totals) -> None:
        """Guards the opposite failure: a budget that passes because the counting
        broke, or because the conversion was never applied."""
        python, _ = totals
        assert 0 < python < BASELINE_PYTHON, (
            f"expected fewer than the {BASELINE_PYTHON} baseline startups and more "
            f"than zero; counted {python}"
        )

    def test_the_ratchet_is_not_quietly_above_the_target(self, totals) -> None:
        """The ratchet records where the work stopped; the target records where it is
        going. This fails if someone relaxes the ratchet past the target instead of
        converting a call site, which is the one way a ratchet becomes decoration.
        It does NOT require the target to be met yet: PYTHON_BUDGET > PYTHON_TARGET is
        the honest mid-cycle state, and the inventory above says what closes it.
        """
        python, _ = totals
        assert python <= PYTHON_BUDGET, "covered by the budget test"
        assert PYTHON_BUDGET <= BASELINE_PYTHON, (
            f"the ratchet ({PYTHON_BUDGET}) is at or above the pre-conversion baseline "
            f"({BASELINE_PYTHON}), so it no longer ratchets anything"
        )

    def test_total_processes_within_budget(self, totals) -> None:
        _, total = totals
        assert total <= TOTAL_BUDGET, (
            f"{total} execve attempts per write exceeds the {TOTAL_BUDGET} baseline"
        )

    def test_real_process_count_within_budget(self, real_total) -> None:
        """The honest figure. See REAL_PROCESS_BUDGET: the attempts ratchet above counts
        PATH-search misses, which are syscalls rather than processes."""
        assert real_total <= REAL_PROCESS_BUDGET, (
            f"{real_total} real processes per write exceeds {REAL_PROCESS_BUDGET}"
        )

    def test_real_processes_never_exceed_attempts(self, totals, real_total) -> None:
        """The invariant that actually holds: every real process is one successful
        execve, so it is a subset of the attempts.

        This first asserted attempts > real, which passed alone and FAILED in a full
        sweep at 135 == 135. That was a bug in the test, not the code: the gap is
        PATH-search misses, so its size depends on where binaries sit on the runner's
        PATH. Under the sweep everything resolved first try and the gap was legitimately
        zero. A test that asserts a property of the machine will fail on a different
        machine and teach nothing.
        """
        _, attempts = totals
        assert 0 < real_total <= attempts, (
            f"real processes ({real_total}) must be non-zero and cannot exceed execve "
            f"attempts ({attempts}); a violation means one counter is broken"
        )

    def test_the_counter_can_see_processes(self) -> None:
        """Anti-vacuity: a broken counter returning zero would make every budget
        above pass."""
        python, total = _counts("writ-pre-write-dispatch.sh")
        assert total > 5, f"counter saw only {total} processes for the main write hook"


class TestInstrumentationSpawnsNothing:
    """hook_instrument runs on EVERY instrumented hook, so anything it spawns is
    multiplied by ten on a single file write.

    It used to call `mktemp` for a scratch buffer that nothing read any more. The
    variable was still assigned and still cleaned up at exit, so the code looked alive to
    a reader; only a syscall trace showed the process. That is the failure mode this test
    exists for, and why it asserts on execve rather than on the source text.
    """

    def _execve_lines(self, tmp_path: Path) -> list[str]:
        script = tmp_path / "instrument-probe.sh"
        script.write_text(
            "#!/usr/bin/env bash\n"
            "set -euo pipefail\n"
            f'source "{REPO / "bin" / "lib" / "common.sh"}"\n'
            'hook_instrument "spawn-probe"\n'
            "exit 0\n"
        )
        env = _isolated_env()
        env["WRIT_CACHE_DIR"] = str(tmp_path / "cache")
        env["WRIT_LOG_ROOT"] = str(tmp_path / "logs")
        env["WRIT_PORT"] = "19999"
        (tmp_path / "cache").mkdir(parents=True, exist_ok=True)
        return trace_execve(
            ["bash", str(script)], timeout=180, env=env,
        ).splitlines()

    def test_instrumenting_a_hook_spawns_no_mktemp(self, tmp_path) -> None:
        lines = self._execve_lines(tmp_path)
        offenders = [ln for ln in lines if "mktemp" in ln]
        assert offenders == [], (
            f"hook_instrument spawned mktemp again, costing 2 execve on each of the 10 "
            f"instrumented write-path hooks: {offenders}"
        )

    def test_instrumenting_a_hook_spawns_no_date(self, tmp_path) -> None:
        """hook_timer_start ran `date +%s%N`, and the exit trap ran it again, so it cost
        two processes on every instrumented hook. bash 5's EPOCHREALTIME is a variable."""
        lines = self._execve_lines(tmp_path)
        offenders = [ln for ln in lines if "/date" in ln]
        assert offenders == [], (
            f"hook_instrument spawned date again; EPOCHREALTIME makes this free: {offenders}"
        )

    def test_the_trace_is_not_empty(self, tmp_path) -> None:
        """Anti-vacuity: a trace that captured nothing would pass the assertion above
        no matter what hook_instrument does."""
        lines = self._execve_lines(tmp_path)
        assert any("execve(" in ln for ln in lines), "the tracer recorded no execve at all"

    def test_instrumenting_a_hook_spawns_no_mkdir_when_the_buffer_dir_exists(
        self, tmp_path,
    ) -> None:
        """HALF B of the spawn-reduction cycle, measured where it is actually paid.

        writ_event_buffer_append ran an unconditional `mkdir -p` before every
        append, so the exit trap of every instrumented hook bought one process to
        create a directory that already existed: 10 per file write, the largest
        remaining per-write item in the shared library. The probe creates the cache
        directory first, so a `mkdir` here means the guard is gone.

        Whether the row still lands when the directory is NOT there is a separate
        question, and it has its own test in TestBufferAppendKeepsTheRow below.
        Both are needed: a guard that saves the process by losing the row is not a
        fix, it is a quieter defect.
        """
        lines = self._execve_lines(tmp_path)
        offenders = [ln for ln in lines if "/mkdir" in ln]
        assert offenders == [], (
            f"the buffered append spawned mkdir for a directory that already exists, "
            f"costing one process on each of the 10 instrumented write-path hooks: "
            f"{offenders}"
        )


class TestBudgetConstantsOnlyRatchetDown:
    """A ratchet that can be relaxed is not a ratchet.

    Every constant in this file is a MEASURED value, so the honest response to a
    new spawn is to remove the spawn, not to raise the number. The pre-cycle
    values are pinned beside the new ones so the direction is checkable by a test
    rather than by a reviewer remembering what the number used to be.
    """

    def test_python_budget_is_not_above_the_previous_value(self) -> None:
        assert PYTHON_BUDGET <= PREVIOUS_PYTHON_BUDGET, (
            f"PYTHON_BUDGET was raised from {PREVIOUS_PYTHON_BUDGET} to "
            f"{PYTHON_BUDGET}. Remove the spawn instead."
        )

    def test_total_budget_is_not_above_the_previous_value(self) -> None:
        assert TOTAL_BUDGET <= PREVIOUS_TOTAL_BUDGET, (
            f"TOTAL_BUDGET was raised from {PREVIOUS_TOTAL_BUDGET} to {TOTAL_BUDGET}."
        )

    def test_real_process_budget_is_not_above_the_previous_value(self) -> None:
        assert REAL_PROCESS_BUDGET <= PREVIOUS_REAL_PROCESS_BUDGET, (
            f"REAL_PROCESS_BUDGET was raised from {PREVIOUS_REAL_PROCESS_BUDGET} "
            f"to {REAL_PROCESS_BUDGET}."
        )

    def test_this_cycle_lowered_all_three(self) -> None:
        """The opposite failure: constants left untouched while the work is
        described as a reduction. Each one must be STRICTLY below its predecessor,
        because the whole cycle was about removing per-write processes.
        """
        assert PYTHON_BUDGET < PREVIOUS_PYTHON_BUDGET
        assert TOTAL_BUDGET < PREVIOUS_TOTAL_BUDGET
        assert REAL_PROCESS_BUDGET < PREVIOUS_REAL_PROCESS_BUDGET

    def test_no_branch_budget_exceeds_the_whole_path_budget(self) -> None:
        """One hook cannot cost more than the whole path it belongs to. This is the
        cheap consistency check that catches a per-branch constant pasted from the
        wrong measurement.
        """
        for name, value in BRANCH_ATTEMPT_BUDGETS.items():
            assert value < TOTAL_BUDGET, (
                f"branch budget {name}={value} is not below the whole-path "
                f"TOTAL_BUDGET={TOTAL_BUDGET}"
            )


# ---------------------------------------------------------------------------
# Per-branch budgets for writ-pre-write-dispatch.sh.
#
# WHY THIS SECTION EXISTS. Everything above measures ONE branch of this hook: the
# daemon-down deny, which is the branch the TEST produces and not one real usage
# hits. conftest pins WRIT_PORT at a port with nothing listening, so every hook
# takes its daemon-down path, and for this hook that is always a deny. Meanwhile
# the branch real source-code writes DO hit -- allow, with file-context rules
# injected -- was never counted at all: measured against the live corpus, a
# trivial 6-byte `.py` stub matched 9 rules where a `.txt` matched zero, so for
# source files the injection branch is TYPICAL, not a tail.
#
# ONE NUMBER CANNOT REPRESENT THIS HOOK. Its cost is chosen by the daemon's
# answer, and the four branches below differ by nearly 2x. Averaging them would
# produce a figure no single run ever produces, so each branch gets its own
# constant and the spread is written down.
#
# EACH MEASUREMENT CARRIES A POSITIVE BRANCH PROOF, because this is the exact
# shape that has burned this repo before. A stub that 404s, or a canned body the
# hook cannot parse, sends the hook down the daemon-down fallback and yields a
# plausible count that reads green. Absence of a failure is not evidence the
# branch ran, so every branch asserts a POSITIVE signal -- in the hook's own
# stdout, and in the stub's request log -- and FAILS rather than reporting a count
# when that signal is missing. test_a_404_pre_write_check_fails_the_live_proof
# below proves the proofs actually discriminate.
#
# WHAT THESE NUMBERS ARE, so they are not misquoted: execve ATTEMPTS, the same
# quantity TOTAL_BUDGET counts. Attempts are used rather than real processes
# because they are deterministic here (identical across three consecutive runs of
# every branch) while the real-process counter moves by one or two run to run for
# the strace reason recorded at REAL_PROCESS_BUDGET. The measured real counts are
# recorded beside each constant anyway.
# ---------------------------------------------------------------------------

from tests._stub_daemon import ALWAYS_ON, PRE_WRITE_CHECK, STUB_HOST, StubDaemon  # noqa: E402
from tests._strace import trace_execve_result  # noqa: E402

DISPATCH_HOOK = "writ-pre-write-dispatch.sh"

# A port nothing listens on, CHOSEN rather than inherited. conftest pins WRIT_PORT at
# the suite's own daemon port and several modules start a real daemon there, so
# inheriting it would let this branch measure a LIVE daemon during a full-suite run and
# read green while measuring the wrong branch. Asserted closed before the measurement.
DEAD_PORT = "19998"

# Canned response bodies. Literals, so two runs of the same branch see the same input.
# Shapes taken from the real routes: writ/server/routes/gate.py's /pre-write-check
# returns decision/reason/rag_rules/rag_meta/mode (+max_denial_count on a refusal), and
# writ/server/routes/query.py's /always-on returns rules/total_tokens/cap.
STUB_MODE = "work"
STUB_DENY_REASON = "[STUB-GATE] canned denial served by tests/_stub_daemon.py"
STUB_RAG_MARKER = "STUB-RAG-RULE-MARKER"

CANNED_ALLOW_NO_RULES = {
    "decision": "allow", "reason": "", "rag_rules": "",
    "rag_meta": {"rule_ids": [], "tokens": 0}, "mode": STUB_MODE,
}
CANNED_ALLOW_WITH_RAG = {
    "decision": "allow", "reason": "",
    "rag_rules": f"[{STUB_RAG_MARKER}] WHEN: a canned trigger. RULE: a canned rule.",
    "rag_meta": {"rule_ids": ["STUB-RULE-001"], "tokens": 42}, "mode": STUB_MODE,
}
CANNED_DENY = {
    "decision": "deny", "reason": STUB_DENY_REASON, "rag_rules": "",
    "rag_meta": {"rule_ids": [], "tokens": 0}, "mode": STUB_MODE,
    "max_denial_count": 1,
}
CANNED_ALWAYS_ON_EMPTY: dict = {"rules": [], "total_tokens": 0, "cap": 5000}

# Measured 2026-09-01, three consecutive runs each, identical every time.
# Each value is the measured attempt count plus 2. The margin is for PATH-search
# misses, which are failed execve inside an already-forked child and depend on where
# binaries sit on the runner's PATH -- the same reason TOTAL_BUDGET carries one.
BRANCH_ATTEMPT_BUDGETS = {
    # measured 13 attempts / 13 real / 5 python. The local fallback runs the gate in
    # python, which is why the CHEAPEST-looking branch on a live daemon is the most
    # expensive one here.
    "daemon_down_deny": 15,
    # measured 8 attempts / 8 real / 2 python. The floor: jq parse, one dispatch
    # python, one always-on python, two curls.
    "live_allow_no_rules": 10,
    # measured 13 attempts / 11 real / 7 python. The TYPICAL branch for a source-code
    # write: it also pays the additionalContext envelope and two session updates.
    "live_allow_with_rag": 15,
    # measured 7 attempts / 7 real / 1 python. Cheapest: it returns before /always-on.
    "live_deny": 9,
}

# THE LIVE SPOT CHECK, recorded next to the hermetic figures rather than asserted equal,
# because they are NOT equal and the difference is the informative part. Run by hand
# against the operator's real daemon on 2026-09-01, three runs per branch, strace, same
# envelope shape as ENVELOPE with a throwaway session id.
#
# Each entry is (measured attempts or None, what the run showed). A None is a branch this
# check could not reproduce, with the reason, because "not measured" and "measured equal"
# are different claims and only one of them was earned here.
LIVE_SPOT_CHECK = {
    "daemon_down_deny": (
        13,
        "reproduced against the live daemon over TCP: 13 attempts / 13 real / 5 python, "
        "identical to the hermetic figure. It lands on the FALLBACK for a reason worth "
        "knowing: with WRIT_TCP_READONLY enabled, which the interactive daemon has, "
        "POST /pre-write-check returns 403 over TCP and is served only over the unix "
        "socket, so `curl -sf` fails and the local gate decides. conftest sets "
        "WRIT_PORT, so the suite always takes this path against a real daemon too.",
    ),
    "live_deny": (
        15,
        "reproduced against the live daemon over its unix socket: 15 attempts / 15 real "
        "/ 6 python, identical across three runs, decision deny. HIGHER than the "
        "hermetic 7 for one identified reason: a real denial's writ_action_push reaches "
        "POST /methodology-companion and gets a body, which costs the push-observability "
        "python plus the reason-merge python. The stub 404s that route, so the hermetic "
        "figure is the deny branch WITHOUT the methodology push. That is a limit of the "
        "stub, stated rather than averaged away.",
    ),
    "live_allow_no_rules": (
        None,
        "not reproduced live. A live allow requires a session with a MODE set, which is "
        "gate state, and this cycle does not manipulate gate state to take a "
        "measurement. The hermetic figure stands on its own positive branch proof.",
    ),
    "live_allow_with_rag": (
        None,
        "not reproduced live, same reason as live_allow_no_rules.",
    ),
}


def _branch_env(port: str) -> dict:
    """`_isolated_env` plus everything that decides WHICH branch the hook takes."""
    env = _isolated_env()
    # HERMETIC, and this is the line that makes it so. bin/lib/common.sh:1665-1679
    # prefers the unix socket when WRIT_SOCKET is set AND that socket exists, and that
    # arm wins over WRIT_PORT -- so an ambient WRIT_SOCKET would silently redirect these
    # calls to the operator's REAL daemon while the stub sat idle and the test still
    # reported a number. Popped, and the stub's request log is the positive proof that
    # the request arrived where this test thinks it did.
    env.pop("WRIT_SOCKET", None)
    env.pop("WRIT_DEBUG", None)
    # 127.0.0.1, not the `localhost` default: localhost can resolve to ::1 first and the
    # stub binds one IPv4 address, so curl would pay a failed connection attempt inside
    # a measurement whose entire point is determinism.
    env["WRIT_HOST"] = STUB_HOST
    env["WRIT_PORT"] = port
    # Fresh per run so two runs of the same branch are equally cold. A warm session
    # cache would give the second run a different amount of work to do, which is the
    # cheapest way to make a determinism check lie.
    env["WRIT_CACHE_DIR"] = tempfile.mkdtemp(prefix="writ-branch-cache-")
    env["WRIT_NO_AUTOSTART"] = "1"
    return env


def _port_is_closed(port: str) -> bool:
    import socket

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.5)
        return sock.connect_ex((STUB_HOST, int(port))) != 0


def _run_dispatch(env: dict, *, argv0: str | None = None, cwd: str | None = None):
    """Trace one run of the dispatch hook; return (CompletedProcess, trace text)."""
    return trace_execve_result(
        ["bash", argv0 or str(HOOKS / DISPATCH_HOOK)],
        input=ENVELOPE, timeout=180, env=env, cwd=cwd,
    )


def _attempts(text: str) -> int:
    return text.count("execve(")


def _reply(proc) -> dict:
    """The hook's stdout parsed as the one JSON reply it emits, or {} when silent."""
    for line in proc.stdout.splitlines():
        line = line.strip()
        if line.startswith("{"):
            return json.loads(line)
    return {}


def _run_live_branch(canned_pre_write_check, canned_always_on=CANNED_ALWAYS_ON_EMPTY):
    """Run the hook against a loopback stub. Returns (proc, trace text, stub)."""
    with StubDaemon(pre_write_check=canned_pre_write_check,
                    always_on=canned_always_on) as stub:
        proc, text = _run_dispatch(_branch_env(str(stub.port)))
    return proc, text, stub


# -- the positive branch proofs ---------------------------------------------
#
# Each raises AssertionError when its branch did NOT run, so a measurement can
# never be reported for a branch that was not exercised.

def _proof_daemon_down_deny(proc) -> None:
    hso = _reply(proc).get("hookSpecificOutput", {})
    assert hso.get("permissionDecision") == "deny", (
        f"expected the daemon-down fallback to deny; hook stdout was "
        f"{proc.stdout[:400]!r}"
    )
    assert STUB_DENY_REASON not in hso.get("permissionDecisionReason", ""), (
        "this branch is supposed to have NO listener, but the denial carries the "
        "stub's canned reason, so something answered on the port"
    )


def _proof_live_allow_no_rules(proc, stub) -> None:
    """The allow branch with nothing to inject emits nothing on stdout, so the
    branch identity comes from the STUB's request log, which is a positive signal.

    The always-on query string is the strong part: `mode=work` can only be there
    if the canned /pre-write-check response was received, split out of
    DISPATCH_BLOB, whitespace-stripped and used. A hook that fell back to the
    local gate never reaches /always-on at all.
    """
    assert stub.saw(*PRE_WRITE_CHECK), (
        f"the hook never reached the stub's /pre-write-check: {stub.describe()}"
    )
    always_on = stub.matching(*ALWAYS_ON)
    assert always_on, (
        f"the allow branch must go on to /always-on; the stub saw: {stub.describe()}"
    )
    assert always_on[0].query.get("mode") == [STUB_MODE], (
        f"expected the canned mode {STUB_MODE!r} to travel into the /always-on "
        f"query, proving the response was parsed; got {always_on[0].query!r}"
    )
    assert proc.stdout.strip() == "", (
        f"the no-rules allow branch must emit nothing: {proc.stdout[:400]!r}"
    )


def _proof_live_allow_with_rag(proc, stub) -> None:
    hso = _reply(proc).get("hookSpecificOutput", {})
    assert STUB_RAG_MARKER in hso.get("additionalContext", ""), (
        f"expected the canned rule text in additionalContext, proving the RAG "
        f"injection branch ran; hook stdout was {proc.stdout[:400]!r}"
    )
    assert "permissionDecision" not in hso, (
        "the injection reply must carry no permissionDecision, or it would touch "
        f"the write gate: {hso!r}"
    )
    assert stub.saw(*ALWAYS_ON), (
        f"the allow branch must reach /always-on: {stub.describe()}"
    )


def _proof_live_deny(proc, stub) -> None:
    hso = _reply(proc).get("hookSpecificOutput", {})
    assert hso.get("permissionDecision") == "deny", (
        f"expected the stub's canned deny to reach stdout; got {proc.stdout[:400]!r}"
    )
    assert STUB_DENY_REASON in hso.get("permissionDecisionReason", ""), (
        "the denial reason must be the stub's, or this is the local fallback "
        f"wearing the same decision: {hso!r}"
    )
    assert stub.saw(*PRE_WRITE_CHECK), stub.describe()
    assert not stub.saw(*ALWAYS_ON), (
        f"a denied write must not fetch write-time rules: {stub.describe()}"
    )


class TestDispatchBranchBudgets:
    """Four named branches, four constants, four positive proofs."""

    def test_daemon_down_deny_within_branch_budget(self) -> None:
        assert _port_is_closed(DEAD_PORT), (
            f"something is listening on {DEAD_PORT}, so this would measure a LIVE "
            f"daemon while claiming to measure the daemon-down branch"
        )
        proc, text = _run_dispatch(_branch_env(DEAD_PORT))
        _proof_daemon_down_deny(proc)
        attempts = _attempts(text)
        budget = BRANCH_ATTEMPT_BUDGETS["daemon_down_deny"]
        assert attempts <= budget, (
            f"daemon_down_deny: {attempts} execve attempts exceeds {budget}"
        )
        assert attempts > 3, f"counter saw only {attempts} attempts"

    def test_live_allow_no_rules_within_branch_budget(self) -> None:
        proc, text, stub = _run_live_branch(CANNED_ALLOW_NO_RULES)
        _proof_live_allow_no_rules(proc, stub)
        attempts = _attempts(text)
        budget = BRANCH_ATTEMPT_BUDGETS["live_allow_no_rules"]
        assert attempts <= budget, (
            f"live_allow_no_rules: {attempts} execve attempts exceeds {budget}"
        )
        assert attempts > 3, f"counter saw only {attempts} attempts"

    def test_live_allow_with_rag_within_branch_budget(self) -> None:
        proc, text, stub = _run_live_branch(CANNED_ALLOW_WITH_RAG)
        _proof_live_allow_with_rag(proc, stub)
        attempts = _attempts(text)
        budget = BRANCH_ATTEMPT_BUDGETS["live_allow_with_rag"]
        assert attempts <= budget, (
            f"live_allow_with_rag: {attempts} execve attempts exceeds {budget}"
        )
        assert attempts > 3, f"counter saw only {attempts} attempts"

    def test_live_deny_within_branch_budget(self) -> None:
        proc, text, stub = _run_live_branch(CANNED_DENY)
        _proof_live_deny(proc, stub)
        attempts = _attempts(text)
        budget = BRANCH_ATTEMPT_BUDGETS["live_deny"]
        assert attempts <= budget, (
            f"live_deny: {attempts} execve attempts exceeds {budget}"
        )
        assert attempts > 3, f"counter saw only {attempts} attempts"

    def test_a_404_pre_write_check_fails_the_live_proof(self) -> None:
        """THE TEST THAT MAKES THE OTHER FOUR MEAN SOMETHING.

        A stub that 404s sends the hook down its daemon-down fallback, which
        produces a perfectly plausible process count. Measured: the 404 run and
        the no-listener run are IDENTICAL at 13 attempts, so the count alone
        cannot tell them apart. This asserts that the live proof refuses that run
        rather than reporting its number, and then says positively what happened
        instead.
        """
        with StubDaemon(pre_write_check=404, always_on=CANNED_ALWAYS_ON_EMPTY) as stub:
            proc, _text = _run_dispatch(_branch_env(str(stub.port)))
        assert stub.saw(*PRE_WRITE_CHECK), (
            "the stub was never reached, so this test is not exercising the 404 "
            f"case it claims to: {stub.describe()}"
        )
        assert stub.matching(*PRE_WRITE_CHECK)[0].status == 404
        with pytest.raises(AssertionError):
            _proof_live_allow_with_rag(proc, stub)
        with pytest.raises(AssertionError):
            _proof_live_allow_no_rules(proc, stub)
        # And what it did instead: the local gate decided, and no write-time rules
        # were fetched.
        _proof_daemon_down_deny(proc)
        assert not stub.saw(*ALWAYS_ON), stub.describe()

    def test_the_same_branch_measures_the_same_twice(self) -> None:
        """Determinism, on the branch with the most moving parts.

        live_allow_with_rag is the one that also spawns the additionalContext
        envelope and two session updates, so if any branch were going to drift
        between runs it is this one.
        """
        first_proc, first_text, first_stub = _run_live_branch(CANNED_ALLOW_WITH_RAG)
        _proof_live_allow_with_rag(first_proc, first_stub)
        second_proc, second_text, second_stub = _run_live_branch(CANNED_ALLOW_WITH_RAG)
        _proof_live_allow_with_rag(second_proc, second_stub)
        assert _attempts(first_text) == _attempts(second_text), (
            f"two runs of the same branch counted differently: "
            f"{_attempts(first_text)} then {_attempts(second_text)}"
        )

    def test_the_branches_are_not_all_the_same_number(self) -> None:
        """Anti-vacuity for the whole section. If every branch cost the same, four
        constants would be four copies of one number and the per-branch split
        would be decoration.
        """
        assert len(set(BRANCH_ATTEMPT_BUDGETS.values())) > 1

    def test_every_branch_has_a_live_spot_check_entry(self) -> None:
        """The live figures must be recorded beside the hermetic ones, and every
        branch must be accounted for: either a measured number or a stated reason
        it was not reproduced. A branch with neither is the gap this asserts
        against, because a missing entry reads exactly like agreement.
        """
        assert set(LIVE_SPOT_CHECK) == set(BRANCH_ATTEMPT_BUDGETS)
        for name, (measured, note) in LIVE_SPOT_CHECK.items():
            assert note.strip(), f"{name}: live spot check carries no note"
            if measured is None:
                assert "not reproduced" in note, (
                    f"{name}: no live figure and no stated reason why not"
                )
            else:
                assert measured > 0, f"{name}: nonsense live figure {measured}"

    def test_the_live_check_disagrees_somewhere_and_says_so(self) -> None:
        """Anti-vacuity for the record above. If every recorded live figure equaled
        its hermetic budget, the record would be decoration and the "difference
        explained rather than asserted equal" discipline would never have been
        exercised. At least one reproduced branch must differ from the hermetic
        measurement, and its note must say why.
        """
        disagreements = [
            name for name, (measured, _note) in LIVE_SPOT_CHECK.items()
            if measured is not None and measured > BRANCH_ATTEMPT_BUDGETS[name]
        ]
        assert disagreements, (
            "no reproduced live branch differs from its hermetic budget; either the "
            "record was copied from the hermetic run or the stub is being credited "
            "with fidelity it was not measured to have"
        )


class TestDecisionOutputUnchangedByTheSpawnRemovals:
    """The removals must not have moved what the hook DECIDES or EMITS.

    This lives beside the budget rather than in a decisions file on purpose: it is
    the equivalence proof for THIS cycle's conversions, and a spawn removal whose
    equivalence proof lives somewhere else is a spawn removal nobody re-checks.
    The field-level proof (both splitters, one shaped corpus, byte for byte) is
    tests/test_pre_write_dispatch_line_split.py; this is the end-to-end version
    on the three decisions the hook can emit.

    THE DENY CASE IS COMPARED AGAINST A PRE-CHANGE CAPTURE, taken by running the
    hook before any of this cycle's edits, on the same envelope and the same
    daemon-down configuration. If the GATE's own refusal wording ever changes,
    this literal has to be re-captured: it pins the conversion, not the wording.
    """

    PRE_CHANGE_DENY_REPLY = {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": (
                "[ENF-GATE-MODE] No mode declared. Set a mode before writing code. "
                "Modes: conversation, debug, investigate, review, work."
            ),
            "additionalContext": (
                "IMPORTANT: This write was denied by a Writ gate. Do NOT attempt more "
                "writes to other files, because the denial applies to ALL files until "
                "the gate advances. Read the denial reason and follow the workflow: "
                "present your work to the user and wait for approval."
            ),
        }
    }

    CANNED_ASK = {
        "decision": "ask", "reason": "[STUB-GATE] canned escalation", "rag_rules": "",
        "rag_meta": {"rule_ids": [], "tokens": 0}, "mode": STUB_MODE,
        "max_denial_count": 3,
    }

    def test_the_daemon_down_deny_reply_matches_the_pre_change_capture(self) -> None:
        proc, _text = _run_dispatch(_branch_env(DEAD_PORT))
        assert _reply(proc) == self.PRE_CHANGE_DENY_REPLY, (
            "the deny reply changed across the spawn removals.\n"
            f"got:      {_reply(proc)!r}\n"
            f"expected: {self.PRE_CHANGE_DENY_REPLY!r}"
        )

    def test_an_ask_decision_still_emits_ask_with_its_escalation_count(self) -> None:
        """The ask arm has its own wording built in the dispatch python
        (`[Writ: repeated gate violation #N]`), and N comes from
        max_denial_count in the same response the converted splitter reads. A
        splitter that shifted a field by one would surface here as the wrong
        number or a missing reason.
        """
        proc, _text, stub = _run_live_branch(self.CANNED_ASK)
        assert stub.saw(*PRE_WRITE_CHECK), stub.describe()
        hso = _reply(proc).get("hookSpecificOutput", {})
        assert hso.get("permissionDecision") == "ask", (
            f"expected an ask decision; got {proc.stdout[:400]!r}"
        )
        reason = hso.get("permissionDecisionReason", "")
        assert "[Writ: repeated gate violation #3]" in reason, (
            f"the escalation count did not survive the split: {reason!r}"
        )
        assert "[STUB-GATE] canned escalation" in reason, (
            f"the server's reason did not survive the split: {reason!r}"
        )
        assert not stub.saw(*ALWAYS_ON), (
            f"an ask must not fetch write-time rules: {stub.describe()}"
        )

    def test_the_allow_replies_are_covered_by_the_branch_proofs(self) -> None:
        """The two allow shapes are asserted by
        TestDispatchBranchBudgets::test_live_allow_no_rules_within_branch_budget
        (silent) and ::test_live_allow_with_rag_within_branch_budget
        (additionalContext carrying the canned rule text). This is the pointer, so
        a reader looking for "where is the allow case" finds it instead of
        concluding it is missing.
        """
        assert "live_allow_no_rules" in BRANCH_ATTEMPT_BUDGETS
        assert "live_allow_with_rag" in BRANCH_ATTEMPT_BUDGETS


class TestSkillDirResolvedWithoutDirname:
    """The `dirname` removal, with its equivalence EXECUTED rather than argued.

    `SKILL_DIR` was `$(cd "$(dirname "$0")/../.." && pwd)`, one process per write
    for a value bin/lib/common.sh:10-11 computes from its own location anyway. The
    hook now sources through `"${0%/*}/../../bin/lib/common.sh"`, a parameter
    expansion that forks nothing, and takes `_WRIT_SKILL_DIR`.

    THIS VALUE IS NOT COSMETIC, which is why it gets an executed proof instead of
    a reading. It travels to the server as `skill_dir` in the /pre-write-check
    body, and the gate resolves skill-dir exemptions from it, so a difference here
    is a governance difference. The comparison reads the value the SERVER actually
    received (out of the stub's recorded request body) against the retired
    spelling run for real in bash, for both invocation forms that exist: an
    absolute path (hooks.json and every test) and a repo-relative path.
    """

    RETIRED_SPELLING = 'cd "$(dirname "$1")/../.." && pwd'

    def _retired_value(self, argv0: str, cwd: str | None = None) -> str:
        import subprocess

        proc = subprocess.run(
            ["bash", "-c", self.RETIRED_SPELLING, "skill-dir-probe", argv0],
            capture_output=True, text=True, timeout=30, cwd=cwd,
        )
        assert proc.returncode == 0, proc.stderr
        return proc.stdout.strip()

    def _skill_dir_the_server_received(self, argv0: str, cwd: str | None = None) -> str:
        with StubDaemon(pre_write_check=CANNED_ALLOW_NO_RULES,
                        always_on=CANNED_ALWAYS_ON_EMPTY) as stub:
            _proc, _text = _run_dispatch(
                _branch_env(str(stub.port)), argv0=argv0, cwd=cwd,
            )
        requests = stub.matching(*PRE_WRITE_CHECK)
        assert requests, (
            f"the hook never POSTed a check body, so there is no skill_dir to "
            f"compare: {stub.describe()}"
        )
        body = requests[0].json_body
        assert isinstance(body, dict), f"check body was not a JSON object: {body!r}"
        return body.get("skill_dir", "")

    def test_absolute_invocation_sends_the_same_skill_dir_as_the_retired_spelling(
        self,
    ) -> None:
        argv0 = str(HOOKS / DISPATCH_HOOK)
        expected = self._retired_value(argv0)
        assert expected, "the retired spelling produced nothing; the probe is broken"
        assert self._skill_dir_the_server_received(argv0) == expected

    def test_relative_invocation_sends_the_same_skill_dir_as_the_retired_spelling(
        self,
    ) -> None:
        """`${0%/*}` on a relative `$0` yields a relative directory, and the source
        that follows is resolved against the cwd. common.sh then turns it absolute
        with its own cd/pwd, so the value must still match. Asserted, not assumed.
        """
        argv0 = f"hooks/scripts/{DISPATCH_HOOK}"
        expected = self._retired_value(argv0, cwd=str(REPO))
        assert expected, "the retired spelling produced nothing; the probe is broken"
        got = self._skill_dir_the_server_received(argv0, cwd=str(REPO))
        assert got == expected, f"relative invocation: got {got!r}, expected {expected!r}"

    def test_the_skill_dir_is_absolute_and_holds_the_library_it_came_from(self) -> None:
        """Anti-vacuity: two empty strings compare equal. The value has to be a
        real absolute path to the checkout, not just consistent.
        """
        argv0 = str(HOOKS / DISPATCH_HOOK)
        got = self._skill_dir_the_server_received(argv0)
        assert got.startswith("/"), f"skill_dir is not absolute: {got!r}"
        assert (Path(got) / "bin" / "lib" / "common.sh").is_file(), (
            f"skill_dir {got!r} does not contain bin/lib/common.sh"
        )

    def test_the_hook_spawns_no_dirname_of_its_own(self) -> None:
        """One `dirname` survives in the trace and it is NOT this hook's:
        bin/lib/common.sh:10 resolves its own directory that way, on every hook
        that sources it, and changing the shared library's bootstrap is outside
        this cycle's scope. So the assertion is on the COUNT dropping from 2 to 1,
        which is a claim about this hook, rather than on the absence of the name,
        which would be a claim about a file this cycle did not touch.
        """
        _proc, text = _run_dispatch(_branch_env(DEAD_PORT))
        dirnames = [ln for ln in text.splitlines() if re.search(r'execve\("[^"]*/dirname"', ln)]
        assert len(dirnames) == 1, (
            f"expected exactly one dirname (bin/lib/common.sh:10) and found "
            f"{len(dirnames)}: {dirnames}"
        )


class TestBufferAppendKeepsTheRow:
    """HALF B's other half: the guard must not buy the process by losing the row.

    Before this cycle, `mkdir -p` ran unconditionally before every buffer append.
    That was waste in the normal case AND load-bearing in one case: a cache
    directory removed mid-session was silently recreated and the row landed. A
    bare `[ -d ] || mkdir -p` would skip the create, the append would fail, and
    the `|| true` beside it would swallow a telemetry or audit row in silence.
    So the shape is guard-plus-create-and-retry, and this proves the retry by
    deleting the directory between two appends.
    """

    SESSION = "buffer-guard-probe"

    def _probe(self, tmp_path: Path, *, remove_between: bool) -> tuple[list[str], Path]:
        cache = tmp_path / "cache"
        cache.mkdir(parents=True, exist_ok=True)
        script = tmp_path / "buffer-probe.sh"
        removal = f'rm -rf "{cache}"\n' if remove_between else ""
        script.write_text(
            "#!/usr/bin/env bash\n"
            "set -euo pipefail\n"
            f'source "{REPO / "bin" / "lib" / "common.sh"}"\n'
            f'writ_event_buffer_append "{self.SESSION}" "probe-hook-a" 1 0 work\n'
            f"{removal}"
            f'writ_event_buffer_append "{self.SESSION}" "probe-hook-b" 2 0 work\n'
            "exit 0\n"
        )
        env = _isolated_env()
        env["WRIT_CACHE_DIR"] = str(cache)
        env["WRIT_LOG_ROOT"] = str(tmp_path / "logs")
        env["WRIT_PORT"] = DEAD_PORT
        lines = trace_execve(["bash", str(script)], timeout=180, env=env).splitlines()
        return lines, cache / f"writ-events-{self.SESSION}.buf"

    def test_no_mkdir_when_the_directory_exists_throughout(self, tmp_path) -> None:
        lines, buf = self._probe(tmp_path, remove_between=False)
        offenders = [ln for ln in lines if "/mkdir" in ln]
        assert offenders == [], f"mkdir spawned for an existing directory: {offenders}"
        assert b"probe-hook-a" in buf.read_bytes()
        assert b"probe-hook-b" in buf.read_bytes()

    def test_the_row_still_lands_when_the_directory_is_removed_between_appends(
        self, tmp_path,
    ) -> None:
        lines, buf = self._probe(tmp_path, remove_between=True)
        assert buf.is_file(), (
            "the buffer directory was removed between two appends and the second "
            "row was LOST: the guard traded a telemetry row for a process"
        )
        assert b"probe-hook-b" in buf.read_bytes(), (
            f"buffer exists but does not hold the post-removal row: {buf.read_bytes()!r}"
        )
        # And it cost exactly the one mkdir it needed, on the retry.
        offenders = [ln for ln in lines if "/mkdir" in ln]
        assert len(offenders) == 1, (
            f"expected exactly one mkdir (the create-and-retry) and found "
            f"{len(offenders)}: {offenders}"
        )

    def test_the_probe_trace_is_not_empty(self, tmp_path) -> None:
        """Anti-vacuity: an empty trace makes every `mkdir` assertion above pass."""
        lines, _buf = self._probe(tmp_path, remove_between=False)
        assert any("execve(" in ln for ln in lines), "the tracer recorded no execve"

    def test_the_gate_decision_buffer_spawns_no_mkdir_when_the_directory_exists(
        self, tmp_path,
    ) -> None:
        """The third guarded call site. `log_gate_decision` buffers an `allow`, and
        that path is guarded but deliberately NOT retried: a failed append there
        falls through to `_gd_emit_now`, which writes the audit record
        synchronously, so retrying the buffer would take the row OFF the durable
        path to save a spawn.
        """
        cache = tmp_path / "cache"
        cache.mkdir(parents=True, exist_ok=True)
        script = tmp_path / "gate-decision-probe.sh"
        script.write_text(
            "#!/usr/bin/env bash\n"
            "set -euo pipefail\n"
            f'source "{REPO / "bin" / "lib" / "common.sh"}"\n'
            f'SESSION_ID="{self.SESSION}"\n'
            'log_gate_decision "probe-gate" "allow" "canned reason" "/tmp/probe.py"\n'
            "exit 0\n"
        )
        env = _isolated_env()
        env["WRIT_CACHE_DIR"] = str(cache)
        env["WRIT_LOG_ROOT"] = str(tmp_path / "logs")
        env["WRIT_PORT"] = DEAD_PORT
        lines = trace_execve(["bash", str(script)], timeout=180, env=env).splitlines()
        offenders = [ln for ln in lines if "/mkdir" in ln]
        assert offenders == [], f"mkdir spawned for an existing directory: {offenders}"
        buf = cache / f"writ-events-{self.SESSION}.buf"
        assert buf.is_file() and b"probe-gate" in buf.read_bytes(), (
            "the gate_decision row did not reach the buffer, so this test proved "
            "nothing about the guard"
        )
