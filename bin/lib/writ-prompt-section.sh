# Shared body of the single-section UserPromptSubmit hooks (always-on, methodology, recall)
# and the friction-row emitter all four injection hooks use. Sourced after common.sh, with
# WRIT_DIR set. See docs/adr/ADR-prompt-injection-split.md.

# Friction/telemetry rows for one /prompt-bundle response, buffered for the Stop drain.
# The response carries only its own section's *_meta, so each hook emits only its own rows.
#
# Friction logging stays CLIENT-SIDE so the per-project friction-log resolution
# (cwd-relative) is preserved: the daemon (fixed cwd) must not own it. jq builds the rows
# when present; bin/lib/writ_friction_rows.py is the python arm and runs when jq is absent
# (the WRIT_NO_JQ seam), so absence changes speed, never behaviour. THE SENTINEL IS jq's
# EXIT STATUS, NOT ITS OUTPUT: a response with no metadata legitimately produces zero rows.
# Rows go to the session event buffer (a bash append, no process); one too large to append
# atomically takes the friction-append.py spawn instead of being truncated.
writ_bundle_friction() {
    local BUNDLE="$1" SESSION_ID="$2" CURRENT_MODE="$3"
    local FRICTION_ROWS _FRICTION_ROWS_OK _frow FRICTION_OVERSIZED
    FRICTION_ROWS=""
    _FRICTION_ROWS_OK=""
    if [ -z "${WRIT_NO_JQ:-}" ] && command -v jq >/dev/null 2>&1 \
            && [ -r "$WRIT_DIR/bin/lib/friction-rows.jq" ]; then
        if FRICTION_ROWS=$(printf '%s' "$BUNDLE" | jq -R -s -r \
                --arg sid "$SESSION_ID" --arg mode "${CURRENT_MODE:-}" \
                -f "$WRIT_DIR/bin/lib/friction-rows.jq" 2>>"$WRIT_HOOK_LOG_SINK"); then
            _FRICTION_ROWS_OK=1
        else
            FRICTION_ROWS=""
        fi
    fi
    if [ -z "$_FRICTION_ROWS_OK" ]; then
        FRICTION_ROWS=$(printf '%s' "$BUNDLE" | WRIT_SID="$SESSION_ID" WRIT_MODE="${CURRENT_MODE:-}" \
            python3 "$WRIT_DIR/bin/lib/writ_friction_rows.py" 2>>"$WRIT_HOOK_LOG_SINK") || true
    fi
    if [ -n "$FRICTION_ROWS" ]; then
        FRICTION_OVERSIZED=""
        while IFS= read -r _frow; do
            [ -n "$_frow" ] || continue
            writ_friction_buffer_append "$SESSION_ID" "$_frow" \
                || FRICTION_OVERSIZED="${FRICTION_OVERSIZED}${_frow}"$'\n'
        done <<< "$FRICTION_ROWS"
        if [ -n "$FRICTION_OVERSIZED" ]; then
            printf '%s' "$FRICTION_OVERSIZED" \
                | python3 "$WRIT_DIR/bin/lib/friction-append.py" --stdin-jsonl 2>>"$WRIT_HOOK_LOG_SINK" || true
        fi
    fi
}

