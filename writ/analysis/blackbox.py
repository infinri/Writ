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
correct starting state, not a defect. `hooks_never_captured`, `directions_never_observed`,
`edit_replace_all`, `write_target_extensions` and `write_rows_without_file_path` extend that
rule: each is present with an explicit zero or empty value even when it has nothing to say.

CONTAMINATION IS PARTITIONED, NOT FILTERED. Roughly a third of the live capture is hand-built
probe payloads of shape `{session_id, tool_name, tool_input}` fed to hooks during latency
measurement, and filing them under an event named "unknown" reads as real traffic that failed
to parse. Every row is classified on a POSITIVE fingerprint into one of three origins, and
only the PROVEN synthetic ones move into the `synthetic_records` sibling. A row we cannot
place stays in `records` and is counted as `undetermined`, because a provenance claim must
narrow on evidence rather than on doubt, and because moving the doubtful rows out would strip
every OUT record class of its evidence and report a delivery gap that is really a capture gap.

`records` IS THE ONE ARTIFACT KEY, and `synthetic_records` is its sibling rather than an
alternative spelling of it. `writ/shared/delivery.py` reads exactly one key on purpose (see
its `_census_record_classes` docstring): two accepted spellings for one artifact means a typo
in either producer reads as an empty census and tags observed entries unproven.

stdlib plus `writ.analysis.jsonl` and `writ.shared.delivery` only, so it stays runnable from a
hook or a test with no daemon and no graph.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from writ.analysis.jsonl import read_jsonl
from writ.shared.delivery import classify_delivery

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
_HOOKS_JSON = _REPO_ROOT / "hooks" / "hooks.json"

# The hook script a manifest command runs, matching the `hook` field's own convention:
# `bin/lib/common.sh::blackbox_log` records `basename "$0" .sh`, so the manifest side is
# reduced to the same spelling and the two populations are comparable without a rewrite.
_HOOK_SCRIPT_RE = re.compile(r"hooks/scripts/([^/\s]+)\.sh")

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

# The three origins a captured row can be placed in. Named constants because all three are
# reported as counts, and a bucket that exists only as a string literal at one call site is
# the bucket that silently stops being written.
_ORIGIN_SYNTHETIC = "synthetic"
_ORIGIN_HARNESS = "harness"
_ORIGIN_UNDETERMINED = "undetermined"

# The fields whose PRESENCE proves Claude Code built the envelope. `cwd`, `session_id` and
# `tool_use_id` are deliberately NOT here: a hand-built probe plausibly carries all three, and
# the committed artifact already measures a class that does (99 rows carrying
# `hook_event_name` against 78 carrying `transcript_path`). A probe has no reason to invent a
# transcript path or a prompt id, so these two are the positive fingerprint.
_HARNESS_ONLY_FIELDS = ("transcript_path", "prompt_id")

# The tools whose payload names a write target. Both carry `file_path`; only Edit carries
# `replace_all`.
_WRITE_TOOLS = ("Write", "Edit")


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


def _classify_in_origin(payload: dict) -> str:
    """Who built this IN payload, decided on what the payload HAS.

    synthetic     no `hook_event_name` key at all, which every real Claude Code envelope
                  carries and a hand-built probe does not bother with
    harness       `hook_event_name` plus at least one of `_HARNESS_ONLY_FIELDS`
    undetermined  `hook_event_name` and neither fingerprint field

    The third answer is kept rather than folded into either neighbour. Calling it synthetic
    would move rows out of `records` on the strength of a missing field, and calling it
    harness would file probe rows as real traffic, which is the `unknown` bucket's defect
    moved rather than fixed.
    """
    if "hook_event_name" not in payload:
        return _ORIGIN_SYNTHETIC
    if any(field in payload for field in _HARNESS_ONLY_FIELDS):
        return _ORIGIN_HARNESS
    return _ORIGIN_UNDETERMINED


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


def _read_manifest(hooks_json_path: str | Path | None) -> dict:
    """`hooks/hooks.json` parsed once. Unreadable or non-object manifest -> {}.

    Read once and handed to both derivations below, because the events and the scripts are
    two views of the same file and reading it twice would let them disagree.
    """
    path = Path(hooks_json_path) if hooks_json_path else _HOOKS_JSON
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError):
        return {}
    return manifest if isinstance(manifest, dict) else {}


