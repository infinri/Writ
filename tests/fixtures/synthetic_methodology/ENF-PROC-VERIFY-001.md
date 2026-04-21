---
rule_id: ENF-PROC-VERIFY-001
domain: process
severity: critical
scope: session
trigger: "When the agent attempts to mark a TodoWrite item complete, or when the session's Stop hook fires and completion claims exist without verification evidence."
statement: "Completion claims require fresh verification evidence in the same message. TodoWrite completion denied without verification_evidence set in session state."
violation: "Agent marks todo 'implement fetch()' as completed in TodoWrite without running pytest in the current message. Gate denies. Friction log records 'gate_denied: ENF-PROC-VERIFY-001'."
pass_example: "Agent runs pytest tests/test_api.py, output shows '1 passed', quotes the output, then TodoWrite marks todo completed with verification_evidence='pytest tests/test_api.py: 1 passed'."
enforcement: "writ-verify-before-claim.sh on PreToolUse TodoWrite + Stop: checks session.verification_evidence for the claimed item. Deny if missing or stale."
rationale: "Completion claims without evidence erode user trust. Mechanical enforcement prevents the confidence-as-evidence failure mode."
mandatory: true
always_on: true
confidence: battle-tested
authority: human
last_validated: 2026-04-21
staleness_window: 365
evidence: doc:superpowers
mechanical_enforcement_path: ".claude/hooks/writ-verify-before-claim.sh"
rationalization_counters:
  - { thought: "I ran it earlier in the session, still counts.", counter: "Stale evidence. Code may have changed since. Run fresh." }
  - { thought: "Linter passed, that's enough.", counter: "Partial verification. Linter does not run the code." }
  - { thought: "Subagent said it's done.", counter: "See ANT-PROC-VERIFY-001. Subagent confidence is not evidence." }
red_flag_thoughts:
  - "Should be fine"
  - "Probably works"
  - "Looks right"
tags: [always-on, completion, enforcement, process, verification]
source_attribution: "superpowers@5.0.7"
source_commit: null
body: "Always-on rule — injected in universal bundle per plan Section 3.4."
edges:
  - { target: SKL-PROC-VERIFY-001, type: TEACHES }
  - { target: ANT-PROC-VERIFY-001, type: COUNTERS }
  - { target: FRB-COMMS-002, type: GATES }
---

# Rule: Verify before claiming complete

Mechanical via `writ-verify-before-claim.sh`. Always-on.
