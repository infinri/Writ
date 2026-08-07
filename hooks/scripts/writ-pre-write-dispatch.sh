#!/bin/bash
# Consolidated PreToolUse Write|Edit dispatcher
#
# Replaces check-gate-approval.sh + enforce-final-gate.sh + writ-pretool-rag.sh
# with a single HTTP call to POST /pre-write-check.
#
# On deny: emits hookSpecificOutput with deny/ask decision.
# On allow: injects RAG rules via stdout.
# Fallback: if server unreachable, calls individual checks.
#
# Hook type: PreToolUse (matcher: Write|Edit)
# Exit: always 0

SKILL_DIR="$(cd "$(dirname "$0")/../.." && pwd)"
SESSION_HELPER="$SKILL_DIR/bin/lib/writ-session.py"
source "$SKILL_DIR/bin/lib/common.sh"

# WRIT_HOOK_LOG stderr breadcrumb sink, gated by WRIT_DEBUG: /dev/null when unset,
# ${WRIT_HOOK_LOG:-/tmp/writ-hooks.log} when WRIT_DEBUG=1 (single source: common.sh).
WRIT_HOOK_LOG_SINK="$(hook_log_sink)"

# PSR-003c follow-up: capture any stderr (Python tracebacks etc.) to a
# debug log so the next time a hook traceback shows in the Claude Code
# UI we can read the actual exception. tee preserves stderr propagation
# so behavior is unchanged. Gated behind WRIT_DEBUG (default OFF): the sink
# is /dev/null unless WRIT_DEBUG=1, so no debug file is opened in production.
exec 2> >(tee -a "$(_writ_debug_enabled && echo "${WRIT_HOOK_LOG:-/tmp/writ-hook-debug.log}" || echo /dev/null)" >&2)

HOOK_START_NS=$(hook_timer_start)

# Read stdin once
STDIN_DATA=$(cat)
printf '%s' "$STDIN_DATA" | blackbox_log in writ-pre-write-dispatch

# Item 4c: ONE parse turns stdin into session_id + write context + check body. Was
# two separate calls in v1.1.0 (session_id parse, then envelope parse).
#
# jq does it in ~3ms where the python arm below needs ~15ms of interpreter startup,
# and this is the hottest gate path in the system (measured 2026-08-07: 8 python
# starts in this one hook, the most of any). Same seam as parsed_field and
# load_hook_env: jq when present, python when not, WRIT_NO_JQ to force the fallback.
# The two arms are held equal by tests/test_pre_write_parse_parity.py.
PARSED_INPUT=""
if [ -z "${WRIT_NO_JQ:-}" ] && [ -r "$SKILL_DIR/bin/lib/pre-write-parse.jq" ] \
   && command -v jq >/dev/null 2>&1; then
    PARSED_INPUT=$(printf '%s' "$STDIN_DATA" | jq -R -s -r \
        --arg skill_dir "$SKILL_DIR" -f "$SKILL_DIR/bin/lib/pre-write-parse.jq" 2>/dev/null)
