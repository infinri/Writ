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
| M0-01d | Semantic review of 288 rules (confirm/correct disposition, conflict analysis) | P1 | M0-01c | IN PROGRESS | Each row moves to `review_status=reviewed` with reviewer and reason; conflicts listed | M0 checklist "manually inspect all 288" | review diff | review prepared in `docs/audit/m0/rule-review/` (15 batches, blank reviewer columns); 0 of 288 reviewed; awaiting human reviewer; PROPOSED deferral to M6/M7/M12 under D-014 with guard that no unreviewed V1 rule becomes enforced policy |
| M0-01e | Hook failure-posture verification in an isolated host | P1 | M0-01b | IN PROGRESS | Each preventive_candidate and workflow_gate row has a runtime-observed outcome for daemon down, crash and malformed input, run in a disposable environment | threat model T-13, T-15; INV-15 | isolated host harness | SANDBOX PREPARED / LIVE VERIFICATION NOT EXECUTED; see SR-06; PROPOSED deferral to M5 under D-014 |
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
| M0-05a | Independent human review of golden decision expectations and broker scenarios | P0 | M0-03b | IN REVIEW | A reviewer other than the author signs off each high-risk case in `m0-golden.json` or files corrections | testing doc "independent reviewers own oracle decisions" | n/a | worksheet `docs/audit/m0/GOLDEN-HUMAN-REVIEW-WORKSHEET.md`; all 26 cases PENDING INDEPENDENT REVIEW; AI-assisted reviews 2-4 technically AGREE; named human verdicts required; PROPOSED: owner-led review substitutes for M1 development only (D-014); independent verdicts still required before release |
| M0-05b | Push branch and confirm both v2 CI jobs green | P0 | M0-00c | DONE | GitHub Actions run URL recorded in TEST-EVIDENCE | M0 first PR DoD | CI | pushed `487b95d`; run https://github.com/infinri/Writ/actions/runs/42094649411 both jobs success (E-003) |
| M0-05c | M0 go/no-go record | P0 | all P0 above | IN PROGRESS | WORK-STATE lists every M0 checklist item with evidence or explicit gap; user decision recorded | master plan section 10 | n/a | checklist `docs/implementation/M0-GO-NO-GO.md`; development GO RECORDED, PENDING OWNER ATTESTATION (2026-11-06); release and production NO-GO; DONE only after the attestation is verified |

## M0 security review priorities (from the user and the independent review, 2026-11-05; see docs/audit/m0/INDEPENDENT-REVIEW-M0.md)

M0 is **not complete** until these are reviewed and resolved or explicitly risk-accepted, together with M0-01d, M0-01e, M0-05a and M0-05c. No code changes for these until the independent reviewer's findings are in.

