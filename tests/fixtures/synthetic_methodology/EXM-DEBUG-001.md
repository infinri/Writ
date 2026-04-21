---
example_id: EXM-DEBUG-001
node_type: WorkedExample
domain: process
scope: task
trigger: "User requests an example of applying PBK-PROC-DEBUG-001 four-phase process to a multi-component bug."
statement: "Walk-through of backward root-cause tracing on a totals-collector race condition that manifests as intermittent wrong discount totals."
rationale: "Multi-component bugs are the hardest to debug; a worked example shows phase-by-phase evidence gathering, hypothesis, test-first fix."
tags: [debugging, example, multi-component, process, worked]
confidence: peer-reviewed
authority: human
last_validated: 2026-04-21
staleness_window: 365
evidence: doc:superpowers
source_attribution: "superpowers@5.0.7"
source_commit: null
title: "Debugging a totals-collector race"
before: "Intermittent: about 1 in 50 cart checkouts produces a discount that's double the expected value. Not reproducible locally under single-user load."
applied_skill: PBK-PROC-DEBUG-001
result: "Phase 1: gathered evidence (MySQL binlog showed duplicate discount rows). Phase 2: pattern analysis (all failures had concurrent cart updates in same second). Phase 3: hypothesis (race in totals collector's totals table write). Phase 4: wrote failing test simulating concurrent update, minimal fix added SELECT FOR UPDATE, test passes."
linked_skill: PBK-PROC-DEBUG-001
edges:
  - { target: PBK-PROC-DEBUG-001, type: DEMONSTRATES }
  - { target: TEC-PROC-ROOTCAUSE-001, type: DEMONSTRATES }
---

# Worked example: Totals-collector race condition

Non-retrievable via standard pipeline. Explicit-lookup.
