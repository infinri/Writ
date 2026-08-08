#!/usr/bin/env bash
# Session-state write gate: the grant store is off limits to the agent.
#
# Gate state (mode, approved gates, the manual-testing grant) lives in the session
# cache. If the agent could write there it could approve its own gates, so this
# denies any Write/Edit whose target lands in that directory. Unconditional and
# server-independent, the same shape as the credential deny in the Bash write gate,
# because a gate the agent can edit is not a gate.
#
# The Bash side is covered by writ-bash-write-gate.sh.
#
# Hook type: PreToolUse (matcher: Write|Edit|NotebookEdit)
# Exit: always 0 (deny via emit_deny JSON).

set -euo pipefail

HOOK_DIR="$(cd "$(dirname "$0")" && pwd)"
WRIT_DIR="$(cd "$HOOK_DIR/../.." && pwd)"
source "$WRIT_DIR/bin/lib/common.sh"
hook_instrument "writ-state-write-gate"

load_hook_env
FILE="$HOOK_FILE_PATH"
[ -z "$FILE" ] && exit 0

PROTECTED_DIR="${WRIT_CACHE_DIR:-$WRIT_DIR/var/session}"
POINTER_FILE="/tmp/writ-current-session"

VERDICT=$(WRIT_TGT="$FILE" WRIT_DIR_PROT="$PROTECTED_DIR" WRIT_PTR="$POINTER_FILE" python3 <<'PY'
import os

target = os.environ.get('WRIT_TGT', '')
protected_dir = os.environ.get('WRIT_DIR_PROT', '')
pointer = os.environ.get('WRIT_PTR', '')

# realpath both sides so a symlink or a ../ walk cannot slip past the prefix test.
def canon(path):
    try:
        return os.path.realpath(os.path.abspath(path))
    except Exception:
        return os.path.abspath(path)

t = canon(target)
d = canon(protected_dir)

if t == canon(pointer):
    print('pointer')
elif d and (t == d or t.startswith(d + os.sep)):
    print('state')
PY
) || true

if [ -n "$VERDICT" ]; then
    REASON="[ENF-GATE-STATE] Refusing this write: '$FILE' is Writ gate state. Approvals, mode and the manual-testing grant are recorded there, so the agent does not get to edit them. A manual-testing bypass is minted only from the user's own words -- ask the user to reply \"manual testing approved\"."
    log_gate_decision "state-write" "deny" "$REASON" "$FILE"
    emit_deny "$REASON"
fi

exit 0
