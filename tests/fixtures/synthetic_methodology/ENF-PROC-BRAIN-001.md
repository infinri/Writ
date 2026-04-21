---
rule_id: ENF-PROC-BRAIN-001
domain: process
severity: critical
scope: session
trigger: "In Work mode: when the agent attempts any code-producing tool call (Write, Edit, Bash with destructive verbs) before a design has been approved for the current task."
statement: "In Work mode, no code-producing action is permitted before a design artifact exists and has been approved by the user. Applies to every project regardless of perceived simplicity."
violation: "Agent in Work mode receives 'refactor this function to use async.' Without presenting approaches or waiting for approval, emits Write(src/api.py, ...). Gate denies the write; friction log records the attempt."
pass_example: "Agent in Work mode receives 'refactor this function to use async.' Presents 3 approaches with trade-offs, asks clarifying questions, waits for user to say 'approved — go with option A,' then emits Write. Gate permits the write because session.design_approved = true."
enforcement: ".claude/hooks/validate-exit-plan.sh + writ-session.py phase state machine check session.mode == 'work' AND session.design_approved == true before any code-producing tool call."
rationale: "The canonical failure mode of agentic coding is premature implementation on tasks the agent considered simple. This rule makes 'too simple' impossible as a rationalization — the gate fires regardless of task size."
mandatory: true
always_on: false
confidence: peer-reviewed
authority: human
last_validated: 2026-04-21
staleness_window: 365
evidence: doc:superpowers
mechanical_enforcement_path: ".claude/hooks/validate-exit-plan.sh + writ-session.py phase state machine"
rationalization_counters:
  - { thought: "This task is obviously simple.", counter: "Every project goes through brainstorm. 'Obvious' is your read, not the user's." }
  - { thought: "User already told me what they want.", counter: "User described a wish. Design is your synthesis with trade-offs." }
  - { thought: "I'll brainstorm after writing a prototype.", counter: "Prototype-then-design inverts the discipline. The rule says design-then-prototype, always." }
  - { thought: "Emergency — no time for brainstorm.", counter: "Emergency is exactly when skipping process fails. Process is a compressor, not a tax." }
red_flag_thoughts:
  - "Too simple for brainstorm"
  - "I'll design as I code"
  - "Just this once"
  - "Let me start and adjust"
tags: [approval-gate, brainstorming, enforcement, process, work-mode]
source_attribution: "superpowers@5.0.7"
source_commit: null
body: "Rationalization counters are also represented as standalone Rationalization nodes (RAT-BRAIN-001 through RAT-BRAIN-004) attached via ATTACHED_TO edges — the graph representation is canonical; the inline rationalization_counters field above is a render convenience for always-on summary injection per schema proposal rationalization-representation section."
edges:
  - { target: SKL-PROC-BRAIN-001, type: TEACHES }
  - { target: PBK-PROC-BRAIN-001, type: TEACHES }
  - { target: ENF-PROC-PLAN-001, type: PRECEDES }
---

# Rule: Brainstorm before code (mandatory in Work mode)

## Statement

The agent must present a design and receive explicit user approval before any code-producing tool call executes in Work mode. No exceptions for task simplicity. Gate applies universally within Work mode; non-work modes skip this enforcement per absorption plan Section 0.4 decision 1.

## Violation (bad)

Agent in Work mode, user request: "refactor this function to use async."

```
Write(src/api.py, "async def fetch(url): ...")
```

No design presented. No approval. Write denied by gate. Friction log records `gate_denied: ENF-PROC-BRAIN-001, attempt=write, approval_state=missing`.

## Pass (good)

Agent in Work mode, user request: "refactor this function to use async."

```
Present design:
Option A: convert fetch() to async, update all callers (3 sites).
Option B: add an async wrapper, leave sync version as fallback.
Option C: async-only, break the sync callers — requires caller refactor.

Trade-offs: A is clean but risky; B is safe but doubles surface area; C is aggressive.

My recommendation: A. Confirm, or say which option you prefer.

Wait for user: "approved - go with A."

Then: Write(src/api.py, ...)
```

## Enforcement path

`.claude/hooks/validate-exit-plan.sh` + `writ-session.py` phase state machine. The hook checks `session.mode == "work"` AND `session.design_approved == true` before any code-producing tool call. Both conditions required.
