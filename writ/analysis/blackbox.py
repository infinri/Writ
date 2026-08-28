"""Pure reader for the blackbox capture log: records in, payload census out.

The user's method rule for this cycle was "do not put too much faith in the Claude Code
docs, use our own black box to map the requests in and out so we see what is truly being
used". This module is the reader half of that: it turns the JSONL that
`bin/lib/common.sh::blackbox_log` appends into a per (event, hook, direction) census of the
keys that were ACTUALLY exchanged, with counts and a timestamp window.

It writes nothing and captures nothing. `writ blackbox-census` is the thin CLI wrapper that
reads a capture log and writes `docs/reference/blackbox-census.json`; extending capture
coverage is a separate cycle's work, and this module deliberately cannot do it.

ABSENCE IS STATED, NEVER INFERRED. The census carries an explicit `events_never_observed`
list derived from `hooks/hooks.json`, so "we have no data for PreToolUse OUT" is a row in the
file rather than a missing key a reader might mistake for "no keys". Capture was only just
switched on, so on the first generation nearly everything is in that list. That is the
correct starting state, not a defect.

stdlib plus `writ.shared.delivery` only, so it stays runnable from a hook or a test with no
daemon and no graph.
"""

from __future__ import annotations

import json
from pathlib import Path

from writ.analysis.jsonl import read_jsonl
from writ.shared.delivery import classify_delivery

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
_HOOKS_JSON = _REPO_ROOT / "hooks" / "hooks.json"

# The hookSpecificOutput / top-level keys whose PRESENCE names a delivery mechanism in
# `writ.shared.delivery`'s vocabulary. Each is validated through classify_delivery before it
# is recorded, so a key that module does not recognize is never counted as a mechanism.
_MECHANISM_KEYS = ("permissionDecisionReason", "additionalContext", "systemMessage")

# An OUT payload carrying none of the keys above delivered by plain stdout. Named here
# rather than inferred at the call site, because "no recognized key" and "no output at all"
# are different facts and only the first one is a mechanism.
_STDOUT_MECHANISM = "stdout"

_DIRECTION_IN = "in"
_DIRECTION_OUT = "out"


def read_capture_records(path: str | Path) -> list[dict]:
    """Read one capture JSONL file into its raw records. Absent or empty file -> [].

    Each row is `{"ts", "hook", "direction", "session", "pid", "payload"}`, where `payload`
    is the RAW JSON-encoded string of the envelope or output Claude Code exchanged with the
    hook. A line that does not parse as JSON contributes ZERO records and is skipped; the
    valid lines around it are still returned, because a single truncated append (the log is
    written by concurrent hooks) must not discard a whole capture window.
    """
    try:
        return list(read_jsonl(path))
    except OSError:
        return []


def _parse_payload(record: dict) -> dict:
    """The record's `payload` string parsed as a JSON object, or {} when it is not one.

    A payload that is valid JSON but not an object (a bare string from a hook that printed
    plain text) yields {} here: it has no keys to census, and the OUT mechanism logic below
    reads that as stdout rather than as a structured reply.
    """
    raw = record.get("payload")
    if not isinstance(raw, str) or not raw.strip():
        return {}
    try:
        parsed = json.loads(raw)
    except (ValueError, json.JSONDecodeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _event_name(payload: dict, direction: str) -> str:
    """The CC event this record belongs to.

    IN records name it at the top level (`hook_event_name`); OUT records name it inside
    `hookSpecificOutput.hookEventName`, which is the field CC's own validator keys on.
    Neither present -> "unknown", so a record is censused under a stated non-answer instead
    of being dropped and silently reducing a count.
    """
    if direction == _DIRECTION_OUT:
        hso = payload.get("hookSpecificOutput")
        if isinstance(hso, dict) and hso.get("hookEventName"):
            return str(hso["hookEventName"])
    if payload.get("hook_event_name"):
        return str(payload["hook_event_name"])
    return "unknown"


def _bump(counter: dict, key: str) -> None:
    counter[key] = counter.get(key, 0) + 1


def _observed_mechanisms(payload: dict, event: str) -> list[str]:
    """The delivery mechanisms this OUT payload actually used.

    Both levels are checked: `permissionDecisionReason` and `additionalContext` live inside
    `hookSpecificOutput`, while `systemMessage` is a top-level sibling of it. Each candidate
    is passed through `classify_delivery`, and only a mechanism that module has a real answer
    for is recorded, because a provenance tag must never rest on a name nothing classifies.
    """
    hso = payload.get("hookSpecificOutput")
    hso = hso if isinstance(hso, dict) else {}
    found = [
        name for name in _MECHANISM_KEYS
        if name in hso or name in payload
    ]
    if not found and payload:
        found = [_STDOUT_MECHANISM]
    return [m for m in found if classify_delivery(event, m) != "unknown"]


def _registered_events(hooks_json_path: str | Path | None) -> list[str]:
    """Every CC event `hooks/hooks.json` registers a hook for. Unreadable manifest -> []."""
    path = Path(hooks_json_path) if hooks_json_path else _HOOKS_JSON
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError):
        return []
    hooks = manifest.get("hooks")
    return sorted(hooks.keys()) if isinstance(hooks, dict) else []


