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


# --- the target argument names a variable something actually sets -------------
#
# Finding 5b of the containment audit (plan.md 2412ba38-51e1-4b73-895b-7b240a3c21d3).
# Four hooks pass `"${FILE_PATH:-}"` as the target argument while assigning only
# `FILE="$HOOK_FILE_PATH"`. `FILE_PATH` is set nowhere in those scripts and nowhere in
# bin/lib/common.sh, whose `load_hook_env` sets HOOK_FILE_PATH and nothing else, so the
# `:-` default fires every time and eight row kinds record an EMPTY target. In the
# measured corpus that is about 3,339 rows (`pre-write-validation` 2,300 plus
# `test-first` 1,039) whose governance record cannot say which file was judged.
#
# THE DETECTOR IS STRUCTURAL AND DERIVED, never a list of the four: no call site may pass
# a variable that neither its own hook nor common.sh ever assigns. A fifth hook written
# next month is judged without anyone remembering to register it, and the finding names
# the hook and the variable rather than a count.

_GATE_CALL_RE = re.compile(
    r"^[ \t]*(?:[^#\n]*&&[ \t]*)?log_gate_decision[ \t]+(?P<args>\S.*)$", re.MULTILINE
)
_VAR_REF_RE = re.compile(r"\$\{?(?P<name>[A-Za-z_][A-Za-z0-9_]*)")
# The assignment shapes this tree really uses. `local -a NAME=(...)`, `read -ra NAME`,
# `printf -v NAME`, `mapfile -t NAME`, `for NAME in`, and the bare `A="" B="" C=""` run
# common.sh:388 opens `load_hook_env` with.
_ASSIGN_RES = (
    re.compile(r"(?:^|[\s;&|(])(?:(?:local|export|declare|typeset)\s+(?:-\w+\s+)*)?"
               r"(?P<name>[A-Za-z_][A-Za-z0-9_]*)\+?=(?!=)"),
    re.compile(r"\bprintf\s+-v\s+(?P<name>[A-Za-z_][A-Za-z0-9_]*)"),
    re.compile(r"\bmapfile\s+(?:-\w+\s+)*(?P<name>[A-Za-z_][A-Za-z0-9_]*)"),
    re.compile(r"\bfor\s+(?P<name>[A-Za-z_][A-Za-z0-9_]*)\s+in\b"),
    # `${NAME:=default}` and `${NAME=default}` assign as a side effect of expanding.
    re.compile(r"\$\{(?P<name>[A-Za-z_][A-Za-z0-9_]*):?="),
)
# `read` assigns EVERY name it is given, not just the first, and this tree depends on it:
# `writ-worktree-safety.sh:1159` splits one verdict into three
# (`read -r WT_DECISION WT_TARGET WT_REASON`) and logs two of them. A first-name-only
# pattern reported those two as unassigned, which is a false alarm on correct code and
# exactly the kind of finding that gets a detector deleted rather than fixed.
_READ_RE = re.compile(
    r"\bread\s+(?P<rest>(?:-\w+\s+)*[A-Za-z_][A-Za-z0-9_]*(?:\s+[A-Za-z_][A-Za-z0-9_]*)*)"
)


def _assigned_names(source: str) -> set[str]:
    """Every variable name this source assigns, on any non-comment line.

    OVER-INCLUSION IS THE SAFE DIRECTION HERE ONLY BECAUSE THE DETECTOR IS PROVED
    CONDITIONAL: a name this function wrongly reports as assigned is a defect the scan
    would miss, so `test_the_detector_sees_a_reintroduced_defect` drives a synthetic hook
    that reintroduces the exact shape and requires it to be found. Without that proof a
    permissive assignment scan would read green on a tree full of empty targets.
    """
    names: set[str] = set()
    for line in source.split("\n"):
        if line.lstrip().startswith("#"):
            continue
        for pattern in _ASSIGN_RES:
            names |= {m.group("name") for m in pattern.finditer(line)}
        for match in _READ_RE.finditer(line):
            names |= {tok for tok in match.group("rest").split()
                      if not tok.startswith("-")}
    return names


