"""Friction-log reader + aggregator.

Reads the JSONL workflow-friction.log that Writ hooks write to, produces
human-readable summaries, and rotates the log when it grows too large.
Exposed via the `writ analyze-friction` CLI subcommand and the
`/dashboard` server endpoint.

Phase 4 added: FrictionEvent Pydantic model + parse_log + per-rule and
per-event aggregators.

Phase 5 adds:
  - log_friction_event(): Python writer honoring WRIT_FRICTION_LOG env var
  - resolve_log_path(): canonical lookup for the active log
  - Six analyzer functions for measurement / graduation / trim
  - Six typed result-row models
"""

from __future__ import annotations

import json
import os
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

from pydantic import BaseModel, ConfigDict

from writ.analysis.jsonl import read_jsonl
from writ.shared.friction import base_friction_entry
from writ.shared.logging import emit, read_streams, resolve_project
from writ.shared.percentile import percentile

_SPLIT_STREAMS = ("audit", "friction", "metrics")

__all__ = [
    "load_events", "summarize", "rotate_if_needed", "format_report",
    "FrictionEvent", "parse_log", "aggregate_by_rule", "aggregate_by_event",
    "log_friction_event", "resolve_log_path",
    # Phase 5 result models
    "RuleEffectivenessRow", "SkillUsageRow", "PlaybookComplianceRow",
    "GraduationCandidate", "TrimCandidate", "QualityJudgeOverride",
    # Phase 5 analyzers
    "analyze_rule_effectiveness", "analyze_skill_usage",
    "analyze_playbook_compliance", "analyze_graduation_candidates",
    "analyze_trim_candidates", "analyze_quality_judge_false_positives",
    # audit-session aggregation + rendering (relocated from writ/cli.py)
    "aggregate_session", "render_audit_json", "render_audit_text",
]


class FrictionEvent(BaseModel):
    """One JSONL row from workflow-friction.log.

    Extra fields (rule_id, gate, matched_prompt, etc.) are preserved via
    model_config.extra so callers can inspect without hard-coding every
    hook's emit schema.
    """

    model_config = ConfigDict(extra="allow")

    ts: str
    session: str
    event: str
    mode: str | None = None
    rule_id: str | None = None
    gate: str | None = None


# --- Log path resolution + writer -------------------------------------------


def resolve_log_path(explicit: Path | str | None = None) -> Path:
    """Return the canonical friction-log path.

    Resolution order:
      1. explicit argument (absolute or relative)
      2. WRIT_FRICTION_LOG env var
      3. ./workflow-friction.log

    Used by both the CLI (which passes --log) and the dashboard (which
    reads the env var). Single source of truth.
    """
    if explicit is not None:
        return Path(explicit)
    env = os.environ.get("WRIT_FRICTION_LOG")
    if env:
        return Path(env)
    return Path("workflow-friction.log")


def log_friction_event(
    session_id: str,
    mode: str | None,
    event: str,
    log_path: Path | str | None = None,
    **fields: Any,
) -> None:
    """Append one JSON event to the friction log. Fire-and-forget.

    An explicit `log_path` writes there directly (explicit beats implicit) and
    bypasses the router-resolved split streams. With no explicit path the event
    is delegated to the P1 logging router (writ.shared.logging.emit), which
    classifies it into a typed stream and still honors WRIT_FRICTION_LOG.
    Written from server-side endpoints that record metrics (quality_judgment,
    playbook_step_complete). Bash hooks have their own writer in bin/lib/common.sh.
    """
    if log_path is not None:
        path = Path(log_path)
        entry = base_friction_entry(session_id, mode, event)
        entry.update({k: v for k, v in fields.items() if v is not None})
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a") as f:
                f.write(json.dumps(entry) + "\n")
        except OSError:
            # Fire-and-forget: never break the server because the log is unwritable.
            pass
        return
    emit(None, event, session_id, mode, **fields)


# --- Parsers -----------------------------------------------------------------


def _read_raw_rows(
    path: Path | str | None, project: str | None
) -> list[dict[str, Any]]:
    """Resolve where to read and return raw event dicts.

    Resolution (explicit beats implicit): an explicit `path` is read verbatim.
    Otherwise WRIT_FRICTION_LOG, if set, is read as the single legacy file; else
    the split audit+friction+metrics streams for `project` are unioned via the
    P1 router's read_streams (SOLID-DIP-002: the reader never knows the layout).
    """
    if path is not None:
        return _read_single_file(Path(path))
    env = os.environ.get("WRIT_FRICTION_LOG")
    if env:
        return _read_single_file(Path(env))
    scope = project if project is not None else resolve_project()
    return read_streams(scope, _SPLIT_STREAMS)


def _read_single_file(path: Path) -> list[dict[str, Any]]:
    """Parse one JSONL file into raw dicts, skipping malformed/missing."""
    if not path.exists():
        return []
    return list(read_jsonl(path))


