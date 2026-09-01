"""Hermetic tests for WRIT_DEBUG gating of the /tmp debug sinks (P1 plan).

RED PHASE: none of the four hook scripts, writ-rag-inject.sh's debug()/
WRIT_DEBUG_LOG knob, auto-approve-gate.sh's prompt log, or common.sh's shared
`_writ_debug_enabled` / `hook_log_sink` helpers are gated yet. Every hook here
is invoked via subprocess with a minimal stdin envelope; assertions on
"no write happened" will currently FAIL because the sinks are always-on today
-- that failure is the expected RED-phase outcome.

Covers:
  - WRIT_DEBUG unset -> no /tmp/writ-rag-debug.log, /tmp/writ-hook-debug.log,
    or /tmp/writ-prompt-debug.log writes.
  - WRIT_DEBUG=1 -> the sinks re-enable (writ-rag-inject debug(), the four
    `exec ... tee` hooks, auto-approve-gate's prompt log).
  - WRIT_DEBUG_LOG becomes a real env-overridable knob (no longer a hardcoded
    literal) honored by writ-rag-inject.sh.
  - WRIT_HOOK_LOG heredoc stderr sinks: /dev/null when WRIT_DEBUG unset,
    ${WRIT_HOOK_LOG:-/tmp/writ-hooks.log} when set.
  - Blackbox capture (WRIT_BLACKBOX / writ-blackbox.on) is unchanged.

Hermetic: every sink path is redirected via env (WRIT_DEBUG_LOG, WRIT_HOOK_LOG)
to tmp_path wherever the plan promises an env-overridable knob; where a path is
still hardcoded (pre-fix), the test targets the literal /tmp path but is
skip-free -- it must fail RED rather than silently pass. No live Neo4j, no
daemon (WRIT_NO_AUTOSTART=1).
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
HOOKS = REPO / "hooks" / "scripts"
COMMON_SH = REPO / "bin" / "lib" / "common.sh"

RAG_INJECT = HOOKS / "writ-rag-inject.sh"
AUTO_APPROVE_GATE = HOOKS / "auto-approve-gate.sh"
PRE_WRITE_DISPATCH = HOOKS / "writ-pre-write-dispatch.sh"
DISPATCH_DISCIPLINE = HOOKS / "writ-dispatch-discipline.sh"
SUBAGENT_START = HOOKS / "writ-subagent-start.sh"
SUBAGENT_STOP = HOOKS / "writ-subagent-stop.sh"

# MEMBERSHIP IS DERIVED; ONLY THE COUNTS ARE DATA.
#
# This was a hand-written list of six (script, count) pairs, and it was wrong in
# the way a hand-written population is always eventually wrong: it OMITTED
# writ-pre-write-dispatch.sh and writ-rag-inject.sh, both of which define the
# gated sink and route breadcrumbs through it. Nothing failed, because a hook
# missing from a list is a hook nobody checks. The omission surfaced on
# 2026-09-01 only because routing writ-pre-write-dispatch.sh's mutating update to
# the sink put it in the population that tests/test_hook_stderr_logging.py DERIVES
# from, and the two definitions of "which hooks are gated" then disagreed.
#
# So membership now comes from the tree: any hook that defines
# WRIT_HOOK_LOG_SINK from hook_log_sink is in, automatically, the day it does.
# The per-hook COUNT stays an explicit literal, because that is the tripwire this
# module exists to be: a sink appearing or vanishing must fail here and be
# reviewed, not be absorbed by a derivation. test_every_gated_hook_has_a_count
# below is what keeps the two halves honest, so a newly gated hook fails loudly
# instead of silently joining with no expectation attached.
SINK_DEFINITION = 'WRIT_HOOK_LOG_SINK="$(hook_log_sink)"'
GATED_SINK_USE = '2>>"$WRIT_HOOK_LOG_SINK"'

QUALITY_JUDGE = HOOKS / "writ-quality-judge.sh"
POSTTOOL_RAG = HOOKS / "writ-posttool-rag.sh"
VERIFY_BEFORE_CLAIM = HOOKS / "writ-verify-before-claim.sh"
VALIDATE_EXIT_PLAN = HOOKS / "validate-exit-plan.sh"
READ_RAG = HOOKS / "writ-read-rag.sh"
INJECT_TIER_WORKFLOW = HOOKS / "inject-tier-workflow.sh"


def _hooks_defining_the_gated_sink() -> list[Path]:
    return sorted(
        (path for path in HOOKS.glob("*.sh") if SINK_DEFINITION in path.read_text()),
        key=lambda path: path.name,
    )


# Expected number of $WRIT_HOOK_LOG_SINK-routed `2>>` breadcrumb sinks per hook.
# writ-read-rag.sh went 1 -> 2 and writ-pre-write-dispatch.sh 1 -> 2 on 2026-09-01,
# when their mutating `_writ_session update` calls stopped sending stderr to
# /dev/null, which tests/test_hook_stderr_logging.py forbids for exactly that call.
EXPECTED_GATED_SINKS: dict[str, int] = {
    "inject-tier-workflow.sh": 1,
    "validate-exit-plan.sh": 1,
    "writ-posttool-rag.sh": 2,
    "writ-pre-write-dispatch.sh": 2,
    "writ-quality-judge.sh": 1,
    "writ-rag-inject.sh": 8,
    "writ-read-rag.sh": 2,
    "writ-verify-before-claim.sh": 1,
}

# (script, expected number of $WRIT_HOOK_LOG_SINK-routed `2>>` breadcrumb sinks)
HOOK_LOG_SINK_HOOKS = [
    (path, EXPECTED_GATED_SINKS.get(path.name, -1))
    for path in _hooks_defining_the_gated_sink()
]


def test_the_gated_sink_population_is_not_empty():
    """Anti-vacuity. A derivation that matched nothing would collapse every
    parametrization below to zero cases and this module would read green while
    checking no hook at all."""
    assert HOOK_LOG_SINK_HOOKS, (
        "no hook defines WRIT_HOOK_LOG_SINK from hook_log_sink; either the gating "
        "was removed tree-wide or SINK_DEFINITION has drifted"
    )


def test_every_gated_hook_has_a_count():
    """The seam between the derived half and the literal half.

    A hook that starts defining the sink joins the population automatically, and
    without this it would join with no expected count and be waved through. It
    fails here instead, which is the review the count table exists to force."""
    missing = sorted(
        path.name for path, count in HOOK_LOG_SINK_HOOKS if count == -1
    )
    assert missing == [], (
        f"hook(s) define WRIT_HOOK_LOG_SINK but have no entry in "
        f"EXPECTED_GATED_SINKS: {missing}. Add the expected sink count "
        "deliberately; do not let a new gated hook join uncounted."
    )


def test_no_counted_hook_has_left_the_population():
    """The other direction: a name in EXPECTED_GATED_SINKS that no longer defines
    the sink is a stale expectation, and a stale expectation on an absent hook is
    a count pin guarding nothing."""
    derived = {path.name for path, _ in HOOK_LOG_SINK_HOOKS}
    stale = sorted(set(EXPECTED_GATED_SINKS) - derived)
    assert stale == [], (
        f"EXPECTED_GATED_SINKS names hook(s) that no longer define "
        f"WRIT_HOOK_LOG_SINK: {stale}"
    )


HARDCODED_HOOK_DEBUG_LOG = Path("/tmp/writ-hook-debug.log")
HARDCODED_PROMPT_DEBUG_LOG = Path("/tmp/writ-prompt-debug.log")
HARDCODED_RAG_DEBUG_LOG = Path("/tmp/writ-rag-debug.log")


def _minimal_envelope(**overrides) -> str:
    """A minimal Claude Code hook stdin envelope, enough for each hook's parser
    to not choke. Individual tests add the fields their hook needs."""
    base = {
        "session_id": "sid-debug-test",
        "prompt": "hello",
        "tool_name": "Write",
        "tool_input": {"file_path": "/tmp/whatever.py", "content": "x"},
        "agent_id": "",
        "agent_type": "",
    }
    base.update(overrides)
    return json.dumps(base)


def _run_hook(script: Path, env_overrides: dict, stdin: str, cwd: Path) -> subprocess.CompletedProcess:
    env = {**os.environ, "WRIT_NO_AUTOSTART": "1"}
    env.pop("WRIT_DEBUG", None)
    env.pop("WRIT_DEBUG_LOG", None)
    env.pop("WRIT_HOOK_LOG", None)
    env.pop("WRIT_BLACKBOX", None)
    env.update(env_overrides)
    return subprocess.run(
        ["bash", str(script)],
        input=stdin, capture_output=True, text=True, env=env, cwd=str(cwd), timeout=30,
    )


def _touch_clean(path: Path) -> None:
    """Ensure the hardcoded /tmp sink doesn't already exist with stale content
    from a previous manual run, so a false negative can't hide a real bug."""
    if path.exists():
        path.unlink()


