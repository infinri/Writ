---
antipattern_id: ANT-PROC-PARALLEL-001
node_type: AntiPattern
domain: process
severity: medium
scope: task
trigger: "When the agent proposes dispatching one subagent with a broad cross-domain scope (e.g., 'fix all the failing tests') instead of one agent per independent problem domain."
statement: "Broad scope per agent: one subagent handed multiple unrelated failures at once. Conflicts, context bleed, and partial results. The whole point of parallel dispatch is one agent per independent domain."
rationale: "One-agent-to-rule-them-all defeats the parallelism: the agent sequentializes internally anyway, and its cross-domain context pollutes each subproblem's investigation. Independent failures in independent domains need independent agents."
tags: [anti-pattern, broad-scope, parallel, process, subagents]
confidence: peer-reviewed
authority: human
last_validated: 2026-04-21
staleness_window: 365
evidence: doc:superpowers
source_attribution: "superpowers@5.0.7"
source_commit: null
counter_nodes: [SKL-PROC-PARALLEL-001]
named_in: "superpowers@5.0.7:dispatching-parallel-agents"
edges:
  - { target: SKL-PROC-PARALLEL-001, type: COUNTERS }
---

# Anti-pattern: Broad scope per parallel agent

## The smell

"One agent to fix all the failing tests." "One agent to investigate the entire slow-endpoint issue." Scope is cross-domain; agent has to thrash between contexts.

## Counter

Decompose first. One domain per agent. Then dispatch in parallel. See `SKL-PROC-PARALLEL-001` for the decision tree.
