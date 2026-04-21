---
antipattern_id: ANT-PROC-TDD-001
node_type: AntiPattern
domain: process
severity: high
scope: task
trigger: "When a test 'passes' on first run after being written, or when the agent cannot explain why the test would have failed before implementation existed."
statement: "Test that passes immediately after being written is an anti-pattern: you did not watch it fail, so you do not know whether it tests the intended behavior or some coincidental truth. Delete the test, start over, write it first, watch it fail for the right reason, then implement."
rationale: "Watching the test fail for the right reason is the only evidence that the test exercises the intended behavior. Without the red phase, false-positive tests accumulate silently and destroy test-suite trust."
tags: [anti-pattern, process, red-phase, tdd, test-watches-nothing]
confidence: peer-reviewed
authority: human
last_validated: 2026-04-21
staleness_window: 365
evidence: doc:superpowers
source_attribution: "superpowers@5.0.7"
source_commit: null
counter_nodes:
  - PBK-PROC-TDD-001
  - ENF-PROC-TDD-001
named_in: "superpowers@5.0.7:testing-anti-patterns"
edges:
  - { target: PBK-PROC-TDD-001, type: COUNTERS }
  - { target: ENF-PROC-TDD-001, type: COUNTERS }
---

# Anti-pattern: Test passes on first run

## Why this is broken

The RED phase of TDD is not a formality. A test that passes immediately could be:

- Testing a different behavior than you intended (false positive).
- Testing a vacuous condition (`assert True`).
- Testing behavior that already happened to exist (no new assertion made).
- Silently skipping via a framework feature (`@pytest.mark.skip`, wrong decorator).

If you cannot state, in one sentence, what the test output was when it failed and why that failure was the correct failure, the test is not trustworthy.

## Counter

Delete the test file. Start over. Write the test first, run it against the missing implementation, read the failure output, confirm the failure is the expected "function undefined" or "assertion X != Y" failure, and only then write the minimal implementation to make it pass.

## Example

- **Bad:** Wrote `test_add_two_numbers()`, ran, passed first try. Cannot explain why.
- **Good:** Wrote `test_add_two_numbers()`, ran, got `NameError: name 'add' is not defined`. Wrote `def add(a, b): return a + b`. Re-ran, passed. Now the test is trusted.

## Related anti-patterns

- `ANT-PROC-TDD-002` — testing the mock instead of the behavior
- `ANT-PROC-TDD-005` — "I'll write the test after the code" (test-after)
