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

# ── Durable state root and session-cache location (THE bash-side definition) ──
# Mirrors writ/shared/state_root.py: $XDG_STATE_HOME/writ when that variable is absolute (the
# XDG spec says a relative value is to be ignored), else ~/.local/state/writ. Session caches,
# the pending-test and lint scratch files and the typed logs all live under it, so they no
# longer depend on where this copy of Writ is installed. The previous default was
# <skill>/var/session, and a plugin install path carries the version, so every upgrade started
# from an empty state tree and orphaned every live session's mode and approvals.
#
# Parameter expansion only, no subshell: this file is sourced by every hook and a fork here is
# paid on every tool call. `${HOME:-}` because every hook runs under `set -u`. The trailing-slash
# strip makes one trailing slash resolve exactly as os.path.join does.
#
# Do NOT reintroduce a tempdir fallback here. Commit 152e722 moved session state off
# /tmp because tmpfiles.d declares `D /tmp`, which EMPTIES it at boot. Three bash
# copies of that old default outlived the move: the auto-route classifier's mode read
# then always answered "no mode set" (it was looking in a directory that holds no
# session caches at all), and a hook-started daemon was born pointed at the same empty
# directory, which is how a gate decision came to be logged with "mode": null.
# tests/test_state_root.py pins this against the package's value under the same environment.
case "${XDG_STATE_HOME:-}" in
    /*) _WRIT_STATE_ROOT="${XDG_STATE_HOME%/}/writ" ;;
    # Tilde, not ${HOME}: with HOME unset bash then asks the password database, exactly as
    # Python's os.path.expanduser does, so the two sides cannot split onto /.local/state.
    *)  _WRIT_STATE_ROOT=~/.local/state/writ ;;
esac
# Where every earlier release kept session state: this install's own var/session. The state
# gates keep protecting it, because bin/lib/writ_state_migrate.py copies caches OUT of it at
# SessionStart and a cache forged there would otherwise be carried in.
_WRIT_LEGACY_SESSION_DIR="$_WRIT_SKILL_DIR/var/session"

writ_session_cache_dir() {
    if [ -n "${WRIT_CACHE_DIR:-}" ]; then
        printf '%s' "$WRIT_CACHE_DIR"
        return 0
    fi
    printf '%s' "$_WRIT_STATE_ROOT/session"
}

# The session's mode read STRAIGHT from the cache file: stdlib only, no writ import and
# no daemon round-trip. The auto-route classifier needs this because a daemon `mode get`
# can spuriously return empty under load, and acting on a false empty would let the
# classifier fight an explicit choice. Prints the mode, or "" when absent/unset/unreadable.
# jq-first, because this is a one-field read of a small JSON file and python charges
# ~13ms of interpreter startup for it against jq's ~2ms (measured 2026-08-07). Same
# WRIT_NO_JQ seam as parsed_field. A missing, empty, or unparseable file prints "" on
# both arms, which is the contract the callers branch on.
writ_session_mode_direct() {
    local p
    p="$(writ_session_cache_dir)/writ-session-$1.json"
    if [ -z "${WRIT_NO_JQ:-}" ] && command -v jq >/dev/null 2>&1; then
        # `|| true` is load-bearing. jq exits 2 on a missing file and 4/5 on a corrupt
        # one, and every hook runs under `set -euo pipefail`, so the bare pipeline
        # aborted the CALLING hook with status 2 for a session that simply has no cache
        # file yet. The python arm never did that because it catches the exception and
        # prints "". Absence of a cache file is a normal state here, not an error.
        { jq -r '.mode // "" | tostring' "$p" 2>/dev/null || true; } | tr -d '[:space:]'
        return 0
    fi
    python3 -c "
import json, os, sys
try:
    print(json.load(open(sys.argv[1])).get('mode') or '')
except Exception:
    print('')
" "$p" 2>/dev/null | tr -d '[:space:]'
}

# The runtime-lens read gate's skip predicate.
# writ_runtime_lens_check_required <session_id>
#   exit 0  the expensive `writ-session.py can-read-code` check is REQUIRED
#   exit 1  the runtime lens provably cannot deny for this session; skipping is safe
#
# THE PROPERTY. gates._can_read_code_check returns allow immediately unless
# mode_engine._effective_source_type(cache) == "runtime", and that helper returns the
# cache's own source_type when truthy, else MODE_CONFIG[mode]["source_type"], where
# "debug" is the only mode carrying a static "runtime". So the check is required when
# source_type == "runtime", or when source_type is falsy and mode == "debug".
# tests/test_debug_lens_predicate.py evaluates that against the REAL gate over the
# derived cross product of modes, source types and matcher tools.
#
# THE FAIL DIRECTION IS THE SAFETY ARGUMENT. Anything that does not resolve to a parsed
# JSON OBJECT returns "check required": an absent file, an unreadable one, bytes that
# are not JSON, a non-object top level, a session id that names no reachable path, or a
# line this function cannot split unambiguously. Skipping on uncertainty would be a
# silent gate bypass, so uncertainty always pays the full cost and never grants a read.
#
# ONE PROCESS FOR BOTH FIELDS, and the same WRIT_NO_JQ seam parsed_field uses. Each arm
# emits a sentinel-prefixed, unit-separated line ONLY when the document parses and is an
# object, and each field is emitted as "s:<value>" when it is a JSON string and "x"
# otherwise. The arms never decide anything: the comparison against the literals "debug"
# and "runtime" happens here, once, in bash. That is deliberate. jq's `//` falls through
# on null and false while python's `or` also falls through on an empty string, and jq
# calls 0 truthy where python calls it falsy; transporting the JSON TYPE rather than a
# truthiness verdict means the two arms cannot disagree. A value that is not a string
# can never be the string "runtime", so treating every non-string as "no usable
# source_type" and deferring to the mode is safe in the required direction.
#
# The jq call is wrapped so absence is a normal input: jq exits 2 on a missing file and
# 4/5 on a corrupt one, and every hook runs under `set -euo pipefail`, exactly as
# writ_session_mode_direct documents.
writ_runtime_lens_check_required() {
    local _wrlc_path _wrlc_line _wrlc_tag _wrlc_mode _wrlc_source _wrlc_extra
    _wrlc_path="$(writ_session_cache_dir)/writ-session-$1.json"
    _wrlc_line=""
    if [ -z "${WRIT_NO_JQ:-}" ] && command -v jq >/dev/null 2>&1; then
        _wrlc_line="$({ jq -r --arg us $'\x1f' 'if type == "object" then
    "WRLC" + $us
    + (if (.mode | type) == "string" then "s:" + .mode else "x" end)
    + $us
    + (if (.source_type | type) == "string" then "s:" + .source_type else "x" end)
else empty end' "$_wrlc_path" 2>/dev/null || true; })"
    else
        _wrlc_line="$(python3 -c '
import json, sys
US = chr(31)
try:
    with open(sys.argv[1]) as fh:
        d = json.load(fh)
except Exception:
    sys.exit(0)
if not isinstance(d, dict):
    sys.exit(0)
def tok(v):
    return "s:" + v if isinstance(v, str) else "x"
sys.stdout.write("WRLC" + US + tok(d.get("mode")) + US + tok(d.get("source_type")) + "\n")
' "$_wrlc_path" 2>/dev/null || true)"
    fi

    # A value carrying a newline or a unit separator would shift the field boundaries,
    # so a line this function cannot split unambiguously pays the check.
    case "$_wrlc_line" in
        *$'\n'*) return 0 ;;
    esac
    _wrlc_tag=""; _wrlc_mode=""; _wrlc_source=""; _wrlc_extra=""
    IFS=$'\x1f' read -r _wrlc_tag _wrlc_mode _wrlc_source _wrlc_extra <<<"$_wrlc_line" || true
    [ "$_wrlc_tag" = "WRLC" ] || return 0
    [ -z "$_wrlc_extra" ] || return 0

    [ "$_wrlc_source" = "s:runtime" ] && return 0
    case "$_wrlc_source" in
        "s:"|x)
            [ "$_wrlc_mode" = "s:debug" ] && return 0
            ;;
    esac
    return 1
}

# ── The session mode a telemetry / audit row is stamped with ─────────────────
# Resolves that mode into the global _WRIT_ROW_MODE.
#
# WHY THIS LIVES HERE AND NOT IN THE HOOKS. log_gate_decision stamped its row from the
# bare `${CURRENT_MODE:-${MODE:-}}`, and exactly ONE of the fourteen gate-emitting hooks
# ever assigns that variable: validate-test-file.sh, whose rows measure 0% null across
# 321. The other thirteen never did, so 4,672 of 4,993 gate_decision rows -- 93.6% --
# recorded "mode": null while the session cache said "work". That is why it survived: a
# null reads as an honest mode-unset session, not as a lost value. A value that has to be
# set correctly in thirteen places is exactly how it drifted, so it is resolved once, in
# the single place that consumes it.
#
# FILE-DIRECT, never the daemon. A daemon started against a different WRIT_CACHE_DIR
# answers a stale mode=None (writ-subagent-start.sh:65-70) -- the same wrong answer this
# exists to fix. writ_session_mode_direct above is that read.
#
# IT SETS A GLOBAL RATHER THAN PRINTING, deliberately: a printing helper is called as
# `$(...)`, which runs the memo assignment in a subshell and discards it, so every call
# would re-read the cache. log_gate_decision is on the per-write hot path and
# writ-bash-write-gate.sh alone has eleven call sites.
_WRIT_ROW_MODE=""
_WRIT_ROW_MODE_MEMO=""
_WRIT_ROW_MODE_SID=""
_writ_row_mode() {
    # An explicitly set variable always wins. A hook that computed the mode itself
    # (validate-test-file.sh's MODE, writ-dispatch-discipline.sh's CURRENT_MODE) is
    # recording the value it ACTED on, which is the one the audit trail wants.
    _WRIT_ROW_MODE="${CURRENT_MODE:-${MODE:-}}"
    [ -n "$_WRIT_ROW_MODE" ] && return 0
    local _sid="${SESSION_ID:-${HOOK_SESSION_ID:-}}"
    # No session, no lookup. An empty id names writ-session-.json, a file that never
    # exists, so the read could only cost a process to learn "".
    [ -z "$_sid" ] && return 0
    # Memoized per process: each hook is its own process, so the cached value cannot
    # outlive the state it describes. Keyed by session id because a few hooks resolve
    # their own id after their first log call.
    if [ "$_WRIT_ROW_MODE_SID" != "$_sid" ]; then
        _WRIT_ROW_MODE_MEMO="$(writ_session_mode_direct "$_sid")"
        _WRIT_ROW_MODE_SID="$_sid"
    fi
    _WRIT_ROW_MODE="$_WRIT_ROW_MODE_MEMO"
    return 0
}

# The same value WITHOUT triggering a resolution: an explicit variable, else whatever a
# log_gate_decision in this same process already looked up.
#
# hook_instrument's exit trap uses this instead of _writ_row_mode because the trap runs
# on EVERY instrumented hook, including the many that never log a decision, and a single
# file write fires fifteen of them. A lookup there would put a process back on all
# fifteen to stamp a metrics row, against a process budget with single-digit headroom
# (tests/test_write_path_process_budget.py). A hook that DID log a decision gets the
# right mode on its hook_execution row for free.
_writ_row_mode_cached() {
    _WRIT_ROW_MODE="${CURRENT_MODE:-${MODE:-${_WRIT_ROW_MODE_MEMO:-}}}"
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

# Which transport a given URL should use. Only DAEMON urls go through the private
# socket.
#
# WHY THIS IS PER-URL. writ_http_get / writ_http_post are GENERIC helpers: hooks call
# them for the daemon and for other hosts alike. An earlier version of this change put
# the socket flag on them unconditionally, which made curl ignore the URL's host and
# fail to connect (exit 7) for every non-daemon call: 15 tests across 5 modules caught
# it. The daemon-only call sites further down can use $WRIT_CURL_TRANSPORT directly.
_writ_transport_for() {
    case "$1" in
        http://localhost:"${WRIT_SESSION_PORT}"/*|http://127.0.0.1:"${WRIT_SESSION_PORT}"/*|http://localhost:"${WRIT_SESSION_PORT}"|http://127.0.0.1:"${WRIT_SESSION_PORT}")
            printf '%s' "${WRIT_CURL_TRANSPORT:-}"
            ;;
        *)
            printf ''
            ;;
    esac
}

# One curl attempt, then ONE retry over TCP when the socket refused the connection.
#
# Exit 7 is "couldn't connect". A socket FILE outliving its listener is the ordinary
# aftermath of a crashed or replaced daemon, and it satisfies `[ -S ]`, so before this
# retry every daemon call over a stale socket failed and fell through to the local
# python subprocess: correct, because that fallback is the design, but the daemon went
# unused with nothing announcing it. Retrying only on 7 costs nothing when the socket
# is healthy, which is why this is a per-call retry rather than a probe at source time
# (a probe would add a round trip or a python start to EVERY hook).
_writ_curl_with_fallback() {
    local url="$1"
    shift
    local transport rc=0
    transport=$(_writ_transport_for "$url")
    curl $transport "$@" || rc=$?
    if [ "$rc" -eq 7 ] && [ -n "$transport" ]; then
        rc=0
        curl "$@" || rc=$?
    fi
    return $rc
}

writ_http_get() {
    local url="$1"
    local fail="" arg
    for arg in "$@"; do
        [ "$arg" = "--fail" ] && fail="--fail"
    done
    local ct="${WRIT_HTTP_CONNECT_TIMEOUT:-0.5}" mt="${WRIT_HTTP_TIMEOUT:-10}" rc=0
    if [ -z "${WRIT_NO_CURL:-}" ] && command -v curl >/dev/null 2>&1; then
        if [ -n "$fail" ]; then
            _writ_curl_with_fallback "$url" -sf --connect-timeout "$ct" --max-time "$mt" "$url" || rc=$?
        else
            _writ_curl_with_fallback "$url" -s --connect-timeout "$ct" --max-time "$mt" "$url" || rc=$?
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
            _writ_curl_with_fallback "$url" -sf --connect-timeout "$ct" --max-time "$mt" -X POST "$url" \
                -H "Content-Type: application/json" -d "$body" || rc=$?
        else
            _writ_curl_with_fallback "$url" -s --connect-timeout "$ct" --max-time "$mt" -X POST "$url" \
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
#
# HOOK_SESSION_ID IS THE PAYLOAD'S ID OR THE EMPTY STRING. It is never synthesized.
# See writ_require_session for the whole argument; the short form is that an id built
# from PPID or md5(cwd:user) can never equal the one Claude Code uses, so every write
# under it lands in a session that does not exist while the hook reports success.
#
# EMPTY, NOT FATAL. Many callers legitimately need no session (a gate that only
# inspects a file path), so this must not exit or return non-zero: doing that would
# turn a Writ bookkeeping problem into a broken tool call. A caller that DOES require
# a session tests for empty, calls `writ_critical <hook> "<message>"`, and no-ops.
#
# THIS DOES NOT WEAKEN ANY GATE. With the fallback, a missing id became a synthetic id,
# that id had no session cache, so is_work_mode found no mode and the gate allowed.
# With the fallback gone the id is empty, still no mode, and the gate still allows --
# the same outcome, now RECORDED as a critical error instead of passing in silence.
# Usage: load_hook_env; echo "$HOOK_FILE_PATH"
load_hook_env() {
    HOOK_SESSION_ID="" HOOK_SESSION_ID_RAW="" HOOK_AGENT_ID="" HOOK_AGENT_TYPE="" HOOK_EVENT=""
    HOOK_TOOL_NAME="" HOOK_FILE_PATH="" HOOK_COMMAND="" HOOK_IS_ERROR="" HOOK_ENVELOPE=""
    # Black-box capture (gated): when ON, read the RAW stdin once, feed it to the parser,
    # and log the RAW envelope -- the true CC payload, not Writ's normalized form. When OFF,
    # the parser reads stdin directly exactly as before: zero added cost on the hot path.
    local _bb_raw=""
    if blackbox_enabled; then
        _bb_raw=$(cat)
        eval "$(printf '%s' "$_bb_raw" | _writ_parse_hook_stdin)"
    else
        eval "$(_writ_parse_hook_stdin)"
    fi
    # NOTHING RUNS HERE. Two fallbacks used to: `ps -o ppid=` and md5(cwd:user)-date.
    # They were removed from 9 hooks first and survived here, in the one place ~20 hooks
    # share, which is why the removal was not the removal it was reported to be.
    # HOOK_SESSION_ID stays whatever the envelope carried, including "".
    # Log the raw envelope (the calling hook's basename labels it). Never affects the caller.
    if [ -n "${_bb_raw:-}" ]; then
        printf '%s' "$_bb_raw" | blackbox_log in "$(basename "${BASH_SOURCE[1]:-$0}" .sh)" "${HOOK_SESSION_ID:-}" "${HOOK_EVENT:-}" || true
    fi
    _writ_seed_subagent_cache
}

# The sub-agent seeder's skip predicate.
# writ_cache_already_seeded <cache_file>
#   exit 0  the file POSITIVELY DECLARES a seeded cache; skipping the seeder is safe
#   exit 1  everything else, including every state this cannot resolve
#
# THIS IS A FAST PATH, NOT THE RULE. The rule lives in
# writ/session/subagent_seed.py::already_seeded, and this exists only because the bash guard
# SHADOWS it: the seeder returns here before the interpreter is ever spawned, so a
# python-only fix would never run. A seeded cache declares a non-empty `cache_source`, or
# `is_subagent: true` for a cache written before `cache_source` existed.
#
# WHAT THIS REPLACED, AND WHY IT WAS WRONG. The guard was `[ -f "$_cache_file" ]`, a test of
# FILE EXISTENCE standing in for ALREADY SEEDED. A gate denial creates the file:
# gates._log_gate_denial counts the denial inside `with mutate_cache(...)`, whose context
# manager ends in an unconditional _write_cache, and _read_cache returns the defaults on a
# miss without writing. So an ungoverned sub-agent's FIRST REFUSED WRITE wrote a cache
# carrying only denial_counts, with no mode and no cache_source, and the seeder then returned
# early forever. The refusal that proved the agent needed governance was what locked it out
# of governance permanently.
#
# THE FAIL DIRECTION IS THE WHOLE SAFETY ARGUMENT, and it is writ_runtime_lens_check_required's
# rule applied to the other side of the same seam: anything unresolved pays the full cost.
# A missing file, an unparseable file, a non-object document, a document with neither field,
# an absent jq and WRIT_NO_JQ all exit 1 and fall through to the python seeder, which is the
# authority and which declines cheaply. Failing toward SEEDING is the safe direction here,
# because a seeded cache is marked lazy_seed and gates._authority_mode resolves that mode as
# ABSENT: a redundant seed grants nothing, where a wrongly skipped one leaves an agent
# ungovernable.
#
# THERE IS DELIBERATELY NO PYTHON FALLBACK ARM. It would pay one python start to answer a
# strictly smaller question than the seeder answers for the same price, so it would buy
# nothing; a jq-less host simply pays the seeder's start instead.
#
# The jq call is wrapped so absence is a normal input: jq exits 2 on a missing file and 4/5
# on a corrupt one, and every hook runs under `set -euo pipefail`, exactly as
# writ_session_mode_direct documents. The arm emits a SENTINEL rather than a truthiness
# verdict, so a multi-document stream or a value carrying a newline yields a string that is
# not the sentinel and falls through, rather than forging a positive answer.
writ_cache_already_seeded() {
    local _wcas_file="${1:-}" _wcas_answer=""
    [ -n "$_wcas_file" ] || return 1
    [ -z "${WRIT_NO_JQ:-}" ] || return 1
    command -v jq >/dev/null 2>&1 || return 1
    _wcas_answer="$({ jq -r 'if type == "object"
    and ((((.cache_source | type) == "string") and (.cache_source != ""))
         or (.is_subagent == true))
then "WCAS-SEEDED" else empty end' "$_wcas_file" 2>/dev/null || true; })"
    [ "$_wcas_answer" = "WCAS-SEEDED" ] && return 0
    return 1
}

# GOVERN A SUB-AGENT ON THE HOOK THAT RUNS, NOT THE EVENT THAT MIGHT NOT FIRE.
#
# `SubagentStart` creates a sub-agent's cache, and it does not arrive for every sub-agent.
# Measured 2026-08-27 across all 239 log files (including the 226 gzipped archives an
# earlier grep of mine silently skipped): 1,381 distinct agents have a `subagent_start` row,
# 2,219 hit the stop-side fallback instead, and the two sets do NOT intersect. So 64% of
# sub-agents never inherit a mode, never inherit the approved gates, and never get a rule
# injected. 1,425 of them DO have daemon rows under their own agent id, which means Writ
# hooks ran inside them: that is the seam this uses.
#
# HERE, NOT IN THREE HOOKS. Every hook calls load_hook_env, and the envelope it parses
# carries both halves needed: HOOK_AGENT_ID (the child) and HOOK_SESSION_ID_RAW (the parent,
# kept separate precisely because HOOK_SESSION_ID prefers the agent id). A call added to the
# PreToolUse hooks that matter today is a list that goes stale the moment a fourth one does,
# which the telemetry cycle measured four times over before making coverage structural.
#
# THE FIELDS ARE ARGUMENTS, NOT GLOBALS, so a hook that has already consumed stdin can still
# reach this. load_hook_env is unusable in writ-debug-code-gate.sh (Grep, Glob) and
# validate-exit-plan.sh (ExitPlanMode) because each reads stdin itself before sourcing this
# library, and stdin cannot be re-read, so those three tools reached no seeder at all. They
# call this entry point with the fields their own parse already holds.
#
# THE PARENT ARGUMENT IS THE RAW SESSION ID, never the agent-preferring collapsed one:
# seeding needs BOTH halves of the pair, and a collapsed id would make the child its own
# parent and decline.
#
# IT GRANTS NOTHING. The seeded cache is marked `lazy_seed`, and the write gate resolves a
# lazy_seed cache's mode as ABSENT, so the write decision is identical to the no-cache
# decision on every path. See writ/session/gates.py::_authority_mode.
#
# COST. For a main session this is one test on an empty variable, no process. For a
# sub-agent it is one jq process per hook and a single python start on the FIRST hook only;
# every later hook in that agent reads a declared seeded cache and returns. A jq-less host
# skips the fast path entirely and pays the python start instead, which declines cheaply.
#
# THE STATUS WORD IS CAPTURED, NEVER PRINTED. The inline python prints exactly one word on
# every outcome it can reach (seeded, skipped, failed) and the command substitution keeps it
# out of the hook's stdout, which is a model channel on some events. A NON-EMPTY word means
# the seeder spoke for itself, so bash stays silent and there is exactly one row per
# failure; an EMPTY word means the python never ran at all (an exec killed by an oversized
# envelope value, a broken interpreter, a killed process), and only then does bash write the
# row. Nothing is inferred from a missing telemetry row, and the substitution adds no
# process: its body is a single external command, which bash execs inside the fork the
# substitution already pays for.
#
# THE BASH ROW CARRIES NOTHING FROM THE ENVELOPE. The agent id rides as the friction session
# argv, where friction-append.py owns the quoting, and the two JSON fields are literals. A
# row built from the value that killed the seed would die exactly where the seed died, which
# is the lesson the oversized-agent_type cycle already paid for, and literals leave no
# question about interpolating an untrusted value into hand-built JSON.
#
# STILL EXIT 0, STILL NOTHING ON STDOUT, STILL NO writ_critical. A sub-agent that cannot
# inherit governance is a gap to report, never a hook failure. `|| _seed_status=""` IS WHAT
# ABSORBS ERREXIT, and every hook sources this file under `set -euo pipefail`, so DO NOT
# DELETE IT AS REDUNDANT. Measured all three ways: `local v; v=$(false)` ABORTS the function,
# `local v=$(false)` survives (there `local` is the command and carries its own status), and
# `local v; v=$(false) || v=""` survives. Declaring `local` separately is what makes errexit
# SEE the assignment's status, not what hides it; an earlier version of this comment said the
# opposite and would have read as permission to remove the one guard holding the hook up.
# `2>/dev/null` stays on the python so an unbounded traceback cannot land in the operator's
# transcript, and unlike the spawn path there is no critical line: the agent is already
# running, a lazily seeded cache confers nothing, and the message would repeat on every hook
# inside that agent.
# Usage: writ_seed_subagent_from_fields <agent_id> <parent_session_id> <agent_type>
writ_seed_subagent_from_fields() {
    local _seed_agent="${1:-}" _seed_parent="${2:-}" _seed_type="${3:-}"
    [ -n "$_seed_agent" ] || return 0
    [ "$_seed_agent" != "$_seed_parent" ] || return 0
    [ -n "$_seed_parent" ] || return 0
    local _cache_file _seed_status
    _cache_file="$(writ_session_cache_dir)/writ-session-${_seed_agent}.json"
    writ_cache_already_seeded "$_cache_file" && return 0
    _seed_status=$(WRIT_SEED_AGENT_ID="$_seed_agent" \
    WRIT_SEED_PARENT="$_seed_parent" \
    WRIT_SEED_AGENT_TYPE="$_seed_type" \
    python3 -c '
import os, sys
sys.path.insert(0, sys.argv[1])
agent = os.environ.get("WRIT_SEED_AGENT_ID", "")
try:
    from writ.session.subagent_seed import seed_subagent_cache
    print("seeded" if seed_subagent_cache(
        agent, os.environ.get("WRIT_SEED_PARENT", ""),
        envelope_agent_type=os.environ.get("WRIT_SEED_AGENT_TYPE", "")) else "skipped")
except Exception as exc:
    # A sub-agent that cannot inherit governance is a gap to report, never a hook failure.
    # Anything that escapes seed_subagent_cache (a failed import, a raise out of
    # resolve_role) is recorded here by the process already running; a fault INSIDE it
    # recorded itself at the site where the fault and a decline are still distinguishable,
    # and returned False, so this arm cannot double-report one failure.
    try:
        from writ.session.subagent_seed import CACHE_SOURCE_LAZY, log_seed_failure
        log_seed_failure(agent, CACHE_SOURCE_LAZY, exc)
        print("failed")
    except Exception:
        pass
' "$_WRIT_SKILL_DIR" 2>/dev/null) || _seed_status=""
    if [ -z "$_seed_status" ]; then
        log_friction_event "$_seed_agent" "" "subagent_seed_failed" \
            '{"hook":"seed-subagent-cache","cache_source":"lazy_seed"}'
    fi
    return 0
}

# The load_hook_env call site, unchanged for its 22 callers: the envelope's own fields,
# handed to the one entry point above.
_writ_seed_subagent_cache() {
    writ_seed_subagent_from_fields "${HOOK_AGENT_ID:-}" "${HOOK_SESSION_ID_RAW:-}" \
        "${HOOK_AGENT_TYPE:-}"
}

# Black-box capture: append the RAW Claude-Code <-> hook payloads to a JSONL so the
# actual contract can be inspected empirically (what CC sends a hook, and what the
# hook returns to Claude) instead of inferred. Reads the payload from stdin.
#   direction: "in"   = the envelope CC passed the hook (prompt, tool_input, agent_type, ...)
#              "out"  = what the hook emits back to Claude (stdout / additionalContext)
#              "exit" = the hook's own non-zero exit status, written by the shared exit trap
# Opt-in via WRIT_BLACKBOX=1 -> when unset this is a no-op that still drains stdin, so
# wiring it into a pipe is zero-overhead and behavior-neutral in production. Never fails
# the caller. Log path: $WRIT_BLACKBOX_LOG (default ~/.claude/writ-blackbox.jsonl).
# Usage:  printf '%s' "$STDIN_JSON" | blackbox_log in "$(basename "$0")" "$SESSION_ID" \
#                                                     "$HOOK_EVENT"
#         blackbox_log exit "$hook" "$session" "$HOOK_EVENT" "$rc" </dev/null
#
# EVENT IS ALWAYS WRITTEN, as a string when the caller observed one and as JSON null when
# it did not. `${x:-}` collapsing to "" would make "this row predates the schema" (key
# ABSENT) indistinguishable from "this process observed no event" (key PRESENT, null), and
# both are falsy under .get(), so the reader could not tell them apart either.
#
# EXIT_CODE IS WRITTEN ONLY ON AN exit ROW, and a non-numeric value DROPS THE ROW rather
# than being coerced, so an exit row without a code cannot exist.
#
# PID IS THE HOOK SHELL'S OWN `$$`, passed in through the environment. It used to be the
# encoder's os.getpid(), which is a DIFFERENT ephemeral python per call, so the
# (hook, pid, session) join in writ/analysis/blackbox.py never matched a real IN row to a
# real OUT row: all four real OUT classes in the committed census read
# origins {harness: 0, undetermined: N}. `$$` stays the hook shell's pid inside both the
# pipeline and the command substitution below, which is exactly the value the join needs.
#
# The "out" direction is NOT called from a hook script. emit_hook_reply below is the one
# writer, so an out row can only ever hold the bytes that were actually sent; a hook that
# cannot reach the logger cannot record a reconstruction of its reply instead of the reply.
# The "exit" direction is likewise never called from a hook script: _writ_hook_exit_trap
# owns it (tests/_inventory.py::direct_blackbox_exit_calls stays empty).
# Default cap on the capture log, 256 MiB. Named rather than inline so an operator
# who finds capture stopped can grep for what bounded it. Override:
# WRIT_BLACKBOX_MAX_BYTES.
WRIT_BLACKBOX_MAX_BYTES_DEFAULT=268435456

# ── Buffered hook telemetry ──────────────────────────────────────────────────
# 8 of the 15 hooks a file write fires used to spawn python purely to append one
# `hook_execution` row: ~96ms per write for logging nobody reads synchronously
# (measured 2026-08-07). The row is now appended by bash and drained once per turn.
#
# COVERAGE IS UNIVERSAL BECAUSE OF WHERE A HOOK LIVES, not because each one remembers to
# ask: the block at the end of this file installs the trap when a script under
# hooks/scripts/ sources common.sh. An earlier version of this comment claimed coverage
# was universal and enforced while it was opt-in, and 8 registered hooks emitted nothing
# behind that sentence. Two rules keep it true: a hook registers exit work with
# `writ_on_exit`, never with `trap ... EXIT`, because bash allows one EXIT trap and a
# second silently replaces this one (tests/test_exit_trap_ownership.py fails on any hook
# that takes the trap directly), and a hook does NOT emit its own hook_execution row,
# because the trap already does (tests/test_hook_telemetry_coverage.py fails on any that
# still calls hook_timer_end).
#
# An earlier version of this comment claimed the opposite and called the gap acceptable,
# on the grounds that running the telemetry after another handler would report that
# handler's `$?` and corrupt a gate decision. Review checked and refuted it: the two
# hooks in question exit 0 on every path and carry their decision in stdout JSON, not
# `$?`. The rc-preserving form below is exact for every exit shape regardless.
#
# THIS IS MORE DURABLE THAN THE SPAWN IT REPLACES, not less. Before, the row existed
# only as arguments to a process that might never start, so a failed spawn dropped it
# with no trace. Now it is on disk before any process runs, and the drain unlinks the
# buffer only after emitting, so a crash mid-drain replays instead of losing rows.
#
# ATOMICITY: Linux does not interleave O_APPEND writes at or below PIPE_BUF (4096
# bytes), which is what lets several hooks share one buffer with no lock. That holds
# only while rows stay under the limit, so the writer TRUNCATES rather than trusting
# callers, and records that it truncated (a silently cut row is a lie; a marked one is
# data). Fields are separated by the unit/record separators defined just below.
WRIT_EVENT_ROW_MAX=3072
WRIT_EVENT_FIELD_MAX=400

writ_event_buffer_path() {
    printf '%s/writ-events-%s.buf' "$(writ_session_cache_dir)" "${1:-unknown}"
}

# Usage: writ_event_buffer_append <session> <hook> <duration_ms> <exit_code> <mode>
# Never fails the caller: telemetry failure must not become enforcement failure.
writ_event_buffer_append() {
    local session="${1:-}" hook="${2:-}" dur="${3:-0}" rc="${4:-0}" mode="${5:-}"
    local truncated=0
    if [ "${#hook}" -gt "$WRIT_EVENT_FIELD_MAX" ]; then
        hook="${hook:0:$WRIT_EVENT_FIELD_MAX}"
        truncated=1
    fi
    if [ "${#mode}" -gt "$WRIT_EVENT_FIELD_MAX" ]; then
        mode="${mode:0:$WRIT_EVENT_FIELD_MAX}"
        truncated=1
    fi
    # A separator inside a value would forge a second row, the same argument
    # SEC-INJ-LOG-001 makes about newlines in a log line.
    hook="${hook//[$'\x1e\x1f\n\r']/_}"
    mode="${mode//[$'\x1e\x1f\n\r']/_}"
    local row
    printf -v row 'hook_execution\x1f%s\x1f%s\x1f%s\x1f%s\x1f%s\x1e' \
        "$hook" "$dur" "$rc" "$mode" "$truncated"
    # Still over the limit after per-field truncation: cut the row itself rather than
    # dropping it. Dropping was the first spelling, and it contradicted this function's
    # own position that a silently cut row is a lie while a marked one is data. The
    # separator is re-appended so the record still parses as a record.
    if [ "${#row}" -gt "$WRIT_EVENT_ROW_MAX" ]; then
        printf -v row 'hook_execution\x1f%s\x1f%s\x1f%s\x1f%s\x1f1\x1e' \
            "${hook:0:64}" "$dur" "$rc" "${mode:0:32}"
    fi
    local buf
    buf="$(writ_event_buffer_path "$session")"
    _writ_buffer_append "$buf" "$row"
    return 0
}

# Append a buffer row, paying a `mkdir` process only when one is actually needed.
#
# WHY THIS IS NOT A BARE `[ -d ] || mkdir -p`. The event-buffer append above runs once
# at exit on every instrumented hook, so an unconditional `mkdir -p` is 10 processes on
# a single file write and the largest per-write item left in this library. But the
# unconditional call WAS doing real work in one case: if the cache directory is removed
# mid-session it silently recreates it and the row lands. A bare directory test would
# skip the create, the append would fail, and the `|| true` beside it would swallow the
# loss in silence -- trading a telemetry or audit row for a process, which is the wrong
# trade for a record that proves a gate ran.
#
# So: test the directory, skip the create while it exists, and if the append fails
# ANYWAY, create the directory and retry ONCE. The mkdir stays reachable exactly when it
# is needed and never otherwise, and that is provable by deleting the directory between
# two calls rather than by reading this comment.
#
# Never fails the caller: telemetry failure must not become enforcement failure. A
# caller that needs to KNOW whether the row landed (the gate-decision path, which falls
# through to a synchronous emit) tests the append itself instead of calling this.
_writ_buffer_append() {
    local _buf="${1:-}" _row="${2:-}" _dir
    [ -n "$_buf" ] || return 0
    _dir="${_buf%/*}"
    [ -d "$_dir" ] || mkdir -p "$_dir" 2>/dev/null || true
    if printf '%s' "$_row" >> "$_buf" 2>/dev/null; then
        return 0
    fi
    mkdir -p "$_dir" 2>/dev/null || true
    printf '%s' "$_row" >> "$_buf" 2>/dev/null || true
    return 0
}

# Buffer one COMPLETE friction entry (a JSON object as produced by the hook's row
# builder) instead of spawning friction-append.py to consume it.
#
# Measured 2026-08-07: that spawn is 35.3ms on the prompt path, and almost none of it is
# work. The interpreter floor is 9.5ms and `import writ.shared.logging` adds 12.5 more
# (52 modules, including ipaddress and locale) to append one line. The drain already
# imports it once per turn for the other row kinds, so these rows ride along for free.
#
# RETURNS 1 WHEN THE CALLER MUST SPAWN INSTEAD, which is the whole safety argument.
# The other row kinds truncate an oversized field and mark it, because a cut hook name is
# still readable data. Truncating JSON produces INVALID JSON, which the drain skips, so
# the same policy here would silently convert a slow row into a lost row. Oversized
# entries take the old direct path: correct, rarer, and slower only when it happens.
writ_friction_buffer_append() {
    local session="${1:-}" entry="${2:-}"
    [ -n "$entry" ] || return 0
    # A separator inside the payload would forge a second row (SEC-INJ-LOG-001's argument
    # about newlines in a log line). json.dumps already escapes newlines, so this guards
    # a hand-built entry rather than the normal path.
    entry="${entry//[$'\x1e\x1f\n\r']/ }"
    local row
    printf -v row 'friction_event\x1f%s\x1e' "$entry"
    [ "${#row}" -le "$WRIT_EVENT_ROW_MAX" ] || return 1
    local buf
    buf="$(writ_event_buffer_path "$session")"
    # Same guarded append as the hook_execution rows above: no `mkdir` process while the
    # buffer directory exists, and a create-and-retry so a directory removed mid-session
    # costs a process rather than the row.
    _writ_buffer_append "$buf" "$row"
    return 0
}

# Drain a session's buffer through the real logging package, in ONE interpreter start.
# Called at turn end (Stop), and again at SessionEnd for a turn that never reached Stop.
writ_event_buffer_flush() {
    local session="${1:-}" buf
    buf="$(writ_event_buffer_path "$session")"
    if [ -s "$buf" ]; then
        python3 "$_WRIT_LIB_DIR/writ-flush-events.py" "$session" >/dev/null 2>&1 || true
    fi
    return 0
}

# Is capture on? WRIT_BLACKBOX=1 OR the sentinel file ~/.claude/writ-blackbox.on (the
# sentinel works for already-running CC sessions that can't get a new env var; remove the
# file to disable). ONE copy of the test, shared by load_hook_env, blackbox_log and
# emit_hook_reply. A predicate: exit status only, never output.
blackbox_enabled() {
    [ "${WRIT_BLACKBOX:-}" = "1" ] || [ -f "${HOME:-}/.claude/writ-blackbox.on" ]
}

blackbox_log() {
    # Tested here as well as at every call site, so a future caller that forgets to test
    # still no-ops. Off => no-op that still drains stdin.
    if ! blackbox_enabled; then
        cat >/dev/null 2>&1; return 0
    fi
    local direction="${1:-?}" hook="${2:-?}" session="${3:-}" event="${4:-}" exit_code="${5:-}"
    local log="${WRIT_BLACKBOX_LOG:-$HOME/.claude/writ-blackbox.jsonl}"

    # An exit row's whole content is its code, so a code that is not a number makes the row
    # a claim with nothing behind it. Dropped rather than coerced or written as null: with
    # no third state there is nothing for a reader to interpret. Same validation idiom as
    # the size cap below, and it runs FIRST so an invalid row costs no `wc -c` either.
    if [ "$direction" = "exit" ]; then
        case "$exit_code" in
            ''|*[!0-9]*)
                cat >/dev/null 2>&1; return 0 ;;
        esac
    fi

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
    rec=$(WRIT_BB_DIR="$direction" WRIT_BB_HOOK="$hook" WRIT_BB_SID="$session" \
          WRIT_BB_EVENT="$event" WRIT_BB_EXIT="$exit_code" WRIT_BB_PID="$$" python3 -c '
import os, sys, json, datetime
try:
    _dir = os.environ.get("WRIT_BB_DIR", "?")
    _pid = os.environ.get("WRIT_BB_PID", "")
    rec = {"ts": datetime.datetime.now(datetime.timezone.utc).isoformat(),
           "hook": os.environ.get("WRIT_BB_HOOK", "?"),
           "direction": _dir,
           "session": os.environ.get("WRIT_BB_SID", ""),
           "pid": int(_pid) if _pid.isdigit() else os.getpid(),
           "event": os.environ.get("WRIT_BB_EVENT") or None,
           "payload": sys.stdin.read()}
    if _dir == "exit":
        rec["exit_code"] = int(os.environ.get("WRIT_BB_EXIT", ""))
    print(json.dumps(rec))
except Exception:
    pass
' 2>/dev/null) || true
    [ -n "$rec" ] && printf '%s\n' "$rec" >> "$log" 2>/dev/null || true
}

# THE ONE PLACE A HOOK REPLY REACHES STDOUT. Prints the hookSpecificOutput envelope, then
# records those same bytes. EMIT BEFORE LOG: capture must never sit between a hook and its
# answer, so a capture failure can cost a row but never the reply.
#
# Empty payload => nothing at all, no stdout and no row. Several emitting python arms exit
# without printing (writ-debug-code-gate's non-deny arm, the venv swap that rewrote
# nothing), and an empty stdout is the allow they mean.
#
# CAPTURE OFF costs one variable test, one [ -f ] and one printf builtin: no fork. The
# sentinel is tested HERE rather than left to blackbox_log, because `printf | blackbox_log`
# forks a subshell before the logger gets to decide it has nothing to do.
#
# The hook label resolves the OUTERMOST BASH_SOURCE frame, not BASH_SOURCE[1]: inside
# emit_deny the frame above this one is common.sh itself, so 20 refusals would file under
# the hook name "common". Basename by parameter expansion, and only when capture is on,
# because the basename binary is a 1.3ms fork against 0.018ms.
# Usage: emit_hook_reply "$ENVELOPE" [hook] [session]
emit_hook_reply() {
    local _payload="${1:-}"
    [ -n "$_payload" ] || return 0
    printf '%s\n' "$_payload"
    blackbox_enabled || return 0
    local _hook="${2:-}" _session="${3:-${HOOK_SESSION_ID:-}}"
    if [ -z "$_hook" ]; then
        local _src="${BASH_SOURCE[${#BASH_SOURCE[@]}-1]:-$0}"
        _hook="${_src##*/}"
        _hook="${_hook%.sh}"
    fi
    printf '%s\n' "$_payload" | blackbox_log out "$_hook" "$_session" "${HOOK_EVENT:-}"
}

