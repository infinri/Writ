---
playbook_id: PBK-PROC-PLAN-001
node_type: Playbook
domain: process
severity: high
scope: task
trigger: "When an approved design is ready to be decomposed into a concrete implementation plan."
statement: "Map the file structure, decompose into bite-sized tasks with complete code per step, integrate TDD per task, self-review the plan before presenting."
rationale: "A single-pass plan that maps structure before decomposing produces coherent, task-level-reproducible artifacts. Iterating in the middle of writing the plan produces inconsistency."
tags: [bite-sized, planning, playbook, process, self-review, tdd]
confidence: peer-reviewed
authority: human
last_validated: 2026-04-21
staleness_window: 365
evidence: doc:superpowers
source_attribution: "superpowers@5.0.7"
source_commit: null
phase_ids: []
preconditions: [SKL-PROC-BRAIN-001]
dispatched_roles: []
edges:
  - { target: SKL-PROC-PLAN-001, type: TEACHES }
  - { target: ENF-PROC-PLAN-001, type: GATES }
  - { target: PBK-PROC-TDD-001, type: PRECEDES }
---

# Playbook: Write a plan

## Order of operations

1. Map file structure — which files will be touched, created, deleted.
2. Decompose — break work into bite-sized tasks.
3. Per task: exact paths, complete code, test first (TDD integrated).
4. Self-review — placeholder scan, spec coverage, type consistency, contradictions.
5. Present.

## No placeholders

See `SKL-PROC-PLAN-001` for the blocklist.
