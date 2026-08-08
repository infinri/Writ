#!/usr/bin/env bash
# Writ RAG Bridge -- UserPromptSubmit hook. Fires at the start of every
# user turn; queries Writ for relevant rules and injects them via stdout.
# Hook type: UserPromptSubmit. Exit: always 0 (never block user prompt).
set -euo pipefail
# Plugin-mode overrides: prefer ${CLAUDE_PLUGIN_ROOT} when set, else
# fall back to the dirname walk that standalone installs rely on.
if [ -n "${CLAUDE_PLUGIN_ROOT:-}" ]; then
  WRIT_DIR="${CLAUDE_PLUGIN_ROOT}"
  VENV_DIR="${CLAUDE_PLUGIN_DATA:-$HOME/.cache/writ}/.venv"
else
  HOOK_DIR="$(cd "$(dirname "$0")" && pwd)"
  WRIT_DIR="$(cd "$HOOK_DIR/../.." && pwd)"
  VENV_DIR="$WRIT_DIR/.venv"
fi
SESSION_HELPER="$WRIT_DIR/bin/lib/writ-session.py"
FA="$WRIT_DIR/bin/lib/friction-append.py"
source "$WRIT_DIR/bin/lib/common.sh"

WRIT_HOST="${WRIT_HOST:-localhost}"
WRIT_PORT="${WRIT_PORT:-8765}"
# #8: the broad /query + /always-on + /methodology-companion channels are fetched in ONE
# warm call to /prompt-bundle (below). COMPANION_URL is still used by the orchestrator branch.
COMPANION_URL="http://${WRIT_HOST}:${WRIT_PORT}/methodology-companion"
WRIT_HEALTH_URL="http://${WRIT_HOST}:${WRIT_PORT}/health"
WRIT_DEBUG_LOG="${WRIT_DEBUG_LOG:-/tmp/writ-rag-debug.log}"

MIN_QUERY_LENGTH=10

# Gated behind WRIT_DEBUG (default OFF) via the shared common.sh helper: no debug
# file is written unless WRIT_DEBUG=1. WRIT_DEBUG_LOG is an env-overridable knob.
debug() {
    debug_log "$WRIT_DEBUG_LOG" "$*"
}

# WRIT_HOOK_LOG stderr breadcrumb sink, gated by WRIT_DEBUG: /dev/null when unset,
# ${WRIT_HOOK_LOG:-/tmp/writ-hooks.log} when WRIT_DEBUG=1 (single source: common.sh).
WRIT_HOOK_LOG_SINK="$(hook_log_sink)"

# Capture stdin once -- Claude Code sends JSON with prompt, session_id, etc.
STDIN_JSON=$(cat)
printf '%s' "$STDIN_JSON" | blackbox_log in writ-rag-inject
debug "stdin: ${STDIN_JSON:0:200}"

# Auto-start: ensure Neo4j and the Writ server are running.
# WRIT_NO_AUTOSTART (set by tests / CI) suppresses the auto-start so running this hook
# against a throwaway WRIT_PORT does not spawn (and leak) a real daemon on that port.
if [ -z "${WRIT_NO_AUTOSTART:-}" ] && ! curl -sf --connect-timeout 0.2 "$WRIT_HEALTH_URL" >/dev/null 2>&1; then
    debug "server down, attempting auto-start"

    # Ensure Neo4j is running (docker start is a no-op if already up).
    if command -v docker >/dev/null 2>&1; then
        docker start writ-neo4j >/dev/null 2>&1 || true
        # Wait up to 8s for Neo4j HTTP port
        for _i in $(seq 1 16); do
            if curl -sf --connect-timeout 0.1 http://localhost:7474 >/dev/null 2>&1; then
                break
            fi
            sleep 0.5
        done
    fi

    # Start the Writ daemon through the shared, flock-guarded singleton entry point
    # (scripts/lib/writ-server-lib.sh) instead of a bespoke noclobber lockfile + an
    # inline server launch (audit #4). The old custom lock did not coordinate with
    # ensure-server.sh / session-start-bootstrap.sh, so two start paths firing close in
    # time could launch two daemons. writ_ensure_server flocks the check-then-start and
    # closes the lock fd in the child (9>&-); it also pins WRIT_CACHE_DIR so the daemon
    # is born aligned.
    if [ -f "$WRIT_DIR/scripts/lib/writ-server-lib.sh" ]; then
        # FIX-2: pin the daemon's session-cache dir before delegating so a hook-started
        # daemon is born aligned (writ_ensure_server also pins it, defensively).
        export WRIT_CACHE_DIR="$(writ_session_cache_dir)"
        # shellcheck source=scripts/lib/writ-server-lib.sh
        source "$WRIT_DIR/scripts/lib/writ-server-lib.sh"
        WRIT_HOST="$WRIT_HOST" WRIT_PORT="$WRIT_PORT" WRIT_DIR="$WRIT_DIR" \
            VENV_DIR="$VENV_DIR" \
            writ_ensure_server || true
    fi
