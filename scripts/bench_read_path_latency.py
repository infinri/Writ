#!/usr/bin/env python3
"""Interleaved before/after wall-clock for the PreToolUse READ path.

Plan: .claude/plans/dfacff61-23d5-474e-846c-2e2f0f0ea482/plan.md, Decision 5. This is
the REPORTED number for acceptance criterion 2. The PRECISE instrument is elsewhere:
tests/test_debug_lens_predicate.py counts `python3` invocations with an argv shim, and a
process count is deterministic where wall time on the same workload moves 15 percent on
this machine. Read the two together; neither replaces the other.

WHY IT LIVES IN scripts/ AND NOT benchmarks/. Files under benchmarks/ carry the
graph-wipe hazard and their own safety preamble, and this harness needs neither: it
spawns hooks and reads a clock, and it never touches the graph.

WHAT IT MEASURES. Per-EVENT totals, not per-hook: for each of Read, Grep and Glob it
runs every PreToolUse hook Claude Code would run for that tool, in the order
hooks/hooks.json registers them, so the number is comparable to the whole-event figures
the cycle quotes. Two session states per tool: one the runtime lens provably cannot deny
(mode work) and one where it is active and must be consulted (mode debug with Evidence
and Narrowing recorded, so the check runs in full and then allows).

HOW THE TWO ARMS ARE KEPT HONEST.

  INTERLEAVED. The arms alternate within each iteration, and which arm goes first
  alternates too, so a machine that gets busier partway through loads both arms equally.
  The paired per-iteration delta is reported alongside the medians because a paired
  delta is what survives a noisy machine; a difference of medians is not.

  THE BEFORE ARM IS REAL CODE, not the new hook with a flag flipped. It is materialized
  with `git show <rev>:<path>` into a shadow skill directory whose every other entry is
  a symlink back to this repo, so the old script resolves its own SKILL_DIR, sources the
  same library and calls the same helper, and only the one file under test differs.

  IT REFUSES TO REPORT WHILE BLACKBOX CAPTURE IS ARMED. The sentinel
  ~/.claude/writ-blackbox.on adds a python spawn and about 20ms to EVERY hook, which is
  large enough to fake or to hide this entire effect.

Usage:
    .venv/bin/python scripts/bench_read_path_latency.py
    .venv/bin/python scripts/bench_read_path_latency.py --iterations 30 --before-rev HEAD
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import statistics
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
HOOKS_JSON = REPO / "hooks" / "hooks.json"
TARGET_SCRIPT = "writ-debug-code-gate.sh"
TARGET_REPO_PATH = "hooks/scripts/" + TARGET_SCRIPT
BLACKBOX_SENTINEL = Path.home() / ".claude" / "writ-blackbox.on"
TOOLS = ("Read", "Grep", "Glob")

EVIDENCE_RECORDED = (
    "## Symptom\nslow checkout\n\n"
    "## Evidence\nStack trace at app.py:42; log 2026-06-01T10:00 shows a 3s query.\n\n"
    "## Narrowing\nOnly the /checkout endpoint, SKU pattern ABC-*.\n\n"
    "## Root cause\n\n"
)


def capture_is_armed() -> bool:
    return BLACKBOX_SENTINEL.exists()


def refusal_text() -> str:
    return (
        "REFUSING TO REPORT: blackbox capture is armed.\n"
        f"  {BLACKBOX_SENTINEL} exists. Capture adds a python spawn and about 20ms to\n"
        "  every hook invocation, which is larger than the effect this harness measures,\n"
        "  so any number printed now would be unattributable.\n"
        "  ACTION: delete that sentinel file, then run this harness again. Only the\n"
        "  developer who armed capture may remove it; this harness never deletes it,\n"
        "  because turning someone else's instrumentation off silently is worse than\n"
        "  refusing to measure."
    )


def pretooluse_chain(tool: str) -> list[str]:
    """Every PreToolUse hook command for `tool`, in registration order."""
    manifest = json.loads(HOOKS_JSON.read_text(encoding="utf-8"))
    chain: list[str] = []
    for entry in manifest.get("hooks", {}).get("PreToolUse", []) or []:
        matcher = entry.get("matcher") or ""
        if not re.fullmatch(matcher, tool):
            continue
        for hook in entry.get("hooks") or []:
            command = str(hook.get("command") or "")
            chain.append(command)
    return chain


def resolve_command(command: str) -> Path:
    """A hooks.json command string as an absolute path in THIS checkout."""
    name = command.rstrip().rsplit("/", 1)[-1]
    return REPO / "hooks" / "scripts" / name


def build_shadow_skill_dir(root: Path, before_rev: str) -> Path:
    """A skill directory identical to this one except for the pre-change target script.

    Every top-level entry is symlinked back to the repo, so the materialized script
    resolves the same `bin/lib/common.sh`, the same `writ` package and the same session
    helper. `hooks/` and `hooks/scripts/` are real directories holding symlinks, because
    exactly one file inside them has to differ.
    """
    shadow = root / "shadow-skill"
    shadow.mkdir(parents=True, exist_ok=True)
    for entry in sorted(REPO.iterdir()):
        if entry.name == "hooks":
            continue
        (shadow / entry.name).symlink_to(entry)
    (shadow / "hooks").mkdir()
    for entry in sorted((REPO / "hooks").iterdir()):
        if entry.name == "scripts":
            continue
        (shadow / "hooks" / entry.name).symlink_to(entry)
    (shadow / "hooks" / "scripts").mkdir()
    for entry in sorted((REPO / "hooks" / "scripts").iterdir()):
        if entry.name == TARGET_SCRIPT:
            continue
        (shadow / "hooks" / "scripts" / entry.name).symlink_to(entry)

    before_source = subprocess.run(
        ["git", "show", f"{before_rev}:{TARGET_REPO_PATH}"],
        cwd=str(REPO), capture_output=True, text=True, check=True,
    ).stdout
    target = shadow / "hooks" / "scripts" / TARGET_SCRIPT
    target.write_text(before_source)
    target.chmod(0o755)
    return shadow


def make_state(root: Path, state: str) -> tuple[str, Path, Path, Path, Path]:
    """One isolated (session id, cache dir, log root, project) for a workload cell."""
    cell = root / f"state-{state}"
    cache_dir = cell / "cache"
    log_root = cell / "logs"
    home = cell / "home"
    project = cell / "proj"
    for d in (cache_dir, log_root, home, project):
        d.mkdir(parents=True, exist_ok=True)
    (home / ".claude").mkdir(parents=True, exist_ok=True)
    (project / ".git").mkdir(exist_ok=True)
    (project / "app.py").write_text("# code\n")
    session_id = f"bench-{state}-{uuid.uuid4().hex[:8]}"
    if state == "skip":
        cache = {"mode": "work"}
    else:
        cache = {"mode": "debug"}
        (project / "debug.md").write_text(EVIDENCE_RECORDED)
    name = "writ" + "-session-" + session_id + ".json"
    (cache_dir / name).write_text(json.dumps(cache))
    return session_id, cache_dir, log_root, home, project


def build_env(cache_dir: Path, log_root: Path, home: Path, project: Path) -> dict:
    env = dict(os.environ)
    env["WRIT_CACHE_DIR"] = str(cache_dir)
    env["WRIT_LOG_ROOT"] = str(log_root)
    env["WRIT_LOG_PROJECT"] = "bench-read-path"
    env.pop("WRIT_FRICTION_LOG", None)
    env["WRIT_NO_AUTOSTART"] = "1"
    env["HOME"] = str(home)
    # HOME is pinned so the blackbox sentinel and the real capture log are out of reach,
    # but the daemon socket is derived from HOME, and losing it would move every daemon
    # call onto TCP and change what is being measured. The real socket is named
    # explicitly instead.
    real_socket = Path.home() / ".cache" / "writ" / "run" / "writ.sock"
    if real_socket.exists():
        env["WRIT_SOCKET"] = str(real_socket)
    return env


def envelope(tool: str, session_id: str, project: Path) -> str:
    if tool == "Read":
        tool_input = {"file_path": str(project / "app.py")}
    elif tool == "Grep":
        tool_input = {"pattern": "foo", "path": str(project)}
    else:
        tool_input = {"pattern": "**/*.py", "path": str(project)}
    return json.dumps({
        "session_id": session_id,
        "hook_event_name": "PreToolUse",
        "tool_name": tool,
        "tool_input": tool_input,
    })


def run_chain(hooks: list[Path], payload: str, env: dict, cwd: Path) -> float:
    """Wall-clock milliseconds for one whole PreToolUse event."""
    start = time.perf_counter()
    for hook in hooks:
        subprocess.run(
            ["bash", str(hook)], input=payload, capture_output=True, text=True,
            env=env, cwd=str(cwd), timeout=60,
        )
    return (time.perf_counter() - start) * 1000.0


def p95(values: list[float]) -> float:
    ordered = sorted(values)
    return ordered[max(0, int(len(ordered) * 0.95) - 1)]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--iterations", type=int, default=20)
    parser.add_argument("--warmups", type=int, default=3)
    parser.add_argument("--before-rev", default="HEAD")
    args = parser.parse_args()

    if capture_is_armed():
        print(refusal_text(), file=sys.stderr)
        return 2

    root = Path(tempfile.mkdtemp(prefix="bench-read-path-"))
    try:
        shadow = build_shadow_skill_dir(root, args.before_rev)
        print(f"before arm: {args.before_rev}:{TARGET_REPO_PATH} materialized in {shadow}")
        print(f"after arm:  {REPO / TARGET_REPO_PATH}")
        print(f"iterations: {args.iterations} timed, {args.warmups} untimed warmups per cell\n")

        rows = []
        for state in ("skip", "lens-active"):
            session_id, cache_dir, log_root, home, project = make_state(root, state)
            env = build_env(cache_dir, log_root, home, project)
            for tool in TOOLS:
                payload = envelope(tool, session_id, project)
                after_chain = [resolve_command(c) for c in pretooluse_chain(tool)]
                before_chain = [
                    shadow / "hooks" / "scripts" / p.name if p.name == TARGET_SCRIPT else p
                    for p in after_chain
                ]
                for _ in range(args.warmups):
                    run_chain(after_chain, payload, env, project)
                    run_chain(before_chain, payload, env, project)

                after_ms: list[float] = []
                before_ms: list[float] = []
                deltas: list[float] = []
                for i in range(args.iterations):
                    if i % 2 == 0:
                        a = run_chain(after_chain, payload, env, project)
                        b = run_chain(before_chain, payload, env, project)
                    else:
                        b = run_chain(before_chain, payload, env, project)
                        a = run_chain(after_chain, payload, env, project)
                    after_ms.append(a)
                    before_ms.append(b)
                    deltas.append(b - a)
                rows.append({
                    "state": state, "tool": tool, "hooks": len(after_chain),
                    "before_median": statistics.median(before_ms),
                    "before_p95": p95(before_ms),
                    "after_median": statistics.median(after_ms),
                    "after_p95": p95(after_ms),
                    "paired_median_delta": statistics.median(deltas),
                    "paired_min_delta": min(deltas),
                    "paired_max_delta": max(deltas),
                })

        header = (
            f"{'state':12s} {'tool':6s} {'n':>2s}  {'before med':>10s} {'before p95':>10s} "
            f"{'after med':>10s} {'after p95':>10s} {'paired med':>10s} {'paired min':>10s} "
            f"{'paired max':>10s}"
        )
        print(header)
        print("-" * len(header))
        for r in rows:
            print(
                f"{r['state']:12s} {r['tool']:6s} {r['hooks']:2d}  "
                f"{r['before_median']:9.1f}m {r['before_p95']:9.1f}m "
                f"{r['after_median']:9.1f}m {r['after_p95']:9.1f}m "
                f"{r['paired_median_delta']:9.1f}m {r['paired_min_delta']:9.1f}m "
                f"{r['paired_max_delta']:9.1f}m"
            )
        print("\nAll figures are per EVENT (every PreToolUse hook for that tool), in "
              "milliseconds.\n'paired' is before minus after within the same iteration; "
              "positive means the after arm was faster.")
        return 0
    finally:
        shutil.rmtree(root, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
