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

# This project's root, computed ONCE for the whole hook and used by two consumers:
# the retrieval requests below send it so the daemon can scope them to this project
# (writ/retrieval/node_scope.py), and step 9b reads this session's gate directory
# under it. It is hoisted here rather than computed per consumer because
# detect_project_root is pure bash, so one call costs nothing and two copies of the
# same expression would be free to drift.
_PROJECT_ROOT=$(detect_project_root "$(pwd -P)")

# Fallback session ID if not provided by Claude Code
# NO SYNTHESIZED SESSION ID. This used to fall back to the parent PID and then to
# md5(cwd:user)+date. Neither can ever equal the id Claude Code uses, so state written
# under one is written to a session that does not exist and is simply never read again,
# while the hook reports success. Claude Code documents session_id as universal and
# authoritative on every hook event, so an empty one is a broken invariant, not a case to
# paper over: record it and stop.
if [ -z "$SESSION_ID" ]; then
    writ_critical writ-rag-inject "no session_id in hook payload; refusing to synthesize one"
    exit 0
fi

# Publish the session id for the callers that have NO hook payload to read one from.
#
# THE STALE COMMENT THIS REPLACES SAID "so Stop hooks (friction-logger) can find it", and
# that reader is gone: every hook now takes its identity from its own payload, because
# this file names whichever session on this machine took a turn most recently and reading
# it as "who am I" was silently wrong (it held cdp-12096297 while the live session was
# another id, and a drain keyed off it stranded 999 rows across 16 sessions).
#
# THE WRITE STAYS, because four callers still read it and none of them HAS a payload:
#   writ/session/cache.py:94   resolve_current_session_id() step 3, behind $CLAUDE_SESSION_ID
#                              and $CLAUDE_JOB_DIR -- serves `writ mode set <mode>` with no
#                              sid (cli_dispatch.py:122) and `writ doctor` (doctor.py:482)
#   hooks/git/post-commit:29   a git hook; git passes no Claude Code envelope at all
#   bin/audit-region.sh:27     a CLI tool, when --session is omitted
#   session-start-bootstrap.sh:112  reads the PRE-rotation id, which by definition is not
#                              in the payload (the payload carries the NEW one)
# Deleting the write would not remove the risk, it would move it: those four would fall
# through to the mtime glob (cache.py step 4), which is strictly racier. Re-check this
# list before removing the write.
#
# Do NOT overwrite when inside a sub-agent -- that would publish the child's id as the
# session, and the readers above want the top-level one.
if [ -z "$AGENT_ID" ]; then
    echo "$SESSION_ID" > /tmp/writ-current-session
fi

debug "session=$SESSION_ID prompt_len=${#PROMPT}"

# 1. Check skip conditions (budget exhausted or context pressure > 75%)
#
# ONE call answers this AND supplies the session cache read at step 1c. They used to be
# two `_writ_session` invocations, each paying a python interpreter start (9.5ms floor)
# plus a round trip plus its own read of the SAME cache file. /session/{id}/prompt-state
# returns should_skip, known, escalation and the full cache from a single read.
#
# Falling back to the separate calls when this comes back empty keeps a stale daemon
# (one without the route) working rather than silently skipping every prompt.
PROMPT_STATE=$(writ_http_get "http://${WRIT_HOST}:${WRIT_PORT}/session/${SESSION_ID}/prompt-state" 2>/dev/null || true)
PS_SKIP=""
if [ -n "$PROMPT_STATE" ]; then
    # PRESENCE FIRST, then the value. Testing the value alone maps an ERROR body to "no":
    # a daemon too old to have this route answers `{"detail":"Not Found"}`, `.should_skip`
    # is absent, and `== true` is false, so the hook would read a 404 as "do not skip" and
    # never consult the fallback. A session whose budget was exhausted would keep
    # injecting for the rest of its life. Emitting nothing when the field is missing is
    # what routes it to the separate call below.
    PS_SKIP=$(printf '%s' "$PROMPT_STATE" | json_transform \
        'if has("should_skip") then (if .should_skip == true then "yes" else "no" end) else empty end' \
        "(('yes' if d.get('should_skip') is True else 'no') if 'should_skip' in d else None)" \
        2>/dev/null || true)