| ID | Issue | Pri | Deps | Status | Acceptance criteria | Refs | Tests | Evidence |
|---|---|---|---|---|---|---|---|---|
| SR-01 | Host identity issuer authenticity and signature verification: kernel trusts adapter-set `verified_by`; proofs are verified only in the host adapter | P0 | M0-05a | DONE (harness) | Reviewer decides where T1 is enforced; if the kernel must verify, add PrincipalProof verification plus forged-issuer negative tests | INV-05, T1, golden case 10 | negative tests | fixed in `94d3701` (D-010, sealed ingress); awaiting review; E-006; accepted for reference harness by review 2 (production key provisioning/rotation/registration open) |
| SR-02 | Approval grants do not bind `requested_capabilities` | P0 | M0-05a | DONE (harness) | Grant and approval request id bind the full capability set, or reviewer records why not; swap test added | INV-06, INV-23, golden case 20 | swap test | fixed in `f8ad085`; awaiting review; E-006; accepted for reference harness by review 2 |
| SR-03 | Request `deadline_at` is never checked | P0 | M0-05a | DONE (harness) | Expired requests refused at kernel or adapter per reviewer decision; golden case added | INV-15, INV-18 | golden case | fixed in `4947489`; invalid vs deny pending review; E-006; accepted for reference harness by review 2 (production trusted clocks open) |
| SR-04 | Parent/subagent approval isolation (D-009 divergence from V1) | P0 | M0-05a | DONE (harness) | Reviewer confirms no inheritance or specifies a delegation contract | INV-06, INV-19 | `test_subagent_does_not_inherit_parent_grant` | owner decision D-011 (no inheritance); test `test_subagent_does_not_inherit_parent_grant`; awaiting independent review; accepted for reference harness; owner decision D-011 |
| SR-05 | Project-level evidence isolation: SourceCatalog and Evidence are tenant-scoped only | P0 | M0-05a | DONE (harness) | Evidence and catalog carry project scope; cross-project negative test | INV-10, P-08 | negative test | fixed in `f5a950e` (D-012); awaiting review; E-009; accepted for reference harness by review 3 |
| SR-06 | Fail-closed behavior when Claude Code hooks crash (host treats non-2 exits as non-blocking) | P0 | M0-01e | NOT STARTED | Isolated live-host test of crash, timeout and malformed output; adapter design that turns internal errors into explicit deny | INV-15, threat T-13, `UNMEDIATED-TOOLS.md` | live host test | SANDBOX PREPARED / LIVE VERIFICATION NOT EXECUTED (2026-11-06): sandbox readiness `docs/audit/m0/live-host/2026-11-06-sandbox-readiness/`, PF-1..PF-8 PASS, 0 of 69 experiments, no credential; sandbox torn down 2026-11-06; static re-audit held locally pending disclosure review; PROPOSED deferral to M5 under D-014 (R-G3 recommended: accept for M1) |
| SR-07 | Broker permit replay at the effect adapter: permit_id never consumed | P0 | none | DONE (harness) | Second presentation of a captured valid permit produces no second effect, including after a new verifier instance on the same ledger | INV-01, INV-09 | replay test | fixed in `2800da3`; awaiting review; E-006; accepted for reference harness by review 2 (single local ledger and controlled key only; E-008 limits) |
| SR-08 | Kernel accepts a canonical resource id with a trailing empty segment | P1 | none | DONE (harness) | `workspace:t/p/` and repeated separators return invalid RESOURCE_NOT_CANONICAL | INV-14, D-008 | golden case + unit test | fixed in `64a5b01`; awaiting review; E-006; accepted for reference harness by review 2 |
| SR-09 | Golden case 22 patch carried no malicious attributes (fixture generator defect) | P1 | none | DONE (harness) | Patch contains the attributes; expected outcome unchanged; test asserts the attributes reach decide() | INV-02, INV-11 | golden case | fixed in `f4489c9`; awaiting review; E-006; accepted by review 2 |
| SR-10 | Effect adapter must bind the permit to the complete request, not only digest/resource/tenant | P0 | none | DONE (harness) | Unused permit presented with changed principal, kind or capabilities is refused without being consumed; legitimate request still succeeds | INV-01, INV-06 | regression test | fixed in `9b90b13` (D-012); awaiting review; E-009; accepted for reference harness by review 3 |
| SR-11 | Idempotent receipt replay must be bound to the full request and the grant used | P1 | SR-10 | DONE (harness) | Different principal with same key and digest refused; duplicate with unrelated grant id refused; identical retry still returns the original receipt | INV-08, INV-09 | regression test | fixed in `4e42d33` (D-012); awaiting review; E-009; accepted for reference harness by review 3 |
| SR-12 | Artifact and human signoff not bound to tenant/project/task in evaluate() | P0 before M1 parity | none | DONE (harness) | Wrong-project, wrong-tenant and wrong-task artifacts never pass; signoff counts only for the same tenant, project, task and artifact digest | INV-10, INV-25 | regression tests | fixed in `0315fde` (D-013); accepted for reference harness by review 4 (`docs/audit/m0/INDEPENDENT-REVIEW-M0-4.md`), CI run 42126984730; limitation: SignoffGrant provenance caller-asserted, production approval service must authenticate |
| RM-01 | Composite multi-effect requests: bind each sub-effect target, digest and permission, or prohibit multiple independent effects per request | roadmap (M1/M3) | none | NOT STARTED | Architecture decision before production multi-effect actions | review 3 design note | n/a | |


