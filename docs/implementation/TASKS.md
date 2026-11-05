# Writ V2 engineering task tracker

Statuses: NOT STARTED, IN PROGRESS, BLOCKED, IN REVIEW, DONE. A task is DONE only when its acceptance criteria are met with recorded evidence ([TEST-EVIDENCE](TEST-EVIDENCE.md)). Priorities: P0 blocks the milestone gate, P1 required for M0 exit, P2 useful but deferrable.

Source checklists: [M0-START-HERE](M0-START-HERE.md), [master plan](MASTER-IMPLEMENTATION-PLAN.md), [invariants](../security/invariants.md).

## M0-00 Continuity and harness

| ID | Task | Pri | Deps | Status | Acceptance criteria | Refs | Tests | Evidence |
|---|---|---|---|---|---|---|---|---|
| M0-00a | Persistent tracking docs and project CLAUDE.md | P0 | none | DONE | WORK-STATE, TASKS, DECISION-LOG, TEST-EVIDENCE, SESSION-HANDOFF exist; CLAUDE.md links them and states recovery protocol | directive section 4 | docs check | see git log for "M0-00" |
| M0-00b | Isolated M0 Python harness (`m0/`) with its own pytest config and venv | P0 | none | DONE | `m0/.venv/bin/python -m pytest` runs without V1 conftest or Neo4j | D-001, D-005 | suite runs | TEST-EVIDENCE E-001 |
| M0-00c | CI job running M0 tests and audit drift check | P1 | M0-00b | DONE | v2 workflow runs `pytest` in `m0/` and `python -m writ_m0.audit check` | testing doc CI target | CI green | CI run 42094649411: m0-harness 201 passed, audit current (E-003) |

## M0-01 V1 baseline audit (work package A)

| ID | Task | Pri | Deps | Status | Acceptance criteria | Refs | Tests | Evidence |
|---|---|---|---|---|---|---|---|---|
| M0-01a | Pin main V1 SHA; checksummed read-only baseline manifest | P0 | M0-00b | DONE | Manifest records commit, tree, file counts, sha256 of hooks.json, corpus, common.sh and all hook scripts; generated from git objects | M0 checklist | `test_committed_artifacts_are_current` | `docs/audit/m0/v1-baseline-manifest.json`, E-001 |
| M0-01b | 52 hook registrations: normalized capabilities, coverage class, static failure posture | P0 | M0-01a | DONE | CSV with 52 rows, line-referenced static signals, mechanical findings | A-02, A-04, A-05, INV-13, INV-16 | `TestHookNormalization`, count test | `docs/audit/m0/hook-registrations.csv`, E-001 |
| M0-01c | 288-rule automated first-pass disposition | P0 | M0-01a | DONE | CSV with 288 rows; every row `review_status=automated_first_pass`; precedence documented and unit tested | A-07, INV-20 | `TestRuleDisposition` | `docs/audit/m0/rule-disposition.csv`, E-001 |
| M0-01d | Semantic review of 288 rules (confirm/correct disposition, conflict analysis) | P1 | M0-01c | IN PROGRESS | Each row moves to `review_status=reviewed` with reviewer and reason; conflicts listed | M0 checklist "manually inspect all 288" | review diff | review prepared in `docs/audit/m0/rule-review/` (15 batches, blank reviewer columns); 0 of 288 reviewed; awaiting human reviewer |
| M0-01e | Hook failure-posture verification in an isolated host | P1 | M0-01b | IN PROGRESS | Each preventive_candidate and workflow_gate row has a runtime-observed outcome for daemon down, crash and malformed input, run in a disposable environment | threat model T-13, T-15; INV-15 | isolated host harness | procedure `docs/audit/m0/LIVE-HOST-VERIFICATION.md`; no trial run; needs disposable host and operator |
| M0-01f | Golden fixtures mined from V1 tests: retrieval golden queries, approval replay, cross-project isolation, effect gaps | P0 | M0-01a | DONE | Language-neutral JSON fixtures with source-test lineage and sha256; documented oracle per case | A-09, A-10, INV-06, INV-10 | fixture schema validation | retrieval golden export (`v1-golden-retrieval.json`); 19 approval/fail-open/isolation lessons mapped in `docs/audit/m0/V1-REGRESSION-PORT.md`, 21 new tests in `m0/tests/test_v1_regressions.py`; GRANT_MALFORMED defect fixed (D-009); gaps listed in that doc; E-004 |
| M0-01g | V1 test and benchmark inventory | P1 | M0-01a | DONE | CSV of all test/benchmark files with function counts, markers and topic tags | A-10 | count in summary | `docs/audit/m0/v1-test-inventory.csv` |

