---
antipattern_id: ANT-PROC-PLAN-001
node_type: AntiPattern
domain: process
severity: high
scope: task
trigger: "When a plan contains placeholder content (TBD, TODO, 'similar to task N', 'implement appropriate error handling', 'write tests for above')."
statement: "Placeholder content in plans: each placeholder is a deferred design decision transferred to the implementer as interpretation. Non-reproducible plans."
rationale: "The plan is a contract between design and implementation. Placeholders break the contract — they force the implementer to guess. Guesses drift from user intent."
tags: [anti-pattern, placeholder, planning, process, tbd]
confidence: peer-reviewed
authority: human
last_validated: 2026-04-21
staleness_window: 365
evidence: doc:superpowers
source_attribution: "superpowers@5.0.7"
source_commit: null
counter_nodes: [SKL-PROC-PLAN-001, ENF-PROC-PLAN-001]
named_in: "superpowers@5.0.7:writing-plans"
edges:
  - { target: SKL-PROC-PLAN-001, type: COUNTERS }
  - { target: ENF-PROC-PLAN-001, type: COUNTERS }
---

# Anti-pattern: Placeholder content

## Counter

Every step has: exact file path, complete code block, exact command. See `SKL-PROC-PLAN-001` and `TEC-PROC-SPEC-REVIEW-001` for the authoring + self-review discipline.
