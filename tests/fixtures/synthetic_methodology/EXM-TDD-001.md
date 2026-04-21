---
example_id: EXM-TDD-001
node_type: WorkedExample
domain: process
scope: task
trigger: "User requests an example of applying PBK-PROC-TDD-001 to a concrete bug fix."
statement: "Concrete walk-through of RED-GREEN-REFACTOR applied to the 'empty request body' bug. Shows the exact test written, the failure output, the minimal fix, the passing output."
rationale: "Worked examples anchor abstract methodology in specific commands and outputs. The TDD skill is easier to apply after seeing a full cycle executed."
tags: [example, process, tdd, worked]
confidence: peer-reviewed
authority: human
last_validated: 2026-04-21
staleness_window: 365
evidence: doc:superpowers
source_attribution: "superpowers@5.0.7"
source_commit: null
title: "Applying TDD to an empty-body bug"
before: "src/api.py::fetch() crashes with KeyError when request.body is empty. User asks for a fix."
applied_skill: PBK-PROC-TDD-001
result: "Test written first (tests/test_api.py::test_fetch_handles_empty_body). Ran pytest — failed with KeyError at line 42 (expected). Added null check at line 42. Ran pytest — passed. Committed both files together."
linked_skill: PBK-PROC-TDD-001
edges:
  - { target: PBK-PROC-TDD-001, type: DEMONSTRATES }
---

# Worked example: TDD applied to empty-body bug

Non-retrievable via standard pipeline. Surfaces when a user explicitly asks for an example of TDD application, via explicit lookup.
