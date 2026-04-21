---
rule_id: META-AUTH-002
domain: meta-authoring
severity: high
scope: task
trigger: "When the agent authors a new Writ Skill, Playbook, or Technique and is about to mark it ready for use without running a pressure test against the rule's target behavior."
statement: "Skills must pass a pressure test before deployment: apply the baseline scenario, confirm RED (agent violates), write or refine the skill, re-run, confirm GREEN (agent complies). No skill ships without RED-GREEN-REFACTOR applied to itself."
violation: "Agent authors a new skill SKL-PROC-FOO-001 and commits without running pressure-test. Advisory warning surfaced during writ add; friction-logged."
pass_example: "Agent authors draft skill, runs writ test-pressure --scenario PSC-FOO-001, confirms agent violates the rule (RED), refines skill text, re-runs, confirms agent now complies (GREEN). Then commits."
enforcement: "writ add advisory check: warns if a new SKL-/PBK-/TEC-/ENF- node is added without a linked PressureScenario. Does not block."
rationale: "Untested skills deploy with unknown effectiveness. The RED-GREEN-REFACTOR discipline that applies to code also applies to methodology content."
mandatory: false
always_on: false
confidence: peer-reviewed
authority: human
last_validated: 2026-04-21
staleness_window: 365
evidence: doc:superpowers
mechanical_enforcement_path: "writ/authoring.py: warn on add/edit if node lacks linked PressureScenario."
rationalization_counters:
  - { thought: "Testing a skill is overkill.", counter: "Untested skill = unvalidated hypothesis about agent behavior." }
red_flag_thoughts:
  - "Skill is obviously correct"
  - "Testing is overkill"
tags: [meta, pressure-testing, skill-authoring, tdd-for-docs]
source_attribution: "superpowers@5.0.7"
source_commit: null
body: "Applies TDD for docs: RED (baseline scenario), GREEN (skill addresses it), REFACTOR (close loopholes). See PBK-PROC-TDD-001 for the underlying discipline."
edges:
  - { target: META-AUTH-001, type: PRECEDES }
  - { target: PBK-PROC-TDD-001, type: TEACHES }
---

# Rule: Pressure-test skills before deployment

Advisory (warn on add). TDD-for-docs discipline.
