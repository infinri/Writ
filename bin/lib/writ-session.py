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

def _log_friction_event(session_id: str, tier: int | None, event: str, **extra: object) -> None:
    """Append a JSON line to workflow-friction.log in the project root."""
    # Find project root
    markers = ['composer.json', 'package.json', 'Cargo.toml', 'go.mod', 'pyproject.toml', '.git']
    path = os.getcwd()
    project_root = ""
    while path != '/':
        if any(os.path.exists(os.path.join(path, m)) for m in markers):
            project_root = path
            break
        path = os.path.dirname(path)
    if not project_root:
        return
    entry = {
        "ts": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "session": session_id,
        "tier": tier,
        "event": event,
        **extra,
    }
    try:
        log_path = os.path.join(project_root, "workflow-friction.log")
        with open(log_path, "a") as f:
            f.write(json.dumps(entry) + "\n")
    except OSError:
        pass


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
        "pretool_queried_files": [],
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
        # Phase 3: centralization fields
        data.setdefault("current_phase", None)
        data.setdefault("gates_approved", [])
        data.setdefault("loaded_rule_ids_by_phase", {})
        data.setdefault("phase_transitions", [])
        data.setdefault("pretool_queried_files", [])
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
            # Flat list (all IDs ever loaded -- for feedback/coverage)
            existing = set(cache["loaded_rule_ids"])
            existing.update(new_ids)
            cache["loaded_rule_ids"] = sorted(existing)
            # Phase-partitioned list (for exclude-list scoping)
            phase = cache.get("current_phase", "unknown")
            by_phase = cache.setdefault("loaded_rule_ids_by_phase", {})
            phase_ids = set(by_phase.get(phase, []))
            phase_ids.update(new_ids)
            by_phase[phase] = sorted(phase_ids)
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
        elif args[i] == "--add-pretool-file" and i + 1 < len(args):
            files = set(cache.get("pretool_queried_files", []))
            files.add(args[i + 1])
            cache["pretool_queried_files"] = sorted(files)
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

# ---------------------------------------------------------------------------
# Phase 3: gate sequences and phase names
# ---------------------------------------------------------------------------

GATE_SEQUENCE_TIER_2 = ["phase-a", "test-skeletons"]
GATE_SEQUENCE_TIER_3 = ["phase-a", "phase-b", "phase-c", "phase-d", "test-skeletons", "gate-final"]

# Maps the last approved gate to the resulting phase name
_PHASE_AFTER_GATE = {
    "phase-a": "testing",      # plan approved, now write test skeletons
    "phase-b": "integration",  # domain invariants done, now integration points
    "phase-c": "testing",      # integration done, now test skeletons (or phase-d)
    "phase-d": "testing",      # concurrency done, now test skeletons
    "test-skeletons": "implementation",
    "gate-final": "complete",
}


def _initial_phase_for_tier(tier: int) -> str:
    if tier == 0:
        return "research"
    if tier == 1:
        return "implementation"
    return "planning"


def _gate_sequence_for_tier(tier: int) -> list[str]:
    if tier == 2:
        return GATE_SEQUENCE_TIER_2
    if tier == 3:
        return GATE_SEQUENCE_TIER_3
    return []


def _next_pending_gate(cache: dict) -> str | None:
    """Return the first gate in the tier's sequence not yet approved."""
    tier = cache.get("tier")
    if tier is None or tier < 2:
        return None
    approved = set(cache.get("gates_approved", []))
    for gate in _gate_sequence_for_tier(tier):
        if gate not in approved:
            return gate
    return None


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

    # Phase 3: set initial phase and log transition
    initial_phase = _initial_phase_for_tier(new_tier)
    old_phase = cache.get("current_phase")
    cache["current_phase"] = initial_phase
    cache.setdefault("phase_transitions", []).append({
        "from": old_phase,
        "to": initial_phase,
        "ts": datetime.now(timezone.utc).isoformat(),
        "trigger": "tier-set",
        "tier": new_tier,
    })

    _write_cache(session_id, cache)


# ---------------------------------------------------------------------------
# Phase 3: centralization commands
# ---------------------------------------------------------------------------

