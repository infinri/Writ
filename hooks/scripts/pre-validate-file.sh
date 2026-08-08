#!/bin/bash
# Pre-write validation hook -- validates content BEFORE the file is written.
# PreToolUse: fires before every Write/Edit/MultiEdit.
#
# HOW THIS BLOCKS: it does NOT use the exit code. Every path below exits 0; a refusal is
# the `permissionDecision: "deny"` JSON that emit_deny (or the inline equivalent at the
# bottom) prints on stdout, and Claude Code reads that. The header used to say "Exit
# non-zero = BLOCKS the write", which was true of an older contract and is now the kind
# of prose that gets the next author to "fix" a blocking gate by changing an exit code
# nothing reads -- and a non-zero exit here is a hook ERROR, not a deny.
# Output: the deny JSON above, plus structured JSON per finding from the analyzers.
#
# Creates a temp file with the proposed content, runs analysis, cleans up.

SKILL_DIR="$(cd "$(dirname "$0")/../.." && pwd)"
source "$SKILL_DIR/bin/lib/common.sh"
hook_instrument "pre-validate-file"

# Parse the Claude Code hook stdin envelope (one python3 spawn)
load_hook_env
FILE="$HOOK_FILE_PATH"

if [ -z "$FILE" ]; then exit 0; fi

# Seed scripts under scripts/seed_*.py contain rule definitions whose
# violation/pass_example fields carry literal bad-code samples from the
# public rulebook. The security analyzers would flag every example as a
# violation. Skip pre-validation for these documentation-bearing scripts.
case "$FILE" in
  */scripts/seed_*.py|scripts/seed_*.py) exit 0 ;;
esac

TMPFILE=""
cleanup() { [ -n "$TMPFILE" ] && rm -f "$TMPFILE"; }
# writ_on_exit, NOT `trap cleanup EXIT`. bash allows one EXIT trap, so installing one
# here replaced hook_instrument's trap and this hook silently stopped recording its
# telemetry row. Registration runs the same cleanup at the same time and keeps the
# instrumentation. Pinned by tests/test_exit_trap_ownership.py.
writ_on_exit cleanup

TMPFILE=$(echo "$HOOK_ENVELOPE" | python3 -c "
import sys, json, os, tempfile

data = json.load(sys.stdin)
fp = data.get('file_path', '')
if not fp:
    sys.exit(0)

ext = os.path.splitext(fp)[1]
ti = data.get('tool_input', {})

if data.get('content'):
    content = data['content']
elif ti.get('old_string') and os.path.exists(fp):
    with open(fp) as f:
        content = f.read()
    content = content.replace(ti['old_string'], ti.get('new_string', ''), 1)
else:
    sys.exit(0)

tf = tempfile.mktemp(suffix=ext, prefix='claude-preval-', dir='/tmp')
with open(tf, 'w') as f:
    f.write(content)
print(tf)
" 2>/dev/null)

if [ -z "$TMPFILE" ] || [ ! -f "$TMPFILE" ]; then exit 0; fi

# ── Shell guard ──────────────────────────────────────────────────────────────
# Runs BEFORE the language gate below, because detect_language answers "unknown" for
# .sh and this hook used to exit right there. That is how a shell file that does not
# parse reached disk on 2026-08-07: an `if` without its `fi` in bin/lib/common.sh, which
# all 37 hooks source. Every hook then failed at the parse, Claude Code reads a failing
# PreToolUse hook as a deny, and Bash, Read and Edit stopped working at once. The file
# that repairs it cannot be reached, because reaching it needs a tool.
#
# The check is on the PROPOSED content ($TMPFILE already holds it), so a broken file is
# refused instead of written. `bash -n` parses without executing.
case "$FILE" in
  *.sh|*.bash)
    SHELL_ERR=$(bash -n "$TMPFILE" 2>&1) || {
      # Report the parse error itself: a deny that does not say what is wrong sends the
      # next person hunting, and this one fires exactly when tooling is about to break.
      SHELL_REASON="[ENF-POST-008] Shell syntax error in $FILE, write refused: ${SHELL_ERR//$TMPFILE/$FILE}"
      emit_deny "$SHELL_REASON"
      log_gate_decision "shell-syntax" "deny" "$SHELL_REASON" "$FILE"
      exit 0
    }

    # Second shape, and the more dangerous one because it parses cleanly: a file whose
    # live code is entirely commented out. The 2026-08-07 recovery did this to
    # common.sh (1409 lines) and writ-rag-inject.sh (585 lines), which silently
    # disabled every helper, every gate, rule injection and mode auto-routing. Nothing
    # failed loudly. Only a LIVE -> DEAD transition is refused; a new comment-only file
    # or a stub is ordinary and passes.
    if [ -f "$FILE" ]; then
      # NO `|| echo 0`: grep -c PRINTS 0 and EXITS 1 when nothing matches, so the
      # fallback appended a second 0 and the comparison below died on "0\n0". Same trap
      # cost a measurement harness an hour earlier today.
      OLD_LIVE=$(grep -vcE '^[[:space:]]*(#|$)' "$FILE" 2>/dev/null); OLD_LIVE=${OLD_LIVE:-0}
      NEW_LIVE=$(grep -vcE '^[[:space:]]*(#|$)' "$TMPFILE" 2>/dev/null); NEW_LIVE=${NEW_LIVE:-0}
      if [ "${OLD_LIVE:-0}" -gt 0 ] && [ "${NEW_LIVE:-0}" -eq 0 ]; then
        DEAD_REASON="[ENF-POST-008] $FILE has $OLD_LIVE live lines and the proposed content has none: commenting out a whole shell file disables it silently rather than failing loudly. Delete it, or keep the code."
        emit_deny "$DEAD_REASON"
        log_gate_decision "shell-commented-out" "deny" "$DEAD_REASON" "$FILE"
        exit 0
      fi
    fi
    ;;