@pytest.fixture(autouse=True)
def _clean_hardcoded_tmp_sinks():
    for p in (HARDCODED_HOOK_DEBUG_LOG, HARDCODED_PROMPT_DEBUG_LOG, HARDCODED_RAG_DEBUG_LOG):
        _touch_clean(p)
    yield
    for p in (HARDCODED_HOOK_DEBUG_LOG, HARDCODED_PROMPT_DEBUG_LOG, HARDCODED_RAG_DEBUG_LOG):
        _touch_clean(p)


# --- WRIT_DEBUG unset -> no writes -------------------------------------------


def test_writ_rag_inject_debug_unset_writes_no_rag_debug_log(tmp_path):
    """writ-rag-inject.sh's debug() must not write /tmp/writ-rag-debug.log
    (or any WRIT_DEBUG_LOG-redirected file) when WRIT_DEBUG is unset."""
    redirected = tmp_path / "rag-debug.log"
    proc = _run_hook(
        RAG_INJECT,
        {"WRIT_DEBUG_LOG": str(redirected)},
        _minimal_envelope(),
        tmp_path,
    )
    assert proc.returncode == 0
    assert not redirected.exists() or redirected.read_text() == ""
    assert not HARDCODED_RAG_DEBUG_LOG.exists()


def test_auto_approve_gate_debug_unset_writes_no_prompt_debug_log(tmp_path):
    proc = _run_hook(
        AUTO_APPROVE_GATE,
        {},
        _minimal_envelope(prompt="just chatting, not an approval"),
        tmp_path,
    )
    assert proc.returncode == 0
    assert not HARDCODED_PROMPT_DEBUG_LOG.exists()


