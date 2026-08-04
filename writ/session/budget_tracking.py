"""Token-budget command set for the session helper.

POL-6g-1 extracts cmd_update / cmd_should_skip / _estimate_cost / cmd_format out of
bin/lib/writ-session.py. cmd_update is the multi-flag session-state writer (budget
decrement, loaded-rule tracking, citations, command-run log, coverage-scope freeze,
source-type selection, task-phase reset). Imports only lower layers (cache, config,
citations, mode_engine) and the stdlib -- never the facade -- so the graph stays acyclic.
The facade re-exports this surface; server.py's budget routes call cmd_format unchanged.
"""

import json
import sys
from datetime import datetime, timezone

from writ.session.cache import _read_cache, _write_cache, mutate_cache
from writ.session.config import (
    DEFAULT_SESSION_BUDGET,
    DEFAULT_ALWAYS_ON_CAP,
)
from writ.session.citations import _append_citation
from writ.session.mode_engine import _VALID_SOURCE_TYPES
from writ.shared.tokens import cost_for


def _upd_add_rules(cache: dict, args: list[str], i: int) -> int:
    new_ids = json.loads(args[i + 1])
    # Flat list (all IDs ever loaded -- for feedback/coverage)
    existing = set(cache.get("loaded_rule_ids", []))
    existing.update(new_ids)
    cache["loaded_rule_ids"] = sorted(existing)
    # Phase-partitioned list (for exclude-list scoping)
    phase = cache.get("current_phase", "unknown")
    by_phase = cache.setdefault("loaded_rule_ids_by_phase", {})
    phase_ids = set(by_phase.get(phase, []))
    phase_ids.update(new_ids)
    by_phase[phase] = sorted(phase_ids)
    return i + 2


def _upd_add_always_on_rules(cache: dict, args: list[str], i: int) -> int:
    """Record the rules the always-on channel injected this turn.

    Same set-union-and-sort shape as _upd_add_rules, but a SEPARATE field on purpose:
    loaded_rule_ids doubles as the ranked query's exclude list, and 5 of the always-on
    bundle's rules live in the ranked pool, so writing them there would silently stop
    them being retrieved by relevance. This field feeds citation validation only.
    """
    existing = set(cache.get("always_on_rule_ids", []))
    existing.update(json.loads(args[i + 1]))
    cache["always_on_rule_ids"] = sorted(existing)
    return i + 2


def _upd_set_last_injected(cache: dict, args: list[str], i: int) -> int:
    # A3: replaces the rag-inject hook's separate raw read-modify-write of the
    # sticky-rules pointer. SET (replace); a malformed payload leaves it unchanged.
    try:
        cache["last_injected_rule_ids"] = json.loads(args[i + 1])
    except (ValueError, json.JSONDecodeError):
        pass
    return i + 2


def _upd_cost(cache: dict, args: list[str], i: int) -> int:
    cost = int(args[i + 1])
    cache["remaining_budget"] = max(0, cache["remaining_budget"] - cost)
    return i + 2


def _upd_context_percent(cache: dict, args: list[str], i: int) -> int:
    cache["context_percent"] = int(args[i + 1])
    return i + 2


def _upd_is_subagent(cache: dict, args: list[str], i: int) -> int:
    value = args[i + 1].strip().lower()
    cache["is_subagent"] = value in ("true", "1", "yes")
    return i + 2


def _upd_inc_queries(cache: dict, args: list[str], i: int) -> int:
    cache["queries"] = cache.get("queries", 0) + 1
    return i + 1


def _upd_add_file(cache: dict, args: list[str], i: int) -> int:
    files = set(cache.get("files_written", []))
    files.add(args[i + 1])
    cache["files_written"] = sorted(files)
    return i + 2


def _upd_add_file_result(cache: dict, args: list[str], i: int) -> int:
    # --add-file-result <filepath> <pass|fail>
    results = cache.get("analysis_results", {})
    results[args[i + 1]] = args[i + 2]
    cache["analysis_results"] = results
    return i + 3


def _upd_add_feedback_sent(cache: dict, args: list[str], i: int) -> int:
    sent = set(cache.get("feedback_sent", []))
    sent.add(args[i + 1])
    cache["feedback_sent"] = sorted(sent)
    return i + 2


def _upd_add_pretool_file(cache: dict, args: list[str], i: int) -> int:
    files = set(cache.get("pretool_queried_files", []))
    files.add(args[i + 1])
    cache["pretool_queried_files"] = sorted(files)
    return i + 2


def _upd_add_rule_objects(cache: dict, args: list[str], i: int) -> int:
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
    return i + 2


