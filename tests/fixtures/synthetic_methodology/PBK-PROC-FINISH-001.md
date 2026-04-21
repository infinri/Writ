---
playbook_id: PBK-PROC-FINISH-001
node_type: Playbook
domain: process
severity: medium
scope: task
trigger: "When development work on a branch is complete (tests pass) and must be wrapped up via merge, PR, keep-as-is, or discard."
statement: "Verify tests pass. Present exactly 4 options: (1) merge locally + cleanup, (2) push + create PR, (3) keep as-is, (4) discard (requires typed 'discard' confirmation)."
rationale: "A fixed option set eliminates novel-path improvisation at finish time. The typed-confirmation requirement on discard prevents accidental work loss."
tags: [branch, finish, merge, playbook, pr, process]
confidence: peer-reviewed
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
  - { target: SKL-PROC-VERIFY-001, type: PRECEDES }
  - { target: TEC-PROC-WORKTREE-001, type: DISPATCHES }
---

# Playbook: Finish a development branch

## Precondition

Tests pass. If not, show failures and STOP — no option presentation.

## The 4 options

1. **Merge locally** — merge to base, delete worktree, delete branch.
2. **Push and create PR** — push, open PR, leave worktree intact.
3. **Keep as-is** — no action, user continues later.
4. **Discard** — requires typed `discard`, deletes branch and worktree.

## Never offer a 5th option

Custom merge workflows are ANT-PROC-FINISH territory. Present the 4 options exactly.
