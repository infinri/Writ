#!/usr/bin/env bash
# Manual-testing grant minter (ENF-PROC-TDD-001 concession).
#
# UserPromptSubmit: fires at the start of every user turn, and its payload is the
# user's literal typed text. That is the whole point of putting the minter here --
# the assistant has no tool that fires this hook, so it cannot grant itself a
# bypass. See bin/lib/manual_test_grant.py for the trust model.
#
# Hook type: UserPromptSubmit
# Exit: always 0 (never block a user prompt). Directive, if any, on stdout.

set -euo pipefail

HOOK_DIR="$(cd "$(dirname "$0")" && pwd)"
WRIT_DIR="$(cd "$HOOK_DIR/../.." && pwd)"
GRANT_LIB="$WRIT_DIR/bin/lib/manual_test_grant.py"
source "$WRIT_DIR/bin/lib/common.sh"
hook_instrument "writ-manual-test-grant"

[ -f "$GRANT_LIB" ] || exit 0

STDIN_JSON=$(cat)

PARSED=$(echo "$STDIN_JSON" | python3 -c "
import sys, json
try:
    data = json.load(sys.stdin)
    sid = data.get('agent_id', '') or data.get('session_id', '')
    prompt = data.get('prompt', data.get('message', data.get('content', '')))
    print(sid)
    print(prompt.replace('\n', ' '))
except Exception:
    print('')
    print('')
" 2>/dev/null) || true

SESSION_ID=$(echo "$PARSED" | head -1)
PROMPT=$(echo "$PARSED" | sed -n '2p')

[ -z "$SESSION_ID" ] && SESSION_ID=$(cat /tmp/writ-current-session 2>/dev/null || true)
[ -z "$SESSION_ID" ] && exit 0

# SEC-INJ-CMD-001: the prompt is passed as an argv element, never interpolated
# into the python source.
if ! python3 "$GRANT_LIB" is-phrase "$PROMPT" 2>/dev/null; then
    exit 0
fi

# The grant row is only logged after `active` re-reads the grant from disk in a
# SEPARATE process. Trusting mint's exit status alone once produced a success row
# with no grant behind it, which sent a whole debugging session the wrong way.
GRANT_PATH=$(python3 "$GRANT_LIB" path "$SESSION_ID" 2>/dev/null) || GRANT_PATH=""
if python3 "$GRANT_LIB" mint "$SESSION_ID" "$PROMPT" 2>/dev/null \
        && python3 "$GRANT_LIB" active "$SESSION_ID" >/dev/null 2>&1; then
    log_gate_decision "manual-test-grant" "grant" "user conceded manual testing; grant verified by read-back" "$GRANT_PATH"
    cat <<'DIRECTIVE'
[Writ: manual-testing grant is live for 30 minutes]
The user has conceded manual testing, so ENF-PROC-TDD-001 will admit production
files without a test file for that window. Every admitted file is recorded in the
session audit trail.

Obligations while the grant is live:
  1. Only lean on it for code that genuinely has no runnable harness. If a test
     can run, write the test.
  2. State plainly which files went in under manual testing.
  3. Give the user the concrete steps to verify by hand: what to open, what to do,
     and the expected result.
DIRECTIVE
else
    log_gate_decision "manual-test-grant" "error" "grant phrase matched but mint or read-back failed" "$GRANT_PATH"
fi

exit 0
