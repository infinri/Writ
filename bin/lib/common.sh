#!/bin/bash
# Shared library for phaselock bin/ scripts and hooks.
# Source this file: source "$(dirname "$0")/lib/common.sh"

# ── Hook stdin parser path ──────────────────────────────────────────────────
# Absolute path to the stdin-envelope parser invoked by load_hook_env (below).
# THE single resolution of this library's own directory. Every helper path below
# derives from it: each `$(cd ... && pwd)` is a subshell fork paid on EVERY hook
# invocation (common.sh is sourced by all 37), and there were four.
_WRIT_LIB_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
_WRIT_SKILL_DIR="${_WRIT_LIB_DIR%/bin/lib}"
_PARSE_HOOK_STDIN_PY="$_WRIT_LIB_DIR/parse-hook-stdin.py"
_PARSE_HOOK_STDIN_JQ="$_WRIT_LIB_DIR/parse-hook-stdin.jq"

# ── Session-cache location (THE bash-side definition) ────────────────────────
# Mirrors writ/session/cache.py: WRIT_CACHE_DIR wins, else <skill>/var/session
# derived from this file's own location (bin/lib/common.sh -> bin/lib -> bin ->
# <skill>), the same three-step walk the package does from writ/session/cache.py.
#
# Do NOT reintroduce a tempdir fallback here. Commit 152e722 moved session state off
# /tmp because tmpfiles.d declares `D /tmp`, which EMPTIES it at boot. Three bash
# copies of that old default outlived the move: the auto-route classifier's mode read
# then always answered "no mode set" (it was looking in a directory that holds no
# session caches at all), and a hook-started daemon was born pointed at the same empty
# directory, which is how a gate decision came to be logged with "mode": null.
# tests/test_session_cache_dir_parity.py pins this against the package's value.
writ_session_cache_dir() {
    if [ -n "${WRIT_CACHE_DIR:-}" ]; then
        printf '%s' "$WRIT_CACHE_DIR"
        return 0
    fi
    printf '%s' "$_WRIT_SKILL_DIR/var/session"
}

# The session's mode read STRAIGHT from the cache file: stdlib only, no writ import and
# no daemon round-trip. The auto-route classifier needs this because a daemon `mode get`
# can spuriously return empty under load, and acting on a false empty would let the
# classifier fight an explicit choice. Prints the mode, or "" when absent/unset/unreadable.
writ_session_mode_direct() {
    python3 -c "
import json, os, sys
p = os.path.join(sys.argv[2], 'writ-session-' + sys.argv[1] + '.json')
try:
    print(json.load(open(p)).get('mode') or '')
except Exception:
    print('')
" "$1" "$(writ_session_cache_dir)" 2>/dev/null | tr -d '[:space:]'
}

# ── HTTP: curl-first, urllib fallback ────────────────────────────────────────
# curl is an OPTIONAL accelerator, never a prerequisite. These two wrappers run curl
# when it is present and fall back to `python3 bin/lib/writ_install.py http-*` (stdlib
# urllib) when it is not, so a curl-less machine still gets rule injection and gate
# approval instead of a silent "server unreachable". WRIT_NO_CURL=1 forces the fallback
# arm, mirroring the WRIT_NO_JQ seam on parsed_field/parsed_bool; the equivalence of the
# two arms is pinned by tests/test_no_tool_prereqs.py.
#
# Two semantics, because callers depend on the difference:
#   writ_http_get URL            = `curl -s`   -> body on stdout for ANY status (>= 400
#                                                 included), non-zero only if unreachable
#   writ_http_get URL --fail     = `curl -sf`  -> nothing on stdout and non-zero on >= 400
# Timeouts are per-call via the environment (curl's --connect-timeout / --max-time; the
# python arm gets the max as its single socket timeout):
#   WRIT_HTTP_CONNECT_TIMEOUT (default 0.5)   WRIT_HTTP_TIMEOUT (default 10)
# Usage: RESP=$(WRIT_HTTP_TIMEOUT=3 writ_http_post "$URL" "$BODY" 2>/dev/null) || true
_WRIT_INSTALL_PY="$_WRIT_LIB_DIR/writ_install.py"

writ_http_get() {
    local url="$1"
    local fail="" arg
    for arg in "$@"; do
        [ "$arg" = "--fail" ] && fail="--fail"
    done
    local ct="${WRIT_HTTP_CONNECT_TIMEOUT:-0.5}" mt="${WRIT_HTTP_TIMEOUT:-10}" rc=0
    if [ -z "${WRIT_NO_CURL:-}" ] && command -v curl >/dev/null 2>&1; then
        if [ -n "$fail" ]; then
            curl -sf --connect-timeout "$ct" --max-time "$mt" "$url" || rc=$?
        else
            curl -s --connect-timeout "$ct" --max-time "$mt" "$url" || rc=$?
        fi
        return $rc
    fi
    python3 "$_WRIT_INSTALL_PY" http-get "$url" ${fail:+--fail} --timeout "$mt" || rc=$?
    return $rc
}

writ_http_post() {
    local url="$1" body="${2:-}"
    local fail="" arg
    for arg in "$@"; do
        [ "$arg" = "--fail" ] && fail="--fail"
    done
    local ct="${WRIT_HTTP_CONNECT_TIMEOUT:-0.5}" mt="${WRIT_HTTP_TIMEOUT:-10}" rc=0
    if [ -z "${WRIT_NO_CURL:-}" ] && command -v curl >/dev/null 2>&1; then
        if [ -n "$fail" ]; then
            curl -sf --connect-timeout "$ct" --max-time "$mt" -X POST "$url" \
                -H "Content-Type: application/json" -d "$body" || rc=$?
        else
            curl -s --connect-timeout "$ct" --max-time "$mt" -X POST "$url" \
                -H "Content-Type: application/json" -d "$body" || rc=$?
        fi
        return $rc
    fi
    python3 "$_WRIT_INSTALL_PY" http-post "$url" "$body" ${fail:+--fail} --timeout "$mt" || rc=$?
    return $rc
}

# ── Debug gating ─────────────────────────────────────────────────────────────
# One shared gate for all opt-in debug sinks (the /tmp/*-debug.log files and the
# WRIT_HOOK_LOG stderr breadcrumbs). Debug is OFF by default; WRIT_DEBUG=1 turns
# every gated sink back on at once. Independent of WRIT_BLACKBOX (raw-payload
# capture), which stays on its own gate.
# Usage: _writ_debug_enabled && debug_log "$LOG" "msg"
_writ_debug_enabled() {
    [ "${WRIT_DEBUG:-}" = "1" ]
}

# Append a timestamped debug line to a log file, but only when WRIT_DEBUG=1.
# No-op (exit 0) when debug is off, so callers can call it unconditionally.
# Usage: debug_log "$WRIT_DEBUG_LOG" "stdin: ${STDIN_JSON:0:200}"
debug_log() {
    _writ_debug_enabled || return 0
    local log="${1:-/dev/null}"; shift
    printf '[%s] %s\n' "$(date '+%H:%M:%S')" "$*" >> "$log" 2>/dev/null || true
}

# Resolve the stderr breadcrumb sink for hook diagnostics. /dev/null when
# WRIT_DEBUG is unset (so recovery breadcrumbs never touch disk in production);
# ${WRIT_HOOK_LOG:-/tmp/writ-hooks.log} when WRIT_DEBUG=1. Print the path so a
# caller can redirect through it: `... 2>>"$(hook_log_sink)"`.
# Usage: SINK=$(hook_log_sink)
hook_log_sink() {
    if _writ_debug_enabled; then
        printf '%s\n' "${WRIT_HOOK_LOG:-/tmp/writ-hooks.log}"
    else
        printf '%s\n' "/dev/null"
    fi
}

# True (exit 0) if the captured Stop/SubagentStop payload has stop_hook_active=true.
# Claude Code sets this when it RE-invokes a Stop hook after a prior block; a Stop
# hook that keeps blocking while it is true loops until CC's block cap (9) then
# force-overrides. So a blocking Stop hook MUST allow the stop when this is true.
# Lightweight grep -- no python spawn (B2: keep hook spawns down).
# Usage: STDIN_JSON=$(cat); stop_hook_active "$STDIN_JSON" && exit 0
stop_hook_active() {
    printf '%s' "${1:-}" | grep -qE '"stop_hook_active"[[:space:]]*:[[:space:]]*true'
}