def _parse_file_path_from_envelope(envelope: dict) -> str:
    """Extract file_path from a Claude Code hook stdin envelope."""
    tool_input = envelope.get("tool_input", {})
    if isinstance(tool_input, str):
        try:
            tool_input = json.loads(tool_input)
        except (json.JSONDecodeError, ValueError):
            tool_input = {}
    return tool_input.get("file_path", tool_input.get("path", ""))


def _load_categories(categories_path: str) -> dict:
    """Load gate-categories.json. Returns empty config on error."""
    try:
        with open(categories_path) as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return {"exclusions": [], "categories": [], "framework_detection": {}}


def _glob_match(path: str, pattern: str) -> bool:
    """Bash-style glob: * matches any character including /."""
    import re
    regex = re.escape(pattern).replace(r'\*', '.*').replace(r'\?', '.')
    return bool(re.fullmatch(regex, path))


def _matches_any(path: str, patterns: list[str]) -> bool:
    basename = os.path.basename(path)
    for p in patterns:
        if _glob_match(path, p) or _glob_match(basename, p):
            return True
    return False


def _detect_language(file_path: str) -> str:
    ext_map = {
        '.php': 'php', '.xml': 'xml',
        '.js': 'javascript', '.jsx': 'javascript',
        '.ts': 'typescript', '.tsx': 'typescript',
        '.py': 'python', '.rs': 'rust', '.go': 'go',
        '.java': 'java', '.rb': 'ruby',
        '.graphqls': 'graphql', '.graphql': 'graphql',
    }
    ext = os.path.splitext(file_path)[1]
    return ext_map.get(ext, 'unknown')


def _detect_frameworks(project_root: str, config: dict) -> list[str]:
    """Detect frameworks from project markers or explicit declaration."""
    frameworks: list[str] = []
    explicit_path = os.path.join(project_root, '.claude', 'framework')
    if os.path.isfile(explicit_path):
        with open(explicit_path) as f:
            for line in f:
                fw = line.strip()
                if fw and not fw.startswith('#'):
                    frameworks.append(fw)
    else:
        for fw, markers in config.get('framework_detection', {}).items():
            for marker in markers:
                if os.path.exists(os.path.join(project_root, marker)):
                    frameworks.append(fw)
                    break
    return frameworks


def _detect_project_root(file_path: str) -> str:
    """Walk up from file_path to find the project root."""
    markers = ['composer.json', 'package.json', 'Cargo.toml', 'go.mod', 'pyproject.toml', '.git']
    path = os.path.abspath(file_path)
    while path != '/':
        path = os.path.dirname(path)
        if any(os.path.exists(os.path.join(path, m)) for m in markers):
            return path
    return ''


