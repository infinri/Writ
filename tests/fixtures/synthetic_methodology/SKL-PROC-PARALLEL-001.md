---
skill_id: SKL-PROC-PARALLEL-001
node_type: Skill
domain: process
severity: medium
scope: task
trigger: "When facing 2+ independent tasks that can be worked on without shared state or sequential dependencies."
statement: "Dispatch one agent per independent problem domain with focused scope and explicit output expectations. Verify independence before dispatching — shared state = sequential, not parallel."
rationale: "Parallel dispatch multiplies investigation speed only when tasks are actually independent. Dispatching parallel agents over coupled tasks produces conflicts and wasted work."
tags: [agents, independence, parallel, process, subagents]
confidence: peer-reviewed
authority: human
last_validated: 2026-04-21
staleness_window: 365
evidence: doc:superpowers
source_attribution: "superpowers@5.0.7"
source_commit: null
edges:
  - { target: PBK-PROC-SDD-001, type: DEMONSTRATES }
---

# Skill: Dispatch parallel agents

## Decision tree

- Multiple tasks? → Are they independent? → Yes: dispatch in parallel / No: dispatch sequentially or as a single agent.
- Tasks share state? → Sequential.
- Tasks produce outputs the next task consumes? → Sequential.
- Tasks touch disjoint problem domains with no shared state? → Parallel.

## One agent per problem domain

Not "one agent to fix all failing tests" (broad scope). Instead: "one agent per failing test subsystem with its own problem domain."

## Explicit output expectations

Each dispatched agent gets a prompt naming exactly what output structure to return (JSON shape, file paths, or finding format). Vague outputs waste parallelism.