# Emit the envelope's shell assignments from stdin: jq when it can, python otherwise.
#
# jq-first for the same reason parsed_field is, just bigger: this is the ONE parse
# every hook performs, so it is the single most-executed process in the system.
# Measured 2026-08-07, one Write fires 15 hooks and pays 13 of these; the python
# interpreter costs ~18ms to start and jq ~3ms, so the arm choice is worth ~195ms of
# a 1,535ms write. Equivalence of the two arms is pinned by
# tests/test_hook_env_parity.py, which found and closed three real divergences.
#
# The arm is chosen BEFORE stdin is consumed, because stdin cannot be re-read. That
# is why there is no retry-on-bad-JSON: a malformed envelope now yields the same
# empty assignments from both arms (jq slurps raw and parses inside a `try`), so
# there is nothing a second attempt could recover. jq compiles its program before
# reading input, so the one fallthrough that does matter -- a missing or unparseable
# filter file, i.e. a partial install -- still reaches python with stdin intact.
_writ_parse_hook_stdin() {
    if [ -z "${WRIT_NO_JQ:-}" ] && [ -r "$_PARSE_HOOK_STDIN_JQ" ] \
       && command -v jq >/dev/null 2>&1; then
        jq -R -s -r -f "$_PARSE_HOOK_STDIN_JQ" 2>/dev/null && return 0
    fi
    python3 "$_PARSE_HOOK_STDIN_PY" --shell 2>/dev/null
}

# Single-spawn field extraction (POL-5b). Runs the parser once with --shell and
# evals its shell-quoted assignments, setting these globals:
#   HOOK_SESSION_ID HOOK_AGENT_ID HOOK_AGENT_TYPE HOOK_EVENT HOOK_TOOL_NAME
#   HOOK_FILE_PATH HOOK_COMMAND HOOK_IS_ERROR HOOK_ENVELOPE
# Replaces the parse_hook_stdin + parsed_field (2+ spawn) idiom: one parser spawn
# (jq, or python3 when jq is absent), then field access is a bash variable.
# HOOK_SESSION_ID gets detect_session_id's PPID/cwd fallback when the envelope
# carries no id.
# Usage: load_hook_env; echo "$HOOK_FILE_PATH"
load_hook_env() {
    HOOK_SESSION_ID="" HOOK_SESSION_ID_RAW="" HOOK_AGENT_ID="" HOOK_AGENT_TYPE="" HOOK_EVENT=""
    HOOK_TOOL_NAME="" HOOK_FILE_PATH="" HOOK_COMMAND="" HOOK_IS_ERROR="" HOOK_ENVELOPE=""
    # Black-box capture (gated): when ON, read the RAW stdin once, feed it to the parser,
    # and log the RAW envelope -- the true CC payload, not Writ's normalized form. When OFF,
    # the parser reads stdin directly exactly as before: zero added cost on the hot path.
    local _bb_raw=""
    if [ "${WRIT_BLACKBOX:-}" = "1" ] || [ -f "${HOME:-}/.claude/writ-blackbox.on" ]; then
        _bb_raw=$(cat)
        eval "$(printf '%s' "$_bb_raw" | _writ_parse_hook_stdin)"
    else
        eval "$(_writ_parse_hook_stdin)"
    fi
    if [ -z "${HOOK_SESSION_ID:-}" ]; then
        HOOK_SESSION_ID=$(ps -o ppid= -p $PPID 2>/dev/null | tr -d ' ')
    fi
    if [ -z "${HOOK_SESSION_ID:-}" ]; then
        HOOK_SESSION_ID=$(echo "${PWD}:${USER}" | md5sum | cut -c1-12)-$(date +%Y%m%d)
    fi
    # Log the raw envelope (the calling hook's basename labels it). Never affects the caller.
    if [ -n "${_bb_raw:-}" ]; then
        printf '%s' "$_bb_raw" | blackbox_log in "$(basename "${BASH_SOURCE[1]:-$0}" .sh)" "${HOOK_SESSION_ID:-}" || true
    fi
}

# Black-box capture: append the RAW Claude-Code <-> hook payloads to a JSONL so the
# actual contract can be inspected empirically (what CC sends a hook, and what the
# hook returns to Claude) instead of inferred. Reads the payload from stdin.
#   direction: "in"  = the envelope CC passed the hook (prompt, tool_input, agent_type, ...)
#              "out" = what the hook emits back to Claude (stdout / additionalContext)
# Opt-in via WRIT_BLACKBOX=1 -> when unset this is a no-op that still drains stdin, so
# wiring it into a pipe is zero-overhead and behavior-neutral in production. Never fails
# the caller. Log path: $WRIT_BLACKBOX_LOG (default ~/.claude/writ-blackbox.jsonl).
# Usage:  printf '%s' "$STDIN_JSON" | blackbox_log in  "$(basename "$0")" "$SESSION_ID"
#         printf '%s' "$OUTPUT"     | blackbox_log out "$(basename "$0")" "$SESSION_ID"
# Default cap on the capture log, 256 MiB. Named rather than inline so an operator
# who finds capture stopped can grep for what bounded it. Override:
# WRIT_BLACKBOX_MAX_BYTES.
WRIT_BLACKBOX_MAX_BYTES_DEFAULT=268435456

blackbox_log() {
    # Enabled by WRIT_BLACKBOX=1 OR the sentinel file ~/.claude/writ-blackbox.on (the
    # sentinel works for already-running CC sessions that can't get a new env var; remove
    # the file to disable). Off => no-op that still drains stdin.
    if [ "${WRIT_BLACKBOX:-}" != "1" ] && [ ! -f "${HOME:-}/.claude/writ-blackbox.on" ]; then
        cat >/dev/null 2>&1; return 0
    fi
    local direction="${1:-?}" hook="${2:-?}" session="${3:-}"
    local log="${WRIT_BLACKBOX_LOG:-$HOME/.claude/writ-blackbox.jsonl}"

    # SIZE CAP. Capture is a debug switch with no expiry: measured 2026-08-06, the
    # sentinel on this developer's machine was dated 19 June and the log had reached
    # 1.48 GB, still growing, costing ~31ms and 2 python spawns on EVERY hook. An
    # unbounded debug switch is indistinguishable from a leak, so past the cap it
    # stops capturing, and says so ONCE rather than going quiet, because a capture
    # that stopped silently is its own debugging trap.
    local _bb_cap="${WRIT_BLACKBOX_MAX_BYTES:-$WRIT_BLACKBOX_MAX_BYTES_DEFAULT}"
    # Validate the cap before comparing. `[ x -gt y ]` on a non-numeric or
    # arithmetic-overflowing value exits non-zero, which an `if` reads as false, so an
    # unvalidated typo in this one variable would silently STOP ENFORCING the cap:
    # fail-open on exactly the setting an operator is most likely to get wrong. 19
    # digits keeps it inside signed 64-bit.
    case "$_bb_cap" in
        ''|*[!0-9]*) _bb_cap="$WRIT_BLACKBOX_MAX_BYTES_DEFAULT" ;;
        ???????????????????*) _bb_cap="$WRIT_BLACKBOX_MAX_BYTES_DEFAULT" ;;
    esac
    # Existence is checked BEFORE the redirect: `wc -c < missing 2>/dev/null` cannot
    # suppress the message, because bash reports the failed redirect itself before wc
    # ever runs, which leaks a raw "No such file or directory" to the hook's stderr on
    # every first capture.
    local _bb_size=0
    if [ -f "$log" ]; then
        _bb_size=$(wc -c < "$log" 2>/dev/null | tr -d ' ') || _bb_size=0
        case "$_bb_size" in ''|*[!0-9]*) _bb_size=0 ;; esac
    fi
    local _bb_marker="${log}.capped"
    if [ "$_bb_size" -gt "$_bb_cap" ]; then
        cat >/dev/null 2>&1
        # Announce once. Emitting per invocation cost ~20ms per write (one
        # friction-append spawn each), making the announcement more expensive than
        # the capture it replaced. The marker is created FIRST and the row is emitted
        # only if that succeeded: otherwise a read-only directory would mean an
        # unmarkable, therefore repeating, announcement every single write.
        if [ ! -f "$_bb_marker" ] && : > "$_bb_marker" 2>/dev/null; then
            # The path is escaped, not interpolated raw. A log path containing a
            # quote would otherwise produce invalid JSON that the writer drops
            # silently, losing the path and size this row exists to report -- the
            # same allowlist-over-trust argument writ_action_push_body makes below.
            local _bb_log_esc="${log//\\/\\\\}"
            _bb_log_esc="${_bb_log_esc//\"/\\\"}"
            log_friction_event "$session" "" "blackbox_capture_disabled" \
                "{\"log\":\"$_bb_log_esc\",\"size_bytes\":$_bb_size,\"cap_bytes\":$_bb_cap,\"hook\":\"$hook\"}" \
                2>/dev/null || true
        fi
        return 0
    fi
    # Under the cap: clear a stale marker so a LATER crossing announces again. Without
    # this, deleting an over-cap log (the documented remediation) leaves the marker
    # behind, and the next time the log grows past the cap it stops capturing in
    # silence, which is the failure mode this whole feature exists to prevent.
    [ -f "$_bb_marker" ] && rm -f "$_bb_marker" 2>/dev/null || true
    # python encodes one JSON record to stdout (handles payload escaping); bash appends
    # it. Encoding-only keeps the file write out of python (no direct file open here).
    local rec
    rec=$(WRIT_BB_DIR="$direction" WRIT_BB_HOOK="$hook" WRIT_BB_SID="$session" python3 -c '
import os, sys, json, datetime
try:
    print(json.dumps({"ts": datetime.datetime.now(datetime.timezone.utc).isoformat(),
                      "hook": os.environ.get("WRIT_BB_HOOK", "?"),
                      "direction": os.environ.get("WRIT_BB_DIR", "?"),
                      "session": os.environ.get("WRIT_BB_SID", ""),
                      "pid": os.getpid(),
                      "payload": sys.stdin.read()}))
except Exception:
    pass
' 2>/dev/null) || true
    [ -n "$rec" ] && printf '%s\n' "$rec" >> "$log" 2>/dev/null || true
}

