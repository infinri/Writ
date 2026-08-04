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
hook_instrument "validate-test-file"

load_hook_env
SESSION_ID="$HOOK_SESSION_ID"
[ -z "$SESSION_ID" ] && exit 0
is_work_mode "$SESSION_ID" || exit 0

# Prototype mode bypass (Section 0.4 decision 2, manual trigger only).
MODE=$(python3 "$SESSION_HELPER" mode get "$SESSION_ID" 2>/dev/null | tr -d '[:space:]')
[ "$MODE" = "prototype" ] && exit 0

FILE="$HOOK_FILE_PATH"
[ -z "$FILE" ] && exit 0

# Manual-testing grant: the user conceded hand verification for code with no
# runnable harness. Minted only from the user's own words by
# writ-manual-test-grant.sh (UserPromptSubmit), so the agent cannot self-serve;
# the store is write-denied by writ-state-write-gate.sh. Every admitted file is
# recorded on the grant for the audit trail.
GRANT_LIB="$WRIT_DIR/bin/lib/manual_test_grant.py"
if [ -f "$GRANT_LIB" ] && python3 "$GRANT_LIB" admit "$SESSION_ID" "$FILE" >/dev/null 2>&1; then
    log_gate_decision "test-first" "allow" "manual-testing grant admitted the file" "$FILE"
    exit 0
fi

DENY=$(WRIT_DIR_ABS="$WRIT_DIR" python3 - "$FILE" <<'PY'
import os, re, sys
f = sys.argv[1]
ext = os.path.splitext(f)[1].lstrip(".")
# Only apply to source files.
if ext not in {"py", "js", "ts", "php", "go", "rs", "java"}:
    sys.exit(0)
# Skill-internal source is governed by the work-mode test-skeletons gate
# (_validate_test_skeletons), which already requires an assertion-bearing test this
# session before implementation. The skill uses increment-named tests, not the
# tests/test_<stem> convention this gate assumes, so do not double-police it here.
# Mirrors _can_write_check's skill_dir exemption (bin/lib/writ-session.py).
writ_dir = os.environ.get("WRIT_DIR_ABS", "")
if writ_dir and os.path.abspath(f).startswith(writ_dir.rstrip("/") + os.sep):
    sys.exit(0)
# Compute a repo-relative path so the regex below doesn't false-positive on
# absolute paths whose ancestor dirs happen to be named src/lib/app (e.g. a user
# project nested under such a directory).
repo = os.getcwd()
try:
    rel = os.path.relpath(f, repo)
except ValueError:
    rel = f
# Test files are never production code -- exempt them up front so files under
# tests/ never trip the "production code without a failing test" gate.
norm = rel.replace(os.sep, "/")
if norm.startswith("tests/") or "/tests/" in norm or norm.startswith("test/") or "/test/" in norm \
        or norm.startswith("Test/") or "/Test/" in norm:
    sys.exit(0)
# Only apply to files under src/, lib/, or app/ at the repo root or immediately
# under a recognized package directory. Anchor the regex to the repo-relative path.
if not re.match(r"^(src|lib|app)/", norm):
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
    # Magento modules keep unit tests inside the module, mirroring the source
    # subpath under Test/Unit/ -- app/code/V/M/Plugin/X.php is tested by
    # app/code/V/M/Test/Unit/Plugin/XTest.php.
    magento_module = re.match(r"^(app/code/[^/]+/[^/]+)/(.+)\.php$", norm)
    if magento_module:
        candidates.append(
            f"{magento_module.group(1)}/Test/Unit/{magento_module.group(2)}Test.php"
        )
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
)

if [ -n "$DENY" ]; then
    log_gate_decision "test-first" "deny" "$DENY" "${FILE_PATH:-}"
    emit_deny "$DENY"
else
    log_gate_decision "test-first" "allow" "test file with assertions found" "${FILE_PATH:-}"
fi
exit 0