def cmd_can_write(session_id: str, skill_dir: str = "") -> None:
    """Decide whether a file write is allowed. Reads tool envelope from stdin.

    Absorbs all file classification, gate checking, and tier routing from
    check-gate-approval.sh. The shell hook becomes a thin client.

    Output: JSON {"decision": "allow"} or {"decision": "deny", "reason": "..."}
    """
    import copy
    import re

    raw = sys.stdin.read()
    try:
        envelope = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        envelope = {}

    file_path = _parse_file_path_from_envelope(envelope)
    if not file_path:
        json.dump({"decision": "allow"}, sys.stdout)
        sys.stdout.write("\n")
        return

    # Skip skill infrastructure and global settings
    if skill_dir and file_path.startswith(skill_dir + "/"):
        json.dump({"decision": "allow"}, sys.stdout)
        sys.stdout.write("\n")
        return
    home = os.environ.get("HOME", "")
    if home and file_path.startswith(os.path.join(home, ".claude", "settings")):
        json.dump({"decision": "allow"}, sys.stdout)
        sys.stdout.write("\n")
        return

    cache = _read_cache(session_id)
    tier = cache.get("tier")
    basename = os.path.basename(file_path)
    current_phase = cache.get("current_phase")

    # plan.md exception: allowed pre-tier only
    if basename == "plan.md" and tier is None:
        json.dump({"decision": "allow"}, sys.stdout)
        sys.stdout.write("\n")
        return

    # capabilities.md: always allowed (used to track capability checkboxes post-implementation)
    if basename == "capabilities.md":
        json.dump({"decision": "allow"}, sys.stdout)
        sys.stdout.write("\n")
        return

    # plan.md blocked during implementation phase (Tier 2+)
    if basename == "plan.md" and tier is not None and tier >= 2:
        if current_phase == "implementation":
            json.dump({
                "decision": "deny",
                "reason": "[ENF-GATE-PLAN] plan.md cannot be modified during implementation phase. "
                          "Invalidate the current gate to return to planning if the plan needs changes.",
            }, sys.stdout)
            sys.stdout.write("\n")
            return

    # Tier 0: allow everything
    if tier == 0:
        json.dump({"decision": "allow"}, sys.stdout)
        sys.stdout.write("\n")
        return

    # No tier: deny everything (plan.md handled above)
    if tier is None:
        json.dump({
            "decision": "deny",
            "reason": "[ENF-GATE-TIER] No task tier declared. Classify the tier before writing code. "
                      "Tier 0=Research, 1=Patch, 2=Standard, 3=Complex.",
        }, sys.stdout)
        sys.stdout.write("\n")
        return

    # Tier 1: allow everything (no gates)
    if tier == 1:
        json.dump({"decision": "allow"}, sys.stdout)
        sys.stdout.write("\n")
        return

    # Tier 2-3: category-based gate enforcement
    project_root = _detect_project_root(file_path)
    categories_path = os.path.join(skill_dir, "bin", "lib", "gate-categories.json") if skill_dir else ""
    if not categories_path or not os.path.isfile(categories_path):
        # Fallback: try relative to this script
        categories_path = os.path.join(os.path.dirname(__file__), "gate-categories.json")
    config = _load_categories(categories_path)

    # Check exclusions
    if _matches_any(file_path, config.get('exclusions', [])):
        json.dump({"decision": "allow"}, sys.stdout)
        sys.stdout.write("\n")
        return

    lang = _detect_language(file_path)
    frameworks = _detect_frameworks(project_root, config) if project_root else []
    approved_gates = set(cache.get("gates_approved", []))

    # Tier 2 gate remapping
    if tier == 2:
        config = copy.deepcopy(config)
        for cat in config['categories']:
            if cat['id'] == 'concurrency':
                pass  # keep full sequence to prompt escalation
            elif cat['id'] == 'implementation':
                cat['gate'] = 'test-skeletons'
                cat['prior_gates'] = ['phase-a']
            else:
                cat['gate'] = 'phase-a'
                cat['prior_gates'] = []

    # Classify and gate
    for cat in config['categories']:
        patterns = cat.get('patterns', {})
        matched = False

        if _matches_any(file_path, patterns.get('_any', [])):
            matched = True
        if not matched and lang != 'unknown':
            if _matches_any(file_path, patterns.get(lang, [])):
                matched = True
        if not matched:
            for fw in frameworks:
                if _matches_any(file_path, patterns.get(fw, [])):
                    matched = True
                    break

        if not matched:
            continue

        # Check prior gates
        for prior in cat.get('prior_gates', []):
            if prior not in approved_gates:
                json.dump({
                    "decision": "deny",
                    "reason": f"[ENF-GATE-004] {cat['id']} requires {prior} approval first (sequential gate ordering)",
                }, sys.stdout)
                sys.stdout.write("\n")
                return

        # Check this category's gate
        gate = cat['gate']
        if gate not in approved_gates:
            json.dump({
                "decision": "deny",
                "reason": f"[{cat['rule']}] {cat['message']}",
            }, sys.stdout)
            sys.stdout.write("\n")
            return

    # No category matched or all gates approved
    json.dump({"decision": "allow"}, sys.stdout)
    sys.stdout.write("\n")


