---
playbook_id: PBK-PROC-REVREQ-001
node_type: Playbook
domain: process
severity: medium
scope: task
trigger: "When a task is complete and ready for external review before merge or PR."
statement: "Dispatch code-reviewer subagent with fresh context and git-SHA-scoped diff. Fix Critical findings before merge; fix Important findings before merge; Minor findings user's call."
rationale: "Review-early-review-often catches issues cheap. Fresh-context reviewer judges the diff, not the implementer's framing. SHA scoping prevents session-history inheritance."
tags: [code-review, git, playbook, process, subagent]
confidence: peer-reviewed
authority: human
last_validated: 2026-04-21
staleness_window: 365
evidence: doc:superpowers
source_attribution: "superpowers@5.0.7"
source_commit: null
phase_ids: []
preconditions: []
dispatched_roles: [ROL-CODE-REVIEWER-001]
edges:
  - { target: ROL-CODE-REVIEWER-001, type: DISPATCHES }
  - { target: SKL-PROC-REVRECV-001, type: PRECEDES }
---

# Playbook: Request code review

## Dispatch

Task tool, subagent type matching `ROL-CODE-REVIEWER-001`. Provide: base SHA, head SHA, spec summary.

## Handle findings

- Critical → must fix before merge.
- Important → must fix before merge.
- Minor → user discretion.

Apply the `SKL-PROC-REVRECV-001` skill when reading findings.
