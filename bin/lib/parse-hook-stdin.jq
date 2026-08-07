# jq equivalent of `parse-hook-stdin.py --shell`, the per-hook stdin parser.
#
# WHY: every hook spawns that python parser once just to read its envelope.
# Measured 2026-08-07, one Write fires 15 hooks and pays 13 of these at ~18ms each;
# this does the same work in ~3ms. Python remains the fallback (WRIT_NO_JQ=1, or jq
# absent), so absence changes speed and never behavior, the same contract
# `parsed_field` already ships under.
#
# THE PORTING RULE: this file mirrors python's semantics, including the ones I would
# not have chosen. It is a translation, not an improvement. Anywhere the python is
# arguably wrong, the fix belongs in BOTH parsers at once (see `tool_input: null`
# below) rather than in this one, or the fallback arm stops being a fallback.
#
# FIDELITY NOTES, each a real divergence measured while porting rather than a style
# preference. Every one is pinned by tests/test_hook_env_parity.py.
#
#  1. HOOK_SESSION_ID prefers agent_id over session_id (sub-agent isolation), which
#     mirrors detect_session_id. HOOK_SESSION_ID_RAW keeps the envelope's own value.
#  2. `//` in jq falls through on null AND false, but python's `.get(k, default)`
#     keeps an explicit false. `is_error` therefore uses an explicit has() test, or
#     a genuine `"tool_result_is_error": false` would read as the env fallback.
#  3. python's `or` chains fall through on the EMPTY STRING as well as null, while
#     jq's `//` keeps "" (an empty string is truthy in jq). `pyor` below models
#     python truthiness so file_path's three-way chain and content's fall-through to
#     new_source (NotebookEdit) land on the same branch on both paths.
#  4. tool_input may arrive as a JSON *string* and gets re-parsed; if it is empty or
#     unparseable, CLAUDE_TOOL_INPUT is consulted, exactly as the python does.
#  5. env fallback is keyed on KEY ABSENCE, not on falsiness. python spells `event`
#     as `.get("hook_event_name", $ENV)`, so an explicit `"hook_event_name": null`
#     yields null, NOT the env value. Measured: with HOOK_EVENT=PreToolUse in the
#     environment and an explicit null in the envelope, a `//` spelling here gave
#     HOOK_EVENT=PreToolUse while python gave the empty string. Hooks branch on
#     HOOK_EVENT, so that one changed behavior.
#  6. Fields are held RAW and stringified only at the emit points, because python
#     does the same: `_q` coerces None to "" for the scalar assignment while
#     `json.dumps` still writes `null` into HOOK_ENVELOPE. Coercing early made the
#     envelope disagree (`"agent_id": ""` against python's `"agent_id": null`).
#  7. Quoting style differs on purpose: shlex.quote leaves safe tokens bare while
#     @sh always wraps in single quotes. Both eval to identical values, which is the
#     contract the parity tests assert (per-field after eval, not raw bytes).
#
# NOT MODELLED, because a CC envelope cannot produce them: a numeric or boolean
# `content` / `file_path` / `command` (jq's tostring would print `true` where python
# prints `True`), and a numeric `tool_result_is_error` (0 is falsy in python, truthy
# in jq). These are string and boolean fields in CC's schema.
#
# INVOCATION: `jq -R -s -r -f parse-hook-stdin.jq`. The raw-slurp flags are part of
# the contract, not a preference. Without them jq does its own parsing, and on empty
# or truncated stdin it emits NOTHING while python emits a full set of empty
# assignments: measured, the only remaining divergence across the whole fixture set.
# Reading stdin as one string and parsing it inside the filter moves the failure into
# a `try`, so a malformed envelope produces the same empty assignments on both arms.
# `root` still accepts pre-parsed input so the filter is usable without -R -s.
#
# Input: the raw hook envelope on stdin. Output: shell assignments, one per line.

def as_object: if type == "object" then . else {} end;

# The whole-stdin string from -R -s, parsed here. python's json.loads sits inside a
# try/except that yields {}; this is the same shape. A raw document that parses to a
# non-object (a bare array, `42`, a JSON string) is normalized once and NOT re-parsed,
# matching python: json.loads('"{\"a\":1}"') is the string, not the object.
def root: (if type == "string" then (try fromjson catch {}) else . end) | as_object;

# python truthiness for the scalars that can appear here, so an `or` chain in the
# python lands on the same branch as this filter. jq's own `//` only tests for null.
def pyor(a; b): if (a == null or a == "" or a == false or a == 0) then b else a end;

# python's `.get(k, default)`: an explicitly-null value is a VALUE, not a miss.
def getor(obj; key; default): if (obj | has(key)) then obj[key] else default end;

def parsed_tool_input:
  (.tool_input // null) as $ti
  | (if ($ti | type) == "string" then
       (try ($ti | fromjson | as_object) catch {})
     else
       ($ti | as_object)
     end) as $obj
  | if ($obj | length) > 0 then $obj
    else (try (($ENV.CLAUDE_TOOL_INPUT // "") | fromjson | as_object) catch {})
    end;

# The scalar coercion python's _q performs: None becomes the empty string.
def q(v): (if v == null then "" else (v | tostring) end) | @sh;

# A root that is not a JSON object (a bare array, a number, a truncated document)
# becomes an empty one: `has()` raises on an array, which would abort the filter and
# hand the hook nothing at all. The python arm is normalized the same way, so both
# answer "no fields" rather than one crashing and one parsing.
root
| . as $env
| parsed_tool_input as $ti
| {
    session_id: getor($env; "session_id"; ""),
    agent_id: getor($env; "agent_id"; ""),
    agent_type: getor($env; "agent_type"; ""),
    event: getor($env; "hook_event_name"; ($ENV.HOOK_EVENT // "")),
    tool_name: getor($env; "tool_name"; ($ENV.HOOK_TOOL_NAME // "")),
    tool_input: $ti,
    tool_output: getor($env; "tool_output"; ($ENV.HOOK_TOOL_OUTPUT // null)),
    is_error: getor($env; "tool_result_is_error"; (($ENV.HOOK_TOOL_IS_ERROR // "") == "1")),
    file_path: pyor($ti.file_path; pyor($ti.path; pyor($ti.notebook_path; ""))),
    content: pyor($ti.content; getor($ti; "new_source"; "")),
    old_string: getor($ti; "old_string"; ""),
    new_string: getor($ti; "new_string"; ""),
    command: getor($ti; "command"; ""),
  } as $r
| pyor($r.agent_id; $r.session_id) as $sid
| "HOOK_SESSION_ID=" + q($sid),
  "HOOK_SESSION_ID_RAW=" + q($r.session_id),
  "HOOK_AGENT_ID=" + q($r.agent_id),
  "HOOK_AGENT_TYPE=" + q($r.agent_type),
  "HOOK_EVENT=" + q($r.event),
  "HOOK_TOOL_NAME=" + q($r.tool_name),
  "HOOK_FILE_PATH=" + q($r.file_path),
  "HOOK_COMMAND=" + q($r.command),
  "HOOK_IS_ERROR=" + (if $r.is_error then "1" else "0" end),
  "HOOK_ENVELOPE=" + ($r | tojson | @sh)
