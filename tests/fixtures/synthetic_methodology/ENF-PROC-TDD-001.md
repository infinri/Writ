---
rule_id: ENF-PROC-TDD-001
domain: process
severity: critical
scope: task
trigger: "In Work mode: when a Write or Edit to a production source file is attempted without a corresponding test file containing assertions."
statement: "Production code requires a failing test before implementation. Gate denies Write/Edit to src/** paths without corresponding tests/** file containing lexical test markers."
violation: "Agent in Work mode attempts Write(src/api.py, 'def fetch(url): ...') without tests/test_api.py existing or containing assertions. Gate denies. Friction log records 'gate_denied: ENF-PROC-TDD-001'."
pass_example: "Agent writes tests/test_api.py with test_fetch_returns_json (containing assert statement), runs pytest (fails as expected — function doesn't exist), then Write(src/api.py, 'def fetch(url): ...'). Gate permits because test exists."
enforcement: "validate-test-file.sh: on PreToolUse Write matching src/**/*.{py,js,ts,php}, find corresponding test file, check for lexical assertion markers (assert|expect|should|test_). Deny if missing."
rationale: "Test-first discipline is what the skill teaches. Mechanical enforcement makes the discipline impossible to rationalize around."
mandatory: true
always_on: false
confidence: battle-tested
authority: human
last_validated: 2026-04-21
staleness_window: 365
evidence: doc:superpowers
mechanical_enforcement_path: ".claude/hooks/validate-test-file.sh"
rationalization_counters:
  - { thought: "I'll write the test after confirming the fix works.", counter: "After-confirm is test-after, see ANT-PROC-TDD-005. Delete code, write test first." }
  - { thought: "I already manually tested it.", counter: "Manual tests don't regress-test. A committed test does." }
red_flag_thoughts:
  - "Just this once"
  - "Tests slow me down"
  - "Manual test is faster"
  - "Already spent X hours"
tags: [enforcement, process, tdd, test-first, work-mode]
source_attribution: "superpowers@5.0.7"
source_commit: null
body: "Prototype mode (session.mode == 'prototype', manual trigger per Section 0.4 decision 2) bypasses this gate. All other modes deny."
edges:
  - { target: PBK-PROC-TDD-001, type: TEACHES }
  - { target: ANT-PROC-TDD-005, type: COUNTERS }
  - { target: ANT-PROC-TDD-001, type: COUNTERS }
---

# Rule: Test first (mandatory in Work mode)

Enforcement: `validate-test-file.sh`. Bypass: `session.mode == "prototype"` only.
