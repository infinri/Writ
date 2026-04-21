---
rationalization_id: RAT-BRAIN-001
node_type: Rationalization
domain: process
scope: session
trigger: "When the agent considers the current task and forms a thought matching 'too simple to need design' or close paraphrase."
statement: "'This task is obviously simple.' The counter: every project goes through brainstorm. 'Obvious' is the agent's read, not the user's."
rationale: "Canonical rationalization for skipping brainstorm. Captured as a standalone node so Stage 4 graph traversal can surface it as a bundle member when the parent rule is retrieved, and so Phase 5 frequency graduation can track how often this specific thought triggers violations."
tags: [brainstorming, process, rationalization, too-simple]
confidence: peer-reviewed
authority: human
last_validated: 2026-04-21
staleness_window: 365
evidence: doc:superpowers
source_attribution: "superpowers@5.0.7"
source_commit: null
thought: "This task is obviously simple."
counter: "Every project goes through brainstorm. 'Obvious' is your read, not the user's. The 'too simple' framing is the canonical anti-pattern named in the source skill."
attached_to: ENF-PROC-BRAIN-001
edges:
  - { target: ENF-PROC-BRAIN-001, type: ATTACHED_TO }
  - { target: SKL-PROC-BRAIN-001, type: COUNTERS }
---

# Rationalization: "This task is obviously simple"

Non-retrievable node. Surfaces only via `ATTACHED_TO` edge during Stage 4 bundle expansion when `ENF-PROC-BRAIN-001` or `SKL-PROC-BRAIN-001` is the primary retrieval hit.

## The thought

"This task is obviously simple."

## The counter

Every project goes through brainstorm. "Obvious" is your read, not the user's. The "too simple" framing is the canonical anti-pattern named in the source skill.

## Inline vs graph representation

Per schema proposal rationalization-representation section: this standalone graph node is canonical. The inline `rationalization_counters` field on `ENF-PROC-BRAIN-001` is a render convenience populated by ingest. On divergence, this node wins.
