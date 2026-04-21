---
skill_id: SKL-PROC-PLAN-001
node_type: Skill
domain: process
severity: high
scope: task
trigger: "When a user's approved design must be converted into an implementation plan with concrete steps, file paths, and complete code per step."
statement: "Write bite-sized implementation plans with exact file paths, complete code blocks, exact commands with expected output. Never use placeholders (TBD, TODO, 'similar to task N', 'fill in later')."
rationale: "Placeholder-laden plans transfer the design decision to the implementer's interpretation. Complete plans are reproducible and auditable; placeholder plans are aspirational fiction."
tags: [exact-paths, placeholders-forbidden, planning, process, tdd-integrated]
confidence: peer-reviewed
authority: human
last_validated: 2026-04-21
staleness_window: 365
evidence: doc:superpowers
source_attribution: "superpowers@5.0.7"
source_commit: null
edges:
  - { target: PBK-PROC-PLAN-001, type: TEACHES }
  - { target: ENF-PROC-PLAN-001, type: GATES }
  - { target: PBK-PROC-TDD-001, type: PRECEDES }
  - { target: PBK-PROC-SDD-001, type: PRECEDES }
---

# Skill: Write a no-placeholder plan

## Forbidden content in plans

- TBD, TODO, "implement later"
- "Add appropriate error handling"
- "Write tests for the above"
- "Similar to Task N"
- Vague step descriptions without code

## Required content

Exact file paths. Complete code blocks per step. Exact commands with expected output. Self-review against spec after writing.
