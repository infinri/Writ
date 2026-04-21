---
rule_id: ENF-COMMS-001
domain: communication
severity: high
scope: session
trigger: "Any time the agent responds to code review, user correction, or technical evaluation input, and the response text contains a forbidden phrase from FRB-COMMS-001 or FRB-COMMS-002."
statement: "Advisory rule: agent responses must not contain performative agreement or unverified success claims. Lexical match against FRB-COMMS forbidden_phrases surfaces violation to friction log."
violation: "Agent responds 'You're absolutely right! Great point!' to reviewer feedback. Matches FRB-COMMS-001 forbidden_phrases. Friction-log records ENF-COMMS-001 violation."
pass_example: "Agent responds 'Let me verify that claim against the codebase. Running the check... Output: <quote>. Based on that, your point holds.' No forbidden phrase. No violation."
enforcement: "Advisory only — post-response pattern match. No pre-response deny (blocking every turn on forbidden-phrase detection would cause false positives). Violations collected for Phase 5 review."
rationale: "Lexical phrase match has false-positive risk in legitimate contexts (e.g., paraphrase of reviewer's argument). Blocking would create friction; advisory+telemetry lets Phase 5 rubric-refine the blocklist."
mandatory: false
always_on: true
confidence: peer-reviewed
authority: human
last_validated: 2026-04-21
staleness_window: 365
evidence: doc:superpowers
mechanical_enforcement_path: null
rationalization_counters:
  - { thought: "Being polite is professional.", counter: "Verified agreement is professional. Performative agreement is reflex, not evaluation." }
red_flag_thoughts:
  - "Great point"
  - "You're right"
  - "Thanks for"
tags: [advisory, always-on, communication, forbidden-phrases]
source_attribution: "superpowers@5.0.7"
source_commit: null
body: "Always-on advisory. Empty mechanical_enforcement_path per plan Section 2.1 — the blocklist is subject to false positives in legitimate contexts."
edges:
  - { target: FRB-COMMS-001, type: TEACHES }
  - { target: FRB-COMMS-002, type: TEACHES }
  - { target: SKL-PROC-REVRECV-001, type: TEACHES }
---

# Rule: No performative agreement or unverified claims

Advisory. No mechanical path. Always-on via universal bundle.
