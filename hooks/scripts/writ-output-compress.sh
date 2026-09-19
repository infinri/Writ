#!/bin/bash
# WRIT-ROADMAP 18a: replace the redundant bulk of an Edit/Write tool response.
#
# Measured across 2,367 PostToolUse envelopes: Edit and Write are 95% of all
# tool-response bytes, and `originalFile` is 96% of an Edit response (the whole
# pre-edit file, echoed back) while structuredPatch carries the change in ~348 chars.
# Bash, the tool usually assumed to be the offender, has a 742-char median.
#
# FAILS OPEN EVERYWHERE. A compression hook that can break an edit is strictly worse
# than no compression, so every error path emits nothing and exits 0, which leaves the
# envelope exactly as the tool produced it.
#
# STDIN IS READ ONCE INTO $RAW and fed to both consumers. load_hook_env would consume
# it, and this hook needs the nested `tool_response` object the shared parser does not
# expose. Reading it twice is not an option and skipping the parser would leave the
# reply without a session identity, which the hook contract requires.
#
# The reply goes through emit_hook_reply, not a bare print: that is the shared funnel
# every envelope-emitting hook uses, so the reply is captured like any other.
#
# Hook type: PostToolUse (matcher: Write|Edit)
set -uo pipefail

SKILL_DIR="$(cd "$(dirname "$0")/../.." && pwd)"
source "$SKILL_DIR/bin/lib/common.sh"

# IDENTITY BEFORE TELEMETRY. hook_instrument keys its row on
# ${SESSION_ID:-${HOOK_SESSION_ID:-}}, so instrumenting before the envelope is parsed
# would file every row under an empty session. Read stdin, parse it, assign the id,
# and only then instrument.
RAW=$(cat 2>/dev/null || true)
[ -n "$RAW" ] || exit 0
eval "$(printf '%s' "$RAW" | _writ_parse_hook_stdin)" 2>/dev/null || true
SESSION_ID="${HOOK_SESSION_ID:-}"

hook_instrument "writ-output-compress"

REPLY=$(printf '%s' "$RAW" | python3 -c "
import json, sys
sys.path.insert(0, '$SKILL_DIR')
try:
    env = json.load(sys.stdin)
    from writ.session.output_compression import compress_tool_response
    out = compress_tool_response(env.get('tool_name', ''), env.get('tool_response'))
    if out is not None:
        print(json.dumps({'hookSpecificOutput': {
            'hookEventName': 'PostToolUse',
            'updatedToolOutput': out,
        }}))
except Exception:
    pass
" 2>/dev/null || true)

[ -n "$REPLY" ] && emit_hook_reply "$REPLY" "writ-output-compress" "${HOOK_SESSION_ID:-}"
exit 0
