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

def _log_friction_event(session_id: str, mode: str | None, event: str, **extra: object) -> None:
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
        "mode": mode,
        "event": event,
        **extra,
    }
    try:
        log_path = os.path.join(project_root, "workflow-friction.log")
        with open(log_path, "a") as f:
            f.write(json.dumps(entry) + "\n")
    except OSError:
        pass


# Per ARCH-DRY-001: budget constants load from the canonical JSON shared with
# writ/retrieval/session.py. Single source of truth. stdlib only.
_BUDGET_JSON = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "writ", "shared", "budget.json",
)
with open(_BUDGET_JSON) as _budget_file:
    _budget_data = json.load(_budget_file)
DEFAULT_SESSION_BUDGET = _budget_data["default_budget"]
APPROX_TOKENS_PER_RULE_FULL = _budget_data["rule_cost_full"]
APPROX_TOKENS_PER_RULE_STANDARD = _budget_data["rule_cost_standard"]
APPROX_TOKENS_PER_RULE_SUMMARY = _budget_data["rule_cost_summary"]

CACHE_DIR = os.environ.get("WRIT_CACHE_DIR", tempfile.gettempdir())


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
        "mode": None,
        "is_subagent": False,
        "files_written": [],
        "analysis_results": {},
        "feedback_sent": [],
        "pending_violations": [],
        "invalidation_history": {},
        "escalation": {"gate": None, "needed": False, "diagnosis": None, "feedback_sent": False},
        "pretool_queried_files": [],
        "paused_work_state": None,
        "failed_writes": [],
        "is_orchestrator": False,
        "last_injected_rule_ids": [],
        "detected_domain": None,
        "instructions_rule_ids": [],
    }
    if not os.path.exists(path):
        return default
    try:
        with open(path) as f:
            data = json.load(f)
        data.setdefault("mode", None)
        data.setdefault("is_subagent", False)
        data.setdefault("files_written", [])
        data.setdefault("analysis_results", {})
        data.setdefault("feedback_sent", [])
        data.setdefault("loaded_rules", [])
        data.setdefault("pending_violations", [])
        data.setdefault("invalidation_history", {})
        data.setdefault("escalation", {"gate": None, "needed": False, "diagnosis": None, "feedback_sent": False})
        data.setdefault("current_phase", None)
        data.setdefault("gates_approved", [])
        data.setdefault("loaded_rule_ids_by_phase", {})
        data.setdefault("phase_transitions", [])
        data.setdefault("pretool_queried_files", [])
        data.setdefault("paused_work_state", None)
        data.setdefault("failed_writes", [])
        data.setdefault("is_orchestrator", False)
        data.setdefault("last_injected_rule_ids", [])
        data.setdefault("detected_domain", None)
        data.setdefault("instructions_rule_ids", [])
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
        elif args[i] == "--token-snapshot" and i + 1 < len(args):
            snapshot_data = json.loads(args[i + 1])
            snapshot_data["ts"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            snapshot_data["phase"] = cache.get("current_phase")
            snapshot_data["mode"] = cache.get("mode")
            cache.setdefault("token_snapshots", []).append(snapshot_data)
            i += 2
        elif args[i] == "--add-failed-write" and i + 1 < len(args):
            record = json.loads(args[i + 1])
            cache.setdefault("failed_writes", []).append(record)
            i += 2
        else:
            i += 1

    _write_cache(session_id, cache)


def cmd_should_skip(session_id: str, threshold: int = 75) -> bool:
    """Return True if the caller should skip its RAG query.

    Sub-agents (is_subagent=True) are NEVER skipped by this check — they get
    unlimited rule injection budget. Master sessions honor both budget and
    context-pressure thresholds.

    Returns a bool for programmatic callers; when invoked from the shell
    dispatcher, the bool is translated into exit codes (0 = skip, 1 = proceed).
    """
    cache = _read_cache(session_id)
    if cache.get("is_subagent"):
        return False  # sub-agents: unlimited budget, never skip
    if cache["remaining_budget"] <= 0:
        return True  # skip: budget exhausted
    if cache["context_percent"] >= threshold:
        return True  # skip: context pressure
    return False  # proceed


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


def cmd_detect_compaction(session_id: str, current_context_percent: int) -> dict:
    """Detect context window compaction via context_percent drop.

    Compares the previous context_percent snapshot in the session cache with the
    current value. A drop of >20% triggers recovery: clearing the current phase's
    exclusion list and resetting remaining_budget to DEFAULT_SESSION_BUDGET.

    Returns a dict with {compacted, context_drop_percent, rules_cleared}.
    """
    cache = _read_cache(session_id)
    previous_pct = cache.get("context_percent", 0)
    drop = previous_pct - current_context_percent

    if drop > 20:
        current_phase = cache.get("current_phase", "unknown")
        by_phase = cache.get("loaded_rule_ids_by_phase", {})
        rules_cleared = list(by_phase.get(current_phase, []))

        # Clear exclusion list for current phase only
        by_phase[current_phase] = []
        cache["loaded_rule_ids_by_phase"] = by_phase

        # Reset budget
        cache["remaining_budget"] = DEFAULT_SESSION_BUDGET

        # Clear sticky rules preference (stale after compaction)
        cache["last_injected_rule_ids"] = []

        _write_cache(session_id, cache)

        # Log friction event
        _log_friction_event(
            session_id, cache.get("mode"), "compaction_detected",
            context_drop_percent=drop,
            previous_context_percent=previous_pct,
            current_context_percent=current_context_percent,
            rules_cleared=rules_cleared,
            phase=current_phase,
        )

        result = {
            "compacted": True,
            "context_drop_percent": drop,
            "rules_cleared": rules_cleared,
        }
        json.dump(result, sys.stdout)
        sys.stdout.write("\n")
        return result

    result = {
        "compacted": False,
        "context_drop_percent": drop,
    }
    json.dump(result, sys.stdout)
    sys.stdout.write("\n")
    return result


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


def cmd_clear_rules_for_compaction(session_id: str) -> None:
    """Clear loaded_rules (full objects) from cache, keep IDs. For PreCompact hook."""
    cache = _read_cache(session_id)
    rules = cache.get("loaded_rules", [])
    rules_cleared = len(rules)
    bytes_freed = rules_cleared * APPROX_TOKENS_PER_RULE_FULL  # ~200 tokens per rule object
    cache["loaded_rules"] = []
    _write_cache(session_id, cache)
    _log_friction_event(
        session_id, cache.get("mode"), "pre_compaction",
        rules_cleared=rules_cleared, bytes_freed=bytes_freed,
    )
    result = {"rules_cleared": rules_cleared, "bytes_freed": bytes_freed}
    json.dump(result, sys.stdout)
    sys.stdout.write("\n")


def cmd_reset_after_compaction(session_id: str) -> None:
    """Clear current phase's exclusion list and reset budget. For PostCompact hook."""
    cache = _read_cache(session_id)
    current_phase = cache.get("current_phase", "unknown")
    by_phase = cache.get("loaded_rule_ids_by_phase", {})
    cleared = list(by_phase.get(current_phase, []))
    by_phase[current_phase] = []
    cache["loaded_rule_ids_by_phase"] = by_phase
    cache["remaining_budget"] = DEFAULT_SESSION_BUDGET
    # Clear sticky rules preference (stale after compaction)
    cache["last_injected_rule_ids"] = []
    _write_cache(session_id, cache)
    _log_friction_event(
        session_id, cache.get("mode"), "post_compaction",
        rules_cleared=cleared, budget_reset=True, phase=current_phase,
    )
    result = {"rules_cleared": cleared, "budget_reset": True}
    json.dump(result, sys.stdout)
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


# ---------------------------------------------------------------------------
# v2: mode-based workflow
# ---------------------------------------------------------------------------

VALID_MODES = {"conversation", "debug", "review", "work"}

GATE_SEQUENCE_WORK = ["phase-a", "test-skeletons"]

_PHASE_AFTER_GATE_WORK = {
    "phase-a": "testing",
    "test-skeletons": "implementation",
}

def _initial_phase_for_mode(mode: str | None) -> str | None:
    if mode == "work":
        return "planning"
    return None


def _gate_sequence_for_mode(mode: str | None) -> list[str]:
    if mode == "work":
        return GATE_SEQUENCE_WORK
    return []


def _next_pending_gate(cache: dict) -> str | None:
    """Return the first gate in the mode's sequence not yet approved."""
    mode = cache.get("mode")
    if mode != "work":
        return None
    approved = set(cache.get("gates_approved", []))
    for gate in _gate_sequence_for_mode(mode):
        if gate not in approved:
            return gate
    return None


def _mode_set(session_id: str, mode: str, is_orchestrator: bool = False) -> None:
    """Set mode with fresh state. Internal -- called by cmd_mode."""
    cache = _read_cache(session_id)
    old_mode = cache.get("mode")
    old_phase = cache.get("current_phase")

    cache["mode"] = mode
    if is_orchestrator:
        cache["is_orchestrator"] = True

    # Fresh workflow state
    new_phase = _initial_phase_for_mode(mode)
    cache["current_phase"] = new_phase
    cache["gates_approved"] = []
    cache["paused_work_state"] = None
    cache["denial_counts"] = {}

    # Audit trail -- skip no-op transitions (e.g. repeated mode set work)
    if old_phase != new_phase:
        cache.setdefault("phase_transitions", []).append({
            "from": old_phase,
            "to": new_phase,
            "ts": datetime.now(timezone.utc).isoformat(),
            "trigger": "mode-set",
            "mode": mode,
        })

    _write_cache(session_id, cache)
    _log_friction_event(
        session_id, mode, "mode_change",
        change_type="set", from_mode=old_mode, to_mode=mode,
    )


def _mode_switch(session_id: str, mode: str) -> None:
    """Switch mode, preserving Work state if leaving/returning."""
    cache = _read_cache(session_id)
    old_mode = cache.get("mode")
    old_phase = cache.get("current_phase")
    restored = False

    # Save Work state when leaving Work
    if old_mode == "work" and mode != "work":
        cache["paused_work_state"] = {
            "phase": cache.get("current_phase"),
            "gates_approved": cache.get("gates_approved", []),
            "loaded_rule_ids_by_phase": cache.get("loaded_rule_ids_by_phase", {}),
        }

    # Restore Work state when returning to Work
    if mode == "work" and cache.get("paused_work_state"):
        paused = cache["paused_work_state"]
        cache["current_phase"] = paused["phase"]
        cache["gates_approved"] = paused["gates_approved"]
        cache["loaded_rule_ids_by_phase"] = paused.get("loaded_rule_ids_by_phase", {})
        cache["paused_work_state"] = None
        new_phase = paused["phase"]
        restored = True
    elif mode == "work":
        # No paused state -- fresh start
        cache["current_phase"] = "planning"
        cache["gates_approved"] = []
        new_phase = "planning"
    else:
        cache["current_phase"] = None
        new_phase = None

    cache["mode"] = mode

    # Audit trail -- collapse restore into single event, skip no-ops
    if restored:
        cache.setdefault("phase_transitions", []).append({
            "from": old_phase,
            "to": new_phase,
            "ts": datetime.now(timezone.utc).isoformat(),
            "trigger": "mode-switch-restore",
            "mode": mode,
        })
    elif old_phase != new_phase:
        cache.setdefault("phase_transitions", []).append({
            "from": old_phase,
            "to": new_phase,
            "ts": datetime.now(timezone.utc).isoformat(),
            "trigger": "mode-switch",
            "mode": mode,
        })

    _write_cache(session_id, cache)
    _log_friction_event(
        session_id, mode, "mode_change",
        change_type="switch", from_mode=old_mode, to_mode=mode,
    )


def cmd_mode(session_id: str, subcmd: str, value: str | None = None, is_orchestrator: bool = False) -> None:
    """Get, set, or switch the session mode."""
    if subcmd == "get":
        cache = _read_cache(session_id)
        mode = cache.get("mode")
        if mode:
            sys.stdout.write(mode)
        sys.stdout.write("\n")
        return

    if subcmd not in ("set", "switch"):
        print(f"Unknown mode subcommand: {subcmd}", file=sys.stderr)
        sys.exit(2)

    if value is None:
        print(f"Usage: writ-session.py mode {subcmd} <conversation|debug|review|work> <session_id>", file=sys.stderr)
        sys.exit(2)

    mode = value.lower()
    if mode not in VALID_MODES:
        print(f"Invalid mode: {value} (must be one of: {', '.join(sorted(VALID_MODES))})", file=sys.stderr)
        sys.exit(1)

    if subcmd == "set":
        _mode_set(session_id, mode, is_orchestrator=is_orchestrator)
        sys.stdout.write(f"set: {mode}\n")
    else:
        _mode_switch(session_id, mode)
        sys.stdout.write(f"switch: {mode}\n")


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


def _log_gate_denial(session_id: str, cache: dict, gate: str, file_path: str, reason: str) -> None:
    """Log gate_denial, write_attempt, and repeated_denial events. Update denial_counts."""
    mode = cache.get("mode")
    phase = cache.get("current_phase")

    # Update denial_counts in cache
    denial_counts = cache.get("denial_counts", {})
    denial_counts[gate] = denial_counts.get(gate, 0) + 1
    cache["denial_counts"] = denial_counts
    _write_cache(session_id, cache)

    count = denial_counts[gate]

    _log_friction_event(session_id, mode, "write_attempt",
                        file_path=file_path, result="deny", gate_status=gate, phase=phase)
    _log_friction_event(session_id, mode, "gate_denial",
                        file_path=file_path, gate=gate, denial_count=count, phase=phase)

    if count > 1:
        _log_friction_event(session_id, mode, "repeated_denial",
                            gate=gate, denial_count=count, file_path=file_path, phase=phase)


def _can_write_check(session_id: str, envelope: dict, skill_dir: str = "") -> dict:
    """Reusable gate check logic. Returns {"can_write": bool, "reason": str|None}.

    Used by both cmd_can_write (CLI) and /pre-write-check (HTTP endpoint).
    """
    file_path = _parse_file_path_from_envelope(envelope)
    if not file_path:
        return {"can_write": True, "reason": None}

    # Skip skill infrastructure and global settings
    if skill_dir and file_path.startswith(skill_dir + "/"):
        return {"can_write": True, "reason": None}
    home = os.environ.get("HOME", "")
    if home and file_path.startswith(os.path.join(home, ".claude", "settings")):
        return {"can_write": True, "reason": None}

    cache = _read_cache(session_id)
    mode = cache.get("mode")
    basename = os.path.basename(file_path)
    current_phase = cache.get("current_phase")

    # Sub-agents bypass mode/gate checks. They are workers dispatched by an
    # orchestrator that already passed the human-approval gate; their scope
    # is narrowed by the agent definition + spawn prompt. Gates exist to stop
    # the master from writing code before plan approval, not to re-police
    # workers the orchestrator has already sanctioned. See rules/writ-orchestrator.md.
    if cache.get("is_subagent"):
        _log_friction_event(session_id, mode, "write_attempt",
                            file_path=file_path, result="allow",
                            gate_status="subagent_bypass")
        return {"can_write": True, "reason": None}

    # plan.md exception: allowed pre-mode
    if basename == "plan.md" and mode is None:
        return {"can_write": True, "reason": None}

    # capabilities.md: always allowed
    if basename == "capabilities.md":
        return {"can_write": True, "reason": None}

    # plan.md in Work mode: allowed during planning/testing, blocked during implementation
    if basename == "plan.md" and mode == "work":
        if current_phase == "implementation":
            return {
                "can_write": False,
                "reason": "[ENF-GATE-PLAN] plan.md cannot be modified during implementation phase. "
                          "Invalidate the current gate to return to planning if the plan needs changes.",
            }
        return {"can_write": True, "reason": None}

    # No mode: deny everything (plan.md handled above)
    if mode is None:
        return {
            "can_write": False,
            "reason": "[ENF-GATE-MODE] No mode declared. Set a mode before writing code. "
                      "Modes: conversation, debug, review, work.",
        }

    # Non-work modes: allow all writes (no gates)
    if mode != "work":
        _log_friction_event(session_id, mode, "write_attempt",
                            file_path=file_path, result="allow", gate_status="no_gates")
        return {"can_write": True, "reason": None}

    # Work mode: two-gate enforcement
    categories_path = os.path.join(skill_dir, "bin", "lib", "gate-categories.json") if skill_dir else ""
    if not categories_path or not os.path.isfile(categories_path):
        categories_path = os.path.join(os.path.dirname(__file__), "gate-categories.json")
    config = _load_categories(categories_path)

    if _matches_any(file_path, config.get('exclusions', [])):
        _log_friction_event(session_id, mode, "write_attempt",
                            file_path=file_path, result="allow", gate_status="excluded")
        return {"can_write": True, "reason": None}

    approved_gates = set(cache.get("gates_approved", []))

    if "phase-a" not in approved_gates:
        reason = (
            "[ENF-GATE-PLAN] ALL writes blocked -- plan not yet approved. "
            "DO NOT attempt more writes.\n"
            "Present your plan to the user and say: \"Say approved to proceed.\"\n"
            "Wait for the user to say \"approved\" before attempting ANY file writes."
        )
        _log_gate_denial(session_id, cache, "phase-a", file_path, reason)
        return {"can_write": False, "reason": reason}

    if "test-skeletons" not in approved_gates:
        reason = (
            "[ENF-GATE-TEST] ALL writes blocked -- test skeletons not yet approved. "
            "DO NOT attempt more writes.\n"
            "Write test skeleton files first (test files ARE allowed), "
            "present them to the user, and say: \"Say approved to proceed.\""
        )
        _log_gate_denial(session_id, cache, "test-skeletons", file_path, reason)
        return {"can_write": False, "reason": reason}

    # Both gates approved
    _log_friction_event(session_id, mode, "write_attempt",
                        file_path=file_path, result="allow", gate_status="all_approved",
                        phase=current_phase)
    return {"can_write": True, "reason": None}


def cmd_can_write(session_id: str, skill_dir: str = "") -> None:
    """Decide whether a file write is allowed. Reads tool envelope from stdin.

    Gating rules:
    - Sub-agents (is_subagent=True): allow all writes. Workers are dispatched
      by an orchestrator that already cleared the human-approval gate.
    - No mode (master): deny all except plan.md and capabilities.md
    - conversation/debug/review (master): allow all (no gates)
    - work (master): two-gate enforcement (phase-a + test-skeletons)

    Output: JSON {"decision": "allow"} or {"decision": "deny", "reason": "..."}
    """
    raw = sys.stdin.read()
    try:
        envelope = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        envelope = {}

    result = _can_write_check(session_id, envelope, skill_dir)

    if result["can_write"]:
        json.dump({"decision": "allow"}, sys.stdout)
        sys.stdout.write("\n")
    else:
        json.dump({"decision": "deny", "reason": result["reason"]}, sys.stdout)
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
        has_rule_id = bool(re.search(r'[A-Z][A-Z0-9]+(?:-[A-Z][A-Z0-9]+)*-\d{3}', section_text))
        has_no_match = bool(re.search(r'[Nn]o matching rules', section_text))
        if not has_rule_id and not has_no_match:
            missing.append('rule ID or "No matching rules" in ## Rules Applied')
        # Validate cited rule IDs against session's loaded_rule_ids
        elif has_rule_id and session_id:
            cited_ids = set(re.findall(r'[A-Z][A-Z0-9]+(?:-[A-Z][A-Z0-9]+)*-\d{3}', section_text))
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
                        session_id, cache.get("mode"),
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


def _validate_test_skeletons(project_root: str, session_id: str = "") -> str | None:
    """Validate that at least one test file with a method signature was written this session.

    Checks files_written in the session cache first. If session tracking is available,
    only files written this session count. Falls back to scanning the project if no
    session is provided.
    """
    import re

    method_patterns = [
        r'function\s+test\w+', r'def\s+test_\w+', r'func\s+Test\w+',
        r'fn\s+test_\w+', r'it\s*\(', r'test\s*\(', r'describe\s*\(',
        r'@Test',
    ]

    test_path_patterns = [
        r'/Test/', r'/tests/', r'/test/', r'/__tests__/',
        r'Test\.php$', r'test_.*\.py$', r'_test\.go$', r'_test\.rs$',
        r'\.test\.[jt]sx?$', r'\.spec\.[jt]sx?$',
    ]

    # Check session-tracked files first
    if session_id:
        cache = _read_cache(session_id)
        files_written = cache.get("files_written", [])
        for filepath in files_written:
            if not any(re.search(p, filepath) for p in test_path_patterns):
                continue
            if not os.path.isfile(filepath):
                continue
            try:
                with open(filepath) as f:
                    content = f.read()
                for mp in method_patterns:
                    if re.search(mp, content):
                        return None  # found a valid session test
            except OSError:
                continue

    # Fallback: scan project for test files (excludes vendor/node_modules)
    import glob
    file_patterns = [
        '**/Test/**/*Test.php', '**/tests/**/*test*.py', '**/test/**/*test*.py',
        '**/__tests__/**/*.test.*', '**/tests/**/*_test.go', '**/test/**/*_test.rs',
        '**/test_*.py', '**/*.test.ts', '**/*.test.js', '**/*.spec.ts', '**/*.spec.js',
    ]
    for pat in file_patterns:
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
    return "No test files found with test method signatures. Write test skeleton files to disk before requesting approval."


# Gate -> validation function mapping
_GATE_VALIDATORS: dict[str, object] = {}  # populated after function definitions


def cmd_advance_phase(session_id: str, project_root: str = "", token: str = "") -> None:
    """Validate artifacts and advance to the next phase gate.

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
            session_id, cache.get("mode"),
            "agent_self_approval_blocked",
            had_token=bool(token),
            had_expected=bool(expected_token),
        )
        json.dump({"advanced": False, "reason": "Invalid or missing gate token. Gates can only be advanced by the approval hook, not by the agent."}, sys.stdout)
        sys.stdout.write("\n")
        return

    # Consume stdin (hooks may pipe prompt text)
    sys.stdin.read()

    cache = _read_cache(session_id)
    mode = cache.get("mode")

    # Only Work mode has gates
    if mode != "work":
        json.dump({"advanced": False, "reason": "No gates for this mode"}, sys.stdout)
        sys.stdout.write("\n")
        return

    approved = set(cache.get("gates_approved", []))
    gate_sequence = GATE_SEQUENCE_WORK

    # Find next pending gate
    target_gate = None
    for gate in gate_sequence:
        if gate not in approved:
            target_gate = gate
            break

    if target_gate is None:
        json.dump({"advanced": False, "reason": "All gates already approved"}, sys.stdout)
        sys.stdout.write("\n")
        return

    # Detect project root if not provided
    if not project_root:
        project_root = os.getcwd()
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
    elif target_gate == "test-skeletons":
        error = _validate_test_skeletons(project_root, session_id)

    if error:
        json.dump({"advanced": False, "reason": error, "gate": target_gate}, sys.stdout)
        sys.stdout.write("\n")
        return

    # Validation passed -- update cache
    old_phase = cache.get("current_phase", "planning")
    new_phase = _PHASE_AFTER_GATE_WORK.get(target_gate, "implementation")

    approved.add(target_gate)
    cache["gates_approved"] = sorted(approved)
    cache["current_phase"] = new_phase

    # Reset denial counts for the advanced gate
    denial_counts = cache.get("denial_counts", {})
    denial_counts.pop(target_gate, None)
    cache["denial_counts"] = denial_counts

    # Clear current-phase loaded_rule_ids, move to historical
    by_phase = cache.get("loaded_rule_ids_by_phase", {})
    current_ids = by_phase.get(old_phase, [])
    if current_ids:
        by_phase.setdefault("_historical", []).extend(current_ids)
        by_phase[old_phase] = []
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
        "mode": mode,
        "gate": target_gate,
        "artifacts_validated": artifacts,
    })

    _write_cache(session_id, cache)

    # Log phase_token_summary from accumulated token snapshots
    snapshots = cache.get("token_snapshots", [])
    phase_snapshots = [s for s in snapshots if s.get("phase") == old_phase]
    if phase_snapshots:
        pcts = [s.get("context_percent", 0) for s in phase_snapshots]
        tokens = [s.get("context_tokens", 0) for s in phase_snapshots]
        _log_friction_event(
            session_id, mode, "phase_token_summary",
            phase=old_phase, snapshot_count=len(phase_snapshots),
            peak_context_percent=max(pcts) if pcts else 0,
            peak_context_tokens=max(tokens) if tokens else 0,
            final_context_percent=pcts[-1] if pcts else 0,
            final_context_tokens=tokens[-1] if tokens else 0,
        )

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

    Output: JSON {"phase": "...", "mode": "...", "gates_approved": [...]}
    """
    cache = _read_cache(session_id)
    mode = cache.get("mode")
    phase = cache.get("current_phase")

    # Derive phase if not set
    if phase is None and mode is not None:
        phase = _initial_phase_for_mode(mode)

    json.dump({
        "phase": phase or "unclassified",
        "mode": mode,
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
        "approval_pattern_match",
        "gate_denied_then_approved",
        "gate_denial",
        "repeated_denial",
        "write_attempt",
        "mode_change",
        "phase_transition_time",
        "phase_transition",
        "phase_token_summary",
        "hallucinated_rule_ids",
        "agent_self_approval_blocked",
        "tier_escalated",
        "exitplanmode_denial",
        "exitplanmode_allow",
        "rag_query",
        "hook_execution",
        "token_snapshot",
        "subagent_start",
        "subagent_complete",
    ]
    event_frequency: dict[str, int] = {t: 0 for t in known_types}
    for e in events:
        evt = e.get("event", "unknown")
        event_frequency[evt] = event_frequency.get(evt, 0) + 1

    # 4. Mode distribution (each session counted at latest mode)
    # Legacy tier events are mapped: 0->conversation, 1/2/3->work
    _legacy_tier_to_mode = {0: "conversation", 1: "work", 2: "work", 3: "work"}
    mode_distribution: dict[str, int] = {m: 0 for m in VALID_MODES}
    session_final_mode: dict[str, str] = {}
    for e in events:
        sid = e.get("session", "unknown")
        mode = e.get("mode")
        if mode is None:
            # Legacy event with tier field
            tier = e.get("tier")
            if tier is not None:
                mode = _legacy_tier_to_mode.get(tier)
        if mode:
            session_final_mode[sid] = mode
    for mode in session_final_mode.values():
        mode_distribution[mode] = mode_distribution.get(mode, 0) + 1

    # 5. Approval pattern miss rate
    miss_count = event_frequency.get("approval_pattern_miss", 0)
    transition_count = event_frequency.get("phase_transition", 0)
    total_approval_attempts = miss_count + transition_count
    approval_miss_rate = (
        round(miss_count / total_approval_attempts * 100, 1)
        if total_approval_attempts > 0 else None
    )

    # 6. Token metrics (from phase_token_summary and token_snapshot events)
    phase_summaries = [e for e in events if e.get("event") == "phase_token_summary"]
    token_snapshots = [e for e in events if e.get("event") == "token_snapshot"]
    token_metrics = None
    if phase_summaries or token_snapshots:
        token_metrics = {}
        # Per-phase peaks from phase_token_summary events
        for ps in phase_summaries:
            phase = ps.get("phase", "unknown")
            token_metrics[phase] = {
                "peak_context_percent": ps.get("peak_context_percent", 0),
                "peak_context_tokens": ps.get("peak_context_tokens", 0),
                "snapshot_count": ps.get("snapshot_count", 0),
            }
        # Overall peak from all token_snapshots
        if token_snapshots:
            all_pcts = [s.get("context_percent", 0) for s in token_snapshots]
            token_metrics["_overall"] = {
                "peak_context_percent": max(all_pcts),
                "total_snapshots": len(token_snapshots),
            }

    # 7. Gate denial metrics
    denial_events = [e for e in events if e.get("event") == "gate_denial"]
    repeated_events = [e for e in events if e.get("event") == "repeated_denial"]
    denial_metrics = None
    if denial_events:
        denial_metrics = {
            "total_denials": len(denial_events),
            "repeated_denials": len(repeated_events),
            "denials_by_gate": {},
        }
        for d in denial_events:
            gate = d.get("gate", "unknown")
            denial_metrics["denials_by_gate"][gate] = \
                denial_metrics["denials_by_gate"].get(gate, 0) + 1

    # 8. Rule coverage per file (sub-agent decision signal)
    # Correlates rag_query events with write_attempt events by timestamp within sessions.
    # Measures whether later implementation files get fewer rules than early ones.
    rule_coverage = None
    impl_writes = [e for e in events
                   if e.get("event") == "write_attempt"
                   and e.get("phase") == "implementation"
                   and e.get("result") == "allow"]
    rag_queries = [e for e in events if e.get("event") == "rag_query"]

    if impl_writes and rag_queries:
        # Group by session
        session_writes: dict[str, list[dict]] = {}
        session_rags: dict[str, list[dict]] = {}
        for w in impl_writes:
            session_writes.setdefault(w.get("session", ""), []).append(w)
        for r in rag_queries:
            session_rags.setdefault(r.get("session", ""), []).append(r)

        per_file_coverage: list[dict] = []
        for sid, writes in session_writes.items():
            rags = session_rags.get(sid, [])
            if not rags:
                continue
            writes_sorted = sorted(writes, key=lambda e: e.get("ts", ""))
            rags_sorted = sorted(rags, key=lambda e: e.get("ts", ""))

            for file_idx, w in enumerate(writes_sorted):
                fp = w.get("file_path", "")
                w_ts = w.get("ts", "")
                # Previous write timestamp (or epoch for the first file)
                prev_ts = writes_sorted[file_idx - 1].get("ts", "") if file_idx > 0 else ""
                # RAG queries between previous write and this write belong to this file
                file_rules = 0
                file_tokens = 0
                for r in rags_sorted:
                    r_ts = r.get("ts", "")
                    if r_ts > w_ts:
                        break
                    if r_ts > prev_ts:
                        file_rules += r.get("rules_returned_count", 0)
                        file_tokens += r.get("tokens_injected", 0)

                # Classify file type by extension
                ext = os.path.splitext(fp)[1] if fp else ""
                file_type = {
                    ".php": "php", ".xml": "xml", ".json": "json",
                    ".js": "js", ".ts": "ts", ".py": "python",
                }.get(ext, "other")

                per_file_coverage.append({
                    "session": sid,
                    "file_number": file_idx + 1,
                    "file_type": file_type,
                    "rules_injected": file_rules,
                    "tokens_injected": file_tokens,
                    "file_path": os.path.basename(fp) if fp else "",
                })

        if per_file_coverage:
            # Compute trend: compare first-half avg vs second-half avg rules per file
            by_session: dict[str, list[dict]] = {}
            for fc in per_file_coverage:
                by_session.setdefault(fc["session"], []).append(fc)

            trends: list[dict] = []
            for sid, files in by_session.items():
                if len(files) < 4:
                    continue
                mid = len(files) // 2
                first_half = files[:mid]
                second_half = files[mid:]
                avg_first = sum(f["rules_injected"] for f in first_half) / len(first_half)
                avg_second = sum(f["rules_injected"] for f in second_half) / len(second_half)
                pct_change = (
                    round((avg_second - avg_first) / avg_first * 100, 1)
                    if avg_first > 0 else None
                )
                trends.append({
                    "session": sid,
                    "files_count": len(files),
                    "first_half_avg_rules": round(avg_first, 1),
                    "second_half_avg_rules": round(avg_second, 1),
                    "pct_change": pct_change,
                })

            rule_coverage = {
                "total_files_analyzed": len(per_file_coverage),
                "sessions_analyzed": len(trends),
                "per_session_trends": trends,
                "per_file_detail": per_file_coverage,
            }

    # 9. Sub-agent aggregate metrics (RAG budget visibility across parent + children)
    subagent_events = [e for e in events if e.get("event") == "subagent_complete"]
    subagent_metrics = None
    if subagent_events:
        by_parent: dict[str, list[dict]] = {}
        for se in subagent_events:
            parent = se.get("parent_session", "unknown")
            by_parent.setdefault(parent, []).append(se)

        subagent_metrics = {
            "total_subagents": len(subagent_events),
            "per_parent_session": {},
        }
        for parent_sid, agents in by_parent.items():
            total_queries = sum(a.get("queries", 0) for a in agents)
            total_rules = sum(a.get("rules_loaded", 0) for a in agents)
            total_files = sum(a.get("files_written", 0) for a in agents)
            total_denials = sum(a.get("denial_count", 0) for a in agents)
            budget_consumed = sum(8000 - a.get("remaining_budget", 8000) for a in agents)
            subagent_metrics["per_parent_session"][parent_sid] = {
                "agent_count": len(agents),
                "total_rag_queries": total_queries,
                "total_rules_loaded": total_rules,
                "total_files_written": total_files,
                "total_denials": total_denials,
                "total_budget_consumed": budget_consumed,
                "agents": [{
                    "agent_id": a.get("agent_id", ""),
                    "agent_type": a.get("agent_type", ""),
                    "queries": a.get("queries", 0),
                    "rules_loaded": a.get("rules_loaded", 0),
                    "remaining_budget": a.get("remaining_budget", 0),
                } for a in agents],
            }

    report = {
        "total_sessions": total_sessions,
        "total_events": len(events),
        "clean_run_rate": clean_run_rate,
        "transition_times": transition_stats,
        "event_frequency": event_frequency,
        "mode_distribution": mode_distribution,
        "approval_miss_rate": approval_miss_rate,
        "token_metrics": token_metrics,
        "denial_metrics": denial_metrics,
        "rule_coverage": rule_coverage,
        "subagent_metrics": subagent_metrics,
    }
    json.dump(report, sys.stdout, indent=2)
    sys.stdout.write("\n")


def main() -> None:
    if len(sys.argv) < 2:
        print("Usage: writ-session.py <command> [args]", file=sys.stderr)
        print("Commands: read, update, format, should-skip, mode, coverage, auto-feedback, can-write, advance-phase, current-phase, detect-compaction, metrics", file=sys.stderr)
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
        # Translate bool return to shell exit code: True=skip (0), False=proceed (1)
        sys.exit(0 if cmd_should_skip(sys.argv[2], threshold) else 1)

    elif cmd == "coverage":
        if len(sys.argv) < 3:
            print("Usage: writ-session.py coverage <session_id>", file=sys.stderr)
            sys.exit(2)
        cmd_coverage(sys.argv[2])

    elif cmd == "mode":
        if len(sys.argv) < 4:
            print("Usage: writ-session.py mode <get|set|switch> <session_id|value> [session_id]", file=sys.stderr)
            sys.exit(2)
        subcmd = sys.argv[2]
        if subcmd == "get":
            cmd_mode(sys.argv[3], "get")
        elif subcmd in ("set", "switch"):
            if len(sys.argv) < 5:
                print(f"Usage: writ-session.py mode {subcmd} <conversation|debug|review|work> <session_id>", file=sys.stderr)
                sys.exit(2)
            orch = "--orchestrator" in sys.argv
            cmd_mode(sys.argv[4], subcmd, sys.argv[3], is_orchestrator=orch)
        else:
            print(f"Unknown mode subcommand: {subcmd}", file=sys.stderr)
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

    elif cmd == "detect-compaction":
        if len(sys.argv) < 3:
            print("Usage: writ-session.py detect-compaction <session_id> --context-percent N", file=sys.stderr)
            sys.exit(2)
        context_pct = 0
        if "--context-percent" in sys.argv:
            idx = sys.argv.index("--context-percent")
            if idx + 1 < len(sys.argv):
                context_pct = int(sys.argv[idx + 1])
        cmd_detect_compaction(sys.argv[2], context_pct)

    elif cmd == "clear-rules-for-compaction":
        if len(sys.argv) < 3:
            print("Usage: writ-session.py clear-rules-for-compaction <session_id>", file=sys.stderr)
            sys.exit(2)
        cmd_clear_rules_for_compaction(sys.argv[2])

    elif cmd == "reset-after-compaction":
        if len(sys.argv) < 3:
            print("Usage: writ-session.py reset-after-compaction <session_id>", file=sys.stderr)
            sys.exit(2)
        cmd_reset_after_compaction(sys.argv[2])

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
