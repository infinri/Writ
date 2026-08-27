#!/bin/bash
# WRIT-READ-JUNK-GATE: token-saving read gate (WRIT-TOKEN-BLUEPRINT.md lever A).
#
# PreToolUse (matcher: Read), ALL Writ modes. Classifies the target file as junk
# (gitignored / vendored-generated dir / minified / map / lockfile / backup / binary)
# or oversized, emits a read_blocked friction event with a CONSERVATIVE prevented-token
# floor (bytes/4; 0 for binaries), and -- ONLY in enforce mode -- denies with a redirect
# recipe. Default mode is OBSERVE: log would_block, never deny. Fail-open: any error or a
# non-junk read -> exit 0 silent. Touches NO credential logic, NO gates.py, NO daemon.
# Exit: always 0.
set -uo pipefail   # NOT -e: a classify failure must fall through to ALLOW

SKILL_DIR="$(cd "$(dirname "$0")/../.." && pwd)"
# shellcheck source=/dev/null
. "$SKILL_DIR/bin/lib/common.sh" 2>/dev/null || exit 0

load_hook_env
FP="${HOOK_FILE_PATH:-}"
SID="${HOOK_SESSION_ID:-}"
[ -n "$FP" ] || exit 0                       # no file path -> nothing to gate (fail-open)

MODE_NOW=$(_writ_session "mode get" "$SID" 2>/dev/null | tr -d '[:space:]')
# MODE is what common.sh's exit trap reads for the hook_execution row (_writ_row_mode_cached
# takes CURRENT_MODE then MODE). This hook's own variable is MODE_NOW, so without this the
# row it now gets for free would carry an empty mode. HOOK_SESSION_ID already covers the
# session field, which is why SID needs no equivalent line.
MODE="$MODE_NOW"
GATE_MODE="${WRIT_READ_JUNK_GATE:-observe}"
SIZE_LIMIT_KB="${WRIT_READ_SIZE_KB:-100}"

# --- classify (cheapest checks first; bash glob -> git check-ignore -> size) ---
reason=""
case "$FP" in
  */node_modules/*|*/vendor/*|*/dist/*|*/build/*|*/.next/*|*/coverage/*|*/out/*|\
  */__pycache__/*|*/.cache/*|*/.pytest_cache/*|*/.mypy_cache/*|*/.git/*|*/.idea/*|*/.vscode/*) \
      reason="path_blocklist" ;;
  *.min.js|*.min.css|*.map|*.pyc) reason="path_blocklist" ;;
  *package-lock.json|*yarn.lock|*poetry.lock|*Cargo.lock|*Gemfile.lock|*composer.lock) \
      reason="path_blocklist" ;;
  *.bak|*.backup|*.orig|*~) reason="path_blocklist" ;;
  *.png|*.jpg|*.jpeg|*.gif|*.webp|*.ico|*.pdf|*.zip|*.gz|*.tar|*.mp4|*.mov|*.woff|*.woff2|\
  *.ttf|*.eot|*.so|*.dylib|*.o|*.a|*.class|*.jar|*.wasm|*.bin) reason="binary" ;;
esac

if [ -z "$reason" ] && git -C "$(dirname "$FP")" rev-parse --is-inside-work-tree >/dev/null 2>&1; then
  git -C "$(dirname "$FP")" check-ignore -q "$FP" 2>/dev/null && reason="gitignore"
fi

FILE_BYTES=$(stat -c%s "$FP" 2>/dev/null || { wc -c < "$FP" 2>/dev/null; } 2>/dev/null || echo 0)
if [ -z "$reason" ] && [ "${FILE_BYTES:-0}" -gt $(( SIZE_LIMIT_KB * 1024 )) ] 2>/dev/null; then
  reason="size_gate"
fi

# not junk -> allow silently (this is the common case; keep it cheap)
[ -n "$reason" ] || exit 0

# --- prevented-cost floor: bytes/4; binaries credit 0 (cannot claim binary bytes are tokens) ---
if [ "$reason" = "binary" ]; then FLOOR=0; else FLOOR=$(( FILE_BYTES / 4 )); fi
REDIRECT_OFFERED=false
[ "$reason" = "size_gate" ] && REDIRECT_OFFERED=true

# --- emit read_blocked friction (both modes); python builds JSON from env (path-quote safe) ---
SID="$SID" MODE_NOW="$MODE_NOW" FP="$FP" FILE_BYTES="$FILE_BYTES" FLOOR="$FLOOR" \
REASON="$reason" REDIRECT="$REDIRECT_OFFERED" GATE_MODE="$GATE_MODE" python3 -c "
import json, os
from datetime import datetime, timezone
print(json.dumps({
  'ts': datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ'),
  'session': os.environ.get('SID',''),
  'mode': os.environ.get('MODE_NOW') or None,
  'event': 'read_blocked',
  'file_path': os.environ.get('FP',''),
  'file_bytes': int(os.environ.get('FILE_BYTES',0) or 0),
  'prevented_tokens_floor': int(os.environ.get('FLOOR',0) or 0),
  'gross_bytes_upper_bound': int(os.environ.get('FILE_BYTES',0) or 0),
  'block_reason': os.environ.get('REASON',''),
  'estimate_basis': 'bytes/4_floor',
  'tool_name': 'Read',
  'redirect_offered': os.environ.get('REDIRECT')=='true',
  'would_block': True,
  'enforced': os.environ.get('GATE_MODE')=='enforce',
}))
" 2>/dev/null | python3 "$SKILL_DIR/bin/lib/friction-append.py" --stdin-json 2>/dev/null || true

# --- observe mode (default): never deny ---
if [ "$GATE_MODE" != "enforce" ]; then
  exit 0
fi

# --- enforce mode: deny with a redirect recipe (the agent learns the cheap path) ---
KB=$(( FILE_BYTES / 1024 ))
if [ "$reason" = "size_gate" ]; then
  MSG="[WRIT-READ-SIZE] $FP is ${KB} KB (~${FLOOR} tokens), over the ${SIZE_LIMIT_KB} KB whole-file read threshold. Read it cheaply: grep the symbol (rg -n '<pattern>' '$FP'), or Read with offset/limit (offset=0 limit=200, then page). Re-issue with offset+limit to proceed."
else
  MSG="[WRIT-READ-JUNK] Skipped $FP: generated/vendored/ignored content ($reason). It dilutes the audit for ~${FLOOR} tokens of little signal. If you need a fact, grep the symbol (rg -n '<symbol>' '$FP') or read the source it was generated from. To override, re-issue the Read after stating why."
fi
emit_deny "$MSG"
exit 0
