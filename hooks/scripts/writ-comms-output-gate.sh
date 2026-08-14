#!/usr/bin/env bash
# ENF-COMMS-OUTPUT-001 punctuation floor: block em-dash slop in the agent's final
# response. The user forbids em dashes, en-dash-as-punctuation, and " -- " used as an
# em-dash substitute. Advisory rule-text alone failed, so this is a deterministic
# lexical backstop (zero false positives: the char is present or it is not).
#
# Stop hook, ALL modes. Loop-safe: surfaces via stderr + exit 1 (the verify-before-
# claim pattern), guarded by stop_hook_active so a continuation Stop is a no-op. Reads
# the last assistant message from transcript_path; strips code spans first so an em
# dash inside quoted code or a `git checkout --` example is not flagged.
set -euo pipefail
HOOK_DIR="$(cd "$(dirname "$0")" && pwd)"
WRIT_DIR="$(cd "$HOOK_DIR/../.." && pwd)"
source "$WRIT_DIR/bin/lib/common.sh"
hook_instrument "writ-comms-output-gate"

STDIN_JSON=$(cat 2>/dev/null || echo '{}')

# THIS HOOK'S OWN TELEMETRY IS KEYED HERE, and it must be keyed BEFORE the
# stop_hook_active exit below: hook_instrument's exit trap reads SESSION_ID when the
# script EXITS, so keying it after that line would leave every continuation-Stop row
# unattributed. Both that trap and log_gate_decision file under
# `${SESSION_ID:-${HOOK_SESSION_ID:-}}` and this hook set neither, so its rows landed
# under the literal session id "unknown" -- measured 2026-08-11, writ-events-unknown.buf
# held 372 rows from this gate alone (197 hook_execution + 175 gate_decision). We cannot
# use load_hook_env: it reads stdin, which $STDIN_JSON has already consumed.
#
# The id is the payload's and only the payload's (agent_id first, so a sub-agent's rows
# are not filed under its parent). It is never synthesized: an id the payload did not
# carry stays EMPTY, leaving a visible gap rather than a silently wrong record.
#
# This is the canonical empty-aware pair, the same spelling writ_require_session uses for
# this exact identity read (common.sh:1022). NOT `.agent_id // .session_id`: jq's `//`
# falls through only on null and false, so a payload carrying `"agent_id": ""` would KEEP
# the empty string and never reach session_id, while the python arm's `or` falls through
# and proceeds. That is the trap documented at common.sh:1010, where it made three
# governance hooks disagree across the WRIT_NO_JQ seam -- the presence of jq is supposed
# to change speed, never behavior. The select() filter and the `or` expression agree on
# every falsy shape, and tests/ pins the pair.
#
# One process on BOTH arms, and it takes the payload on a pipe from $STDIN_JSON rather
# than from this hook's stdin, which was already consumed. Measured 2026-08-11, 12 runs
# each on the stop_hook_active early-exit path (median total hook duration): 3ms before
# this fix (unattributed), 8ms with jq, 19.5ms under WRIT_NO_JQ=1. Both shapes this
# replaced were worse on an arm: an inline `python3 -c` cost 19ms even WITH jq, and a
# two-call parsed_field fallthrough cost 35ms without it, because each call is its own
# python start. A jq-less host is a tested configuration here (the CI bare-python3
# class), not a hypothetical, so the no-jq arm is not a throwaway number.
SESSION_ID="$(printf '%s' "$STDIN_JSON" | json_transform \
    '[.agent_id, .session_id] | map(select(. != null and . != "" and . != false)) | first // empty' \
    "(d.get('agent_id') or d.get('session_id'))" 2>/dev/null || true)"
# Edge-trim in the shell, matching .strip(): a padded id would otherwise become a buffer
# filename with spaces in it. Parameter expansion, so this adds no process.
SESSION_ID="${SESSION_ID#"${SESSION_ID%%[![:space:]]*}"}"
SESSION_ID="${SESSION_ID%"${SESSION_ID##*[![:space:]]}"}"

stop_hook_active "$STDIN_JSON" && exit 0          # block at most once; never loop

TP=$(printf '%s' "$STDIN_JSON" \
    | python3 -c "import sys,json; print(json.load(sys.stdin).get('transcript_path',''))" 2>/dev/null || echo "")
[ -n "$TP" ] && [ -f "$TP" ] || exit 0

# NOTE: the transcript path is passed via env (WRIT_TP) and read INSIDE python.
# We cannot pipe `tail` into `python3 - <<'PY'`: a stdin heredoc makes the script
# its own stdin, so the piped transcript would never be seen. The forbidden chars
# are written as \u escapes (NOT literal em/en dash) to avoid any encoding mangling.
VIOLATION=$(WRIT_TP="$TP" python3 - <<'PY' 2>/dev/null
import os, json, re
EM = chr(0x2014)  # em dash, ASCII-safe source (no literal char in this file)
EN = chr(0x2013)  # en dash, ASCII-safe source
tp = os.environ.get("WRIT_TP", "")
try:
    with open(tp, encoding="utf-8", errors="replace") as fh:
        lines = fh.readlines()
except Exception:
    raise SystemExit(0)
lines = lines[-400:]  # bound the scan: only the tail of the transcript
last_text = ""
for line in lines:
    if '"assistant"' not in line:
        continue
    try:
        d = json.loads(line)
    except Exception:
        continue
    if d.get("type") != "assistant":
        continue
    content = (d.get("message") or {}).get("content")
    if isinstance(content, list):
        last_text = "".join(b.get("text", "") for b in content
                            if isinstance(b, dict) and b.get("type") == "text")
    elif isinstance(content, str):
        last_text = content
if not last_text:
    raise SystemExit(0)
# strip fenced + inline code so code / CLI examples are never scanned
prose = re.sub(r"```.*?```", "", last_text, flags=re.DOTALL)
prose = re.sub(r"`[^`]*`", "", prose)
hits = []
if EM in prose: hits.append("em dash (" + EM + ")")
if EN in prose: hits.append("en dash (" + EN + ")")
if re.search(r" -- ", prose): hits.append('" -- " (double hyphen as em-dash)')
if hits:
    print("; ".join(hits))
PY
) || true

if [ -n "$VIOLATION" ]; then
    log_gate_decision "comms-output" "deny" "$VIOLATION" "assistant-response"
    echo "[ENF-COMMS-OUTPUT-001] Your last response used forbidden punctuation: $VIOLATION. \
The user forbids em dashes and em-dash-substitute double hyphens. Re-send the SAME content using \
commas, colons, semicolons, or parentheses for clause breaks, and hyphens only to join words." >&2
    exit 1
fi
log_gate_decision "comms-output" "allow" "no forbidden punctuation" "assistant-response"
exit 0