def _registered_events(manifest: dict) -> list[str]:
    """Every CC event `hooks/hooks.json` registers a hook for."""
    hooks = manifest.get("hooks")
    return sorted(hooks.keys()) if isinstance(hooks, dict) else []


def _registered_hook_scripts(manifest: dict) -> set[str]:
    """Every hook SCRIPT the manifest registers, `.sh` stripped.

    A reader of the JSON artifact does not have `hooks/hooks.json`, so "this script has never
    been captured at all" cannot be derived downstream and is stated here instead.
    """
    hooks = manifest.get("hooks")
    if not isinstance(hooks, dict):
        return set()
    scripts: set[str] = set()
    for matcher_groups in hooks.values():
        if not isinstance(matcher_groups, list):
            continue
        for group in matcher_groups:
            if not isinstance(group, dict):
                continue
            for hook in group.get("hooks") or []:
                if not isinstance(hook, dict):
                    continue
                match = _HOOK_SCRIPT_RE.search(str(hook.get("command") or ""))
                if match:
                    scripts.add(match.group(1))
    return scripts


def _new_entry(event: str, hook: str, direction: str, ts: str, origin: str) -> dict:
    entry = {
        "event": event,
        "hook": hook,
        "direction": direction,
        "count": 0,
        "first_ts": ts,
        "last_ts": ts,
        "keys": {},
    }
    if origin != _ORIGIN_SYNTHETIC:
        # Only on the `records` side. Every row under `synthetic_records` is synthetic by
        # definition, so an origins map there would be one populated key and two zeros that
        # say nothing.
        entry["origins"] = {_ORIGIN_HARNESS: 0, _ORIGIN_UNDETERMINED: 0}
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


