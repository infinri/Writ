---
rule_id: ENF-PROC-DEBUG-001
domain: process
severity: high
scope: task
trigger: "When the agent is actively debugging (session.mode == 'debug') and proposes a fix without documented root-cause evidence."
statement: "Advisory rule: fixes during debug mode should cite root-cause evidence in the same response. No mechanical enforcement path — advisory because lexical detection of 'evidence' is unreliable."
violation: "Agent in debug mode says 'let me try changing X' without having explained why X is the cause. Advisory warning surfaced to agent in response bundle."
pass_example: "Agent in debug mode says 'The failure appears at line 42 when request.body is empty. Fix: validate body before accessing. Evidence: traceback shows KeyError at line 42.' Advisory passes."
enforcement: "Advisory only — surfaced as part of debug-mode always-on bundle. No deny condition. Friction-logged when agent claims success on fix without evidence."
rationale: "Symptom-patching is the canonical debug-mode failure. Forcing evidence-cite discipline reduces it, but no reliable lexical detector exists for 'is this evidence?' so the rule stays advisory."
mandatory: false
always_on: true
confidence: peer-reviewed
authority: human
last_validated: 2026-04-21
staleness_window: 365
evidence: doc:superpowers
mechanical_enforcement_path: null
rationalization_counters:
  - { thought: "Quick fix now, investigate later.", counter: "Fix without cause is patching symptoms. Later never comes." }
  - { thought: "Emergency, no time.", counter: "Emergency is when process saves you. Fix without cause and you'll debug the fix in an hour." }
red_flag_thoughts:
  - "Just try X"
  - "Quick fix"
  - "Probably it"
tags: [advisory, always-on, debug-mode, debugging, process]
source_attribution: "superpowers@5.0.7"
source_commit: null
body: "Always-on in debug mode only. Empty mechanical_enforcement_path per plan Section 2.1 advisory-vs-mandatory distinction."
edges:
  - { target: PBK-PROC-DEBUG-001, type: TEACHES }
  - { target: ANT-PROC-DEBUG-001, type: COUNTERS }
---

# Rule: Cite root-cause evidence for debug-mode fixes

Advisory (no mechanical path). Always-on in debug mode.
