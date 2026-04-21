---
playbook_id: PBK-PROC-DEBUG-001
node_type: Playbook
domain: process
severity: high
scope: task
trigger: "When a bug is reported, a test fails unexpectedly, an error is observed, or a fix attempt has failed three or more times."
statement: "Four phases: investigate evidence, pattern analysis, hypothesis testing, implementation. If 3+ fixes fail: question architecture, don't attempt fix #4."
rationale: "Symptom-patching accumulates faster than root-cause fixing. The four-phase sequence forces evidence before action. The 3-fix rule prevents architectural problems from being misdiagnosed as tactical bugs."
tags: [debugging, hypothesis, playbook, process, root-cause]
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
  - { target: ENF-PROC-DEBUG-001, type: GATES }
  - { target: TEC-PROC-ROOTCAUSE-001, type: DISPATCHES }
  - { target: TEC-PROC-HYPOTHESIS-001, type: DISPATCHES }
  - { target: PBK-PROC-TDD-001, type: PRECEDES }
---

# Playbook: Systematic debugging

## Four phases

1. **Investigate** — gather evidence. Logs, traces, reproducers, values at each step.
2. **Pattern** — what's common across failures? What's different across non-failures?
3. **Hypothesis** — form a testable hypothesis. Write a failing test that captures the bug.
4. **Implement** — minimal fix. Watch test pass. Regression-test adjacent functionality.

## The 3-fix rule

Three failed fix attempts = architectural problem. Stop patching. Re-examine the design.

## Red flags

- "Quick fix for now, investigate later."
- "Just try X and see if it works."
- "Emergency, skip the process."