fi
# Empty means the jq arm was skipped or failed. It cannot mean "parsed to nothing":
# both arms always emit three lines, the third of which is a non-empty JSON object.
[ -z "$PARSED_INPUT" ] && PARSED_INPUT=$(python3 -c "
import sys, json
raw = sys.argv[1] or '{}'
skill_dir = sys.argv[2] or ''
try:
    data = json.loads(raw)
except (ValueError, json.JSONDecodeError):
    data = {}
# Normalize to a dict before any .get(). A root that parses to a list, a string, or
# null used to raise AttributeError here, and so did a 'tool_input': null -- which
# this hook cannot distinguish from a successful empty parse: PARSED_INPUT comes back
# empty, CHECK_BODY is empty, and the hook exits 0 WITHOUT running the write gate.
# A malformed envelope must fail closed into the gate, not around it. Found
# 2026-08-07 by tests/test_pre_write_parse_parity.py comparing this arm to the jq one.
if not isinstance(data, dict):
    data = {}
sid = (data.get('agent_id') or data.get('session_id') or '').strip()
ti = data.get('tool_input', {})
if isinstance(ti, str):
    try:
        ti = json.loads(ti)
    except (ValueError, json.JSONDecodeError):
        ti = {}
if not isinstance(ti, dict):
    ti = {}
# NotebookEdit uses notebook_path (not file_path), so map it -- else the server
# gate sees an empty path and silently allows (the empty-body bypass). (#4)
file_path = ti.get('file_path') or ti.get('path') or ti.get('notebook_path') or ''
body = json.dumps({
    'session_id': sid,
    'tool_input': ti if isinstance(ti, dict) else {},
    'skill_dir': skill_dir,
    'file_path': file_path,
})
# Write-context (file path + content) for the always-on applicability filter is
# derived HERE rather than in a second python3 spawn further down: that spawn
# re-parsed this exact same STDIN_DATA for these exact fields, paying a full
# interpreter start (~26ms) on the hottest gate path to recompute what this parse
# already had in hand. Emitted on ONE line so the existing line-split contract
# below still holds.
write_ctx = ' '.join(p for p in [
    file_path,
    ti.get('content') or ti.get('new_source') or '',
    ti.get('new_string') or '',
] if p).replace('\n', ' ').replace('\r', ' ')
print(sid)
print(write_ctx)
print(body)
" "$STDIN_DATA" "$SKILL_DIR" 2>/dev/null)

SESSION_ID=$(echo "$PARSED_INPUT" | head -1)
WRITE_CTX=$(echo "$PARSED_INPUT" | sed -n 2p)
CHECK_BODY=$(echo "$PARSED_INPUT" | tail -n +3)

if [ -z "$SESSION_ID" ]; then
    SESSION_ID=$(detect_session_id "")
fi

# A11: mode for hook_execution telemetry now travels in the /pre-write-check
# response (parsed from DISPATCH_BLOB below), replacing a separate `mode get`
# daemon round-trip. The two early-exit timers below run before that response
# exists, so they log an empty mode -- telemetry metadata only, and both are
# degenerate no-write paths (empty body, or server unreachable).
MODE=""

if [ -z "$CHECK_BODY" ]; then
    hook_timer_end "$HOOK_START_NS" "writ-pre-write-dispatch" "$SESSION_ID" "${MODE:-}"
    exit 0
fi

# Single HTTP call to /pre-write-check. The helper reads the JSON body from its
# SECOND positional arg (session_id is first); passing only CHECK_BODY sent an
# empty `{}` body, so the server saw no file_path and always returned "allow" --
# a silent gate bypass that also suppressed every real write_attempt event. Pass
# SESSION_ID first so CHECK_BODY lands in $2.
RESULT=$(_writ_session pre-write-check "$SESSION_ID" "$CHECK_BODY" 2>/dev/null || echo "")

if [ -z "$RESULT" ]; then
    hook_timer_end "$HOOK_START_NS" "writ-pre-write-dispatch" "$SESSION_ID" "${MODE:-}"
    exit 0
fi

# Item 4c: single python3 spawn computes decision + reason + file_path + payload
# + hookSpecificOutput JSON + RAG metadata. Was three sequential json.load() spawns
# plus an inline hookSpecificOutput builder. Output is tab-separated lines the
# shell reads with `mapfile` to avoid further parsing spawns.
DISPATCH_BLOB=$(python3 -c "
import json, sys
result_raw = sys.argv[1] or '{}'
body_raw = sys.argv[2] or '{}'
try:
    result = json.loads(result_raw)
except (ValueError, json.JSONDecodeError):
    result = {}
try:
    body = json.loads(body_raw)
except (ValueError, json.JSONDecodeError):
    body = {}
decision = result.get('decision', 'allow') or 'allow'
reason = result.get('reason', '') or ''
file_path = body.get('file_path', '') or ''
rag_rules = result.get('rag_rules', '') or ''
rag_meta = result.get('rag_meta', {}) or {}
rule_ids = rag_meta.get('rule_ids', []) or []
tokens = rag_meta.get('tokens', 0)
# A11: mode + denial count come from the /pre-write-check envelope (was a
# separate 'mode get' and a separate cache read). The ask/deny ESCALATION
# decision is already made server-side; denial_count here is only the '#N' shown
# in the message. Default 2 so an older server without the field keeps the prior
# 'repeated #2' wording.
mode = result.get('mode', '') or ''
denial_count = str(result.get('max_denial_count', 2))

if decision == 'ask':
    hook_output = json.dumps({
        'hookSpecificOutput': {
            'hookEventName': 'PreToolUse',
            'permissionDecision': 'ask',
            'permissionDecisionReason': '[Writ: repeated gate violation #' + denial_count + '] ' + (reason or 'Gate approval required'),
        }
    })
elif decision == 'deny':
    hook_output = json.dumps({
        'hookSpecificOutput': {
            'hookEventName': 'PreToolUse',
            'permissionDecision': 'deny',
            'permissionDecisionReason': reason or 'Gate approval required',
            'additionalContext': 'IMPORTANT: This write was denied by a Writ gate. Do NOT attempt more writes to other files -- the denial applies to ALL files until the gate advances. Read the denial reason and follow the workflow: present your work to the user and wait for approval.',
        }
    })
else:
    hook_output = ''

sys.stdout.write(decision + '\n')
sys.stdout.write(file_path + '\n')
sys.stdout.write(hook_output + '\n')
sys.stdout.write(rag_rules.replace('\n', ' ') + '\n')
sys.stdout.write(json.dumps(rule_ids) + '\n')
sys.stdout.write(str(tokens) + '\n')
sys.stdout.write(mode + '\n')
" "$RESULT" "$CHECK_BODY" 2>/dev/null || echo "")

DECISION=$(echo "$DISPATCH_BLOB" | sed -n '1p')
DECISION_FILE=$(echo "$DISPATCH_BLOB" | sed -n '2p')
HOOK_OUTPUT=$(echo "$DISPATCH_BLOB" | sed -n '3p')
RAG_RULES_RAW=$(echo "$DISPATCH_BLOB" | sed -n '4p')
NEW_RULE_IDS=$(echo "$DISPATCH_BLOB" | sed -n '5p')
COST=$(echo "$DISPATCH_BLOB" | sed -n '6p')
# A11: mode from the /pre-write-check envelope -> the closing timer call (no
# separate `mode get`). Empty for the early-exit timers above (degenerate paths).
MODE=$(echo "$DISPATCH_BLOB" | sed -n '7p' | tr -d '[:space:]')

DECISION="${DECISION:-allow}"
# write_attempt (emitted by the gate, writ/session/gates.py) is the canonical
# write-decision telemetry; the bare pre_write_decision event was retired (1.3).

# 1.8b push-by-action: on a DENY, re-surface the gate-failure methodology
# (SKL-PROC-WRIT-FAILURE-001) right in the denial reason, AT the denial moment.
# Additive + fail-open: if the companion is unreachable the original deny stands.
if [ "$DECISION" = "deny" ]; then
    GATE_PUSH=$(writ_action_push "$SESSION_ID" "gate-denial" || true)
    if [ -n "$GATE_PUSH" ] && [ -n "$HOOK_OUTPUT" ]; then
        HOOK_OUTPUT=$(WRIT_HO="$HOOK_OUTPUT" WRIT_PUSH="$GATE_PUSH" python3 -c "
import json, os
try:
    ho = json.loads(os.environ['WRIT_HO'])
    hso = ho.get('hookSpecificOutput', {})
    base = hso.get('permissionDecisionReason', '') or ''
    hso['permissionDecisionReason'] = base + '\n\n[Writ: methodology -- gate-denial]\n' + os.environ['WRIT_PUSH']
    ho['hookSpecificOutput'] = hso
    print(json.dumps(ho))
except Exception:
    print(os.environ.get('WRIT_HO', ''))
" 2>/dev/null || echo "$HOOK_OUTPUT")
    fi
fi

if [ "$DECISION" = "deny" ] || [ "$DECISION" = "ask" ]; then
    [ -n "$HOOK_OUTPUT" ] && echo "$HOOK_OUTPUT"
    printf '%s' "$HOOK_OUTPUT" | blackbox_log out writ-pre-write-dispatch "$SESSION_ID"
else
    # Applicability-scoped always-on (WRIT-BLUEPRINT 3.5), flag-gated. The write-scoped
    # rules deferred off the per-prompt channel inject HERE, at the write moment, when the
    # file path + content match their trigger_keywords. Same plain-stdout mechanism as the
    # file-context rules below. Off => no fetch, no behavior change.
    AO_WRITE_BLOCK=""
    # Applicability filter DEFAULT ON (#N2): write-time injection now delivers via
    # hookSpecificOutput.additionalContext (#2 -- the allow-path block below was moved off bare
    # stdout), so the deferred write-scoped rules reach the model at the write moment. Parity
    # verified: every always-on rule is reachable (prompt-active or write-keyword). Disable with
    # WRIT_ALWAYS_ON_FILTER=0.
    case "${WRIT_ALWAYS_ON_FILTER:-1}" in 1|on|true|yes) _AO_FILTER=1 ;; *) _AO_FILTER="" ;; esac
    if [ -n "$_AO_FILTER" ]; then
        # WRITE_CTX already computed by the single consolidated parse above.
        AO_WRITE_JSON=$(curl -s -G --connect-timeout 0.3 --max-time 1 \
            --data-urlencode "mode=${MODE:-universal}" \
            --data-urlencode "at=write" \
            --data-urlencode "context=${WRITE_CTX}" \
            "http://${WRIT_SESSION_HOST}:${WRIT_SESSION_PORT}/always-on" 2>/dev/null) || true
        AO_WRITE_BLOCK=$(printf '%s' "$AO_WRITE_JSON" | python3 -c "
import json, sys
try:
    d = json.load(sys.stdin)
except Exception:
    raise SystemExit
rules = d.get('rules') or []
if rules:
    out = ['=== APPLICABLE RULES (this write) ===']
    for r in rules:
        rid = r.get('rule_id', ''); trig = (r.get('trigger') or '').strip(); stmt = (r.get('statement') or '').strip()
        if rid and trig and stmt:
            out.append(f'[{rid}] WHEN: {trig}')
            out.append(f'  {stmt}')
    out.append('=== END APPLICABLE RULES ===')
    print('\n'.join(out))
" 2>/dev/null)
    fi
    # RAG_RULES_RAW arrives flattened (newlines collapsed to spaces) so the
    # 6-field transport above stays single-line per field. Render as-is.
    if [ -n "$RAG_RULES_RAW" ] || [ -n "$AO_WRITE_BLOCK" ]; then
        # #2: deliver via additionalContext. The allow-path bare stdout reached
        # only the CC debug log on PreToolUse (verified delivery rule); now the
        # file-context + write-scoped rules reach the model. Additive, no
        # permissionDecision -> does not touch the write gate (deny path above).
        WRIT_AC="[Writ: file-context rules for $(basename "${DECISION_FILE:-unknown}")]
${RAG_RULES_RAW}
${AO_WRITE_BLOCK}" python3 <<'PY' 2>>"$WRIT_HOOK_LOG_SINK" || true
import json, os
print(json.dumps({"hookSpecificOutput": {
    "hookEventName": "PreToolUse",
    "additionalContext": os.environ.get("WRIT_AC", ""),
}}))
PY
        printf '%s' "${RAG_RULES_RAW}${AO_WRITE_BLOCK}" | blackbox_log out writ-pre-write-dispatch "$SESSION_ID"
    fi
    if [ -n "$NEW_RULE_IDS" ] && [ "$NEW_RULE_IDS" != "[]" ]; then
        _writ_session update "$SESSION_ID" \
            --add-rules "$NEW_RULE_IDS" \
            --cost "${COST:-0}" \
            --inc-queries 2>/dev/null || true
        if [ -n "$DECISION_FILE" ]; then
            _writ_session update "$SESSION_ID" \
                --add-queried-rules-for-file "$DECISION_FILE" "$NEW_RULE_IDS" 2>/dev/null || true
        fi
        # Delivery telemetry (#7): the allow-path file-context rules emit via bare
        # stdout on PreToolUse, so classify_delivery buckets them as debug-log
        # (INERT) today. Logging them makes that waste visible in
        # `writ analyze-friction`; flip mechanism to additionalContext when #2
        # moves this emit into hookSpecificOutput.
        log_rag_query_event "$SESSION_ID" "${MODE:-}" "file-write-pre" "${COST:-0}" "$NEW_RULE_IDS" "" "PreToolUse" "additionalContext"
    fi
fi

hook_timer_end "$HOOK_START_NS" "writ-pre-write-dispatch" "$SESSION_ID" "${MODE:-}"
exit 0
