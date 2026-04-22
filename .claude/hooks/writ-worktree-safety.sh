#!/usr/bin/env bash
# Phase 2: enforce worktree gitignore safety (ENF-PROC-WORKTREE-001).
#
# PreToolUse on Bash matching `git worktree add`. Denies if the target
# path is project-local and not in .gitignore. Feature-flag gated.
set -euo pipefail
HOOK_DIR="$(cd "$(dirname "$0")" && pwd)"
WRIT_DIR="$(cd "$HOOK_DIR/../.." && pwd)"
source "$WRIT_DIR/bin/lib/common.sh"

is_superpowers_absorb_enabled || exit 0

PARSED=$(parse_hook_stdin)
SESSION_ID=$(detect_session_id "$PARSED")
[ -z "$SESSION_ID" ] && exit 0
is_work_mode "$SESSION_ID" || exit 0

CMD=$(echo "$PARSED" | python3 -c "import sys,json; d=json.load(sys.stdin); print((d.get('tool_input') or {}).get('command',''))" 2>/dev/null)
# Only act on `git worktree add` commands.
case "$CMD" in
    *"git worktree add"*) ;;
    *) exit 0 ;;
esac

DENY=$(python3 <<'PY'
import os, re, sys
cmd = sys.argv[1]
# Extract the path argument. `git worktree add [opts] <path> [branch]`
m = re.search(r'git\s+worktree\s+add\s+((?:--?\S+\s+)*)(\S+)', cmd)
if not m:
    sys.exit(0)
target = m.group(2)
# Absolute paths or paths outside the repo tree are not project-local.
repo_root = os.getcwd()
abs_target = os.path.abspath(target)
if not abs_target.startswith(repo_root + os.sep) and abs_target != repo_root:
    sys.exit(0)
# Compute path relative to repo root.
rel = os.path.relpath(abs_target, repo_root)
# Check .gitignore for a matching entry.
ignore_path = os.path.join(repo_root, ".gitignore")
if not os.path.exists(ignore_path):
    print(f"ENF-PROC-WORKTREE-001: project-local worktree target '{rel}' but no .gitignore exists. Add an entry for '{rel}' (or a parent like '.worktrees/') before creating the worktree.")
    sys.exit(0)
with open(ignore_path) as f:
    ignored = [line.strip() for line in f if line.strip() and not line.startswith("#")]
# Match the rel path against gitignore patterns. Simple prefix match for directories.
top = rel.split(os.sep)[0]
matched = any(
    top == p.strip("/") or p.rstrip("/") == top or p.startswith(top + "/")
    for p in ignored
)
if not matched:
    print(f"ENF-PROC-WORKTREE-001: project-local worktree target '{rel}' is not matched by any .gitignore entry. Add '{top}/' to .gitignore before creating the worktree.")
PY
"$CMD")

if [ -n "$DENY" ]; then
    python3 -c "
import json
print(json.dumps({
    'hookSpecificOutput': {
        'hookEventName': 'PreToolUse',
        'permissionDecision': 'deny',
        'permissionDecisionReason': '''$DENY'''
    }
}))"
fi
exit 0
