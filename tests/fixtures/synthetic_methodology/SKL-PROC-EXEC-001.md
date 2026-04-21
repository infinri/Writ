---
skill_id: SKL-PROC-EXEC-001
node_type: Skill
domain: process
severity: high
scope: task
trigger: "When an approved implementation plan exists and must be executed task-by-task in a separate session, with per-task verification."
statement: "Load the plan, review it for gaps, execute tasks in order, verify each before the next. On blocker: STOP and ask — never work around."
rationale: "Execution without per-task verification silently accumulates defects. Stopping on blockers prevents partial-completion states that are expensive to unwind."
tags: [blockers, execution, plan, process, verification]
confidence: peer-reviewed
authority: human
last_validated: 2026-04-21
staleness_window: 365
evidence: doc:superpowers
source_attribution: "superpowers@5.0.7"
source_commit: null
edges:
  - { target: SKL-PROC-PLAN-001, type: PRECEDES }
  - { target: PBK-PROC-SDD-001, type: DEMONSTRATES }
  - { target: SKL-PROC-VERIFY-001, type: DEMONSTRATES }
---

# Skill: Execute an approved plan

## Load and review

Read the entire plan before starting. If gaps exist (undefined types, missing files, contradictions), STOP and ask — do not fill in.

## Execute in order

One task at a time. Verify each task's success criteria before moving to the next.

## On blocker

STOP and ask the user. Do not improvise a workaround. Blockers are design-level, not implementation-level.
