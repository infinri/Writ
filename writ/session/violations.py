"""Pending-violation and gate-invalidation/escalation state machine.

POL-6g-3 extracts the enforcement-state commands out of bin/lib/writ-session.py: the pending
violation ledger, the gate invalidation history, and the escalation cycle counter. Imports only
the cache layer + stdlib -- never the facade -- so the graph stays acyclic. The facade
re-exports this surface; main()'s violation/escalation subcommands resolve unchanged.
"""

import json
import os
import sys
from datetime import datetime, timezone

from writ.session.cache import _read_cache, mutate_cache
from writ.session.locators import gate_artifact_path


MAX_CYCLES_BEFORE_ESCALATION = 3


def cmd_add_pending_violation(session_id: str, args: list[str]) -> None:
    """Append a pending violation to the session. Deduplicates by (rule_id, file, line)."""
    rule_id = file = evidence = ""
    line: int | None = None

    i = 0
    while i < len(args):
        if args[i] == "--rule" and i + 1 < len(args):
            rule_id = args[i + 1]; i += 2
        elif args[i] == "--file" and i + 1 < len(args):
            file = args[i + 1]; i += 2
        elif args[i] == "--line" and i + 1 < len(args):
            line = int(args[i + 1]); i += 2
        elif args[i] == "--evidence" and i + 1 < len(args):
            evidence = args[i + 1]; i += 2
        else:
            i += 1

    if not rule_id or not file:
        print("Required: --rule and --file", file=sys.stderr)
        sys.exit(1)

    triple = (rule_id, file, line)
    # Fast path: a duplicate triple is a true no-op (no lock, no write, no mtime
    # bump), matching the pre-lock behavior. validate-rules.sh re-adds the same
    # triple across retry loops on the hot PreToolUse path, so the common case must
    # not take the lock or rewrite the cache.
    existing = _read_cache(session_id).get("pending_violations", [])
    if any((v["rule_id"], v["file"], v.get("line")) == triple for v in existing):
        return

    with mutate_cache(session_id) as cache:
        violations = cache.get("pending_violations", [])
        for v in violations:
            if (v["rule_id"], v["file"], v.get("line")) == triple:
                return  # raced: another writer added the same triple first

        violations.append({"rule_id": rule_id, "file": file, "line": line, "evidence": evidence})
        cache["pending_violations"] = violations


def cmd_clear_pending_violations(session_id: str) -> None:
    """Clear all pending violations (called at phase-boundary)."""
    with mutate_cache(session_id) as cache:
        cache["pending_violations"] = []


# Invalidate-gate option flags: `--flag value` -> the field it populates. Uniform
# single-value flags, so one registry replaces the six-way elif scan.
_INVALIDATE_FLAGS = {
    "--rule": "rule_id",
    "--file": "file",
    "--evidence": "evidence",
    "--trace": "trace",
    "--plan-hash": "plan_hash",
    "--project-root": "project_root",
}


def _parse_invalidate_args(args: list[str]) -> dict:
    """Parse the `--flag value` options after the gate name (args[0]). A recognized flag
    missing its trailing value, or an unknown token, is skipped (i += 1) -- reproducing the
    original positional scan exactly. Returns every field, defaulting to ''."""
    parsed = {key: "" for key in _INVALIDATE_FLAGS.values()}
    i = 1
    while i < len(args):
        key = _INVALIDATE_FLAGS.get(args[i])
        if key is not None and i + 1 < len(args):
            parsed[key] = args[i + 1]
            i += 2
        else:
            i += 1
    return parsed


def _diagnose_escalation(records: list[dict]) -> str:
    """Classify an escalation by its cycle rule_ids: one rule every cycle ('same-rule'),
    all distinct ('different-rules'), or 'mixed'."""
    rule_ids_in_cycles = [r["rule_id"] for r in records]
    unique_rules = set(rule_ids_in_cycles)
    if len(unique_rules) == 1:
        return "same-rule"
    elif len(unique_rules) == len(rule_ids_in_cycles):
        return "different-rules"
    return "mixed"


def cmd_invalidate_gate(session_id: str, args: list[str]) -> None:
    """Invalidate a gate: write record, delete .approved file, check escalation.

    Exit 0: success. Exit 1: bad arguments. Exit 2: cache error.
    Caller should run check-escalation afterward to determine next steps.
    """
    gate_name = args[0] if args else ""
    parsed = _parse_invalidate_args(args)
    rule_id = parsed["rule_id"]
    file = parsed["file"]
    project_root = parsed["project_root"]

    if not gate_name or not rule_id or not file:
        print("Required: <gate_name> --rule <id> --file <path>", file=sys.stderr)
        sys.exit(1)

    # The whole read-modify-write runs under the per-session lock so a concurrent
    # writer can't clobber the invalidation record (or mode/gates). Any failure in
    # this block (cache I/O OR the in-memory record/escalation build) maps to exit 2
    # -- a slightly wider scope than the pre-lock code's two cache-only guards.
    try:
        with mutate_cache(session_id) as cache:
            history = cache.get("invalidation_history", {})
            records = history.get(gate_name, [])
            cycle = len(records) + 1

            records.append({
                "cycle": cycle,
                "rule_id": rule_id,
                "file": file,
                "line": None,
                "evidence": parsed["evidence"],
                "trace": parsed["trace"],
                "prior_plan_hash": parsed["plan_hash"],
                "timestamp": datetime.now(timezone.utc).isoformat(),
            })
            history[gate_name] = records
            cache["invalidation_history"] = history

            # Check escalation threshold
            if cycle >= MAX_CYCLES_BEFORE_ESCALATION:
                cache["escalation"] = {
                    "gate": gate_name,
                    "needed": True,
                    "diagnosis": _diagnose_escalation(records),
                    "feedback_sent": False,
                }
    except Exception as e:
        print(f"invalidate-gate failed: {e}", file=sys.stderr)
        sys.exit(2)

    # Delete THIS SESSION's own gate file (best-effort -- record already written). The
    # invalidation belongs to the session whose rule was violated, so it must never reach a
    # sibling session's artifact in the same project: the flat path this replaces was shared
    # by every session in the repo. An unresolvable path (no root, or a session id that is
    # not a valid path component) leaves nothing to delete.
    gate_file = gate_artifact_path(project_root, session_id, gate_name)
    if gate_file:
        try:
            os.remove(gate_file)
        except OSError:
            pass  # File missing or not deletable; next boundary check retries


def cmd_check_escalation(session_id: str) -> None:
    """Read-only query: is escalation needed? Always exits 0."""
    cache = _read_cache(session_id)
    esc = cache.get("escalation", {"gate": None, "needed": False, "diagnosis": None})
    gate = esc.get("gate")
    cycles = 0
    if gate:
        cycles = len(cache.get("invalidation_history", {}).get(gate, []))
    else:
        # Report max cycles across all gates even when escalation hasn't triggered
        history = cache.get("invalidation_history", {})
        for gate_name, records in history.items():
            if len(records) > cycles:
                cycles = len(records)
                gate = gate_name
    result = {
        "needed": esc.get("needed", False),
        "gate": gate,
        "diagnosis": esc.get("diagnosis"),
        "cycles": cycles,
        "feedback_sent": esc.get("feedback_sent", False),
    }
    json.dump(result, sys.stdout)
    sys.stdout.write("\n")


def cmd_pending_violations(session_id: str) -> None:
    """Output pending violations as JSON array."""
    cache = _read_cache(session_id)
    json.dump(cache.get("pending_violations", []), sys.stdout)
    sys.stdout.write("\n")