@pytest.mark.parametrize("script", [
    PRE_WRITE_DISPATCH, DISPATCH_DISCIPLINE, SUBAGENT_START, SUBAGENT_STOP,
])
def test_always_on_tee_hooks_debug_unset_write_no_hook_debug_log(tmp_path, script):
    """The four `exec 2> >(tee -a /tmp/writ-hook-debug.log >&2)` hooks must not
    tee to that sink (or any redirected WRIT_HOOK_LOG file) when WRIT_DEBUG is
    unset."""
    redirected = tmp_path / "hook-debug.log"
    proc = _run_hook(script, {"WRIT_HOOK_LOG": str(redirected)}, _minimal_envelope(), tmp_path)
    assert proc.returncode == 0
    assert not redirected.exists() or redirected.read_text() == ""
    assert not HARDCODED_HOOK_DEBUG_LOG.exists()


def test_common_sh_writ_debug_enabled_helper_is_false_when_unset():
    """bin/lib/common.sh must expose a shared `_writ_debug_enabled` gate that
    fails (exit 1) when WRIT_DEBUG is unset.

    A bare `returncode != 0` would pass vacuously today because the function
    does not exist at all (bash exit 127, "command not found") -- that proves
    absence, not a correctly-false gate. Assert the function is actually
    defined FIRST (as its own check, independent of the later call), so this
    fails loudly with a clear AssertionError until the helper exists AND
    correctly evaluates to false when unset.
    """
    env = {**os.environ}
    env.pop("WRIT_DEBUG", None)

    defined = subprocess.run(
        ["bash", "-c", f'source "{COMMON_SH}" && declare -F _writ_debug_enabled'],
        capture_output=True, text=True, env=env,
    )
    assert defined.returncode == 0, "_writ_debug_enabled is not defined in common.sh"

    res = subprocess.run(
        ["bash", "-c", f'source "{COMMON_SH}" && _writ_debug_enabled'],
        capture_output=True, text=True, env=env,
    )
    assert "command not found" not in res.stderr
    assert res.returncode != 0