def _new_entry(event: str, hook: str, direction: str, ts: str) -> dict:
    entry = {
        "event": event,
        "hook": hook,
        "direction": direction,
        "count": 0,
        "first_ts": ts,
        "last_ts": ts,
        "keys": {},
    }
    if direction == _DIRECTION_IN:
        entry["tool_input_keys"] = {}
    else:
        entry["hook_specific_output_keys"] = {}
        entry["mechanisms"] = {}
    return entry


def _fold_in_record(entry: dict, payload: dict) -> None:
    """IN half: the tool_input key set per tool_name, which is the field guess this cycle's
    successor has to settle. Kept per tool_name because `tool_input` is tool-shaped, so one
    merged key set would report `file_path` and `command` as siblings that never co-occur."""
    tool_name = str(payload.get("tool_name") or "")
    tool_input = payload.get("tool_input")
    if not tool_name or not isinstance(tool_input, dict):
        return
    per_tool = entry["tool_input_keys"].setdefault(tool_name, {})
    for key in tool_input:
        _bump(per_tool, str(key))


def _fold_out_record(entry: dict, payload: dict, event: str) -> None:
    hso = payload.get("hookSpecificOutput")
    if isinstance(hso, dict):
        for key in hso:
            _bump(entry["hook_specific_output_keys"], str(key))
    for mechanism in _observed_mechanisms(payload, event):
        _bump(entry["mechanisms"], mechanism)


def build_census(
    records: list[dict],
    *,
    hooks_json_path: str | Path | None = None,
    claude_code_version: str | None = None,
) -> dict:
    """Fold raw capture records into a per (event, hook, direction) census.

    Pure: records in, census out, no file IO beyond reading `hooks_json_path` (default
    `hooks/hooks.json`) to derive `events_never_observed`.

    `claude_code_version` is stamped verbatim so the census stays traceable to the build it
    was measured on; the delivery rule is a property of a specific Claude Code, and an
    undated census would be re-read as timeless.
    """
    census_records: dict[str, dict] = {}
    for record in records:
        if not isinstance(record, dict):
            continue
        direction = _DIRECTION_OUT if record.get("direction") == _DIRECTION_OUT else _DIRECTION_IN
        hook = str(record.get("hook") or "?")
        ts = str(record.get("ts") or "")
        payload = _parse_payload(record)
        event = _event_name(payload, direction)

        key = f"{event}|{hook}|{direction}"
        entry = census_records.get(key)
        if entry is None:
            entry = _new_entry(event, hook, direction, ts)
            census_records[key] = entry

        entry["count"] += 1
        # String compare, not a parsed datetime: blackbox_log writes one ISO-8601 UTC
        # format, and lexical order is chronological for it. Parsing would add a failure
        # mode (a row whose ts a future CC writes differently) for no gain.
        if ts and (not entry["first_ts"] or ts < entry["first_ts"]):
            entry["first_ts"] = ts
        if ts and ts > entry["last_ts"]:
            entry["last_ts"] = ts
        for top_key in payload:
            _bump(entry["keys"], str(top_key))
        if direction == _DIRECTION_IN:
            _fold_in_record(entry, payload)
        else:
            _fold_out_record(entry, payload, event)

    observed_events = {entry["event"] for entry in census_records.values()}
    never_observed = [
        event for event in _registered_events(hooks_json_path)
        if event not in observed_events
    ]

    window_first = min((e["first_ts"] for e in census_records.values() if e["first_ts"]), default="")
    window_last = max((e["last_ts"] for e in census_records.values() if e["last_ts"]), default="")

    return {
        "claude_code_version": claude_code_version or "unknown",
        "capture_window": {"first_ts": window_first, "last_ts": window_last},
        "record_count": sum(e["count"] for e in census_records.values()),
        "records": census_records,
        "events_never_observed": never_observed,
    }