def parse_log(
    path: Path | str | None = None,
    project: str | None = None,
) -> list[FrictionEvent]:
    """Parse the friction log(s) into validated FrictionEvent models.

    With an explicit `path` a single file is read (existing CLI/dashboard
    behavior). With no path the split audit+friction+metrics streams for
    `project` (or the resolved project) are unioned via the router, unless
    WRIT_FRICTION_LOG collapses them to one file. Malformed rows are skipped.
    """
    events: list[FrictionEvent] = []
    for row in _read_raw_rows(path, project):
        try:
            events.append(FrictionEvent.model_validate(row))
        except Exception:
            continue
    return events


def aggregate_by_rule(events: list[FrictionEvent]) -> dict[str, int]:
    """Count events per rule_id. Events without rule_id are ignored."""
    counts: Counter[str] = Counter()
    for e in events:
        if e.rule_id:
            counts[e.rule_id] += 1
    return dict(counts)


def aggregate_by_event(events: list[FrictionEvent]) -> dict[str, int]:
    """Count events per event-name."""
    counts: Counter[str] = Counter()
    for e in events:
        counts[e.event] += 1
    return dict(counts)


DEFAULT_ROTATION_THRESHOLD_BYTES = 5 * 1024 * 1024  # 5MB


def load_events(
    log_path: Path | str | None = None,
    project: str | None = None,
) -> list[dict[str, Any]]:
    """Parse the friction log(s) into raw event dicts, skipping malformed lines.

    With an explicit `log_path` a single file is read (existing CLI/dashboard
    behavior). With no path the split audit+friction+metrics streams for
    `project` (or the resolved project) are unioned via the router, unless
    WRIT_FRICTION_LOG collapses them to one file.
    """
    return _read_raw_rows(log_path, project)


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


