---
phase_id: PHA-BRAIN-001
node_type: Phase
domain: process
scope: session
trigger: "First phase of PBK-PROC-BRAIN-001. Fires when the playbook starts execution."
statement: "Understand the user's intent by restating their goal in your own words and confirming the restatement."
rationale: "Premature approach generation is a leading cause of brainstorm-fail. Restating the user's goal forces the agent to notice ambiguities before committing to an approach, and surfaces disagreements early when they are cheap to correct."
tags: [brainstorming, intent, phase, process]
confidence: peer-reviewed
authority: human
last_validated: 2026-04-21
staleness_window: 365
evidence: doc:superpowers
source_attribution: "superpowers@5.0.7"
source_commit: null
position: 1
name: "Understand intent"
description: |
  Restate the user's goal in your own words. Confirm the restatement with the user before proceeding to constraint clarification. If the user corrects the restatement, accept the correction and re-confirm — do not proceed until the restatement matches the user's intent.
parent_playbook_id: PBK-PROC-BRAIN-001
edges: []
---

# Phase 1: Understand intent

Non-retrievable structural node. Surfaces only via `CONTAINS` edge traversal from `PBK-PROC-BRAIN-001`.

## What this phase does

Restate the user's goal in your own words. Confirm the restatement. If corrected, accept correction and re-confirm.

## Why first

Everything downstream (approaches, trade-offs, design) depends on a correct understanding of intent. Getting intent wrong cheap to fix at Phase 1, expensive at Phase 8.

## Advance criterion

User affirms the restatement (e.g., "yes, that's right" or equivalent). Silence or ambiguity is not affirmation — ask again.
