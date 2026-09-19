#!/bin/bash
# 1.8b push-by-action: PostToolUse Write|Edit on bible/** -> re-surface the node-
# authoring methodology (PBK-AUTHOR-001 + META-AUTH-*) AT the moment a bible node
# is written or edited. Emits hookSpecificOutput.additionalContext (PostToolUse).
# Fail-open; informational only; always exit 0.
#
# Hook type: PostToolUse (matcher: Write|Edit)
SKILL_DIR="$(cd "$(dirname "$0")/../.." && pwd)"
source "$SKILL_DIR/bin/lib/common.sh"
hook_instrument "writ-bible-authoring-push"

load_hook_env
[ -z "${HOOK_FILE_PATH:-}" ] && exit 0
case "$HOOK_FILE_PATH" in
    */bible/*) ;;
    *) exit 0 ;;
esac

PUSH=$(writ_action_push "$HOOK_SESSION_ID" "bible-authoring" || true)
[ -z "$PUSH" ] && exit 0

PUSH_REPLY=$(WRIT_BIBLE_PUSH="$PUSH" python3 <<'PY' 2>/dev/null
import json, os
print(json.dumps({
    "hookSpecificOutput": {
        "hookEventName": "PostToolUse",
        "additionalContext": "[Writ: methodology -- bible-authoring]\n" + os.environ.get("WRIT_BIBLE_PUSH", ""),
    }
}))
PY
) || PUSH_REPLY=""
emit_hook_reply "$PUSH_REPLY"
exit 0
