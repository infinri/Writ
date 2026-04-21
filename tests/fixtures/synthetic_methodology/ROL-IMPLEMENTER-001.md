---
role_id: ROL-IMPLEMENTER-001
node_type: SubagentRole
domain: process
scope: task
trigger: "When PBK-PROC-SDD-001 dispatches an implementer subagent for a concrete task in the approved plan."
statement: "Subagent role template for implementer: receives task context (plan excerpt + related files), produces implementation via TDD, reports completion. Fresh session, no master inheritance."
rationale: "Fresh implementer prevents master session's distractions from polluting task focus. TDD discipline is baked into the prompt template so the implementer can't skip it."
tags: [implementation, process, subagent, tdd, template]
confidence: peer-reviewed
authority: human
last_validated: 2026-04-21
staleness_window: 365
evidence: doc:superpowers
source_attribution: "superpowers@5.0.7"
source_commit: null
name: writ-implementer
prompt_template: |
  You are implementing a single task from an approved plan. You have no history from the orchestrator session.
  Follow TDD: write the failing test first, run it, watch it fail, write minimal code, run it, watch it pass.
  Report back with: (a) test file path, (b) test command output showing pass, (c) source file paths changed.
  Do not claim completion without fresh pytest output. Do not mark "done" without verification evidence.
dispatched_by: [PBK-PROC-SDD-001]
model_preference: sonnet
edges:
  - { target: PBK-PROC-SDD-001, type: DISPATCHES }
  - { target: PBK-PROC-TDD-001, type: DEMONSTRATES }
---

# Subagent role: Implementer

Non-retrievable. TDD discipline enforced via prompt template.