fi
if [ -n "$PS_SKIP" ]; then
    if [ "$PS_SKIP" = "yes" ]; then
        debug "skipped: budget or context pressure"
        exit 0
    fi
elif _writ_session should-skip "$SESSION_ID" 2>/dev/null; then
    debug "skipped: budget or context pressure (fallback path)"
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
  # Empty means "no restore happened", which is the truth on every path except the
  # investigate -> work switch below. Initialized here because the hook runs under `set -u`.
  RESTORED_GATES=""
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
  else
    # Mid-session re-route. The hint used to be computed every turn and then thrown away
    # once ANY mode existed, so five weeks of logs hold zero switch rows: mid-work, a
    # discovery that needed investigating could never start an investigation.
    #
    # `mode switch`, NEVER `mode set`: set runs _apply_mode_set, which clears
    # gates_approved and paused_work_state, so routing a misclassified prompt through it
    # would destroy an approved plan and approved tests. switch SAVES them, which makes a
    # false positive cost a detour instead of the approvals.
    #
    # Only between work and investigate, the two modes the classifier emits. An explicitly
    # chosen debug / review / conversation mode is the user's and stays: flipping a debug
    # session to work would fire the debug-to-work root-cause handoff as a side effect of
    # a guess.
    case "$PRIOR_MODE:$MODE_HINT" in
      work:investigate|investigate:work)
        python3 "$SESSION_HELPER" mode switch "$MODE_HINT" "$SESSION_ID" >/dev/null 2>&1 || true
        # Re-read rather than trust the hint, for the same reason the unset path does:
        # announcing the mode we ASKED for is how the hook came to tell the user the mode
        # was 'work' while the cache said otherwise.
        CONFIRMED_MODE=$(writ_session_mode_direct "$SESSION_ID")
        if [ -n "$CONFIRMED_MODE" ]; then
          CURRENT_MODE="$CONFIRMED_MODE"
          [ "$CONFIRMED_MODE" = "$MODE_HINT" ] && AUTOROUTED="yes"
        fi
        # A switch back into work either RESTORES the gates approved before the detour
        # (plan.md unchanged) or re-arms to planning (plan.md pivoted), and the two need
        # different messages: telling a user whose approvals were just restored to go write
        # plan.md and present it for approval sends them to redo work the cache still holds.
        # Read the count off the cache rather than guessing which branch ran, so the
        # announcement can never claim an approval the session does not actually have.
        if [ "$MODE_HINT" = "work" ]; then
          SWITCH_CACHE_FILE="$(writ_session_cache_dir)/writ-session-$SESSION_ID.json"
          # Tested before the redirect: `< missing` fails the command outright and the
          # shell reports it before any 2>/dev/null on the same line can take effect, so a
          # cacheless session would print a hook error for a state that is simply "nothing
          # to restore".
          if [ -f "$SWITCH_CACHE_FILE" ]; then
            RESTORED_GATES=$(json_transform '.gates_approved | length' \
              "len(d.get('gates_approved') or [])" < "$SWITCH_CACHE_FILE" 2>/dev/null || true)
          fi
        fi
        debug "mid-session re-route $PRIOR_MODE -> $MODE_HINT; mode is now '${CONFIRMED_MODE:-unset}'"
        ;;
    esac
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
    elif [ -n "$RESTORED_GATES" ] && [ "$RESTORED_GATES" != "0" ]; then
      # The switch restored a paused work cycle: plan.md was unchanged, so the approvals
      # granted before the detour still stand. Say that, and say nothing about writing a
      # plan; the count comes from the cache, so it cannot overstate what is approved.
      cat << WORKRESTORE

