---
phase_id: PHA-BRAIN-005
node_type: Phase
domain: process
scope: session
trigger: "Fifth phase of PBK-PROC-BRAIN-001. Fires when the design has spatial/visual/layout elements after trade-offs are named."
statement: "If the design has visual elements (UI, layout, state machines, data flows), offer a visual companion artifact in a separate message. Do not bundle the offer with clarifying questions."
rationale: "Visual elements are ambiguous in text. Offering a companion diagram gives the user the option to request clarity at low cost."
tags: [brainstorming, phase, process, visual]
confidence: peer-reviewed
authority: human
last_validated: 2026-04-21
staleness_window: 365
evidence: doc:superpowers
source_attribution: "superpowers@5.0.7"
source_commit: null
position: 5
name: "Offer visual companion"
description: "If the design has visual, spatial, or layout elements, offer a diagram/sketch as a separate message. Skip this phase if the design is purely logical/textual."
parent_playbook_id: PBK-PROC-BRAIN-001
edges: []
---

# Phase 5: Offer visual companion

Non-retrievable. Optional — skip if design has no visual elements.
