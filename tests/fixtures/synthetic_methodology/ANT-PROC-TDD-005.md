---
antipattern_id: ANT-PROC-TDD-005
node_type: AntiPattern
domain: process
severity: high
scope: task
trigger: "When the agent writes production code first and adds tests afterward (test-after), or rationalizes that 'tests after achieve the same purpose.'"
statement: "Test-after: tests written after code become confirmations of what was built, not specifications of what should be built. They pass trivially and miss design-level errors."
rationale: "Test-first forces clear interface design before implementation. Test-after forces tests to accommodate whatever got implemented. The former catches design errors; the latter ratifies them."
tags: [anti-pattern, process, tdd, test-after, test-first-discipline]
confidence: battle-tested
authority: human
last_validated: 2026-04-21
staleness_window: 365
evidence: doc:superpowers
source_attribution: "superpowers@5.0.7"
source_commit: null
counter_nodes: [PBK-PROC-TDD-001, ENF-PROC-TDD-001]
named_in: "superpowers@5.0.7:test-driven-development"
edges:
  - { target: PBK-PROC-TDD-001, type: COUNTERS }
  - { target: ENF-PROC-TDD-001, type: COUNTERS }
---

# Anti-pattern: Test after implementation

## The rationalizations

- "I already manually tested it."
- "Tests after achieve the same purpose."
- "Deleting X hours of work to restart is wasteful."
- "The code works, the tests will just confirm."

## The counter

Delete the code. Write the test first. Watch it fail. Then write the code. No exceptions for sunk cost.