# Convenience: extract a single SCALAR field (string/number) from parsed JSON.
# jq-first (~1-2ms) with a python3 fallback (~10ms cold start) when jq is absent or
# WRIT_NO_JQ is set (B2: cut per-hook spawn latency -- jq replaces the python3
# cold-start). Missing/null -> DEFAULT (3rd arg, default ""). For booleans use
# parsed_bool (jq prints true/false vs python True/False -- different strings).
# Usage: FILE=$(parsed_field "$PARSED" "file_path")            # default ""
#        BUDGET=$(parsed_field "$CACHE" "remaining_budget" "8000")
parsed_field() {
    local json="$1" field="$2" default="${3:-}"
    if [ -z "${WRIT_NO_JQ:-}" ] && command -v jq >/dev/null 2>&1; then
        printf '%s' "$json" | jq -r --arg k "$field" --arg d "$default" \
            'if (.[$k]) == null then $d else .[$k] end' 2>/dev/null || printf '%s' "$default"
    else
        printf '%s' "$json" | python3 -c "import sys, json
f, d = sys.argv[1], sys.argv[2]
try: _v = json.load(sys.stdin).get(f)
except Exception: _v = None
print(_v if _v is not None else d)" "$field" "$default" 2>/dev/null || printf '%s' "$default"
    fi
}

# Convenience: true (exit 0) iff a JSON field is boolean true (missing/null/false
# -> false). jq-first with a python3 fallback (WRIT_NO_JQ forces fallback).
# Usage: if parsed_bool "$PARSED" "is_error"; then ...
parsed_bool() {
    local json="$1" field="$2"
    if [ -z "${WRIT_NO_JQ:-}" ] && command -v jq >/dev/null 2>&1; then
        printf '%s' "$json" | jq -e --arg k "$field" '(.[$k]) == true' >/dev/null 2>&1
    else
        local val
        val=$(printf '%s' "$json" | python3 -c 'import sys,json; print(json.load(sys.stdin).get(sys.argv[1], False))' "$field" 2>/dev/null)
        [ "$val" = "True" ]
    fi
}

# Reshape JSON from stdin with one process instead of a python interpreter start.
#   json_transform <jq_filter> <python_expr>       # reads stdin, prints one value
# The python expression receives the parsed object as `d`; it is the fallback arm and
# runs only when jq is absent or WRIT_NO_JQ is set, the same seam parsed_field uses.
#
# WHY BOTH ARMS EXIST: 24 inline `python3 -c` snippets fired per file write, each
# paying ~15ms of interpreter startup to do JSON reshaping jq does in ~3ms (measured
# 2026-08-07). This concentrates that conversion in one place so the equivalence
# argument is made once, under test, instead of at 24 call sites.
#
# THE OUTPUT IS CANONICALIZED, because the two tools disagree on three shapes and
# every disagreement would be a silent behavior change at some call site:
#   booleans   jq prints true/false, python prints True/False -> JSON spelling wins
#   null       jq prints the four characters "null", python raises -> BOTH print
#              nothing, so a missing field can never be mistaken for the text "null"
#   containers `jq -r` pretty-prints over several lines, python's json.dumps does not
#              -> compact on both (-c), so a caller reading one line still gets one
# Integers are safe as-is: jq 1.7 round-trips 9007199254740993 exactly.
#
# CONTRACT: one value out. A jq filter that emits two (`.a, .b`) has no python
# equivalent here, and callers that need two fields should call twice or pass a filter
# that builds one object.
#
# On the `eval` in the fallback arm (SEC-INJ-CMD-002): the expression is a literal
# written in the hook's own source, and the untrusted DATA arrives on stdin, never in
# the expression. Builtins are replaced with a small allowlist rather than left
# exposed, so the arm cannot reach the filesystem or the process table even if a
# future call site is careless about what it passes.
json_transform() {
    local filter="$1" pyexpr="$2"
    if [ -z "${WRIT_NO_JQ:-}" ] && command -v jq >/dev/null 2>&1; then
        jq -c -r "( $filter ) | if . == null then empty else . end" 2>/dev/null
    else
        python3 -c '
import sys, json
try:
    d = json.load(sys.stdin)
except Exception:
    sys.exit(0)
_allowed = {"len": len, "sorted": sorted, "str": str, "int": int, "float": float,
            "bool": bool, "list": list, "dict": dict, "min": min, "max": max,
            "sum": sum, "any": any, "all": all, "abs": abs, "round": round}
try:
    v = eval(sys.argv[1], {"__builtins__": _allowed}, {"d": d, "json": json})
except Exception:
    sys.exit(0)
if v is None:
    sys.exit(0)
if v is True:
    print("true")
elif v is False:
    print("false")
elif isinstance(v, (dict, list)):
    print(json.dumps(v, separators=(",", ":")))
else:
    print(v)
' "$pyexpr" 2>/dev/null
    fi
}

# Phase 2 feature flag check was removed 2026-04-21 in favor of
# settings.json hook registration + mode-scope checks. Hooks should
# not reference any feature-flag function.

# Phase 2 mode-scope check. Phase 1 Section 0.4 decision 1: methodology
# enforcement hooks fire only in Work mode. Non-work modes skip.
# Usage: if ! is_work_mode "$SESSION_ID"; then exit 0; fi
is_work_mode() {
    # A5: use the _writ_session curl fast-path (~3ms vs ~10ms python cold-start)
    # instead of spawning python directly -- 7 enforcement hooks call this per
    # invocation. The daemon's /mode route reads the cache file FRESH (no stale
    # in-memory view), and _writ_session falls back to the file-direct python
    # helper when the daemon is down, so the result is unchanged.
    local sid="$1"
    local mode
    mode=$(_writ_session "mode get" "$sid" 2>/dev/null | tr -d '[:space:]')
    [ "$mode" = "work" ]
}