def test_common_sh_hook_log_sink_defaults_to_dev_null_when_debug_unset():
    """The shared `hook_log_sink` helper resolves to /dev/null (not
    /tmp/writ-hooks.log) when WRIT_DEBUG is unset, even if WRIT_HOOK_LOG is
    unset too."""
    env = {**os.environ}
    env.pop("WRIT_DEBUG", None)
    env.pop("WRIT_HOOK_LOG", None)
    res = subprocess.run(
        ["bash", "-c", f'source "{COMMON_SH}" && hook_log_sink'],
        capture_output=True, text=True, env=env,
    )
    assert res.returncode == 0, res.stderr
    assert res.stdout.strip() == "/dev/null"


# --- WRIT_DEBUG=1 -> re-enabled ----------------------------------------------


def test_writ_rag_inject_debug_enabled_writes_rag_debug_log(tmp_path):
    redirected = tmp_path / "rag-debug.log"
    proc = _run_hook(
        RAG_INJECT,
        {"WRIT_DEBUG": "1", "WRIT_DEBUG_LOG": str(redirected)},
        _minimal_envelope(),
        tmp_path,
    )
    assert proc.returncode == 0
    assert redirected.exists()
    assert redirected.read_text().strip() != ""


def test_auto_approve_gate_debug_enabled_writes_prompt_debug_log(tmp_path, monkeypatch):
    """WRIT_DEBUG=1 re-enables the prompt log. Since the plan does not promise
    an env-overridable path for THIS sink (only that it's gated), this test
    targets the documented hardcoded path directly."""
    proc = _run_hook(
        AUTO_APPROVE_GATE,
        {"WRIT_DEBUG": "1"},
        _minimal_envelope(prompt="just chatting, not an approval"),
        tmp_path,
    )
    assert proc.returncode == 0
    assert HARDCODED_PROMPT_DEBUG_LOG.exists()
    assert "sid-debug-test" in HARDCODED_PROMPT_DEBUG_LOG.read_text()


@pytest.mark.parametrize("script", [
    PRE_WRITE_DISPATCH, DISPATCH_DISCIPLINE, SUBAGENT_START, SUBAGENT_STOP,
])
def test_always_on_tee_hooks_debug_enabled_write_hook_debug_log(tmp_path, script):
    redirected = tmp_path / "hook-debug.log"
    proc = _run_hook(script, {"WRIT_DEBUG": "1", "WRIT_HOOK_LOG": str(redirected)}, _minimal_envelope(), tmp_path)
    assert proc.returncode == 0
    # A hook that hits no stderr this run wrote a zero-byte file via tee's open;
    # the assertion is on file EXISTENCE (tee opened it), matching the always-on
    # `exec 2> >(tee -a ...)` redirection semantics.
    assert redirected.exists()


