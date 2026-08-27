"""Audit #5: hooks must tag their hook_execution telemetry with the real mode.

Six call sites across five hooks called `hook_timer_end ... "$SESSION_ID" ""` with an
empty 4th arg, so every hook_execution event those hooks emitted logged mode:null --
the latency/frequency telemetry could not be broken down by mode. Each hook must read
the session mode (or reuse one it already computed) and pass it.

Structural guard (per TEST-REGRESSION-001): the empty-literal 4th arg must be gone, and
each timer_end call must reference a mode variable. The hook_timer_end mechanism itself
is covered by test_friction_isolation.py::test_common_sh_hook_timer_end_honors_env.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

HOOKS_DIR = Path(__file__).resolve().parent.parent / "hooks" / "scripts"

# Hooks the audit flagged as passing an empty mode to hook_timer_end.
FLAGGED_HOOKS = [
    "writ-postcompact.sh",
    "writ-precompact.sh",
    "writ-posttool-rag.sh",
    "writ-session-end.sh",
    "writ-pre-write-dispatch.sh",
]

# A hook_timer_end call ending in an empty-string 4th positional arg, e.g.
#   hook_timer_end "$HOOK_START_NS" "name" "$SESSION_ID" ""
EMPTY_MODE_TIMER_END = re.compile(r'hook_timer_end\b[^\n]*"\$SESSION_ID"\s*""')


@pytest.mark.parametrize("hook", FLAGGED_HOOKS)
def test_no_empty_mode_timer_end(hook: str) -> None:
    src = (HOOKS_DIR / hook).read_text()
    bad = EMPTY_MODE_TIMER_END.findall(src)
    assert not bad, (
        f"{hook} still calls hook_timer_end with an empty mode arg "
        f"(logs mode:null): {bad}"
    )


@pytest.mark.parametrize("hook", FLAGGED_HOOKS)
def test_the_hook_sets_a_mode_variable_for_its_row(hook: str) -> None:
    """The property is unchanged, the mechanism is not. These hooks no longer call
    hook_timer_end: common.sh's exit trap writes the row for every hook under
    hooks/scripts/, and it reads the mode from `CURRENT_MODE` then `MODE`
    (`_writ_row_mode_cached`). So what each hook owes is an ASSIGNMENT of one of those,
    not an argument. Passing it to a call that no longer exists proved nothing.
    """
    src = (HOOKS_DIR / hook).read_text()
    assigns = [
        ln for ln in src.splitlines()
        if re.match(r'\s*(MODE|CURRENT_MODE)=', ln) and not ln.lstrip().startswith("#")
    ]
    assert assigns, (
        f"{hook} sets neither MODE nor CURRENT_MODE, so the hook_execution row common.sh "
        "writes for it will carry mode:null"
    )
