"""Workflow-friction-log metrics report for the session helper.

POL-6h extracts cmd_metrics out of bin/lib/writ-session.py -- the final facade extraction.
It analyzes workflow-friction.log (located via an explicit path or a project-marker walk) and
emits a confidence-metrics JSON report: clean-run rate, phase-transition stats, event-frequency,
gate denials, coverage trends, sub-agent and token-snapshot rollups. Its only package dependency
is mode_engine.VALID_MODES (the mode-distribution counter); mode_engine is a lower layer, so the
graph stays acyclic. The facade re-exports it.
"""

import json
import os
import sys

from writ.session.mode_engine import VALID_MODES
from writ.shared.logging import NON_GOVERNANCE_MODE_EVENTS, read_streams, resolve_project
from writ.shared.percentile import percentile
from writ.session.cli_io import _emit_json

_SPLIT_STREAMS = ("audit", "friction", "metrics")


def _compute_clean_run_rate(events: list[dict], total_sessions: int) -> float | None:
    """Clean-run rate: percent of sessions with no gate invalidation."""
    sessions_with_invalidations = {
        e.get("session") for e in events
        if e.get("event") == "gate_denied_then_approved"
    }
    clean_sessions = total_sessions - len(sessions_with_invalidations)
    return round(clean_sessions / total_sessions * 100, 1) if total_sessions > 0 else None


def _compute_transition_stats(events: list[dict]) -> dict | None:
    """avg/p50/p90/min/max of phase_transition_time elapsed_seconds (None if none)."""
    import statistics as _stats
    transition_times = [
        e["elapsed_seconds"] for e in events
        if e.get("event") == "phase_transition_time" and "elapsed_seconds" in e
    ]
    if not transition_times:
        return None
    sorted_times = sorted(transition_times)
    n = len(sorted_times)
    return {
        "count": n,
        "avg": round(_stats.mean(sorted_times), 1),
        "p50": percentile(sorted_times, 50),
        "p90": percentile(sorted_times, 90),
        "min": sorted_times[0],
        "max": sorted_times[-1],
    }


def _compute_event_frequency(events: list[dict]) -> dict[str, int]:
    """Count events by type; known types seeded to 0 so the report shape is stable."""
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
    return event_frequency


def _compute_mode_distribution(events: list[dict]) -> dict[str, int]:
    """Sessions counted at their latest mode; legacy tier events mapped to modes.

    Session-governance events ONLY. `mode` is not a single field: `retrieval_result`
    reuses the key for the retrieval delivery mode ("standard" / "summary" / "abstained"),
    and _SPLIT_STREAMS puts metrics last, so those values were counted as session modes
    AND overwrote the real mode of every session that had queried -- live output read
    {'work': 145, 'standard': 30, 'abstained': 3, ...}. NON_GOVERNANCE_MODE_EVENTS is the
    schema's own record of which events redefine the key.
    """
    _legacy_tier_to_mode = {0: "conversation", 1: "work", 2: "work", 3: "work"}
    mode_distribution: dict[str, int] = {m: 0 for m in VALID_MODES}
    session_final_mode: dict[str, str] = {}
    for e in events:
        if e.get("event") in NON_GOVERNANCE_MODE_EVENTS:
            continue
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
    return mode_distribution


def _compute_approval_miss_rate(event_frequency: dict[str, int]) -> float | None:
    """Approval-pattern miss rate derived from the event-frequency table."""
    miss_count = event_frequency.get("approval_pattern_miss", 0)
    transition_count = event_frequency.get("phase_transition", 0)
    total_approval_attempts = miss_count + transition_count
    return (
        round(miss_count / total_approval_attempts * 100, 1)
        if total_approval_attempts > 0 else None
    )


def _compute_token_metrics(events: list[dict]) -> dict | None:
    """Per-phase peaks (phase_token_summary) + overall peak (token_snapshot)."""
    phase_summaries = [e for e in events if e.get("event") == "phase_token_summary"]
    token_snapshots = [e for e in events if e.get("event") == "token_snapshot"]
    if not (phase_summaries or token_snapshots):
        return None
    token_metrics: dict = {}
    for ps in phase_summaries:
        phase = ps.get("phase", "unknown")
        token_metrics[phase] = {
            "peak_context_percent": ps.get("peak_context_percent", 0),
            "peak_context_tokens": ps.get("peak_context_tokens", 0),
            "snapshot_count": ps.get("snapshot_count", 0),
        }
    if token_snapshots:
        all_pcts = [s.get("context_percent", 0) for s in token_snapshots]
        token_metrics["_overall"] = {
            "peak_context_percent": max(all_pcts),
            "total_snapshots": len(token_snapshots),
        }
    return token_metrics


def _compute_denial_metrics(events: list[dict]) -> dict | None:
    """Gate-denial counts: total, repeated, and by gate."""
    denial_events = [e for e in events if e.get("event") == "gate_denial"]
    if not denial_events:
        return None
    repeated_events = [e for e in events if e.get("event") == "repeated_denial"]
    denial_metrics: dict = {
        "total_denials": len(denial_events),
        "repeated_denials": len(repeated_events),
        "denials_by_gate": {},
    }
    for d in denial_events:
        gate = d.get("gate", "unknown")
        denial_metrics["denials_by_gate"][gate] = \
            denial_metrics["denials_by_gate"].get(gate, 0) + 1
    return denial_metrics


# Extension -> file_type label for coverage classification.
_COVERAGE_FILE_TYPES = {
    ".php": "php", ".xml": "xml", ".json": "json",
    ".js": "js", ".ts": "ts", ".py": "python",
}