def test_common_sh_writ_debug_enabled_helper_is_true_when_set():
    env = {**os.environ, "WRIT_DEBUG": "1"}
    res = subprocess.run(
        ["bash", "-c", f'source "{COMMON_SH}" && _writ_debug_enabled'],
        capture_output=True, text=True, env=env,
    )
    assert res.returncode == 0, res.stderr


def test_common_sh_hook_log_sink_resolves_to_writ_hook_log_when_debug_set(tmp_path):
    custom = tmp_path / "custom-hooks.log"
    env = {**os.environ, "WRIT_DEBUG": "1", "WRIT_HOOK_LOG": str(custom)}
    res = subprocess.run(
        ["bash", "-c", f'source "{COMMON_SH}" && hook_log_sink'],
        capture_output=True, text=True, env=env,
    )
    assert res.returncode == 0, res.stderr
    assert res.stdout.strip() == str(custom)


def test_common_sh_hook_log_sink_default_path_when_debug_set_but_no_override(tmp_path):
    env = {**os.environ, "WRIT_DEBUG": "1"}
    env.pop("WRIT_HOOK_LOG", None)
    res = subprocess.run(
        ["bash", "-c", f'source "{COMMON_SH}" && hook_log_sink'],
        capture_output=True, text=True, env=env,
    )
    assert res.returncode == 0, res.stderr
    assert res.stdout.strip() == "/tmp/writ-hooks.log"


# --- WRIT_DEBUG_LOG env-overridable knob (writ-rag-inject.sh) ---------------


def test_writ_debug_log_is_not_a_hardcoded_literal_in_source():
    """The plan requires `WRIT_DEBUG_LOG="/tmp/writ-rag-debug.log"` (hardcoded)
    to become `WRIT_DEBUG_LOG="${WRIT_DEBUG_LOG:-/tmp/writ-rag-debug.log}"`
    (a real overridable knob). Source-level pin, RED until the substitution
    lands."""
    source = RAG_INJECT.read_text()
    assert 'WRIT_DEBUG_LOG="/tmp/writ-rag-debug.log"' not in source
    assert 'WRIT_DEBUG_LOG="${WRIT_DEBUG_LOG:-' in source


def test_writ_debug_log_env_override_is_honored_end_to_end(tmp_path):
    custom = tmp_path / "my-custom-rag-debug.log"
    proc = _run_hook(
        RAG_INJECT,
        {"WRIT_DEBUG": "1", "WRIT_DEBUG_LOG": str(custom)},
        _minimal_envelope(),
        tmp_path,
    )
    assert proc.returncode == 0
    assert custom.exists()
    assert not HARDCODED_RAG_DEBUG_LOG.exists()


# --- WRIT_HOOK_LOG heredoc stderr sinks (common.sh helpers) ------------------


def test_log_rag_query_event_stderr_sink_is_dev_null_when_debug_unset(tmp_path):
    """common.sh::log_rag_query_event routes its recovery-breadcrumb stderr
    through the shared hook_log_sink gate; with WRIT_DEBUG unset it must not
    write to /tmp/writ-hooks.log."""
    env = {**os.environ}
    env.pop("WRIT_DEBUG", None)
    env.pop("WRIT_HOOK_LOG", None)
    # Malformed rule_ids JSON triggers the recovery breadcrumb write path.
    cmd = (
        f'source "{COMMON_SH}" && '
        f'log_rag_query_event "sid-x" "work" "broad" "10" "NOT-VALID-JSON"'
    )
    hooks_default = Path("/tmp/writ-hooks.log")
    if hooks_default.exists():
        hooks_default.unlink()
    res = subprocess.run(["bash", "-c", cmd], capture_output=True, text=True, env=env)
    assert res.returncode == 0, res.stderr
    assert not hooks_default.exists() or hooks_default.read_text() == ""