[Writ: implementation request -> paused work mode restored automatically]
This reads as a build/implementation task, so the mode is back to 'work'. The plan did not
change during the detour, so the paused phase and $RESTORED_GATES already-approved gate(s)
were restored with it. Continue that cycle: do not rewrite plan.md and do not re-request an
approval you already hold. If this is a trivial edit that needs no workflow, override with:
  python3 $SESSION_HELPER mode set conversation $SESSION_ID
WORKRESTORE
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
# Reuse the cache that came back with the skip answer above, which is a CONSISTENT
# snapshot rather than two reads that can straddle a concurrent write.
#
# EXCEPT when the auto-route block just set a mode: that snapshot predates the write, and
# this read is positioned here precisely so a just-set mode is reflected. A stale mode
# here is not a cosmetic bug, it decides whether the gates arm for the whole turn, so the
# rare path re-reads rather than reusing.
CACHE=""
if [ -n "$PROMPT_STATE" ] && [ "${AUTOROUTED:-no}" != "yes" ]; then
    CACHE=$(printf '%s' "$PROMPT_STATE" | json_transform '.cache' "d.get('cache')" 2>/dev/null || true)
fi
[ -n "$CACHE" ] || CACHE=$(_writ_session read "$SESSION_ID" 2>/dev/null || echo '{"loaded_rule_ids":[],"remaining_budget":8000}')
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
    ORCH_REMAINING_BUDGET=$(echo "$CACHE_DATA" | json_transform 'if (.remaining_budget // null) == null then 8000 else .remaining_budget end' "(8000 if d.get('remaining_budget') is None else d.get('remaining_budget'))" 2>/dev/null || echo '8000')
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
    'project_root': sys.argv[3],
}))
" "$PROMPT" "$ORCH_LOADED_RULE_IDS" "${_PROJECT_ROOT:-}" 2>/dev/null)

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
# jq builds this request when present: five strings and a boolean assembled from
# variables already in the shell cost a 9.5ms interpreter start plus 4.9 for `import
# json`, against 2.3 for jq. --arg is used for every value so a prompt containing quotes,
# newlines or backslashes is encoded by jq rather than by string concatenation here.
# always_on_filter must stay a JSON BOOLEAN, not the string "true", so it goes through
# --argjson after being normalised to a bare true/false above.
BUNDLE_REQUEST=""
if [ -z "${WRIT_NO_JQ:-}" ] && command -v jq >/dev/null 2>&1; then
    BUNDLE_REQUEST=$(jq -n -c \
        --arg session_id "$SESSION_ID" \
        --arg mode "${CURRENT_MODE:-}" \
        --arg prompt "$PROMPT" \
        --arg effort "$EFFORT" \
        --arg project_root "${_PROJECT_ROOT:-}" \
        --argjson always_on_filter "$_AO_FILTER_BOOL" \
        '{session_id: $session_id, mode: $mode, prompt: $prompt, effort: $effort, project_root: $project_root, always_on_filter: $always_on_filter}' \
        2>/dev/null) || BUNDLE_REQUEST=""