fi

# Extract session_id and prompt from the captured stdin JSON.
# Claude Code provides: session_id, prompt, cwd, hook_event_name, etc.
# The prompt is cleaned before use: code blocks, markdown chrome, and tool
# output are stripped so the RAG query contains only the user's intent.
PARSED=$(echo "$STDIN_JSON" | python3 "$WRIT_DIR/bin/lib/writ-prompt-parse.py" 2>/dev/null) || true

SESSION_ID=$(echo "$PARSED" | head -1)
PROMPT=$(echo "$PARSED" | sed -n '2p')
AGENT_ID=$(echo "$PARSED" | sed -n '3p')
MODE_HINT=$(echo "$PARSED" | sed -n '4p' | tr -d '[:space:]')
EFFORT=$(echo "$PARSED" | sed -n '5p' | tr -d '[:space:]')

# Fallback session ID if not provided by Claude Code
if [ -z "$SESSION_ID" ]; then
    SESSION_ID=$(ps -o ppid= -p $PPID 2>/dev/null | tr -d ' ')
fi
if [ -z "$SESSION_ID" ]; then
    SESSION_ID=$(echo "${PWD}:${USER}" | md5sum | cut -c1-12)-$(date +%Y%m%d)
fi

# Publish session ID so Stop hooks (friction-logger) can find it
# Do NOT overwrite when inside a sub-agent -- protect parent's session file
if [ -z "$AGENT_ID" ]; then
    echo "$SESSION_ID" > /tmp/writ-current-session
fi

debug "session=$SESSION_ID prompt_len=${#PROMPT}"

# 1. Check skip conditions (budget exhausted or context pressure > 75%)
if _writ_session should-skip "$SESSION_ID" 2>/dev/null; then
    debug "skipped: budget or context pressure"
    exit 0
fi

# 1a. Compaction recovery runs on the real PostCompact event via
# writ-postcompact.sh. The previous heuristic here relied on a
# non-existent env var and was removed.

# 1b. A1: mode is derived from the SINGLE session-cache read below (step 1c),
# not a standalone `mode get` daemon round-trip. Initialized empty here so the
# auto-route block can set it before that read happens.
CURRENT_MODE=""

# 1b-2. Mode auto-routing. When no mode is set on the MASTER session and the prompt is
# audit/explore/research-shaped (classify_mode_hint), auto-set investigate -- the gate-light
# audit/explore/research engine -- so the audit path is governed from turn one instead of
# defaulting to conversation (the observed A.S.E. failure). Only investigate is auto-set; it
# has no disruptive gates and is announced + overridable. Skip inside sub-agents (they
# inherit the parent's mode at SubagentStart).
#
# SAFETY: the daemon-first `mode get` above can spuriously return empty under load, so we do
# NOT trust CURRENT_MODE's emptiness for this side-effecting set. Re-confirm "truly unset"
# with a tiny stdlib-only read of the cache file's mode field (no writ-package import, so it
# cannot flake the way the package read can). Only auto-set when that confirms no mode --
# never override an explicit choice.
if [ -z "$AGENT_ID" ] && [ -n "$MODE_HINT" ]; then
  # Single-source resolver (common.sh). This read used to inline its own cache-dir
  # resolution with a tempdir fallback, which pointed at a directory holding no session
  # caches, so it answered "unset" for EVERY session and the branch below fired every turn.
  PRIOR_MODE=$(writ_session_mode_direct "$SESSION_ID")
  AUTOROUTED="no"
  if [ -z "$PRIOR_MODE" ]; then
    # `mode init` (not `mode set`): authoritatively sets the mode ONLY if still
    # unset (checked inside the helper's own cache read), so a spurious re-fire on
    # a transient empty PRIOR_MODE read can never reset a live gate cycle.
    python3 "$SESSION_HELPER" mode init "$MODE_HINT" "$SESSION_ID" >/dev/null 2>&1 || true
    # Re-read and act on the mode that is ACTUALLY set. `mode init` declines when a mode
    # already exists (its own locked check is the authority, not ours) and prints
    # "init: <mode>" either way, so its output cannot distinguish the two. Trusting the
    # hint instead is how the hook came to tell the user "the mode is now 'work'" while
    # the cache said conversation and the gate ran unarmed for the whole turn.
    CONFIRMED_MODE=$(writ_session_mode_direct "$SESSION_ID")
    if [ -n "$CONFIRMED_MODE" ]; then
      CURRENT_MODE="$CONFIRMED_MODE"
      [ "$CONFIRMED_MODE" = "$MODE_HINT" ] && AUTOROUTED="yes"
    fi
    debug "auto-route requested $MODE_HINT -> mode is now '${CONFIRMED_MODE:-unset}'"
  fi
  # Announce ONLY a change we actually made.
  if [ "$AUTOROUTED" = "yes" ]; then
    if [ "$MODE_HINT" = "investigate" ]; then
      cat << AUTOROUTE

