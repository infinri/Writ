#!/usr/bin/env python3
"""Session cache helper for Writ RAG bridge hooks.

Manages per-session state (loaded rule IDs, remaining budget, context pressure)
in a temp file so hooks can deduplicate rules across turns.

Stdlib only -- no external dependencies.
"""

import json
import os
import sys
import tempfile
from datetime import datetime, timezone

# Mirrored from writ/retrieval/session.py
DEFAULT_SESSION_BUDGET = 8000
APPROX_TOKENS_PER_RULE_FULL = 200
APPROX_TOKENS_PER_RULE_STANDARD = 120
APPROX_TOKENS_PER_RULE_SUMMARY = 40

CACHE_DIR = tempfile.gettempdir()


def _cache_path(session_id: str) -> str:
    return os.path.join(CACHE_DIR, f"writ-session-{session_id}.json")


def _read_cache(session_id: str) -> dict:
    path = _cache_path(session_id)
    default = {
        "loaded_rule_ids": [],
        "loaded_rules": [],
        "remaining_budget": DEFAULT_SESSION_BUDGET,
        "context_percent": 0,
        "queries": 0,
        "tier": None,
        "files_written": [],
        "analysis_results": {},
        "feedback_sent": [],
        "pending_violations": [],
        "invalidation_history": {},
        "escalation": {"gate": None, "needed": False, "diagnosis": None, "feedback_sent": False},
    }
    if not os.path.exists(path):
        return default
    try:
        with open(path) as f:
            data = json.load(f)
        data.setdefault("tier", None)
        data.setdefault("files_written", [])
        data.setdefault("analysis_results", {})
        data.setdefault("feedback_sent", [])
        data.setdefault("loaded_rules", [])
        data.setdefault("pending_violations", [])
        data.setdefault("invalidation_history", {})
        data.setdefault("escalation", {"gate": None, "needed": False, "diagnosis": None, "feedback_sent": False})
        return data
    except (json.JSONDecodeError, OSError):
        return default


def _write_cache(session_id: str, data: dict) -> None:
    path = _cache_path(session_id)
    tmp_path = path + ".tmp"
    with open(tmp_path, "w") as f:
        json.dump(data, f)
    os.rename(tmp_path, path)


def cmd_read(session_id: str) -> None:
    cache = _read_cache(session_id)
    json.dump(cache, sys.stdout)
    sys.stdout.write("\n")


def cmd_update(session_id: str, args: list[str]) -> None:
    cache = _read_cache(session_id)

    i = 0
    while i < len(args):
        if args[i] == "--add-rules" and i + 1 < len(args):
            new_ids = json.loads(args[i + 1])
            existing = set(cache["loaded_rule_ids"])
            existing.update(new_ids)
            cache["loaded_rule_ids"] = sorted(existing)
            i += 2
        elif args[i] == "--cost" and i + 1 < len(args):
            cost = int(args[i + 1])
            cache["remaining_budget"] = max(0, cache["remaining_budget"] - cost)
            i += 2
        elif args[i] == "--context-percent" and i + 1 < len(args):
            cache["context_percent"] = int(args[i + 1])
            i += 2
        elif args[i] == "--inc-queries":
            cache["queries"] = cache.get("queries", 0) + 1
            i += 1
        elif args[i] == "--add-file" and i + 1 < len(args):
            files = set(cache.get("files_written", []))
            files.add(args[i + 1])
            cache["files_written"] = sorted(files)
            i += 2
        elif args[i] == "--add-file-result" and i + 2 < len(args):
            # --add-file-result <filepath> <pass|fail>
            results = cache.get("analysis_results", {})
            results[args[i + 1]] = args[i + 2]
            cache["analysis_results"] = results
            i += 3
        elif args[i] == "--add-feedback-sent" and i + 1 < len(args):
            sent = set(cache.get("feedback_sent", []))
            sent.add(args[i + 1])
            cache["feedback_sent"] = sorted(sent)
            i += 2
        elif args[i] == "--add-rule-objects" and i + 1 < len(args):
            new_rules = json.loads(args[i + 1])
            existing_ids = {r["rule_id"] for r in cache.get("loaded_rules", [])}
            for rule in new_rules:
                if rule.get("rule_id") and rule["rule_id"] not in existing_ids:
                    cache["loaded_rules"].append({
                        "rule_id": rule["rule_id"],
                        "trigger": rule.get("trigger", ""),
                        "statement": rule.get("statement", ""),
                        "violation": rule.get("violation", ""),
                        "pass_example": rule.get("pass_example", ""),
                        "enforcement": rule.get("enforcement", ""),
                        "domain": rule.get("domain", ""),
                        "severity": rule.get("severity", ""),
                    })
                    existing_ids.add(rule["rule_id"])
            i += 2
        else:
            i += 1

    _write_cache(session_id, cache)