def _fold_write_axes(axes: dict, payload: dict) -> None:
    """The two write-path distributions the deferred tiers need as evidence.

    `edit_replace_all` is the audit's own measurement that the reconstruction defect in
    `hooks/scripts/pre-validate-file.sh` is latent rather than live. It keys on `is True`, the
    same test the fixed reconstruction uses, so an absent field and an explicit `false` count
    as the same behaviour there and here.

    `write_target_extensions` sizes the residual saving the dropped Tier 1 skip would have
    had, because both analyzer hooks exit before the analyzer whenever `detect_language`
    answers unknown. Extensions are reported RAW rather than bucketed into analyzed and
    not-analyzed: `detect_language` is a bash function, and re-encoding its extension table
    here would be a second population to go stale.
    """
    tool_name = str(payload.get("tool_name") or "")
    if tool_name not in _WRITE_TOOLS:
        return
    tool_input = payload.get("tool_input")
    tool_input = tool_input if isinstance(tool_input, dict) else {}

    if tool_name == "Edit":
        if "replace_all" not in tool_input:
            _bump(axes["edit_replace_all"], "absent")
        elif tool_input["replace_all"] is True:
            _bump(axes["edit_replace_all"], "true")
        else:
            _bump(axes["edit_replace_all"], "false")

    file_path = tool_input.get("file_path")
    if not isinstance(file_path, str) or not file_path:
        axes["write_rows_without_file_path"] += 1
        return
    _bump(axes["write_target_extensions"], Path(file_path).suffix.lstrip("."))


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
    `hooks/hooks.json`) to derive `events_never_observed` and `hooks_never_captured`.

    `claude_code_version` is stamped verbatim so the census stays traceable to the build it
    was measured on; the delivery rule is a property of a specific Claude Code, and an
    undated census would be re-read as timeless.

    ONE sequential pass over the records, no DB query, no daemon call, no network call, and
    O(1) added work per row (a fixed number of dict lookups, one suffix split for a write row,
    one same-process lookup for an OUT row), so the fold stays linear in the capture size.

    `record_count` and `capture_window` span BOTH `records` and `synthetic_records`: they
    describe the capture log, and narrowing them alongside `records` would silently change
    what two already-published numbers mean.
    """
    census_records: dict[str, dict] = {}
    synthetic_records: dict[str, dict] = {}
    origin_counts = {_ORIGIN_SYNTHETIC: 0, _ORIGIN_HARNESS: 0, _ORIGIN_UNDETERMINED: 0}
    axes = {
        "edit_replace_all": {"true": 0, "false": 0, "absent": 0},
        "write_target_extensions": {},
        "write_rows_without_file_path": 0,
    }
    # One hook process handles one envelope, so (hook, pid, session) is the join that gives an
    # OUT row the origin of the IN row it is replying to. A log interleaving a probe and a
    # real envelope for the same hook therefore attributes each OUT row to its own process
    # rather than to whichever IN row came last.
    last_in_origin: dict[tuple, str] = {}
    observed_directions: dict[str, set[str]] = {}
    observed_hooks: set[str] = set()

    for record in records:
        if not isinstance(record, dict):
            continue
        direction = _DIRECTION_OUT if record.get("direction") == _DIRECTION_OUT else _DIRECTION_IN
        hook = str(record.get("hook") or "?")
        ts = str(record.get("ts") or "")
        payload = _parse_payload(record)
        event = _event_name(payload, direction)

        process = (hook, record.get("pid"), record.get("session"))
        if direction == _DIRECTION_IN:
            origin = _classify_in_origin(payload)
            last_in_origin[process] = origin
        else:
            # An OUT payload is the hook's own reply and carries no fingerprint of its own,
            # so it INHERITS. With no IN row from the same process it is undetermined, never
            # harness: harness is only ever reached through an inheritance we can point at.
            origin = last_in_origin.get(process, _ORIGIN_UNDETERMINED)
        origin_counts[origin] += 1
        observed_hooks.add(hook)
        observed_directions.setdefault(event, set()).add(direction)

        target = synthetic_records if origin == _ORIGIN_SYNTHETIC else census_records
        key = f"{event}|{hook}|{direction}"
        entry = target.get(key)
        if entry is None:
            entry = _new_entry(event, hook, direction, ts, origin)
            target[key] = entry

        entry["count"] += 1
        if origin != _ORIGIN_SYNTHETIC:
            _bump(entry["origins"], origin)
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
            # PROVEN-SYNTHETIC ROWS ARE EXCLUDED FROM THE EVIDENCE AXES, for the same
            # reason they are excluded from `records`. These two axes are what a later
            # cycle is told to read as evidence, so a probe row counted here is a
            # contaminated number presented as a measurement, which is the exact defect
            # this cycle exists to remove. Undetermined rows still count: they stay in
            # `records` because they may be real, and the same reasoning applies here.
            if origin != _ORIGIN_SYNTHETIC:
                _fold_write_axes(axes, payload)
        else:
            _fold_out_record(entry, payload, event)

    manifest = _read_manifest(hooks_json_path)
    all_entries = list(census_records.values()) + list(synthetic_records.values())

    observed_events = {entry["event"] for entry in all_entries}
    never_observed = [
        event for event in _registered_events(manifest)
        if event not in observed_events
    ]
    hooks_never_captured = sorted(_registered_hook_scripts(manifest) - observed_hooks)

    # A missing OUT class is precisely a missing key, which is the shape a reader mistakes for
    # "nothing to report". Named per event instead, so the 60-IN-against-4-OUT asymmetry is a
    # line in the artifact rather than something a reader has to notice.
    directions_never_observed = {}
    for event in sorted(observed_directions):
        missing = [d for d in (_DIRECTION_IN, _DIRECTION_OUT) if d not in observed_directions[event]]
        if missing:
            directions_never_observed[event] = missing

    window_first = min((e["first_ts"] for e in all_entries if e["first_ts"]), default="")
    window_last = max((e["last_ts"] for e in all_entries if e["last_ts"]), default="")

    return {
        "claude_code_version": claude_code_version or "unknown",
        "capture_window": {"first_ts": window_first, "last_ts": window_last},
        "record_count": sum(e["count"] for e in all_entries),
        "records": census_records,
        "synthetic_records": synthetic_records,
        "origin_counts": origin_counts,
        "events_never_observed": never_observed,
        # WITHOUT THIS FLAG THE TWO KEYS BELOW LIE BY OMISSION. Both are derived from the
        # manifest, and an unreadable manifest yields an empty registered set, so both come
        # back EMPTY, which reads as "every hook was captured and every event observed": the
        # strongest possible coverage claim produced by a failure to read a file. The flag
        # makes "nothing to report" distinguishable from "could not tell", which is this
        # module's whole premise applied to its own inputs.
        "manifest_read": bool(manifest),
        "hooks_never_captured": hooks_never_captured,
        "directions_never_observed": directions_never_observed,
        "edit_replace_all": axes["edit_replace_all"],
        "write_target_extensions": axes["write_target_extensions"],
        "write_rows_without_file_path": axes["write_rows_without_file_path"],
    }
