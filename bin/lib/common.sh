#!/bin/bash
# Shared library for phaselock bin/ scripts and hooks.
# Source this file: source "$(dirname "$0")/lib/common.sh"

# ── Hook stdin envelope parser ──────────────────────────────────────────────
# Reads the Claude Code hook stdin envelope and normalizes it into a JSON
# object with flattened fields (file_path, content, tool_name, is_error, etc.).
# Falls back to CLAUDE_TOOL_INPUT env var when envelope is missing.
#
# Usage (call ONCE per hook, stdin is consumed):
#   PARSED=$(parse_hook_stdin)
#   FILE=$(echo "$PARSED" | jq -r '.file_path // empty')
#   TOOL=$(echo "$PARSED" | jq -r '.tool_name // empty')
#
# If jq is unavailable, use python3:
#   FILE=$(echo "$PARSED" | python3 -c "import sys,json; print(json.load(sys.stdin).get('file_path',''))")
_PARSE_HOOK_STDIN_PY="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/parse-hook-stdin.py"

parse_hook_stdin() {
    python3 "$_PARSE_HOOK_STDIN_PY" 2>/dev/null || echo '{}'
}

# Convenience: extract a single string field from parsed JSON.
# Usage: FILE=$(parsed_field "$PARSED" "file_path")
parsed_field() {
    local json="$1" field="$2"
    echo "$json" | python3 -c "import sys,json; print(json.load(sys.stdin).get('$field',''))" 2>/dev/null
}

# Convenience: extract a boolean field from parsed JSON.
# Usage: if parsed_bool "$PARSED" "is_error"; then ...
parsed_bool() {
    local json="$1" field="$2"
    local val
    val=$(echo "$json" | python3 -c "import sys,json; print(json.load(sys.stdin).get('$field', False))" 2>/dev/null)
    [ "$val" = "True" ]
}

# ── Phase 2 feature flag check ──────────────────────────────────────────────
# Returns success (exit 0) if enforcement.superpowers_absorb.enabled=true in
# writ.toml. Plan Section 7.1 deliverable 10: all new Phase 2 hooks MUST early-
# exit when the flag is false, preserving pre-Phase-2 session behavior.
# Usage: if ! is_superpowers_absorb_enabled; then exit 0; fi
is_superpowers_absorb_enabled() {
    local writ_root="${WRIT_DIR:-}"
    if [ -z "$writ_root" ]; then
        local hook_dir
        hook_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
        writ_root="$(cd "$hook_dir/.." && pwd)"
    fi
    local toml="$writ_root/writ.toml"
    [ -f "$toml" ] || return 1
    python3 -c "
import sys
try:
    import tomllib
except ImportError:
    try:
        import tomli as tomllib
    except ImportError:
        sys.exit(1)
try:
    with open('$toml', 'rb') as f:
        data = tomllib.load(f)
    enforcement = data.get('enforcement', {})
    absorb = enforcement.get('superpowers_absorb', {})
    enabled = absorb.get('enabled', False)
    sys.exit(0 if enabled else 1)
except Exception:
    sys.exit(1)
" 2>/dev/null
}

# Phase 2 mode-scope check. Phase 1 Section 0.4 decision 1: methodology
# enforcement hooks fire only in Work mode. Non-work modes skip.
# Usage: if ! is_work_mode "$SESSION_ID"; then exit 0; fi
is_work_mode() {
    local sid="$1"
    local helper="${WRIT_DIR:-}/bin/lib/writ-session.py"
    [ -f "$helper" ] || return 1
    local mode
    mode=$(python3 "$helper" mode get "$sid" 2>/dev/null | tr -d '[:space:]')
    [ "$mode" = "work" ]
}