# ── Project root detection ────────────────────────────────────────────────────
# Walks up from a given path to find the project root by marker files.
# Usage: PROJECT_ROOT=$(detect_project_root "/path/to/some/file.php")
# E-PROJROOT-BUG fix: check markers at `path` BEFORE walking up, so a caller that
# passes the project root directory itself (e.g. `$(pwd)` from the repo root --
# check-gates.sh / writ-session-end.sh / inject-tier-workflow.sh) resolves it instead
# of returning "" and silently skipping gate checks. Harmless for file-path callers:
# a file has no marker inside its own join, so the first iteration falls through to
# dirname exactly as before.
detect_project_root() {
  local start_path="$1"
  # Pass start_path via argv (never spliced into the python source) so a path
  # containing an apostrophe/space/glob cannot break the source string and
  # abort the caller under `set -euo pipefail`. Double-quote the bash arg so a
  # path with spaces stays a single argv.
  python3 -c '
import os, sys
markers = ["composer.json","package.json","Cargo.toml","go.mod","pyproject.toml",".git"]
path = os.path.abspath(sys.argv[1])
while path != "/":
    if any(os.path.exists(os.path.join(path, m)) for m in markers):
        print(path); sys.exit(0)
    path = os.path.dirname(path)
print("")
' "$start_path" 2>/dev/null
}

# ── Session ID detection ────────────────────────────────────────────────────
# Extracts session ID from parsed hook envelope, falling back to PID.
# Usage: SESSION_ID=$(detect_session_id "$PARSED")
#   where PARSED is the output of parse_hook_stdin.
#   If called without args, falls back to PID detection (less reliable).
detect_session_id() {
  local parsed="${1:-}"
  local sid=""
  # Prefer agent_id for sub-agent isolation (each worker gets its own session cache)
  if [ -n "$parsed" ]; then
    sid=$(echo "$parsed" | python3 -c "
import sys,json
d=json.load(sys.stdin)
aid=d.get('agent_id')
sid=d.get('session_id')
aid = str(aid).strip() if aid is not None else ''
sid = str(sid).strip() if sid is not None else ''
print(aid or sid)
" 2>/dev/null)
  fi
  # Fallback: PID-based detection
  if [ -z "$sid" ]; then
    sid=$(ps -o ppid= -p $PPID 2>/dev/null | tr -d ' ')
  fi
  if [ -z "$sid" ]; then
    sid=$(echo "${PWD}:${USER}" | md5sum | cut -c1-12)-$(date +%Y%m%d)
  fi
  echo "$sid"
}

# ── JSON output helpers ──────────────────────────────────────────────────────
# Produces a JSON array from individual JSON objects (one per line on stdin).
# Usage: echo "$FINDINGS" | json_array
json_array() {
  python3 -c "
import json, sys
items = []
for line in sys.stdin:
    line = line.strip()
    if not line:
        continue
    try:
        items.append(json.loads(line))
    except json.JSONDecodeError:
        pass
print(json.dumps(items, indent=2, ensure_ascii=False))
" 2>/dev/null
}

# Emit a PreToolUse "deny" decision (Claude Code hookSpecificOutput contract)
# carrying the given reason. Single source for the deny envelope shared by the
# validate-design-doc / validate-test-file / worktree-safety PreToolUse gates.
# Usage: [ -n "$DENY" ] && emit_deny "$DENY"
emit_deny() {
  WRIT_DENY_REASON="$1" python3 <<'PY'
import json, os
print(json.dumps({
    'hookSpecificOutput': {
        'hookEventName': 'PreToolUse',
        'permissionDecision': 'deny',
        'permissionDecisionReason': os.environ.get('WRIT_DENY_REASON', '')
    }
}))
PY
}

# Emit a PreToolUse "ask" decision (Claude Code hookSpecificOutput contract): the
# tool is neither allowed nor denied, the USER confirms it. Single source for the ask
# envelope, the twin of emit_deny above. The reason rides an env var into python, which
# does the JSON encoding, so newlines and quotes inside a reason (the egress guard lists
# one destination per line) cannot corrupt or forge the envelope (SEC-INJ-LOG-001).
# Usage: [ -n "$ASK" ] && emit_ask "$ASK"
emit_ask() {
  WRIT_ASK_REASON="$1" python3 <<'PY'
import json, os
print(json.dumps({
    'hookSpecificOutput': {
        'hookEventName': 'PreToolUse',
        'permissionDecision': 'ask',
        'permissionDecisionReason': os.environ.get('WRIT_ASK_REASON', '')
    }
}))
PY
}

# Extract the rule objects (the fields used for violation pattern matching) from
# a /query JSON response on stdin, as a JSON array; '[]' on any error. Single
# source for the --add-rule-objects payload built by the RAG hooks
# (posttool-rag / rag-inject / read-rag).
# Usage: RULE_OBJECTS=$(echo "$RESPONSE" | extract_rule_objects)
extract_rule_objects() {
  python3 -c "
import sys, json
try:
    resp = json.load(sys.stdin)
    rules = resp.get('rules', [])
    objects = []
    for r in rules:
        objects.append({
            'rule_id': r.get('rule_id', ''),
            'trigger': r.get('trigger', ''),
            'statement': r.get('statement', ''),
            'violation': r.get('violation', ''),
            'pass_example': r.get('pass_example', ''),
            'enforcement': r.get('enforcement', ''),
            'domain': r.get('domain', ''),
            'severity': r.get('severity', ''),
        })
    print(json.dumps(objects))
except Exception:
    print('[]')
" 2>/dev/null || echo '[]'
}

# ── Tool location helpers ────────────────────────────────────────────────────
# Finds a tool binary: checks project vendor path first, then global PATH.
# Usage: PHPSTAN=$(find_tool "$PROJECT_ROOT" "vendor/bin/phpstan" "phpstan")
find_tool() {
  local project_root="$1" vendor_path="$2" global_name="$3"
  if [ -n "$project_root" ] && [ -f "$project_root/$vendor_path" ]; then
    echo "$project_root/$vendor_path"
  elif command -v "$global_name" &>/dev/null; then
    echo "$global_name"
  fi
}

# ── File extension helpers ───────────────────────────────────────────────────
# Returns the language category for a file extension.
# Usage: LANG=$(detect_language "src/Foo.php")
detect_language() {
  local file="$1"
  case "$file" in
    *.php)               echo "php" ;;
    *.xml)               echo "xml" ;;
    *.js|*.jsx)          echo "javascript" ;;
    *.ts|*.tsx)          echo "typescript" ;;
    *.py)                echo "python" ;;
    *.rs)                echo "rust" ;;
    *.go)                echo "go" ;;
    *.graphqls|*.graphql) echo "graphql" ;;
    *)                   echo "unknown" ;;
  esac
}

# ── Friction event logging ──────────────────────────────────────────────────
# Appends a JSON event to the typed stream its event name maps to. Fire-and-forget.
# (Historically this wrote the single per-project friction log; since the P1 router
# it routes through friction-append.py -> writ.shared.logging.emit, which classifies
# by event via STREAM_MAP.)
# Usage: log_friction_event "$SESSION_ID" "$MODE" "event_name" '{"key":"val"}'
# Extra fields arg is optional JSON object to merge.
# Single env-aware writer (Phase 1.2): all friction writes route through
# bin/lib/friction-append.py so WRIT_FRICTION_LOG redirects every writer at once
# (and the marker-walk fallback lives in exactly one place).
_FRICTION_APPEND="$_WRIT_LIB_DIR/friction-append.py"

log_friction_event() {
  # `"${4:-"{}"}"` is required: `${4:-{}}` parses the second `}` as the
  # closing brace of the parameter expansion, leaving a stray `}` appended
  # to the value. Quoting the default makes `{}` literal.
  local session_id="$1" mode="$2" event="$3" extra="${4:-"{}"}"
  python3 "$_FRICTION_APPEND" "$session_id" "$mode" "$event" "$extra" 2>/dev/null || true
}

# ── Hook timing ─────────────────────────────────────────────────────────────
# Records start time. Call at the beginning of a hook.
# Usage: HOOK_START_NS=$(hook_timer_start)
hook_timer_start() {
  date +%s%N 2>/dev/null || python3 -c "import time; print(int(time.time()*1e9))"
}

