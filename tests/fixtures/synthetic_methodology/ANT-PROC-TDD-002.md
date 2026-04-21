---
antipattern_id: ANT-PROC-TDD-002
node_type: AntiPattern
domain: process
severity: high
scope: task
trigger: "When a test uses a mock for the very function or class it claims to verify."
statement: "Testing the mock instead of the behavior: the test asserts that the mock was called with certain arguments instead of that the real code produces correct output. Passes when the real code is broken."
rationale: "Mocks exist to isolate collaborators, not the subject under test. Mocking the subject replaces behavior verification with call-pattern verification, which is vacuous."
tags: [anti-pattern, mock, process, tdd, testing-the-mock]
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

# Anti-pattern: Testing the mock

## The smell

`mock.assert_called_with(expected_args)` as the primary assertion. No assertion against real output. Real code could be returning wrong values; test still passes.

## Counter

Mock the collaborators, not the subject. Assert on real output values. If you can't avoid mocking the subject, the test design is wrong — consider integration-level test instead.