def cmd_should_skip(session_id: str, threshold: int = 75) -> None:
    cache = _read_cache(session_id)
    if cache["remaining_budget"] <= 0:
        sys.exit(0)  # skip: budget exhausted
    if cache["context_percent"] >= threshold:
        sys.exit(0)  # skip: context pressure
    sys.exit(1)  # don't skip: proceed with query


def _estimate_cost(rules: list[dict], mode: str) -> int:
    if mode == "full":
        return len(rules) * APPROX_TOKENS_PER_RULE_FULL
    elif mode == "standard":
        return len(rules) * APPROX_TOKENS_PER_RULE_STANDARD
    else:
        return len(rules) * APPROX_TOKENS_PER_RULE_SUMMARY


def cmd_format() -> None:
    """Read /query JSON response from stdin, output formatted rule block."""
    try:
        response = json.load(sys.stdin)
    except (json.JSONDecodeError, OSError):
        sys.exit(0)

    rules = response.get("rules", [])
    if not rules:
        sys.exit(0)

    mode = response.get("mode", "standard")
    total = response.get("total_candidates", 0)
    latency = response.get("latency_ms", 0)

    lines = [f"--- WRIT RULES ({len(rules)} rules, {mode} mode) ---", ""]

    for rule in rules:
        rid = rule.get("rule_id", "UNKNOWN")
        severity = rule.get("severity", "?")
        authority = rule.get("authority", "?")
        domain = rule.get("domain", "?")
        score = rule.get("score", 0)

        lines.append(f"[{rid}] ({severity}, {authority}, {domain}) score={score:.3f}")

        trigger = rule.get("trigger", "")
        if trigger:
            lines.append(f"WHEN: {trigger}")

        statement = rule.get("statement", "")
        if statement:
            lines.append(f"RULE: {statement}")

        if mode in ("standard", "full"):
            violation = rule.get("violation", "")
            if violation:
                lines.append(f"VIOLATION: {violation}")
            pass_example = rule.get("pass_example", "")
            if pass_example:
                lines.append(f"CORRECT: {pass_example}")

        if mode == "full":
            rationale = rule.get("rationale", "")
            if rationale:
                lines.append(f"RATIONALE: {rationale}")
            relationships = rule.get("relationships", [])
            if relationships:
                rel_ids = [r.get("rule_id", "?") for r in relationships if isinstance(r, dict)]
                if rel_ids:
                    lines.append(f"RELATED: {', '.join(rel_ids)}")

        lines.append("")

    lines.append("--- END WRIT RULES ---")

    sys.stdout.write("\n".join(lines))
    sys.stdout.write("\n")

    # Also output metadata as JSON on a separate fd for the hook to parse.
    # The hook captures stdout for Claude injection; it parses the last line
    # starting with WRIT_META: for cache updates.
    rule_ids = []
    for rule in rules:
        rid = rule.get("rule_id")
        if rid:
            rule_ids.append(rid)
        for member_id in rule.get("rule_ids", []):
            rule_ids.append(member_id)

    cost = _estimate_cost(rules, mode)
    meta = json.dumps({"rule_ids": rule_ids, "cost": cost})
    sys.stdout.write(f"WRIT_META:{meta}\n")


