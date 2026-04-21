---
skill_id: SKL-PROC-VISUAL-001
node_type: Skill
domain: process
severity: medium
scope: session
trigger: "During brainstorming, when the design has spatial, visual, UI, or layout elements that text alone cannot convey."
statement: "Offer a visual companion artifact (diagram, mockup, sketch) as a separate message, not combined with clarifying questions. The offer is a yes/no question to the user."
rationale: "Combining visual offers with clarifying questions overloads the user's response. Separating them lets the user answer one at a time and produces a cleaner design artifact."
tags: [brainstorming, design, process, visual]
confidence: peer-reviewed
authority: human
last_validated: 2026-04-21
staleness_window: 365
evidence: doc:superpowers
source_attribution: "superpowers@5.0.7"
source_commit: null
edges:
  - { target: SKL-PROC-BRAIN-001, type: DEMONSTRATES }
  - { target: PBK-PROC-BRAIN-001, type: DISPATCHES }
---

# Skill: Offer visual companion

## When to offer

Design contains spatial, layout, UI, state-machine, or data-flow elements. Text alone would be ambiguous or verbose.

## How to offer

Separate message. Simple binary: "This design has visual elements. Would a diagram help?" Do not bundle with clarifying questions.

## What counts

ASCII diagrams, structured tables, simple sketches embedded in response, or deferrable "I'll produce a mermaid diagram if useful."