def _find_plan_md(project_root: str) -> str | None:
    """Find plan.md, checking project root first then module directories."""
    import glob
    candidates = [os.path.join(project_root, 'plan.md')]
    candidates += glob.glob(os.path.join(project_root, 'app/code/*/*/plan.md'))
    candidates += glob.glob(os.path.join(project_root, 'src/*/plan.md'))
    candidates += glob.glob(os.path.join(project_root, '*/plan.md'))
    found = [c for c in candidates if os.path.isfile(c)]
    if not found:
        return None
    found.sort(key=os.path.getmtime, reverse=True)
    return found[0]


def _validate_phase_a(project_root: str, session_id: str = "") -> str | None:
    """Validate plan.md for phase-a gate. Returns error message or None."""
    import re
    plan_path = _find_plan_md(project_root)
    if not plan_path:
        return ("plan.md not found. Write plan.md with ALL of these sections: "
                "## Files (list every file to create/modify), "
                "## Analysis (what and why, contracts, integration points), "
                "## Rules Applied (cite rule IDs from the injected WRIT RULES block), "
                "## Capabilities (use - [ ] checkbox format for each testable behavior). "
                "All four sections are required in a single write. Do not present partial plans.")
    with open(plan_path) as f:
        content = f.read()
    missing = []
    if not re.search(r'^##\s+Files', content, re.MULTILINE):
        missing.append('## Files')
    if not re.search(r'^##\s+Analysis', content, re.MULTILINE):
        missing.append('## Analysis')
    rules_match = re.search(r'^##\s+Rules\s+[Aa]pplied', content, re.MULTILINE)
    if not rules_match:
        missing.append('## Rules Applied')
    else:
        section_start = rules_match.end()
        rest = content[section_start:]
        next_section = re.search(r'^## ', rest, re.MULTILINE)
        section_text = rest[:next_section.start()] if next_section else rest
        has_rule_id = bool(re.search(r'[A-Z]+-[A-Z]+-\d{3}', section_text))
        has_no_match = bool(re.search(r'[Nn]o matching rules', section_text))
        if not has_rule_id and not has_no_match:
            missing.append('rule ID or "No matching rules" in ## Rules Applied')
        # Validate cited rule IDs against session's loaded_rule_ids
        elif has_rule_id and session_id:
            cited_ids = set(re.findall(r'[A-Z]+-[A-Z]+-\d{3}', section_text))
            cache = _read_cache(session_id)
            # Collect all rule IDs loaded across all phases
            loaded_ids = set(cache.get("loaded_rule_ids", []))
            by_phase = cache.get("loaded_rule_ids_by_phase", {})
            for phase_ids in by_phase.values():
                loaded_ids.update(phase_ids)
            if loaded_ids:
                hallucinated = cited_ids - loaded_ids
                if hallucinated:
                    _log_friction_event(
                        session_id, cache.get("tier"),
                        "hallucinated_rule_ids",
                        cited=sorted(cited_ids),
                        loaded=sorted(loaded_ids),
                        hallucinated=sorted(hallucinated),
                    )
                    missing.append(
                        f'hallucinated rule IDs in ## Rules Applied: {", ".join(sorted(hallucinated))}. '
                        f'Only cite rules from the injected --- WRIT RULES --- block'
                    )
    caps_match = re.search(r'^##\s+Capabilities', content, re.MULTILINE)
    if not caps_match:
        missing.append('## Capabilities (use checkbox format: - [ ] description)')
    else:
        section_start = caps_match.end()
        rest = content[section_start:]
        next_section = re.search(r'^## ', rest, re.MULTILINE)
        section_text = rest[:next_section.start()] if next_section else rest
        if not re.search(r'\[[ x]\]', section_text):
            missing.append('## Capabilities must use checkbox format: - [ ] description (not dashes or bullets)')
        # Capabilities must start unchecked -- pre-checked boxes bypass verification
        elif re.search(r'\[x\]', section_text):
            missing.append('capabilities must start as [ ] (unchecked), not [x]. They are checked after implementation')
    if missing:
        return f"plan.md validation failed: {'; '.join(missing)}. Fix ALL issues in one edit."
    return None