def _upd_token_snapshot(cache: dict, args: list[str], i: int) -> int:
    snapshot_data = json.loads(args[i + 1])
    snapshot_data["ts"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    snapshot_data["phase"] = cache.get("current_phase")
    snapshot_data["mode"] = cache.get("mode")
    cache.setdefault("token_snapshots", []).append(snapshot_data)
    return i + 2


def _upd_add_always_on_tokens(cache: dict, args: list[str], i: int) -> int:
    n = int(args[i + 1])
    cache["always_on_tokens_used"] = cache.get("always_on_tokens_used", 0) + n
    cache["always_on_budget"] = max(
        0, cache.get("always_on_budget", DEFAULT_ALWAYS_ON_CAP) - n
    )
    return i + 2


def _upd_add_command_run(cache: dict, args: list[str], i: int) -> int:
    # Increment 7a (INV-2): a captured Bash run is a command-type citation.
    # One JSON arg {command, exit_code, output_excerpt}; bounded + truncated.
    try:
        run = json.loads(args[i + 1])
    except (ValueError, json.JSONDecodeError):
        run = {}
    if isinstance(run, dict) and run.get("command"):
        _append_citation(cache, {
            "artifact_type": "command",
            "ref": str(run.get("command", "")),
            "excerpt": str(run.get("output_excerpt", "")),
            "exit_code": run.get("exit_code", 0),
        })
    return i + 2


def _upd_add_citation(cache: dict, args: list[str], i: int) -> int:
    # INV-2: the general artifact-citation verb (file/url/...). One JSON arg
    # {artifact_type, ref, excerpt, ...extra}; bounded + excerpt-truncated.
    try:
        cite = json.loads(args[i + 1])
    except (ValueError, json.JSONDecodeError):
        cite = {}
    if isinstance(cite, dict) and cite.get("ref"):
        _append_citation(cache, cite)
    return i + 2


def _upd_freeze_scope(cache: dict, args: list[str], i: int) -> int:
    # INV-4: freeze the investigation's file-level coverage denominator ONCE.
    # JSON {files:[...], span_budget?:int, source?:str, force?:bool}. A second
    # freeze is a no-op unless force=true -- the denominator must not silently
    # grow (that is what makes the coverage map ungameable).
    try:
        payload = json.loads(args[i + 1])
    except (ValueError, json.JSONDecodeError):
        payload = {}
    if isinstance(payload, dict):
        existing = cache.get("coverage_scope")
        already_frozen = bool(existing and existing.get("frozen_at"))
        if not already_frozen or payload.get("force"):
            cache["coverage_scope"] = {
                "frozen_at": datetime.now(timezone.utc).isoformat(),
                "files": sorted({str(f) for f in payload.get("files", []) if f}),
                "span_budget": payload.get("span_budget"),
                "source": str(payload.get("source", "")),
            }
    return i + 2


def _upd_set_source_type(cache: dict, args: list[str], i: int) -> int:
    # INV-8: select the investigation lens for investigate mode. Only the three
    # valid source types are accepted; anything else is ignored.
    value = args[i + 1].strip().lower()
    if value in _VALID_SOURCE_TYPES:
        cache["source_type"] = value
    return i + 2


def _upd_parent_session_id(cache: dict, args: list[str], i: int) -> int:
    cache["parent_session_id"] = args[i + 1]
    return i + 2


def _upd_agent_type(cache: dict, args: list[str], i: int) -> int:
    cache["agent_type"] = args[i + 1]
    return i + 2


def _upd_add_queried_rules_for_file(cache: dict, args: list[str], i: int) -> int:
    """Union the rule_ids RAG queried for a file into cache['queried_rules_by_file'][path]."""
    from writ.session.remote_parse import normalize_path
    key = normalize_path(args[i + 1])
    new_ids = json.loads(args[i + 2])
    by_file = cache.setdefault("queried_rules_by_file", {})
    merged = set(by_file.get(key, []))
    merged.update(new_ids)
    by_file[key] = sorted(merged)
    return i + 3


def _upd_reset_task_phase(cache: dict, args: list[str], i: int) -> int:
    # Fresh-task transition: ExitPlanMode validation succeeded, signaling the user
    # is reviewing a brand-new plan. Reset current_phase to planning and clear
    # gates_approved so the next /advance-phase walks planning -> testing ->
    # implementation from the start, not the prior task's residual state.
    old_phase = cache.get("current_phase")
    cache["current_phase"] = "planning"
    cache["gates_approved"] = []
    if old_phase != "planning":
        cache.setdefault("phase_transitions", []).append({
            "from": old_phase,
            "to": "planning",
            "trigger": "exit-plan-reset",
            "ts": datetime.now(timezone.utc).isoformat(),
        })
    return i + 1


def _upd_set_recall_briefed(cache: dict, args: list[str], i: int) -> int:
    # Layer 3: the once-per-session recall marker, formerly a hand-rolled hook write.
    cache["recall_briefed"] = True
    return i + 1


def _upd_set_escalation_feedback_sent(cache: dict, args: list[str], i: int) -> int:
    # Layer 3: the escalation feedback marker, formerly a hand-rolled hook write.
    cache.setdefault("escalation", {})["feedback_sent"] = True
    return i + 1


def _upd_set_detected_domain(cache: dict, args: list[str], i: int) -> int:
    # Layer 3: the cwd-changed domain marker, formerly a hand-rolled hook write.
    cache["detected_domain"] = args[i + 1]
    return i + 2


# B6a: cmd_update's 19-branch flag if/elif (CC=59) becomes one (handler, min_extra)
# table. min_extra = extra argv tokens the flag needs; the dispatcher skips a flag
# whose value is missing (i + min_extra >= len), exactly reproducing each old
# `and i+N < len(args)` guard. Built once at module load (this runs per cache write).
_UPDATE_HANDLERS: dict = {
    "--add-rules": (_upd_add_rules, 1),
    "--add-always-on-rules": (_upd_add_always_on_rules, 1),
    "--set-last-injected-rule-ids": (_upd_set_last_injected, 1),
    "--cost": (_upd_cost, 1),
    "--context-percent": (_upd_context_percent, 1),
    "--is-subagent": (_upd_is_subagent, 1),
    "--inc-queries": (_upd_inc_queries, 0),
    "--add-file": (_upd_add_file, 1),
    "--add-file-result": (_upd_add_file_result, 2),
    "--add-feedback-sent": (_upd_add_feedback_sent, 1),
    "--add-pretool-file": (_upd_add_pretool_file, 1),
    "--add-rule-objects": (_upd_add_rule_objects, 1),
    "--token-snapshot": (_upd_token_snapshot, 1),
    "--add-always-on-tokens": (_upd_add_always_on_tokens, 1),
    "--add-command-run": (_upd_add_command_run, 1),
    "--add-citation": (_upd_add_citation, 1),
    "--freeze-scope": (_upd_freeze_scope, 1),
    "--set-source-type": (_upd_set_source_type, 1),
    "--parent-session-id": (_upd_parent_session_id, 1),
    "--agent-type": (_upd_agent_type, 1),
    "--add-queried-rules-for-file": (_upd_add_queried_rules_for_file, 2),
    "--reset-task-phase": (_upd_reset_task_phase, 0),
    "--set-recall-briefed": (_upd_set_recall_briefed, 0),
    "--set-escalation-feedback-sent": (_upd_set_escalation_feedback_sent, 0),
    "--set-detected-domain": (_upd_set_detected_domain, 1),
}


def cmd_update(session_id: str, args: list[str]) -> None:
    with mutate_cache(session_id) as cache:
        i = 0
        while i < len(args):
            entry = _UPDATE_HANDLERS.get(args[i])
            # Unknown flag, or a known flag missing its value(s) -> skip one token
            # (the old elif chain fell through to `else: i += 1` in both cases).
            if entry is None or i + entry[1] >= len(args):
                i += 1
                continue
            i = entry[0](cache, args, i)


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
    if cache.get("remaining_budget", DEFAULT_SESSION_BUDGET) <= 0:
        return True  # skip: budget exhausted
    if cache.get("context_percent", 0) >= threshold:
        return True  # skip: context pressure
    return False  # proceed


def _estimate_cost(rules: list[dict], mode: str) -> int:
    # Delegates to the single cost policy in shared.tokens (DUP-S4). Kept as a
    # named function: it is part of this module's extracted public surface
    # (facade re-export + bin/lib/writ-session.py import + pol6g1 tests).
    return cost_for(rules, mode)


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

    lines = [f"--- WRIT RULES ({len(rules)} rules, {mode} mode) ---", ""]

    for rule in rules:
        # Summary-mode abstraction entries (_summary_with_abstractions) carry an
        # abstraction_id + summary, not rule fields. Render them as the abstraction summary
        # they are -- otherwise the rule formatter shows "[UNKNOWN] (?, ?, ?) score=0.000"
        # and drops the summary text entirely (the observed garbage-injection bug).
        if rule.get("abstraction_id"):
            covered = len(rule.get("rule_ids", []))
            dom = rule.get("domain", "")
            lines.append(f"[ABSTRACT: {rule['abstraction_id']}] (covers {covered} rules"
                         + (f", {dom}" if dom else "") + ")")
            summary = rule.get("summary", "")
            if summary:
                lines.append(summary)
            lines.append("")
            continue

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
        # A summary-mode abstraction was rendered above as "[ABSTRACT: <id>]", so that
        # id is what the model SAW and what it naturally cites in ## Rules Applied.
        # Recording only the covered rule_ids left the abstraction's own id absent from
        # loaded_rule_ids, and the phase-a gate then flagged the citation as
        # hallucinated -- for an id Writ itself had just injected.
        abstraction_id = rule.get("abstraction_id")
        if abstraction_id:
            rule_ids.append(abstraction_id)
        for member_id in rule.get("rule_ids", []):
            rule_ids.append(member_id)

    cost = _estimate_cost(rules, mode)
    meta = json.dumps({"rule_ids": rule_ids, "cost": cost})
    sys.stdout.write(f"WRIT_META:{meta}\n")