# Logs hook_execution event with duration. Call before exit.
# Bypasses log_friction_event to avoid shell quoting issues with JSON.
# Usage: hook_timer_end "$HOOK_START_NS" "hook_name" "$SESSION_ID" "$MODE"
# `exit_code` (5th arg) is OPTIONAL so the pre-existing 4-arg callers keep working;
# when given it is emitted, which is what makes "which hooks are quietly failing"
# answerable. hook_name is hook-controlled (never tool input), so interpolating it
# into the JSON is safe here.
hook_timer_end() {
  local start_ns="$1" hook_name="$2" session_id="$3" mode="$4" exit_code="${5:-}"
  local now_ns dur_ms extra
  now_ns=$(date +%s%N 2>/dev/null || echo 0)
  if [ "${start_ns:-0}" -gt 0 ] 2>/dev/null && [ "$now_ns" -gt 0 ] 2>/dev/null; then
    dur_ms=$(( (now_ns - start_ns) / 1000000 ))
  else
    dur_ms=0
  fi
  [ "$dur_ms" -lt 0 ] 2>/dev/null && dur_ms=0
  extra="{\"hook_name\":\"$hook_name\",\"duration_ms\":$dur_ms"
  case "$exit_code" in
    ''|*[!0-9]*) : ;;                                  # absent or non-numeric: omit
    *) extra="$extra,\"exit_code\":$exit_code" ;;
  esac
  extra="$extra}"
  python3 "$_FRICTION_APPEND" "$session_id" "$mode" "hook_execution" \
    "$extra" 2>/dev/null || true
}

# ── Hook instrumentation (one trap, every exit path) ────────────────────────
# Installs an EXIT trap that records the run REGARDLESS of how the hook leaves:
# an early `exit 0`, a gate's `exit 2`, a `set -e` abort, or a command-not-found
# crash. Editing each `exit` instead would be 96 edits across the 18 hooks and
# would still miss the abort/crash paths -- the ones that vanish silently today.
#
# Behavior-neutral by construction: `$?` is captured FIRST and re-exited with, so
# a gate's exit code (Claude Code reads 2 as deny) is preserved exactly, and every
# write is stderr-redirected + `|| true` so a logging fault can never change a
# hook's outcome.
#
# SESSION_ID / CURRENT_MODE are read INSIDE the trap (late binding): most hooks
# resolve them after this call, so reading them at install time would record empty
# values.
# Usage: hook_instrument "writ-bash-write-gate"   (call right after sourcing common.sh)
hook_instrument() {
  _WRIT_HOOK_NAME="${1:-$(basename "${BASH_SOURCE[1]:-$0}" .sh)}"
  _WRIT_HOOK_START_NS=$(hook_timer_start)
  # Event buffer: log_gate_decision appends here instead of spawning its own
  # python, so a hook that both decides and times out pays ONE spawn at exit
  # rather than two. Measured: a friction-append spawn is ~26ms, and 11 of the
  # instrumented hooks emit both events. mktemp failure leaves the buffer empty,
  # which falls back to the direct per-event spawn.
  _WRIT_EVENT_BUF="$(mktemp 2>/dev/null || echo "")"
  trap '_writ_hook_exit_trap' EXIT
}

_writ_hook_exit_trap() {
  local rc=$?
  local start_ns="${_WRIT_HOOK_START_NS:-0}" now_ns dur_ms
  now_ns=$(date +%s%N 2>/dev/null || echo 0)
  if [ "${start_ns:-0}" -gt 0 ] 2>/dev/null && [ "$now_ns" -gt 0 ] 2>/dev/null; then
    dur_ms=$(( (now_ns - start_ns) / 1000000 ))
  else
    dur_ms=0
  fi
  [ "$dur_ms" -lt 0 ] 2>/dev/null && dur_ms=0

  # One spawn for every event this hook produced: the buffered gate decisions
  # (if any) plus this hook_execution row. The buffer holds RAW field values
  # separated by ASCII unit/record separators, so python does all JSON encoding
  # and no bash-side quoting can corrupt a record (SEC-INJ-LOG-001).
  WRIT_EV_BUF="${_WRIT_EVENT_BUF:-}" \
  WRIT_EV_HOOK="${_WRIT_HOOK_NAME:-unknown}" \
  WRIT_EV_DUR="$dur_ms" \
  WRIT_EV_RC="$rc" \
  WRIT_EV_SESSION="${SESSION_ID:-${HOOK_SESSION_ID:-}}" \
  WRIT_EV_MODE="${CURRENT_MODE:-${MODE:-}}" \
  WRIT_EV_SKILL="$_WRIT_SKILL_DIR" \
  python3 -c '
import os, sys
sys.path.insert(0, os.environ["WRIT_EV_SKILL"])
from writ.shared.logging import emit

session = os.environ.get("WRIT_EV_SESSION", "")
mode = os.environ.get("WRIT_EV_MODE") or None

buf = os.environ.get("WRIT_EV_BUF", "")
if buf and os.path.exists(buf):
    try:
        raw = open(buf).read()
    except OSError:
        raw = ""
    for rec in raw.split("\x1e"):
        parts = rec.split("\x1f")
        if len(parts) < 5 or parts[0] != "gate_decision":
            continue
        emit(None, "gate_decision", session, mode, gate=parts[1],
             decision=parts[2], reason=parts[3], target=parts[4])

emit(None, "hook_execution", session, mode,
     hook_name=os.environ.get("WRIT_EV_HOOK", "unknown"),
     duration_ms=int(os.environ.get("WRIT_EV_DUR") or 0),
     exit_code=int(os.environ.get("WRIT_EV_RC") or 0))
' 2>/dev/null || true
  [ -n "${_WRIT_EVENT_BUF:-}" ] && rm -f "$_WRIT_EVENT_BUF" 2>/dev/null || true
  exit "$rc"
}

# Records a gate's allow/deny decision. Emitted on BOTH branches on purpose: a
# gate ALLOW is already silent by design (only a deny sends JSON back to Claude
# Code), so deny-only logging leaves "was this gate even active" unanswerable.
# `gate_decision` is mapped to the audit stream (governance decision, 365-day
# retention), not metrics.
#
# reason/target carry tool input, so they are passed through the environment into
# python and serialized with json.dumps rather than interpolated into a JSON
# string -- quotes, backslashes, and newlines in a file path or denial reason
# cannot forge a second record (SEC-INJ-LOG-001).
# Usage: log_gate_decision "phase-a" "deny" "no plan approved" "src/foo.py"
log_gate_decision() {
  # Fast path: append RAW field values to the event buffer and let the exit trap
  # emit everything in ONE python spawn. printf writes the values verbatim with
  # ASCII unit (\x1f) / record (\x1e) separators -- control characters that cannot
  # occur in a bash variable -- so python does all JSON encoding and no bash-side
  # quoting can corrupt or forge a record.
  if [ -n "${_WRIT_EVENT_BUF:-}" ] && [ -f "${_WRIT_EVENT_BUF}" ]; then
    printf 'gate_decision\037%s\037%s\037%s\037%s\036' \
      "${1:-}" "${2:-}" "${3:-}" "${4:-}" >> "$_WRIT_EVENT_BUF" 2>/dev/null || true
    return 0
  fi
  # Fallback for a hook that calls this without hook_instrument (no buffer):
  # emit directly so the decision is still recorded.
  WRIT_GD_GATE="${1:-}" \
  WRIT_GD_DECISION="${2:-}" \
  WRIT_GD_REASON="${3:-}" \
  WRIT_GD_TARGET="${4:-}" \
  WRIT_GD_SESSION="${SESSION_ID:-${HOOK_SESSION_ID:-}}" \
  WRIT_GD_MODE="${CURRENT_MODE:-${MODE:-}}" \
  python3 -c '
import json, os
print(json.dumps({
    "session": os.environ.get("WRIT_GD_SESSION", ""),
    "mode": os.environ.get("WRIT_GD_MODE") or None,
    "event": "gate_decision",
    "gate": os.environ.get("WRIT_GD_GATE", ""),
    "decision": os.environ.get("WRIT_GD_DECISION", ""),
    "reason": os.environ.get("WRIT_GD_REASON", ""),
    "target": os.environ.get("WRIT_GD_TARGET", ""),
}))
' 2>/dev/null | python3 "$_FRICTION_APPEND" --stdin-json 2>/dev/null || true
}