fi
if [ -z "$BUNDLE_REQUEST" ]; then
    BUNDLE_REQUEST=$(WRIT_SID="$SESSION_ID" WRIT_MODE="${CURRENT_MODE:-}" WRIT_PROMPT="$PROMPT" WRIT_EFFORT="$EFFORT" WRIT_AOF="$_AO_FILTER_BOOL" WRIT_PROOT="${_PROJECT_ROOT:-}" python3 -c "
import os, json
print(json.dumps({
    'session_id': os.environ['WRIT_SID'],
    'mode': os.environ.get('WRIT_MODE', ''),
    'prompt': os.environ.get('WRIT_PROMPT', ''),
    'effort': os.environ.get('WRIT_EFFORT', ''),
    'project_root': os.environ.get('WRIT_PROOT', ''),
    'always_on_filter': os.environ.get('WRIT_AOF', 'true') == 'true',
}))" 2>/dev/null)
fi

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
# ONE pass over the bundle for every field the rest of this hook needs.
#
# These were 5 separate parsed_field calls, each piping the WHOLE ~10KB response (it
# carries the full always-on rule text) into a fresh jq: 50KB of piping and 5 interpreter
# starts to read 5 strings. Measured ~38ms on the prompt path, second only to the HTTP
# request that fetched the data. $BUNDLE is assigned once above and never reassigned, so
# one parse here is valid for every consumer below.
eval "$(parsed_fields "$BUNDLE" \
    BUNDLE_ERR=error \
    AO_BLOCK=always_on_block \
    RULES_TEXT=rules_text \
    METHOD_BLOCK=methodology_block \
    NUDGE=nudge)"
# parsed_fields emits nothing when the document is unparseable, leaving these unset under
# `set -u`. Defaulting them keeps that case identical to parsed_field's empty default.
BUNDLE_ERR="${BUNDLE_ERR-}"
AO_BLOCK="${AO_BLOCK-}"
RULES_TEXT="${RULES_TEXT-}"
METHOD_BLOCK="${METHOD_BLOCK-}"
NUDGE="${NUDGE-}"
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
# AO_BLOCK / RULES_TEXT / METHOD_BLOCK / NUDGE were read in the single parsed_fields
# pass above, alongside BUNDLE_ERR. Re-reading them here was 4 more jq starts over the
# same 10KB document.
# REMAINING_BUDGET (from the single $CACHE read at step 1c) still gates the
# review-feedback push below; the endpoint owns the channel budgets itself.
REMAINING_BUDGET=$(parsed_field "$CACHE" "remaining_budget"); REMAINING_BUDGET="${REMAINING_BUDGET:-8000}"

# Friction logging stays CLIENT-SIDE so the per-project friction-log resolution
# (cwd-relative) is preserved -- the daemon (fixed cwd) must not own it, or a real
# project's rag_query/always_on_inject telemetry would land in the writ dir. ONE builder
# spawn turns the bundle meta into JSON lines (same events/fields/delivery tags as the
# prior per-channel log_rag_query_event / always_on_inject) and pipes them to the
# canonical friction-append.py writer (single-source path resolution; no inline marker-walk).
# jq builds these rows when present. The python arm below is unchanged and still runs when
# jq is absent, which is the WRIT_NO_JQ seam every other conversion on this path uses:
# absence changes speed, never behaviour. Measured 16.9ms for the interpreter start against
# 2.3 for jq, to turn three metadata objects into up to three JSON lines.
#
# THE SENTINEL IS jq's EXIT STATUS, NOT ITS OUTPUT. A bundle with no metadata legitimately
# produces ZERO rows, and treating empty output as failure would spawn python to rediscover
# that there is nothing to emit: the exact pattern removed twice already this cycle (the
# dead mktemp, and the renderer that printed nothing on every prompt).
#
# These rows are AUDIT records (rag_query, always_on_inject), so parity is asserted on the
# PARSED objects across every bundle shape the endpoint produces, not on the text.
FRICTION_ROWS=""
_FRICTION_ROWS_OK=""
if [ -z "${WRIT_NO_JQ:-}" ] && command -v jq >/dev/null 2>&1 \
        && [ -r "$WRIT_DIR/bin/lib/friction-rows.jq" ]; then
    if FRICTION_ROWS=$(printf '%s' "$BUNDLE" | jq -R -s -r \
            --arg sid "$SESSION_ID" --arg mode "${CURRENT_MODE:-}" --arg effort "$EFFORT" \
            -f "$WRIT_DIR/bin/lib/friction-rows.jq" 2>>"$WRIT_HOOK_LOG_SINK"); then
        _FRICTION_ROWS_OK=1
    else
        FRICTION_ROWS=""
    fi
fi
if [ -z "$_FRICTION_ROWS_OK" ]; then
FRICTION_ROWS=$(printf '%s' "$BUNDLE" | WRIT_SID="$SESSION_ID" WRIT_MODE="${CURRENT_MODE:-}" WRIT_EFFORT="$EFFORT" python3 -c "
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
" 2>>"$WRIT_HOOK_LOG_SINK") || true
fi
# The builder's rows used to be piped straight into friction-append.py, a SECOND
# interpreter start whose only job was appending them. Measured 35.3ms on the prompt path,
# and almost none of it work: the interpreter floor is 9.5ms and `import
# writ.shared.logging` adds 12.5 more (52 modules) to append a line. The rows go to the
# session event buffer instead (a bash append, no process) and the once-per-turn drain
# emits them through the SAME writ.shared.logging router, so classification, path
# resolution and the durable fallback are unchanged.
if [ -n "$FRICTION_ROWS" ]; then
    FRICTION_OVERSIZED=""
    while IFS= read -r _frow; do
        [ -n "$_frow" ] || continue
        # A row too large to append atomically returns 1 rather than being truncated:
        # truncated JSON is unparseable, so the drain would drop it, turning a slow row
        # into a lost one. Those take the original spawn, which is what this collects.
        writ_friction_buffer_append "$SESSION_ID" "$_frow" \
            || FRICTION_OVERSIZED="${FRICTION_OVERSIZED}${_frow}"$'\n'
    done <<< "$FRICTION_ROWS"
    if [ -n "$FRICTION_OVERSIZED" ]; then
        printf '%s' "$FRICTION_OVERSIZED" \
            | python3 "$FA" --stdin-jsonl 2>>"$WRIT_HOOK_LOG_SINK" || true
    fi
fi

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
        # Work mode: inject workflow reminder based on gate state. _PROJECT_ROOT is
        # computed once near the top of the hook (the retrieval requests need it too).
        # THIS SESSION's own gate directory, never the project-wide one: a flat
        # phase-a.approved from any session in the repo used to silence this reminder for a
        # session that had approved nothing, and told it "test-skeletons gate pending",
        # which reads as progress it never made. An empty answer (no project root, or a
        # session id that cannot be a path component) means there is nowhere to read gate
        # state from, so no reminder is printed -- the same silence a rootless project got
        # before, and never a reminder derived from another session's files.
        _GATE_DIR=$(writ_gate_dir "$_PROJECT_ROOT" "$SESSION_ID")

        if [ -n "$_GATE_DIR" ]; then
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
# ASK BEFORE SPAWNING, fourth instance of this pattern on this path. /prompt-state already
# answered "is escalation pending", from the SAME cache file /check-escalation reads, so
# when the answer is no this round trip only confirms it (measured 12.2ms).
#
# WHY THE EARLIER SNAPSHOT IS SAFE HERE, which is the part that needed checking rather than
# assuming. I previously kept this call fresh because reusing the snapshot risked missing an
# escalation that arrived mid-hook. The `escalation` field is written in exactly three
# places (writ/session/approval_workflow.py, violations.py, budget_tracking.py), and none of
# them run during a UserPromptSubmit hook: they fire on gate approvals and on write
# violations. So nothing can set it between the two reads in this hook's lifetime.
#
# The full object is still fetched when escalation IS pending, because gate, diagnosis,
# cycles and feedback_sent are read below and /prompt-state returns only the boolean.
#
# Presence first, again: a daemon without /prompt-state returns a body with no `escalation`
# key, which yields empty here and falls through to the real call rather than being read as
# "no escalation" and silently suppressing the warning.
PS_ESC=""
if [ -n "$PROMPT_STATE" ]; then
    PS_ESC=$(printf '%s' "$PROMPT_STATE" | json_transform \
        'if has("escalation") then (if .escalation == true then "yes" else "no" end) else empty end' \
        "(('yes' if d.get('escalation') is True else 'no') if 'escalation' in d else None)" \
        2>/dev/null || true)
fi
if [ "$PS_ESC" = "no" ]; then
    ESCALATION='{"needed":false}'
else
    ESCALATION=$(_writ_session check-escalation "$SESSION_ID" 2>/dev/null || echo '{"needed":false}')
fi
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
    ESC_GATE=$(echo "$ESCALATION" | json_transform 'if (.gate // null) == null then "?" else .gate end' "('?' if d.get('gate') is None else d.get('gate'))" 2>/dev/null)
    ESC_DIAG=$(echo "$ESCALATION" | json_transform 'if (.diagnosis // null) == null then "?" else .diagnosis end' "('?' if d.get('diagnosis') is None else d.get('diagnosis'))" 2>/dev/null)
    ESC_CYCLES=$(echo "$ESCALATION" | json_transform 'if (.cycles // null) == null then 0 else .cycles end' "(0 if d.get('cycles') is None else d.get('cycles'))" 2>/dev/null)

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
    ESC_FB_SENT=$(echo "$ESCALATION" | json_transform 'if (.feedback_sent) != null and (.feedback_sent) != false and (.feedback_sent) != "" and (.feedback_sent) != 0 and (.feedback_sent) != [] and (.feedback_sent) != {} then "yes" else "no" end' "('yes' if d.get('feedback_sent') else 'no')" 2>/dev/null || echo "no")
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
    # Reuses the session-scoped $_GATE_DIR section 9b already built (both blocks run only in
    # work mode, so the build happens once per prompt). Recomputed only if 9b was skipped;
    # an empty answer means no session-scoped directory to read, and the invalidation check
    # is skipped rather than pointed at the project-wide path, where another session's
    # missing artifact would read as THIS session's gate invalidation.
    # ${_PROJECT_ROOT:-} because this arm is only reached when 9b was skipped, and an
    # unset variable under `set -u` would abort the whole hook rather than skip a check.
    _GATE_DIR="${_GATE_DIR:-$(writ_gate_dir "${_PROJECT_ROOT:-}" "$SESSION_ID")}"
    if [ -n "$_GATE_DIR" ]; then

        # Check if any gate was invalidated (records exist but .approved file missing)
        #
        # ASK BEFORE SPAWNING. The renderer prints nothing unless invalidation_history
        # holds a non-empty entry, which for almost every session it does not (measured 0
        # across live sessions). Discovering that cost a 19.5ms interpreter start on EVERY
        # prompt. jq answers the same question in 2.3ms from $CACHE, already in memory.
        #
        # This is a NECESSARY-condition test, not the renderer's full logic: an empty
        # history guarantees no output, while a non-empty one still has to check whether
        # each gate's .approved file is missing. So the guard can only skip work the
        # renderer would have skipped anyway.
        #
        # Fail-open: an unreadable cache leaves this empty and the default runs the
        # renderer, because losing a gate-invalidation warning is worse than a spawn.
        _HAS_INVALIDATION=$(printf '%s' "$CACHE" | json_transform \
            'if ((.invalidation_history // {}) | to_entries | map(select((.value | length) > 0)) | length) > 0 then "yes" else "no" end' \
            "('yes' if any((d.get('invalidation_history') or {}).values()) else 'no')" \
            2>/dev/null || true)
        BACKWARD_CTX=""
        if [ "${_HAS_INVALIDATION:-yes}" != "no" ]; then
            BACKWARD_CTX=$(python3 "$WRIT_DIR/bin/lib/writ_render_backward_context.py" "$CACHE" "$_GATE_DIR" 2>/dev/null)
        fi

        if [ -n "$BACKWARD_CTX" ]; then
            echo ""
            echo "$BACKWARD_CTX"
            debug "injected backward context for invalidated gate"
        fi
    fi
fi

exit 0
