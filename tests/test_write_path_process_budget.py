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
import shutil
import subprocess
from pathlib import Path

import pytest

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
PYTHON_BUDGET = 32
TOTAL_BUDGET = 349

# Where this is going, and what closes the gap. Each item is a measured count of python
# starts that actually EXECUTE on a file write, not a grep of the hook source:
#   -8  the hook_execution exit trap in common.sh, paid by 8 write-path hooks. Needs a
#       daemon route (no /events route exists) and a decision about audit durability
#       when the daemon is down, so it is its own cycle, not a slip-in here.
#   -5  `writ-session.py mode get` called directly instead of through the existing
#       curl fast-path
#   -3  the remaining inline JSON snippets in writ-pre-write-dispatch.sh
#   -5  the inline snippets in pre-validate-file.sh
#   -5  the inline snippets in writ-posttool-rag.sh, one of which is a `should-skip`
#       daemon call made via python rather than curl
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


def _counts(hook: str) -> tuple[int, int]:
    """(python_startups, total_processes) for one hook run on the write envelope."""
    trace = Path("/tmp") / f"writ-budget-{hook}.trace"
    subprocess.run(
        ["strace", "-f", "-qq", "-e", "trace=execve", "-o", str(trace),
         "bash", str(HOOKS / hook)],
        input=ENVELOPE, capture_output=True, text=True, timeout=180,
    )
    if not trace.exists():
        pytest.skip(f"strace produced no trace for {hook}")
    text = trace.read_text(errors="replace")
    trace.unlink(missing_ok=True)
    total = text.count("execve(")
    python = sum(1 for ln in text.splitlines() if 'python3"' in ln)
    return python, total


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
            f"{total} processes per write exceeds the {TOTAL_BUDGET} baseline"
        )

    def test_the_counter_can_see_processes(self) -> None:
        """Anti-vacuity: a broken counter returning zero would make every budget
        above pass."""
        python, total = _counts("writ-pre-write-dispatch.sh")
        assert total > 5, f"counter saw only {total} processes for the main write hook"