def test_log_rag_query_event_stderr_sink_writes_when_debug_enabled(tmp_path):
    custom = tmp_path / "hooks-debug.log"
    env = {**os.environ, "WRIT_DEBUG": "1", "WRIT_HOOK_LOG": str(custom)}
    cmd = (
        f'source "{COMMON_SH}" && '
        f'log_rag_query_event "sid-y" "work" "broad" "10" "NOT-VALID-JSON"'
    )
    res = subprocess.run(["bash", "-c", cmd], capture_output=True, text=True, env=env)
    assert res.returncode == 0, res.stderr
    assert custom.exists()
    assert "recovery" in custom.read_text()


# --- Blackbox capture unchanged ----------------------------------------------


def test_blackbox_capture_unaffected_by_writ_debug_being_unset(tmp_path):
    """WRIT_BLACKBOX=1 must still capture the raw envelope even when WRIT_DEBUG
    is unset -- the two gates are independent (blackbox was already gated)."""
    blackbox_log = tmp_path / "blackbox.jsonl"
    env = {**os.environ, "WRIT_NO_AUTOSTART": "1", "WRIT_BLACKBOX": "1",
           "WRIT_BLACKBOX_LOG": str(blackbox_log)}
    env.pop("WRIT_DEBUG", None)
    proc = subprocess.run(
        ["bash", str(RAG_INJECT)],
        input=_minimal_envelope(), capture_output=True, text=True, env=env,
        cwd=str(tmp_path), timeout=30,
    )
    assert proc.returncode == 0
    assert blackbox_log.exists()
    rows = [json.loads(ln) for ln in blackbox_log.read_text().splitlines() if ln.strip()]
    assert any(r.get("direction") == "in" for r in rows)


def test_blackbox_capture_off_by_default_regardless_of_writ_debug(tmp_path):
    """Hermetic: common.sh's blackbox gate also checks the sentinel file
    ${HOME}/.claude/writ-blackbox.on (independent of WRIT_BLACKBOX). Point
    HOME at a fresh tmp_path so this assertion is never at the mercy of an
    operator's real ~/.claude/writ-blackbox.on -- without this, the test
    would false-fail on any machine where that sentinel happens to exist,
    and false-pass-by-luck on a clean machine, neither of which reflects the
    plan's untouched code being right or wrong."""
    fake_home = tmp_path / "fake-home"
    fake_home.mkdir()
    blackbox_log = tmp_path / "blackbox-off.jsonl"
    env = {**os.environ, "WRIT_NO_AUTOSTART": "1",
           "WRIT_BLACKBOX_LOG": str(blackbox_log), "HOME": str(fake_home)}
    env.pop("WRIT_BLACKBOX", None)
    env.pop("WRIT_DEBUG", None)
    proc = subprocess.run(
        ["bash", str(RAG_INJECT)],
        input=_minimal_envelope(), capture_output=True, text=True, env=env,
        cwd=str(tmp_path), timeout=30,
    )
    assert proc.returncode == 0
    assert not blackbox_log.exists()


# --- The six remaining WRIT_HOOK_LOG heredoc sinks (gated via hook_log_sink) --
#
# Each of these six hooks had an always-on `2>>"${WRIT_HOOK_LOG:-/tmp/writ-hooks.log}"`
# breadcrumb sink that wrote to disk in production even with WRIT_DEBUG unset. They now
# follow the writ-rag-inject.sh pattern EXACTLY: source common.sh, define
# WRIT_HOOK_LOG_SINK="$(hook_log_sink)", and route every breadcrumb through
# $WRIT_HOOK_LOG_SINK (which hook_log_sink resolves to /dev/null when WRIT_DEBUG is
# unset, ${WRIT_HOOK_LOG:-/tmp/writ-hooks.log} when set).


