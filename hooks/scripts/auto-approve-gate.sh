#!/usr/bin/env bash
# Auto-approve gate: approval detection for the human phase gate.
# UserPromptSubmit: fires at the start of every user turn.
#
# TWO TIERS, ONE AUTHORITY (cycle 1). bin/lib/approval_match.py classifies the prompt as
# exact, embedded or none, and only ONE of them can advance a gate:
#
#   exact     the prompt IS an approval. The hook mints a bound single-use token and
#             advances the pending gate, exactly as it did before the tier split.
#   embedded  a strong approval word inside a longer sentence ("ok remember we want to
#             fix all our findings, approved", which cost a turn on 2026-08-10). The hook
#             ASKS: it names the pending gate and advances nothing. If the user confirms,
#             that turn is an exact approval and takes the path above. Recall goes up
#             without widening what can advance a gate, and the cost of a
#             misclassification is one question instead of an approval nobody gave.
#   none      the hook does nothing at all: no directive, no telemetry row, and neither
#             the project root (a cwd walk) nor the session state is resolved. This is
#             every turn but one, so it must stay cheap.
#
# The bare substring scan that used to sit here (a search for `approv`, `proceed`,
# `accept`, `lgtm`, `good`, `go`, `yes` or `ok` anywhere in the prompt) is DELETED along
# with its second interpreter start. It had no authority beyond deciding whether to write a telemetry
# row, and it fired on the `go` inside "going" and the `ok` opening "ok how about", which
# is every approval_pattern_miss row in the friction log for 2026-08-10.
#
# Why the hook advances at all: the user typed the approval, the hook (trusted infra)
# executes it, the agent is never in the loop. It replaces the /writ-approve dance where
# the agent had to read and echo the token back (circular + unworkable).
#
# Hook type: UserPromptSubmit
# Exit: always 0 (never block user prompt). Directive (if any) is on stdout.

set -euo pipefail

HOOK_DIR="$(cd "$(dirname "$0")" && pwd)"
WRIT_DIR="$(cd "$HOOK_DIR/../.." && pwd)"
SESSION_HELPER="$WRIT_DIR/bin/lib/writ-session.py"
FA="$WRIT_DIR/bin/lib/friction-append.py"
source "$WRIT_DIR/bin/lib/common.sh"

# Read stdin once
STDIN_JSON=$(cat)

# Extract session_id and prompt
PARSED=$(echo "$STDIN_JSON" | python3 -c "
import sys, json
try:
    data = json.load(sys.stdin)
    sid = data.get('agent_id', '') or data.get('session_id', '')
    agent_id = data.get('agent_id', '')
    prompt = data.get('prompt', data.get('message', data.get('content', '')))
    print(f'{sid}\n{prompt}\n{agent_id}')
except Exception:
    print('\n\n')
" 2>/dev/null) || true

SESSION_ID=$(echo "$PARSED" | head -1)
PROMPT=$(echo "$PARSED" | sed -n '2p')
AGENT_ID=$(echo "$PARSED" | sed -n '3p')

# Fallback session ID
# NO SYNTHESIZED SESSION ID. This used to fall back to the parent PID and then to
# md5(cwd:user)+date. Neither can ever equal the id Claude Code uses, so state written
# under one is written to a session that does not exist and is simply never read again,
# while the hook reports success. Claude Code documents session_id as universal and
# authoritative on every hook event, so an empty one is a broken invariant, not a case to
# paper over: record it and stop.
if [ -z "$SESSION_ID" ]; then
    writ_critical auto-approve-gate "no session_id in hook payload; refusing to synthesize one"
    exit 0
fi

# Publish the session id for the callers that have NO payload to read one from. This is
# the SECOND writer of this file; writ-rag-inject.sh:129 is the other.
#
# TWO READERS REMAIN, and neither could use a payload if it had one:
#   hooks/git/post-commit:29        a git hook; git passes no Claude Code envelope, ever
#   session-start-bootstrap.sh:112  reads the PRE-rotation id, which by definition is not
#                                   in the payload (the payload carries the NEW one)
# The other two are gone as of the session-identity cycle: resolve_current_session_id()
# no longer reads this file or the newest cache by mtime (it answers from
# $CLAUDE_SESSION_ID or $CLAUDE_JOB_DIR, or None), and bin/audit-region.sh now requires
# --session or $CLAUDE_SESSION_ID. Nothing here answers "which session am I" from this
# file any more, because it names whichever session on this machine took a turn most
# recently. The write stays because deleting it would leave the two readers above with no
# signal at all, and neither can be handed one. Re-check this list before removing it.
#
# Do NOT overwrite when inside a sub-agent: that would publish the child's id as the
# session, and both readers want the top-level one.
if [ -z "$AGENT_ID" ]; then
    echo "$SESSION_ID" > /tmp/writ-current-session
fi

# Check approval pattern
PROMPT_LOWER=$(echo "$PROMPT" | tr '[:upper:]' '[:lower:]' | sed 's/^[[:space:]]*//;s/[[:space:]]*$//')

# Approval detection delegates to the single source of truth in
# bin/lib/approval_match.py (the hook and the tests bind to one module; no inline
# detector to drift). SEC-INJ-CMD-001: PROMPT_LOWER is passed as an argv element,
# never interpolated into the python string. ONE interpreter start decides the tier,
# where there used to be two (is_approval plus the substring scan).
TIER=$(python3 -c "import sys; sys.path.insert(0, '$WRIT_DIR/bin/lib'); from approval_match import classify; print(classify(sys.argv[1]))" "$PROMPT_LOWER" 2>/dev/null || echo "none")
# Fail closed on anything unexpected (a broken module, an empty print): an unrecognized
# tier must behave like "none", never like an approval.
case "$TIER" in
    exact|embedded) ;;
    *) TIER="none" ;;
