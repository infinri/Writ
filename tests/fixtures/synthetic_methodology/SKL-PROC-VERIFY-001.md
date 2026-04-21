---
skill_id: SKL-PROC-VERIFY-001
node_type: Skill
domain: process
severity: critical
scope: task
trigger: "Any time the agent is about to claim an implementation is complete, a test passes, a build succeeds, or any success condition holds."
statement: "No completion claim without fresh verification evidence. Run the command in this message. Read the output. Then claim — only then."
rationale: "Claims without evidence are the canonical failure mode that destroys trust with users. A single false 'done' requires triple the recovery effort of evidence-first claims."
tags: [completion-claim, evidence, fresh-run, process, verification]
confidence: battle-tested
authority: human
last_validated: 2026-04-21
staleness_window: 365
evidence: doc:superpowers
source_attribution: "superpowers@5.0.7"
source_commit: null
edges:
  - { target: ENF-PROC-VERIFY-001, type: GATES }
  - { target: FRB-COMMS-002, type: DEMONSTRATES }
---

# Skill: Verify before completion

## The gate function

1. Identify the verification command.
2. Run it (fresh, in this message).
3. Read the output.
4. Then claim — and state the claim with the evidence inline.

## Red flags

- "Should", "probably", "seems to"
- Claiming success before running the command
- Trusting subagent success reports without verifying
- Partial verification (linter but not build; unit but not integration)

## Forbidden phrases

See `FRB-COMMS-002` for the always-on phrase blocklist. Examples: "Should work now", "Looks good", "I'm confident this passes."
