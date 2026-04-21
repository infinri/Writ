---
rule_id: META-AUTH-001
domain: meta-authoring
severity: high
scope: task
trigger: "When the agent authors a new Writ Skill, Playbook, or Technique node, and is about to populate the trigger or description field."
statement: "The trigger field of a node must describe WHEN the node should activate, not WHAT the node does. Description-as-workflow-summary causes Claude to follow descriptions instead of reading skill bodies, fragmenting methodology retrieval at scale."
violation: "New skill authored with trigger: 'This skill performs systematic debugging by gathering evidence, forming hypotheses, testing them, and implementing fixes in four phases.' Field describes what; agent sees the summary at retrieval and skips the body. Authoring-gate warns on action verbs in trigger text."
pass_example: "New skill authored with trigger: 'When a bug is reported, a test fails, an error is observed, or the same fix attempt has failed three times.' Field describes when; agent retrieves the node and reads the body for the how. Authoring-gate passes."
enforcement: "writ add and writ edit lint the trigger field, warn on action verbs (does, performs, executes) that indicate workflow-summary rather than triggering-condition content."
rationale: "At scale (>30 skills), agent-side selection of which skill to use becomes the bottleneck. If descriptions summarize workflow, the agent has enough information to act without reading the body — and it acts on the summary, which is incomplete. Triggering-conditions descriptions force the agent to the body, where the actual methodology lives."
mandatory: false
always_on: false
confidence: peer-reviewed
authority: human
last_validated: 2026-04-21
staleness_window: 365
evidence: doc:superpowers
mechanical_enforcement_path: "writ/authoring.py: lint on add/edit warns if trigger contains action verbs rather than trigger conditions."
rationalization_counters:
  - { thought: "A workflow summary is clearer.", counter: "Clearer to you as author; fatal to retrieval at scale. Agents see the summary, skip the body, miss the nuance." }
  - { thought: "Both WHEN and WHAT in one field is fine.", counter: "The field is a trigger predicate, not documentation. Keep the WHAT for the body." }
red_flag_thoughts:
  - "This skill does the following..."
  - "This is a skill for..."
  - "Performs the following steps..."
tags: [description-field, meta, skill-authoring, triggering-conditions]
source_attribution: "superpowers@5.0.7"
source_commit: null
body: "Documented in skills/writing-skills/SKILL.md lines 150-157 of the Superpowers pinned commit. The anti-pattern was discovered through retrieval-quality testing at scale: summary-style descriptions cut retrieval effectiveness measurably because agents act on descriptions without traversing to bodies."
edges:
  - { target: META-AUTH-002, type: PRECEDES }
---

# Rule: Description field is a trigger predicate, not a workflow summary

## Statement

A skill/playbook/technique's `trigger` field answers WHEN the node applies, not WHAT the node does. The WHAT lives in the body (body is indexed at 0.5× weight in Stage 2 BM25 per plan Section 3.2; the trigger is the primary retrieval surface).

## Violation (bad)

```yaml
---
skill_id: SKL-PROC-DEBUG-001
trigger: "This skill performs systematic debugging by gathering evidence, forming hypotheses, testing them, and implementing fixes in four phases."
---
```

Describes what the skill does. Claude sees it, thinks "got it," and skips the body.

## Pass (good)

```yaml
---
skill_id: SKL-PROC-DEBUG-001
trigger: "When a bug is reported, a test fails, an error is observed, or the same fix attempt has failed three times."
---
```

Names the conditions under which the skill should fire. Claude retrieves and reads the body for the how.
