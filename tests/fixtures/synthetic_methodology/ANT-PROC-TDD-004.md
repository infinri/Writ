---
antipattern_id: ANT-PROC-TDD-004
node_type: AntiPattern
domain: process
severity: medium
scope: task
trigger: "When a mock is configured with partial behavior that differs from the real collaborator's contract (e.g., mocks return None where real code would raise)."
statement: "Incomplete mocks: the mock's behavior diverges from the real collaborator in ways that mask bugs. Tests pass; production fails at the same seam the mock covered."
rationale: "A mock is a hypothesis about the collaborator. An incomplete hypothesis produces a test that verifies the wrong thing. The bug migrates from code to test configuration."
tags: [anti-pattern, incomplete-mock, mock, process, tdd]
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

# Anti-pattern: Incomplete mocks

## The smell

Mock returns a default value when the real collaborator would raise. Mock returns success when the real collaborator would time out. Test passes; integration fails in the same direction the mock diverged.

## Counter

Mock behavior must match the real collaborator's contract at least on the paths the test exercises. Prefer integration tests or fakes for collaborators with complex failure modes.