# ── Writ session daemon helper ───────────────────────────────────────────────
# Tries curl to the daemon first (connect-timeout 0.1s), falls back to
# subprocess when the daemon is unreachable.
#
# Usage: _writ_session <subcommand> [args...]
#   e.g., _writ_session "should-skip" "$SESSION_ID"
#         _writ_session "mode get" "$SESSION_ID"
#         _writ_session "read" "$SESSION_ID"
#
# The function locates SESSION_HELPER from the calling script's WRIT_DIR
# or falls back to the SKILL_DIR variable.

WRIT_SESSION_PORT="${WRIT_PORT:-8765}"
WRIT_SESSION_HOST="${WRIT_HOST:-localhost}"
WRIT_SESSION_BASE="http://${WRIT_SESSION_HOST}:${WRIT_SESSION_PORT}"

_writ_session() {
    local subcmd="$1"
    shift
    local session_id="${1:-}"

    # Determine the session helper path
    local helper="${SESSION_HELPER:-${SKILL_DIR:-${WRIT_DIR:-}}/bin/lib/writ-session.py}"

    # Map subcommand to HTTP endpoint and method
    local url="" method="GET" body=""
    case "$subcmd" in
        "read")
            url="${WRIT_SESSION_BASE}/session/${session_id}"
            ;;
        "should-skip")
            # Special: exit code matters (0=skip, 1=don't skip)
            local skip_result=""
            skip_result=$(curl -sf --connect-timeout 0.1 --max-time 0.5 \
                "${WRIT_SESSION_BASE}/session/${session_id}/should-skip" 2>/dev/null) || true
            if [ -n "$skip_result" ]; then
                # Desync guard (mirrors the mode-get guard below): known=false means
                # the daemon answered but has no cache for this session (divergent
                # cache dir or a stale daemon) -- its boolean is a default, not an
                # answer. Fall through to the subprocess, which reads the cache we
                # actually write. A pre-`known` daemon also lands here (correct,
                # just slower) until it is restarted.
                if parsed_bool "$skip_result" "known"; then
                    # jq-first parse (B2: ~1-2ms vs ~10ms python cold-start per call).
                    if parsed_bool "$skip_result" "should_skip"; then
                        return 0
                    else
                        return 1
                    fi
                fi
            fi
            # Fallback to subprocess
            python3 "$helper" should-skip "$session_id"
            return $?
            ;;
        "mode get")
            # Special: hooks expect plain mode string, not JSON
            local mode_result=""
            mode_result=$(curl -sf --connect-timeout 0.1 --max-time 0.5 \
                "${WRIT_SESSION_BASE}/session/${session_id}/mode" 2>/dev/null) || true
            if [ -n "$mode_result" ]; then
                # jq-first parse (B2: ~1-2ms vs ~10ms python cold-start per call).
                local _mode_val
                _mode_val=$(parsed_field "$mode_result" "mode")
                # An EMPTY mode means the daemon answered but does not know this
                # session -- typically because its cache dir differs from ours (the
                # server-desync state, or a stale daemon on this port). Returning ""
                # here silently disables every mode-gated hook; fall through to the
                # local subprocess, which reads the cache we actually write.
                if [ -n "$_mode_val" ]; then
                    printf '%s\n' "$_mode_val"
                    return 0
                fi
            fi
            # Fallback to subprocess
            python3 "$helper" mode get "$session_id"
            return $?
            ;;
        "mode set")
            local mode_val="${2:-}"
            local orch_flag="${3:-}"
            url="${WRIT_SESSION_BASE}/session/${session_id}/mode"
            method="POST"
            if [ "$orch_flag" = "--orchestrator" ]; then
                body="{\"mode\":\"${mode_val}\",\"orchestrator\":true}"
            else
                body="{\"mode\":\"${mode_val}\"}"
            fi
            ;;
        "can-write")
            # C1 (audit): read the tool envelope ({"tool_input": {...}}) from stdin and
            # forward it as the POST body. The arm hardcoded body="{}", so the piped
            # envelope was discarded -> the server saw an empty file_path and returned
            # can_write=True: a silent write-gate bypass on the daemon-degraded fallback
            # (sibling of the 132eb04 empty-body bug). Guard: only default to "{}" when
            # nothing is piped (never block on a TTY).
            local cw_body=""
            if [ ! -t 0 ]; then
                cw_body=$(cat)
            fi
            [ -z "$cw_body" ] && cw_body="{}"
            # The server can-write route reads skill_dir from the body; fold the
            # --skill-dir arg in so skill-dir exemptions resolve as on the primary
            # /pre-write-check path.
            local cw_skill_dir=""
            [ "${2:-}" = "--skill-dir" ] && cw_skill_dir="${3:-}"
            local cw_post_body=""
            cw_post_body=$(WRIT_SD="$cw_skill_dir" python3 -c "
import json, os, sys
try:
    d = json.loads(sys.argv[1])
except (ValueError, json.JSONDecodeError):
    d = {}
if not isinstance(d, dict):
    d = {}
d.setdefault('skill_dir', os.environ.get('WRIT_SD', ''))
print(json.dumps(d))
" "$cw_body" 2>/dev/null) || cw_post_body="$cw_body"
            local cw_result=""
            cw_result=$(curl -sf --connect-timeout 0.1 --max-time 0.5 \
                -X POST "${WRIT_SESSION_BASE}/session/${session_id}/can-write" \
                -H "Content-Type: application/json" -d "$cw_post_body" 2>/dev/null) || true
            if [ -n "$cw_result" ]; then
                # Normalize the server's {"can_write":bool,"reason":...} into the
                # {"decision":"allow|deny","reason":...} shape the fallback consumer
                # (writ-pre-write-dispatch) parses -- the two can-write backends differ.
                printf '%s' "$cw_result" | python3 -c "
import json, sys
try:
    r = json.load(sys.stdin)
except Exception:
    print(json.dumps({'decision': 'allow', 'reason': None})); raise SystemExit
if r.get('can_write', True):
    print(json.dumps({'decision': 'allow', 'reason': r.get('reason')}))
else:
    print(json.dumps({'decision': 'deny', 'reason': r.get('reason')}))
"
                return 0
            fi
            # Server unreachable: the subprocess CLI reads the SAME envelope from stdin
            # and already emits the {"decision":...} shape.
            if [ -n "$cw_skill_dir" ]; then
                printf '%s' "$cw_body" | python3 "$helper" can-write "$session_id" --skill-dir "$cw_skill_dir"
            else
                printf '%s' "$cw_body" | python3 "$helper" can-write "$session_id"
            fi
            return $?
            ;;
        "advance-phase")
            url="${WRIT_SESSION_BASE}/session/${session_id}/advance-phase"
            method="POST"
            body="{}"
            ;;
        "current-phase")
            url="${WRIT_SESSION_BASE}/session/${session_id}/current-phase"
            ;;
        "format")
            # Stateless: read /query response JSON from stdin, POST it to
            # /session/format, render the {"text": ..., "meta": ...} response
            # back into the legacy stdout shape (text body + WRIT_META: line)
            # so existing hook consumers (writ-posttool-rag.sh) are unchanged.
            # Falls back to the subprocess CLI when the server is unreachable.
            local stdin_data
            stdin_data=$(cat)
            local fmt_body fmt_result
            fmt_body=$(python3 -c "
import json, sys
try:
    data = json.loads(sys.argv[1])
except (ValueError, json.JSONDecodeError):
    data = {}
print(json.dumps({'query_response': data}))
" "$stdin_data" 2>/dev/null)
            if [ -n "$fmt_body" ]; then
                fmt_result=$(curl -sf --connect-timeout 0.1 --max-time 0.5 \
                    -X POST "${WRIT_SESSION_BASE}/session/format" \
                    -H "Content-Type: application/json" \
                    -d "$fmt_body" 2>/dev/null) || true
                if [ -n "$fmt_result" ]; then
                    python3 -c "
import json, sys
try:
    body = json.loads(sys.argv[1])
except (ValueError, json.JSONDecodeError):
    sys.exit(1)
text = body.get('text', '')
meta = body.get('meta', {}) or {}
if text:
    sys.stdout.write(text)
    sys.stdout.write('\n')
sys.stdout.write('WRIT_META:' + json.dumps({
    'rule_ids': meta.get('rule_ids', []),
    'cost': meta.get('tokens', 0),
}) + '\n')
" "$fmt_result"
                    return 0
                fi
            fi
            # Fallback: subprocess (original behavior)
            echo "$stdin_data" | python3 "$helper" format "$@"
            return $?
            ;;
        "coverage")
            url="${WRIT_SESSION_BASE}/session/${session_id}/coverage"
            ;;
        "check-escalation")
            url="${WRIT_SESSION_BASE}/session/${session_id}/check-escalation"
            ;;
        "auto-feedback")
            url="${WRIT_SESSION_BASE}/session/${session_id}/auto-feedback"
            method="POST"
            body="{\"feedback\":\"\"}"
            ;;
        "clear-pending-violations")
            url="${WRIT_SESSION_BASE}/session/${session_id}/clear-pending-violations"
            method="POST"
            body="{}"
            ;;
        "add-pending-violation")
            # Complex args; fall through to subprocess
            python3 "$helper" add-pending-violation "$@"
            return $?
            ;;
        "invalidate-gate")
            # Complex args; fall through to subprocess
            python3 "$helper" invalidate-gate "$@"
            return $?
            ;;
        "pending-violations")
            url="${WRIT_SESSION_BASE}/session/${session_id}/pending-violations"
            ;;
        "update")
            # Complex args; fall through to subprocess
            python3 "$helper" update "$@"
            return $?
            ;;
        "pre-write-check")
            # Combined gate + final-gate + RAG check. Expects JSON body via $2.
            # `"${2:-"{}"}"` is required: the unquoted `${2:-{}}` form appends a
            # stray `}` when $2 is set (bash closes the expansion at the first `}`),
            # producing malformed JSON that the server rejects -- see the same
            # gotcha documented on log_friction_event above.
            local check_body="${2:-"{}"}"
            local pwc_result=""
            pwc_result=$(curl -sf --connect-timeout 0.2 --max-time 1 \
                -X POST "${WRIT_SESSION_BASE}/pre-write-check" \
                -H "Content-Type: application/json" \
                -d "$check_body" 2>/dev/null) || true
            if [ -n "$pwc_result" ]; then
                echo "$pwc_result"
                return 0
            fi
            # Fallback: run individual checks when server is unreachable
            local fallback_result=""
            fallback_result=$(echo "$check_body" | python3 -c "
import sys, json
body = json.load(sys.stdin)
sid = body.get('session_id', '')
print(sid)
" 2>/dev/null)
            if [ -n "$fallback_result" ]; then
                local cw_result=""
                cw_result=$(echo "$check_body" | python3 -c "
import sys, json
body = json.load(sys.stdin)
# Build stdin envelope for can-write
envelope = json.dumps({'tool_input': body.get('tool_input', {})})
print(envelope)
" 2>/dev/null | _writ_session can-write "$fallback_result" --skill-dir "${SKILL_DIR:-}" 2>/dev/null || echo '{"decision":"allow"}')
                echo "$cw_result"
                return 0
            fi
            # No answer obtainable at all (daemon down AND the body yielded no
            # session id for the local fallback). Policy point: fail open by
            # default, fail closed under WRIT_STRICT=1 so an auditor can pin the
            # gate to availability.
            if [ "${WRIT_STRICT:-}" = "1" ]; then
                echo '{"decision":"deny","reason":"[ENF-STRICT-001] Writ strict mode (WRIT_STRICT=1): the write gate could not be evaluated (daemon unreachable, no local fallback), so this write fails closed. Start the daemon (systemctl --user start writ-server) or unset WRIT_STRICT.","rag_rules":"","rag_meta":{"rule_ids":[],"tokens":0}}'
                return 0
            fi
            echo '{"decision":"allow","reason":null,"rag_rules":"","rag_meta":{"rule_ids":[],"tokens":0}}'
            return 0
            ;;
        "clear-rules-for-compaction")
            url="${WRIT_SESSION_BASE}/session/${session_id}/clear-rules-for-compaction"
            method="POST"
            body="{}"
            ;;
        "reset-after-compaction")
            url="${WRIT_SESSION_BASE}/session/${session_id}/reset-after-compaction"
            method="POST"
            body="{}"
            ;;
        *)
            # Unknown subcommand -- fall through to subprocess
            python3 "$helper" "$subcmd" "$@"
            return $?
            ;;
    esac

    # Try curl first (fast path)
    local result=""
    if [ "$method" = "POST" ]; then
        result=$(curl -sf --connect-timeout 0.1 --max-time 0.5 \
            -X POST "$url" \
            -H "Content-Type: application/json" \
            -d "$body" 2>/dev/null) || true
    else
        result=$(curl -sf --connect-timeout 0.1 --max-time 0.5 "$url" 2>/dev/null) || true
    fi

    if [ -n "$result" ]; then
        echo "$result"
        return 0
    fi

    # Fallback: subprocess
    python3 "$helper" $subcmd "$@"
    return $?
}