def _validate_plan_section(project_root: str, heading_pattern: str, label: str) -> str | None:
    """Validate that plan.md contains a specific section heading."""
    import re
    plan_path = _find_plan_md(project_root)
    if not plan_path:
        return "plan.md not found"
    with open(plan_path) as f:
        content = f.read()
    if not re.search(heading_pattern, content, re.MULTILINE):
        return f"plan.md missing {label} section"
    return None


def _validate_gate_final(project_root: str) -> str | None:
    """Validate that capabilities.md has all checkboxes checked and all planned files exist."""
    import re
    import glob as globmod

    # Check capabilities.md
    caps_path = os.path.join(project_root, "capabilities.md")
    if not os.path.exists(caps_path):
        # Fall back to plan.md Capabilities section
        plan_path = _find_plan_md(project_root)
        if plan_path:
            with open(plan_path) as f:
                content = f.read()
            caps_match = re.search(r'^##\s+Capabilities', content, re.MULTILINE)
            if caps_match:
                section_start = caps_match.end()
                rest = content[section_start:]
                next_section = re.search(r'^## ', rest, re.MULTILINE)
                section_text = rest[:next_section.start()] if next_section else rest
                unchecked = re.findall(r'\[ \]', section_text)
                if unchecked:
                    return f"gate-final: {len(unchecked)} unchecked capabilities. Update capabilities.md (or plan.md ## Capabilities) to mark completed items as [x]."
        else:
            return "gate-final: neither capabilities.md nor plan.md found."
    else:
        with open(caps_path) as f:
            content = f.read()
        unchecked = re.findall(r'\[ \]', content)
        if unchecked:
            return f"gate-final: {len(unchecked)} unchecked capabilities in capabilities.md. Mark completed items as [x]."

    # Check all planned files exist
    plan_path = _find_plan_md(project_root)
    if plan_path:
        with open(plan_path) as f:
            plan_content = f.read()
        files_match = re.search(r'^##\s+Files', plan_content, re.MULTILINE)
        if files_match:
            section_start = files_match.end()
            rest = plan_content[section_start:]
            next_section = re.search(r'^## ', rest, re.MULTILINE)
            section_text = rest[:next_section.start()] if next_section else rest
            # Extract file paths from backtick-quoted paths or table rows
            paths = re.findall(r'`([^`]+\.\w+)`', section_text)
            missing_files = []
            for p in paths:
                full = os.path.join(project_root, p)
                if not os.path.exists(full):
                    missing_files.append(p)
            if missing_files:
                return f"gate-final: planned files missing: {', '.join(missing_files)}"

    return None


def _validate_test_skeletons(project_root: str) -> str | None:
    """Validate that at least one test file with a method signature exists."""
    import glob
    import re
    test_patterns = [
        '**/Test/**/*Test.php', '**/tests/**/*test*.py', '**/test/**/*test*.py',
        '**/__tests__/**/*.test.*', '**/tests/**/*_test.go', '**/test/**/*_test.rs',
        '**/test_*.py', '**/*.test.ts', '**/*.test.js', '**/*.spec.ts', '**/*.spec.js',
    ]
    method_patterns = [
        r'function\s+test\w+', r'def\s+test_\w+', r'func\s+Test\w+',
        r'fn\s+test_\w+', r'it\s*\(', r'test\s*\(', r'describe\s*\(',
        r'@Test',
    ]
    for pat in test_patterns:
        full = os.path.join(project_root, pat)
        matches = glob.glob(full, recursive=True)
        matches = [m for m in matches if '/vendor/' not in m and '/node_modules/' not in m]
        for match in matches:
            try:
                with open(match) as f:
                    content = f.read()
                for mp in method_patterns:
                    if re.search(mp, content):
                        return None  # found a valid test
            except OSError:
                continue
    return "No test files found with test method signatures."


# Gate -> validation function mapping
_GATE_VALIDATORS: dict[str, object] = {}  # populated after function definitions


