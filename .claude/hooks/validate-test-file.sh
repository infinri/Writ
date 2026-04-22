#!/usr/bin/env bash
# Phase 2: Gate 5 Tier 1 test-file assertion gate (ENF-PROC-TDD-001).
#
# PreToolUse on Write matching src/**/*.{py,js,ts,php,go,rs,java}.
# Denies if no corresponding test file exists with lexical assertion markers.
# Bypass: session.mode == "prototype" (reserved for throwaway work).
# Feature-flag gated.
set -euo pipefail
HOOK_DIR="$(cd "$(dirname "$0")" && pwd)"
WRIT_DIR="$(cd "$HOOK_DIR/../.." && pwd)"
SESSION_HELPER="$WRIT_DIR/bin/lib/writ-session.py"
source "$WRIT_DIR/bin/lib/common.sh"

PARSED=$(parse_hook_stdin)
SESSION_ID=$(detect_session_id "$PARSED")
[ -z "$SESSION_ID" ] && exit 0
is_work_mode "$SESSION_ID" || exit 0

# Prototype mode bypass (Section 0.4 decision 2, manual trigger only).
MODE=$(python3 "$SESSION_HELPER" mode get "$SESSION_ID" 2>/dev/null | tr -d '[:space:]')
[ "$MODE" = "prototype" ] && exit 0

FILE=$(parsed_field "$PARSED" "file_path")
[ -z "$FILE" ] && exit 0

DENY=$(python3 <<'PY'
import os, re, sys
f = sys.argv[1]
ext = os.path.splitext(f)[1].lstrip(".")
# Only apply to source files.
if ext not in {"py", "js", "ts", "php", "go", "rs", "java"}:
    sys.exit(0)
# Only apply to files under src/, lib/, app/, or similar.
if not re.search(r"/(src|lib|app|writ)/", f):
    sys.exit(0)
# Derive plausible test paths. Convention: tests/test_X.{py} for src/X.py; specs, etc.
base = os.path.basename(f)
stem = os.path.splitext(base)[0]
# Find repo root.
repo = os.getcwd()
candidates = []
if ext == "py":
    candidates += [f"tests/test_{stem}.py", f"tests/test_{stem}s.py"]
elif ext in {"js", "ts"}:
    candidates += [f"tests/{stem}.test.{ext}", f"tests/{stem}.spec.{ext}"]
elif ext == "php":
    candidates += [f"tests/Unit/{stem}Test.php", f"tests/{stem}Test.php"]
elif ext == "go":
    candidates += [f.replace(".go", "_test.go")]
elif ext == "rs":
    candidates += [f"tests/{stem}.rs"]
elif ext == "java":
    candidates += [f"src/test/java/{stem}Test.java"]
marker_re = re.compile(r"\b(assert|expect|should|test_)\w*")
for c in candidates:
    path = os.path.join(repo, c)
    if os.path.isfile(path):
        try:
            with open(path) as fh:
                if marker_re.search(fh.read()):
                    sys.exit(0)
        except OSError:
            pass
# No test file with assertions found.
print(f"ENF-PROC-TDD-001: writing '{os.path.relpath(f, repo)}' requires a test file with assertions. Expected at one of: {', '.join(candidates)}. Bypass: set session.mode=prototype for throwaway work.")
PY
"$FILE")

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
