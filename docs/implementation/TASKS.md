# Writ V2 engineering task tracker

Statuses: NOT STARTED, IN PROGRESS, BLOCKED, IN REVIEW, DONE. A task is DONE only when its acceptance criteria are met with recorded evidence ([TEST-EVIDENCE](TEST-EVIDENCE.md)). Priorities: P0 blocks the milestone gate, P1 required for M0 exit, P2 useful but deferrable.

Source checklists: [M0-START-HERE](M0-START-HERE.md), [master plan](MASTER-IMPLEMENTATION-PLAN.md), [invariants](../security/invariants.md).

## M0-00 Continuity and harness

| ID | Task | Pri | Deps | Status | Acceptance criteria | Refs | Tests | Evidence |
|---|---|---|---|---|---|---|---|---|
| M0-00a | Persistent tracking docs and project CLAUDE.md | P0 | none | DONE | WORK-STATE, TASKS, DECISION-LOG, TEST-EVIDENCE, SESSION-HANDOFF exist; CLAUDE.md links them and states recovery protocol | directive section 4 | docs check | see git log for "M0-00" |
| M0-00b | Isolated M0 Python harness (`m0/`) with its own pytest config and venv | P0 | none | DONE | `m0/.venv/bin/python -m pytest` runs without V1 conftest or Neo4j | D-001, D-005 | suite runs | TEST-EVIDENCE E-001 |
| M0-00c | CI job running M0 tests and audit drift check | P1 | M0-00b | NOT STARTED | v2 workflow runs `pytest` in `m0/` and `python -m writ_m0.audit check` | testing doc CI target | CI green | |

## M0-01 V1 baseline audit (work package A)

| ID | Task | Pri | Deps | Status | Acceptance criteria | Refs | Tests | Evidence |
|---|---|---|---|---|---|---|---|---|
| M0-01a | Pin main V1 SHA; checksummed read-only baseline manifest | P0 | M0-00b | DONE | Manifest records commit, tree, file counts, sha256 of hooks.json, corpus, common.sh and all hook scripts; generated from git objects | M0 checklist | `test_committed_artifacts_are_current` | `docs/audit/m0/v1-baseline-manifest.json`, E-001 |
| M0-01b | 52 hook registrations: normalized capabilities, coverage class, static failure posture | P0 | M0-01a | DONE | CSV with 52 rows, line-referenced static signals, mechanical findings | A-02, A-04, A-05, INV-13, INV-16 | `TestHookNormalization`, count test | `docs/audit/m0/hook-registrations.csv`, E-001 |
| M0-01c | 288-rule automated first-pass disposition | P0 | M0-01a | DONE | CSV with 288 rows; every row `review_status=automated_first_pass`; precedence documented and unit tested | A-07, INV-20 | `TestRuleDisposition` | `docs/audit/m0/rule-disposition.csv`, E-001 |
| M0-01d | Semantic review of 288 rules (confirm/correct disposition, conflict analysis) | P1 | M0-01c | NOT STARTED | Each row moves to `review_status=reviewed` with reviewer and reason; conflicts listed | M0 checklist "manually inspect all 288" | review diff | |
| M0-01e | Hook failure-posture verification in an isolated host | P1 | M0-01b | NOT STARTED | Each preventive_candidate and workflow_gate row has a runtime-observed outcome for daemon down, crash and malformed input, run in a disposable environment | threat model T-13, T-15; INV-15 | isolated host harness | |
| M0-01f | Golden fixtures mined from V1 tests: retrieval golden queries, approval replay, cross-project isolation, effect gaps | P0 | M0-01a | NOT STARTED | Language-neutral JSON fixtures with source-test lineage and sha256; documented oracle per case | A-09, A-10, INV-06, INV-10 | fixture schema validation | |
| M0-01g | V1 test and benchmark inventory | P1 | M0-01a | DONE | CSV of all test/benchmark files with function counts, markers and topic tags | A-10 | count in summary | `docs/audit/m0/v1-test-inventory.csv` |

## M0-02 Cross-domain contracts (work packages B, C)