## PROPOSED tasks (D-014; active once the owner attestation is verified and CI on the decision commit is green)

| ID | Task | Pri | Deps | Status | Acceptance criteria | Refs | Tests | Evidence |
|---|---|---|---|---|---|---|---|---|
| M0-06 | Owner-directed scoped M1 development GO decision | P0 | M0-05c | IN REVIEW | Criteria 1-6 of amendment A-1 met; Decision table filled with owner, date, accepted risks; release and production stated NO-GO; owner attestation verified; CI green on decision commit | D-014 | CI on decision commit | decision RECORDED per owner instruction 2026-11-06, PENDING OWNER ATTESTATION; awaiting push, CI and attestation (signoff package section 3) |
| REL-01 | Independent named human verdicts on all 26 golden cases before any V2 release | P0 (release) | M0-06 | PROPOSED | Worksheet attestation complete; fixture statuses updated only from attributable verdicts; parity re-run on any correction | R-G1, R-ORACLE | parity re-run | |
| M5-SR06 | Execute SR-06 live-host trials (69 priority, then full acceptance) before any host-enforcement claim | P0 (M5) | sandbox credential, R-SB decision | PROPOSED | SR-06-TRIAL-PLAN section 9 met | R-G3, R-SB | 69 experiments + full matrix | |
| M6-RULES | Semantic review of each V1 rule before it is imported; no unreviewed rule enforced | P0 (M6) | M0-06 | PROPOSED | Rule-review CSV row reviewed, re-bound to a V2 evaluator, fixtures pass, INV-20 negative control | R-G2, INV-20 | per-rule fixtures | |
| SB-01 | SR-06 sandbox teardown (only `sr06=1` objects) | P2 | owner authorization | DONE | Procedure in the readiness README executed and verified | R-SB | n/a | executed 2026-11-06 after the telemetry diagnostic; 3 containers and 2 networks removed, nothing else touched; readiness README |
| V2-TEL | V2 makes no undeclared egress; third-party telemetry (for example ONNX Runtime) disabled or proxied by declared policy | P1 (M7) | none | PROPOSED | Allowlist-proxy egress test in CI shows zero undeclared destinations | R-V1TEL | egress test | V1 diagnostic: `evidence/telemetry-attribution.txt` |

## Writ Control Center (D-015, ADR-0007; specification in docs/control-center)

Not a prerequisite for M1. Each task starts with its milestone; acceptance tests in `docs/control-center/acceptance-tests.md`.

