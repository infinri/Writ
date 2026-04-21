---
rule_id: ENF-PROC-SDD-001
domain: process
severity: high
scope: task
trigger: "When subagent-driven development is active and reviewers are dispatched out of order (code-quality before spec-compliance, or either review skipped)."
statement: "Spec-compliance review must complete before code-quality review starts. Out-of-order dispatch denied by gate."
violation: "Agent dispatches ROL-CODE-REVIEWER-001 before ROL-SPEC-REVIEWER-001 has returned findings. Gate denies the Task dispatch."
pass_example: "Agent dispatches ROL-SPEC-REVIEWER-001, waits for findings, resolves any, then dispatches ROL-CODE-REVIEWER-001. Gate permits."
enforcement: "writ-sdd-review-order.sh on PreToolUse Task: checks session.review_ordering_state for the current task. Denies if code-reviewer dispatched before spec-reviewer has completed."
rationale: "Spec-first catches wrong-thing-built. Polishing wrong code is wasted work."
mandatory: true
always_on: false
confidence: peer-reviewed
authority: human
last_validated: 2026-04-21
staleness_window: 365
evidence: doc:superpowers
mechanical_enforcement_path: ".claude/hooks/writ-sdd-review-order.sh"
rationalization_counters:
  - { thought: "Code-quality review is fast, let me start in parallel.", counter: "Parallel + out-of-order = polishing unknown correctness. Sequential, spec first." }
  - { thought: "Self-review by implementer is enough.", counter: "Implementer bias. Fresh reviewer is the whole point of SDD." }
red_flag_thoughts:
  - "Save time by parallel reviews"
  - "Skip spec review, it looks right"
tags: [enforcement, process, review-order, sdd]
source_attribution: "superpowers@5.0.7"
source_commit: null
body: ""
edges:
  - { target: PBK-PROC-SDD-001, type: TEACHES }
  - { target: ANT-PROC-SDD-001, type: COUNTERS }
---

# Rule: Spec-compliance review before code-quality review

Mechanical via `writ-sdd-review-order.sh`.