# ── Push-by-action methodology companion (1.8b) ──────────────────────────────
# Fetches the methodology node(s) a workflow ACTION pushes, rendered as text for
# injection. Calls /methodology-companion with action=<action>, NO mode (so no
# floor), empty prompt (no pull), empty exclude_ids (push re-surfaces through the
# turn's loaded set -- D-A; timing is the value). Returns the rendered body text
# on stdout, or nothing. FAIL-OPEN: any error (server down, empty push) -> empty
# output + return 0, so a calling hook never breaks on a missing/slow companion.
# `sid` is accepted for future friction attribution (1.8c); unused today.
# Usage: PUSH=$(writ_action_push "$SESSION_ID" "gate-denial")
# The request body for writ_action_push, extracted so it can be tested against the
# python path it replaces. Bash-built (no spawn, ~25ms saved on the write path) ONLY
# when the action is a plain token; anything else falls back to python.
#
# The allowlist decides, not a blocklist (ABS-SECURITY-024): hand-rolling JSON string
# escaping for arbitrary input is how injection bugs get written, so a token carrying
# a quote, backslash, whitespace or non-ASCII byte goes to the encoder that already
# handles it correctly. All four live callers pass plain tokens (gate-denial,
# review-feedback, bible-authoring, and a derived phase token), so the fast path is
# the normal path. WRIT_NO_BASH_JSON=1 forces the fallback, mirroring the WRIT_NO_JQ
# seam parsed_field uses, so tests can compare the two byte for byte.
writ_action_push_body() {
    local action="$1"
    [ -z "$action" ] && return 0
    local _fallback=0
    # A token outside the allowlist needs real JSON escaping: hand it to python.
    case "$action" in
        *[!A-Za-z0-9_-]* ) _fallback=1 ;;
    esac
    # The test seam is checked separately, not folded into the pattern above: a flag
    # value made of allowed characters (WRIT_NO_BASH_JSON=1) would not match the
    # unsafe class and the force would silently do nothing.
    [ -n "${WRIT_NO_BASH_JSON:-}" ] && _fallback=1
    if [ "$_fallback" = "1" ]; then
        # Propagate python's status rather than forcing 0: the caller's
        # `req=$(writ_action_push_body ...) || return 0` is the fail-open branch the
        # original `req=$(python3 ...) || return 0` relied on, and swallowing the
        # status here would quietly narrow that contract to the later empty check.
        python3 -c "import json,sys; print(json.dumps({'action':sys.argv[1],'prompt':'','exclude_rule_ids':[],'budget_tokens':2000}))" "$action" 2>/dev/null
        return $?
    fi
    printf '{"action": "%s", "prompt": "", "exclude_rule_ids": [], "budget_tokens": 2000}\n' "$action"
}