## M0-02 Cross-domain contracts (work packages B, C)

| ID | Task | Pri | Deps | Status | Acceptance criteria | Refs | Tests | Evidence |
|---|---|---|---|---|---|---|---|---|
| M0-02a | Contract test harness: every schema checked, every fixture validated by pytest | P0 | M0-00b | DONE | Schema meta-validation plus positive/negative fixture tests; format checking enabled | contracts README | `test_contracts.py` | `m0/tests/test_contracts.py`, E-002 |
| M0-02b | Canonical JSON digest for action/operation digests | P0 | M0-02a | DONE | Deterministic digest independent of key order and whitespace; property tests | INV-04, INV-14 | hypothesis | `m0/writ_m0/canonical.py`, D-006, E-002 |
| M0-02c | Artifact, Evidence and EvaluationRecord fixtures for code review, research, document review | P0 | M0-02a | DONE | All three tasks have artifacts, evidence and evaluation records validating against identical schemas; no repo/file field required by research or review | INV-24, P-15 | contract tests | `docs/contracts/fixtures/domains/`, `test_evaluation.py`, E-002 |
| M0-02d | Negative tasks: fabricated citation, insufficient evidence, unapproved tool effect | P0 | M0-02c | DONE | Evaluator reports fail/incomplete for each; broker refuses unapproved effect | INV-25, INV-01 | negative tests | fabricated citation and insufficient evidence in `test_evaluation.py`; unapproved effect in `test_broker.py::test_require_approval_without_grant_refused` and runner test; E-002 |
| M0-02e | Two stub ModelAdapters with dissimilar features and one simulated external host | P0 | M0-02a | DONE | Streaming+tools stub and non-streaming stub emit schema-valid ModelEvents; host runs task with no ModelAdapter | INV-21, INV-22, P-13, P-14 | provider tests | `providers.py`, `runner.py`, `hosts.py`; `test_independence.py` E-002 |
| M0-02f | Verdict independence across model and pack | P0 | M0-02e, M0-03b | DONE | Same ActionRequest yields identical Decision regardless of provider, pack or host | INV-04, P-01 | differential test | `test_independence.py::TestManagedRunner`, `TestHostNormalization`; E-002 |

## M0-03 Security boundary and pack registry (work package D)

| ID | Task | Pri | Deps | Status | Acceptance criteria | Refs | Tests | Evidence |
|---|---|---|---|---|---|---|---|---|
| M0-03a | Draft contracts: principal proof, broker permit, approval grant | P0 | M0-02a | DONE | Schemas plus fixtures; grant binds tenant, principal, action, target, digest, policy version, nonce, expiry | INV-05, INV-06 | contract tests | 4 schemas in `docs/contracts/`, E-002 |
| M0-03b | Reference decision oracle and broker stub (non-production, Python) | P0 | M0-03a | DONE | Deny precedence, single-use approval consume, idempotency, unknown capability not covered | INV-01..03, INV-06, INV-07, INV-09, INV-13 | unit, property, concurrency | `decision.py`, `broker.py`, 24 golden cases, D-008, E-002 |
| M0-03c | Host tool normalization independent of host tool names | P1 | M0-01b | DONE | Two simulated hosts with different tool names normalize to the same ActionRequest | P-01, INV-13 | differential test | `hosts.py` two extractors; parity test E-002 |
| M0-03d | Pack registry verification: schema, trust policy, model features, semver, evaluator rubric | P0 | M0-02a | DONE | Unsigned/untrusted/unsupported packs rejected with explicit reason | P-16, INV-23 | registry tests | `packs.py`; `TestPacks` E-002 |
| M0-03e | Prove packs cannot authorize requested capabilities or change tenant scope | P0 | M0-03b, M0-03d | DONE | Pack requesting forbidden tool still denied; pack cannot alter tenant | INV-23 | negative tests | `TestPacks::test_verified_pack_requesting_egress_is_still_denied`, scope tests E-002 |
| M0-03f | Enumerate unmediated tools for the Claude Code host | P1 | M0-01b | DONE | List of host tools/paths Writ cannot mediate, with basis | threat model enforcement profiles | doc review | `docs/audit/m0/UNMEDIATED-TOOLS.md` (static; runtime confirmation under M0-01e) |

