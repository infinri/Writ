---
phase_id: PHA-BRAIN-009
node_type: Phase
domain: process
scope: session
trigger: "Ninth and final phase of PBK-PROC-BRAIN-001. Fires after design is presented."
statement: "Wait for explicit user approval. Do not advance to implementation. Silence is not approval. Maybe is not approval. 'Sounds good' is not approval. Only an explicit approval word advances."
rationale: "The approval gate is the load-bearing control that prevents premature implementation. Any weakening (inferring approval from tone, silence, or casual phrasing) collapses the whole discipline."
tags: [approval, brainstorming, gate, phase, process]
confidence: battle-tested
authority: human
last_validated: 2026-04-21
staleness_window: 365
evidence: doc:superpowers
source_attribution: "superpowers@5.0.7"
source_commit: null
position: 9
name: "Wait for approval"
description: "Wait for the user's explicit approval word ('approved', 'yes go ahead', or equivalent explicit signal). Do not infer approval from silence, tone, or paraphrase."
parent_playbook_id: PBK-PROC-BRAIN-001
edges: []
---

# Phase 9: Wait for approval

Non-retrievable. Bundle-only. Gates advancement to implementation — see `ENF-PROC-BRAIN-001` for mechanical enforcement.