esac

# Check if the file type is one we analyze
lang=$(detect_language "$FILE")
if [ "$lang" = "unknown" ]; then exit 0; fi

PROJECT_ROOT=$(detect_project_root "$FILE")

# Run analysis on the temp file
OUTPUT=$("$SKILL_DIR/bin/run-analysis.sh" --project-root "$PROJECT_ROOT" "$TMPFILE" 2>&1)
EXIT_CODE=$?

# Pre-existing violations in untouched code must not block an unrelated edit to
# the same file. Re-analyze the file as it stands and keep only what this edit
# ADDS. See bin/lib/filter-new-findings.py for the fingerprinting rationale.
if [ $EXIT_CODE -ne 0 ] && [ -f "$FILE" ]; then
  BASE_OUT=$("$SKILL_DIR/bin/run-analysis.sh" --project-root "$PROJECT_ROOT" "$FILE" 2>/dev/null || true)
  printf '%s' "$OUTPUT"   > "$TMPFILE.new.json"
  printf '%s' "$BASE_OUT" > "$TMPFILE.base.json"
  OUTPUT=$(FILE_NEW="$TMPFILE" FILE_OLD="$FILE" python3 \
    "$SKILL_DIR/bin/lib/filter-new-findings.py" "$TMPFILE.new.json" "$TMPFILE.base.json")
  rm -f "$TMPFILE.new.json" "$TMPFILE.base.json"
  echo "$OUTPUT" | grep -q '"severity": "error"' || EXIT_CODE=0
fi

if [ $EXIT_CODE -ne 0 ]; then
  # Build denial reason from analysis output
  REASON=$(echo "$OUTPUT" | python3 -c "
import json, sys
try:
    findings = json.load(sys.stdin)
    errors = []
    for f in findings:
        if f.get('severity') == 'error':
            tool = f.get('tool', 'unknown')
            msg = f.get('message', '').replace('$TMPFILE', '$FILE')
            errors.append(f'{tool}: {msg}')
    if errors:
        print('[ENF-POST-007] Pre-write validation errors in $FILE: ' + '; '.join(errors[:5]))
    else:
        print('[ENF-POST-007] Pre-write validation failed for $FILE')
except Exception:
    print('[ENF-POST-007] Pre-write validation failed for $FILE')
" 2>/dev/null)
  python3 -c "
import json, sys
print(json.dumps({
    'hookSpecificOutput': {
        'hookEventName': 'PreToolUse',
        'permissionDecision': 'deny',
        'permissionDecisionReason': sys.argv[1]
    }
}))
" "${REASON:-Pre-write validation failed}"
  log_gate_decision "pre-write-validation" "deny" "${REASON:-Pre-write validation failed}" "${FILE_PATH:-}"
  exit 0
fi

log_gate_decision "pre-write-validation" "allow" "pre-write validation passed" "${FILE_PATH:-}"
exit 0
