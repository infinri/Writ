---
name: writ-approve
description: Advance the current Writ workflow phase with an explicit tool-confirmed advance. NOT a replacement for pattern-match approval detection; this command spends the token that detection mints (see Authorization below).
---

You have been invoked to advance the Writ workflow phase. Confirm the user's intent is to advance, then run this command via Bash.

## Authorization: read this before assuming this command is the primary path

This command does NOT detect approval, and it cannot substitute for detection. Step 3
below reads the gate token, and the ONLY thing that writes that token is
`auto-approve-gate.sh` when the user's own prompt classified as an `exact` approval. So on
a detection miss there is no token, this command has nothing to send, and the advance
fails closed. The description used to claim this command replaced pattern matching, which
was never true of its own procedure.

The detector has three tiers (`bin/lib/approval_match.py`):

- **exact**: the prompt is an approval. The hook mints a bound token and advances the
  pending gate itself, so this command is usually unnecessary on that turn.
- **embedded**: a strong approval word inside a longer sentence, for example "ok remember
  we want to fix all our findings, approved". Nothing is minted and nothing is advanced.
  The hook emits a directive naming the pending gate and asks you to confirm the user's
  intent. ASK; do not call this command to work around it. If the user confirms with
  "approved", that turn is an exact approval and the hook advances the gate.
- **none**: not approval-related. Nothing happens.

## Procedure

1. Check the current phase via `GET /session/$SESSION_ID/current-phase`. The response also
   reports `next_gate` (the gate a token would be bound to) and `plan_hash`.
2. If the current phase artifact exists and was presented to the user in this or a prior turn (plan.md for planning, test skeletons for testing, etc.), proceed. Otherwise, respond: "No current phase artifact to approve. Present the artifact first."
3. Advance via POST, passing the gate token and the working directory. The token at
   `/tmp/writ-gate-token-$SESSION_ID` is written by the approval hook ONLY when the
   user's prompt matched an exact approval pattern, so it proves genuine user approval; the
   advance route requires it and consumes it (one approval = one advance).

   The token file has THREE lines: the secret, the gate that approval authorizes, and the
   plan fingerprint it was given for. Send LINE ONE ONLY. The advance route checks lines
   two and three itself: a token bound to a different gate, or to a plan.md that has
   changed since the approval, is refused with the reason named.

   `cwd` MUST be sent: the server resolves the project root from it (that is where
   plan.md and the test skeletons are looked for), and it cannot substitute its own
   working directory, which is Writ's install dir. Omitting it makes a planning advance
   fail closed on an empty root.

```bash
TOKEN=$(head -1 "/tmp/writ-gate-token-$SESSION_ID" 2>/dev/null)
curl -sX POST http://localhost:8765/session/$SESSION_ID/advance-phase \
  -H 'Content-Type: application/json' \
  -d "{\"confirmation_source\": \"tool\", \"token\": \"$TOKEN\", \"cwd\": \"$(pwd -P)\"}"
```

   If the response is `{"advanced": false, ...}` with a token error, the user has not
   actually approved this turn (no token was written). Do NOT retry or fabricate a token:
   tell the user the approval was not detected and ask them to confirm explicitly.

4. Confirm to the user: "[Writ: $ARG advanced -> $NEW_PHASE]" where $ARG is what they approved (design / plan / tests) and $NEW_PHASE is the new phase name from the response. Also report the response's `validated` and `project_root` fields verbatim, so the user can see WHICH plan.md was accepted and catch a wrong project root.

## Audit trail

Each advance is recorded to `session.phase_transitions` with `confirmation_source: "tool"` AND appended to `workflow-friction.log` as a `phase_advance` event. Phase 5 telemetry distinguishes tool-confirmed from pattern-confirmed advances for rubric refinement.

## Never

- Never advance without this command (or its MCP equivalent `writ_approve`). Pattern match on "approved" in user prompts is the channel that mints the authorization, and it is what advances the gate on an exact match.
- Never advance multiple phases in a single invocation. One call = one advance.
- Never fabricate approval. If the user has not explicitly authorized, ask them before calling.
- Never send more than line one of the token file, and never write that file yourself. The
  token is the human's authorization; an agent-written one is agent self-approval.