## M0-04 M1 preparation (work package E)

| ID | Task | Pri | Deps | Status | Acceptance criteria | Refs | Tests | Evidence |
|---|---|---|---|---|---|---|---|---|
| M0-04a | Define Rust/Go parity suite and workload distribution | P0 | M0-03b | DONE | Spec lists fixture sets, workloads A-H mapping, harness interface, recording format | ADR-0002 | n/a | `docs/implementation/M1-PARITY-SUITE.md` (specification only) |
| M0-04b | Record benchmark environment template (hardware, toolchains) with no invented numbers | P1 | M0-04a | DONE | Template plus this machine's hardware recorded; Rust/Go toolchains noted as absent if absent | testing doc | n/a | `docs/implementation/BENCHMARK-ENVIRONMENT.md`; Rust and Go toolchains absent |
| M0-04c | CI checks for schemas, examples, links and fixture suite | P1 | M0-00c | DONE | Workflow runs docs check, M0 tests and audit check | M0 first PR DoD | CI | same CI run as M0-00c (E-003) |

## M0 exit and follow-ups

| ID | Task | Pri | Deps | Status | Acceptance criteria | Refs | Tests | Evidence |
|---|---|---|---|---|---|---|---|---|
| M0-05a | Independent human review of golden decision expectations and broker scenarios | P0 | M0-03b | IN REVIEW | A reviewer other than the author signs off each high-risk case in `m0-golden.json` or files corrections | testing doc "independent reviewers own oracle decisions" | n/a | worksheet `docs/audit/m0/GOLDEN-HUMAN-REVIEW-WORKSHEET.md`; all 26 cases PENDING INDEPENDENT REVIEW; AI-assisted reviews 2-4 technically AGREE; named human verdicts required |
| M0-05b | Push branch and confirm both v2 CI jobs green | P0 | M0-00c | DONE | GitHub Actions run URL recorded in TEST-EVIDENCE | M0 first PR DoD | CI | pushed `487b95d`; run https://github.com/infinri/Writ/actions/runs/42094649411 both jobs success (E-003) |
| M0-05c | M0 go/no-go record | P0 | all P0 above | IN PROGRESS | WORK-STATE lists every M0 checklist item with evidence or explicit gap; user decision recorded | master plan section 10 | n/a | checklist `docs/implementation/M0-GO-NO-GO.md`; current recommendation NO-GO; owner decision and risk table open |

## M0 security review priorities (from the user and the independent review, 2026-11-05; see docs/audit/m0/INDEPENDENT-REVIEW-M0.md)

M0 is **not complete** until these are reviewed and resolved or explicitly risk-accepted, together with M0-01d, M0-01e, M0-05a and M0-05c. No code changes for these until the independent reviewer's findings are in.

