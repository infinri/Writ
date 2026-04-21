---
skill_id: SKL-PROC-REVRECV-001
node_type: Skill
domain: communication
severity: high
scope: task
trigger: "When the agent receives code review feedback, a user correction, or external reviewer output and must respond."
statement: "Evaluate feedback technically before implementing. Verify against the codebase. Ask clarification on unclear items. Never respond with performative agreement."
rationale: "Performative agreement collapses the boundary between 'I heard you' and 'I agree.' Without technical verification, the agent implements things that may be wrong, making the review a liability rather than a check."
tags: [communication, external-review, process, technical-rigor, verification]
confidence: peer-reviewed
authority: human
last_validated: 2026-04-21
staleness_window: 365
evidence: doc:superpowers
source_attribution: "superpowers@5.0.7"
source_commit: null
edges:
  - { target: FRB-COMMS-001, type: DEMONSTRATES }
  - { target: ENF-COMMS-001, type: GATES }
---

# Skill: Receive code review

## Never-phrases

See `FRB-COMMS-001`. Gratitude, agreement, or implementation-intent BEFORE verification are all forbidden.

## The workflow

1. Read each item.
2. For each: verify against the codebase. Is the reviewer correct? Is there something they missed?
3. If unclear: ask for clarification. Don't implement until understood.
4. If disagree: push back with reasoning, not defensiveness.
5. Only then implement. Test each change individually.

## Anti-pattern

Batch implementation of all review items without testing each. If any is wrong, all become suspect.
