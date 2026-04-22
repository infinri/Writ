---
name: writ-approve
description: Advance the current Writ workflow phase. Replaces pattern-match "approved" detection with an explicit tool-confirmed advance (plan Section 8.1).
---

You have been invoked to advance the Writ workflow phase. Confirm the user's intent is to advance, then run this command via Bash.

## Procedure

1. Check the current phase via `GET /session/$SESSION_ID/current-phase`.
2. If the current phase artifact exists and was presented to the user in this or a prior turn (plan.md for planning, test skeletons for testing, etc.), proceed. Otherwise, respond: "No current phase artifact to approve. Present the artifact first."
3. Advance via POST with explicit tool source:

```bash
curl -sX POST http://localhost:8765/session/$SESSION_ID/advance-phase \
  -H 'Content-Type: application/json' \
  -d '{"confirmation_source": "tool"}'
```

4. Confirm to the user: "[Writ: $ARG advanced → $NEW_PHASE]" where $ARG is what they approved (design / plan / tests) and $NEW_PHASE is the new phase name from the response.

## Audit trail

Each advance is recorded to `session.phase_transitions` with `confirmation_source: "tool"` AND appended to `workflow-friction.log` as a `phase_advance` event. Phase 5 telemetry distinguishes tool-confirmed from pattern-confirmed advances for rubric refinement.

## Never

- Never advance without this command (or its MCP equivalent `writ_approve`). Pattern match on "approved" in user prompts is defence in depth, not the primary path.
- Never advance multiple phases in a single invocation. One call = one advance.
- Never fabricate approval. If the user has not explicitly authorized, ask them before calling.