| ID | Task | Pri | Deps | Status | Acceptance criteria | Refs | Tests | Evidence |
|---|---|---|---|---|---|---|---|---|
| M0-02a | Contract test harness: every schema checked, every fixture validated by pytest | P0 | M0-00b | NOT STARTED | Schema meta-validation plus positive/negative fixture tests; format checking enabled | contracts README | `test_contracts.py` | |
| M0-02b | Canonical JSON digest for action/operation digests | P0 | M0-02a | NOT STARTED | Deterministic digest independent of key order and whitespace; property tests | INV-04, INV-14 | hypothesis | |
| M0-02c | Artifact, Evidence and EvaluationRecord fixtures for code review, research, document review | P0 | M0-02a | NOT STARTED | All three tasks have artifacts, evidence and evaluation records validating against identical schemas; no repo/file field required by research or review | INV-24, P-15 | contract tests | |
| M0-02d | Negative tasks: fabricated citation, insufficient evidence, unapproved tool effect | P0 | M0-02c | NOT STARTED | Evaluator reports fail/incomplete for each; broker refuses unapproved effect | INV-25, INV-01 | negative tests | |
| M0-02e | Two stub ModelAdapters with dissimilar features and one simulated external host | P0 | M0-02a | NOT STARTED | Streaming+tools stub and non-streaming stub emit schema-valid ModelEvents; host runs task with no ModelAdapter | INV-21, INV-22, P-13, P-14 | provider tests | |
| M0-02f | Verdict independence across model and pack | P0 | M0-02e, M0-03b | NOT STARTED | Same ActionRequest yields identical Decision regardless of provider, pack or host | INV-04, P-01 | differential test | |

## M0-03 Security boundary and pack registry (work package D)

| ID | Task | Pri | Deps | Status | Acceptance criteria | Refs | Tests | Evidence |
|---|---|---|---|---|---|---|---|---|
| M0-03a | Draft contracts: principal proof, broker permit, approval grant | P0 | M0-02a | NOT STARTED | Schemas plus fixtures; grant binds tenant, principal, action, target, digest, policy version, nonce, expiry | INV-05, INV-06 | contract tests | |
| M0-03b | Reference decision oracle and broker stub (non-production, Python) | P0 | M0-03a | NOT STARTED | Deny precedence, single-use approval consume, idempotency, unknown capability not covered | INV-01..03, INV-06, INV-07, INV-09, INV-13 | unit, property, concurrency | |
| M0-03c | Host tool normalization independent of host tool names | P1 | M0-01b | NOT STARTED | Two simulated hosts with different tool names normalize to the same ActionRequest | P-01, INV-13 | differential test | |
| M0-03d | Pack registry verification: schema, trust policy, model features, semver, evaluator rubric | P0 | M0-02a | NOT STARTED | Unsigned/untrusted/unsupported packs rejected with explicit reason | P-16, INV-23 | registry tests | |
| M0-03e | Prove packs cannot authorize requested capabilities or change tenant scope | P0 | M0-03b, M0-03d | NOT STARTED | Pack requesting forbidden tool still denied; pack cannot alter tenant | INV-23 | negative tests | |
| M0-03f | Enumerate unmediated tools for the Claude Code host | P1 | M0-01b | NOT STARTED | List of host tools/paths Writ cannot mediate, with basis | threat model enforcement profiles | doc review | |

## M0-04 M1 preparation (work package E)

| ID | Task | Pri | Deps | Status | Acceptance criteria | Refs | Tests | Evidence |
|---|---|---|---|---|---|---|---|---|
| M0-04a | Define Rust/Go parity suite and workload distribution | P0 | M0-03b | NOT STARTED | Spec lists fixture sets, workloads A-H mapping, harness interface, recording format | ADR-0002 | n/a | |
| M0-04b | Record benchmark environment template (hardware, toolchains) with no invented numbers | P1 | M0-04a | NOT STARTED | Template plus this machine's hardware recorded; Rust/Go toolchains noted as absent if absent | testing doc | n/a | |
| M0-04c | CI checks for schemas, examples, links and fixture suite | P1 | M0-00c | NOT STARTED | Workflow runs docs check, M0 tests and audit check | M0 first PR DoD | CI | |
