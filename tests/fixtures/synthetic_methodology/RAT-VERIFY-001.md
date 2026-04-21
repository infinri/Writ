---
rationalization_id: RAT-VERIFY-001
node_type: Rationalization
domain: process
scope: session
trigger: "When the agent is about to claim completion and has recent (but not fresh) verification evidence in session memory."
statement: "'I ran the tests earlier in this session, the claim is fine.' The counter: stale evidence. Code has potentially changed since. Run fresh in this message."
rationale: "Session-stale evidence is a common trap. Earlier runs attest to an earlier state. Fresh runs attest to the current state. Only fresh runs back a current claim."
tags: [completion-claim, process, rationalization, stale-evidence, verification]
confidence: peer-reviewed
authority: human
last_validated: 2026-04-21
staleness_window: 365
evidence: doc:superpowers
source_attribution: "superpowers@5.0.7"
source_commit: null
thought: "I ran the tests earlier in this session, the claim is fine."
counter: "Stale evidence. Code may have changed since. Run the verification command fresh in this message, quote the output, then claim."
attached_to: ENF-PROC-VERIFY-001
edges:
  - { target: ENF-PROC-VERIFY-001, type: ATTACHED_TO }
  - { target: SKL-PROC-VERIFY-001, type: COUNTERS }
---

# Rationalization: Earlier-in-session evidence is enough

Non-retrievable. Bundle-only.
