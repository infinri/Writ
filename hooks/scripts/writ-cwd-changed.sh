#!/usr/bin/env bash
# Writ CwdChanged hook -- fires when working directory changes
#
# Detects the project domain from marker files in the new cwd and stores
# it in the session cache as detected_domain. The RAG injection hook reads
# this field to pass a domain hint to the /query endpoint.
#
# Domain detection is heuristic (file-existence checks, no parsing).
#
# Hook type: CwdChanged
# Exit: always 0 (advisory only)

set -euo pipefail

HOOK_DIR="$(cd "$(dirname "$0")" && pwd)"
WRIT_DIR="$(cd "$HOOK_DIR/../.." && pwd)"
SESSION_HELPER="$WRIT_DIR/bin/lib/writ-session.py"
source "$WRIT_DIR/bin/lib/common.sh"

HOOK_START_NS=$(hook_timer_start)

# Read stdin JSON envelope
STDIN_JSON=$(cat)

# Extract session_id and cwd from envelope
PARSED=$(echo "$STDIN_JSON" | python3 -c "
import sys, json
try:
    data = json.load(sys.stdin)
    sid = data.get('agent_id', '') or data.get('session_id', '')
    cwd = data.get('cwd', '')
    print(f'{sid}\n{cwd}')
except Exception:
    print('\n')
" 2>/dev/null) || true

SESSION_ID=$(echo "$PARSED" | head -1)
NEW_CWD=$(echo "$PARSED" | sed -n '2p')

# Fallback session ID
# NO SYNTHESIZED SESSION ID. This used to fall back to the parent PID and then to
# md5(cwd:user)+date. Neither can ever equal the id Claude Code uses, so state written
# under one is written to a session that does not exist and is simply never read again,
# while the hook reports success. Claude Code documents session_id as universal and
# authoritative on every hook event, so an empty one is a broken invariant, not a case to
# paper over: record it and stop.
if [ -z "$SESSION_ID" ]; then
    writ_critical writ-cwd-changed "no session_id in hook payload; refusing to synthesize one"
    exit 0
fi

# Read current mode for friction logging
CURRENT_MODE=$(_writ_session "mode get" "$SESSION_ID" 2>/dev/null || echo "")
CURRENT_MODE=$(echo "$CURRENT_MODE" | tr -d '[:space:]')

# Detect domain from marker files in the new cwd
# Priority order: composer.json, pyproject.toml, package.json, Cargo.toml, go.mod
DETECTED_DOMAIN="universal"
if [ -n "$NEW_CWD" ]; then
    if [ -f "$NEW_CWD/composer.json" ]; then
        DETECTED_DOMAIN="php"
    elif [ -f "$NEW_CWD/pyproject.toml" ]; then
        DETECTED_DOMAIN="python"
    elif [ -f "$NEW_CWD/package.json" ]; then
        DETECTED_DOMAIN="javascript"
    elif [ -f "$NEW_CWD/Cargo.toml" ]; then
        DETECTED_DOMAIN="rust"
    elif [ -f "$NEW_CWD/go.mod" ]; then
        DETECTED_DOMAIN="go"
    fi
fi

# Update session cache with detected domain (locked/atomic via the update CLI;
# never hand-roll the write -- a torn write here can wipe the whole session cache)
python3 "$SESSION_HELPER" update "$SESSION_ID" --set-detected-domain "$DETECTED_DOMAIN" 2>/dev/null || true

# Log friction event
log_friction_event "$SESSION_ID" "${CURRENT_MODE:-}" "cwd_changed" \
    "{\"detected_domain\":\"$DETECTED_DOMAIN\",\"cwd\":\"$NEW_CWD\"}"

# Auto-install the Writ git hooks on first work-mode entry into a repo (Phase 1d,
# ADR-1d-3). Fires only when mode is work, the cwd is inside a git repo, and the
# Writ marker is not already present in the repo's post-commit hook (the hook Writ
# installs; the older commit-message hook is retired). The daemon does the install
# over HTTP; a down daemon, non-repo cwd, or already-installed state is a silent
# no-op. Fail-open: never blocks the session.
if [ "$CURRENT_MODE" = "work" ] && [ -n "$NEW_CWD" ]; then
    REPO_ROOT=$(git -C "$NEW_CWD" rev-parse --show-toplevel 2>/dev/null || echo "")
    if [ -n "$REPO_ROOT" ]; then
        GIT_COMMON=$(git -C "$NEW_CWD" rev-parse --git-common-dir 2>/dev/null || echo "")
        case "$GIT_COMMON" in
            /*) ;;
            "") GIT_COMMON="" ;;
            *) GIT_COMMON="$NEW_CWD/$GIT_COMMON" ;;
        esac
        POST_COMMIT_HOOK="$GIT_COMMON/hooks/post-commit"
        if [ -z "$GIT_COMMON" ] || ! grep -q "# >>> Writ" "$POST_COMMIT_HOOK" 2>/dev/null; then
            AUTO_PAYLOAD=$(WRIT_REPO_ROOT="$REPO_ROOT" python3 -c 'import json, os; print(json.dumps({"project_root": os.environ["WRIT_REPO_ROOT"]}))' 2>/dev/null || echo "")
            AUTO_RESP=$(curl -s --connect-timeout 0.2 --max-time 1 \
                -X POST "${WRIT_SESSION_BASE}/git-hooks/auto-install" \
                -H "Content-Type: application/json" \
                -d "$AUTO_PAYLOAD" 2>/dev/null || true)
            if printf '%s' "$AUTO_RESP" | grep -q '"installed"[[:space:]]*:[[:space:]]*true'; then
                echo "Writ: installed git hooks in $REPO_ROOT" >&2
            fi
        fi
    fi
fi

hook_timer_end "$HOOK_START_NS" "writ-cwd-changed" "$SESSION_ID" "${CURRENT_MODE:-}"
exit 0
