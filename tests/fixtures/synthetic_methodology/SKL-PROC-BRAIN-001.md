---
skill_id: SKL-PROC-BRAIN-001
node_type: Skill
domain: process
severity: high
scope: session
trigger: "When a user requests a new feature, behavior change, refactor, or investigation that could result in code changes, including tasks that seem obviously simple."
statement: "Before writing any code, produce a design by presenting 2-3 concrete approaches with trade-offs, asking clarifying questions, and waiting for explicit user approval."
rationale: "The canonical failure mode of agentic coding is premature implementation on tasks the agent considered simple. Naming the discipline, the failure modes, and the counter-thoughts lets retrieval surface the right reminder at the right trigger."
tags: [approval-gate, brainstorming, design, pre-implementation, process]
confidence: peer-reviewed
authority: human
last_validated: 2026-04-21
staleness_window: 365
evidence: doc:superpowers
source_attribution: "superpowers@5.0.7"
source_commit: null
edges:
  - { target: PBK-PROC-BRAIN-001, type: TEACHES }
  - { target: ENF-PROC-BRAIN-001, type: GATES }
  - { target: SKL-PROC-VISUAL-001, type: DEMONSTRATES }
  - { target: SKL-PROC-PLAN-001, type: PRECEDES }
---

# Skill: Brainstorm before writing code

Natural language: before any implementation, you must present a design and receive user approval. This skill names the discipline, the failure modes, and the counter-thoughts.

## When this applies

Any new feature request. Any behavior change. Any refactor. Any investigation that would result in code changes. The skill applies regardless of perceived task size; "simple" is a property of how the agent feels, not of how the user will evaluate the outcome.

## Red flag thoughts (indicators of violation)

- "This is too simple to need a design."
- "I've brainstormed enough, let me start coding."
- "The user already described what they want, I can skip the design."
- "They'll tell me if I'm wrong once I show them the code."

## Hard gate

Do not invoke any implementation skill, write any code, scaffold any project, or take any implementation action until a design has been presented AND the user has approved it. This is mechanical, not advisory.

## Related rationalizations

Attached as standalone `Rationalization` nodes via `ATTACHED_TO` edges (graph-canonical). See `RAT-BRAIN-*` family. Inline render-convenience form lives on `ENF-PROC-BRAIN-001`.