# The /prompt-bundle body for one section. Every string goes through --arg (or the
# environment on the python arm) so quotes and newlines in the prompt are encoded, never
# concatenated; always_on_filter stays a JSON boolean.
writ_section_request() {
    local sid="$1" section="$2" mode="$3" prompt="$4" root="$5" aof="$6" body=""
    if [ -z "${WRIT_NO_JQ:-}" ] && command -v jq >/dev/null 2>&1; then
        body=$(jq -n -c --arg session_id "$sid" --arg section "$section" --arg mode "$mode" \
            --arg prompt "$prompt" --arg project_root "$root" --argjson always_on_filter "$aof" \
            '{session_id: $session_id, sections: [$section], mode: $mode, prompt: $prompt, project_root: $project_root, always_on_filter: $always_on_filter}' \
            2>/dev/null) || body=""
    fi
    if [ -z "$body" ]; then
        body=$(WRIT_SID="$sid" WRIT_SECTION="$section" WRIT_MODE="$mode" WRIT_PROMPT="$prompt" \
            WRIT_PROOT="$root" WRIT_AOF="$aof" python3 -c "
import os, json
print(json.dumps({
    'session_id': os.environ['WRIT_SID'],
    'sections': [os.environ['WRIT_SECTION']],
    'mode': os.environ.get('WRIT_MODE', ''),
    'prompt': os.environ.get('WRIT_PROMPT', ''),
    'project_root': os.environ.get('WRIT_PROOT', ''),
    'always_on_filter': os.environ.get('WRIT_AOF', 'true') == 'true',
}))" 2>/dev/null)
    fi
    printf '%s' "$body"
}

# One section hook, start to finish. $1 = always_on | methodology | recall, $2 = hook name.
# Never autostarts the daemon (writ-rag-inject.sh owns that) and never writes the session
# cache: every write for a section happens server-side under the cache lock.
#
# SESSION_ID and CURRENT_MODE are deliberately global: the exit trap reads them for the
# hook_execution row.
writ_prompt_section_main() {
    local section="$1" hook="$2"
    local host="${WRIT_HOST:-localhost}" port="${WRIT_PORT:-8765}"
    local stdin_json parsed agent hint prompt root aof req resp field err text
    stdin_json=$(cat)
    printf '%s' "$stdin_json" | blackbox_log in "$hook"
    parsed=$(printf '%s' "$stdin_json" | python3 "$WRIT_DIR/bin/lib/writ-prompt-parse.py" 2>/dev/null) || true
    # Same positional record writ-rag-inject.sh reads: the prompt is the remainder.
    SESSION_ID=$(printf '%s\n' "$parsed" | head -1)
    agent=$(printf '%s\n' "$parsed" | sed -n '2p')
    hint=$(printf '%s\n' "$parsed" | sed -n '3p' | tr -d '[:space:]')
    prompt=$(printf '%s\n' "$parsed" | sed -n '4,$p')
    if [ -z "$SESSION_ID" ]; then
        writ_critical "$hook" "no session_id in hook payload; refusing to synthesize one"
        return 0
    fi
    case "$section" in
        recall) [ -z "$agent" ] || return 0 ;;
        *) [ "${#prompt}" -ge "$WRIT_MIN_QUERY_LENGTH" ] || return 0 ;;
    esac
    CURRENT_MODE=$(writ_turn_mode "$SESSION_ID" "$agent" "$hint")
    root=$(detect_project_root "$(pwd -P)")
    case "${WRIT_ALWAYS_ON_FILTER:-1}" in 1|on|true|yes) aof=true ;; *) aof=false ;; esac
    req=$(writ_section_request "$SESSION_ID" "$section" "${CURRENT_MODE:-}" "$prompt" "${root:-}" "$aof")
    [ -n "$req" ] || return 0
    resp=$(WRIT_HTTP_CONNECT_TIMEOUT=0.5 WRIT_HTTP_TIMEOUT=3 \
        writ_http_post "http://${host}:${port}/prompt-bundle" "$req" 2>/dev/null) || true
    [ -n "$resp" ] || return 0
    case "$section" in
        always_on) field=always_on_block ;;
        methodology) field=methodology_block ;;
        recall) field=recall_block ;;
    esac
    eval "$(parsed_fields "$resp" err=error text="$field")"
    err="${err-}"; text="${text-}"
    case "$err" in ""|false|False|null|None|0) err="" ;; esac
    [ -z "$err" ] || return 0
    writ_bundle_friction "$resp" "$SESSION_ID" "${CURRENT_MODE:-}"
    [ -n "$text" ] || return 0
    printf '\n%s\n' "$text" | writ_emit_capped "$WRIT_PROMPT_CHAR_CEILING"
}
