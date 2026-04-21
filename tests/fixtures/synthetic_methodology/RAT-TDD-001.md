---
rationalization_id: RAT-TDD-001
node_type: Rationalization
domain: process
scope: task
trigger: "When the agent has written production code and is considering whether to add tests after the fact."
statement: "'I already manually tested it, writing unit tests later is the same thing.' The counter: manual tests don't regress. A committed test does. Manual testing is not substitutable."
rationale: "The most common TDD bypass. Manual testing gives one data point; unit tests give permanent regression coverage. Conflating them is the canonical rationalization that erodes the test-first discipline."
tags: [manual-testing, process, rationalization, tdd, test-after]
confidence: peer-reviewed
authority: human
last_validated: 2026-04-21
staleness_window: 365
evidence: doc:superpowers
source_attribution: "superpowers@5.0.7"
source_commit: null
thought: "I already manually tested it, writing unit tests later is the same thing."
counter: "Manual tests don't regress. A committed test does. Manual testing is not substitutable. Delete the code, write the test first, watch it fail, reimplement."
attached_to: ENF-PROC-TDD-001
edges:
  - { target: ENF-PROC-TDD-001, type: ATTACHED_TO }
  - { target: PBK-PROC-TDD-001, type: COUNTERS }
---

# Rationalization: Manual testing substitutes for unit tests

Non-retrievable. Surfaces via bundle expansion when `ENF-PROC-TDD-001` or `PBK-PROC-TDD-001` is retrieved.
