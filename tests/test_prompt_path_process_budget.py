"""The per-prompt path had no budget guard, so its cost was free to drift.

WHY THIS EXISTS SEPARATELY FROM THE WRITE PATH. The two paths fail differently. A file
write fires 15 hooks that are individually cheap, so its cost is process COUNT. A user
prompt fires 3 hooks, and its cost is python INTERPRETER STARTS: measured 2026-08-07,
writ-rag-inject.sh spent ~145ms of its 247ms on 8 of them, while all 5 of its daemon round
trips together came to ~62ms. Optimising the round trips first was the intuitive move and
would have bought ~30ms while leaving the real cost untouched.

THE COST MODEL THE BUDGETS BELOW ARE DERIVED FROM (measured, same machine, median of 10):
    bare `python3 -c pass`      9.5 ms      `import json`             +4.9 ms
    `python3 -S -c pass`        6.7 ms      `import writ.shared.logging` +12.5 ms
    `jq -n 1`                   2.3 ms      `import urllib.request`   +22.0 ms
So converting one call from python to jq saves ~7ms before its imports, and any hot-path
module that reaches urllib pays more for the import than for the work.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
HOOKS = REPO / "hooks" / "scripts"

PROMPT_PATH_HOOKS = [
    "writ-manual-test-grant.sh",
    "auto-approve-gate.sh",
    "writ-rag-inject.sh",
]

# Measured 11 on this envelope after converting the escalation check to json_transform.
#
# I first set this to 10 from a hand-rolled probe that measured 9, and the test failed at
# 11. The probe and the test send different session ids, which takes different branches
# (recall and escalation fire for one and not the other), so the probe was measuring one
# path and calling it the cost. The number here comes from the test's own envelope, which
# is the one that will keep running.
PYTHON_BUDGET = 11
# Baseline before this cycle touched the prompt path, so the ratchet demonstrably ratchets.
BASELINE_PYTHON = 13
#
# MEASURED BEFORE/AFTER for the friction change, clean cache, identical envelope,
# writ-rag-inject.sh alone: python 6 -> 5, git 32 -> 0, execve 71 -> 40. The git figure is
# the interesting one: friction-append's `emit` shelled out to resolve the log root, the
# same pattern that dominated the write path, so removing the spawn took its children too.
#
# I briefly set this ratchet to 10 on the strength of a warm-session probe that showed
# 4 -> 3. The clean-cache path is different and only reached 11, so 10 would have been a
# number I wanted rather than one I measured. Lower it when the next conversion earns it.

# Inline `python3 -c` sites that still parse JSON in writ-rag-inject.sh. Each is ~14ms
# (9.5 interpreter + 4.9 for `import json`) to answer one question jq answers in 2.3.
# Converting each needs its own three-way parity check (jq arm, python arm, and the
# original) across null / false / "" / 0 / [] / malformed, because jq and python disagree
# on truthiness and that disagreement has already caused one gate to skip its check.
REMAINING_INLINE_JSON = 5

ENVELOPE = json.dumps({
    "session_id": "prompt-path-budget-probe",
    "hook_event_name": "UserPromptSubmit",
    "prompt": "add retry logic and error handling to the payment client",
})

pytestmark = pytest.mark.skipif(
    shutil.which("strace") is None, reason="strace unavailable; cannot count execve"
)


def _python_starts(hook: str, cache_dir: Path) -> int:
    trace = Path("/tmp") / f"writ-prompt-budget-{hook}.trace"
    # ISOLATED CACHE, and this is load-bearing rather than tidiness. Without it the hooks
    # read and write the real session cache, so the count depends on state left by earlier
    # runs (a session that has already tripped should-skip takes a shorter path and starts
    # fewer interpreters). The first version of this file omitted it and measured a
    # different number than a standalone probe, which is what exposed it.
    env = {**os.environ, "WRIT_CACHE_DIR": str(cache_dir),
           "WRIT_LOG_ROOT": str(cache_dir / "logs")}
    subprocess.run(
        ["strace", "-f", "-qq", "-e", "trace=execve", "-o", str(trace),
         "bash", str(HOOKS / hook)],
        input=ENVELOPE, capture_output=True, text=True, timeout=180, env=env,
    )
    if not trace.exists():
        pytest.skip(f"strace produced no trace for {hook}")
    text = trace.read_text(errors="replace")
    trace.unlink(missing_ok=True)
    return sum(1 for ln in text.splitlines() if 'python3"' in ln)


@pytest.fixture(scope="module")
def python_total(tmp_path_factory) -> int:
    cache = tmp_path_factory.mktemp("prompt-budget-cache")
    return sum(
        _python_starts(hook, cache) for hook in PROMPT_PATH_HOOKS
        if (HOOKS / hook).exists()
    )


class TestPromptPathBudget:
    def test_python_startups_within_budget(self, python_total: int) -> None:
        assert python_total <= PYTHON_BUDGET, (
            f"{python_total} python interpreter startups per prompt exceeds the ratchet "
            f"of {PYTHON_BUDGET}. Each costs ~9.5ms before it does any work, and ~14ms "
            f"once it imports json. If this is deliberate, say why in the constant."
        )

    def test_the_ratchet_still_ratchets(self, python_total: int) -> None:
        """Anti-vacuity: a budget at or above the starting point guards nothing."""
        assert PYTHON_BUDGET < BASELINE_PYTHON, (
            f"ratchet {PYTHON_BUDGET} is not below the {BASELINE_PYTHON} baseline"
        )

    def test_the_counter_can_see_python(self, tmp_path) -> None:
        """Anti-vacuity: a counter stuck at zero would satisfy every budget above."""
        assert _python_starts("writ-rag-inject.sh", tmp_path) > 0, (
            "counted zero python starts for the RAG hook, which certainly starts python"
        )

    def test_the_count_is_stable_across_runs(self, tmp_path) -> None:
        """A ratchet that drifts is a ratchet nobody trusts.

        Two runs against SEPARATE clean caches must agree. Against a shared cache they do
        not: the second run sees state the first left behind and can take a shorter path.
        """
        a = _python_starts("writ-rag-inject.sh", tmp_path / "a")
        b = _python_starts("writ-rag-inject.sh", tmp_path / "b")
        assert a == b, (
            f"python starts varied between identical clean-cache runs ({a} vs {b}); the "
            f"measurement depends on leftover state and cannot anchor a budget"
        )


class TestEscalationCheckIsNotInlinePython:
    """The converted call, pinned. It read the escalation response through
    `python3 -c 'import sys,json; ...'`, which is 13.7ms measured to answer one boolean."""

    def test_the_escalation_check_no_longer_starts_python(self) -> None:
        """The specific line that was converted, pinned so it cannot quietly revert."""
        source = (HOOKS / "writ-rag-inject.sh").read_text()
        reverted = [
            ln.strip() for ln in source.splitlines()
            if "python3 -c" in ln and "needed" in ln
        ]
        assert reverted == [], (
            f"the escalation check is back to inline python: {reverted}"
        )

    def test_escalation_uses_json_transform(self) -> None:
        """Positive half: asserting the old form is gone proves nothing if the new form
        is not there either (deleting the line would pass the test above)."""
        source = (HOOKS / "writ-rag-inject.sh").read_text()
        assert "json_transform" in source and "ESC_NEEDED" in source, (
            "the escalation check no longer routes through json_transform"
        )

    def test_inline_json_python_does_not_grow(self) -> None:
        """A ratchet, not a clean-slate assertion.

        This started life asserting zero, which failed immediately and usefully: FIVE more
        inline sites remain that I had not converted. Asserting an aspiration would have
        meant either a permanently red test or deleting the check. Counting instead keeps
        the pressure on without lying about today.
        """
        source = (HOOKS / "writ-rag-inject.sh").read_text()
        offenders = [
            ln.strip()[:70] for ln in source.splitlines()
            if "python3 -c" in ln and "json" in ln
        ]
        assert len(offenders) <= REMAINING_INLINE_JSON, (
            f"inline `python3 -c` JSON parsing grew to {len(offenders)}, above the "
            f"{REMAINING_INLINE_JSON} known sites. Use json_transform: {offenders}"
        )