WRIT_FEEDBACK_URL = "http://localhost:8765/feedback"


def cmd_auto_feedback(session_id: str) -> None:
    """Correlate rules-in-context with analysis outcomes, POST feedback to Writ.

    Logic:
    - If files were written and analysis passed: positive feedback for loaded rules
      whose domain matches the file domains.
    - If analysis failed: negative feedback for loaded rules whose domain matches
      the failed file domains (rules were present but didn't prevent the error).
    - Only send feedback once per rule per session (tracked via feedback_sent).
    """
    import urllib.request
    import urllib.error

    cache = _read_cache(session_id)
    rules = cache.get("loaded_rule_ids", [])
    results = cache.get("analysis_results", {})
    already_sent = set(cache.get("feedback_sent", []))

    if not rules or not results:
        return

    # Map file extensions to domain hints
    pass_domains: set[str] = set()
    fail_domains: set[str] = set()
    for filepath, outcome in results.items():
        ext = os.path.splitext(filepath)[1].lower()
        domain = EXT_TO_DOMAIN.get(ext)
        if domain:
            if outcome == "pass":
                pass_domains.add(domain)
            else:
                fail_domains.add(domain)

    # Map rule IDs to domains (heuristic from prefix)
    rule_domain_map: dict[str, str] = {}
    prefix_to_domain = {
        "PY": "python", "PHP": "php", "JS": "javascript", "TS": "typescript",
        "GO": "go", "RS": "rust", "JAVA": "java", "RB": "ruby",
        "DB": "database", "SQL": "database",
        "ARCH": "architecture", "PERF": "performance", "TEST": "testing",
        "SEC": "security", "ENF": "enforcement", "OPS": "operations",
        "FW": "framework",
    }
    # Universal domains apply to any file type
    universal_domains = {"architecture", "performance", "testing", "security", "enforcement"}

    for rid in rules:
        prefix = rid.split("-")[0] if "-" in rid else rid
        mapped = prefix_to_domain.get(prefix)
        if mapped:
            rule_domain_map[rid] = mapped

    feedback_queue: list[tuple[str, str]] = []  # (rule_id, signal)

    for rid in rules:
        if rid in already_sent:
            continue
        domain = rule_domain_map.get(rid)
        if not domain:
            continue

        # Check if this rule's domain is relevant to files that were written
        is_universal = domain in universal_domains
        relevant_to_pass = is_universal or domain in pass_domains
        relevant_to_fail = is_universal or domain in fail_domains

        if not relevant_to_pass and not relevant_to_fail:
            continue  # rule domain doesn't match any written files

        if relevant_to_pass and pass_domains:
            # Rule's domain had files that passed -- positive signal.
            # Even if some files failed, the rule helped on the passing ones.
            feedback_queue.append((rid, "positive"))
        elif relevant_to_fail and fail_domains and not relevant_to_pass:
            # Rule's domain ONLY had failing files -- negative signal.
            # Rules were in context but didn't prevent errors.
            feedback_queue.append((rid, "negative"))

    # Send feedback to Writ
    sent_count = 0
    for rid, signal in feedback_queue:
        payload = json.dumps({"rule_id": rid, "signal": signal}).encode()
        req = urllib.request.Request(
            WRIT_FEEDBACK_URL,
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            urllib.request.urlopen(req, timeout=0.2)
            already_sent.add(rid)
            sent_count += 1
        except (urllib.error.URLError, OSError):
            break  # Server down, stop trying

    # Update cache with sent feedback
    if sent_count > 0:
        cache["feedback_sent"] = sorted(already_sent)
        _write_cache(session_id, cache)

    report = {
        "feedback_sent": sent_count,
        "positive": sum(1 for _, s in feedback_queue[:sent_count] if s == "positive"),
        "negative": sum(1 for _, s in feedback_queue[:sent_count] if s == "negative"),
        "skipped_already_sent": len([r for r in rules if r in set(cache.get("feedback_sent", [])) - already_sent]),
    }
    json.dump(report, sys.stdout)
    sys.stdout.write("\n")


EXT_TO_DOMAIN = {
    ".py": "python", ".php": "php",
    ".js": "javascript", ".jsx": "javascript",
    ".ts": "typescript", ".tsx": "typescript",
    ".go": "go", ".rs": "rust", ".java": "java", ".rb": "ruby",
    ".sql": "database", ".xml": "xml", ".graphqls": "graphql",
}


def cmd_coverage(session_id: str) -> None:
    """Report coverage: which file domains had rules vs which didn't."""
    cache = _read_cache(session_id)
    files = cache.get("files_written", [])
    rules = cache.get("loaded_rule_ids", [])

    if not files:
        json.dump({"status": "no_files", "message": "No files written this session"}, sys.stdout)
        sys.stdout.write("\n")
        return

    # Map files to domains
    file_domains = set()
    for f in files:
        ext = os.path.splitext(f)[1].lower()
        domain = EXT_TO_DOMAIN.get(ext)
        if domain:
            file_domains.add(domain)

    # Extract domains from rule IDs (heuristic: first segment of rule ID)
    rule_domains = set()
    domain_map = {
        "PY": "python", "PHP": "php", "JS": "javascript", "TS": "typescript",
        "GO": "go", "RS": "rust", "JAVA": "java", "RB": "ruby",
        "DB": "database", "SQL": "database",
        "ARCH": "architecture", "PERF": "performance", "TEST": "testing",
        "SEC": "security", "ENF": "enforcement", "OPS": "operations",
        "FW": "framework",
    }
    for rid in rules:
        prefix = rid.split("-")[0] if "-" in rid else rid
        mapped = domain_map.get(prefix)
        if mapped:
            rule_domains.add(mapped)

    # Always-relevant domains (architecture, performance, testing apply to all files)
    universal = {"architecture", "performance", "testing", "security", "enforcement"}

    covered = file_domains & (rule_domains | universal)
    uncovered = file_domains - covered

    report = {
        "status": "coverage_report",
        "files_written": len(files),
        "rules_loaded": len(rules),
        "file_domains": sorted(file_domains),
        "rule_domains": sorted(rule_domains),
        "covered_domains": sorted(covered),
        "uncovered_domains": sorted(uncovered),
        "coverage_pct": round(len(covered) / len(file_domains) * 100) if file_domains else 100,
    }
    json.dump(report, sys.stdout, indent=2)
    sys.stdout.write("\n")


MAX_CYCLES_BEFORE_ESCALATION = 3


def cmd_add_pending_violation(session_id: str, args: list[str]) -> None:
    """Append a pending violation to the session. Deduplicates by (rule_id, file, line)."""
    cache = _read_cache(session_id)
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

    violations = cache.get("pending_violations", [])
    triple = (rule_id, file, line)
    for v in violations:
        if (v["rule_id"], v["file"], v.get("line")) == triple:
            return  # exact triple already exists

    violations.append({"rule_id": rule_id, "file": file, "line": line, "evidence": evidence})
    cache["pending_violations"] = violations
    _write_cache(session_id, cache)


def cmd_clear_pending_violations(session_id: str) -> None:
    """Clear all pending violations (called at phase-boundary)."""
    cache = _read_cache(session_id)
    cache["pending_violations"] = []
    _write_cache(session_id, cache)


def cmd_invalidate_gate(session_id: str, args: list[str]) -> None:
    """Invalidate a gate: write record, delete .approved file, check escalation.

    Exit 0: success. Exit 1: bad arguments. Exit 2: cache error.
    Caller should run check-escalation afterward to determine next steps.
    """
    gate_name = args[0] if args else ""
    rule_id = file = evidence = trace = plan_hash = ""
    project_root = ""

    i = 1
    while i < len(args):
        if args[i] == "--rule" and i + 1 < len(args):
            rule_id = args[i + 1]; i += 2
        elif args[i] == "--file" and i + 1 < len(args):
            file = args[i + 1]; i += 2
        elif args[i] == "--evidence" and i + 1 < len(args):
            evidence = args[i + 1]; i += 2
        elif args[i] == "--trace" and i + 1 < len(args):
            trace = args[i + 1]; i += 2
        elif args[i] == "--plan-hash" and i + 1 < len(args):
            plan_hash = args[i + 1]; i += 2
        elif args[i] == "--project-root" and i + 1 < len(args):
            project_root = args[i + 1]; i += 2
        else:
            i += 1

    if not gate_name or not rule_id or not file:
        print("Required: <gate_name> --rule <id> --file <path>", file=sys.stderr)
        sys.exit(1)

    try:
        cache = _read_cache(session_id)
    except Exception as e:
        print(f"Cache error: {e}", file=sys.stderr)
        sys.exit(2)

    history = cache.get("invalidation_history", {})
    records = history.get(gate_name, [])
    cycle = len(records) + 1

    records.append({
        "cycle": cycle,
        "rule_id": rule_id,
        "file": file,
        "line": None,
        "evidence": evidence,
        "trace": trace,
        "prior_plan_hash": plan_hash,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    })
    history[gate_name] = records
    cache["invalidation_history"] = history

    # Check escalation threshold
    if cycle >= MAX_CYCLES_BEFORE_ESCALATION:
        rule_ids_in_cycles = [r["rule_id"] for r in records]
        unique_rules = set(rule_ids_in_cycles)
        if len(unique_rules) == 1:
            diagnosis = "same-rule"
        elif len(unique_rules) == len(rule_ids_in_cycles):
            diagnosis = "different-rules"
        else:
            diagnosis = "mixed"
        cache["escalation"] = {
            "gate": gate_name,
            "needed": True,
            "diagnosis": diagnosis,
            "feedback_sent": False,
        }

    try:
        _write_cache(session_id, cache)
    except Exception as e:
        print(f"Cache write error: {e}", file=sys.stderr)
        sys.exit(2)

    # Delete gate file (best-effort -- record already written)
    if project_root:
        gate_file = os.path.join(project_root, ".claude", "gates", f"{gate_name}.approved")
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
    }
    json.dump(result, sys.stdout)
    sys.stdout.write("\n")