esac

if [ "$TIER" = "none" ]; then
    # Debug prompt log, gated behind WRIT_DEBUG (default OFF).
    if _writ_debug_enabled; then
        echo "$(date -u '+%Y-%m-%dT%H:%M:%SZ') session=$SESSION_ID prompt=$(echo "$PROMPT" | head -c 200)" \
            >> "/tmp/writ-prompt-debug.log" 2>/dev/null || true
    fi
    exit 0
fi

# Deferred behind the tier gate: this prompt is approval-related, so the session's gate
# state is worth one call. current-phase reports the phase, the mode, the next pending
# gate and the plan fingerprint from ONE cache read, which is also the binding the token
# is minted with, so the binding costs this path nothing beyond a call it already made
# and the mint and the later claim fingerprint the same plan.md by construction.
PHASE_JSON=$(python3 "$SESSION_HELPER" current-phase "$SESSION_ID" 2>/dev/null || echo "{}")
# Tab-separated, one value out, per json_transform's contract (jq when present, stdlib
# python otherwise, since jq is not a prerequisite for the gate to work).
#
# FIELD 5 is a PRESENCE flag for next_gate, not a value: an absent key and a null both
# render as an empty field, and those two mean opposite things. Null is the answer "no
# gate is pending"; absent is "this responder is older than the field" (writ/ code and
# the running daemon are separate artifacts -- the daemon has to be restarted to pick up
# a checkout). The advance guard below has to tell them apart to know whether it may
# trust the empty answer or must fall back to the phase.
PHASE_FIELDS=$(printf '%s' "$PHASE_JSON" | json_transform \
    '[(.phase // ""), (.mode // ""), (.next_gate // ""), (.plan_hash // ""), (if has("next_gate") then "1" else "" end)] | join("\t")' \
    '"\t".join([str(d.get(k) or "") for k in ("phase", "mode", "next_gate", "plan_hash")] + ["1" if "next_gate" in d else ""])' \
    2>/dev/null || echo "")
# `cut -s`: a line with no tab is a parse that failed, and every field must then read
# empty rather than repeating the whole line into CURRENT_MODE (which would compare equal
# to nothing and silently skip the advance without saying why).
CURRENT_PHASE=$(printf '%s' "$PHASE_FIELDS" | cut -s -f1)
CURRENT_MODE=$(printf '%s' "$PHASE_FIELDS" | cut -s -f2)
NEXT_GATE=$(printf '%s' "$PHASE_FIELDS" | cut -s -f3)
PLAN_HASH=$(printf '%s' "$PHASE_FIELDS" | cut -s -f4)
NEXT_GATE_REPORTED=$(printf '%s' "$PHASE_FIELDS" | cut -s -f5)