def cmd_advance_phase(session_id: str, project_root: str = "", token: str = "") -> None:
    """Validate artifacts and advance to the next phase gate.

    Reads user prompt from stdin (for phase-d skip logic).
    Creates gate file on disk as artifact. Updates session cache as source of truth.
    Clears current-phase loaded_rule_ids. Logs transition to audit trail.

    Requires a --token matching the gate token created by auto-approve-gate.sh.
    This prevents the agent from calling advance-phase directly via Bash.

    Output: JSON {"advanced": true, "gate": "...", "phase": "..."} or
            {"advanced": false, "reason": "..."}
    """
    # Validate caller token
    token_path = os.path.join(tempfile.gettempdir(), f"writ-gate-token-{session_id}")
    expected_token = ""
    try:
        with open(token_path) as f:
            expected_token = f.read().strip()
    except FileNotFoundError:
        pass

    if not token or not expected_token or token != expected_token:
        cache = _read_cache(session_id)
        _log_friction_event(
            session_id, cache.get("tier"),
            "agent_self_approval_blocked",
            had_token=bool(token),
            had_expected=bool(expected_token),
        )
        json.dump({"advanced": False, "reason": "Invalid or missing gate token. Gates can only be advanced by the approval hook, not by the agent."}, sys.stdout)
        sys.stdout.write("\n")
        return

    raw = sys.stdin.read()
    prompt_lower = raw.strip().lower() if raw else ""

    cache = _read_cache(session_id)
    tier = cache.get("tier")

    # No tier or tier < 2: no gates to advance
    if tier is None or tier < 2:
        json.dump({"advanced": False, "reason": "No gates for this tier"}, sys.stdout)
        sys.stdout.write("\n")
        return

    approved = set(cache.get("gates_approved", []))
    gate_sequence = _gate_sequence_for_tier(tier)

    # Find next pending gate
    target_gate = None
    for gate in gate_sequence:
        if gate in approved:
            continue
        # phase-d: skip unless explicitly mentioned
        if gate == "phase-d" and "phase" not in prompt_lower:
            continue
        if gate == "phase-d" and not any(x in prompt_lower for x in ["phase d", "phase-d", "phased"]):
            continue
        target_gate = gate
        break

    if target_gate is None:
        json.dump({"advanced": False, "reason": "All gates already approved"}, sys.stdout)
        sys.stdout.write("\n")
        return

    # Detect project root if not provided
    if not project_root:
        project_root = os.getcwd()
        # Walk up to find project root
        markers = ['composer.json', 'package.json', 'Cargo.toml', 'go.mod', 'pyproject.toml', '.git']
        path = project_root
        while path != '/':
            if any(os.path.exists(os.path.join(path, m)) for m in markers):
                project_root = path
                break
            path = os.path.dirname(path)

    # Validate artifacts for the target gate
    error = None
    if target_gate == "phase-a":
        error = _validate_phase_a(project_root, session_id)
    elif target_gate == "phase-b":
        error = _validate_plan_section(project_root, r'^##\s+Domain\s+Invariants', '## Domain Invariants')
    elif target_gate == "phase-c":
        error = _validate_plan_section(project_root, r'^##\s+Integration\s+Points', '## Integration Points')
    elif target_gate == "phase-d":
        error = _validate_plan_section(project_root, r'^##\s+Concurrency', '## Concurrency')
    elif target_gate == "test-skeletons":
        error = _validate_test_skeletons(project_root)
    elif target_gate == "gate-final":
        error = _validate_gate_final(project_root)

    if error:
        json.dump({"advanced": False, "reason": error, "gate": target_gate}, sys.stdout)
        sys.stdout.write("\n")
        return

    # Validation passed -- update cache
    old_phase = cache.get("current_phase", "planning")
    new_phase = _PHASE_AFTER_GATE.get(target_gate, "implementation")

    approved.add(target_gate)
    cache["gates_approved"] = sorted(approved)
    cache["current_phase"] = new_phase

    # Clear current-phase loaded_rule_ids, move to historical
    by_phase = cache.get("loaded_rule_ids_by_phase", {})
    current_ids = by_phase.get(old_phase, [])
    if current_ids:
        by_phase.setdefault("_historical", []).extend(current_ids)
        by_phase[old_phase] = []
    # Initialize new phase bucket
    by_phase.setdefault(new_phase, [])
    cache["loaded_rule_ids_by_phase"] = by_phase

    # Audit trail
    artifacts = []
    plan_path = _find_plan_md(project_root)
    if plan_path and target_gate != "test-skeletons":
        artifacts.append(os.path.relpath(plan_path, project_root))
    cache.setdefault("phase_transitions", []).append({
        "from": old_phase,
        "to": new_phase,
        "ts": datetime.now(timezone.utc).isoformat(),
        "trigger": "user-approved",
        "tier": tier,
        "gate": target_gate,
        "artifacts_validated": artifacts,
    })

    _write_cache(session_id, cache)

    # Create gate file on disk as artifact (not source of truth)
    gate_dir = os.path.join(project_root, ".claude", "gates")
    os.makedirs(gate_dir, exist_ok=True)
    gate_file = os.path.join(gate_dir, f"{target_gate}.approved")
    with open(gate_file, "w") as f:
        f.write(session_id + "\n")

    json.dump({
        "advanced": True,
        "gate": target_gate,
        "phase": new_phase,
        "from_phase": old_phase,
    }, sys.stdout)
    sys.stdout.write("\n")