def cmd_pending_violations(session_id: str) -> None:
    """Output pending violations as JSON array."""
    cache = _read_cache(session_id)
    json.dump(cache.get("pending_violations", []), sys.stdout)
    sys.stdout.write("\n")


VALID_TIERS = {0, 1, 2, 3}


def cmd_tier(session_id: str, subcmd: str, value_str: str | None = None) -> None:
    """Get or set the task complexity tier (0-3) with up-only enforcement."""
    cache = _read_cache(session_id)

    if subcmd == "get":
        tier = cache.get("tier")
        if tier is not None:
            sys.stdout.write(str(tier))
        sys.stdout.write("\n")
        return

    if subcmd != "set":
        print(f"Unknown tier subcommand: {subcmd}", file=sys.stderr)
        sys.exit(2)

    if value_str is None:
        print("Usage: writ-session.py tier set <0-3> <session_id>", file=sys.stderr)
        sys.exit(2)

    try:
        new_tier = int(value_str)
    except ValueError:
        print(f"Invalid tier value: {value_str} (must be 0-3)", file=sys.stderr)
        sys.exit(1)

    if new_tier not in VALID_TIERS:
        print(f"Invalid tier value: {new_tier} (must be 0-3)", file=sys.stderr)
        sys.exit(1)

    current = cache.get("tier")

    if current is not None:
        if new_tier < current:
            print(
                f"Cannot downgrade tier: {current} -> {new_tier} (escalation is one-way)",
                file=sys.stderr,
            )
            sys.exit(1)
        if new_tier == current:
            sys.stdout.write(f"set: {new_tier}\n")
            return
        sys.stdout.write(f"escalated: {current} -> {new_tier}\n")
    else:
        sys.stdout.write(f"set: {new_tier}\n")

    cache["tier"] = new_tier
    _write_cache(session_id, cache)