def _sink_resolution(script: Path, env_overrides: dict) -> str:
    """Source the hook's WRIT_HOOK_LOG_SINK definition in isolation and print what
    it resolves to. Reproduces the exact two lines the hook runs after the source
    (`source common.sh` then `WRIT_HOOK_LOG_SINK="$(hook_log_sink)"`) without
    executing the hook body, so resolution is asserted independent of stdin/daemon.
    """
    env = {**os.environ}
    env.pop("WRIT_DEBUG", None)
    env.pop("WRIT_HOOK_LOG", None)
    env.update(env_overrides)
    cmd = (
        f'source "{COMMON_SH}" && '
        f'WRIT_HOOK_LOG_SINK="$(hook_log_sink)" && '
        f'printf "%s" "$WRIT_HOOK_LOG_SINK"'
    )
    res = subprocess.run(["bash", "-c", cmd], capture_output=True, text=True, env=env)
    assert res.returncode == 0, res.stderr
    return res.stdout.strip()


@pytest.mark.parametrize("script,expected_sinks", HOOK_LOG_SINK_HOOKS,
                         ids=[p.name for p, _ in HOOK_LOG_SINK_HOOKS])
def test_hook_sources_common_sh_so_hook_log_sink_is_defined(script, expected_sinks):
    """Each gated hook must source common.sh (so hook_log_sink is in scope) AND
    define WRIT_HOOK_LOG_SINK from it -- the prerequisite for the gate to work."""
    source = script.read_text()
    assert "bin/lib/common.sh" in source, f"{script.name} does not source common.sh"
    assert 'WRIT_HOOK_LOG_SINK="$(hook_log_sink)"' in source, (
        f"{script.name} does not define WRIT_HOOK_LOG_SINK from hook_log_sink"
    )


@pytest.mark.parametrize("script,expected_sinks", HOOK_LOG_SINK_HOOKS,
                         ids=[p.name for p, _ in HOOK_LOG_SINK_HOOKS])
def test_hook_has_no_raw_writ_hook_log_sink(script, expected_sinks):
    """No raw `2>>"${WRIT_HOOK_LOG:-/tmp/writ-hooks.log}"` breadcrumb sinks may
    remain: every one must route through the gated $WRIT_HOOK_LOG_SINK instead."""
    source = script.read_text()
    assert '2>>"${WRIT_HOOK_LOG' not in source, (
        f"{script.name} still has a raw, always-on 2>> WRIT_HOOK_LOG sink"
    )


@pytest.mark.parametrize("script,expected_sinks", HOOK_LOG_SINK_HOOKS,
                         ids=[p.name for p, _ in HOOK_LOG_SINK_HOOKS])
def test_hook_routes_breadcrumbs_through_gated_sink_var(script, expected_sinks):
    """Every breadcrumb `2>>` in the hook redirects through $WRIT_HOOK_LOG_SINK,
    and the expected count is present (1 for the single-sink hooks, 2 for
    writ-posttool-rag.sh)."""
    source = script.read_text()
    gated = source.count('2>>"$WRIT_HOOK_LOG_SINK"')
    assert gated == expected_sinks, (
        f"{script.name}: expected {expected_sinks} $WRIT_HOOK_LOG_SINK-routed "
        f"breadcrumb sink(s), found {gated}"
    )


@pytest.mark.parametrize("script,expected_sinks", HOOK_LOG_SINK_HOOKS,
                         ids=[p.name for p, _ in HOOK_LOG_SINK_HOOKS])
def test_hook_sink_resolves_to_dev_null_when_debug_unset(script, expected_sinks):
    """With WRIT_DEBUG unset, the hook's WRIT_HOOK_LOG_SINK resolves to /dev/null,
    so its stderr breadcrumbs never touch disk in production."""
    assert _sink_resolution(script, {}) == "/dev/null"


@pytest.mark.parametrize("script,expected_sinks", HOOK_LOG_SINK_HOOKS,
                         ids=[p.name for p, _ in HOOK_LOG_SINK_HOOKS])