writ_action_push() {
    local sid="$1" action="$2"
    [ -z "$action" ] && return 0
    local req resp text
    req=$(writ_action_push_body "$action") || return 0
    [ -z "$req" ] && return 0
    # --fail keeps the previous fail-on-HTTP-error semantics: a >= 400 yields no body, and
    # the caller (fail-open by contract) returns empty rather than injecting an error doc.
    resp=$(WRIT_HTTP_CONNECT_TIMEOUT=0.2 WRIT_HTTP_TIMEOUT=2 \
        writ_http_post "${WRIT_SESSION_BASE}/methodology-companion" "$req" --fail 2>/dev/null) || return 0
    [ -z "$resp" ] && return 0
    # 1.8c push observability: log a methodology_push friction event (action +
    # per-channel counts + rule_ids + tokens) before returning the text -- recorded
    # even when 0 nodes match (the action still fired). Fire-and-forget; routes
    # through friction-append.py like every other hook emit. Single choke point ->
    # every action hook is observed without per-hook logging code.
    local _push_extra
    _push_extra=$(printf '%s' "$resp" | python3 -c "
import sys, json
from collections import Counter
try:
    r = json.load(sys.stdin)
except Exception:
    sys.exit(0)
rules = r.get('rules', []) or []
ch = Counter(x.get('channel') for x in rules)
print(json.dumps({
    'action': sys.argv[1],
    'channels': {'floor': ch.get('floor', 0), 'push': ch.get('push', 0), 'pull': ch.get('pull', 0)},
    'rules_returned_count': len(rules),
    'rule_ids': [x.get('rule_id') for x in rules],
    'tokens_injected': r.get('total_tokens', 0),
}))
" "$action" 2>/dev/null) || true
    [ -n "$_push_extra" ] && log_friction_event "$sid" "" "methodology_push" "$_push_extra" 2>/dev/null || true
    text=$(printf '%s' "$resp" | _writ_session format 2>/dev/null | grep -v '^WRIT_META:') || return 0
    [ -z "$text" ] && return 0
    printf '%s' "$text"
}

# ── Config readers ───────────────────────────────────────────────────────────
# Reads a single-line config value from a project config file.
# Usage: LEVEL=$(read_project_config "$PROJECT_ROOT" ".claude/phpstan-level" "8")
read_project_config() {
  local project_root="$1" config_file="$2" default="$3"
  local full_path="$project_root/$config_file"
  if [ -f "$full_path" ]; then
    cat "$full_path"
  else
    echo "$default"
  fi
}

# ── RAG hook shared blocks (B1 step 1) ───────────────────────────────────────
# These three were copy-pasted across writ-rag-inject.sh (per-prompt) and
# writ-posttool-rag.sh (per-write). Centralizing removes the 4x/4x/2x duplication
# the optimization audit flagged (D-WRITMETA-SH / D-RAGLOG / D-MODEDIR) and is the
# prerequisite for safely consolidating that hook's per-prompt reads/spawns.

# Parse a WRIT_META payload (the JSON after the "WRIT_META:" prefix) read on stdin;
# emit rule_ids JSON on line 1 and cost on line 2. Invalid/empty input -> "[]" / "0".
# Usage: META_FIELDS=$(echo "$META_JSON" | parse_writ_meta)
#        RULE_IDS=$(echo "$META_FIELDS" | sed -n '1p'); COST=$(echo "$META_FIELDS" | sed -n '2p')
parse_writ_meta() {
  python3 -c "
import sys, json
try:
    m = json.load(sys.stdin)
except Exception:
    m = {}
print(json.dumps(m.get('rule_ids', [])))
print(m.get('cost', 0))
" 2>/dev/null
}

# Build a /query request from (query, budget_tokens, exclude_rule_ids_json) and POST
# it to $WRIT_URL (caller must have set WRIT_URL; the RAG hooks do). Echoes the raw
# JSON /query response on stdout; empty string when the request cannot be built
# (python raises, e.g. malformed exclude JSON or non-numeric budget) or the POST
# fails / returns nothing. Callers bail on empty
# (`RESPONSE=$(rag_query ...); [ -z "$RESPONSE" ] && exit 0`).
# Always returns 0 (safe under set -euo pipefail). The build+POST body is byte-identical
# to the copies previously inlined in writ-read-rag.sh / writ-posttool-rag.sh.
# Build-failure path (python raises): HEAD ran that build in the hook's main shell, so
# set -e exited the hook non-zero; here it runs inside this function under $(...), where
# set -e is not inherited, so it degrades to an empty RESPONSE and a clean exit 0 (a
# benign no-rules skip, not a hook error). That path is unreachable from the two call
# sites (budget is bash arithmetic; exclude is always a valid JSON array via the callers'
# '[]' fallback), so live behavior is unchanged.
# Usage: RESPONSE=$(rag_query "$QUERY" "$PRETOOL_BUDGET" "$LOADED_RULE_IDS")
rag_query() {
    local query="$1" budget="$2" exclude="$3"
    local request
    request=$(python3 -c "
import json, sys
print(json.dumps({
    'query': sys.argv[1],
    'budget_tokens': int(sys.argv[2]),
    'exclude_rule_ids': json.loads(sys.argv[3]),
    'top_k': 3,
}))
" "$query" "$budget" "$exclude" 2>/dev/null)
    [ -z "$request" ] && return 0
    # Same budgets the inlined request carried (connect 0.3s, total 1s), now through the
    # HTTP wrapper: a missing accelerator degrades to urllib, not to "no rules this turn".
    WRIT_HTTP_CONNECT_TIMEOUT=0.3 WRIT_HTTP_TIMEOUT=1 \
        writ_http_post "$WRIT_URL" "$request" 2>/dev/null || true
}

# Emit a rag_query friction event and append it via friction-append.py. Builds the
# canonical entry (event=rag_query; rules_returned_count derived from rule_ids) and
# recovers a malformed rule_ids array to [] with a stderr breadcrumb -- the inline
# copies had diverged (posttool-rag dropped the recovery). Fire-and-forget.
# Usage: log_rag_query_event "$SESSION_ID" "$MODE" "broad" "$COST" "$RULE_IDS_JSON"
log_rag_query_event() {
  local session_id="$1" mode="$2" query_source="$3" tokens="$4" rule_ids="$5" effort="${6:-}" event_name="${7:-}" mechanism="${8:-}"
  local _sink; _sink=$(hook_log_sink)
  python3 -c "
import sys, json
from datetime import datetime, timezone
try:
    rule_ids = json.loads(sys.argv[5])
except (json.JSONDecodeError, ValueError) as _e:
    sys.stderr.write(
        f'[writ-hook json.loads recovery] argv[5] (rule_ids) in log_rag_query_event source={sys.argv[3]}: {_e}\\n'
        f'  len={len(sys.argv[5])} sample={sys.argv[5][:200]!r}\\n'
    )
    rule_ids = []
entry = {
    'ts': datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ'),
    'session': sys.argv[1],
    'mode': sys.argv[2] if sys.argv[2] else None,
    'event': 'rag_query',
    'query_source': sys.argv[3],
    'tokens_injected': int(sys.argv[4]),
    'rules_returned_count': len(rule_ids),
    'rule_ids': rule_ids,
}
# effort: CC's reasoning level for this turn (e.g. 'xhigh'). Telemetry so the friction
# analyzers can correlate effort with outcomes. Omitted when CC sent none.
if len(sys.argv) > 6 and sys.argv[6]:
    entry['effort'] = sys.argv[6]
# Delivery telemetry (#7): the CC event + emit mechanism this injection used,
# stored RAW so writ.shared.delivery.classify_delivery can bucket model vs
# debug-log at READ time (single source of truth, re-runnable per CC version).
# Do NOT classify here -- store the facts, classify offline.
if len(sys.argv) > 7 and sys.argv[7]:
    entry['event_name'] = sys.argv[7]
if len(sys.argv) > 8 and sys.argv[8]:
    entry['mechanism'] = sys.argv[8]
print(json.dumps(entry))
" "$session_id" "$mode" "$query_source" "$tokens" "$rule_ids" "$effort" "$event_name" "$mechanism" 2>>"$_sink" \
    | python3 "$_FRICTION_APPEND" --stdin-json 2>>"$_sink" || true
}

# Emit the "set mode before proceeding" directive (D-MODEDIR), byte-identical in the
# orchestrator and normal branches of writ-rag-inject.sh. Leading blank line preserved.
# Usage: emit_mode_directive "$SESSION_HELPER" "$SESSION_ID"
emit_mode_directive() {
  local session_helper="$1" session_id="$2"
  cat << MODE_DIRECTIVE

[Writ: set mode before proceeding]
Conversation: discussion, no code. Debug: investigating a problem, no code.
Review: evaluating code against rules, no code. Work: building/modifying code (full workflow).
Investigate: audit / explore / research a codebase or topic (evidence-grounded, read-heavy).
Declare: python3 ${session_helper} mode set <conversation|debug|review|work|investigate> ${session_id}
Full definitions: see HANDBOOK.md "Mode system" section.
MODE_DIRECTIVE
}