case "$TIER" in
embedded)
    # A genuine near miss, unlike the substring scan's "going". Logged so the tier's
    # precision can be measured from the friction log instead of guessed at.
    # json.dumps keeps the free-form prompt JSON-safe (quotes/backslashes/newlines).
    MISS_EXTRA=$(python3 -c "import json,sys; print(json.dumps({'prompt': sys.argv[1][:120], 'next_gate': sys.argv[2]}))" "$PROMPT" "${NEXT_GATE:-}" 2>/dev/null || echo '{}')
    log_friction_event "$SESSION_ID" "${CURRENT_MODE:-}" "approval_pattern_miss" "$MISS_EXTRA"
    # ASK, do not advance, and do not mint: an embedded approval is a question about
    # intent. Naming the pending gate is what makes the question answerable in one turn.
    if [ -n "$NEXT_GATE" ]; then
        cat <<DIRECTIVE
[Writ: approval wording detected, nothing was advanced]
The ${NEXT_GATE} gate is pending, and this prompt is not an exact approval, so no gate
was advanced and no approval token was minted. Ask the user whether they meant to approve
the ${NEXT_GATE} gate before doing any gated work. If they confirm with "approved", the
approval hook advances it on that turn.
DIRECTIVE
    else
        cat <<'DIRECTIVE'
[Writ: approval wording detected, nothing was advanced]
No approval gate is pending in this phase/mode, so there is nothing to advance and no
token was minted. Treat the prompt as an ordinary instruction, not as a gate approval.
DIRECTIVE
    fi
    ;;
