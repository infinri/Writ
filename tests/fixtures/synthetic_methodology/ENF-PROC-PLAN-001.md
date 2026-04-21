---
rule_id: ENF-PROC-PLAN-001
domain: process
severity: high
scope: task
trigger: "When a plan.md artifact is written and contains placeholder content (TBD, TODO, 'similar to N') or fails structural quality gate."
statement: "plan.md artifacts must contain no placeholder content, exact file paths, and complete code blocks. Gate 5 Tier 1 (structural) denies writes with placeholder text in any plan section."
violation: "plan.md contains 'Step 5: implement appropriate error handling, similar to Step 3.' Gate 5 Tier 1 matches 'appropriate' and 'similar to' in the blocklist, denies the write, logs to friction log."
pass_example: "plan.md Step 5: 'In src/api.py line 42, wrap the fetch() call in try/except OrderNotFoundError, log the error with order_id context, re-raise.' Concrete path, concrete change, concrete reasoning. Gate passes."
enforcement: "Gate 5 Tier 1 via validate-exit-plan.sh: lexical match against placeholder blocklist (TBD, TODO, fill in, appropriate, similar to, as needed, placeholder). Gate 5 Tier 2 via Haiku judge rubric on PostToolUse for semantic-level boilerplate."
rationale: "Placeholder plans transfer design decisions to the implementer as interpretation. They are the canonical failure mode of AI-generated planning."
mandatory: true
always_on: false
confidence: peer-reviewed
authority: human
last_validated: 2026-04-21
staleness_window: 365
evidence: doc:superpowers
mechanical_enforcement_path: ".claude/hooks/validate-exit-plan.sh (Tier 1) + writ-quality-judge.sh (Tier 2)"
rationalization_counters:
  - { thought: "Engineer can figure this out.", counter: "Plan is the contract. Figuring out is interpretation, which drifts from user intent." }
  - { thought: "I'll expand TBDs before handing off.", counter: "The plan is handed off when written. TBDs persist unless removed in the same authoring pass." }
red_flag_thoughts:
  - "Similar to above"
  - "TBD"
  - "TODO — fill in"
  - "Add appropriate X"
tags: [enforcement, placeholder, planning, process, quality-gate]
source_attribution: "superpowers@5.0.7"
source_commit: null
body: "Standalone Rationalization nodes attached via ATTACHED_TO edges complement the inline rationalization_counters field per schema proposal."
edges:
  - { target: SKL-PROC-PLAN-001, type: TEACHES }
  - { target: PBK-PROC-PLAN-001, type: TEACHES }
  - { target: ANT-PROC-PLAN-001, type: COUNTERS }
---

# Rule: No placeholder content in plans

Gate 5 Tier 1 structural check. Gate 5 Tier 2 semantic check via Haiku judge.
