---
playbook_id: PBK-PROC-BRAIN-001
node_type: Playbook
domain: process
severity: high
scope: session
trigger: "When the brainstorming skill is invoked and a design must be produced from the user's request."
statement: "Nine-phase process: understand intent, clarify constraints, propose 2-3 approaches, name trade-offs, offer visual companion when applicable, ask clarifying questions, synthesize design, present design, wait for approval."
rationale: "Without an ordered sequence, the agent skips steps it considers obvious (typically the approval wait). A named playbook forces the sequence to be completed or explicitly short-circuited."
tags: [9-step, brainstorming, design, playbook, process]
confidence: peer-reviewed
authority: human
last_validated: 2026-04-21
staleness_window: 365
evidence: doc:superpowers
source_attribution: "superpowers@5.0.7"
source_commit: null
phase_ids:
  - PHA-BRAIN-001
  - PHA-BRAIN-002
  - PHA-BRAIN-003
  - PHA-BRAIN-004
  - PHA-BRAIN-005
  - PHA-BRAIN-006
  - PHA-BRAIN-007
  - PHA-BRAIN-008
  - PHA-BRAIN-009
preconditions: []
dispatched_roles: []
edges:
  - { target: SKL-PROC-BRAIN-001, type: TEACHES }
  - { target: SKL-PROC-VISUAL-001, type: DISPATCHES }
  - { target: SKL-PROC-PLAN-001, type: PRECEDES }
  - { target: ENF-PROC-BRAIN-001, type: GATES }
  - { target: PHA-BRAIN-001, type: CONTAINS }
  - { target: PHA-BRAIN-002, type: CONTAINS }
  - { target: PHA-BRAIN-003, type: CONTAINS }
  - { target: PHA-BRAIN-004, type: CONTAINS }
  - { target: PHA-BRAIN-005, type: CONTAINS }
  - { target: PHA-BRAIN-006, type: CONTAINS }
  - { target: PHA-BRAIN-007, type: CONTAINS }
  - { target: PHA-BRAIN-008, type: CONTAINS }
  - { target: PHA-BRAIN-009, type: CONTAINS }
---

# Playbook: Brainstorm-before-code process

Mandatory sequence. Every project goes through each phase, regardless of perceived simplicity.

## Phase references

Each `PHA-BRAIN-00N` node carries the full phase description as a structural bundle member. Retrieved via `CONTAINS` edge during Stage 4 graph traversal.

1. `PHA-BRAIN-001` — Understand intent
2. `PHA-BRAIN-002` — Clarify constraints
3. `PHA-BRAIN-003` — Propose 2-3 approaches
4. `PHA-BRAIN-004` — Name trade-offs
5. `PHA-BRAIN-005` — Offer visual companion (if applicable)
6. `PHA-BRAIN-006` — Ask clarifying questions
7. `PHA-BRAIN-007` — Synthesize design
8. `PHA-BRAIN-008` — Present design
9. `PHA-BRAIN-009` — Wait for approval

## Skip conditions

None for Phases 1, 2, 8, 9. Phases 3-7 may compress for genuinely investigation-only tasks that will not result in code changes, but the skill must still announce itself.

## Failure mode reference

See `SKL-PROC-BRAIN-001` for the teaching. See `ENF-PROC-BRAIN-001` for mechanical enforcement. See `RAT-BRAIN-*` family for rationalization counters attached to each phase's failure modes.