exact)
    # The project root: a cwd walk, resolved ONLY here, for the telemetry row below. The
    # gate decision itself uses the root the SERVER resolves (see the payload comment).
    PROJECT_ROOT=$(detect_project_root "$(pwd -P)")

    # False-positive guard (what the tool-only design was protecting against):
    # auto-advance ONLY in work mode, and ONLY when a gate is actually pending. Which
    # gate that is comes from the server (see the precedence comment below the mint);
    # the other modes have no gates at all, so a stray "approved" there is a logged
    # no-op.
    #
    # The token is minted for EVERY exact approval, including the no-gate-pending case:
    # that is the token /session/{id}/promote-candidate needs, and it is bound to the
    # empty gate, which is precisely what that route now requires.
    GATE_TOKEN_FILE="/tmp/writ-gate-token-${SESSION_ID}"
    GATE_TOKEN=$(python3 -c "import secrets; print(secrets.token_hex(16))" 2>/dev/null || echo "")
    if [ -n "$GATE_TOKEN" ]; then
        # OVERWRITE any existing file, where this used to mint only when none existed.
        # A token now records the gate and the plan it authorizes, so a leftover from an
        # earlier turn is bound to an earlier state: keeping it would make the user's
        # fresh approval be refused as a gate mismatch, with no way to clear it. The
        # newest approval is the authoritative one.
        write_gate_token_file "$GATE_TOKEN_FILE" "$GATE_TOKEN" "${NEXT_GATE:-}" "${PLAN_HASH:-}"
    fi

    ADVANCED_TO=""
    # Initialized here so the final branch chain is safe under `set -u` even when the
    # work/phase guard below is skipped (no advance attempted).
    OUTCOME=""
    GATE_ERROR=""
    VALIDATED=""
    TOKEN_SPENT=""
    # PRECEDENCE: the server's next_gate WINS; the planning/testing phase inference is
    # a FALLBACK, used only when the responder did not report the field at all (an
    # older daemon -- writ/ and the running daemon are separate artifacts, and the
    # daemon must be restarted to pick up a checkout).
    #
    # The inference maps planning -> phase-a, testing -> test-skeletons and
    # implementation -> nothing, which is not the question _next_pending_gate answers.
    # A gate RE-ARMS whenever plan.md changes under an approval, and that can happen in
    # any phase; when it happened during implementation the inference said "no gate
    # here" while enforcement denied every write, and the user's "approved" could not
    # clear it. So a non-empty next_gate advances regardless of phase, and an empty one
    # the server actually REPORTED (field present, value null) means nothing is
    # pending -- an answer, not a gap, which the phase must not second-guess.
    GATE_PENDING=""
    if [ -n "$NEXT_GATE" ]; then
        GATE_PENDING=1
    elif [ -z "$NEXT_GATE_REPORTED" ] && { [ "$CURRENT_PHASE" = "planning" ] || [ "$CURRENT_PHASE" = "testing" ]; }; then
        GATE_PENDING=1
    fi
    if [ -n "$GATE_TOKEN" ] && [ "$CURRENT_MODE" = "work" ] && [ -n "$GATE_PENDING" ]; then
        # Send the cwd and let the SERVER resolve the project root
        # (locators.resolve_project_root: marker dir at or above cwd, else the cwd itself).
        # Two reasons not to send the bash marker walk as project_root instead:
        #   - it would arrive as the "explicit" tier, so the reported root_tier could never
        #     say whether a marker or the bare cwd produced the root -- the whole point of
        #     showing the user where the approved plan came from;
        #   - the marker list exists in both bash (detect_project_root) and python
        #     (PROJECT_ROOT_MARKERS); resolving server-side keeps ONE of them authoritative
        #     for the gate decision, so a future drift cannot change which plan is approved.
        # $PROJECT_ROOT is still computed above, for the friction log. Sending cwd is what
        # makes an unmarked directory workable at all: it used to resolve to no root, and the
        # route then refused the advance and spent the approval every time. The server cannot
        # substitute its own cwd -- that is Writ's install dir, which has its own plan.md.
        ADVANCE_PAYLOAD=$(python3 -c "import json,sys; print(json.dumps({'confirmation_source':'pattern','token':sys.argv[1],'cwd':sys.argv[2]}))" "$GATE_TOKEN" "$(pwd -P)" 2>/dev/null || echo "{}")
        # Address the daemon through WRIT_SESSION_HOST/PORT (common.sh derives them from
        # WRIT_HOST/WRIT_PORT), as every other hook does. This request once hardcoded
        # localhost:8765, so it ignored WRIT_PORT: the test suite pins WRIT_PORT to its own
        # daemon precisely to leave the interactive 8765 singleton alone, and that one line
        # reached past that isolation and advanced gates on the developer's real daemon,
        # writing real phase_advance rows into the real audit log.
        # WRIT_HTTP_TIMEOUT=10, not 3: this POST runs the target gate's validator, and
        # _validate_test_skeletons falls back to a recursive glob over the project when no
        # session-tracked test file matches, which on a large repo can outlast a 3s budget.
        # A timeout is the worst outcome here: the server can still advance and consume the
        # token while the hook, seeing no response, tells the user nothing was advanced. This
        # runs once per human approval, not on a hot path, so the wider budget is cheap.
        #
        # writ_http_post, not raw curl: without a fallback, a user typing "approved" on a
        # curl-less machine silently advanced NOTHING. The session helper's own advance arm
        # cannot substitute -- it posts {}, dropping the single-use token and the cwd the
        # server needs to resolve the project root -- so the request is preserved byte for byte
        # here and urllib carries it when curl is absent. Non-fail mode on purpose: a >= 400
        # body is what gate_advance_outcome.py classifies as a rejection.
        ADVANCE_RESP=$(WRIT_HTTP_CONNECT_TIMEOUT=0.5 WRIT_HTTP_TIMEOUT=10 \
            writ_http_post "http://${WRIT_SESSION_HOST}:${WRIT_SESSION_PORT}/session/${SESSION_ID}/advance-phase" \
            "$ADVANCE_PAYLOAD" 2>/dev/null || echo "")
        # Classify the response via the shared stdlib helper (single source; the two
        # inline parses this replaces had drifted). A rejection SPENDS the token, so
        # the user must fix the artifact and type "approved" again to mint a fresh one.
        OUTCOME_RAW=$(echo "$ADVANCE_RESP" | python3 "$WRIT_DIR/bin/lib/gate_advance_outcome.py" 2>/dev/null || printf 'none\t\t\t\t')
        OUTCOME=$(printf '%s' "$OUTCOME_RAW" | cut -f1)
        # Fields: 1 outcome, 2 phase, 3 what the gate judged, 4 token_spent, 5- the error.
        # The error is last because it is the only unbounded/multi-line field; `head -1`
        # keeps a multi-line error from smearing the fixed fields across its later lines.
        VALIDATED=$(printf '%s' "$OUTCOME_RAW" | head -1 | cut -f3)
        TOKEN_SPENT=$(printf '%s' "$OUTCOME_RAW" | head -1 | cut -f4)
        if [ "$OUTCOME" = "advanced" ]; then
            ADVANCED_TO=$(printf '%s' "$OUTCOME_RAW" | cut -f2)
        elif [ "$OUTCOME" = "rejected" ]; then
            GATE_ERROR=$(printf '%s' "$OUTCOME_RAW" | cut -f5-)
        fi
    fi

    # Friction log: advanced (hook executed the user's approval), rejected, or ask-prompt
    # fallback. Unconditional: this used to be gated on a non-empty PROJECT_ROOT, so an
    # approval typed in an unmarked directory -- the case where the gate refuses to advance
    # at all -- logged nothing, which is how the defect stayed invisible.
    python3 -c "
