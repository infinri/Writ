#!/usr/bin/env bash
# Phase 2: session-end pressure audit.
#
# SessionEnd hook. Emits a workflow-friction.log event summarizing session
# pressure metrics: quality_override_count, verification_evidence count,
# active_playbook phase history length, review_ordering_state violations.
# Feature-flag gated. Always exits 0 (audit is observational, not blocking).
set -euo pipefail
HOOK_DIR="$(cd "$(dirname "$0")" && pwd)"
WRIT_DIR="$(cd "$HOOK_DIR/../.." && pwd)"
SESSION_HELPER="$WRIT_DIR/bin/lib/writ-session.py"
source "$WRIT_DIR/bin/lib/common.sh"

is_superpowers_absorb_enabled || exit 0

PARSED=$(parse_hook_stdin)
SESSION_ID=$(detect_session_id "$PARSED")
[ -z "$SESSION_ID" ] && exit 0

python3 <<PY 2>/dev/null || true
import json, os, sys
from datetime import datetime, timezone
sys.path.insert(0, "$WRIT_DIR/bin/lib")
from importlib import util
spec = util.spec_from_file_location('writ_session', "$SESSION_HELPER")
mod = util.module_from_spec(spec); spec.loader.exec_module(mod)
session = mod._read_cache("$SESSION_ID")

metrics = {
    "ts": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    "session": "$SESSION_ID",
    "event": "pressure_audit",
    "mode": session.get("mode"),
    "active_playbook": session.get("active_playbook"),
    "phases_traversed": len(session.get("playbook_phase_history") or []),
    "verification_evidence_count": len(session.get("verification_evidence") or {}),
    "quality_judgment_count": len(session.get("quality_judgment_state") or {}),
    "quality_override_count": session.get("quality_override_count", 0),
}
# Flag if quality override threshold exceeded (plan Section 0.4 decision 4: threshold=3).
if metrics["quality_override_count"] > 3:
    metrics["escalation"] = "quality_override_threshold_exceeded"

# Find project root and append to workflow-friction.log
markers = ['composer.json','package.json','Cargo.toml','go.mod','pyproject.toml','.git']
path = os.getcwd()
while path != '/':
    if any(os.path.exists(os.path.join(path, m)) for m in markers):
        try:
            with open(os.path.join(path, 'workflow-friction.log'), 'a') as f:
                f.write(json.dumps(metrics) + '\n')
        except OSError:
            pass
        break
    path = os.path.dirname(path)
PY
exit 0