# Convenience: extract a single SCALAR field (string/number) from parsed JSON.
# jq-first (~1-2ms) with a python3 fallback (~10ms cold start) when jq is absent or
# WRIT_NO_JQ is set (B2: cut per-hook spawn latency -- jq replaces the python3
# cold-start). Missing/null -> DEFAULT (3rd arg, default ""). For booleans use
# parsed_bool (jq prints true/false vs python True/False -- different strings).
# Usage: FILE=$(parsed_field "$PARSED" "file_path")            # default ""
#        BUDGET=$(parsed_field "$CACHE" "remaining_budget" "8000")
# Extract SEVERAL fields from ONE json document in a single pass.
#
# Usage: eval "$(parsed_fields "$json" VAR1=field1 VAR2=field2 ...)"
#
# WHY THIS EXISTS. parsed_field pipes the WHOLE document into a fresh jq for every field.
# The RAG hook called it 5 times on the /prompt-bundle response, which is ~10KB because it
# carries the full always-on rule text: 50KB of piping and 5 interpreter starts to read 5
# strings. Measured on the prompt path, those calls were ~38ms, second only to the HTTP
# request that fetched the data.
#
# SEMANTICS ARE parsed_field's, FIELD FOR FIELD, including a difference that already
# exists between its two arms: a JSON `false` renders as "false" under jq and "False"
# under python, because one uses tostring and the other str(). That divergence is
# pre-existing and its consumer in writ-rag-inject.sh tests for both spellings, so this
# reproduces it rather than quietly canonicalizing. Absent and null both give "".
#
# The output is @sh-quoted assignments, evaluated by the caller. That is the same
# eval-shell-assignments shape parse-hook-stdin.jq uses for the hook envelope, and the
# same safety argument: @sh quoting means a value cannot break out and become a command.
parsed_fields() {
    local json="$1"; shift
    local p var field
    if [ -z "${WRIT_NO_JQ:-}" ] && command -v jq >/dev/null 2>&1; then
        local prog="" sep=""
        for p in "$@"; do
            var="${p%%=*}"; field="${p#*=}"
            prog="${prog}${sep}\"${var}=\" + (if (.[\"${field}\"]) == null then \"\" else (.[\"${field}\"] | tostring) end | @sh)"
            sep=","
        done
        [ -n "$prog" ] || return 0
        printf '%s' "$json" | jq -r "$prog" 2>/dev/null || true
    else
        local names=""
        for p in "$@"; do
            names="${names}${p}"$'\n'
        done
        printf '%s' "$json" | PARSED_FIELDS_SPEC="$names" python3 -c '
import json, os, shlex, sys
try:
    d = json.load(sys.stdin)
except Exception:
    d = {}
if not isinstance(d, dict):
    d = {}
for line in os.environ.get("PARSED_FIELDS_SPEC", "").splitlines():
    if not line.strip():
        continue
    var, _, field = line.partition("=")
    v = d.get(field)
    # str(), matching parsed_field s python arm: a JSON false prints "False" here and
    # "false" under jq. Pre-existing, and the caller handles both.
    # No nested quotes in this expression on purpose: the whole program is inside a
    # single-quoted shell string, so an escaped quote here is a python SyntaxError and
    # the arm emits nothing at all, which reads as "every field was empty".
    val = "" if v is None else str(v)
    print(var + "=" + shlex.quote(val))
' 2>/dev/null || true
    fi
}

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
        # `|| true` is load-bearing, and its absence INVERTED this function's whole
        # contract. jq exits 5 on malformed input; every hook runs under
        # `set -euo pipefail`, so a bad payload aborted the calling hook at status 5
        # while the python arm below exits 0 and prints nothing. That makes the
        # PRESENCE of jq the thing that changes behavior, which is backwards. Measured
        # 2026-08-07 on `printf '{not json' | json_transform '.foo' "d.get('foo')"`:
        # the jq arm never reached the next line, the python arm did. Same defect class
        # as writ_session_mode_direct above; a bad payload is a normal input here.
        jq -c -r "( $filter ) | if . == null then empty else . end" 2>/dev/null || true
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
    # Reads the cache FILE, not the daemon. The A5 note this replaces called the curl
    # path a "~3ms fast path against a ~10ms python cold start"; measured 2026-08-07 on
    # this machine, a daemon round trip costs 13-17ms and a python start 13ms, so the
    # fast path was not faster. The file read via jq is ~2ms.
    #
    # It is also the SAME ANSWER, which is what makes this safe rather than merely
    # quick. GET /session/{id}/mode is `_read_cache(id).get("mode","") or ""`
    # (writ/server/routes/session_state.py:86), so the daemon reads this exact file; a
    # missing file yields _default_cache()["mode"] = None, which renders "" on both
    # sides; and _write_cache fsyncs a `.tmp` file then os.rename()s it into place
    # (writ/session/cache.py:291), so a reader can never observe a partial write.
    #
    # Strictly more robust, too: this answers correctly while the daemon is down or
    # saturated. writ_session_mode_direct exists because a loaded daemon can return a
    # spurious empty mode, and acting on a false empty would silently un-gate the 7
    # enforcement hooks that call this.
    local sid="$1"
    [ "$(writ_session_mode_direct "$sid")" = "work" ]
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
# Pure bash: no processes at all. This ran twice per file write and again on the
# per-prompt path, paying ~13ms of python interpreter startup each time to stat a few
# files (measured 2026-08-07). The walk itself is `[ -e ]` and a suffix strip.
#
# NORMALIZATION IS LEXICAL, ON PURPOSE. The python it replaces used os.path.abspath,
# which collapses `.`, `..` and repeated slashes but does NOT resolve symlinks.
# `realpath` DOES resolve them, so the obvious one-line rewrite would walk the link
# TARGET's parents and can return a different project root than this function has always
# returned. tests/test_project_root_bash_parity.py builds two marked projects around a
# symlink specifically to hold that behavior in place.
detect_project_root() {
  local start_path="$1" path seg
  case "$start_path" in
    /*) path="$start_path" ;;
    *)  path="$PWD/$start_path" ;;
  esac

  # Split on / and rebuild, dropping empty and "." segments and popping one level per
  # ".."; an unmatched ".." at the top is discarded, as abspath does.
  #
  # SPLIT WITH PARAMETER EXPANSION, NOT `read`. The first version used
  # `IFS='/' read -r -a raw <<< "$path"`, and `read` consumes only the FIRST LINE of the
  # here-string: a directory whose name contains a newline (legal on Linux) lost every
  # segment after it and the walk returned "". Callers gate on an empty PROJECT_ROOT and
  # skip their checks, so that was a silent enforcement hole rather than a cosmetic bug.
  # `${rest%%/*}` is byte-exact and has no notion of lines.
  local -a parts=()
  local rest="$path"
  while [ -n "$rest" ]; do
    seg="${rest%%/*}"
    if [ "$seg" = "$rest" ]; then
      rest=""
    else
      rest="${rest#*/}"
    fi
    case "$seg" in
      ''|.) ;;
      ..) [ ${#parts[@]} -gt 0 ] && parts=("${parts[@]:0:${#parts[@]}-1}") ;;
      *)  parts+=("$seg") ;;
    esac
  done
  path=""
  for seg in "${parts[@]:-}"; do
    [ -n "$seg" ] && path="$path/$seg"
  done
  [ -z "$path" ] && path="/"

  # Markers are checked at `path` BEFORE walking up, so a caller passing the project
  # root itself resolves to it rather than to its parent (E-PROJROOT-BUG).
  while [ "$path" != "/" ]; do
    if [ -e "$path/composer.json" ] || [ -e "$path/package.json" ] \
       || [ -e "$path/Cargo.toml" ] || [ -e "$path/go.mod" ] \
       || [ -e "$path/pyproject.toml" ] || [ -e "$path/.git" ]; then
      printf '%s\n' "$path"
      return 0
    fi
    path="${path%/*}"
    [ -z "$path" ] && path="/"
  done
  printf '\n'
}

