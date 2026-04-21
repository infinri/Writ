---
playbook_id: PBK-PROC-TDD-001
node_type: Playbook
domain: process
severity: critical
scope: task
trigger: "When writing any new behavior, bug fix, or feature that produces production code."
statement: "RED-GREEN-REFACTOR. Write failing test first. Watch it fail for the right reason. Minimal code to pass. Watch it pass. Refactor. No production code without a failing test first."
rationale: "The RED phase is the evidence that the test works. Skipping it produces false-positive tests that silently erode suite trust. The discipline is absolute: violating the letter = violating the spirit."
tags: [green, playbook, process, red, refactor, tdd]
confidence: battle-tested
authority: human
last_validated: 2026-04-21
staleness_window: 365
evidence: doc:superpowers
source_attribution: "superpowers@5.0.7"
source_commit: null
phase_ids: []
preconditions: []
dispatched_roles: []
edges:
  - { target: ENF-PROC-TDD-001, type: GATES }
  - { target: ANT-PROC-TDD-001, type: COUNTERS }
  - { target: ANT-PROC-TDD-002, type: COUNTERS }
  - { target: ANT-PROC-TDD-003, type: COUNTERS }
  - { target: ANT-PROC-TDD-004, type: COUNTERS }
  - { target: ANT-PROC-TDD-005, type: COUNTERS }
---

# Playbook: TDD

## The cycle

1. **RED** — write the smallest failing test that captures the next behavior. Watch it fail.
2. **GREEN** — write the minimal code that makes it pass. No more, no less.
3. **REFACTOR** — clean up. Both test and code. Tests still pass.
4. Repeat.

## Hard rules

- No production code before a failing test exists.
- If you wrote code before a test: delete the code. Start over. No exceptions.
- If a test passes on first run: see `ANT-PROC-TDD-001`.

## Related

TDD integrates with `SKL-PROC-PLAN-001` (test-per-task in plan), `PBK-PROC-DEBUG-001` (failing test before fix in Phase 4), `PBK-PROC-SDD-001` (implementer does TDD per task).
