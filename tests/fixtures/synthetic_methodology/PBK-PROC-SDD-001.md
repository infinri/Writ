---
playbook_id: PBK-PROC-SDD-001
node_type: Playbook
domain: process
severity: high
scope: task
trigger: "When executing an approved plan with subagents: dispatch implementer per task, then spec-reviewer, then code-quality-reviewer, all in the same session."
statement: "Fresh subagent per task. Two-stage review in order: spec compliance FIRST, then code quality. Ignore implementer success reports, verify independently."
rationale: "Fresh subagents avoid context pollution from the implementer's framing. The two-stage order (spec first) catches wrong-thing-built before polishing wrong thing. Independent verification prevents subagent rubber-stamping."
tags: [dispatch, playbook, process, subagents, two-stage-review]
confidence: peer-reviewed
authority: human
last_validated: 2026-04-21
staleness_window: 365
evidence: doc:superpowers
source_attribution: "superpowers@5.0.7"
source_commit: null
phase_ids: []
preconditions: [SKL-PROC-PLAN-001]
dispatched_roles:
  - ROL-IMPLEMENTER-001
  - ROL-SPEC-REVIEWER-001
  - ROL-CODE-REVIEWER-001
edges:
  - { target: ENF-PROC-SDD-001, type: GATES }
  - { target: ROL-IMPLEMENTER-001, type: DISPATCHES }
  - { target: ROL-SPEC-REVIEWER-001, type: DISPATCHES }
  - { target: ROL-CODE-REVIEWER-001, type: DISPATCHES }
  - { target: SKL-PROC-EXEC-001, type: TEACHES }
---

# Playbook: Subagent-driven development

## Per task

1. Fresh implementer subagent with full task context (plan excerpt + related files).
2. Fresh spec-reviewer subagent. Does the implementation match the spec? If no: back to implementer with diff.
3. Fresh code-quality-reviewer subagent. ONLY after spec review passes.
4. Implementer fixes code-quality issues if any. Re-review.
5. Mark task complete — and verify with `SKL-PROC-VERIFY-001` before believing the implementer.

## Anti-patterns

- Wrong review order (code-quality before spec): see `ANT-PROC-SDD-001`.
- Self-review by implementer: forbidden.
- Parallel implementer dispatch: forbidden (conflicts).