def gate_decision_call_sites(*, scripts_dir: Path = HOOKS_DIR) -> list[tuple[str, int, str]]:
    """(script, 1-based line, argument text) for every real `log_gate_decision` call.

    A call, not a mention: the same anchored form `_calls` above uses, which exists
    because a comment naming the helper once made an unwired gate look wired.
    """
    sites: list[tuple[str, int, str]] = []
    for path in sorted(Path(scripts_dir).glob("*.sh")):
        text = path.read_text(encoding="utf-8", errors="replace")
        for match in _GATE_CALL_RE.finditer(text):
            line = text[:match.start()].count("\n") + 1
            sites.append((path.name, line, match.group("args")))
    return sites


def unassigned_gate_decision_arguments(
    *, scripts_dir: Path = HOOKS_DIR, common: Path = COMMON_SH
) -> list[tuple[str, int, str]]:
    """(script, line, variable) for every `log_gate_decision` argument naming a variable
    that neither the hook nor common.sh ever assigns. Must be empty.

    `scripts_dir` and `common` are keywords for the reason
    `tests/_inventory.py::envelope_emitting_scripts(*, scripts_dir=)` gives: the
    detector's precision is pinned against synthetic hooks under `tmp_path`, never
    against a real hook's current wording.
    """
    shared = _assigned_names(common.read_text(encoding="utf-8", errors="replace"))
    findings: list[tuple[str, int, str]] = []
    for path in sorted(Path(scripts_dir).glob("*.sh")):
        own = _assigned_names(path.read_text(encoding="utf-8", errors="replace")) | shared
        text = path.read_text(encoding="utf-8", errors="replace")
        for match in _GATE_CALL_RE.finditer(text):
            line = text[:match.start()].count("\n") + 1
            for ref in _VAR_REF_RE.finditer(match.group("args")):
                name = ref.group("name")
                if name not in own:
                    findings.append((path.name, line, name))
    return sorted(set(findings))


def test_the_call_site_scanner_sees_the_live_gates():
    """ANTI-VACUITY for the detector below, which only ever asserts emptiness. A scanner
    whose matching silently broke would report zero findings on any tree."""
    sites = gate_decision_call_sites()
    assert sites, "no log_gate_decision call site was found at all"
    seen = {script for script, _line, _args in sites}
    missing = [h for h in DECIDING_HOOKS if h not in seen]
    assert missing == [], f"the scanner missed the call sites in {missing}"


def test_no_gate_decision_argument_names_a_variable_nothing_assigns():
    """Eight sites across four hooks pass `${FILE_PATH:-}` today, so every row those
    gates write records an empty target and the governance record cannot say which file
    was judged. The fix is one token per site; this is what stops the ninth."""
    findings = unassigned_gate_decision_arguments()
    assert findings == [], (
        "these log_gate_decision arguments name a variable that neither the hook nor "
        "bin/lib/common.sh ever assigns, so the `:-` default fires on every row and the "
        "field is recorded empty: "
        + "; ".join(f"{script}:{line} ${var}" for script, line, var in findings)
    )


def test_the_detector_sees_a_reintroduced_defect(tmp_path):
    """CONDITIONALITY, on a synthetic hook rather than by editing the real tree: the same
    predicate that must pass on a fixed tree has to fail the moment the shape returns."""
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    (scripts / "probe.sh").write_text(
        '#!/usr/bin/env bash\n'
        'FILE="$HOOK_FILE_PATH"\n'
        'log_gate_decision "probe" "allow" "ok" "${FILE_PATH:-}"\n'
    )
    common = tmp_path / "common.sh"
    common.write_text('HOOK_FILE_PATH=""\n')
    findings = unassigned_gate_decision_arguments(scripts_dir=scripts, common=common)
    assert findings == [("probe.sh", 3, "FILE_PATH")], findings


def test_the_detector_accepts_a_variable_the_hook_assigns(tmp_path):
    """The other half. A detector that flagged every variable would be just as useless,
    and it would make the fix look impossible."""
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    (scripts / "probe.sh").write_text(
        '#!/usr/bin/env bash\n'
        'FILE="$HOOK_FILE_PATH"\n'
        'log_gate_decision "probe" "allow" "ok" "$FILE"\n'
    )
    common = tmp_path / "common.sh"
    common.write_text('HOOK_FILE_PATH=""\n')
    assert unassigned_gate_decision_arguments(scripts_dir=scripts, common=common) == []