| ID | Task | Pri | Deps | Status | Acceptance criteria | Refs | Tests | Evidence |
|---|---|---|---|---|---|---|---|---|
| CC-00 | Control Center product specification, architecture, ADR and roadmap integration | P1 | D-015 | DONE | Spec, journeys, management API, trust, audit model, lifecycle semantics, acceptance tests, plan, ADR-0007, INV-27..31, P-20..24 committed; docs check passes | D-015 | docs check | `docs/control-center/`; documentation only, nothing implemented |
| CC-M2 | Management contracts and conventions; kernel builds and tests without management modules | P1 (M2) | M2 | NOT STARTED | Schemas and fixtures in `docs/contracts/management/`; CC-SEC-01 harness | INV-27 | CC-SEC-01, CC-SEC-14 | |
| CC-M3 | Management service skeleton, management authorization policy, audit query model, correlation, explanation service | P1 (M3) | CC-M2, M3 | NOT STARTED | CC-A01..A04, CC-G06 service | INV-28, INV-30 | CC-SEC-01, 02, 03, 05, 08, 11, 14; CC-UX-09 | |
| CC-M4 | Approval experience with step-up | P0 (M4) | CC-M3, M4 | NOT STARTED | CC-AP01..AP03 | INV-06, P-03 | CC-SEC-07 | |
| CC-M5 | Adapter status, coverage labels, unmediated activity | P1 (M5) | CC-M3, M5, M5-SR06 | NOT STARTED | CC-I01, I02, I04, A05 | INV-13, INV-16 | label-evidence tests | |
| CC-M6 | Policy, contract and pack management; enforcement labels | P1 (M6) | CC-M3, M6 | NOT STARTED | CC-R01..R05, CC-C01..C06, CC-I03, CC-G07 | INV-12, INV-20, INV-29 | CC-SEC-04, 12, 13, 16, 18 | |
| CC-M7 | Knowledge administration and retrieval observability | P1 (M7) | CC-M3, M7 | NOT STARTED | CC-K01..K07 | INV-10, INV-11, INV-30 | CC-SEC-03, 09, 10 | |
| CC-M8 | Memory and graph management | P1 (M8) | CC-M3, M8 | NOT STARTED | CC-M01..M04 | INV-10, INV-30 | CC-SEC-03, 09 | |
| CC-M9 | Unified local Control Center (minimum usable beta scope) | P0 (M9) | CC-M3..CC-M8 | NOT STARTED | All tier B requirements | P-20..P-24 | all beta CC-SEC; CC-UX-01..09 | |
| CC-E | Enterprise feature set, each with its own ADR | P2 (M10+) | CC-M9 | NOT STARTED | Tier E requirements justified by recorded workload | P-20 | per feature | |

## Acceptance Gate Integrity (D-016, ADR-0008 proposed)

| ID | Task | Pri | Deps | Status | Acceptance criteria | Refs | Tests | Evidence |
|---|---|---|---|---|---|---|---|---|
| AG-00 | Acceptance Gate Integrity architecture proposal, invariants, threats, parity-suite additions, HTML projection | P0 | D-016 | IN REVIEW | Specification, ADR-0008, INV-32..39, threat rows, M1 parity additions, Control Center rows, HTML sections; docs check passes; owner accepts or amends ADR-0008 | D-016 | docs check | `docs/architecture/acceptance-gate-integrity.md`; documentation only |
| AG-M1 | Transition and acceptance-binding fixtures with hand-reasoned expectations; both prototypes pass them | P0 (M1) | M0-06 accepted, AG-00 | NOT STARTED | Parity suite "Acceptance Gate Integrity readiness" sets pass in Rust and Go | INV-32..INV-36 | AG-DIF-01, AG-PROP-01, AG-CON-01, AG-CON-02 | |
| AG-M2 | Production contracts: AcceptanceRecord, EvaluationRecord scope fields, evaluator registry entry, runner attestation, protected workflow states | P0 (M2) | AG-M1, ADR-0002 | NOT STARTED | Schemas, fixtures and contract tests | INV-32..INV-39 | contract tests | |
| AG-M3 | Transition service with CAS, single-use records, use-time recomputation; sandboxed mechanical runner; audit events | P0 (M3) | AG-M2 | NOT STARTED | AG positive, negative, failure and concurrency tests pass | INV-32..INV-38 | AG-POS, AG-NEG, AG-FAIL, AG-CON | |
| AG-M4 | Authenticated signoff provenance; waivers as approvals | P0 (M4) | AG-M3 | NOT STARTED | AG-NEG-05; forged signoff refused | INV-06, INV-34 | AG-NEG-05 | |
| AG-M6 | Signed acceptance policies; evaluator registry publication; pack rubrics bound to registered evaluators | P0 (M6) | AG-M3, M6 | NOT STARTED | AG-ADV-09, AG-ADV-10, AG-ADV-16 | INV-37 | adversarial tests | |
| AG-M12 | Adversarial, integration and failure campaign before any production enforcement claim | P0 (M12) | all AG | NOT STARTED | Full AG suite in a disposable environment; results recorded | INV-32..INV-39 | full suite | |
