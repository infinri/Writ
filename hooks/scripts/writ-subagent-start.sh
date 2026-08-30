#!/usr/bin/env bash
# SubagentStart hook -- creates isolated session cache for each sub-agent worker.
#
# When a sub-agent spawns, this hook:
# 1. Reads the parent's session state (mode, phase, gates)
# 2. Creates a fresh session cache keyed by agent_id
# 3. Pre-populates with parent's gate state but fresh RAG budget
# 4. Queries Writ for phase-specific rules
# 5. Injects rules + state via additionalContext
#
# Hook type: SubagentStart
# Exit: always 0

set -euo pipefail

HOOK_DIR="$(cd "$(dirname "$0")" && pwd)"
WRIT_DIR="$(cd "$HOOK_DIR/../.." && pwd)"
SESSION_HELPER="$WRIT_DIR/bin/lib/writ-session.py"

# Source shared helpers (log_friction_event, _writ_session) BEFORE any use.
# Previously this was sourced near the end (line ~169); the empty-agent_type
# fallback branch below calls log_friction_event, so under `set -e` the hook
# died at that call -- never logging subagent_start, never creating the session
# cache, never injecting rules. Sourcing first is the keystone fix.
source "$WRIT_DIR/bin/lib/common.sh"

# Phase 4c: capture stderr (Python tracebacks etc.) to debug log so
# next-occurrence diagnostics are readable. tee preserves stderr
# propagation so behavior is unchanged. Gated behind WRIT_DEBUG (default OFF):
# the sink is /dev/null unless WRIT_DEBUG=1.
exec 2> >(tee -a "$(_writ_debug_enabled && echo "${WRIT_HOOK_LOG:-/tmp/writ-hook-debug.log}" || echo /dev/null)" >&2)

WRIT_HOST="${WRIT_HOST:-localhost}"
WRIT_PORT="${WRIT_PORT:-8765}"

# Read stdin envelope
STDIN_JSON=$(cat)
printf '%s' "$STDIN_JSON" | blackbox_log in writ-subagent-start

# Bounded capture of the raw SubagentStart envelope so we can learn whether/where
# Claude Code sends agent_type (Phase 3.2 input). /tmp, capped at 50 lines,
# fire-and-forget -- never affects the hook outcome.
_WRIT_PAYLOAD_CAP=/tmp/writ-subagent-payloads.jsonl
if [ "$(wc -l < "$_WRIT_PAYLOAD_CAP" 2>/dev/null || echo 0)" -lt 50 ]; then
    printf '%s\n' "$STDIN_JSON" >> "$_WRIT_PAYLOAD_CAP" 2>/dev/null || true
fi

# Extract agent metadata and parent session
AGENT_ID=$(parsed_field "$STDIN_JSON" "agent_id")
AGENT_TYPE=$(parsed_field "$STDIN_JSON" "agent_type")
PARENT_SESSION=$(parsed_field "$STDIN_JSON" "session_id")

if [ -z "$AGENT_ID" ]; then
    exit 0
fi

# THIS HOOK'S OWN TELEMETRY IS KEYED HERE, and AGENT_ID is non-empty by the guard above.
#
# log_gate_decision files its row under `${SESSION_ID:-${HOOK_SESSION_ID:-}}`
# (bin/lib/common.sh:1304), and this hook set neither -- so the manual-test-grant
# `inherit` row below, a governance record of a user concession passing to a worker,
# was written with an empty session and read back as the literal id "unknown",
# untraceable to the sub-agent that inherited the grant.
#
# AGENT_ID AND NEVER PARENT_SESSION, the same agent-first rule as
# writ-debug-code-gate.sh:44: a sub-agent's rows belong to the sub-agent, and filing
# them under its parent would attribute a child's inherited grant to the session that
# merely dispatched it. It is not synthesized either -- the payload carried this id.
#
# ATTRIBUTION ONLY, verified rather than assumed. Nothing this hook reaches keys STATE
# off SESSION_ID: it never calls hook_instrument (no exit trap) or writ_gate_dir, the
# one SESSION_ID-keyed file write in common.sh is the buffered arm at :1284, which
# log_gate_decision reaches only for the decision "allow" (:1264) and never for
# "inherit", and the name is deliberately NOT exported, so no python child (the cache
# writer, manual_test_grant.py, writ-session.py) can see it. The one other reader,
# _writ_row_mode's fallback at :93, is a read-only mode lookup against this agent's own
# cache file -- the same file CURRENT_MODE below already reads.
SESSION_ID="$AGENT_ID"