def test_the_detector_accepts_a_variable_only_common_sh_assigns(tmp_path):
    """The shared source counts: a hook that passes `$HOOK_FILE_PATH` straight through is
    correct, and common.sh is where that name is set."""
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    (scripts / "probe.sh").write_text(
        '#!/usr/bin/env bash\n'
        'log_gate_decision "probe" "allow" "ok" "$HOOK_FILE_PATH"\n'
    )
    common = tmp_path / "common.sh"
    common.write_text('load_hook_env() {\n  HOOK_FILE_PATH="" HOOK_COMMAND=""\n}\n')
    assert unassigned_gate_decision_arguments(scripts_dir=scripts, common=common) == []


# --- the row a judging hook writes names the file it judged -------------------
#
# The structural detector above cannot see what a hook RECORDS, only what it spells, so
# the claim about the log is read out of the log: each of the four hooks is TRIGGERED and
# the emitted row's target is asserted. The plan requires this order (verify by
# triggering, then fix), because a claim about a log has to be read out of the log.

# The env, the cache seeding and the two-sink row collector live in
# tests/test_gate_decision_mode_stamp.py, which already owns "drive a real judging hook
# and read the gate_decision row it wrote". Importing them keeps the hook-subprocess
# environment in ONE place: its drift is silent (a run against the developer's live cache
# that still passes), while a wrong exit-code expectation is loud, so the runner below is
# local and the environment is not.
from tests.test_gate_decision_mode_stamp import _env as _stamp_env  # noqa: E402
from tests.test_gate_decision_mode_stamp import _gate_rows as _stamp_gate_rows  # noqa: E402


def _sandbox(tmp_path: Path) -> Path:
    """A throwaway project root for every subprocess below.

    NOT DECORATION. `mode set` stamps `cache["project_root"]` from the process cwd and
    clearing gate state DELETES `<project_root>/.claude/gates/*.approved`, so a spawned
    `writ-session.py mode set` that inherits pytest's cwd destroys THIS repo's approval
    artifacts as a side effect of running the suite; a sentinel probe once found 26
    modules doing exactly that, which is why
    `tests/fixtures/session_state.py::sandbox_cwd` exists.

    An explicit `cwd=` rather than that autouse fixture, because importing it here would
    chdir all 70-odd pre-existing tests in this module. The `.git` marker stops a
    project-root walk inside the sandbox, and the gates directory gives the cleanup a
    real (empty) directory to act on instead of a missing-path no-op.
    """
    sandbox = tmp_path / "cwd-sandbox"
    (sandbox / ".claude" / "gates").mkdir(parents=True, exist_ok=True)
    (sandbox / ".git").mkdir(exist_ok=True)
    return sandbox


def _seed_work_mode(tmp_path: Path, sid: str) -> None:
    """Set the session's mode exactly as production does (file-direct CLI), inside the
    sandbox. Four of the hooks below exit early unless `is_work_mode` says yes."""
    subprocess.run(
        [sys.executable, str(REPO / "bin" / "lib" / "writ-session.py"),
         "mode", "set", "work", sid],
        env=_stamp_env(tmp_path), cwd=str(_sandbox(tmp_path)),
        check=True, capture_output=True, text=True, timeout=60,
    )


def _run_judging_hook(tmp_path: Path, hook: str, envelope: dict) -> subprocess.CompletedProcess:
    """Drive a real hook. The exit code is NOT asserted here: validate-handoff.sh refuses
    with exit 1 by design, and a runner that demanded 0 could only ever trigger the allow
    branches."""
    return subprocess.run(
        ["bash", str(HOOKS_DIR / hook)], input=json.dumps(envelope),
        capture_output=True, text=True, env=_stamp_env(tmp_path),
        cwd=str(_sandbox(tmp_path)), timeout=120,
    )


# The four hooks finding 5b names. An ENUMERATION on purpose, and the one in this cycle:
# each needs its own envelope, its own file shape and its own reachable branch, which is
# fixture-building rather than a derivation. The guard against a fifth is the structural
# detector above, which derives its population from the tree.
_JUDGING_HOOKS = (
    "pre-validate-file.sh",
    "validate-design-doc.sh",
    "validate-handoff.sh",
    "validate-test-file.sh",
)


