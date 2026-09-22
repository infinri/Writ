#!/usr/bin/env bash
# Universal passive black-box capture. Reads the raw stdin envelope, logs it (gated by the
# blackbox sentinel / WRIT_BLACKBOX), emits NOTHING, exits 0. Registered first on tool and
# lifecycle events so every event's raw input is captured regardless of which functional hook
# (if any) also handles it. Behavior-neutral: never writes stdout, never blocks, never fails.
#
# When capture is OFF this exits before sourcing common.sh: just one sentinel stat + a stdin
# drain, so the permanent hot-path cost is a single cheap bash spawn per tool call.
[ "${WRIT_BLACKBOX:-}" = "1" ] || [ -f "${HOME:-}/.claude/writ-blackbox.on" ] || { cat >/dev/null 2>&1; exit 0; }
HOOK_DIR="$(cd "$(dirname "$0")" && pwd)"
WRIT_DIR="$(cd "$HOOK_DIR/../.." && pwd)"
source "$WRIT_DIR/bin/lib/common.sh" 2>/dev/null || { cat >/dev/null 2>&1; exit 0; }
hook_instrument "writ-blackbox-capture"

# THE IDENTITY IS RESOLVED BEFORE THE ROW IS FILED, and everything about where these four
# lines sit is the fix.
#
# Measured 2026-09-21: 71 rows in var/session/writ-events-unknown.buf, every one of them
# from this hook. hook_instrument's exit trap files under ${SESSION_ID:-${HOOK_SESSION_ID:-}}
# and this hook set neither, so the buffer rendered the literal `unknown` and the rows waited
# for the abandoned-session sweep under a name that belongs to no session.
#
# AFTER THE OPT-IN GATE, NEVER ABOVE IT. Capture off is the common path and stays byte for
# byte what it was: the sentinel test at the top exits before common.sh is sourced, so the
# hook is still one bash spawn plus a stdin drain. Moving this work above that line would put
# two processes on every tool call for a debug switch that is off.
#
# `$(cat)` AND NOT `read -r -d ''`: the builtin costs no process and truncates at a NUL byte,
# and fidelity of the raw envelope is the entire point of capture. This is also the shape
# load_hook_env uses, trailing-newline strip included, so the captured payload is unchanged
# from the 22 hooks that capture through it.
#
# `_writ_parse_hook_stdin` AND NOT `load_hook_env`, which does exactly this parse and then
# calls _writ_seed_subagent_cache. This hook is behavior neutral and registered on every
# event; making it seed caches would turn a passive capture into a writer of governance
# state. The parser already collapses `agent_id // session_id`, so a sub-agent's rows land in
# the buffer writ-subagent-stop drains rather than in its parent's, and that rule is not
# reimplemented here.
#
# A LITERAL SESSION_ID ASSIGNMENT, not the eval's side effect alone: the trap reads
# SESSION_ID late, at exit, and the directory-derived sweep in
# tests/test_hook_session_attribution.py reads this hook's own source for an assignment.
STDIN_DATA=$(cat)
eval "$(printf '%s' "$STDIN_DATA" | _writ_parse_hook_stdin)"
SESSION_ID="${HOOK_SESSION_ID:-}"
printf '%s' "$STDIN_DATA" | blackbox_log in writ-blackbox-capture "$SESSION_ID" || true
exit 0
