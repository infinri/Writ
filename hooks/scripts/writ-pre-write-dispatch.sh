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

# THE SKILL DIR, RESOLVED WITHOUT A FORK. This used to be
# `SKILL_DIR="$(cd "$(dirname "$0")/../.." && pwd)"`, one `dirname` process on every
# single write for a value common.sh:10-11 recomputes from its OWN location one line
# later anyway. The old spelling was a bootstrap problem, not carelessness: the source
# below needs a path to common.sh before common.sh has told us anything. Sourcing
# through a relative path breaks that circle, and ${0%/*} is a parameter expansion,
# so it costs no process at all.
#
# ${0%/*} leaves $0 UNCHANGED when $0 holds no slash (invocation by bare name found on
# PATH). hooks.json and the suite both invoke this file by path, so that form never
# occurs here; the guard is written out rather than left implied.
case "$0" in
    */*) _WRIT_BOOTSTRAP_DIR="${0%/*}" ;;
    *)   _WRIT_BOOTSTRAP_DIR="." ;;
esac
source "$_WRIT_BOOTSTRAP_DIR/../../bin/lib/common.sh"
# _WRIT_SKILL_DIR is common.sh's single resolution of the same directory. This value is
# not decorative: it travels to the server as `skill_dir` in the /pre-write-check body
# and the gate resolves skill-dir exemptions from it, so byte identity with the retired
# spelling is ASSERTED (tests/test_write_path_process_budget.py, class
# TestSkillDirResolvedWithoutDirname), not assumed.
SKILL_DIR="$_WRIT_SKILL_DIR"
SESSION_HELPER="$SKILL_DIR/bin/lib/writ-session.py"

# WRIT_HOOK_LOG stderr breadcrumb sink, gated by WRIT_DEBUG: /dev/null when unset,
# ${WRIT_HOOK_LOG:-/tmp/writ-hooks.log} when WRIT_DEBUG=1 (single source: common.sh).
WRIT_HOOK_LOG_SINK="$(hook_log_sink)"

# PSR-003c follow-up: capture any stderr (Python tracebacks etc.) to a
# debug log so the next time a hook traceback shows in the Claude Code
# UI we can read the actual exception. tee preserves stderr propagation
# so behavior is unchanged.
#
# THE GATE IS ON THE FORK, NOT ON THE DESTINATION FILE, and this hook is deliberately
# the only one of the four that spells it this way. The previous spelling was
# unconditional: `exec 2> >(tee -a "$(_writ_debug_enabled && echo ... || echo
# /dev/null)" >&2)`. With WRIT_DEBUG unset that still forked tee, wrote every stderr
# byte into /dev/null and re-emitted the same bytes to stderr, which buys nothing
# because the file sink was already the null device. Measured before the change: one
# `tee` execve with WRIT_DEBUG unset and one with it set, 44 total execve attempts in
# both states, so the fork was paid on every write regardless of the gate. After:
# 29 attempts with debug unset (no tee), 30 with debug set (tee). Skipping the redirect
# entirely also removes a buffering hop that could reorder stderr against stdout, so
# the quiet path is MORE faithful, not less.
#
# THE COST, NAMED: this process can no longer start teeing later. That is theoretical
# here (WRIT_DEBUG is fixed at exec, and the old line read it exactly once too), but
# the REAL cost is uniformity. writ-dispatch-discipline.sh, writ-subagent-start.sh and
# writ-subagent-stop.sh keep the unconditional idiom because they are not on the write
# path and changing them is outside this cycle's approved scope. Do NOT "restore
# consistency" by putting the fork back here; see
# docs/adr/ADR-write-path-branch-budget.md for the whole argument.
if _writ_debug_enabled; then
    exec 2> >(tee -a "${WRIT_HOOK_LOG:-/tmp/writ-hook-debug.log}" >&2)
fi


# READ STDIN ONCE, AND KEEP THE `cat`. `IFS= read -r -d ''` was measured against it
# rather than reasoned about, and it LOST: bash's read builtin does unbuffered
# single-byte reads from a NON-SEEKABLE fd, and the write path carries the whole file
# in tool_input.content.
#
# THE MEASUREMENT, taken on the channel this hook actually reads: stdin delivered
# through a PIPE, which is how Claude Code hands a hook its envelope. Median of seven
# runs: 1 KB cat 2.09 ms against read 1.66 ms (0.80x), 64 KB 2.42 ms against 28.65 ms
# (11.8x), 1 MB 6.96 ms against 444.64 ms (63.9x). The abort criterion was 1.25x at any
# size, so this is not close.
#
# DO NOT RE-MEASURE THIS WITH `< file`. An earlier attempt did, reported 1.8x at 1 MB
# and read as acceptable. That number is not a competing result, it is a measurement of
# the wrong channel: a SEEKABLE fd lets bash read in blocks and seek back, where a PIPE
# forces the unbuffered single-byte reads. Isolated at 1 MB, same script and payload,
# `read` takes 0.44 s from a pipe against 0.01 s from a regular-file redirect, and a
# second independent run of the same comparison put it at 446.0 ms against 14.6 ms. No
# hook is ever handed a seekable stdin, so only the pipe figure describes this line.
#
# It also returns exit status 1 when it finds no NUL even after reading all 1,048,576
# bytes, so it reports failure on success, and it truncates at the first NUL where
# command substitution drops the NUL and keeps the rest. Recorded as NOT TAKEN in
# docs/adr/ADR-write-path-branch-budget.md so the next reader does not re-derive it.
STDIN_DATA=$(cat)
# CAPTURE DRAIN GATED AT THE CALL SITE, the same shape load_hook_env already uses at
# bin/lib/common.sh:394. blackbox_log's disabled arm drains the pipe it was handed,
# which forks a `cat` on every write while capture is OFF. Gating here means the pipe
# is never created, so there is nothing to drain. The guard INSIDE blackbox_log stays:
# it protects every other caller.
if blackbox_enabled; then
    printf '%s' "$STDIN_DATA" | blackbox_log in writ-pre-write-dispatch
fi

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
# Strip embedded newlines before stripping the ends: this output is split positionally
# by line (element 0, element 1, elements 2 onward), so a newline inside the session id
# emits four lines and CHECK_BODY becomes a stray line glued to the real JSON body.
# Mirrored in pre-write-parse.jq; see the longer note there.
sid = (data.get('agent_id') or data.get('session_id') or '')
sid = sid.replace('\n', ' ').replace('\r', ' ').strip() if isinstance(sid, str) else ''
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

# ONE BUILTIN READ OF THE WHOLE STRING, replacing three external processes paid per
# write (one per field). mapfile -t strips only the trailing newline of each element,
# and the herestring appends exactly one newline, so an empty PARSED_INPUT yields a
# single empty element rather than no elements.
#
# CHECK_BODY IS "${_PARSE_LINES[@]:2}" JOINED BY NEWLINES, NOT "${_PARSE_LINES[2]}".
# The retired spelling took line 3 ONWARD, so a four-line parse sent lines 3 and 4 as
# the body. The single-index form truncates to line 3 alone, and the two FAIL
# DIFFERENTLY, which is why this is not a style question: the joined body is rejected by
# the server, so curl fails, RESULT comes back empty and the write gate is SKIPPED,
# while the truncated body can still parse and the gate then runs on a PARTIAL request.
# Both outcomes are unacceptable; reproducing the old semantics exactly means this
# conversion changes no failure mode. Executed proof, both spellings over one corpus:
# tests/test_pre_write_dispatch_line_split.py.
#
# printf -v joins without touching IFS (an IFS save-and-restore is wrong when IFS is
# unset: restoring "" would silently change word splitting for the rest of the script).
# With an empty slice printf still applies the format once, giving a lone newline, which
# the strip below turns back into the empty string.
#
# THE STRIP TAKES THE WHOLE TRAILING NEWLINE RUN, not one newline, and the differential
# caught this: the old arm captured through `$( )`, which strips ALL trailing newlines,
# so a PARSED_INPUT that itself ends in a newline yielded a trailing EMPTY element here
# and a body one byte longer than the old spelling produced. `${X##*[!\n]}` is the
# trailing run of newlines (the whole string when it holds no other character), and
# removing that suffix reproduces the command-substitution behaviour exactly.
#
# Every read carries an explicit :- default. Command substitution strips ALL trailing
# newlines, so a parse whose last field is empty yields FEWER elements than the reader
# expects and the index is UNSET, not empty. Measured: under `set -u` that is an
# "unbound variable" abort; with the default it is the empty string at exit status 0.
mapfile -t _PARSE_LINES <<<"$PARSED_INPUT"
SESSION_ID="${_PARSE_LINES[0]:-}"
WRITE_CTX="${_PARSE_LINES[1]:-}"
printf -v CHECK_BODY '%s\n' "${_PARSE_LINES[@]:2}"
_CHECK_BODY_NL_RUN="${CHECK_BODY##*[!$'\n']}"
CHECK_BODY="${CHECK_BODY%"$_CHECK_BODY_NL_RUN"}"

# NO SYNTHESIZED ID, AND NO EARLY EXIT. This used to call `detect_session_id ""`, which
# invented an id from PPID or md5(cwd:user).
#
# It does NOT return here, and that distinction matters more here than anywhere else in
# this cycle. The gate decision is made from CHECK_BODY, whose session_id comes from the
# same parse above and is unaffected by this variable; returning early would skip
# /pre-write-check entirely and turn its no-mode DENY ([ENF-GATE-MODE]) and its
# credential-path DENY into a silent ALLOW. So the gate runs exactly as before, the
# broken invariant is recorded, and only the SESSION-KEYED bookkeeping below is skipped
# (see the "$SESSION_ID" guard on the cache update near the end).
if [ -z "$SESSION_ID" ]; then
    writ_critical writ-pre-write-dispatch \
        "no session_id in hook payload; the write gate still runs, but RAG budget and queried-rule bookkeeping are skipped"
fi

# A11: mode for hook_execution telemetry now travels in the /pre-write-check
# response (parsed from DISPATCH_BLOB below), replacing a separate `mode get`
# daemon round-trip. The two early-exit timers below run before that response
# exists, so they log an empty mode -- telemetry metadata only, and both are
# degenerate no-write paths (empty body, or server unreachable).
MODE=""

if [ -z "$CHECK_BODY" ]; then
    exit 0
fi

# Single HTTP call to /pre-write-check. The helper reads the JSON body from its
# SECOND positional arg (session_id is first); passing only CHECK_BODY sent an
# empty `{}` body, so the server saw no file_path and always returned "allow" --
# a silent gate bypass that also suppressed every real write_attempt event. Pass
# SESSION_ID first so CHECK_BODY lands in $2.
RESULT=$(_writ_session pre-write-check "$SESSION_ID" "$CHECK_BODY" 2>/dev/null || echo "")

if [ -z "$RESULT" ]; then
    exit 0
fi

# Item 4c: single python3 spawn computes decision + reason + file_path + payload
# + hookSpecificOutput JSON + RAG metadata. Was three sequential json.load() spawns
# plus an inline hookSpecificOutput builder. Output is one field per line, read below
# with `mapfile` so the split itself costs no further process.
#
# That last sentence has been in this file since Item 4c landed and was FALSE until the
# write-path spawn-reduction cycle: it sat directly above seven per-field external
# process calls plus a whitespace strip. It is true now.
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
            'additionalContext': 'IMPORTANT: This write was denied by a Writ gate. Do NOT attempt more writes to other files, because the denial applies to ALL files until the gate advances. Read the denial reason and follow the workflow: present your work to the user and wait for approval.',
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

# The same builtin read as the PARSED_INPUT split above: ONE mapfile instead of eight
# external processes (seven per-field reads plus the whitespace strip on MODE).
mapfile -t _BLOB_LINES <<<"$DISPATCH_BLOB"
DECISION="${_BLOB_LINES[0]:-}"
DECISION_FILE="${_BLOB_LINES[1]:-}"
HOOK_OUTPUT="${_BLOB_LINES[2]:-}"
RAG_RULES_RAW="${_BLOB_LINES[3]:-}"
NEW_RULE_IDS="${_BLOB_LINES[4]:-}"
COST="${_BLOB_LINES[5]:-}"
# A11: mode from the /pre-write-check envelope -> the closing timer call (no
# separate `mode get`). Empty for the early-exit timers above (degenerate paths).
#
# THE INDEX IS UNSET ON THE NORMAL PATH, not an exotic one. Command substitution strips
# ALL trailing newlines, so a response carrying an empty mode arrives as SIX elements,
# not seven, and the explicit :- default is the only reason element 6 reads as the empty
# string instead of aborting under `set -u`. The whitespace strip is a pattern
# expansion, not a process; the two spellings agree on every case in the corpus at
# tests/test_pre_write_dispatch_line_split.py, and where a future locale makes them
# disagree the expansion result is the one this hook takes.
MODE="${_BLOB_LINES[6]:-}"
MODE="${MODE//[[:space:]]/}"

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
    emit_hook_reply "$HOOK_OUTPUT" "" "$SESSION_ID"
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
        # The capture used to log ${RAG_RULES_RAW}${AO_WRITE_BLOCK}, the INGREDIENTS of
        # this envelope rather than the envelope, so every row parsed as a non-object and
        # filed under the event name "unknown". The reply is captured into a variable and
        # that same variable is what reaches stdout and the capture log.
        # The display name, computed without a `basename` process. Two steps because
        # bash cannot nest :- inside ##*/ in one expansion. The two spellings agree on
        # every ordinary path and differ on exactly three inputs, all pinned WITH their
        # values in tests/test_pre_write_dispatch_line_split.py rather than assumed
        # away: a trailing slash (basename "/a/b/" is "b", the expansion is empty), a
        # bare "/" (basename is "/", the expansion is empty), and a leading-dash
        # argument, where the expansion is the SAFER answer because GNU basename with no
        # `--` separator reads "-rf" as an option, exits 1 and prints nothing. This value
        # only ever reaches the display header below, never a gate decision.
        _DECISION_NAME="${DECISION_FILE:-unknown}"
        _DECISION_NAME="${_DECISION_NAME##*/}"
        AC_REPLY=$(WRIT_AC="[Writ: file-context rules for ${_DECISION_NAME}]
${RAG_RULES_RAW}
${AO_WRITE_BLOCK}" python3 <<'PY' 2>>"$WRIT_HOOK_LOG_SINK"
import json, os
print(json.dumps({"hookSpecificOutput": {
    "hookEventName": "PreToolUse",
    "additionalContext": os.environ.get("WRIT_AC", ""),
}}))
PY
) || AC_REPLY=""
        emit_hook_reply "$AC_REPLY" "" "$SESSION_ID"
    fi
    # "$SESSION_ID" is required, not decorative: _cache_path() has no empty-id guard, so
    # `update ""` creates a REAL cache file named for the empty string and files this
    # turn's rules under a session that can never be read back. The critical error was
    # already recorded up top; this is the no-op that follows it.
    if [ -n "$SESSION_ID" ] && [ -n "$NEW_RULE_IDS" ] && [ "$NEW_RULE_IDS" != "[]" ]; then
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

exit 0