[Writ: audit/explore request -> investigate mode set automatically]
This reads as an audit / exploration / research task, so the mode is now 'investigate'
(the evidence-grounded audit/explore/research engine). Dispatch writ-explorer (read-only)
for the actual exploration; it inherits this mode and runs governed. To override:
  python3 $SESSION_HELPER mode set <conversation|debug|review|work|investigate> $SESSION_ID
AUTOROUTE
    else
      cat << WORKROUTE

[Writ: implementation request -> work mode set automatically]
This reads as a build/implementation task, so the mode is now 'work' (the full gated
workflow). BEFORE writing source: write plan.md and capabilities.md at the project root by
filling in templates/plan-template.md and templates/capabilities-template.md from the Writ
skill directory (they encode the gate's exact format, including the ## Files line grammar),
present them for approval, then write test skeletons, then implement. Source writes are
BLOCKED by the gate until the plan and test-skeleton gates are approved. If this is a trivial
edit that needs no workflow, override with:
  python3 $SESSION_HELPER mode set conversation $SESSION_ID
WORKROUTE
    fi
  fi
fi

# 1c. A1: the SINGLE session-cache read for the whole hook. Was THREE daemon
# round-trips -- a standalone `mode get`, this orchestrator read, and the
# main-path read at step 3. Now one read whose $CACHE is reused everywhere:
# mode + is_orchestrator are derived here via the jq-first parsed_* helpers (no
# extra python spawn), the orchestrator branch reuses it through the CACHE_DATA
# alias, the main path reuses it for CACHE_FIELDS, and the escalation /
# backward-context blocks reuse it raw. Positioned AFTER the auto-route block so
# a just-set investigate mode is reflected.
CACHE=$(_writ_session read "$SESSION_ID" 2>/dev/null || echo '{"loaded_rule_ids":[],"remaining_budget":8000}')
CACHE_DATA="$CACHE"  # orchestrator branch alias -- same single read, no second round-trip
if [ -z "$CURRENT_MODE" ]; then
    CURRENT_MODE=$(parsed_field "$CACHE" "mode")
    CURRENT_MODE=$(echo "$CURRENT_MODE" | tr -d '[:space:]')
fi
debug "mode=$CURRENT_MODE"
if parsed_bool "$CACHE" "is_orchestrator"; then IS_ORCHESTRATOR="true"; else IS_ORCHESTRATOR="false"; fi

# 1c-2. Decision-memory Phase 2: once-per-session recall briefing. On the FIRST
# UserPromptSubmit of a master session, surface the project's recent
# rule-grounded decisions via additionalContext (the confirmed channel; see
# docs/reference/claude-code-blackbox.md -- SessionStart's only confirmed surface is
# initialUserMessage, which would seed a fake user turn). Guarded by the
# recall_briefed cache flag so it fires exactly once; fail-open + time-bounded so
# a recall failure or a slow daemon never blocks the prompt.
if [ -z "$AGENT_ID" ]; then
    RECALL_BRIEFED=$(parsed_bool "$CACHE" "recall_briefed" && echo "yes" || echo "no")
    if [ "$RECALL_BRIEFED" != "yes" ]; then
        RECALL_REQ=$(WRIT_ROOT="${PWD}" python3 -c "
import os, json
print(json.dumps({'project_root': os.environ.get('WRIT_ROOT', ''), 'budget': 20000}))
" 2>/dev/null)
        # Documented daemon-down-equivalent raw curl: with curl absent this degrades to
        # exactly the "no briefing this session" branch a stopped daemon produces.
        RECALL_RESP=$(curl -s --connect-timeout 0.3 --max-time 1.5 -X POST "http://${WRIT_HOST}:${WRIT_PORT}/recall" \
            -H "Content-Type: application/json" -d "$RECALL_REQ" 2>/dev/null) || true
        # parsed_field (jq-first, python3 fallback) rather than raw jq: with jq absent the
        # raw extraction returned empty and the briefing was silently dropped.
        RECALL_BRIEFING=$(parsed_field "$RECALL_RESP" "briefing")
        if [ -n "$RECALL_BRIEFING" ]; then
            echo ""
            echo "$RECALL_BRIEFING"
            debug "injected recall briefing (once-per-session)"
        fi
        # Mark briefed regardless of whether decisions existed, so we attempt
        # recall at most once per session (empty project -> empty briefing -> no
        # re-attempt every turn).
        python3 "$SESSION_HELPER" update "$SESSION_ID" --set-recall-briefed 2>>"$WRIT_HOOK_LOG_SINK" || true
    fi
fi

if [ "$IS_ORCHESTRATOR" = "true" ]; then
    debug "orchestrator mode: skipping broad /query, firing methodology companion + status line"
    # Still emit mode-classification directive if no mode set
    if [ -z "$CURRENT_MODE" ]; then
        emit_mode_directive "$SESSION_HELPER" "$SESSION_ID"
    fi
    # Compact status line for orchestrator (CACHE_DATA already read above)
    STATUS_LINE=$(echo "$CACHE_DATA" | python3 -c "
import sys, json
try:
    c = json.load(sys.stdin)
    phase = c.get('current_phase', 'unknown')
    gates = c.get('gates_approved', [])
    violations = len(c.get('pending_violations', []))
    mode = c.get('mode', 'unknown')
    print(f'[Writ: mode={mode}, phase={phase}, gates={gates}, violations={violations}]')
except Exception:
    print('[Writ: orchestrator mode active]')
" 2>/dev/null)
    echo "$STATUS_LINE"

    # PSR-008 Finding 1: orchestrator master must still surface
    # methodology context (skills, playbooks). The broad coding-rule
    # RAG is intentionally suppressed -- workers cover that domain --
    # but methodology nodes guide workflow decisions the orchestrator
    # itself owns. Fires when CURRENT_MODE=work AND prompt is non-trivial.
    ORCH_REMAINING_BUDGET=$(echo "$CACHE_DATA" | python3 -c "import sys,json; print(json.load(sys.stdin).get('remaining_budget',8000))" 2>/dev/null || echo '8000')
    ORCH_LOADED_RULE_IDS=$(echo "$CACHE_DATA" | python3 "$WRIT_DIR/bin/lib/writ_phase_scoped_rules.py" 2>/dev/null || echo '[]')

    if [ "${CURRENT_MODE:-}" = "work" ] && [ "${ORCH_REMAINING_BUDGET:-0}" -gt 600 ] && [ ${#PROMPT} -ge $MIN_QUERY_LENGTH ]; then
        ORCH_METHOD_REQUEST=$(python3 -c "
import json, sys
try:
    exclude = json.loads(sys.argv[2])
except (json.JSONDecodeError, ValueError) as _e:
    sys.stderr.write(
        f'[writ-hook json.loads recovery] argv[2] (exclude_rule_ids) in writ-rag-inject.sh '
        f'orchestrator companion request: {_e}\\n  sample={sys.argv[2][:200]!r}\\n'
    )
    exclude = []
print(json.dumps({
    'mode': 'work',
    'prompt': sys.argv[1],
    'exclude_rule_ids': exclude,
    'budget_tokens': 2000,
}))
" "$PROMPT" "$ORCH_LOADED_RULE_IDS" 2>/dev/null)

        if [ -n "$ORCH_METHOD_REQUEST" ]; then
            # Documented daemon-down-equivalent raw curl: no companion block, same as a
            # stopped daemon produces.
            ORCH_METHOD_RESPONSE=$(curl -s --connect-timeout 0.5 --max-time 2 -X POST "$COMPANION_URL" \
                -H "Content-Type: application/json" \
                -d "$ORCH_METHOD_REQUEST" 2>/dev/null) || true

            if [ -n "$ORCH_METHOD_RESPONSE" ]; then
                ORCH_METHOD_FORMAT=$(echo "$ORCH_METHOD_RESPONSE" | _writ_session format 2>/dev/null) || true
                ORCH_METHOD_TEXT=""
                ORCH_METHOD_META=""
                if [ -n "$ORCH_METHOD_FORMAT" ]; then
                    ORCH_METHOD_TEXT=$(echo "$ORCH_METHOD_FORMAT" | grep -v "^WRIT_META:" || true)
                    ORCH_METHOD_META=$(echo "$ORCH_METHOD_FORMAT" | grep "^WRIT_META:" | head -1 || true)
                fi

                if [ -n "$ORCH_METHOD_TEXT" ]; then
                    echo ""
                    echo "[Writ: methodology companion]"
                    echo "$ORCH_METHOD_TEXT"
                fi

                if [ -n "$ORCH_METHOD_META" ]; then
                    ORCH_METHOD_META_JSON="${ORCH_METHOD_META#WRIT_META:}"
                    ORCH_METHOD_FIELDS=$(echo "$ORCH_METHOD_META_JSON" | parse_writ_meta)
                    ORCH_METHOD_RULE_IDS=$(echo "$ORCH_METHOD_FIELDS" | sed -n '1p'); ORCH_METHOD_RULE_IDS="${ORCH_METHOD_RULE_IDS:-[]}"
                    ORCH_METHOD_COST=$(echo "$ORCH_METHOD_FIELDS" | sed -n '2p'); ORCH_METHOD_COST="${ORCH_METHOD_COST:-0}"

                    if [ "$ORCH_METHOD_RULE_IDS" != "[]" ]; then
                        _writ_session update "$SESSION_ID" \
                            --add-rules "$ORCH_METHOD_RULE_IDS" \
                            --cost "$ORCH_METHOD_COST" \
                            --inc-queries 2>>"$WRIT_HOOK_LOG_SINK" || true
                    fi

                    log_rag_query_event "$SESSION_ID" "${CURRENT_MODE:-}" "methodology" "$ORCH_METHOD_COST" "$ORCH_METHOD_RULE_IDS" "$EFFORT" "UserPromptSubmit" "stdout"
                fi
            fi
        fi
    fi

    exit 0
fi

# 2. Minimum query length gate
if [ ${#PROMPT} -lt $MIN_QUERY_LENGTH ]; then
    debug "skipped: prompt too short (${#PROMPT} < $MIN_QUERY_LENGTH)"
    exit 0
fi

# 3-8 (#8): broad /query + always-on + methodology-companion are retrieved, parsed,
# rendered, cache-updated, and friction-logged server-side in ONE warm call
# (POST /prompt-bundle), dropping ~16 cold python3 spawns + 2 curls from the hot path
# (measured ~646ms -> ~300ms). Retrieval already required the daemon, so daemon-down
# degrades exactly as before. The endpoint returns the three rendered pieces SEPARATELY
# so they keep their legacy emit order around the bash-side mode reminders (step 9b).
case "${WRIT_ALWAYS_ON_FILTER:-1}" in 1|on|true|yes) _AO_FILTER_BOOL=true ;; *) _AO_FILTER_BOOL=false ;; esac
BUNDLE_REQUEST=$(WRIT_SID="$SESSION_ID" WRIT_MODE="${CURRENT_MODE:-}" WRIT_PROMPT="$PROMPT" WRIT_EFFORT="$EFFORT" WRIT_AOF="$_AO_FILTER_BOOL" python3 -c "
import os, json
print(json.dumps({
    'session_id': os.environ['WRIT_SID'],
    'mode': os.environ.get('WRIT_MODE', ''),
    'prompt': os.environ.get('WRIT_PROMPT', ''),
    'effort': os.environ.get('WRIT_EFFORT', ''),
    'always_on_filter': os.environ.get('WRIT_AOF', 'true') == 'true',
}))" 2>/dev/null)

# writ_http_post: curl when present (unchanged fast path and unchanged budgets),
# stdlib urllib when it is not, so a curl-less machine still gets its rules.
BUNDLE=$(WRIT_HTTP_CONNECT_TIMEOUT=0.5 WRIT_HTTP_TIMEOUT=3 \
    writ_http_post "http://${WRIT_HOST}:${WRIT_PORT}/prompt-bundle" "$BUNDLE_REQUEST" 2>/dev/null) || true

if [ -z "$BUNDLE" ]; then
    debug "failed: empty /prompt-bundle response"
    echo "[Writ: server unavailable, proceeding without rules]"
    exit 0
fi

# The error field, read through the jq-first/python-fallback helper. This was a raw
# `jq -r` whose `|| true` guard plus a default-to-failed expansion meant an absent jq
# yielded an empty string that became "1": a perfectly healthy daemon response was
# reported as "query failed" and rule injection was disabled for the WHOLE session,
# with a message blaming the server. Empty now correctly means "no error".
BUNDLE_ERR=$(parsed_field "$BUNDLE" "error")
# JSON truthiness, which is what the old `if .error then` tested: the endpoint sets
# error=false on EVERY healthy response and only true (or a string) on a real failure,
# so the falsy spellings of both extraction arms (jq "false"/"null", python
# "False"/"None") must read as "no error". A non-empty check alone would report every
# healthy bundle as failed -- the same class of defect as the default it replaces.
case "$BUNDLE_ERR" in
    ""|false|False|null|None|0) BUNDLE_ERR="" ;;
esac
if [ -n "$BUNDLE_ERR" ]; then
    debug "failed: error in /prompt-bundle response ($BUNDLE_ERR)"
    echo "[Writ: query failed, proceeding without rules]"
    exit 0
fi

# Each rendered piece via parsed_field: still jq-first on the hot path (~1-2ms), with the
# python3 fallback when jq is absent, and multi-line safe on both arms (these blocks are
# multi-line). Missing/null yields the empty default, which every consumer below treats
# as "nothing to inject".
AO_BLOCK=$(parsed_field "$BUNDLE" "always_on_block")
RULES_TEXT=$(parsed_field "$BUNDLE" "rules_text")
METHOD_BLOCK=$(parsed_field "$BUNDLE" "methodology_block")
NUDGE=$(parsed_field "$BUNDLE" "nudge")
# REMAINING_BUDGET (from the single $CACHE read at step 1c) still gates the
# review-feedback push below; the endpoint owns the channel budgets itself.
REMAINING_BUDGET=$(parsed_field "$CACHE" "remaining_budget"); REMAINING_BUDGET="${REMAINING_BUDGET:-8000}"

# Friction logging stays CLIENT-SIDE so the per-project friction-log resolution
# (cwd-relative) is preserved -- the daemon (fixed cwd) must not own it, or a real
# project's rag_query/always_on_inject telemetry would land in the writ dir. ONE builder
# spawn turns the bundle meta into JSON lines (same events/fields/delivery tags as the
# prior per-channel log_rag_query_event / always_on_inject) and pipes them to the
# canonical friction-append.py writer (single-source path resolution; no inline marker-walk).
printf '%s' "$BUNDLE" | WRIT_SID="$SESSION_ID" WRIT_MODE="${CURRENT_MODE:-}" WRIT_EFFORT="$EFFORT" python3 -c "
import json, os, sys
try:
    b = json.load(sys.stdin)
except Exception:
    sys.exit(0)
sid = os.environ.get('WRIT_SID', '')
mode = os.environ.get('WRIT_MODE', '') or None
effort = os.environ.get('WRIT_EFFORT', '')
def rag(src, meta):
    e = {'session': sid, 'mode': mode, 'event': 'rag_query', 'query_source': src,
         'tokens_injected': int(meta.get('cost', 0)),
         'rules_returned_count': len(meta.get('rule_ids', [])), 'rule_ids': meta.get('rule_ids', [])}
    if effort:
        e['effort'] = effort
    e['event_name'] = 'UserPromptSubmit'; e['mechanism'] = 'stdout'
    return e
lines = []
bm = b.get('broad_meta')
if bm is not None:
    lines.append(rag('broad', bm))
ao = b.get('ao_meta')
if ao is not None and int(ao.get('tokens', 0)) > 0:
    lines.append({'session': sid, 'mode': mode, 'event': 'always_on_inject',
                  'tokens': int(ao.get('tokens', 0)), 'rule_count': int(ao.get('count', 0)),
                  'rule_ids': ao.get('rule_ids') or [],
                  'event_name': 'UserPromptSubmit', 'mechanism': 'stdout'})
mm = b.get('method_meta')
if mm is not None:
    lines.append(rag(mm.get('query_source', ''), mm))
for e in lines:
    print(json.dumps(e))
" 2>>"$WRIT_HOOK_LOG_SINK" | python3 "$FA" --stdin-jsonl 2>>"$WRIT_HOOK_LOG_SINK" || true

# 8a. Always-on bundle (rendered + token-tracked server-side; friction logged above).
if [ -n "$AO_BLOCK" ]; then
    echo "$AO_BLOCK"
    echo
    debug "injected always-on bundle"
fi

# 8. Broad rules (rendered server-side; cache + friction already applied).
if [ -n "$RULES_TEXT" ]; then
    echo "$RULES_TEXT"
    debug "injected rules"
fi

# 9. Inject mode classification directive if no mode set yet
if [ -z "$CURRENT_MODE" ]; then
    emit_mode_directive "$SESSION_HELPER" "$SESSION_ID"
    debug "injected mode classification directive"
fi

# 9b. Inject mode-specific reminders
case "$CURRENT_MODE" in
    conversation)
        echo ""
        echo "[Writ: Conversation mode. Rules injected as context. No code generation expected.]"
        debug "injected conversation mode reminder"
        ;;
    debug)
        echo ""
        echo "[Writ: Debug mode. Rules injected for investigation. No code generation -- recommend Work mode when fix is identified.]"
        debug "injected debug mode reminder"
        ;;
    review)
        echo ""
        echo "[Writ: Review mode. Evaluate code against injected rules. Output structured findings per file.]"
        debug "injected review mode reminder"
        ;;
    work)
        # Work mode: inject workflow reminder based on gate state
        _PROJECT_ROOT=$(detect_project_root "$(pwd -P)")

        if [ -n "$_PROJECT_ROOT" ]; then
            _GATE_DIR="$_PROJECT_ROOT/.claude/gates"
            _PHASE_A="$_GATE_DIR/phase-a.approved"
            _TEST_SKEL="$_GATE_DIR/test-skeletons.approved"

            if [ ! -f "$_PHASE_A" ]; then
                echo ""
                echo "[Writ: Work mode -- plan gate pending. Enter /plan, write plan.md, exit, present, wait for approval.]"
                debug "injected work mode state (plan)"
            elif [ ! -f "$_TEST_SKEL" ]; then
                echo ""
                echo "[Writ: Work mode -- test-skeletons gate pending. Write test files to disk, present, wait for approval.]"
                debug "injected work mode state (test-skeletons)"
            fi
        fi
        ;;
esac

# 10. Append proposal nudge if low relevance (only when tier is set -- don't mix directives)
if [ "$NUDGE" = "NO_RULES" ]; then
    echo ""
    echo "[Writ: no matching rules found for this task. If you discover a pattern, constraint, or gotcha during this work that would help future tasks, propose it via POST /propose. See HANDBOOK.md for the format and trigger conditions.]"
elif [ "$NUDGE" = "LOW_SCORES" ]; then
    echo ""
    echo "[Writ: retrieved rules have low relevance scores (< 0.3). The knowledge base may not cover this area well. If you discover a pattern worth codifying, propose it via POST /propose.]"
fi

# 11c (#8): the methodology companion block was retrieved, rendered, cache-updated,
# and friction-logged server-side by /prompt-bundle; emit it here in its legacy
# position (after the mode reminders + proposal nudge).
if [ -n "$METHOD_BLOCK" ]; then
    echo ""
    echo "$METHOD_BLOCK"
fi

# 1.8b push-by-action (review-feedback): when the incoming prompt signals review
# feedback / a correction, re-surface SKL-PROC-REVRECV-001 -- but only OUTSIDE
# review mode, where it is NOT floored (in review mode the companion block above
# already injected it). Conservative phrase match to avoid false fires.
if [ -n "${PROMPT:-}" ] && [ "${CURRENT_MODE:-}" != "review" ] && [ "${REMAINING_BUDGET:-0}" -gt 600 ]; then
    if echo "$PROMPT" | grep -iqE 'review feedback|code review|reviewer|pr (comment|feedback)|requested change|addressing (the )?feedback|review comment'; then
        REVIEW_PUSH=$(writ_action_push "$SESSION_ID" "review-feedback" || true)
        if [ -n "$REVIEW_PUSH" ]; then
            echo ""
            echo "[Writ: methodology -- review-feedback]"
            echo "$REVIEW_PUSH"
        fi
    fi
fi

# 12. Escalation and backward-context checks reuse the main-path $CACHE. These
# read only invalidation_history, which is written by gate-validator hooks on
# other tool calls and never mutated within this UserPromptSubmit run, so the
# cache captured at step 3 is current (was a redundant second _writ_session read).

# Check for escalation and inject backward context
ESCALATION=$(_writ_session check-escalation "$SESSION_ID" 2>/dev/null || echo '{"needed":false}')
# jq when present, python when not: a python start is 9.5ms before it does anything, and
# `import json` adds 4.9 more, against 2.3ms for jq. Measured on this line: 13.7ms.
# The truthiness test is spelled out rather than left to jq's, because jq counts "" and 0
# and [] as true while python does not, and this decides whether escalation fires.
ESC_NEEDED=$(printf '%s' "$ESCALATION" | json_transform \
    'if (.needed) != null and (.needed) != false and (.needed) != "" and (.needed) != 0 and (.needed) != [] and (.needed) != {} then "yes" else "no" end' \
    "'yes' if d.get('needed') else 'no'" 2>/dev/null || true)
# Both arms print NOTHING on malformed input, where the old inline python exited non-zero
# and the `|| echo no` supplied the default. Restoring that default explicitly, because an
# empty ESC_NEEDED would compare unequal to "yes" by luck rather than by decision.
[ -n "$ESC_NEEDED" ] || ESC_NEEDED="no"

if [ "$ESC_NEEDED" = "yes" ]; then
    ESC_GATE=$(echo "$ESCALATION" | python3 -c "import sys,json; print(json.load(sys.stdin).get('gate','?'))" 2>/dev/null)
    ESC_DIAG=$(echo "$ESCALATION" | python3 -c "import sys,json; print(json.load(sys.stdin).get('diagnosis','?'))" 2>/dev/null)
    ESC_CYCLES=$(echo "$ESCALATION" | python3 -c "import sys,json; print(json.load(sys.stdin).get('cycles',0))" 2>/dev/null)

    # Build failure history from invalidation records
    FAILURE_HISTORY=$(python3 "$WRIT_DIR/bin/lib/writ_render_failure_history.py" "$CACHE" "$ESC_GATE" "$ESC_DIAG" 2>/dev/null)

    cat << ESCALATION_MSG

[Writ: ESCALATION -- ${ESC_GATE} invalidated ${ESC_CYCLES} times]

Failure history:
${FAILURE_HISTORY}

User action needed: review the rule definitions or re-scope the task.
Do NOT proceed with automated work until the user responds.
ESCALATION_MSG
    debug "injected escalation for $ESC_GATE ($ESC_DIAG, $ESC_CYCLES cycles)"

    # C10: Post enriched negative feedback (once per escalation)
    ESC_FB_SENT=$(echo "$ESCALATION" | python3 -c "import sys,json; d=json.load(sys.stdin); print('yes' if d.get('feedback_sent') else 'no')" 2>/dev/null || echo "no")
    if [ "$ESC_FB_SENT" != "yes" ]; then
        python3 "$WRIT_DIR/bin/lib/writ_send_escalation_feedback.py" "$CACHE" "$ESC_GATE" 2>>"$WRIT_HOOK_LOG_SINK" || true

        # Mark feedback as sent in escalation
        python3 "$SESSION_HELPER" update "$SESSION_ID" --set-escalation-feedback-sent 2>>"$WRIT_HOOK_LOG_SINK" || true
        debug "sent enriched negative feedback for escalation"
    fi

    exit 0
fi

# 13. Check for gate invalidation (backward context without escalation)
# Only relevant in Work mode
if [ "$CURRENT_MODE" = "work" ]; then
    if [ -n "$_PROJECT_ROOT" ]; then
        _GATE_DIR="${_GATE_DIR:-$_PROJECT_ROOT/.claude/gates}"

        # Check if any gate was invalidated (records exist but .approved file missing)
        BACKWARD_CTX=$(python3 "$WRIT_DIR/bin/lib/writ_render_backward_context.py" "$CACHE" "$_GATE_DIR" 2>/dev/null)

        if [ -n "$BACKWARD_CTX" ]; then
            echo ""
            echo "$BACKWARD_CTX"
            debug "injected backward context for invalidated gate"
        fi
    fi
fi

exit 0
