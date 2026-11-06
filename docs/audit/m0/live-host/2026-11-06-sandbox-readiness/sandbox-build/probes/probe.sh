#!/usr/bin/env bash
# SR-06 probe hook for arms B and C1. Never sources V1 code.
# Usage (from a hook registration): probe.sh <experiment-id> <behavior> [arg]
# Appends one marker line to $SR06_RUN/markers.log, then performs <behavior>.
exp="${1:-unknown}" behavior="${2:-allow}" arg="${3:-}"
run="${SR06_RUN:-/home/tester/run/current}"
input="$(cat)"
tool="$(printf '%s' "$input" | jq -r '.tool_name // empty' 2>/dev/null)"
agent="$(printf '%s' "$input" | jq -r '.agent_id // empty' 2>/dev/null)"
session="$(printf '%s' "$input" | jq -r '.session_id // empty' 2>/dev/null)"
event="$(printf '%s' "$input" | jq -r '.hook_event_name // empty' 2>/dev/null)"
printf '%s\texp=%s\tbehavior=%s\tevent=%s\ttool=%s\tagent_id=%s\tsession_id=%s\n' \
  "$(date -u +%Y-%m-%dT%H:%M:%S.%3NZ)" "$exp" "$behavior" "$event" "$tool" "$agent" "$session" >> "$run/markers.log"

case "$behavior" in
  allow)          exit 0 ;;
  exit2)          echo "SR06 probe $exp: denied by exit 2" >&2; exit 2 ;;
  exit1)          echo "SR06 probe $exp: exit 1" >&2; exit 1 ;;
  exit127)        sr06_undefined_command_for_exit_127 ;;
  sleep)          sleep "${arg:-120}"; exit 0 ;;
  invalid-json)   printf '{not json}\n'; exit 0 ;;  # starts with { and ends with }: a parse failure per the hooks docs
  plain-text)     printf 'plain text output\n'; exit 0 ;;  # documented as not a failure
  deny-envelope)  printf '{"hookSpecificOutput":{"hookEventName":"PreToolUse","permissionDecision":"deny","permissionDecisionReason":"SR06 probe %s deny"}}\n' "$exp"; exit 0 ;;
  allow-envelope) printf '{"hookSpecificOutput":{"hookEventName":"PreToolUse","permissionDecision":"allow","permissionDecisionReason":"SR06 probe %s allow"}}\n' "$exp"; exit 0 ;;
  bad-decision)   printf '{"hookSpecificOutput":{"hookEventName":"PreToolUse","permissionDecision":"maybe"}}\n'; exit 0 ;;
  bad-structure)  printf '{"hookSpecificOutput":"deny"}\n'; exit 0 ;;
  *)              echo "SR06 probe: unknown behavior $behavior" >&2; exit 3 ;;
esac
