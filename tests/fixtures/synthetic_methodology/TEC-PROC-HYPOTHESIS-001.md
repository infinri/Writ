---
technique_id: TEC-PROC-HYPOTHESIS-001
node_type: Technique
domain: process
severity: medium
scope: task
trigger: "During systematic debugging Phase 3, when evidence is gathered and a testable hypothesis about root cause must be formed."
statement: "Form hypothesis as a statement of the form 'IF cause X, THEN we would observe evidence Y.' Write a failing test that isolates Y. Test the hypothesis before implementing fix."
rationale: "Untested hypotheses produce scattershot fixes. The IF-THEN structure forces the hypothesis to be falsifiable, which catches wrong hypotheses before a wrong fix is written."
tags: [debugging, falsifiable, hypothesis, process, technique]
confidence: peer-reviewed
authority: human
last_validated: 2026-04-21
staleness_window: 365
evidence: doc:superpowers
source_attribution: "superpowers@5.0.7"
source_commit: null
edges:
  - { target: PBK-PROC-DEBUG-001, type: DEMONSTRATES }
  - { target: PBK-PROC-TDD-001, type: DEMONSTRATES }
---

# Technique: Hypothesis testing in debugging

## Structure

`IF <cause>, THEN <observable evidence>`

Example: IF the race condition is in the totals collector, THEN running two concurrent cart updates should produce a split-discount total. The failing test encodes exactly that.

## Integration with TDD

The failing test that isolates the hypothesis is the same test that will verify the fix. One artifact, two uses: hypothesis test + regression test.