# ── Project root detection ────────────────────────────────────────────────────
# Walks up from a given path to find the project root by marker files.
# Usage: PROJECT_ROOT=$(detect_project_root "/path/to/some/file.php")
detect_project_root() {
  local start_path="$1"
  python3 -c "
import os, sys
markers = ['composer.json','package.json','Cargo.toml','go.mod','pyproject.toml','.git']
path = os.path.abspath('$start_path')
while path != '/':
    path = os.path.dirname(path)
    if any(os.path.exists(os.path.join(path, m)) for m in markers):
        print(path); sys.exit(0)
print('')
" 2>/dev/null
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
# Produces a single JSON finding object. All values are passed as arguments.
# Usage: json_finding true "ENF-POST-007" "PHPStan error" "src/Foo.php" "Fix it"
json_finding() {
  local is_error="$1" rule="$2" message="$3" file="$4" fix="$5"
  python3 -c "
import json, sys
print(json.dumps({
    'error': $(echo "$is_error" | python3 -c "import sys; print(sys.stdin.read().strip().capitalize())"),
    'rule': sys.argv[1],
    'message': sys.argv[2],
    'file': sys.argv[3],
    'fix': sys.argv[4]
}, ensure_ascii=False))" "$rule" "$message" "$file" "$fix" 2>/dev/null
}

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
# Appends a JSON event to workflow-friction.log. Fire-and-forget.
# Usage: log_friction_event "$SESSION_ID" "$MODE" "event_name" '{"key":"val"}'
# Extra fields arg is optional JSON object to merge.
log_friction_event() {
  local session_id="$1" mode="$2" event="$3" extra="${4:-{}}"
  python3 -c "
import json, sys, os
from datetime import datetime, timezone
entry = {
    'ts': datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ'),
    'session': sys.argv[1],
    'mode': sys.argv[2] if sys.argv[2] else None,
    'event': sys.argv[3],
}
try:
    entry.update(json.loads(sys.argv[4]))
except (json.JSONDecodeError, ValueError):
    pass
markers = ['composer.json','package.json','Cargo.toml','go.mod','pyproject.toml','.git']
path = os.getcwd()
while path != '/':
    if any(os.path.exists(os.path.join(path, m)) for m in markers):
        try:
            with open(os.path.join(path, 'workflow-friction.log'), 'a') as f:
                f.write(json.dumps(entry) + '\n')
        except OSError:
            pass
        break
    path = os.path.dirname(path)
" "$session_id" "$mode" "$event" "$extra" 2>/dev/null || true
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
hook_timer_end() {
  local start_ns="$1" hook_name="$2" session_id="$3" mode="$4"
  python3 -c "
import json, time, os, sys
from datetime import datetime, timezone
try:
    start_ns = int(sys.argv[1])
except (ValueError, IndexError):
    start_ns = 0
end_ns = int(time.time() * 1e9)
duration_ms = max(0, (end_ns - start_ns) // 1_000_000)
entry = {
    'ts': datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ'),
    'session': sys.argv[2],
    'mode': sys.argv[3] if sys.argv[3] else None,
    'event': 'hook_execution',
    'hook_name': sys.argv[4],
    'duration_ms': duration_ms,
}
markers = ['composer.json','package.json','Cargo.toml','go.mod','pyproject.toml','.git']
path = os.getcwd()
while path != '/':
    if any(os.path.exists(os.path.join(path, m)) for m in markers):
        try:
            with open(os.path.join(path, 'workflow-friction.log'), 'a') as f:
                f.write(json.dumps(entry) + '\n')
        except OSError:
            pass
        break
    path = os.path.dirname(path)
" "$start_ns" "$session_id" "$mode" "$hook_name" 2>/dev/null || true
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
                local should_skip
                should_skip=$(echo "$skip_result" | python3 -c "import sys,json; print(json.load(sys.stdin).get('should_skip',False))" 2>/dev/null || echo "False")
                if [ "$should_skip" = "True" ]; then
                    return 0
                else
                    return 1
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
                echo "$mode_result" | python3 -c "import sys,json; print(json.load(sys.stdin).get('mode',''))" 2>/dev/null
                return $?
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
            url="${WRIT_SESSION_BASE}/session/${session_id}/can-write"
            method="POST"
            body="{}"
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
            # format is stateless and reads from stdin; fall through to subprocess
            python3 "$helper" format "$@"
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
            local check_body="${2:-{}}"
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
        "detect-compaction")
            local pct="${2:-0}"
            local dc_result=""
            dc_result=$(curl -sf --connect-timeout 0.1 --max-time 0.5 \
                -X POST "${WRIT_SESSION_BASE}/session/${session_id}/detect-compaction" \
                -H "Content-Type: application/json" \
                -d "{\"context_percent\":${pct}}" 2>/dev/null) || true
            if [ -n "$dc_result" ]; then
                echo "$dc_result"
                return 0
            fi
            # Fallback to subprocess
            python3 "$helper" detect-compaction "$session_id" --context-percent "$pct"
            return $?
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
