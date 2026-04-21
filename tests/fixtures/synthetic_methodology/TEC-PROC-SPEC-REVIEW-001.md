---
technique_id: TEC-PROC-SPEC-REVIEW-001
node_type: Technique
domain: process
severity: medium
scope: task
trigger: "After a plan or spec is drafted, before it is presented to the user for approval."
statement: "Inline self-review checklist: placeholder scan, internal consistency, scope check, ambiguity check. Catches 3-5 real issues per run in seconds, not minutes."
rationale: "Subagent review loops were replaced by this inline technique in Superpowers v5.0.6 after regression testing showed comparable defect rates at ~25× less time cost."
tags: [inline, plan, process, self-review, technique]
confidence: production-validated
authority: human
last_validated: 2026-04-21
staleness_window: 365
evidence: doc:superpowers
source_attribution: "superpowers@5.0.7"
source_commit: null
edges:
  - { target: SKL-PROC-PLAN-001, type: DEMONSTRATES }
  - { target: SKL-PROC-BRAIN-001, type: DEMONSTRATES }
---

# Technique: Inline self-review

## Checklist

- [ ] No placeholders (TBD, TODO, "similar to above", vague phrases)
- [ ] Internal consistency (types match across tasks, functions named the same way referenced)
- [ ] Scope matches spec (no silent scope creep, no missing spec items)
- [ ] Unambiguous (each step has exactly one interpretation)

## Time budget

~30 seconds. Not a replacement for external review on large PRs, but eliminates the cheap defect class before presenting.
