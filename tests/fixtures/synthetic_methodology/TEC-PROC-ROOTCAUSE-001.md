---
technique_id: TEC-PROC-ROOTCAUSE-001
node_type: Technique
domain: process
severity: medium
scope: task
trigger: "During systematic debugging Phase 1 (investigate), when multi-component data flow needs to be traced backward from the failure point."
statement: "Trace the call stack backward from the failure point. At each component boundary, add diagnostic instrumentation. Note values at each step. Identify the exact boundary where expected and actual diverge."
rationale: "Forward tracing (input-to-output) misses divergences that appear mid-stack. Backward tracing pinpoints the component boundary where the bug is introduced."
tags: [backward-tracing, call-stack, debugging, process, technique]
confidence: peer-reviewed
authority: human
last_validated: 2026-04-21
staleness_window: 365
evidence: doc:superpowers
source_attribution: "superpowers@5.0.7"
source_commit: null
edges:
  - { target: PBK-PROC-DEBUG-001, type: DEMONSTRATES }
---

# Technique: Backward root-cause tracing

## Procedure

1. Start at the failure point (exception location, wrong output).
2. Read the failing value. Compare to expected.
3. Traverse one level up the stack. Instrument entry values.
4. Compare expected vs actual at that boundary. Divergence? That's the locus.
5. If still matching: continue up one level.

## When to use

Multi-component systems where the failure manifests far from the cause. Single-module code usually doesn't need this technique — forward reading suffices.
