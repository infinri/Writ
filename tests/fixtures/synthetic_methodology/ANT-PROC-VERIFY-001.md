---
antipattern_id: ANT-PROC-VERIFY-001
node_type: AntiPattern
domain: process
severity: critical
scope: session
trigger: "When the agent claims success based on a subagent's reported success, without running the verification command itself."
statement: "Trusting subagent success reports: the implementer subagent says 'done'; the agent marks task complete without fresh verification. Subagent was wrong; bug ships."
rationale: "Subagents can be wrong. Their confidence is not evidence. The verification discipline exists precisely because confidence-as-evidence is the canonical agentic-coding failure mode."
tags: [anti-pattern, process, subagent-trust, verification]
confidence: battle-tested
authority: human
last_validated: 2026-04-21
staleness_window: 365
evidence: doc:superpowers
source_attribution: "superpowers@5.0.7"
source_commit: null
counter_nodes: [SKL-PROC-VERIFY-001, ENF-PROC-VERIFY-001]
named_in: "superpowers@5.0.7:verification-before-completion"
edges:
  - { target: SKL-PROC-VERIFY-001, type: COUNTERS }
  - { target: ENF-PROC-VERIFY-001, type: COUNTERS }
---

# Anti-pattern: Trust subagent success reports

## Counter

Run the verification command yourself, in this message. Subagent reports are hypotheses, not evidence. Fresh run is evidence.
