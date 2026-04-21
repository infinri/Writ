---
antipattern_id: ANT-PROC-TDD-003
node_type: AntiPattern
domain: process
severity: high
scope: task
trigger: "When a test calls private/internal methods (e.g., Python `_name` prefix, or class-private fields exposed only for test access)."
statement: "Test-only methods: production code gains a public surface that exists only so tests can reach internal state. The test is then coupled to implementation, not behavior."
rationale: "Tests that reach internals break on refactor even when behavior is unchanged. A test that tests behavior should not care how the behavior is produced internally."
tags: [anti-pattern, encapsulation, process, tdd, test-only-method]
confidence: peer-reviewed
authority: human
last_validated: 2026-04-21
staleness_window: 365
evidence: doc:superpowers
source_attribution: "superpowers@5.0.7"
source_commit: null
counter_nodes: [PBK-PROC-TDD-001]
named_in: "superpowers@5.0.7:testing-anti-patterns"
edges:
  - { target: PBK-PROC-TDD-001, type: COUNTERS }
---

# Anti-pattern: Test-only methods

## The smell

Public method, field, or getter added "just so tests can verify X." Removed in refactor → tests break, production unaffected.

## Counter

Test through the public interface. If behavior is hard to reach through public methods, the public interface is missing something the user of the class also needs — not just the test.