| ID | Issue | Pri | Deps | Status | Acceptance criteria | Refs | Tests | Evidence |
|---|---|---|---|---|---|---|---|---|
| SR-01 | Host identity issuer authenticity and signature verification: kernel trusts adapter-set `verified_by`; proofs are verified only in the host adapter | P0 | M0-05a | DONE (harness) | Reviewer decides where T1 is enforced; if the kernel must verify, add PrincipalProof verification plus forged-issuer negative tests | INV-05, T1, golden case 10 | negative tests | fixed in `94d3701` (D-010, sealed ingress); awaiting review; E-006; accepted for reference harness by review 2 (production key provisioning/rotation/registration open) |
| SR-02 | Approval grants do not bind `requested_capabilities` | P0 | M0-05a | DONE (harness) | Grant and approval request id bind the full capability set, or reviewer records why not; swap test added | INV-06, INV-23, golden case 20 | swap test | fixed in `f8ad085`; awaiting review; E-006; accepted for reference harness by review 2 |
| SR-03 | Request `deadline_at` is never checked | P0 | M0-05a | DONE (harness) | Expired requests refused at kernel or adapter per reviewer decision; golden case added | INV-15, INV-18 | golden case | fixed in `4947489`; invalid vs deny pending review; E-006; accepted for reference harness by review 2 (production trusted clocks open) |
| SR-04 | Parent/subagent approval isolation (D-009 divergence from V1) | P0 | M0-05a | DONE (harness) | Reviewer confirms no inheritance or specifies a delegation contract | INV-06, INV-19 | `test_subagent_does_not_inherit_parent_grant` | owner decision D-011 (no inheritance); test `test_subagent_does_not_inherit_parent_grant`; awaiting independent review; accepted for reference harness; owner decision D-011 |
| SR-05 | Project-level evidence isolation: SourceCatalog and Evidence are tenant-scoped only | P0 | M0-05a | DONE (harness) | Evidence and catalog carry project scope; cross-project negative test | INV-10, P-08 | negative test | fixed in `f5a950e` (D-012); awaiting review; E-009; accepted for reference harness by review 3 |
| SR-06 | Fail-closed behavior when Claude Code hooks crash (host treats non-2 exits as non-blocking) | P0 | M0-01e | NOT STARTED | Isolated live-host test of crash, timeout and malformed output; adapter design that turns internal errors into explicit deny | INV-15, threat T-13, `UNMEDIATED-TOOLS.md` | live host test | |
| SR-07 | Broker permit replay at the effect adapter: permit_id never consumed | P0 | none | DONE (harness) | Second presentation of a captured valid permit produces no second effect, including after a new verifier instance on the same ledger | INV-01, INV-09 | replay test | fixed in `2800da3`; awaiting review; E-006; accepted for reference harness by review 2 (single local ledger and controlled key only; E-008 limits) |
| SR-08 | Kernel accepts a canonical resource id with a trailing empty segment | P1 | none | DONE (harness) | `workspace:t/p/` and repeated separators return invalid RESOURCE_NOT_CANONICAL | INV-14, D-008 | golden case + unit test | fixed in `64a5b01`; awaiting review; E-006; accepted for reference harness by review 2 |
| SR-09 | Golden case 22 patch carried no malicious attributes (fixture generator defect) | P1 | none | DONE (harness) | Patch contains the attributes; expected outcome unchanged; test asserts the attributes reach decide() | INV-02, INV-11 | golden case | fixed in `f4489c9`; awaiting review; E-006; accepted by review 2 |
| SR-10 | Effect adapter must bind the permit to the complete request, not only digest/resource/tenant | P0 | none | DONE (harness) | Unused permit presented with changed principal, kind or capabilities is refused without being consumed; legitimate request still succeeds | INV-01, INV-06 | regression test | fixed in `9b90b13` (D-012); awaiting review; E-009; accepted for reference harness by review 3 |
| SR-11 | Idempotent receipt replay must be bound to the full request and the grant used | P1 | SR-10 | DONE (harness) | Different principal with same key and digest refused; duplicate with unrelated grant id refused; identical retry still returns the original receipt | INV-08, INV-09 | regression test | fixed in `4e42d33` (D-012); awaiting review; E-009; accepted for reference harness by review 3 |
| SR-12 | Artifact and human signoff not bound to tenant/project/task in evaluate() | P0 before M1 parity | none | DONE (harness) | Wrong-project, wrong-tenant and wrong-task artifacts never pass; signoff counts only for the same tenant, project, task and artifact digest | INV-10, INV-25 | regression tests | fixed in `0315fde` (D-013); accepted for reference harness by review 4 (`docs/audit/m0/INDEPENDENT-REVIEW-M0-4.md`), CI run 42126984730; limitation: SignoffGrant provenance caller-asserted, production approval service must authenticate |
| RM-01 | Composite multi-effect requests: bind each sub-effect target, digest and permission, or prohibit multiple independent effects per request | roadmap (M1/M3) | none | NOT STARTED | Architecture decision before production multi-effect actions | review 3 design note | n/a | |