def test_hook_sink_resolves_to_writ_hook_log_when_debug_set(script, expected_sinks, tmp_path):
    """With WRIT_DEBUG=1 and WRIT_HOOK_LOG set, the hook's WRIT_HOOK_LOG_SINK
    resolves to that WRIT_HOOK_LOG path (the breadcrumbs re-enable)."""
    custom = tmp_path / f"{script.stem}-hooks.log"
    resolved = _sink_resolution(script, {"WRIT_DEBUG": "1", "WRIT_HOOK_LOG": str(custom)})
    assert resolved == str(custom)


@pytest.mark.parametrize("script,expected_sinks", HOOK_LOG_SINK_HOOKS,
                         ids=[p.name for p, _ in HOOK_LOG_SINK_HOOKS])
def test_hook_sink_resolves_to_default_path_when_debug_set_no_override(script, expected_sinks):
    """With WRIT_DEBUG=1 but no WRIT_HOOK_LOG override, the sink resolves to the
    documented default /tmp/writ-hooks.log path."""
    assert _sink_resolution(script, {"WRIT_DEBUG": "1"}) == "/tmp/writ-hooks.log"


# --- writ-pre-write-dispatch.sh's second (allow-path heredoc) WRIT_HOOK_LOG sink -
#
# writ-pre-write-dispatch.sh had its top-of-file `exec 2> >(tee -a ...)` sink gated
# behind WRIT_DEBUG already, but a SECOND always-on sink survived on the hot allow-path
# heredoc (~line 251): `python3 <<'PY' 2>>"${WRIT_HOOK_LOG:-/tmp/writ-hooks.log}"`. It
# now follows the same pattern as the other gated hooks: define
# WRIT_HOOK_LOG_SINK="$(hook_log_sink)" after sourcing common.sh and route the
# heredoc's stderr through $WRIT_HOOK_LOG_SINK.


def test_pre_write_dispatch_defines_hook_log_sink_var():
    """writ-pre-write-dispatch.sh must source common.sh and define
    WRIT_HOOK_LOG_SINK from hook_log_sink (the prerequisite for gating its
    allow-path heredoc breadcrumb sink)."""
    source = PRE_WRITE_DISPATCH.read_text()
    assert "bin/lib/common.sh" in source, "does not source common.sh"
    assert 'WRIT_HOOK_LOG_SINK="$(hook_log_sink)"' in source, (
        "does not define WRIT_HOOK_LOG_SINK from hook_log_sink"
    )


def test_pre_write_dispatch_has_no_raw_writ_hook_log_sink():
    """No raw `2>>"${WRIT_HOOK_LOG:-/tmp/writ-hooks.log}"` sink may remain on the
    allow-path heredoc: it must route through the gated $WRIT_HOOK_LOG_SINK."""
    source = PRE_WRITE_DISPATCH.read_text()
    assert '2>>"${WRIT_HOOK_LOG' not in source, (
        "writ-pre-write-dispatch.sh still has a raw, always-on 2>> WRIT_HOOK_LOG sink"
    )


def test_pre_write_dispatch_routes_heredoc_breadcrumb_through_gated_sink():
    """The allow-path heredoc's stderr redirects through $WRIT_HOOK_LOG_SINK."""
    source = PRE_WRITE_DISPATCH.read_text()
    assert '2>>"$WRIT_HOOK_LOG_SINK"' in source, (
        "writ-pre-write-dispatch.sh heredoc does not route stderr through "
        "$WRIT_HOOK_LOG_SINK"
    )


def test_pre_write_dispatch_sink_resolves_to_dev_null_when_debug_unset():
    """With WRIT_DEBUG unset, writ-pre-write-dispatch.sh's WRIT_HOOK_LOG_SINK
    resolves to /dev/null, so its heredoc breadcrumbs never touch disk."""
    assert _sink_resolution(PRE_WRITE_DISPATCH, {}) == "/dev/null"
