"""Friction-log reader + aggregator.

Reads the JSONL workflow-friction.log that Writ hooks write to, produces
a human-readable summary, and rotates the log when it grows too large.
Exposed via the `writ analyze-friction` CLI subcommand.
"""

from __future__ import annotations

import json
import os
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

__all__ = ["load_events", "summarize", "rotate_if_needed", "format_report"]

DEFAULT_ROTATION_THRESHOLD_BYTES = 5 * 1024 * 1024  # 5MB


def load_events(log_path: Path) -> list[dict[str, Any]]:
    """Parse JSONL log, skipping malformed lines. Returns list of event dicts."""
    events: list[dict[str, Any]] = []
    if not log_path.exists():
        return events
    with log_path.open() as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                events.append(json.loads(line))
            except (json.JSONDecodeError, ValueError):
                continue
    return events


def _filter_since(events: list[dict], since_days: int | None) -> list[dict]:
    if since_days is None:
        return events
    cutoff = datetime.now(timezone.utc) - timedelta(days=since_days)
    filtered: list[dict] = []
    for e in events:
        ts = e.get("ts")
        if not ts:
            continue
        try:
            event_time = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        except (ValueError, AttributeError):
            continue
        if event_time >= cutoff:
            filtered.append(e)
    return filtered


def _percentile(values: list[int], pct: float) -> int:
    if not values:
        return 0
    sorted_values = sorted(values)
    idx = int(len(sorted_values) * pct / 100)
    idx = min(idx, len(sorted_values) - 1)
    return sorted_values[idx]


def summarize(
    events: list[dict[str, Any]],
    top: int = 10,
    since_days: int | None = None,
) -> dict[str, Any]:
    """Aggregate events into a summary dict."""
    events = _filter_since(events, since_days)

    event_counts: Counter[str] = Counter()
    hook_durations: dict[str, list[int]] = defaultdict(list)
    rule_hits: Counter[str] = Counter()
    pre_write_decisions: Counter[str] = Counter()
    subagent_completions: Counter[str] = Counter()
    session_denials: Counter[str] = Counter()
    write_failures = 0
    phase_transitions = 0
    approval_matches = 0

    for e in events:
        evt = e.get("event", "unknown")
        event_counts[evt] += 1

        if evt == "hook_execution":
            name = e.get("hook_name")
            dur = e.get("duration_ms")
            if name and isinstance(dur, (int, float)):
                hook_durations[name].append(int(dur))
        elif evt == "rag_query":
            for rid in e.get("rule_ids", []):
                rule_hits[rid] += 1
        elif evt == "pre_write_decision":
            decision = e.get("decision", "unknown")
            pre_write_decisions[decision] += 1
        elif evt == "subagent_complete":
            agent_type = e.get("agent_type") or "general-purpose"
            subagent_completions[agent_type] += 1
        elif evt == "gate_denial":
            sess = e.get("session", "unknown")
            session_denials[sess] += 1
        elif evt == "write_failure":
            write_failures += 1
        elif evt == "phase_transition":
            phase_transitions += 1
        elif evt == "approval_pattern_match":
            approval_matches += 1

    hook_p95: dict[str, int] = {
        name: _percentile(durs, 95) for name, durs in hook_durations.items()
    }

    return {
        "total_events": len(events),
        "event_counts": dict(event_counts.most_common(top)),
        "hook_p95_ms": dict(sorted(hook_p95.items(), key=lambda kv: -kv[1])[:top]),
        "top_rules": dict(rule_hits.most_common(top)),
        "pre_write_decisions": dict(pre_write_decisions),
        "subagent_completions": dict(subagent_completions),
        "sessions_with_denials": dict(session_denials.most_common(top)),
        "write_failures": write_failures,
        "phase_transitions": phase_transitions,
        "approval_matches": approval_matches,
    }


def format_report(summary: dict[str, Any]) -> str:
    """Render the summary dict as a human-readable report."""
    lines: list[str] = []
    lines.append(f"Writ friction report ({summary['total_events']} events)")
    lines.append("=" * 60)
    lines.append("")

    if summary["event_counts"]:
        lines.append("Event breakdown:")
        for name, count in summary["event_counts"].items():
            lines.append(f"  {name:30s} {count:>6d}")
        lines.append("")

    if summary["hook_p95_ms"]:
        lines.append("Hook latency p95 (top slowest):")
        for name, ms in summary["hook_p95_ms"].items():
            lines.append(f"  {name:30s} {ms:>5d} ms")
        lines.append("")

    if summary["top_rules"]:
        lines.append("Top rules injected:")
        for rid, count in summary["top_rules"].items():
            lines.append(f"  {rid:20s} {count:>4d}x")
        lines.append("")

    if summary["pre_write_decisions"]:
        lines.append("Pre-write decisions:")
        for decision, count in summary["pre_write_decisions"].items():
            lines.append(f"  {decision:15s} {count:>4d}")
        lines.append("")

    if summary["subagent_completions"]:
        lines.append("Sub-agent completions:")
        for agent_type, count in summary["subagent_completions"].items():
            lines.append(f"  {agent_type:30s} {count:>3d}")
        lines.append("")

    lines.append("Gate activity:")
    lines.append(f"  approval_pattern_match {summary['approval_matches']:>5d}")
    lines.append(f"  phase_transitions      {summary['phase_transitions']:>5d}")
    lines.append(f"  write_failures         {summary['write_failures']:>5d}")
    if summary["sessions_with_denials"]:
        lines.append("  sessions with gate denials:")
        for sess, count in summary["sessions_with_denials"].items():
            lines.append(f"    {sess}  ({count})")

    return "\n".join(lines) + "\n"


def rotate_if_needed(
    log_path: Path,
    threshold_bytes: int = DEFAULT_ROTATION_THRESHOLD_BYTES,
) -> bool:
    """Rotate the log if it exceeds the size threshold.

    Renames log -> log.1 (overwriting any existing .1) and truncates the
    original. Returns True if rotation occurred, False otherwise.
    """
    if not log_path.exists():
        return False
    if log_path.stat().st_size < threshold_bytes:
        return False
    rotated = log_path.with_suffix(log_path.suffix + ".1")
    if rotated.exists():
        rotated.unlink()
    os.rename(str(log_path), str(rotated))
    log_path.touch()
    return True