def summarize(
    events: list[dict[str, Any]],
    top: int = 10,
    since_days: int | None = None,
) -> dict[str, Any]:
    """Aggregate events into a summary dict."""
    from writ.shared.delivery import classify_delivery, INERT_DELIVERIES

    events = _filter_since(events, since_days)

    event_counts: Counter[str] = Counter()
    hook_durations: dict[str, list[int]] = defaultdict(list)
    rule_hits: Counter[str] = Counter()
    write_decisions: Counter[str] = Counter()
    subagent_completions: Counter[str] = Counter()
    session_denials: Counter[str] = Counter()
    phase_transitions = 0
    approval_matches = 0
    # #7 delivery telemetry: bucket injected tokens by where they actually went
    # (model vs debug-log/INERT vs unknown), and track which injection sources
    # are inert. A rule sent to dead bare-stdout must NOT count the same as one
    # the model saw -- that blindness is why the inert injectors went unnoticed.
    inject_tokens_by_delivery: Counter[str] = Counter()
    inject_tokens_by_source: dict[str, Counter[str]] = defaultdict(Counter)

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
            delivery = classify_delivery(e.get("event_name"), e.get("mechanism"))
            tok = int(e.get("tokens_injected") or 0)
            inject_tokens_by_delivery[delivery] += tok
            inject_tokens_by_source[e.get("query_source") or "rag_query"][delivery] += tok
        elif evt == "always_on_inject":
            delivery = classify_delivery(e.get("event_name"), e.get("mechanism"))
            tok = int(e.get("tokens") or 0)
            inject_tokens_by_delivery[delivery] += tok
            inject_tokens_by_source["always_on"][delivery] += tok

        single_rid = e.get("rule_id")
        if single_rid and evt != "rag_query":
            rule_hits[single_rid] += 1
        elif evt == "write_attempt":
            # write_attempt carries the rich decision telemetry. gate_status is
            # the "why" (phase-a, subagent_bypass, ...); result (allow/deny) is
            # the fallback. The retired pre_write_decision event was bare, which
            # is why this summary used to be 100% "unknown".
            key = e.get("gate_status") or e.get("result") or "unknown"
            write_decisions[key] += 1
        elif evt == "subagent_complete":
            agent_type = e.get("agent_type") or "general-purpose"
            subagent_completions[agent_type] += 1
        elif evt == "gate_denial":
            sess = e.get("session", "unknown")
            session_denials[sess] += 1
        elif evt == "phase_transition":
            phase_transitions += 1
        elif evt == "approval_pattern_match":
            approval_matches += 1

    hook_p95: dict[str, int] = {
        name: percentile(durs, 95) for name, durs in hook_durations.items()
    }

    # Sources whose injected tokens went (even partly) to an INERT bucket: rules paid for
    # that the model never saw, whether CC filed them in the debug log or rejected the
    # payload outright. Sorted by inert tokens, biggest first.
    def _inert(by: Counter[str]) -> int:
        return sum(by[d] for d in INERT_DELIVERIES)

    inert_inject_sources = {
        src: _inert(by)
        for src, by in sorted(
            inject_tokens_by_source.items(), key=lambda kv: -_inert(kv[1])
        )
        if _inert(by) > 0
    }

    return {
        "total_events": len(events),
        "event_counts": dict(event_counts.most_common(top)),
        "hook_p95_ms": dict(sorted(hook_p95.items(), key=lambda kv: -kv[1])[:top]),
        "top_rules": dict(rule_hits.most_common(top)),
        "write_decisions": dict(write_decisions),
        "subagent_completions": dict(subagent_completions),
        "sessions_with_denials": dict(session_denials.most_common(top)),
        "phase_transitions": phase_transitions,
        "approval_matches": approval_matches,
        "inject_tokens_by_delivery": dict(inject_tokens_by_delivery),
        "inert_inject_sources": inert_inject_sources,
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

    # #7: where injected rule-tokens actually went. debug-log and rejected tokens are both
    # INERT: the hook paid the cost but the model never saw the rules.
    from writ.shared.delivery import INERT_DELIVERIES

    by_delivery = summary.get("inject_tokens_by_delivery") or {}
    if by_delivery:
        lines.append("Rule-injection delivery (tokens):")
        order = {"model": 0, "debug-log": 1, "rejected": 2, "unknown": 3, "user": 4, "state": 5}
        for delivery, tok in sorted(by_delivery.items(), key=lambda kv: order.get(kv[0], 9)):
            tag = "  (INERT -- model never saw these)" if delivery in INERT_DELIVERIES else ""
            lines.append(f"  -> {delivery:12s} {tok:>7d}{tag}")
        inert = summary.get("inert_inject_sources") or {}
        if inert:
            lines.append("  INERT injection sources (rules paid for, not delivered):")
            for src, tok in inert.items():
                lines.append(f"    {src:18s} {tok:>7d}")
        lines.append("")

    if summary["write_decisions"]:
        lines.append("Write-gate decisions (by gate_status):")
        for status, count in summary["write_decisions"].items():
            lines.append(f"  {status:20s} {count:>4d}")
        lines.append("")

    if summary["subagent_completions"]:
        lines.append("Sub-agent completions:")
        for agent_type, count in summary["subagent_completions"].items():
            lines.append(f"  {agent_type:30s} {count:>3d}")
        lines.append("")

    lines.append("Gate activity:")
    lines.append(f"  approval_pattern_match {summary['approval_matches']:>5d}")
    lines.append(f"  phase_transitions      {summary['phase_transitions']:>5d}")
    if summary["sessions_with_denials"]:
        lines.append("  sessions with gate denials:")
        for sess, count in summary["sessions_with_denials"].items():
            lines.append(f"    {sess}  ({count})")

    return "\n".join(lines) + "\n"


def rotate_if_needed(
    log_path: Path,
    threshold_bytes: int = DEFAULT_ROTATION_THRESHOLD_BYTES,
) -> bool:
    """Rotate the log if it exceeds the size threshold."""
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


# ============================================================================
# Phase 5: result models + analyzer functions
# ============================================================================


class RuleEffectivenessRow(BaseModel):
    rule_id: str
    activations: int
    stuck_denials: int
    denial_stick_rate: float
    rationalizations: int


class SkillUsageRow(BaseModel):
    skill_id: str
    loads: int
    completions: int
    completion_rate: float


class PlaybookComplianceRow(BaseModel):
    playbook_id: str
    runs: int
    compliant_runs: int
    common_skip_points: list[str]


class GraduationCandidate(BaseModel):
    rule_id: str
    days_stable: int
    current_tier: str
    recommended_tier: str
    denial_stick_rate: float


class TrimCandidate(BaseModel):
    entity_id: str
    entity_type: str  # "rule" or "skill"
    last_activation: str | None
    activations_in_window: int
    recommendation: str


class QualityJudgeOverride(BaseModel):
    rubric: str
    total_fails: int
    overrides: int
    override_rate: float


# --- Helpers shared across analyzers ----------------------------------------


def _parse_ts(ts: str) -> datetime | None:
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None


def _within_window(
    events: Iterable[FrictionEvent], since_days: int,
    as_of: datetime | None = None,
) -> list[FrictionEvent]:
    if since_days <= 0:
        return list(events)
    cutoff = (as_of or datetime.now(timezone.utc)) - timedelta(days=since_days)
    out: list[FrictionEvent] = []
    for e in events:
        t = _parse_ts(e.ts)
        if t is None:
            continue
        if t.tzinfo is None:
            t = t.replace(tzinfo=timezone.utc)
        if t >= cutoff:
            out.append(e)
    return out


def _session_grouped(
    events: Iterable[FrictionEvent],
) -> dict[str, list[FrictionEvent]]:
    grouped: dict[str, list[FrictionEvent]] = defaultdict(list)
    for e in events:
        grouped[e.session].append(e)
    for sid in grouped:
        grouped[sid].sort(key=lambda e: e.ts)
    return dict(grouped)


def _extract_rule_ids(ev: FrictionEvent) -> list[str]:
    """Pull rule_ids out of a rag_query bundle, single rule_id, or empty."""
    rule_ids = _ev_field(ev, "rule_ids")
    if isinstance(rule_ids, list):
        return [r for r in rule_ids if isinstance(r, str)]
    rid = _ev_field(ev, "rule_id")
    return [rid] if isinstance(rid, str) else []


def _ev_field(ev: FrictionEvent, key: str) -> Any:
    """Read a field from a FrictionEvent (declared or extra).

    getattr resolves both declared fields and extra="allow" fields, so this is
    equivalent to model_dump().get(key) but without serializing the whole model
    on every field access (this runs in per-event analyzer loops, F-4).
    """
    return getattr(ev, key, None)


# --- Analyzer 1: rule effectiveness -----------------------------------------

_STUCK_WINDOW = timedelta(minutes=30)


def analyze_rule_effectiveness(
    events: list[FrictionEvent],
    since_days: int = 30,
    top: int = 50,
    as_of: datetime | None = None,
) -> list[RuleEffectivenessRow]:
    """Per rule: activations, stuck denials, denial-stick-rate, rationalizations.

    A gate_denial is "stuck" if no approval_pattern_match or
    phase_advance for the same rule appears in the same session within
    30 minutes after it.
    """
    events = _within_window(events, since_days, as_of)
    grouped = _session_grouped(events)

    activations: Counter[str] = Counter()
    stuck: Counter[str] = Counter()
    rationalizations: Counter[str] = Counter()

    for sid, ses_events in grouped.items():
        for i, e in enumerate(ses_events):
            if e.event == "rag_query":
                for rid in _extract_rule_ids(e):
                    activations[rid] += 1
            elif e.event == "gate_denial" and e.rule_id:
                # Look ahead within session for an unsticking event.
                t0 = _parse_ts(e.ts)
                if t0 is None:
                    continue
                resolved = False
                for nxt in ses_events[i + 1:]:
                    t = _parse_ts(nxt.ts)
                    if t is None or (t - t0) > _STUCK_WINDOW:
                        break
                    if (
                        nxt.event in ("approval_pattern_match", "phase_advance")
                        and (nxt.rule_id == e.rule_id or _ev_field(nxt, "gate") == _ev_field(e, "gate"))
                    ):
                        resolved = True
                        break
                if not resolved:
                    stuck[e.rule_id] += 1
                # Single-rule denials count toward activations even without rag_query.
                activations[e.rule_id] += 0
            elif e.event == "repeated_denial" and e.rule_id:
                rationalizations[e.rule_id] += 1

    rows: list[RuleEffectivenessRow] = []
    all_rules = set(activations) | set(stuck) | set(rationalizations)
    for rid in all_rules:
        a = activations.get(rid, 0)
        s = stuck.get(rid, 0)
        rate = (s / a) if a > 0 else 0.0
        rows.append(RuleEffectivenessRow(
            rule_id=rid,
            activations=a,
            stuck_denials=s,
            denial_stick_rate=rate,
            rationalizations=rationalizations.get(rid, 0),
        ))
    rows.sort(key=lambda r: (-r.stuck_denials, -r.activations, r.rule_id))
    return rows[:top]


# --- Analyzer 2: skill usage -------------------------------------------------


def analyze_skill_usage(
    events: list[FrictionEvent],
    since_days: int = 60,
    top: int = 50,
    as_of: datetime | None = None,
) -> list[SkillUsageRow]:
    """Per skill: loads vs sessions where the relevant playbook completed.

    Skill load = rag_query event with skill_id or a SKL-* rule_id.
    Completion = same session also has a playbook_step_complete event
    where step_index + 1 == total_steps.
    """
    events = _within_window(events, since_days, as_of)
    grouped = _session_grouped(events)

    skill_sessions: dict[str, set[str]] = defaultdict(set)
    completed_sessions: set[str] = set()

    for sid, ses_events in grouped.items():
        for e in ses_events:
            if e.event == "rag_query":
                skill = _ev_field(e, "skill_id")
                if isinstance(skill, str):
                    skill_sessions[skill].add(sid)
                # Treat any SKL-* rule_id as a skill load.
                for rid in _extract_rule_ids(e):
                    if rid.startswith("SKL-"):
                        skill_sessions[rid].add(sid)
            elif e.event == "playbook_step_complete":
                idx = _ev_field(e, "step_index")
                total = _ev_field(e, "total_steps")
                if isinstance(idx, int) and isinstance(total, int) and idx + 1 >= total:
                    completed_sessions.add(sid)

    rows: list[SkillUsageRow] = []
    for skill, sessions in skill_sessions.items():
        loads = len(sessions)
        completions = len(sessions & completed_sessions)
        rate = (completions / loads) if loads > 0 else 0.0
        rows.append(SkillUsageRow(
            skill_id=skill,
            loads=loads,
            completions=completions,
            completion_rate=rate,
        ))
    rows.sort(key=lambda r: (-r.loads, r.skill_id))
    return rows[:top]


# --- Analyzer 3: playbook compliance ----------------------------------------


def _group_playbook_runs(grouped: dict) -> dict:
    """Group each session's playbook_step_complete events by playbook_id into per-playbook
    run lists (one run = one playbook's steps observed within one session)."""
    runs_by_pb: dict[str, list[list[FrictionEvent]]] = defaultdict(list)
    for sid, ses_events in grouped.items():
        per_pb: dict[str, list[FrictionEvent]] = defaultdict(list)
        for e in ses_events:
            if e.event != "playbook_step_complete":
                continue
            pb = _ev_field(e, "playbook_id")
            if isinstance(pb, str):
                per_pb[pb].append(e)
        for pb, evs in per_pb.items():
            if evs:
                runs_by_pb[pb].append(evs)
    return runs_by_pb


def _run_compliance(run: list[FrictionEvent]) -> tuple[bool, list[str]]:
    """Evaluate one playbook run: compliant iff its steps are contiguous-in-order AND the run
    reached the end. Returns (is_compliant, out_of_order_step_ids). A "skip point" is the
    step_id of an out-of-place observation -- a step whose index doesn't equal its position in
    the run. A run with no usable (index, id) pairs, or an in-order run that didn't reach the
    end, returns (False, []) -- not compliant, contributing no skip points (matching the
    original's `continue` and empty-skip cases exactly)."""
    pairs: list[tuple[int, str]] = []
    for e in run:
        idx = _ev_field(e, "step_index")
        sid = _ev_field(e, "step_id")
        if isinstance(idx, int) and isinstance(sid, str):
            pairs.append((idx, sid))
    if not pairs:
        return False, []
    indices = [p[0] for p in pairs]
    in_order = (
        bool(indices)
        and indices == list(range(indices[0], indices[0] + len(indices)))
    )
    total_known = _ev_field(run[-1], "total_steps")
    reached_end = (
        not isinstance(total_known, int)
        or max(indices) >= total_known - 1
    )
    if in_order and reached_end:
        return True, []
    return False, [sid for position, (idx, sid) in enumerate(pairs) if idx != position]


def _compliance_row(pb: str, runs: list) -> PlaybookComplianceRow:
    """Aggregate one playbook's runs into a compliance row: compliant-run count plus the most
    common skip-point step_ids across all non-compliant runs."""
    compliant = 0
    skip_points: Counter[str] = Counter()
    for run in runs:
        is_compliant, skip_ids = _run_compliance(run)
        if is_compliant:
            compliant += 1
        else:
            for sid in skip_ids:
                skip_points[sid] += 1
    return PlaybookComplianceRow(
        playbook_id=pb,
        runs=len(runs),
        compliant_runs=compliant,
        common_skip_points=[sid for sid, _ in skip_points.most_common(5)],
    )


def analyze_playbook_compliance(
    events: list[FrictionEvent],
    since_days: int = 30,
    top: int = 50,
    as_of: datetime | None = None,
) -> list[PlaybookComplianceRow]:
    """Per playbook: runs, compliant runs (in-order, no skipped indices),
    plus the most common skip-point step_ids across all non-compliant
    runs.
    """
    events = _within_window(events, since_days, as_of)
    grouped = _session_grouped(events)
    runs_by_pb = _group_playbook_runs(grouped)
    rows = [_compliance_row(pb, runs) for pb, runs in runs_by_pb.items()]
    rows.sort(key=lambda r: (-r.runs, r.playbook_id))
    return rows[:top]


# --- Analyzer 4: graduation candidates --------------------------------------


def analyze_graduation_candidates(
    events: list[FrictionEvent],
    days_stable: int = 30,
    stick_rate_threshold: float = 0.85,
    max_rationalizations: int = 5,
    top: int = 50,
    as_of: datetime | None = None,
) -> list[GraduationCandidate]:
    """Rules with high stuck-denial rate and low rationalization count.

    days_stable is reported on the row for the human reviewer's context;
    selection itself runs on all events to capture cumulative stability.
    A rule qualifies if denial_stick_rate >= stick_rate_threshold and
    rationalizations < max_rationalizations.
    """
    rows = analyze_rule_effectiveness(events, since_days=0, top=10_000, as_of=as_of)
    candidates: list[GraduationCandidate] = []
    for r in rows:
        if r.activations < 5:
            continue
        if r.denial_stick_rate < stick_rate_threshold:
            continue
        if r.rationalizations >= max_rationalizations:
            continue
        candidates.append(GraduationCandidate(
            rule_id=r.rule_id,
            days_stable=days_stable,
            current_tier="probationary",
            recommended_tier="canonical",
            denial_stick_rate=r.denial_stick_rate,
        ))
    candidates.sort(key=lambda c: (-c.denial_stick_rate, c.rule_id))
    return candidates[:top]


# --- Analyzer 5: trim candidates --------------------------------------------


def analyze_trim_candidates(
    events: list[FrictionEvent],
    since_days: int = 90,
    rule_min_activations: int = 5,
    skill_min_loads: int = 2,
    top: int = 100,
    as_of: datetime | None = None,
) -> list[TrimCandidate]:
    """Rules with <N activations in window; skills with <M loads in window.

    Scans the FULL log to identify every rule / skill the system has
    ever seen, then counts activations within the window. Entities
    that fall below threshold (including those with zero recent
    activity) are flagged.
    """
    universe_rules: set[str] = set()
    universe_skills: set[str] = set()
    last_seen: dict[str, str] = {}
    for e in events:
        for rid in _extract_rule_ids(e):
            if rid.startswith("SKL-"):
                universe_skills.add(rid)
            else:
                universe_rules.add(rid)
            last_seen[rid] = max(e.ts, last_seen.get(rid, ""))
        if e.rule_id:
            if e.rule_id.startswith("SKL-"):
                universe_skills.add(e.rule_id)
            else:
                universe_rules.add(e.rule_id)
            last_seen[e.rule_id] = max(e.ts, last_seen.get(e.rule_id, ""))
        skill = _ev_field(e, "skill_id")
        if isinstance(skill, str):
            universe_skills.add(skill)
            last_seen[skill] = max(e.ts, last_seen.get(skill, ""))

    events = _within_window(events, since_days, as_of)

    rule_acts: Counter[str] = Counter()
    rule_denials: Counter[str] = Counter()
    rule_last_seen: dict[str, str] = dict(last_seen)
    skill_loads: Counter[str] = Counter()
    skill_last_seen: dict[str, str] = dict(last_seen)

    for e in events:
        if e.event == "rag_query":
            for rid in _extract_rule_ids(e):
                rule_acts[rid] += 1
                rule_last_seen[rid] = e.ts
                if rid.startswith("SKL-"):
                    skill_loads[rid] += 1
                    skill_last_seen[rid] = e.ts
            skill = _ev_field(e, "skill_id")
            if isinstance(skill, str):
                skill_loads[skill] += 1
                skill_last_seen[skill] = e.ts
        elif e.event == "gate_denial" and e.rule_id:
            rule_denials[e.rule_id] += 1
            rule_last_seen[e.rule_id] = e.ts

    candidates: list[TrimCandidate] = []
    for rid in universe_rules:
        if rule_acts[rid] < rule_min_activations and rule_denials[rid] == 0:
            candidates.append(TrimCandidate(
                entity_id=rid,
                entity_type="rule",
                last_activation=rule_last_seen.get(rid),
                activations_in_window=rule_acts[rid],
                recommendation="trim or consolidate",
            ))

    for skill in universe_skills:
        loads = skill_loads.get(skill, 0)
        if loads < skill_min_loads:
            candidates.append(TrimCandidate(
                entity_id=skill,
                entity_type="skill",
                last_activation=skill_last_seen.get(skill),
                activations_in_window=loads,
                recommendation="deprecate",
            ))

    candidates.sort(key=lambda c: (c.activations_in_window, c.entity_id))
    return candidates[:top]


# --- Analyzer 6: quality-judge false positives ------------------------------


def analyze_quality_judge_false_positives(
    events: list[FrictionEvent],
    since_days: int = 30,
    top: int = 50,
    as_of: datetime | None = None,
) -> list[QualityJudgeOverride]:
    """Per rubric: total fail judgments + override count + rate.

    Override = the user proceeded despite the judge saying fail.
    A high override rate suggests the rubric is too strict (false
    positive) and needs refinement.
    """
    events = _within_window(events, since_days, as_of)

    fails: Counter[str] = Counter()
    overrides: Counter[str] = Counter()

    for e in events:
        if e.event != "quality_judgment":
            continue
        if _ev_field(e, "decision") != "fail":
            continue
        rubric = _ev_field(e, "rubric")
        if not isinstance(rubric, str):
            rubric = "unknown"
        fails[rubric] += 1
        if _ev_field(e, "override"):
            overrides[rubric] += 1

    rows: list[QualityJudgeOverride] = []
    for rubric, total in fails.items():
        ovr = overrides[rubric]
        rate = ovr / total if total > 0 else 0.0
        rows.append(QualityJudgeOverride(
            rubric=rubric,
            total_fails=total,
            overrides=ovr,
            override_rate=rate,
        ))
    rows.sort(key=lambda r: (-r.override_rate, -r.total_fails, r.rubric))
    return rows[:top]


# ============================================================================
# audit-session: per-session event aggregation + JSON/text rendering
# (relocated verbatim from writ/cli.py; consumed by `writ audit-session`)
# ============================================================================


def _agg_phase_advance(e: dict, agg: dict) -> None:
    agg["phase_transitions"].append({
        "ts": e.get("ts"),
        "from": e.get("from_phase"),
        "to": e.get("to_phase"),
        "source": e.get("confirmation_source"),
    })


def _agg_rag_query(e: dict, agg: dict) -> None:
    from writ.shared.delivery import classify_delivery
    qs = e.get("query_source") or "unknown"
    tok = int(e.get("tokens_injected") or 0)
    agg["tokens_by_source"][qs] += tok
    # #7: bucket by where the injection actually landed (model vs debug-log/INERT).
    agg["tokens_by_delivery"][classify_delivery(e.get("event_name"), e.get("mechanism"))] += tok
    for rid in (e.get("rule_ids") or []):
        if isinstance(rid, str):
            agg["rule_loads"][rid] += 1
            if rid.startswith("SKL-"):
                agg["skill_loads"][rid] += 1
            elif rid.startswith("PBK-"):
                agg["playbook_loads"][rid] += 1


def _agg_always_on_inject(e: dict, agg: dict) -> None:
    from writ.shared.delivery import classify_delivery
    agg["always_on_injects"] += 1
    tok = int(e.get("tokens") or 0)
    agg["always_on_tokens"] += tok
    agg["tokens_by_delivery"][classify_delivery(e.get("event_name"), e.get("mechanism"))] += tok


def _agg_gate_denial(e: dict, agg: dict) -> None:
    agg["gate_denials"].append({"ts": e.get("ts"), "rule_id": e.get("rule_id")})


def _agg_subagent(e: dict, agg: dict) -> None:
    agg["subagents"].append({
        "ts": e.get("ts"),
        "kind": e.get("event"),
        "type": e.get("subagent_type"),
    })


def _agg_playbook_step_complete(e: dict, agg: dict) -> None:
    agg["playbook_completions"].append({
        "ts": e.get("ts"),
        "playbook_id": e.get("playbook_id"),
        "step_id": e.get("step_id"),
        "step_index": e.get("step_index"),
        "total_steps": e.get("total_steps"),
    })


def _agg_mode_change(e: dict, agg: dict) -> None:
    agg["mode_changes"].append({
        "ts": e.get("ts"),
        "from": e.get("from_mode"),
        "to": e.get("to_mode"),
    })


def _agg_methodology_push(e: dict, agg: dict) -> None:
    # 1.8c push observability: tally action -> events and channel -> nodes.
    agg["push_by_action"][e.get("action") or "?"] += 1
    for cname, cnt in (e.get("channels") or {}).items():
        agg["push_channel_counts"][cname] += int(cnt or 0)


# event -> handler that folds it into the aggregate. subagent_start/complete share
# one handler (it reads e["event"] for the "kind"); events with no entry only bump
# event_counts. Adding a tracked event is one registry line.
_SESSION_EVENT_HANDLERS = {
    "phase_advance": _agg_phase_advance,
    "rag_query": _agg_rag_query,
    "always_on_inject": _agg_always_on_inject,
    "gate_denial": _agg_gate_denial,
    "subagent_start": _agg_subagent,
    "subagent_complete": _agg_subagent,
    "playbook_step_complete": _agg_playbook_step_complete,
    "mode_change": _agg_mode_change,
    "methodology_push": _agg_methodology_push,
}


def aggregate_session(session_events: list[dict]) -> dict:
    """Aggregate one session's friction events into the counters/timelines the
    audit renderers consume. Pure (no I/O); feeds the JSON and text renderers from a
    single source so they cannot drift. Extracted from audit_session (was CC=46)."""
    from collections import Counter

    agg: dict = {
        "event_counts": Counter(),
        "phase_transitions": [],
        "rule_loads": Counter(),
        "skill_loads": Counter(),
        "playbook_loads": Counter(),
        "gate_denials": [],
        "subagents": [],
        "tokens_by_source": Counter(),
        "tokens_by_delivery": Counter(),  # #7: model vs debug-log (INERT) vs unknown
        "always_on_injects": 0,
        "always_on_tokens": 0,
        "playbook_completions": [],
        "mode_changes": [],
        "push_by_action": Counter(),      # 1.8c: action -> push events
        "push_channel_counts": Counter(),  # 1.8c: channel -> nodes delivered
    }

    for e in session_events:
        ev = e.get("event", "?")
        agg["event_counts"][ev] += 1
        handler = _SESSION_EVENT_HANDLERS.get(ev)
        if handler is not None:
            handler(e, agg)

    return agg


def render_audit_json(session_id: str, session_events: list[dict], agg: dict) -> str:
    """Render the audit-session JSON payload from the aggregate (audit_session --json)."""
    import json as _json
    return _json.dumps({
        "session": session_id,
        "event_count": len(session_events),
        "first_ts": session_events[0].get("ts"),
        "last_ts": session_events[-1].get("ts"),
        "event_counts": dict(agg["event_counts"]),
        "phase_transitions": agg["phase_transitions"],
        "mode_changes": agg["mode_changes"],
        "rule_loads": dict(agg["rule_loads"].most_common()),
        "skill_loads": dict(agg["skill_loads"].most_common()),
        "playbook_loads": dict(agg["playbook_loads"].most_common()),
        "gate_denials": agg["gate_denials"],
        "subagents": agg["subagents"],
        "playbook_completions": agg["playbook_completions"],
        "always_on_injects": agg["always_on_injects"],
        "always_on_tokens": agg["always_on_tokens"],
        "tokens_by_source": dict(agg["tokens_by_source"]),
        "tokens_by_delivery": dict(agg["tokens_by_delivery"]),

        "push_by_action": dict(agg["push_by_action"].most_common()),
        "push_channels": dict(agg["push_channel_counts"]),
    }, indent=2)


def render_audit_text(session_id: str, session_events: list[dict], agg: dict) -> str:
    """Render the human-readable audit-session report from the aggregate."""
    out: list[str] = []
    out.append(f"=== Session audit: {session_id} ===")
    out.append(f"Events: {len(session_events)} "
               f"(first={session_events[0].get('ts')}, last={session_events[-1].get('ts')})")

    if agg["mode_changes"]:
        out.append("")
        out.append("Mode changes:")
        for m in agg["mode_changes"]:
            out.append(f"  {m['ts']}  {m['from']} -> {m['to']}")

    if agg["phase_transitions"]:
        out.append("")
        out.append("Phase progression:")
        for p in agg["phase_transitions"]:
            out.append(f"  {p['ts']}  {p['from']} -> {p['to']}  ({p['source']})")

    out.append("")
    out.append("Tokens injected by source:")
    if agg["always_on_injects"]:
        out.append(f"  always_on        {agg['always_on_tokens']:>6}  ({agg['always_on_injects']} injects)")
    for src, tok in agg["tokens_by_source"].most_common():
        out.append(f"  {src:<16} {tok:>6}")

    if agg["tokens_by_delivery"]:
        out.append("")
        out.append("Tokens by delivery (#7):")
        from writ.shared.delivery import INERT_DELIVERIES

        order = {"model": 0, "debug-log": 1, "rejected": 2, "unknown": 3, "user": 4, "state": 5}
        for delivery, tok in sorted(agg["tokens_by_delivery"].items(),
                                    key=lambda kv: order.get(kv[0], 9)):
            tag = "  (INERT)" if delivery in INERT_DELIVERIES else ""
            out.append(f"  -> {delivery:<12} {tok:>6}{tag}")

    if agg["push_by_action"]:
        out.append("")
        out.append("Push-by-action:")
        for action, n in agg["push_by_action"].most_common():
            out.append(f"  {action:<16} {n}")
        ch = agg["push_channel_counts"]
        out.append(
            f"  channels: floor={ch.get('floor', 0)} "
            f"push={ch.get('push', 0)} pull={ch.get('pull', 0)}"
        )

    if agg["skill_loads"]:
        out.append("")
        out.append("Skill loads:")
        for sid, n in agg["skill_loads"].most_common():
            out.append(f"  {sid:<32} {n}")

    if agg["playbook_loads"]:
        out.append("")
        out.append("Playbook loads:")
        for pid, n in agg["playbook_loads"].most_common():
            out.append(f"  {pid:<32} {n}")

    if agg["gate_denials"]:
        out.append("")
        out.append(f"Gate denials: {len(agg['gate_denials'])}")
        for g in agg["gate_denials"]:
            out.append(f"  {g['ts']}  denied: {g['rule_id']}")

    if agg["subagents"]:
        out.append("")
        out.append("Subagent dispatches:")
        for s in agg["subagents"]:
            out.append(f"  {s['ts']}  {s['kind']:<18} {s['type']}")

    if agg["playbook_completions"]:
        out.append("")
        out.append("Playbook step completions:")
        for c in agg["playbook_completions"]:
            out.append(f"  {c['ts']}  {c['playbook_id']} step {c['step_index']}/{c['total_steps']} ({c['step_id']})")

    out.append("")
    out.append("Top event types:")
    for ev, n in agg["event_counts"].most_common(10):
        out.append(f"  {ev:<28} {n}")

    return "\n".join(out)