def cmd_current_phase(session_id: str) -> None:
    """Return the authoritative current phase from session state.

    Output: JSON {"phase": "...", "tier": N, "gates_approved": [...]}
    """
    cache = _read_cache(session_id)
    tier = cache.get("tier")
    phase = cache.get("current_phase")

    # Derive phase if not set (backward compat with pre-Phase-3 sessions)
    if phase is None and tier is not None:
        phase = _initial_phase_for_tier(tier)

    json.dump({
        "phase": phase or "unclassified",
        "tier": tier,
        "gates_approved": cache.get("gates_approved", []),
    }, sys.stdout)
    sys.stdout.write("\n")


def cmd_metrics(log_path: str = "") -> None:
    """Analyze workflow-friction.log and produce confidence metrics report.

    Reads friction events and computes:
    - Clean run rate (sessions without gate invalidations)
    - Phase transition time statistics (avg, p50, p90)
    - Friction event frequency by type
    - Tier distribution (sessions counted at final tier)
    - Approval pattern miss rate

    Output: JSON to stdout.
    """
    import statistics as _stats

    # Find friction log
    if not log_path:
        markers = ['composer.json', 'package.json', 'Cargo.toml', 'go.mod', 'pyproject.toml', '.git']
        path = os.getcwd()
        while path != '/':
            if any(os.path.exists(os.path.join(path, m)) for m in markers):
                log_path = os.path.join(path, "workflow-friction.log")
                break
            path = os.path.dirname(path)

    if not log_path or not os.path.exists(log_path):
        json.dump({"error": "No friction log found"}, sys.stdout)
        sys.stdout.write("\n")
        sys.exit(1)

    # Parse events
    events: list[dict] = []
    try:
        with open(log_path) as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith('#'):
                    continue
                try:
                    events.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    except OSError:
        json.dump({"error": f"Cannot read {log_path}"}, sys.stdout)
        sys.stdout.write("\n")
        sys.exit(1)

    if not events:
        json.dump({"error": "No events in friction log"}, sys.stdout)
        sys.stdout.write("\n")
        sys.exit(1)

    # Group by session
    sessions: dict[str, list[dict]] = {}
    for e in events:
        sid = e.get("session", "unknown")
        sessions.setdefault(sid, []).append(e)

    total_sessions = len(sessions)

    # 1. Clean run rate
    sessions_with_invalidations = {
        e.get("session") for e in events
        if e.get("event") == "gate_denied_then_approved"
    }
    clean_sessions = total_sessions - len(sessions_with_invalidations)
    clean_run_rate = round(clean_sessions / total_sessions * 100, 1) if total_sessions > 0 else None

    # 2. Phase transition times
    transition_times = [
        e["elapsed_seconds"] for e in events
        if e.get("event") == "phase_transition_time" and "elapsed_seconds" in e
    ]

    transition_stats = None
    if transition_times:
        sorted_times = sorted(transition_times)
        n = len(sorted_times)
        transition_stats = {
            "count": n,
            "avg": round(_stats.mean(sorted_times), 1),
            "p50": sorted_times[n // 2],
            "p90": sorted_times[int(n * 0.9)] if n >= 10 else sorted_times[-1],
            "min": sorted_times[0],
            "max": sorted_times[-1],
        }

    # 3. Event frequency by type
    known_types = [
        "approval_pattern_miss",
        "gate_denied_then_approved",
        "tier_escalated",
        "phase_transition_time",
        "phase_transition",
        "hallucinated_rule_ids",
        "agent_self_approval_blocked",
    ]
    event_frequency: dict[str, int] = {t: 0 for t in known_types}
    for e in events:
        evt = e.get("event", "unknown")
        event_frequency[evt] = event_frequency.get(evt, 0) + 1

    # 4. Tier distribution (each session counted at final tier)
    tier_distribution: dict[str, int] = {"0": 0, "1": 0, "2": 0, "3": 0}
    session_final_tier: dict[str, int] = {}
    for e in events:
        sid = e.get("session", "unknown")
        tier = e.get("tier")
        if tier is not None:
            current = session_final_tier.get(sid)
            if current is None or tier > current:
                session_final_tier[sid] = tier
    for tier in session_final_tier.values():
        tier_distribution[str(tier)] = tier_distribution.get(str(tier), 0) + 1

    # 5. Approval pattern miss rate
    miss_count = event_frequency.get("approval_pattern_miss", 0)
    transition_count = event_frequency.get("phase_transition", 0)
    total_approval_attempts = miss_count + transition_count
    approval_miss_rate = (
        round(miss_count / total_approval_attempts * 100, 1)
        if total_approval_attempts > 0 else None
    )

    report = {
        "total_sessions": total_sessions,
        "total_events": len(events),
        "clean_run_rate": clean_run_rate,
        "transition_times": transition_stats,
        "event_frequency": event_frequency,
        "tier_distribution": tier_distribution,
        "approval_miss_rate": approval_miss_rate,
    }
    json.dump(report, sys.stdout, indent=2)
    sys.stdout.write("\n")


def main() -> None:
    if len(sys.argv) < 2:
        print("Usage: writ-session.py <command> [args]", file=sys.stderr)
        print("Commands: read, update, format, should-skip, tier, coverage, auto-feedback, can-write, advance-phase, current-phase, metrics", file=sys.stderr)
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

    # Phase 3: centralization commands
    elif cmd == "can-write":
        if len(sys.argv) < 3:
            print("Usage: writ-session.py can-write <session_id> [--skill-dir PATH]", file=sys.stderr)
            sys.exit(2)
        skill_dir = ""
        if "--skill-dir" in sys.argv:
            idx = sys.argv.index("--skill-dir")
            if idx + 1 < len(sys.argv):
                skill_dir = sys.argv[idx + 1]
        cmd_can_write(sys.argv[2], skill_dir)

    elif cmd == "advance-phase":
        if len(sys.argv) < 3:
            print("Usage: writ-session.py advance-phase <session_id> [--project-root PATH] [--token TOKEN]", file=sys.stderr)
            sys.exit(2)
        project_root = ""
        if "--project-root" in sys.argv:
            idx = sys.argv.index("--project-root")
            if idx + 1 < len(sys.argv):
                project_root = sys.argv[idx + 1]
        token = ""
        if "--token" in sys.argv:
            idx = sys.argv.index("--token")
            if idx + 1 < len(sys.argv):
                token = sys.argv[idx + 1]
        cmd_advance_phase(sys.argv[2], project_root, token)

    elif cmd == "current-phase":
        if len(sys.argv) < 3:
            print("Usage: writ-session.py current-phase <session_id>", file=sys.stderr)
            sys.exit(2)
        cmd_current_phase(sys.argv[2])

    elif cmd == "metrics":
        log_path = ""
        if "--log" in sys.argv:
            idx = sys.argv.index("--log")
            if idx + 1 < len(sys.argv):
                log_path = sys.argv[idx + 1]
        cmd_metrics(log_path)

    else:
        print(f"Unknown command: {cmd}", file=sys.stderr)
        sys.exit(2)


if __name__ == "__main__":
    main()