# THE ROLE IS RESOLVED HERE, AND THIS IS THE EARLIEST POINT IT CAN BE.
#
# `agent_type` is in this event's schema and arrives EMPTY on this build. The old code
# rewrote the empty string to the literal `general-purpose`, which is how a role nobody
# observed became a fact in the child's cache and in every record downstream of it.
#
# RESOLVED EARLY AND STORED, because the sidecar Claude Code writes beside the agent's
# transcript is EPHEMERAL: it is deleted after the agent finishes (measured 2026-08-27, 0
# of 57 completed agents still had one). A later hook cannot re-derive what is gone, so the
# answer is captured while the file exists and replayed from the cache afterwards.
ROLE_RESOLUTION=$(AGENT_ID="$AGENT_ID" AGENT_TYPE="$AGENT_TYPE" python3 -c '
import os, sys
sys.path.insert(0, sys.argv[1])
role, source = "", "unresolved"
try:
    from writ.session.subagent_role import resolve_role
    role, source = resolve_role(os.environ.get("AGENT_ID", ""),
                                os.environ.get("AGENT_TYPE", ""))
except Exception:
    # Resolution failure must not fail the hook, and must not invent a role.
    pass
print(role)
print(source)
' "$WRIT_DIR" 2>/dev/null || true)

if [ -n "$ROLE_RESOLUTION" ]; then
    AGENT_TYPE=$(printf '%s' "$ROLE_RESOLUTION" | head -1)
    ROLE_SOURCE=$(printf '%s' "$ROLE_RESOLUTION" | sed -n 2p)
fi
[ -z "$AGENT_TYPE" ] && AGENT_TYPE="unknown"
[ -z "${ROLE_SOURCE:-}" ] && ROLE_SOURCE="unresolved"

if [ "$ROLE_SOURCE" = "unresolved" ]; then
    log_friction_event "$AGENT_ID" "" "subagent_type_fallback" \
        "{\"hook\":\"writ-subagent-start\",\"parent_session\":\"$PARENT_SESSION\",\"role_source\":\"unresolved\"}"
fi

# THE PARENT'S CACHE NO LONGER CROSSES AN EXEC BOUNDARY, and that is the whole of this
# hook's oldest silent failure. Its entire JSON used to be captured here and handed to
# three python blocks as argv[1]. Linux caps a SINGLE argv or env string at MAX_ARG_STRLEN
# (32 pages, 131072 bytes here); the live parent measured 181529, so execve died with
# E2BIG before python started, `2>/dev/null || true` swallowed it, and every sub-agent
# dispatched from a long session ran with no mode and no gates. Only the bounded session id
# crosses now, and each block re-reads the cache in process. The standing rule that came out
# of it is docs/adr/ADR-hook-exec-argument-boundary.md.
#
# STILL FILE-DIRECT, which is the property the old comment here existed to protect. The
# blocks read `writ.session.cache._read_cache`, never _writ_session (daemon-first): the
# daemon's in-memory or cache-dir view can diverge from the file cache and answer with a
# stale mode=None, and the sub-agent would inherit that and run gate-less. This is exactly
# what `writ-session.py read` printed before, because cmd_read is
# `json.dump(_read_cache(session_id))`.
if [ -z "$PARENT_SESSION" ]; then
    # NO POINTER FALLBACK. /tmp/writ-current-session names whichever Claude Code session on
    # this machine took a turn most recently, which for a sub-agent means inheriting mode
    # and gate state from an unrelated parent. That is worse than inheriting nothing: the
    # sub-agent would run governed by another session's phase, and the mismatch is
    # invisible from either side.
    #
    # Empty state is the honest answer, and it is recorded rather than assumed.
    # Filed under AGENT_ID, which is known non-empty here. Omitting it sent the row to
    # session "unknown", so the one record saying a sub-agent started ungoverned could not
    # be traced back to which sub-agent that was.
    writ_critical writ-subagent-start \
        "no parent session in payload; sub-agent starts with no inherited gate state" \
        "$AGENT_ID"
fi

# Create isolated session for the sub-agent with parent's gate state but fresh budget.
# The importlib dance that used to load bin/lib/writ-session.py here is gone with the
# inline mutate_cache block it served: the seeder is a package module, so a plain import
# off the skill root reaches it.
#
# NAMED ENV VARIABLES, NOT POSITIONAL ARGUMENTS. Deleting the unread argv[1] would have
# shifted every later index by one, and a wrong shift swaps the role with the parent session
# and produces a quieter version of the same silent failure. The four values are bounded (an
# agent id, a role name, a source label, a session id), which is the only reason env is safe
# here: MAX_ARG_STRLEN applies to env strings too, so this is not a way to move an unbounded
# value across.
#
# A QUOTED HEREDOC. The old block was a double-quoted string whose comments carry markdown
# backticks around `cache_source` and `envelope`, and bash ran them as command substitutions
# ("cache_source: command not found" on every dispatch). `<<'PY'` suppresses every expansion,
# which also means WRIT_DIR has to ride the env channel instead of being interpolated.
#
# STDOUT IS THE STATUS, and an EMPTY status is the signal. `seeded` and `skipped` both mean
# the block ran to completion; nothing at all means the exec died, python raised before
# printing, or the process was killed. Nothing is inferred from a missing telemetry row.
SEED_STATUS=$(WRIT_DIR="$WRIT_DIR" \
    WRIT_SA_AGENT_ID="$AGENT_ID" \
    WRIT_SA_ROLE="$AGENT_TYPE" \
    WRIT_SA_ROLE_SOURCE="$ROLE_SOURCE" \
    WRIT_SA_PARENT="$PARENT_SESSION" \
    python3 - <<'PY'
import os, sys
sys.path.insert(0, os.environ.get('WRIT_DIR', ''))

agent_id = os.environ.get('WRIT_SA_AGENT_ID', '')
parent_session = os.environ.get('WRIT_SA_PARENT', '')
resolved_role = os.environ.get('WRIT_SA_ROLE', '')
role_source = os.environ.get('WRIT_SA_ROLE_SOURCE', '') or 'unresolved'

# ONE COPY OF THE INHERITANCE RULES, in writ/session/subagent_seed.py. This block used to
# hold its own, which meant the lazily seeded path (for the 64% of sub-agents that never get
# a SubagentStart) would have been a second copy free to drift. `cache_source` records that
# THIS path created the cache, which is what the write gate keys the sub-agent bypass on: a
# dispatch that fired SubagentStart is an authorization event, a hook-seeded cache is not.
#
# `default_mode='work'` preserves this path's long-standing null-safety (audit P1): a
# mode-unset parent would otherwise hand the child a None and let it run mode-less. The lazy
# path passes no default, so it seeds nothing rather than inventing a mode.
#
# The role is resolved ONCE, above, and handed over with its source, so a sidecar-derived
# role is not relabelled `envelope` on the way in.
from writ.session.subagent_seed import CACHE_SOURCE_START, seed_subagent_cache

seeded = seed_subagent_cache(agent_id, parent_session,
                             cache_source=CACHE_SOURCE_START,
                             default_mode='work',
                             role=resolved_role,
                             role_source=role_source)
print('seeded' if seeded else 'skipped')
PY
) || SEED_STATUS=""

# THE SUPPRESSION IS GONE, AND THE EXIT CODE STILL CANNOT PROPAGATE. A dispatch must never
# fail because governance could not be inherited, so this hook keeps exiting 0; what changes
# is that the outcome stops being invisible. Two readers, two surfaces: a person watching
# stderr gets the critical line at the moment it happens, and the governance census gets one
# countable row. The row's fields are deliberately minimal and bounded, because a row built
# from an oversized value would die on the same limit that caused the condition it reports.
if [ -z "$SEED_STATUS" ]; then
    writ_critical writ-subagent-start \
        "governance seeding did not run; this sub-agent starts with no mode and no gates. The operator who dispatched it may re-dispatch this worker, and writ doctor reports the failures under subagent-governance-census." \
        "$AGENT_ID"
    log_friction_event "$AGENT_ID" "" "subagent_seed_failed" \
        "{\"hook\":\"writ-subagent-start\",\"parent_session\":\"$PARENT_SESSION\"}"
fi

# The sub-agent's mode, read once here rather than at the bottom of the hook. It used
# to be resolved just before the subagent_start friction row (the last thing this hook
# does), which is AFTER the manual-test-grant decision below -- so that gate row, a
# governance record, was stamped with an empty mode while the value was one command
# away. Same command, same value, earlier.
#
# STILL RESOLVED HERE EXPLICITLY, even though SESSION_ID is now set above and
# _writ_row_mode's fallback (common.sh:93) could reach the same cache file: that fallback
# is a consolation prize for hooks that never computed the mode, and this hook did. The
# explicit value is the one the hook ACTED on, which is what the audit trail wants, and
# it keeps the mode on the gate row independent of the fallback's memoization.
CURRENT_MODE=$(_writ_session "mode get" "$AGENT_ID" 2>/dev/null || echo "")
CURRENT_MODE=$(echo "$CURRENT_MODE" | tr -d '[:space:]')

# Manual-testing grant inherits exactly like gates_approved above: the user's
# concession was given to the orchestrating session, and a dispatched worker acts
# on its behalf, so it is not re-typed per worker. The child gets the REMAINING
# TTL (never refreshed) and an inherited_from stamp for the audit trail. Keyed on
# PARENT_SESSION (the payload session id), NOT /tmp/writ-current-session -- the
# pointer churns across concurrent sessions and could leak another session's grant.
GRANT_LIB="$WRIT_DIR/bin/lib/manual_test_grant.py"
if [ -f "$GRANT_LIB" ] && [ -n "$PARENT_SESSION" ] && [ "$PARENT_SESSION" != "$AGENT_ID" ]; then
    if python3 "$GRANT_LIB" inherit "$PARENT_SESSION" "$AGENT_ID" 2>/dev/null; then
        log_gate_decision "manual-test-grant" "inherit" \
            "sub-agent inherited the parent session's live manual-testing grant" "$AGENT_ID"
    fi
fi

# Link this sub-agent to its parent so commit_capture's enumeration
# (_collect_subagent_queried_rules) can merge this child's queried rules at commit time.
# The link is the PAYLOAD's session id and nothing else.
#
# THIS USED TO PREFER /tmp/writ-current-session, and that was the same defect as
# everywhere else, with a worse blast radius: the pointer is ONE file rewritten by every
# Claude Code session on the machine, so a sub-agent spawned while another session had
# just taken a turn was stamped as a child of THAT session -- its queried rules merged
# into a stranger's commit, and this session's commit lost them. Invisible from both ends.
#
# WHAT THE POINTER BOUGHT, AND WHY LOSING IT IS SURVIVABLE: it named the committing
# session directly, so a deeply nested sub-agent linked straight to the root rather than
# to its immediate parent. Without it, a grandchild's parent_session_id is its immediate
# parent and the parent_match arm of _collect_subagent_queried_rules misses it. The
# path + recency arm still catches it: that cache holds queried rules for a committed
# file and was modified inside the window, which is exactly the case that arm was
# written for (writ/session/cache.py:409). So a nested worker's rules are merged by
# content instead of by linkage -- one arm narrower, and never wrong.
#
# THE CALL IS GUARDED, AND NOT WITH `|| true`. It runs under `set -euo pipefail` and reaches
# `python3 "$helper" update "$@"` in common.sh, which returns that child's exit code. A role
# name longer than MAX_ARG_STRLEN made execve fail here with 126 and killed the whole hook:
# no rules injected, no telemetry, nothing. Suppression would put back the silence this
# change exists to remove, so the outcome is captured and reported on the surface a reader
# of this hook is already trained on, and only the exit code is dropped.
PARENT="$PARENT_SESSION"
if [ -n "$PARENT" ] && [ "$PARENT" != "$AGENT_ID" ]; then
    LINK_STATUS="linked"
    _writ_session update "$AGENT_ID" --parent-session-id "$PARENT" \
        --agent-type "$AGENT_TYPE" || LINK_STATUS=""
    if [ -z "$LINK_STATUS" ]; then
        writ_critical writ-subagent-start \
            "the parent link was not written, so this worker's queried rules reach the parent's commit only by the path and recency arm. The operator who dispatched it may re-dispatch this worker to restore the link." \
            "$AGENT_ID"
    fi
fi

# Query Writ for rules if server is available
ADDITIONAL_CONTEXT=""
HEALTH=$(curl ${WRIT_CURL_TRANSPORT} -sf --connect-timeout 0.5 --max-time 1 "http://${WRIT_HOST}:${WRIT_PORT}/health" 2>/dev/null || echo "")
if [ -n "$HEALTH" ]; then
    # Build the retrieval query. Newer Claude Code sends the delegated task in a
    # `task` field; use it when present. CC 2.1.181's SubagentStart payload carries
    # ONLY agent_type (no task/prompt/description) -- verified from captured live
    # envelopes -- so without a fallback the query never runs and ZERO rules reach
    # the sub-agent. Fall back to a role-descriptive phrase keyed on agent_type
    # (a bare agent_type like "writ-explorer" retrieves poorly; a descriptive
    # phrase returns role-relevant rules).
    AGENT_PROMPT=$(echo "$STDIN_JSON" | python3 -c "
import sys, json
try:
    d = json.load(sys.stdin)
    prompt = d.get('task') or d.get('prompt') or d.get('description') or d.get('message') or ''
    print(prompt[:500])
except Exception:
    print('')
" 2>/dev/null || echo "")
    QUERY_SOURCE="task"

    if [ -z "$AGENT_PROMPT" ] || [ ${#AGENT_PROMPT} -le 10 ]; then
        QUERY_SOURCE="agent_type"
        case "$AGENT_TYPE" in
            *explor*)    AGENT_PROMPT="explore and understand codebase architecture, structure, conventions, and existing patterns" ;;
            *planner*)   AGENT_PROMPT="design an implementation plan with architecture decisions and trade-offs" ;;
            *implement*) AGENT_PROMPT="implement production code with correct error handling and project conventions" ;;
            *test*)      AGENT_PROMPT="write tests: skeletons, assertions, fixtures, isolation, and coverage" ;;
            *review*)    AGENT_PROMPT="review code for correctness, quality, security, and spec compliance" ;;
            *)           AGENT_PROMPT="software engineering best practices: clean code, correctness, security, testing, error handling" ;;
        esac
    fi

    if [ -n "$AGENT_PROMPT" ] && [ ${#AGENT_PROMPT} -gt 10 ]; then
        # The DISPATCHING project's root: this hook runs in the parent Claude Code
        # process, so its cwd is the project that spawned the sub-agent. Without it the
        # sub-agent's one and only rule injection is unscoped, so a dispatched worker
        # could be governed by a different project's records than its dispatcher.
        # detect_project_root is pure bash (no spawn).
        _PROJECT_ROOT=$(detect_project_root "$(pwd -P)")
        RESPONSE=$(python3 -c "
import json, sys
print(json.dumps({
    'query': sys.argv[1][:500],
    'budget_tokens': 2000,
    'exclude_rule_ids': [],
    'project_root': sys.argv[2],
}))
" "$AGENT_PROMPT" "$_PROJECT_ROOT" 2>/dev/null | \
            curl ${WRIT_CURL_TRANSPORT} -s --connect-timeout 0.5 --max-time 2 \
                -X POST "http://${WRIT_HOST}:${WRIT_PORT}/query" \
                -H "Content-Type: application/json" \
                -d @- 2>/dev/null) || true

        if [ -n "$RESPONSE" ]; then
            RULES_TEXT=$(echo "$RESPONSE" | _writ_session format 2>/dev/null) || true
            if [ -n "$RULES_TEXT" ]; then
                RULES_ONLY=$(echo "$RULES_TEXT" | grep -v "^WRIT_META:" || true)
                ADDITIONAL_CONTEXT="$RULES_ONLY"
                # Observability: record that rules were injected into this sub-agent
                # (count + ids + whether the query came from the real task or the
                # agent_type fallback). The ABSENCE of this event for an agent now
                # means it got 0 rules -- the gap that was previously invisible.
                RULES_INJECTED_EXTRA=$(echo "$RESPONSE" | AGENT_TYPE="$AGENT_TYPE" QSRC="$QUERY_SOURCE" python3 -c "
import sys, json, os
try:
    rs = json.load(sys.stdin).get('rules', [])
except Exception:
    rs = []
print(json.dumps({
    'agent_type': os.environ.get('AGENT_TYPE', ''),
    'query_source': os.environ.get('QSRC', ''),
    'rule_count': len(rs),
    'rule_ids': [r.get('rule_id', '') for r in rs],
}))
" 2>/dev/null || echo '{}')
                log_friction_event "$AGENT_ID" "" "subagent_rules_injected" "$RULES_INJECTED_EXTRA"
            fi
        fi
    fi
fi

# Get the parent's phase state for context injection.
#
# AN EMPTY PARENT SESSION READS NO CACHE AT ALL, and the distinction is load-bearing: `{}`
# renders the documented defaults (mode=work, phase=planning, gates=none), while a cache
# read for the empty id would answer with the default cache and render mode=None. That is
# the fallback branch the critical above already recorded.
PHASE_INFO=$(WRIT_DIR="$WRIT_DIR" WRIT_SA_PARENT="$PARENT_SESSION" python3 - <<'PY' 2>/dev/null || echo "[Writ sub-agent: isolated session]"
import os, sys
sys.path.insert(0, os.environ.get('WRIT_DIR', ''))

parent_session = os.environ.get('WRIT_SA_PARENT', '')
if parent_session:
    from writ.session.cache import _read_cache
    parent = _read_cache(parent_session)
else:
    parent = {}
mode = parent.get('mode', 'work')
phase = parent.get('current_phase', 'planning')
gates = parent.get('gates_approved', [])
joined = ','.join(gates) if gates else 'none'
print('[Writ sub-agent: mode={}, phase={}, gates={}]'.format(mode, phase, joined))
PY
)

# The plan artifacts are scoped to the PARENT's session, not this worker's agent id: the
# parent is the session whose gates get approved, so a plan written under the worker's own
# id is a plan its orchestrator cannot find. The path is computed from the same resolver the
# gate reads with, and injected because agents/writ-planner.md is static text that cannot
# interpolate a session id of its own.
#
# WRIT_DIR IS PASSED ON THE COMMAND, and that is a fix rather than a restatement. It is a
# plain, unexported bash variable at the top of this file, so the child's
# `os.environ.get('WRIT_DIR', '')` read the EMPTY STRING on every real dispatch,
# `sys.path.insert(0, '')` inserted the current directory, and the import only resolved when
# the dispatching project happened to be the skill directory itself. Every other project lost
# this line silently.
PLAN_DIR_INFO=$(WRIT_DIR="$WRIT_DIR" WRIT_SA_PARENT="$PARENT_SESSION" python3 - <<'PY' 2>/dev/null || echo ""
import os, sys
sys.path.insert(0, os.environ.get('WRIT_DIR', ''))

parent_session = os.environ.get('WRIT_SA_PARENT', '')
try:
    from writ.session.cache import _read_cache
    from writ.session.locators import plan_dir
    root = (_read_cache(parent_session).get('project_root') or '') if parent_session else ''
    d = plan_dir(root, parent_session)
    print('[Writ plan artifacts: write plan.md and capabilities.md to {}/]'.format(d)
          if d else '')
except Exception:
    print('')
PY
)
if [ -n "$PLAN_DIR_INFO" ]; then
    PHASE_INFO="$PHASE_INFO
$PLAN_DIR_INFO"
fi

# Inject via additionalContext
if [ -n "$ADDITIONAL_CONTEXT" ] || [ -n "$PHASE_INFO" ]; then
    SA_OUTPUT=$(python3 -c "
import json, sys
ctx = sys.argv[1]
if sys.argv[2]:
    ctx = sys.argv[2] + '\n' + ctx
print(json.dumps({
    'hookSpecificOutput': {
        'hookEventName': 'SubagentStart',
        'additionalContext': ctx,
    }
}))
" "$ADDITIONAL_CONTEXT" "$PHASE_INFO" 2>/dev/null)
    emit_hook_reply "$SA_OUTPUT" "" "$AGENT_ID"
fi

# Log sub-agent start to friction log (common.sh already sourced at top).
# CURRENT_MODE was resolved right after the sub-agent's cache was created, so the
# manual-test-grant gate row above carries it too. Nothing between there and here
# changes this agent's mode.
# BOUNDED FOR THE ROW, because this is an exec boundary like every other one this cycle
# closed. $AGENT_TYPE is attacker-or-caller-supplied and arrives unbounded, and it is
# interpolated into a JSON string passed as an ARGUMENT, so a large role name blows this
# exec with E2BIG exactly as the parent cache blew the seed exec. log_friction_event's
# internal `|| true` would then swallow it and the dispatch would lose its subagent_start
# row silently.
#
# THE NARROW CASE IS THE DANGEROUS ONE, and it is why truncating matters rather than being
# tidy: the JSON wrapper adds around 50 to 90 bytes, so a role name just under the kernel
# cap PASSES the seed exec (seeding succeeds, so no subagent_seed_failed row is written)
# and still overflows here. Neither row fires, and _subagent_governance_census then counts
# a genuinely governed dispatch as unreachable. That is a miscount in the very census this
# cycle exists to make honest.
#
# Truncating the LOGGED value only. The role itself is untouched: it has already been
# resolved, stamped into the cache and used for injection above.
log_friction_event "$AGENT_ID" "$CURRENT_MODE" "subagent_start" \
    "{\"agent_type\":\"${AGENT_TYPE:0:128}\",\"parent_session\":\"${PARENT_SESSION:0:128}\"}"

exit 0
