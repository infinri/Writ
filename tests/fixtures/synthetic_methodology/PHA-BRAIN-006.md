---
phase_id: PHA-BRAIN-006
node_type: Phase
domain: process
scope: session
trigger: "Sixth phase of PBK-PROC-BRAIN-001. Fires after visual companion is handled (offered and responded to, or skipped)."
statement: "Ask clarifying questions. Batched, not one at a time. Only after the visual companion phase has resolved, so questions are not bundled with the visual offer."
rationale: "Batching clarifications lets the user answer in one pass. Unbatched one-at-a-time Q&A drags the brainstorm out and exhausts the user."
tags: [brainstorming, clarification, phase, process]
confidence: peer-reviewed
authority: human
last_validated: 2026-04-21
staleness_window: 365
evidence: doc:superpowers
source_attribution: "superpowers@5.0.7"
source_commit: null
position: 6
name: "Ask clarifying questions"
description: "Batch all clarifying questions about scope, edge cases, constraints, or trade-off preferences into a single message. If no questions: state so explicitly — silence is not the same as confirmed."
parent_playbook_id: PBK-PROC-BRAIN-001
edges: []
---

# Phase 6: Ask clarifying questions

Non-retrievable. Bundle-only.