import json, sys
from datetime import datetime, timezone
advanced_to = sys.argv[4]
rejected = sys.argv[6]
if advanced_to:
    outcome = 'advanced->' + advanced_to
elif rejected:
    outcome = 'rejected'
else:
    outcome = 'ask-prompt-emitted'
entry = {
    'ts': datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ'),
    'session': sys.argv[1],
    'mode': sys.argv[2] if sys.argv[2] else None,
    'event': 'approval_pattern_match',
    'matched_prompt': sys.argv[3][:120],
    'confirmation_source': 'pattern',
    'outcome': outcome,
    'project_root': sys.argv[5],
}
print(json.dumps(entry))
" "$SESSION_ID" "${CURRENT_MODE:-}" "$PROMPT" "${ADVANCED_TO:-}" "${PROJECT_ROOT:-}" "${GATE_ERROR:-}" \
        2>/dev/null | python3 "$FA" --stdin-json 2>/dev/null || true

    if [ -n "$ADVANCED_TO" ]; then
        # Confirm the advance to the assistant/user via next-turn context, NAMING the
        # artifact that was accepted. "approved" alone hid which plan.md the gate read, so a
        # project root resolved from a stray marker file above the work dir could stamp an
        # unrelated plan silently.
        echo "[Writ: ${CURRENT_PHASE} gate approved -> ${ADVANCED_TO}] (advanced by the approval hook on your confirmation; no agent self-approval)"
        [ -n "$VALIDATED" ] && echo "[Writ: ${VALIDATED}] -- if that is not the plan you meant to approve, the project root is wrong: invalidate the gate before continuing."
    elif [ -n "$GATE_ERROR" ]; then
        # The server REFUSED the advance. Surface the reason on STDOUT so the agent sees WHY
        # and fixes it -- stderr is not shown in the UserPromptSubmit context, which made
        # refusals look like "no gate pending".
        echo "[Writ: ${CURRENT_PHASE} gate REJECTED -- not advanced] ${GATE_ERROR}"
        # Two kinds of refusal, and telling the user the wrong one is a real cost: a spent
        # token means they MUST type the approval again, an unspent one means they must not.
        # token_spent=false comes back when the gate could not evaluate the artifact at all
        # (no resolvable project root), where the approval stays valid.
        if [ "$TOKEN_SPENT" = "false" ]; then
            echo "Your approval was NOT consumed: fix the cause above and retry; you do not need to approve again."
        else
            echo "Fix the issue above in one edit; the rejection spent the prior approval, so the user must approve again."
        fi
    elif [ "$OUTCOME" = "noop" ]; then
        # Benign no-op: the server reported no pending gate to advance (not a rejection).
        # Emit the SAME neutral steer as the none/else branch -- no REJECTED text, no
        # "fix the issue", and the approval token is NOT spent.
        cat <<'DIRECTIVE'
[Writ: approval pattern detected]
No approval gate was advanced (no gate is pending in this phase/mode). If you intended
to advance a Work-mode plan/test gate, ensure the server is up; otherwise this approval
needs no gate action.
DIRECTIVE
    else
        # No pending gate, or server unreachable: fall back to the steer directive.
        cat <<'DIRECTIVE'
[Writ: approval pattern detected]
No approval gate was advanced (no gate is pending in this phase/mode, or the Writ
server is unreachable). If you intended to advance a Work-mode plan/test gate, ensure the
server is up; otherwise this approval needs no gate action.
DIRECTIVE
    fi
    ;;
esac

# Debug prompt log, gated behind WRIT_DEBUG (default OFF).
if _writ_debug_enabled; then
    echo "$(date -u '+%Y-%m-%dT%H:%M:%SZ') session=$SESSION_ID prompt=$(echo "$PROMPT" | head -c 200)" \
        >> "/tmp/writ-prompt-debug.log" 2>/dev/null || true
fi

exit 0