def main() -> None:
    if len(sys.argv) < 2:
        print("Usage: writ-session.py <command> [args]", file=sys.stderr)
        print("Commands: read, update, format, should-skip, tier, coverage, auto-feedback", file=sys.stderr)
        sys.exit(2)

    cmd = sys.argv[1]

    if cmd == "read":
        if len(sys.argv) < 3:
            print("Usage: writ-session.py read <session_id>", file=sys.stderr)
            sys.exit(2)
        cmd_read(sys.argv[2])

    elif cmd == "update":
        if len(sys.argv) < 3:
            print("Usage: writ-session.py update <session_id> [--add-rules JSON] [--cost N] [--context-percent N]", file=sys.stderr)
            sys.exit(2)
        cmd_update(sys.argv[2], sys.argv[3:])

    elif cmd == "format":
        cmd_format()

    elif cmd == "should-skip":
        if len(sys.argv) < 3:
            print("Usage: writ-session.py should-skip <session_id> [--threshold N]", file=sys.stderr)
            sys.exit(2)
        threshold = 75
        if "--threshold" in sys.argv:
            idx = sys.argv.index("--threshold")
            if idx + 1 < len(sys.argv):
                threshold = int(sys.argv[idx + 1])
        cmd_should_skip(sys.argv[2], threshold)

    elif cmd == "coverage":
        if len(sys.argv) < 3:
            print("Usage: writ-session.py coverage <session_id>", file=sys.stderr)
            sys.exit(2)
        cmd_coverage(sys.argv[2])

    elif cmd == "tier":
        if len(sys.argv) < 4:
            print("Usage: writ-session.py tier <get|set> <session_id|value> [session_id]", file=sys.stderr)
            sys.exit(2)
        subcmd = sys.argv[2]
        if subcmd == "get":
            cmd_tier(sys.argv[3], "get")
        elif subcmd == "set":
            if len(sys.argv) < 5:
                print("Usage: writ-session.py tier set <0-3> <session_id>", file=sys.stderr)
                sys.exit(2)
            cmd_tier(sys.argv[4], "set", sys.argv[3])
        else:
            print(f"Unknown tier subcommand: {subcmd}", file=sys.stderr)
            sys.exit(2)

    elif cmd == "auto-feedback":
        if len(sys.argv) < 3:
            print("Usage: writ-session.py auto-feedback <session_id>", file=sys.stderr)
            sys.exit(2)
        cmd_auto_feedback(sys.argv[2])

    elif cmd == "add-pending-violation":
        if len(sys.argv) < 3:
            print("Usage: writ-session.py add-pending-violation <session_id> --rule R --file F [--line N] [--evidence E]", file=sys.stderr)
            sys.exit(2)
        cmd_add_pending_violation(sys.argv[2], sys.argv[3:])

    elif cmd == "clear-pending-violations":
        if len(sys.argv) < 3:
            print("Usage: writ-session.py clear-pending-violations <session_id>", file=sys.stderr)
            sys.exit(2)
        cmd_clear_pending_violations(sys.argv[2])

    elif cmd == "invalidate-gate":
        if len(sys.argv) < 4:
            print("Usage: writ-session.py invalidate-gate <session_id> <gate> --rule R --file F [--evidence E] [--trace T] [--plan-hash H] [--project-root P]", file=sys.stderr)
            sys.exit(2)
        cmd_invalidate_gate(sys.argv[2], sys.argv[3:])

    elif cmd == "check-escalation":
        if len(sys.argv) < 3:
            print("Usage: writ-session.py check-escalation <session_id>", file=sys.stderr)
            sys.exit(2)
        cmd_check_escalation(sys.argv[2])

    elif cmd == "pending-violations":
        if len(sys.argv) < 3:
            print("Usage: writ-session.py pending-violations <session_id>", file=sys.stderr)
            sys.exit(2)
        cmd_pending_violations(sys.argv[2])

    else:
        print(f"Unknown command: {cmd}", file=sys.stderr)
        sys.exit(2)


if __name__ == "__main__":
    main()
