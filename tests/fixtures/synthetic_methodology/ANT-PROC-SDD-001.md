---
antipattern_id: ANT-PROC-SDD-001
node_type: AntiPattern
domain: process
severity: high
scope: task
trigger: "When subagent-driven development is invoked and reviewers are dispatched in wrong order (code-quality before spec-compliance) or spec review is skipped entirely."
statement: "Wrong review order: running code-quality review before spec-compliance review wastes effort polishing code that implements the wrong spec."
rationale: "Spec-compliance is the coarser filter — it catches 'built the wrong thing.' Code-quality is the finer filter — it polishes 'built the right thing.' Inverting the order polishes wrongness."
tags: [anti-pattern, process, review-order, sdd, spec-first]
confidence: peer-reviewed
authority: human
last_validated: 2026-04-21
staleness_window: 365
evidence: doc:superpowers
source_attribution: "superpowers@5.0.7"
source_commit: null
counter_nodes: [PBK-PROC-SDD-001]
named_in: "superpowers@5.0.7:subagent-driven-development"
edges:
  - { target: PBK-PROC-SDD-001, type: COUNTERS }
---

# Anti-pattern: Wrong review order in SDD

## Counter

Spec compliance first, always. If spec fails: back to implementer. Only when spec passes does code-quality review run. See `PBK-PROC-SDD-001`.
