---
antipattern_id: ANT-PROC-DEBUG-001
node_type: AntiPattern
domain: process
severity: high
scope: task
trigger: "When the agent attempts multiple fixes simultaneously, or attempts a fourth fix after three have failed on the same bug."
statement: "Multiple fixes at once or one-more-fix-attempt: attempting simultaneous changes makes it impossible to attribute cause; attempting a fourth fix after three failures means the architecture is wrong, not the patch."
rationale: "Parallel changes destroy the ability to attribute cause. Repeated fix attempts without new hypotheses indicate the bug model is wrong — the bug is architectural, not tactical."
tags: [anti-pattern, debugging, multi-fix, process, three-fix-rule]
confidence: peer-reviewed
authority: human
last_validated: 2026-04-21
staleness_window: 365
evidence: doc:superpowers
source_attribution: "superpowers@5.0.7"
source_commit: null
counter_nodes: [PBK-PROC-DEBUG-001]
named_in: "superpowers@5.0.7:systematic-debugging"
edges:
  - { target: PBK-PROC-DEBUG-001, type: COUNTERS }
---

# Anti-pattern: Multiple fixes / fourth-fix

## Counter

One change at a time. Verify each. If three fixes failed: stop. Re-examine the architecture. The bug is bigger than a patch.