# ── Session-scoped gate artifact directory ───────────────────────────────────
# Prints <project_root>/.claude/gates/<session_id>, or an EMPTY LINE when there is no
# valid path (no project root, or a session id that is not [A-Za-z0-9._-]{1,128} or is
# "." / ".."). Always returns 0: an empty answer is a legitimate result meaning "this
# session has no gate directory, so it has approved nothing", and a non-zero return would
# abort every caller under `set -euo pipefail` -- failing OPEN on a gate check, which is
# the wrong direction for a human-oversight boundary.
# Usage: GATE_DIR=$(writ_gate_dir "$PROJECT_ROOT" "$SESSION_ID")
#
# THE BYTE-IDENTICAL PYTHON MIRROR IS writ/session/locators.py `gate_dir`. Two
# implementations of one path is a seam this repo has been bitten by before (the two
# `## Files` parsers; gate_token_path matching the bash writer byte for byte), so
# tests/test_gate_dir_bash_python_parity.py runs both sides over the same inputs,
# including a rejected id, and compares bytes. CHANGE BOTH OR NEITHER.
#
# PURE SHELL, NO SPAWN, deliberately: writ-rag-inject.sh builds this on the per-prompt
# path, where a python interpreter start costs ~19.5ms and `grep` a fork. The charset
# check is a `case` glob for that reason, and tests/test_prompt_path_process_budget.py's
# python-start ratchet must not move because of this function.
#
# LC_ALL=C IS LOAD-BEARING. Bracket ranges in a glob are COLLATION-ordered outside the C
# locale, so `[A-Za-z]` under en_US.UTF-8 can admit accented letters that the python
# mirror's ASCII regex rejects -- a silent parity break on exactly the inputs the
# validation exists to refuse. It is `local`, so the locale is restored on return.
writ_gate_dir() {
  local root="${1:-}" sid="${2:-}"
  local LC_ALL=C
  if [ -z "$root" ] || [ -z "$sid" ] || [ ${#sid} -gt 128 ]; then
    printf '\n'
    return 0
  fi
  # "." and ".." pass the charset but name a directory that is not a session; a separator
  # or any other character outside the class is refused so the id can never traverse.
  case "$sid" in
    .|..|*[!A-Za-z0-9._-]*)
      printf '\n'
      return 0
      ;;
  esac
  # Strip trailing separators so "/srv/app/" and "/srv/app" produce the same bytes as the
  # python mirror's rstrip. A root of "/" strips to "" and rejoins as "/.claude/...".
  while [ -n "$root" ] && [ "${root%/}" != "$root" ]; do
    root="${root%/}"
  done
  printf '%s\n' "$root/.claude/gates/$sid"
}

# ── Session ID detection ────────────────────────────────────────────────────
# Extracts the session id from a parsed hook envelope, and NOTHING ELSE.
#
# NO FALLBACK, for the reasons spelled out on load_hook_env and writ_require_session:
# `ps -o ppid=` and md5(cwd:user)-date both produce an id Claude Code has never heard
# of, so state written under it is written to a session that does not exist. Both used
# to live here, which is how three hooks that had "already been fixed" kept synthesizing.
#
# PRINTS THE ID OR AN EMPTY LINE, and always returns 0. An empty answer is a legitimate
# result, not an error: plenty of callers do work that needs no session at all, and a
# non-zero return here would abort them under `set -euo pipefail`. A caller that
# REQUIRES a session checks for empty, calls writ_critical, and no-ops.
# Usage: SESSION_ID=$(detect_session_id "$PARSED")
#   where PARSED is the output of parse_hook_stdin. Called with no argument (or with an
#   envelope carrying no id) it prints nothing.
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
#
# THE REASON RIDES STDIN, NOT AN ENVIRONMENT STRING, and that is a size decision rather
# than a style one (docs/adr/ADR-hook-exec-argument-boundary.md, property 2). MAX_ARG_STRLEN
# caps each ENV string exactly as it caps each argument, and the Bash gate's reasons embed
# extractor row values (a credential path, an unresolved spelling, an egress host), which
# `flat()` collapses but does NOT truncate, so a reason is as long as the command token it
# quotes. Those reasons were unreachable at size only while the extractor died first; the
# command-file transport in writ-bash-write-gate.sh makes them reachable, and an oversized
# env string here would move the silence one layer down: execve fails, `_reply` falls back to
# "", and emit_hook_reply returns 0 on an empty payload, so the DENY disappears exactly as
# the extractor's verdict used to. Stdin has no per-string cap. The process count is
# unchanged (one python either way) and no call site changes.
#
# THE PROGRAM MOVES OFF THE HEREDOC FOR THE SAME REASON: a quoted heredoc IS stdin, so a
# payload cannot use it. It is a SINGLE-quoted `-c` program instead, which needs the JSON
# keys spelled with double quotes and must contain no apostrophe, so nothing in it can be
# interpolated by bash (no `$`, no backtick, no quote to close). The two Bash-side gates
# cannot take this route (their programs are ~1,900 and ~600 lines), which is why they
# pass a FILE PATH and this one passes the value.
#
# surrogateescape on the way in mirrors how os.environ already decoded this value, so a
# reason quoting a command token that is not valid UTF-8 renders the same as before rather
# than raising. json.dumps then escapes it (ensure_ascii), so stdout stays pure ASCII.
# Usage: [ -n "$DENY" ] && emit_deny "$DENY"
emit_deny() {
  local _reply
  _reply=$(printf '%s' "$1" | python3 -c 'import json, sys
print(json.dumps({
    "hookSpecificOutput": {
        "hookEventName": "PreToolUse",
        "permissionDecision": "deny",
        "permissionDecisionReason": sys.stdin.buffer.read().decode("utf-8", "surrogateescape")
    }
}))') || _reply=""
  emit_hook_reply "$_reply"
}

# Emit a PreToolUse "ask" decision (Claude Code hookSpecificOutput contract): the
# tool is neither allowed nor denied, the USER confirms it. Single source for the ask
# envelope, the twin of emit_deny above. The reason rides STDIN into python, which does
# the JSON encoding, so newlines and quotes inside a reason (the egress guard lists one
# destination per line) cannot corrupt or forge the envelope (SEC-INJ-LOG-001), and no
# per-string exec cap can silently empty it. See emit_deny above for the whole argument;
# it applies here unchanged, and one step harder: this is the envelope a DECIDER FAULT
# reports itself with (writ_decider_fault below), so an ask that goes silent at size
# would hide the very failure it exists to announce.
# Usage: [ -n "$ASK" ] && emit_ask "$ASK"
emit_ask() {
  local _reply
  _reply=$(printf '%s' "$1" | python3 -c 'import json, sys
print(json.dumps({
    "hookSpecificOutput": {
        "hookEventName": "PreToolUse",
        "permissionDecision": "ask",
        "permissionDecisionReason": sys.stdin.buffer.read().decode("utf-8", "surrogateescape")
    }
}))') || _reply=""
  emit_hook_reply "$_reply"
}

# THE COMPLETION SENTINEL the two Bash-side decision blocks print as their LAST line, and
# the text their consumers compare against. Held here because both gates
# (writ-bash-write-gate.sh, writ-worktree-safety.sh) read it and neither should own it.
#
# It exists because "the decision block ran and found nothing" and "the decision block did
# not run" both used to arrive as an empty string, and the consumer answered both with a
# silent allow. This row makes the first state say so out loud, which leaves the second one
# nameable (ADR property 3: the outcome is observed, never inferred).
#
# THE PYTHON SIDE SPELLS THE SAME TEXT AS A LITERAL (`STATUS_COMPLETE`) rather than reading
# it from here, and the reason is structural: both blocks are QUOTED heredocs, so nothing
# bash-side interpolates into them, and one of the two is additionally run STANDALONE by
# seven test harnesses. Adding a third environment channel to carry a five-word constant
# would cost more than the second literal does, because the drift is deliberately NOT
# silent: a mismatch makes every command look like a fault, so the whole Bash surface
# starts asking on the first run rather than quietly loosening.
WRIT_EXTRACTOR_SENTINEL=$'status\tcomplete'

# A DECIDER THAT DID NOT RUN TO COMPLETION. The one shared answer for all three write
# doors (writ-bash-write-gate.sh, writ-worktree-safety.sh, writ-pre-write-dispatch.sh):
# their decision block crossed an exec boundary, that exec failed, and the empty value
# it left behind used to read as "nothing to gate".
#
# Usage: writ_decider_fault <hook> <stage> <what could not be decided>
#   <hook> and <stage> are SOURCE LITERALS at every call site, never payload-derived.
#   The third argument reaches the USER, never the log row.
#
# THE POSTURE, and it is two postures because the two states are not the same claim:
#
#   python3 PRESENT, block incomplete -> ASK. This is a fault and it should be loud. The
#     Bash gate already asks in exactly this epistemic state (an unresolvable target, an
#     unnameable destination): "this cannot be trusted" is answered by the prompt, not by
#     silence. An ask costs one confirmation and cannot be self-approved by the agent.
#   python3 ABSENT -> ALLOW, exit 0, and say so once on stderr. Asking here would be
#     worse than dishonest: on such a machine EVERY Writ decision path is inert
#     (writ_critical's row, log_friction_event, log_gate_decision, emit_deny and emit_ask
#     are all interpreter-bound), so an ask would imply a protection that does not exist
#     while making every write-shaped command unusable. Stderr is the only surface that
#     survives, and it is used. Without this probe the ask path itself goes silent:
#     emit_ask cannot build its envelope and emit_hook_reply returns 0 on an empty
#     payload, which is the same defect one layer down.
#
# `command -v` is a BUILTIN: the probe forks nothing, and it runs only on the fault path,
# so the hot path pays for none of this.
#
# NON-BLOCKING IS NOT SILENT (user directive 2026-08-01, docs/reference/session-and-gates.md
# section 8): the compensating control is visibility. Every fault writes TWO records. One
# `[WRIT CRITICAL]` line on stderr, which Claude Code surfaces in the session and which
# needs no interpreter, and one `gate_decider_incomplete` row on the audit stream carrying
# `hook` and `stage` ONLY, both source literals, because a row built from the value that
# killed the block would die exactly where the block died.
writ_decider_fault() {
  local _hook="${1:-unknown}" _stage="${2:-unknown}" _what="${3:-}"
  if ! command -v python3 >/dev/null 2>&1; then
    # No interpreter: no row can be written and no envelope can be built. One line, and
    # it names the scope honestly: not this hook, the whole enforcement surface.
    printf '[WRIT CRITICAL] %s: no python3 on PATH, so NO Writ Bash or write decision can be made on this machine. This command was allowed unchecked (%s).\n' \
      "$_hook" "$_stage" >&2
    return 0
  fi
  writ_critical "$_hook" \
    "the $_stage decision block did not run to completion, so this tool call was not judged; asking the user instead of allowing it unseen" \
    "${SESSION_ID:-${HOOK_SESSION_ID:-unknown}}"
  log_friction_event "${SESSION_ID:-${HOOK_SESSION_ID:-}}" "${MODE:-}" "gate_decider_incomplete" \
    "{\"hook\": \"$_hook\", \"stage\": \"$_stage\"}"
  emit_ask "[ENF-DECIDER-INCOMPLETE] Writ could not judge this tool call: ${_what:-the decision block did not run to completion}. That is an infrastructure fault in Writ, not a finding about this command, so nothing is claimed about it either way. Confirm only if you already know it is safe. Writ's own record of the fault is the gate_decider_incomplete row on the audit stream ($_hook / $_stage)."
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

# Record a CRITICAL condition: an invariant this code depends on is false, and the
# operation was abandoned rather than guessed at.
#
# Writ had no way to say this before. Hooks that could not identify their session fell back
# to a global pointer file or to an id synthesized from PID and cwd, so acting on the WRONG
# session was indistinguishable from a normal run. One such fallback stranded 999 telemetry
# rows for a day before anyone noticed, and the same shape sat in the gate hooks, where the
# consequence is an approval recorded against another session.
#
# STDERR FIRST, on purpose: the log write can itself fail, and this is the class of event
# that must not depend on the logging path being healthy. Claude Code surfaces hook stderr,
# so the operator sees it in the session rather than only in a file nobody greps.
writ_critical() {
  local component="${1:-unknown}" message="${2:-}" session="${3:-unknown}"
  printf '[WRIT CRITICAL] %s: %s\n' "$component" "$message" >&2
  local extra
  if [ -z "${WRIT_NO_JQ:-}" ] && command -v jq >/dev/null 2>&1; then
    extra=$(jq -n -c --arg component "$component" --arg message "$message" \
      '{component: $component, message: $message, severity: "critical"}' 2>/dev/null) || extra=""
  fi
  # No jq: build it with the same python that is about to be spawned anyway, so the
  # message cannot break the JSON regardless of what it contains.
  if [ -z "$extra" ]; then
    extra=$(WRIT_C="$component" WRIT_M="$message" python3 -c "
import json, os
print(json.dumps({'component': os.environ.get('WRIT_C',''),
                  'message': os.environ.get('WRIT_M',''),
                  'severity': 'critical'}))" 2>/dev/null) || extra='{"severity":"critical"}'
  fi
  python3 "$_FRICTION_APPEND" --stream errors "$session" "" "critical_error" "$extra" \
    2>/dev/null || true
  return 0
}

# The session id for THIS hook invocation, from the payload and nowhere else.
#
# NO FALLBACK BY DESIGN. Claude Code documents session_id as universal and authoritative on
# every hook event (docs/reference/claude-code-blackbox.md), so its absence is not a normal
# condition to paper over. The two things this replaces both produced silently wrong
# answers: /tmp/writ-current-session is ONE file shared by every Claude Code session on the
# machine, so it names whichever session took a turn most recently; and an id synthesized
# from PPID or md5(cwd:user) never matches the real session, so state written under it is
# simply lost.
#
# Prints the id and returns 0, or records a critical error and returns 1. Callers must
# check the status: `SESSION_ID=$(writ_require_session "$STDIN_JSON" my-hook) || exit 0`.
writ_require_session() {
  local stdin_json="${1:-}" hook="${2:-unknown}" sid
  # agent_id first, matching load_hook_env: inside a sub-agent that is the identity whose
  # state must not be written under the parent's key.
  # THE TWO ARMS MUST AGREE ON AN EMPTY STRING, and the obvious jq spelling does not.
  # `.agent_id // .session_id` falls through only on null and false, so a payload carrying
  # `"agent_id": ""` KEEPS the empty string under jq and this function refuses the whole
  # hook, while the python arm's `or` falls through to session_id and proceeds. Measured on
  # {"agent_id":"","session_id":"real-sess"}: rc=1 with jq, rc=0 with WRIT_NO_JQ=1. That
  # inverts the seam's contract -- the presence of jq is supposed to change speed, never
  # behavior -- and it does so on the identity three governance hooks read
  # (validate-exit-plan, enforce-violations, friction-logger). Same defect the `pyor()`
  # helper in bin/lib/parse-hook-stdin.jq exists to prevent; this is a single inline filter
  # rather than that program, so the falsy skip is spelled out here instead.
  # PYTHON IS THE CORRECT SIDE: an empty agent_id means "not a sub-agent", which is a
  # session_id case, not a refusal.
  sid=$(printf '%s' "$stdin_json" | json_transform \
    '[.agent_id, .session_id] | map(select(. != null and . != "" and . != false)) | first // empty' \
    "(d.get('agent_id') or d.get('session_id'))" 2>/dev/null || true)
  sid=$(printf '%s' "$sid" | tr -d '[:space:]')
  if [ -z "$sid" ]; then
    writ_critical "$hook" "no session_id in hook payload; refusing to act on a guessed session"
    return 1
  fi
  printf '%s' "$sid"
  return 0
}

log_friction_event() {
  # `"${4:-"{}"}"` is required: `${4:-{}}` parses the second `}` as the
  # closing brace of the parameter expansion, leaving a stray `}` appended
  # to the value. Quoting the default makes `{}` literal.
  local session_id="$1" mode="$2" event="$3" extra="${4:-"{}"}"
  python3 "$_FRICTION_APPEND" "$session_id" "$mode" "$event" "$extra" 2>/dev/null || true
}

# ── Gate token writer ───────────────────────────────────────────────────────
# Writes the five-line gate-token file: the secret, the gate the approval
# authorizes, and the plan fingerprint it was given for. Lives here beside
# log_friction_event and writ_http_post because it is the third primitive
# auto-approve-gate.sh shares with the rest of the surface.
#
# BYTE-IDENTICAL TO writ.session.gate_token.mint_gate_token FOR THE SAME INPUTS, and
# tests/test_gate_token_binding.py holds the two writers against each other. The reader
# is python and the production writer is bash, so a one-character disagreement about the
# format is a gate that fail-closes on every approval; that is the same class of defect
# as a writer/reader disagreement about the PATH, which is why gate_token_path hardcodes
# /tmp.
#
# An empty gate and an empty fingerprint are legitimate values, not missing ones: they
# are what an approval typed with no phase gate pending is bound to, and the claim
# enforces them as "must be exactly empty".
# LINE 4 is the candidate a promotion is bound to, empty for every other approval. It
# exists because the promotion route used to take the candidate from the request body, so
# one approval authorized promoting whichever candidate the caller named.
# LINE 5 is the RULE a promotion approval authorizes, empty for every other approval, and
# it is a separate line rather than a namespaced reuse of line 4 because a graduation
# candidate id and a Rule id are different objects: one field holding either would leave
# the next reader unable to tell WHICH object a token authorizes. An omitted fifth
# argument writes an empty line 5, which is what a phase advance compares against and what
# a rule promotion is refused for.
# Usage: write_gate_token_file <path> <token> <gate> <plan_hash> [candidate_id] [rule_id]
write_gate_token_file() {
  local path="$1" secret="$2" gate="${3:-}" plan_hash="${4:-}" candidate="${5:-}" rule="${6:-}"
  printf '%s\n%s\n%s\n%s\n%s\n' "$secret" "$gate" "$plan_hash" "$candidate" "$rule" > "$path"
  # The file holds a secret in a world-readable directory; the python writer chmods too.
  chmod 600 "$path" 2>/dev/null || true
}

# ── Hook timing ─────────────────────────────────────────────────────────────
# Records start time. Call at the beginning of a hook.
# Usage: HOOK_START_NS=$(hook_timer_start)
# Nanoseconds since the epoch, with NO process on the normal path.
#
# `date +%s%N` is 1 fork+exec, and it ran twice per instrumented hook (start and exit
# trap): 20 of the 24 date processes on a file write. bash 5 exposes EPOCHREALTIME as a
# variable, so the common case costs nothing.
#
# LOCALE: EPOCHREALTIME's decimal separator follows LC_NUMERIC and IS a comma in some
# locales, so the split matches either character rather than assuming a dot. Getting this
# wrong would not error, it would silently produce a garbage duration.
# The microsecond field is always 6 digits, hence the fixed "000" to reach nanoseconds.
_writ_now_ns() {
  local t="${EPOCHREALTIME:-}"
  case "$t" in
    *[.,]*) printf '%s%s000\n' "${t%%[.,]*}" "${t#*[.,]}" ;;
    ?*)     printf '%s000000000\n' "$t" ;;
    # bash 4 or a stripped environment: the old path, still correct, just slower.
    *)      date +%s%N 2>/dev/null || python3 -c "import time; print(int(time.time()*1e9))" ;;
  esac
}

hook_timer_start() {
  _writ_now_ns
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
  # NO PER-HOOK TEMP FILE. An earlier design gave each hook its own mktemp scratch
  # buffer for log_gate_decision to append to. That buffer became dead when both row
  # kinds moved to the session-scoped buffer (writ_event_buffer_path), but the mktemp
  # call outlived its reader and kept spawning a process on EVERY instrumented hook:
  # ~15 wasted execve per file write, in the cycle whose entire purpose was removing
  # exactly those. Found by strace, not by reading, because the variable was still
  # assigned and still cleaned up and so looked alive.
  _WRIT_EXIT_HANDLERS=()
  trap '_writ_hook_exit_trap' EXIT
  # A signal-killed hook used to record exit_code 0: the shell runs the EXIT trap with
  # `$?` still 0 because no command set it. Converting the signal into an explicit exit
  # gives the trap the real status (128 + signal, the shell's own convention) and keeps
  # ONE trap function rather than adding a parallel signal path. The process's reported
  # status was always correct, so enforcement never depended on this; the RECORD did.
  trap 'exit 129' HUP
  trap 'exit 130' INT
  trap 'exit 143' TERM
}

# Register a function to run at hook exit. USE THIS INSTEAD OF `trap ... EXIT`.
#
# bash allows exactly ONE EXIT trap, so a hook that installs its own after
# hook_instrument REPLACES the telemetry trap and silently stops recording. Two hooks
# did: pre-validate-file.sh and inject-tier-workflow.sh. The gap was invisible because
# nothing fails when a metrics row is never written.
# tests/test_exit_trap_ownership.py fails on any hook that takes the trap directly, so
# the next one cannot reintroduce this quietly.
# NOTE FOR HANDLER AUTHORS: handlers run with errexit OFF (see _writ_hook_exit_trap),
# because a trap that dies partway would skip the telemetry behind it. A multi-statement
# handler therefore does NOT stop at its own first failing command the way the rest of a
# hook does. If a step must gate the next one, say so explicitly with `||` or an `if`.
writ_on_exit() {
  _WRIT_EXIT_HANDLERS+=("$1")
}

_writ_hook_exit_trap() {
  local rc=$?
  # errexit OFF for the rest of this function. The trap is the last thing that runs and
  # must not die partway: `( exit "$rc" )` below deliberately returns non-zero, and under
  # `set -euo pipefail` (which every hook sets) that ABORTED THE TRAP, skipping telemetry
  # on exactly the non-zero exits that matter most. Measured while building this: with
  # errexit left on, `exit 2` and a failed command ran neither the hook's own handler nor
  # the telemetry, while the exit status still looked correct.
  set +e

  # THE ROW GOES IN BEFORE THE HANDLERS RUN, and the order is the whole point. Three
  # hooks drain the buffer as their exit work (friction-logger, writ-subagent-stop,
  # writ-session-end). With the append last, each drained the buffer and then re-created
  # it with its own row: measured by seeding one row and running friction-logger, which
  # left a file holding `hook_execution|friction-logger|73|0||0` behind. Every turn
  # stranded a one-row orphan, and a session's final row waited on a turn that might never
  # come. Appending first means a drain handler flushes the drainer too.
  #
  # THE COST, so nobody has to rediscover it: dur_ms now covers the hook's own body and
  # NOT its registered exit work. For the 37 hooks that register nothing this is identical;
  # for the three drainers the row under-reports by the flush. The alternative is a row
  # that measures the flush and cannot be inside it.
  local start_ns="${_WRIT_HOOK_START_NS:-0}" now_ns dur_ms
  now_ns=$(_writ_now_ns 2>/dev/null || echo 0)
  if [ "${start_ns:-0}" -gt 0 ] 2>/dev/null && [ "$now_ns" -gt 0 ] 2>/dev/null; then
    dur_ms=$(( (now_ns - start_ns) / 1000000 ))
  else
    dur_ms=0
  fi
  [ "$dur_ms" -lt 0 ] 2>/dev/null && dur_ms=0

  # The plain hook_execution row is APPENDED, not emitted: bash costs 0.018ms and no
  # process, against ~13ms of interpreter startup, and 8 write-path hooks pay this on
  # every file write. writ_event_buffer_flush drains it once per turn.
  # Mode from the same resolution log_gate_decision uses, but CACHED ONLY: this trap runs
  # on every instrumented hook, so a fresh lookup here would be a process on each of them.
  _writ_row_mode_cached
  writ_event_buffer_append \
    "${SESSION_ID:-${HOOK_SESSION_ID:-}}" \
    "${_WRIT_HOOK_NAME:-unknown}" \
    "$dur_ms" "$rc" "$_WRIT_ROW_MODE"

  # NOTHING SPAWNS HERE ANY MORE. Both row kinds (hook_execution above,
  # gate_decision via log_gate_decision) are appended to the session buffer and emitted
  # by one drain per turn. The python block that used to live here ran on every write
  # that recorded a decision, and with its git children it measured 203ms per write.

  # The hook's own exit handlers, in registration order, each seeing the hook's REAL
  # exit status in $?. `( exit "$rc" )` is a subshell whose only job is to set $? for
  # the command that follows. These run AFTER the row above so a handler that drains the
  # buffer includes it.
  local _h
  for _h in "${_WRIT_EXIT_HANDLERS[@]:-}"; do
    [ -n "$_h" ] || continue
    ( exit "$rc" )
    "$_h"
  done

  # THE REFUSAL EXIT CODE, captured structurally. This trap is already the owner of every
  # instrumented hook's exit path, so the eleventh non-zero exit site needs no registration:
  # the alternative, an enumerated list, is measured in tests/firedrill/_census.py, which
  # declares 7 of the 10 real sites.
  #
  # LAST, after the hook_execution append and after the registered handlers. Capture is a
  # debug switch, and nothing with retention may be delayed or skipped by it. `exit "$rc"`
  # below uses the saved status, so a failure here cannot change the hook's outcome.
  #
  # NON-ZERO ONLY. An unconditional row would add a python spawn to all 40 instrumented
  # hooks whenever capture is on, and nothing is lost: the denominator (every hook's exit
  # code, including 0) is the hook_execution row appended above.
  #
  # `[ "$rc" -ne 0 ]` FIRST because it is a builtin and the cheapest discriminator, then
  # blackbox_enabled, which is one variable test and one `[ -f ]`. Neither forks, so capture
  # off adds no process.
  #
  # `</dev/null`, NOT `printf '' |`: blackbox_log reads stdin, and a pipe forks a subshell
  # before the logger can decide it has nothing to do. The redirect costs no process and
  # gives the encoder the empty payload an exit row should carry.
  #
  # THE SESSION EXPRESSION IS REVERSED from the telemetry row's above, deliberately. The
  # other side of the join is the IN row, which load_hook_env writes with
  # ${HOOK_SESSION_ID:-}; preferring SESSION_ID here would key differently in every hook
  # where the two differ and the join this row exists to feed could not fire.
  if [ "$rc" -ne 0 ] && blackbox_enabled; then
    blackbox_log exit "${_WRIT_HOOK_NAME:-unknown}" \
      "${HOOK_SESSION_ID:-${SESSION_ID:-}}" "${HOOK_EVENT:-}" "$rc" </dev/null || true
  fi

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
  # Append to the SESSION buffer that hook_execution uses, so the once-per-turn drain
  # emits both kinds and no hook spawns python to log a decision. Separators are stripped
  # from the values first: one inside a reason or a path would forge a second record,
  # which is the whole point of SEC-INJ-LOG-001.
  local _gd_gate="${1:-}" _gd_dec="${2:-}" _gd_reason="${3:-}" _gd_target="${4:-}"
  _gd_gate="${_gd_gate//[$'\x1e\x1f\n\r']/_}"
  _gd_dec="${_gd_dec//[$'\x1e\x1f\n\r']/_}"
  _gd_reason="${_gd_reason//[$'\x1e\x1f\n\r']/ }"
  _gd_target="${_gd_target//[$'\x1e\x1f\n\r']/_}"
  # NOT the bare variable: thirteen of the fourteen hooks that reach this line never set
  # it, and their rows recorded a null mode for a session that had one. _writ_row_mode
  # keeps an explicit CURRENT_MODE/MODE winning and falls back to the session cache.
  _writ_row_mode
  local _gd_mode="$_WRIT_ROW_MODE"
  _gd_mode="${_gd_mode//[$'\x1e\x1f\n\r']/_}"

  # A DENIAL IS WRITTEN SYNCHRONOUSLY, always. It is the record that proves a gate
  # blocked something, and a buffered row whose session never sees another Stop or
  # SessionEnd is lost rather than delayed. Denials are rare, so this spawn is not on the
  # hot path; the per-write cost came from the "allow" that every successful write logs.
  # An allow is buffered: it is the volume, and its claim ("the gate ran") is still
  # carried by the hook_execution row beside it.
  # ANYTHING THAT IS NOT EXACTLY "allow" takes the synchronous path, not just "deny".
  # Matching on "deny" would send a decision value nobody anticipated (a new gate verb, a
  # capitalised variant, a typo) down the lossy branch SILENTLY, which is the wrong
  # default for a governance record. Only the one high-volume value whose loss is
  # tolerable is buffered, and it is named explicitly.
  if [ "$_gd_dec" != "allow" ]; then
    _gd_emit_now "$_gd_gate" "$_gd_dec" "$_gd_reason" "$_gd_target"
    return 0
  fi

  # Decision time, stamped here rather than at drain time. emit() timestamps when it
  # runs, so a buffered row used to land with the DRAIN's clock: measured, a decision at
  # 22:48:41 was recorded as 22:48:44, and several decisions in one turn all collapsed
  # onto the same wrong instant. printf '%(%s)T' is a bash builtin, so this costs nothing.
  local _gd_at
  printf -v _gd_at '%(%s)T' -1
  local _gd_row
  printf -v _gd_row 'gate_decision\x1f%s\x1f%s\x1f%s\x1f%s\x1f%s\x1f%s\x1e' \
    "$_gd_gate" "$_gd_dec" "$_gd_reason" "$_gd_target" "$_gd_mode" "$_gd_at"
  # A row that fits the atomic-append bound goes in the buffer. One that does not falls
  # through to the immediate emit below rather than being truncated: an audit reason is
  # the text a human needs, and silently shortening it is worse than paying for a spawn
  # on the rare long denial.
  if [ "${#_gd_row}" -le "${WRIT_EVENT_ROW_MAX:-3072}" ]; then
    local _gd_buf _gd_dir
    _gd_buf="$(writ_event_buffer_path "${SESSION_ID:-${HOOK_SESSION_ID:-}}")"
    # GUARDED, BUT NOT RETRIED, and the difference is deliberate. The guard is the same
    # as _writ_buffer_append's: no `mkdir` process while the directory exists. The retry
    # is NOT needed here because this path already has a durability fallback -- a failed
    # buffered append falls through to _gd_emit_now below, which writes the audit record
    # synchronously. Retrying the buffer instead would take the row off the durable path
    # to save a spawn on the one branch where the spawn is warranted.
    _gd_dir="${_gd_buf%/*}"
    [ -d "$_gd_dir" ] || mkdir -p "$_gd_dir" 2>/dev/null || true
    if printf '%s' "$_gd_row" >> "$_gd_buf" 2>/dev/null; then
      return 0
    fi
  fi
  # Fallback for a hook that calls this without hook_instrument (no buffer), and the
  # path every deny takes: emit directly so the decision is recorded before the hook
  # returns control.
  _gd_emit_now "${1:-}" "${2:-}" "${3:-}" "${4:-}"
}

# The bound on the two UNBOUNDED fields of an audit row, and the enforcer the
# exec-argument ADR asks to be NAMED: a `${var:0:N}` substring expansion, applied below.
# 4,000 CHARACTERS, which is at most 16 KB in the worst multibyte case, against the
# 131,072-byte MAX_ARG_STRLEN cap on a single env string.
#
# WHY TRUNCATION HERE AND TRANSPORT IN emit_deny, which are opposite answers to the same
# limit: emit_deny's value is what the USER reads, so shortening it would shorten the
# explanation of a refusal, and stdin costs nothing. This row is EVIDENCE, and it crosses
# as two env strings with `|| true` behind it, so an oversized reason would silently drop
# the AUDIT ROW for a denial while the denial itself still reached the model: the record
# and the refusal would disagree. A reason long enough to hit this bound is already
# quoting a command token nobody will read in full; losing the row entirely is the only
# outcome that cannot be recovered later.
WRIT_GD_FIELD_MAX=4000

_gd_emit_now() {
  # Same resolution as the buffered path above, and it matters more here: this is the
  # branch every DENIAL takes, the record that proves a gate blocked something.
  _writ_row_mode
  # reason/target are the only two fields carrying tool input (a denial's text, a file
  # path), so they are the only two that can reach the cap; gate, decision, session and
  # mode are all short by construction. Truncated, not dropped: see WRIT_GD_FIELD_MAX.
  #
  # BOUND THROUGH A LOCAL, not `${3:0:N}` directly: substring expansion has no `:-`
  # default, so under `set -u` (which every hook sets) it ABORTS on a caller that passed
  # fewer than four arguments, where the old `${3:-}` degraded to the empty string.
  local _gd_now_reason="${3:-}" _gd_now_target="${4:-}"
  WRIT_GD_GATE="${1:-}" \
  WRIT_GD_DECISION="${2:-}" \
  WRIT_GD_REASON="${_gd_now_reason:0:${WRIT_GD_FIELD_MAX}}" \
  WRIT_GD_TARGET="${_gd_now_target:0:${WRIT_GD_FIELD_MAX}}" \
  WRIT_GD_SESSION="${SESSION_ID:-${HOOK_SESSION_ID:-}}" \
  WRIT_GD_MODE="$_WRIT_ROW_MODE" \
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

# Transport for every daemon call in this file. When the daemon's unix socket exists we
# go through it; otherwise this is empty and the call takes the TCP port exactly as
# before. curl ignores the URL's host when --unix-socket is given, so the URLs above are
# unchanged and each call site needs only this one variable.
#
# DELIBERATELY UNQUOTED at the call sites. Empty expands to nothing, and non-empty must
# split into the two words `--unix-socket` and the path; quoting it would pass one
# argument containing a space and curl would reject it. It is set here unconditionally so
# `set -u` cannot trip on it.
# Unquoted on purpose: an assignment never word-splits, and only an unquoted default gets
# tilde expansion, which (unlike $HOME) still resolves when HOME is unset under set -u.
WRIT_SESSION_SOCKET=${WRIT_SOCKET:-~/.cache/writ/run/writ.sock}
# AN EXPLICIT HOST OR PORT OVERRIDE WINS, and that is not a nicety. Callers that set
# WRIT_HOST or WRIT_PORT are naming the endpoint they want -- a fake daemon in a test,
# a second instance, a probe. curl ignores the URL's host when --unix-socket is given,
# so leaving the socket on would silently redirect those calls to the real daemon.
# Measured: 6 tests across 3 modules did exactly that before this guard existed.
if [ -n "${WRIT_SOCKET:-}" ] && [ -S "$WRIT_SESSION_SOCKET" ]; then
    # BOTH set means "use this socket, with that port as the fallback". Without this
    # arm the suite could not reach the socket at all: tests/conftest.py sets
    # WRIT_PORT globally, on purpose, to keep the suite off the interactive daemon,
    # and the arm below then read that as "the caller named an endpoint". A test
    # meant to exercise the socket passed without touching one.
    WRIT_CURL_TRANSPORT="--unix-socket $WRIT_SESSION_SOCKET"
elif [ -n "${WRIT_HOST:-}" ] || [ -n "${WRIT_PORT:-}" ]; then
    WRIT_CURL_TRANSPORT=""
elif [ -S "$WRIT_SESSION_SOCKET" ]; then
    WRIT_CURL_TRANSPORT="--unix-socket $WRIT_SESSION_SOCKET"
else
    WRIT_CURL_TRANSPORT=""
fi

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
            skip_result=$(curl ${WRIT_CURL_TRANSPORT} -sf --connect-timeout 0.1 --max-time 0.5 \
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
            mode_result=$(curl ${WRIT_CURL_TRANSPORT} -sf --connect-timeout 0.1 --max-time 0.5 \
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
            # THE BODY GOES ON CURL'S STDIN (`--data-binary @-`), not on its argv.
            # `-d "$cw_post_body"` put the whole tool envelope in ONE argument, and this
            # envelope carries a write's CONTENT: measured on this machine, `curl -d` with
            # a 132,000-byte value fails to exec at all ("Argument list too long", rc 126)
            # while 130,000 succeeds, so a large write could never reach the daemon and
            # every such call silently took the local-fallback branch below. `--data-binary`
            # rather than `-d` because `-d` strips newlines from a file/stdin body, and
            # `printf` is a builtin, so this adds no process. The Content-Type header stays
            # explicit, which is the only thing `-d` was giving us here.
            local cw_result=""
            cw_result=$(printf '%s' "$cw_post_body" | curl ${WRIT_CURL_TRANSPORT} -sf --connect-timeout 0.1 --max-time 0.5 \
                -X POST "${WRIT_SESSION_BASE}/session/${session_id}/can-write" \
                -H "Content-Type: application/json" --data-binary @- 2>/dev/null) || true
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
                fmt_result=$(curl ${WRIT_CURL_TRANSPORT} -sf --connect-timeout 0.1 --max-time 0.5 \
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
            # Own curl so the exit status survives. rc 28 is a timeout, either at
            # connect or after the POST was accepted. After acceptance the daemon is
            # still running auto-feedback in its own thread, and a local run would
            # read feedback_sent before that thread writes it back and POST every
            # rule a second time. Either way the local run's /feedback POSTs go to
            # the same socket first, so a daemon too slow for curl is too slow for
            # them. (Its TCP fallback is the default localhost:8765: WRIT_SESSION_BASE
            # is not exported, so a custom WRIT_HOST/WRIT_PORT does not reach it.)
            # Every other failure (7 no listener, 22 HTTP error, 127 no curl) falls back.
            local afb_result="" afb_rc=0
            afb_result=$(curl ${WRIT_CURL_TRANSPORT} -sf --connect-timeout 0.1 --max-time 0.5 \
                -X POST "${WRIT_SESSION_BASE}/session/${session_id}/auto-feedback" \
                -H "Content-Type: application/json" \
                -d '{"feedback":""}' 2>/dev/null) || afb_rc=$?
            if [ -n "$afb_result" ]; then
                echo "$afb_result"
                return 0
            fi
            if [ "$afb_rc" -eq 28 ]; then
                echo '{"auto_feedback":"daemon_timeout","local_fallback":"skipped"}'
                return 0
            fi
            python3 "$helper" auto-feedback "$@"
            return $?
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
            # THE BODY GOES ON CURL'S STDIN, for the reason spelled out on the can-write
            # arm above: this body embeds the write's full `content`, `-d` puts it in one
            # argv string, and MAX_ARG_STRLEN refuses the exec above ~131,000 bytes. The
            # consequence was not a wrong decision but a MISSING one at the daemon: curl
            # never ran, `pwc_result` came back empty, and every write over the cap fell
            # through to the local evaluator below, losing the server's write_attempt row
            # and its RAG rules. Fixing the argv on the hook side (writ-pre-write-dispatch.sh)
            # without fixing this line would leave the door shut one layer further in.
            local pwc_result=""
            pwc_result=$(printf '%s' "$check_body" | curl ${WRIT_CURL_TRANSPORT} -sf --connect-timeout 0.2 --max-time 1 \
                -X POST "${WRIT_SESSION_BASE}/pre-write-check" \
                -H "Content-Type: application/json" \
                --data-binary @- 2>/dev/null) || true
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
" 2>/dev/null | _writ_session can-write "$fallback_result" --skill-dir "${SKILL_DIR:-}" 2>/dev/null) || cw_result=""
                if [ -n "$cw_result" ]; then
                    echo "$cw_result"
                    return 0
                fi
                # The local evaluator itself could not run (a partial install, a broken
                # venv: the helper exits non-zero with empty stdout). This arm used to be
                # an inline `|| echo allow`, which ran BEFORE the strict check below and
                # so handed an allow to an operator who had opted into failing closed.
                # Same policy as the no-session arm, applied where the crash lands.
                if [ "${WRIT_STRICT:-}" = "1" ]; then
                    echo '{"decision":"deny","reason":"[ENF-STRICT-001] Writ strict mode (WRIT_STRICT=1): the local write-gate evaluator could not be run (daemon unreachable and the session helper failed), so this write fails closed. Check the Writ install (writ doctor) or unset WRIT_STRICT.","rag_rules":"","rag_meta":{"rule_ids":[],"tokens":0}}'
                    return 0
                fi
                echo '{"decision":"allow","reason":null,"rag_rules":"","rag_meta":{"rule_ids":[],"tokens":0}}'
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
        result=$(curl ${WRIT_CURL_TRANSPORT} -sf --connect-timeout 0.1 --max-time 0.5 \
            -X POST "$url" \
            -H "Content-Type: application/json" \
            -d "$body" 2>/dev/null) || true
    else
        result=$(curl ${WRIT_CURL_TRANSPORT} -sf --connect-timeout 0.1 --max-time 0.5 "$url" 2>/dev/null) || true
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
# The 4th argument is the caller's PROJECT ROOT, sent so the daemon can scope
# retrieval to that project's records (writ/retrieval/node_scope.py). A root, not a
# resolved name: the registry lookup needs the graph, and resolving it here would cost
# a python start plus a Neo4j round trip on a path that runs on every file read and
# every write. Empty is a legitimate value (no project root found) and degrades to
# doctrine-only retrieval server-side, never to an error.
# Usage: RESPONSE=$(rag_query "$QUERY" "$PRETOOL_BUDGET" "$LOADED_RULE_IDS" "$PROJECT_ROOT")
rag_query() {
    local query="$1" budget="$2" exclude="$3" project_root="${4:-}"
    local request
    request=$(python3 -c "
import json, sys
print(json.dumps({
    'query': sys.argv[1],
    'budget_tokens': int(sys.argv[2]),
    'exclude_rule_ids': json.loads(sys.argv[3]),
    'top_k': 3,
    'project_root': sys.argv[4],
}))
" "$query" "$budget" "$exclude" "$project_root" 2>/dev/null)
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
# session_helper is kept in the signature for its two callers; the Declare
# line names the path-free `writ mode` (bin/writ), which is on the Bash tool's PATH.
emit_mode_directive() {
  local session_helper="$1" session_id="$2"
  cat << MODE_DIRECTIVE

[Writ: set mode before proceeding]
Conversation: discussion, no code. Debug: investigating a problem, no code.
Review: evaluating code against rules, no code. Work: building/modifying code (full workflow).
Investigate: audit / explore / research a codebase or topic (evidence-grounded, read-heavy).
Declare: writ mode set <conversation|debug|review|work|investigate> ${session_id}
Full definitions: see HANDBOOK.md "Mode system" section.
MODE_DIRECTIVE
}

# Emit the post-compaction workflow state line + the PSR-004 verify-discipline directive.
# THE SINGLE SOURCE of both texts. They used to live in writ-postcompact.sh, which cannot
# deliver them: on 2026-08-14 a real /compact showed CC's hook-output validator reject a
# PostCompact hookSpecificOutput reply outright ("(root): Invalid input") and discard it, so
# the directive had reached nothing since cycle B. writ-postcompact.sh now only queues
# (post_compact_pending) and writ-rag-inject.sh calls this on the next UserPromptSubmit,
# where bare stdout is a confirmed model channel.
# The state line is emitted only when a mode is set; the directive always is.
# Usage: emit_post_compact_directive "$CURRENT_MODE" "$PHASE"
emit_post_compact_directive() {
  local mode="$1" phase="${2:-}"
  if [ -n "$mode" ]; then
    cat << PC_STATE

[Writ: post-compact workflow state] mode=$mode, phase=${phase:-unknown}. This turn re-injects
the full rule set; treat this as your current workflow position.
PC_STATE
  fi
  # Quoted delimiter: the directive is literal text, and an unquoted heredoc would expand
  # any \$ or backtick a future edit adds to it.
  cat << 'PC_DIRECTIVE'

[Writ: context compacted]
Until the next compaction, treat any pre-compact verification output (test counts,
"passing" claims, file reads) as second-hand evidence.

If asked "is it working?" / "is it done?" / "did it pass?":
  1. Re-run the relevant verification (tests, lint, typecheck, smoke command) FIRST.
  2. If the re-run is BLOCKED (tool rejection, permission denied, env unavailable):
       STOP. Do NOT answer "yes", "passing", or "should be working".
       Respond instead: "Re-verification was blocked by [reason]. I cannot confirm
       post-compact. Pre-compact context says X but I have no fresh evidence.
       Want me to verify another way?"
  3. Only answer affirmatively with fresh test/lint output cited inline.

Saying "yes" / "passing" / "all good" without fresh evidence is a forbidden response
in this state. Recalled output is not fresh evidence.
PC_DIRECTIVE
}

# ── Telemetry coverage follows LOCATION, not memory ──────────────────────────
# hook_instrument is opt-in, and 8 of the 40 hooks registered in hooks/hooks.json called
# neither it nor any other emitter, so nothing could say whether they had ever run. The
# reason that sat unnoticed is instructive: three successive lexical scans for "does this
# file instrument itself" returned 21, then 12, then 10 uninstrumented hooks before
# landing on 8, because there are four spellings that produce a row. A check that must
# enumerate spellings is the defect it is meant to catch, so coverage is now structural.
#
# THE GUARD IS THE SOURCING SCRIPT'S PATH. ${BASH_SOURCE[1]} is the file that sourced
# this one. All 40 registered hooks live under hooks/scripts/ and source common.sh; the
# five CLI tools under bin/ that also source it are NOT hooks, must not carry an exit
# trap, and must not appear in hook metrics.
#
# SAFE BECAUSE NOTHING COMPETES FOR THE TRAP: no script under hooks/scripts/ or bin/
# installs its own EXIT/TERM/INT/HUP handler, and tests/test_exit_trap_ownership.py keeps
# it that way. A hook that calls hook_instrument itself afterwards re-installs the same
# single trap under its chosen name, which is why the existing explicit calls keep
# working and keep their names.
#
# THE NAME IS DERIVED WITHOUT A PROCESS. hook_instrument's own default would resolve
# BASH_SOURCE[1] to common.sh from here, so the name is passed explicitly, via parameter
# expansion rather than basename: this runs on every hook of every turn.
#
# BOTH PATTERNS ARE REQUIRED. `*/hooks/scripts/*` needs a slash BEFORE `hooks`, so it
# misses a RELATIVE invocation (`bash hooks/scripts/x.sh`), which is how the scripts are
# run by hand and from some tests. Claude Code always passes an absolute path, so this gap
# would never have shown up in production and would have silently dropped telemetry
# everywhere else. Found by probing a real hook, not by the unit tests, which happened to
# use absolute tmp_path scripts.
#
# LOCATION IS NOT QUITE THE PREDICATE: one script under hooks/scripts/ is NOT a hook.
# writ-statusline.sh is wired through the settings `statusLine` channel, not hooks.json, and
# it renders constantly. Instrumenting it cost a trap on every render and wrote 673 rows in
# one day under the session id `unknown` (it never sets SESSION_ID), which did more damage
# than the waste: writ-flush-events.py sweeps a buffer once it goes ABANDONED_SESSION_SECONDS
# without a write, so the statusline's constant appends kept `writ-events-unknown.buf`
# perpetually young and stranded 22 writ-subagent-stop rows that would otherwise have been
# collected. One non-hook held the whole bucket open.
#
# WHY A HARD-CODED NAME AND NOT A PREDICATE. Reading hooks/hooks.json here to test
# membership costs a subshell on every hook of every turn, on the path where the cycle
# before this one removed 15 execve per file write. An env-var opt-out (WRIT_NOT_A_HOOK=1)
# is one line but hands the agent a switch that silences all hook telemetry, and a control
# the agent can disable is not a control. Moving the script under bin/ is structurally
# cleanest and breaks every installed statusLine setting, because writ_install.py pins the
# path (STATUSLINE_REL).
#
# THE ENUMERATION IS KEPT HONEST BY A TEST, not by vigilance:
# test_subagent_role_census.py derives the unregistered-script set from hooks.json and fails
# if any member is missing from this line, so the next non-hook added here cannot be
# instrumented silently.
case "${BASH_SOURCE[1]:-}" in
    hooks/scripts/writ-statusline.sh|*/hooks/scripts/writ-statusline.sh)
        ;;
    hooks/scripts/*|*/hooks/scripts/*)
        _writ_auto_hook="${BASH_SOURCE[1]##*/}"
        hook_instrument "${_writ_auto_hook%.sh}"
        unset _writ_auto_hook
        ;;
esac