def _judging_cases(tmp_path: Path) -> dict[str, dict]:
    """One trigger per hook, each reaching a site that passes `${FILE_PATH:-}` today.

    Deliberately the CHEAPEST branch per hook rather than both: the defect is in the
    argument, which is identical on the allow and deny sites of each pair, and the
    structural detector above is what covers the sites a trigger cannot reach.
    """
    docs = tmp_path / "docs" / "specs"
    docs.mkdir(parents=True, exist_ok=True)
    handoffs = tmp_path / ".claude" / "handoffs"
    handoffs.mkdir(parents=True, exist_ok=True)
    notes = tmp_path / "notes.txt"
    target = tmp_path / "probe_target.py"
    design = docs / "probe-design.md"
    handoff = handoffs / "slice-1.json"
    handoff.write_text("{not json")
    return {
        "validate-test-file.sh": {
            "gate": "test-first",
            "file": notes,
            "envelope": {
                "hook_event_name": "PreToolUse",
                "tool_name": "Write",
                "tool_input": {"file_path": str(notes), "content": "hello\n"},
            },
        },
        "pre-validate-file.sh": {
            "gate": "pre-write-validation",
            "file": target,
            "envelope": {
                "hook_event_name": "PreToolUse",
                "tool_name": "Write",
                "tool_input": {"file_path": str(target), "content": "x = 1\n"},
            },
        },
        "validate-design-doc.sh": {
            "gate": "design-doc",
            "file": design,
            "envelope": {
                "hook_event_name": "PreToolUse",
                "tool_name": "Write",
                "tool_input": {"file_path": str(design), "content": "# probe\n"},
            },
        },
        "validate-handoff.sh": {
            "gate": "handoff",
            "file": handoff,
            "envelope": {
                "hook_event_name": "PostToolUse",
                "tool_name": "Write",
                "tool_input": {"file_path": str(handoff)},
            },
        },
    }


@pytest.mark.parametrize("hook", _JUDGING_HOOKS)
def test_the_gate_decision_row_names_the_file_that_was_judged(tmp_path, hook):
    """Read out of the emitted row, not out of the source. A governance record that
    cannot say which file was judged is a record of nothing, and 3,339 rows in the
    measured corpus are in that state."""
    cases = _judging_cases(tmp_path)
    assert set(cases) == set(_JUDGING_HOOKS), (
        "the trigger map and the declared hook list disagree, so one hook is being "
        f"skipped silently: {sorted(cases)}"
    )
    case = cases[hook]
    sid = f"target-{hook.replace('.sh', '')}"
    _seed_work_mode(tmp_path, sid)
    envelope = {"session_id": sid, **case["envelope"]}
    _run_judging_hook(tmp_path, hook, envelope)
    rows = _stamp_gate_rows(tmp_path, sid, case["gate"])
    assert rows, (
        f"{hook} wrote no {case['gate']!r} gate_decision row at all, so this trigger "
        f"does not reach the site under test"
    )
    targets = [r.get("target") for r in rows]
    assert all(targets), (
        f"{hook} recorded an EMPTY target for gate {case['gate']!r}: the argument is "
        f"${{FILE_PATH:-}} and nothing assigns FILE_PATH, so every row it writes names "
        f"no file. Rows: {rows!r}"
    )
    assert str(case["file"]) in targets, (
        f"{hook} recorded {targets!r} rather than the file it judged "
        f"({str(case['file'])!r})"
    )


def test_every_wired_hook_emits_hook_execution():
    """The audit's headline finding, as an executable check: 18 of 37 wired hooks emitted
    nothing. This fails until that number is 0.

    THE PREDICATE CHANGED, and why matters more than the change. It used to look for any of
    four token spellings in the file. That is a lexical scan for a CALL, and it was wrong in
    four different ways: it counted a mention inside a comment as a call, it missed the
    guarded `type hook_instrument && ...` form, and when the redundant `hook_timer_end`
    calls were deleted in favour of the universal trap it read the deletions as silence.
    Coverage is now structural: common.sh installs the trap for any script under
    hooks/scripts/ that sources it, so what has to be true of a wired hook is that it lives
    there and sources common.sh. `tests/test_hook_telemetry_coverage.py` proves the rest by
    RUNNING hooks and counting rows, which no spelling can fool.
    """
    silent = []
    for name in sorted(_wired_hooks()):
        path = HOOKS_DIR / name
        if not path.is_file():
            continue
        if "common.sh" not in path.read_text():
            silent.append(name)
    assert silent == [], (
        "wired hooks that cannot emit a row, because they do not source common.sh and so "
        f"never get its telemetry trap: {silent}"
    )
