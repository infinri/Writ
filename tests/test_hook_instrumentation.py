"""Every wired hook records that it ran, on every exit path, without changing behavior.

RED PHASE: `hook_instrument` and `log_gate_decision` do not exist in
bin/lib/common.sh yet, and no hook calls them. The coverage test and the trap
tests fail until they do.

The audit found 18 of 37 wired hooks emitting nothing at all -- six of them
gates that decide allow/deny. This file pins both halves of the fix:

  1. hook_instrument installs ONE EXIT trap per hook rather than an edit at each
     of the 96 `exit` statements, so the paths a per-exit edit cannot reach
     (`set -e` aborts, command-not-found crashes) are covered too.
  2. The trap must be behavior-neutral. Two contracts carry real risk:
     exit codes ARE the gate mechanism (Claude Code reads 2 as deny), and stdout
     IS the injection channel. Tests assert both survive byte-for-byte.

Hermetic: WRIT_LOG_ROOT and WRIT_CACHE_DIR are redirected per test; hooks are
pointed at a dead WRIT_PORT so none reach the live daemon.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
COMMON_SH = REPO / "bin" / "lib" / "common.sh"
HOOKS_JSON = REPO / "hooks" / "hooks.json"
HOOKS_DIR = REPO / "hooks" / "scripts"

# The six gates and five validators that decide allow/deny. These must emit a
# decision on BOTH branches: a gate ALLOW is already silent by design (deny-only
# JSON back to Claude Code), so deny-only logging would leave "was this gate
# even active" unanswerable -- the exact blind spot the audit found.
DECIDING_HOOKS = [
    "writ-bash-write-gate.sh",
    "writ-comms-output-gate.sh",
    "writ-debug-code-gate.sh",
    "writ-verify-before-claim.sh",
    "writ-dispatch-discipline.sh",
    "writ-worktree-safety.sh",
    "validate-file.sh",
    "pre-validate-file.sh",
    "validate-test-file.sh",
    "validate-design-doc.sh",
    "validate-handoff.sh",
]

# Instrumented for timing only: they inject or capture, they do not decide.
TIMING_ONLY_HOOKS = [
    "writ-read-rag.sh",
    "inject-tier-workflow.sh",
    "session-start-bootstrap.sh",
    "writ-bible-authoring-push.sh",
    "writ-blackbox-capture.sh",
    "writ-quality-judge.sh",
    "writ-web-capture.sh",
]


def _wired_hooks() -> set[str]:
    """Hook script basenames actually registered in hooks.json."""
    return set(re.findall(r"[a-z0-9-]+\.sh", HOOKS_JSON.read_text()))


def _run_snippet(body: str, tmp_path: Path, argv: list[str] | None = None):
    """Source common.sh in a throwaway script and run `body` under `set -euo pipefail`.

    Exercises hook_instrument the way a real hook uses it, without coupling the
    trap tests to any one hook's control flow.
    """
    script = tmp_path / "probe.sh"
    script.write_text(textwrap.dedent(f"""\
        #!/usr/bin/env bash
        set -euo pipefail
        source "{COMMON_SH}"
        {body}
    """))
    env = os.environ.copy()
    env["WRIT_LOG_ROOT"] = str(tmp_path / "logs")
    env["WRIT_LOG_PROJECT"] = "hookproj"
    env["WRIT_CACHE_DIR"] = str(tmp_path / "cache")
    env["WRIT_PORT"] = "19999"  # nothing listens; hooks must fall back, not hang
    env.pop("WRIT_FRICTION_LOG", None)
    (tmp_path / "cache").mkdir(parents=True, exist_ok=True)
    return subprocess.run(
        ["bash", str(script), *(argv or [])],
        capture_output=True, text=True, env=env, timeout=20,
    )


def _drain(tmp_path: Path) -> None:
    """Flush any buffered hook_execution rows into the log streams.

    hook_execution is APPENDED by bash at hook exit and drained once per turn, instead
    of costing a python interpreter start in every hook (8 per file write, ~96ms). The
    row still reaches the same stream with the same fields; it arrives at the drain
    rather than at the exit. Every assertion below is unchanged, so what these tests
    check is unchanged: this only moves the read to after the point where the row lands.
    """
    cache = tmp_path / "cache"
    if not cache.is_dir():
        return
    flush = Path(__file__).resolve().parent.parent / "bin" / "lib" / "writ-flush-events.py"
    env = os.environ.copy()
    env["WRIT_CACHE_DIR"] = str(cache)
    env["WRIT_LOG_ROOT"] = str(tmp_path / "logs")
    env["WRIT_LOG_PROJECT"] = "hookproj"
    # Mirror _run_snippet: conftest's autouse fixture sets WRIT_FRICTION_LOG, and emit
    # honours it over the stream router. Inheriting it sent every drained row to that
    # file instead of metrics.jsonl, so the rows existed and the assertions still read
    # an empty stream. The drain must run in the same environment as the hook did.
    env.pop("WRIT_FRICTION_LOG", None)
    for buf in cache.glob("writ-events-*.buf"):
        session = buf.name[len("writ-events-"):-len(".buf")]
        subprocess.run([sys.executable, str(flush), session],
                       capture_output=True, text=True, env=env, timeout=30)


def _rows(tmp_path: Path, stream: str) -> list[dict]:
    _drain(tmp_path)
    path = tmp_path / "logs" / "hookproj" / f"{stream}.jsonl"
    if not path.is_file():
        return []
    return [json.loads(ln) for ln in path.read_text().splitlines() if ln.strip()]


def _exec_rows(tmp_path: Path) -> list[dict]:
    return [r for r in _rows(tmp_path, "metrics") if r.get("event") == "hook_execution"]


# --- the trap fires on every exit shape -------------------------------------
# A per-`exit` edit would cover the first two and miss the rest. That is the
# whole argument for the trap, so it is what the tests check hardest.


def test_trap_emits_on_early_exit_zero(tmp_path):
    r = _run_snippet('hook_instrument "probe-early"\nexit 0', tmp_path)
    assert r.returncode == 0
    assert len(_exec_rows(tmp_path)) == 1


def test_trap_emits_on_gate_deny_exit_two(tmp_path):
    r = _run_snippet('hook_instrument "probe-deny"\nexit 2', tmp_path)
    assert r.returncode == 2
    assert len(_exec_rows(tmp_path)) == 1


def test_trap_emits_on_set_e_abort(tmp_path):
    """`set -e` kills the hook without reaching any `exit` line."""
    r = _run_snippet('hook_instrument "probe-abort"\nfalse\necho unreachable', tmp_path)
    assert r.returncode == 1
    assert len(_exec_rows(tmp_path)) == 1


def test_trap_emits_on_command_not_found_crash(tmp_path):
    """The sub-agent start-hook crash class: died silently, left no trace."""
    r = _run_snippet('hook_instrument "probe-crash"\nnosuchcommand_xyz', tmp_path)
    assert r.returncode == 127
    assert len(_exec_rows(tmp_path)) == 1


def test_trap_emits_on_fallthrough_to_end(tmp_path):
    r = _run_snippet('hook_instrument "probe-fall"\necho done >/dev/null', tmp_path)
    assert r.returncode == 0
    assert len(_exec_rows(tmp_path)) == 1


def test_trap_emits_exactly_once_despite_many_exit_paths(tmp_path):
    """writ-read-rag.sh has 10 `exit` statements; one run must yield one row."""
    body = (
        'hook_instrument "probe-many"\n'
        'if [ "${1:-}" = "a" ]; then exit 0; fi\n'
        'if [ "${1:-}" = "b" ]; then exit 0; fi\n'
        'exit 0'
    )
    _run_snippet(body, tmp_path, argv=["b"])
    assert len(_exec_rows(tmp_path)) == 1


# --- payload -----------------------------------------------------------------


def test_row_carries_hook_name_and_duration(tmp_path):
    _run_snippet('hook_instrument "probe-payload"\nexit 0', tmp_path)
    row = _exec_rows(tmp_path)[0]
    assert row["hook_name"] == "probe-payload"
    assert isinstance(row["duration_ms"], int)
    assert row["duration_ms"] >= 0


def test_row_carries_the_real_exit_code(tmp_path):
    """exit_code is the new field: without it, "which hooks are quietly failing"
    stays unanswerable even once every hook emits timing."""
    _run_snippet('hook_instrument "probe-code"\nexit 2', tmp_path)
    assert _exec_rows(tmp_path)[0]["exit_code"] == 2


def test_row_exit_code_distinguishes_crash_from_clean_exit(tmp_path):
    _run_snippet('hook_instrument "probe-127"\nnosuchcommand_xyz', tmp_path)
    assert _exec_rows(tmp_path)[0]["exit_code"] == 127


def test_hook_execution_routes_to_the_metrics_stream(tmp_path):
    _run_snippet('hook_instrument "probe-stream"\nexit 0', tmp_path)
    assert _exec_rows(tmp_path)
    assert not [r for r in _rows(tmp_path, "audit") if r.get("event") == "hook_execution"]


# --- the two contracts that must not break -----------------------------------


@pytest.mark.parametrize("code", [0, 1, 2, 42])
def test_exit_code_reaches_the_caller_unchanged(tmp_path, code):
    """Exit codes ARE the gate mechanism; Claude Code reads 2 as deny."""
    r = _run_snippet(f'hook_instrument "probe-rc"\nexit {code}', tmp_path)
    assert r.returncode == code


def test_stdout_is_byte_identical_with_the_trap_installed(tmp_path):
    """stdout IS the injection channel for hooks like writ-read-rag.sh."""
    payload = '{"additionalContext":"INJECTED"}'
    with_trap = _run_snippet(
        f'hook_instrument "probe-stdout"\nprintf \'%s\' \'{payload}\'\nexit 0', tmp_path
    )
    without = _run_snippet(f'printf \'%s\' \'{payload}\'\nexit 0', tmp_path)
    assert with_trap.stdout == without.stdout == payload


def test_hook_stdout_remains_parseable_json(tmp_path):
    payload = '{"additionalContext":"INJECTED"}'
    r = _run_snippet(
        f'hook_instrument "probe-json"\nprintf \'%s\' \'{payload}\'\nexit 0', tmp_path
    )
    assert json.loads(r.stdout) == {"additionalContext": "INJECTED"}


def test_instrumentation_writes_nothing_to_stdout(tmp_path):
    """Guarded against a vacuous pass: a script that dies at `hook_instrument:
    command not found` also has empty stdout, so assert the run SUCCEEDED and the
    row was actually written before concluding stdout stayed clean."""
    r = _run_snippet('hook_instrument "probe-quiet"\nexit 0', tmp_path)
    assert r.returncode == 0, r.stderr
    assert len(_exec_rows(tmp_path)) == 1, "no row written; stdout check would be vacuous"
    assert r.stdout == ""


def test_instrumentation_failure_never_changes_the_exit_code(tmp_path):
    """An unwritable log root must not turn a gate ALLOW into a failure."""
    dud = tmp_path / "not-a-dir"
    dud.write_text("i am a file")
    script = tmp_path / "probe2.sh"
    script.write_text(textwrap.dedent(f"""\
        #!/usr/bin/env bash
        set -euo pipefail
        source "{COMMON_SH}"
        hook_instrument "probe-unwritable"
        exit 2
    """))
    env = os.environ.copy()
    env["WRIT_LOG_ROOT"] = str(dud)
    env["WRIT_PORT"] = "19999"
    r = subprocess.run(["bash", str(script)], capture_output=True, text=True,
                       env=env, timeout=20)
    assert r.returncode == 2


# --- gate decision records ---------------------------------------------------


def test_log_gate_decision_emits_a_row(tmp_path):
    _run_snippet(
        'hook_instrument "probe-gate"\n'
        'log_gate_decision "phase-a" "deny" "no plan approved" "src/foo.py"\n'
        'exit 2',
        tmp_path,
    )
    rows = [r for r in _rows(tmp_path, "audit") if r.get("event") == "gate_decision"]
    assert len(rows) == 1


def test_gate_decision_row_carries_gate_decision_reason_target(tmp_path):
    _run_snippet(
        'hook_instrument "probe-gate2"\n'
        'log_gate_decision "phase-a" "deny" "no plan approved" "src/foo.py"\n'
        'exit 2',
        tmp_path,
    )
    row = [r for r in _rows(tmp_path, "audit") if r.get("event") == "gate_decision"][0]
    assert row["gate"] == "phase-a"
    assert row["decision"] == "deny"
    assert row["reason"] == "no plan approved"
    assert row["target"] == "src/foo.py"


def test_gate_decision_records_allow_not_only_deny(tmp_path):
    """Deny-only logging preserves the audit's blind spot: a silent ALLOW is
    indistinguishable from a gate that never ran."""
    _run_snippet(
        'hook_instrument "probe-allow"\n'
        'log_gate_decision "phase-a" "allow" "gates approved" "src/foo.py"\n'
        'exit 0',
        tmp_path,
    )
    row = [r for r in _rows(tmp_path, "audit") if r.get("event") == "gate_decision"][0]
    assert row["decision"] == "allow"


def test_gate_decision_routes_to_audit_not_metrics(tmp_path):
    """It is a governance decision: 365-day retention, not the 90-day metrics window."""
    _run_snippet(
        'hook_instrument "probe-route"\n'
        'log_gate_decision "phase-a" "deny" "r" "t"\n'
        'exit 2',
        tmp_path,
    )
    assert [r for r in _rows(tmp_path, "audit") if r.get("event") == "gate_decision"]
    assert not [r for r in _rows(tmp_path, "metrics") if r.get("event") == "gate_decision"]


def test_gate_decision_sanitizes_newlines_in_the_reason(tmp_path):
    """A reason carries tool input; a forged newline must not fake a second row."""
    _run_snippet(
        'hook_instrument "probe-inj"\n'
        'log_gate_decision "phase-a" "deny" "line one\nline two" "t"\n'
        'exit 2',
        tmp_path,
    )
    # Read through _rows so the buffer is drained first: gate decisions are appended and
    # emitted once per turn now, like hook_execution, rather than spawning python per
    # hook. Asserting on the parsed rows is also stricter than counting lines, because it
    # proves exactly one gate_decision RECORD exists rather than one line of text.
    gate_rows = [r for r in _rows(tmp_path, "audit") if r.get("event") == "gate_decision"]
    assert len(gate_rows) == 1, f"a newline in the reason forged extra rows: {gate_rows}"
    assert "\n" not in gate_rows[0]["reason"]


def _audit_without_draining(tmp_path: Path) -> list[dict]:
    """Read the audit stream WITHOUT flushing. Distinguishes 'written synchronously'
    from 'written eventually', which is the whole point of the split below."""
    path = tmp_path / "logs" / "hookproj" / "audit.jsonl"
    if not path.is_file():
        return []
    return [json.loads(ln) for ln in path.read_text().splitlines() if ln.strip()]


def test_a_denial_is_written_before_the_hook_returns(tmp_path):
    """A DENY must not wait for a drain.

    Review found the regression this pins: routing every gate_decision through the
    once-per-turn drain meant a record whose session never saw another Stop or
    SessionEnd was LOST, not delayed. A denial is the record proving a gate blocked
    something, so it keeps the synchronous path it always had. Denials are rare; the
    per-write cost came from the allow that every successful write logs.
    """
    _run_snippet(
        'hook_instrument "probe-deny"\n'
        'log_gate_decision "phase-a" "deny" "no plan approved" "/tmp/x.py"\n'
        'exit 2',
        tmp_path,
    )
    rows = [r for r in _audit_without_draining(tmp_path) if r.get("event") == "gate_decision"]
    assert len(rows) == 1, (
        f"a denial was not recorded synchronously; it would be lost if this session "
        f"never drained. Found: {rows}"
    )
    assert rows[0]["decision"] == "deny"


@pytest.mark.parametrize("decision", ["ask", "error", "inherit"])
def test_any_decision_that_is_not_allow_is_written_synchronously(tmp_path, decision):
    """The check is `!= "allow"`, not `== "deny"`, and that distinction is live.

    writ-bash-write-gate.sh logs "ask" at a suspected data-exfiltration command and at a
    commit over an unresolved reviewer CRITICAL; writ-manual-test-grant.sh logs "error";
    writ-subagent-start.sh logs "inherit". None is "allow", every one is as audit-worthy
    as a denial, and a future edit to `== "deny"` reads just as plausibly correct in
    isolation. This is what would catch that edit.
    """
    _run_snippet(
        'hook_instrument "probe-nonallow"\n'
        f'log_gate_decision "bash-egress" "{decision}" "needs confirmation" "/tmp/x.py"\n'
        'exit 0',
        tmp_path,
    )
    rows = [r for r in _audit_without_draining(tmp_path) if r.get("event") == "gate_decision"]
    assert len(rows) == 1 and rows[0]["decision"] == decision, (
        f"a {decision!r} decision was buffered rather than written synchronously, so it "
        f"would be lost if this session never drained: {rows}"
    )


def test_an_allow_is_buffered_not_written_synchronously(tmp_path):
    """The other half, and the reason the write path got faster: an allow is the volume,
    it is written on every successful write, and its claim (the gate ran) is still
    carried by the hook_execution row beside it."""
    _run_snippet(
        'hook_instrument "probe-allow"\n'
        'log_gate_decision "phase-a" "allow" "validation passed" "/tmp/x.py"\n'
        'exit 0',
        tmp_path,
    )
    assert _audit_without_draining(tmp_path) == [], (
        "an allow was emitted synchronously, so the per-write spawn is back"
    )
    drained = [r for r in _rows(tmp_path, "audit") if r.get("event") == "gate_decision"]
    assert len(drained) == 1 and drained[0]["decision"] == "allow", (
        f"the buffered allow never reached the audit stream: {drained}"
    )


def test_a_buffered_decision_carries_its_own_decision_time(tmp_path):
    """emit() stamps `ts` when it runs, so a drained row claimed DRAIN time: review
    measured a decision made at 22:48:41 recorded as 22:48:44, and several decisions in
    one turn collapsing onto the same instant. The row carries its own stamp now."""
    before = int(time.time())
    _run_snippet(
        'hook_instrument "probe-when"\n'
        'log_gate_decision "phase-a" "allow" "ok" "/tmp/x.py"\n'
        'exit 0',
        tmp_path,
    )
    after = int(time.time())
    time.sleep(1.2)  # drain strictly later than the decision
    rows = [r for r in _rows(tmp_path, "audit") if r.get("event") == "gate_decision"]
    assert len(rows) == 1
    decided = int(rows[0]["decided_at"])
    assert before <= decided <= after, (
        f"decided_at {decided} is outside the window the decision was made in "
        f"({before}..{after}); it is probably drain time"
    )


# --- coverage: no wired hook left silent -------------------------------------


def _calls(hook: str, func: str) -> int:
    """Count real invocations of `func`, not mere mentions.

    A substring check counts a comment naming the helper as a call, which is a
    false green (it happened: a comment reading "hook_instrument /
    log_gate_decision" made an unwired gate look wired). Requires the name at the
    start of a line, optionally indented and optionally guarded by a `type ... &&`
    availability check, followed by whitespace and an argument.
    """
    text = (HOOKS_DIR / hook).read_text()
    # The call may stand alone or follow a `&&` guard (two hooks that do not
    # normally source common.sh use `type hook_instrument >/dev/null 2>&1 && ...`).
    # The guard prefix must allow `&` -- an earlier `[^&]*` version failed on the
    # `2>&1` inside the redirect. Excluding `#` keeps a comment that merely names
    # the helper from counting as a call.
    pattern = rf"^[ \t]*(?:[^#\n]*&&[ \t]*)?{func}[ \t]+\S"
    return len(re.findall(pattern, text, re.MULTILINE))


@pytest.mark.parametrize("hook", sorted(set(DECIDING_HOOKS + TIMING_ONLY_HOOKS)))
def test_target_hook_calls_hook_instrument(hook):
    assert _calls(hook, "hook_instrument") >= 1, (
        f"{hook} runs on every turn and records nothing"
    )


@pytest.mark.parametrize("hook", DECIDING_HOOKS)
def test_deciding_hook_records_a_gate_decision(hook):
    assert _calls(hook, "log_gate_decision") >= 1, (
        f"{hook} decides allow/deny but records no decision"
    )


@pytest.mark.parametrize("hook", DECIDING_HOOKS)
def test_deciding_hook_records_both_branches(hook):
    """Both allow and deny, not deny-only: a silent ALLOW is indistinguishable
    from a gate that never ran, which is the blind spot the audit found.

    Two shapes satisfy this, so the check is on the decision ARGUMENT, not the
    call count (an earlier count>=2 version wrongly failed the second shape):
      - separate literal calls on each branch: `log_gate_decision "g" "deny" ...`
        and `... "allow" ...`
      - one call whose decision is computed from what the gate actually did:
        `log_gate_decision "g" "$GATE_DECISION" ...` -- strictly better, since the
        record cannot disagree with the decision.
    """
    text = (HOOKS_DIR / hook).read_text()
    args = re.findall(r"^[ \t]*log_gate_decision\s+\S+\s+(\S+)", text, re.MULTILINE)
    assert args, f"{hook} never calls log_gate_decision"
    literals = {a.strip('"\'') for a in args if not a.startswith(('"$', "$"))}
    computed = [a for a in args if a.startswith(('"$', "$"))]
    assert computed or {"allow", "deny"} <= literals, (
        f"{hook} records only {sorted(literals)}; allow and deny must both be "
        f"recorded, or the decision passed as a computed variable"
    )


def test_every_wired_hook_emits_hook_execution():
    """The audit's headline finding, as an executable check: 18 of 37 wired hooks
    emitted nothing. This fails until that number is 0."""
    silent = []
    for name in sorted(_wired_hooks()):
        path = HOOKS_DIR / name
        if not path.is_file():
            continue
        text = path.read_text()
        if not any(tok in text for tok in
                   ("hook_instrument", "hook_timer_end", "log_friction_event",
                    "friction-append")):
            silent.append(name)
    assert silent == [], f"wired hooks emitting nothing: {silent}"