def _group_by(items: list[dict], key) -> dict:
    """Bucket items into {key(item): [items]} preserving encounter order."""
    grouped: dict = {}
    for it in items:
        grouped.setdefault(key(it), []).append(it)
    return grouped


def _compute_per_file_coverage(session_writes: dict, session_rags: dict) -> list[dict]:
    """Per implementation write, attribute the RAG queries that fired between the previous
    write and it (by timestamp) as that file's injected rules/tokens. Skips sessions with
    no RAG queries."""
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

            ext = os.path.splitext(fp)[1] if fp else ""
            file_type = _COVERAGE_FILE_TYPES.get(ext, "other")

            per_file_coverage.append({
                "session": sid,
                "file_number": file_idx + 1,
                "file_type": file_type,
                "rules_injected": file_rules,
                "tokens_injected": file_tokens,
                "file_path": os.path.basename(fp) if fp else "",
            })
    return per_file_coverage


def _compute_coverage_trends(per_file_coverage: list[dict]) -> list[dict]:
    """First-half vs second-half avg rules-per-file per session (sessions with >=4 files),
    surfacing whether later files get fewer rules than early ones."""
    by_session = _group_by(per_file_coverage, lambda fc: fc["session"])
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
    return trends


def _compute_rule_coverage(events: list[dict]) -> dict | None:
    """Per-file rule-injection coverage + first-half vs second-half trend per session.

    Correlates rag_query events with implementation write_attempts by timestamp
    within each session, to surface whether later files get fewer rules than early ones.
    """
    impl_writes = [e for e in events
                   if e.get("event") == "write_attempt"
                   and e.get("phase") == "implementation"
                   and e.get("result") == "allow"]
    rag_queries = [e for e in events if e.get("event") == "rag_query"]
    if not (impl_writes and rag_queries):
        return None

    session_writes = _group_by(impl_writes, lambda w: w.get("session", ""))
    session_rags = _group_by(rag_queries, lambda r: r.get("session", ""))

    per_file_coverage = _compute_per_file_coverage(session_writes, session_rags)
    if not per_file_coverage:
        return None

    trends = _compute_coverage_trends(per_file_coverage)

    return {
        "total_files_analyzed": len(per_file_coverage),
        "sessions_analyzed": len(trends),
        "per_session_trends": trends,
        "per_file_detail": per_file_coverage,
    }


def _compute_subagent_metrics(events: list[dict]) -> dict | None:
    """Per-parent-session sub-agent rollups (queries, rules, files, denials, budget)."""
    subagent_events = [e for e in events if e.get("event") == "subagent_complete"]
    if not subagent_events:
        return None
    by_parent: dict[str, list[dict]] = {}
    for se in subagent_events:
        parent = se.get("parent_session", "unknown")
        by_parent.setdefault(parent, []).append(se)

    subagent_metrics: dict = {
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
    return subagent_metrics


def _read_single_file(log_path: str) -> list[dict] | None:
    """Parse one JSONL friction log; None signals an unreadable file (error path)."""
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
        return None
    return events


def _load_metrics_events(log_path: str) -> list[dict] | None:
    """Resolve where to read metrics events and return raw dicts.

    Resolution (explicit beats implicit): an explicit `log_path` is read verbatim.
    Otherwise WRIT_FRICTION_LOG, if set, is read as the single legacy file; else
    the split audit+friction+metrics streams are unioned via the P1 router. None
    signals an error condition (missing/unreadable single file) for the caller.
    """
    if log_path:
        if not os.path.exists(log_path):
            return None
        return _read_single_file(log_path)
    env = os.environ.get("WRIT_FRICTION_LOG", "")
    if env:
        if not os.path.exists(env):
            return None
        return _read_single_file(env)
    return read_streams(resolve_project(), _SPLIT_STREAMS)


def cmd_metrics(log_path: str = "") -> None:
    """Analyze the friction log(s) and produce a confidence metrics report.

    Reads friction events and computes:
    - Clean run rate (sessions without gate invalidations)
    - Phase transition time statistics (avg, p50, p90)
    - Friction event frequency by type
    - Tier distribution (sessions counted at final tier)
    - Approval pattern miss rate

    Source resolution: an explicit `log_path`, else WRIT_FRICTION_LOG, else the
    split audit+friction+metrics streams unioned via the P1 logging router.

    Output: JSON to stdout.
    """
    events = _load_metrics_events(log_path)

    if events is None:
        _emit_json({"error": "No friction log found"})
        sys.stdout.write("\n")
        sys.exit(1)

    if not events:
        _emit_json({"error": "No events in friction log"})
        sys.stdout.write("\n")
        sys.exit(1)

    # Group by session
    sessions: dict[str, list[dict]] = {}
    for e in events:
        sid = e.get("session", "unknown")
        sessions.setdefault(sid, []).append(e)

    total_sessions = len(sessions)

    # Confidence metrics: each block is a pure _compute_* helper (B6b). event_frequency
    # is computed first because the approval-miss rate is derived from it.
    clean_run_rate = _compute_clean_run_rate(events, total_sessions)
    transition_stats = _compute_transition_stats(events)
    event_frequency = _compute_event_frequency(events)
    mode_distribution = _compute_mode_distribution(events)
    approval_miss_rate = _compute_approval_miss_rate(event_frequency)
    token_metrics = _compute_token_metrics(events)
    denial_metrics = _compute_denial_metrics(events)
    rule_coverage = _compute_rule_coverage(events)
    subagent_metrics = _compute_subagent_metrics(events)

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
    _emit_json(report, indent=2)
    sys.stdout.write("\n")
