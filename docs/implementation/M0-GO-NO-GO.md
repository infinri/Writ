# M0 go/no-go checklist (M0-05c)

**Current recommendation: NO-GO.** Prepared 2026-11-05 at branch head after `c3b37af`. The decision belongs to the project owner; nothing here is a decision. M0 output is a Python reference harness and audit evidence, **not a production system and not a security certification**.

## Completed, with evidence

| Requirement (source) | Evidence | Qualification |
|---|---|---|
| Pin V1 SHA, checksummed baseline (M0-01) | `docs/audit/m0/v1-baseline-manifest.json`; drift check in CI | Static |
| 52 hook registrations with capabilities, coverage, failure posture (M0-01) | `docs/audit/m0/hook-registrations.csv` | Failure posture static only; runtime is gate G3 |
| 288-rule classification (M0-01) | `docs/audit/m0/rule-disposition.csv` | Automated first pass only; semantic review is gate G2 |
| Golden fixtures: retrieval, approval replay, isolation, effect gaps (M0-01) | `v1-golden-retrieval.json`; `m0-golden.json`; `test_v1_regressions.py`; `V1-REGRESSION-PORT.md` | Golden decisions await named review (G1) |
| TaskSpec/Artifact/Evidence with no mandatory file/PR fields (M0-02) | `docs/contracts/`; `test_contracts.py` | Draft contracts |
| 3 positive and 3 negative cross-domain tasks (M0-02) | `docs/contracts/fixtures/domains/`; `test_evaluation.py`; broker unapproved-effect tests | |
| Two stub ModelAdapters, one simulated external host (M0-02) | `providers.py`, `hosts.py`; `test_independence.py` | Stubs only |
| Identical verdicts independent of model and pack (M0-02) | `TestManagedRunner`, `TestHostNormalization`, `TestPacks` | |
| Principal proof, permit and grant contracts (M0-03) | 4 schemas; D-008, D-010, D-012 | Production key management open |
| Host-name-independent normalization (M0-03) | two host extractors, parity test | Simulated hosts |
| Pack manifest verification; pack cannot authorize or change scope (M0-03) | `packs.py`; `TestPacks` | Unsigned design fixtures only |
| Unmediated tools enumerated (M0-03) | `UNMEDIATED-TOOLS.md` | Static; runtime confirmation is G3 |
| M1 parity suite and workloads defined (M0-04) | `M1-PARITY-SUITE.md` | Specification only |
| Hardware/toolchain record with no invented numbers (M0-04) | `BENCHMARK-ENVIRONMENT.md` | Rust and Go absent locally |
| CI for schemas, examples, links, fixture suite (M0-04, first PR DoD) | `.github/workflows/v2-docs.yml`; last run 42130113990, 281 passed | Pages deploy job fails on a repository protection rule (unrelated) |
| Model output cannot mint approval; external host needs no ModelAdapter (first PR DoD) | `test_model_saying_approved_mints_nothing`, `test_approval_text_anywhere_mints_nothing`, INV-22 subprocess test | |
| Security findings SR-01..SR-05, SR-07..SR-12 | DONE (harness) per AI-assisted reviews 2 to 4; `INDEPENDENT-REVIEW-M0-*.md` | Reference-harness scope only |

## Outstanding gates

| Gate | Requirement | Prepared material | Status |
|---|---|---|---|
| G1 / M0-05a | Named human verdicts on all 26 golden cases | `docs/audit/m0/GOLDEN-HUMAN-REVIEW-WORKSHEET.md` | Open, 0 of 26 |
| G2 / M0-01d | Human semantic review of 288 rules | `docs/audit/m0/rule-review/` (15 batches) | Open, 0 of 288 |
| G3 / M0-01e, SR-06 | Isolated live Claude Code host verification | `docs/audit/m0/SR-06-TRIAL-PLAN.md`; `docs/audit/m0/live-host/2026-11-06-sandbox-readiness/README.md` | **SANDBOX PREPARED / LIVE VERIFICATION NOT EXECUTED** (0 of 69 priority experiments; no credential provisioned) |
| G4 / M0-05c | Owner go/no-go record | this file | Open |

## Risks requiring an owner decision (accept, mitigate later, or block)

| Risk | Owner milestone | Decision | Decided by / date |
|---|---|---|---|
| Ingress seal and permit keys are in-process; no provisioning, rotation or registration (SR-01, SR-07) | M3/M5 | Mitigate in M3/M5 (key provisioning, rotation, registration); not closed | Recommended by independent technical review 2026-11-06; owner signature pending |
| SignoffGrant provenance caller-asserted in M0 (SR-12, D-013) | M4 | Mitigate in M4 (authenticated approval service); not closed | Recommended by independent technical review 2026-11-06; owner signature pending |
| Policy bundles unsigned; signature verification absent | M6 | Mitigate in M6 (signed bundles); not closed | Recommended by independent technical review 2026-11-06; owner signature pending |
| Composite multi-effect requests not individually bound (RM-01) | M1/M3 | Mitigate in M1/M3 (bind or prohibit sub-effects); not closed | Recommended by independent technical review 2026-11-06; owner signature pending |
| SQLite ledger: no network filesystem, power-loss or interrupted-commit guarantees (E-008) | M3/M9 | Mitigate in M3/M9; E-008 limitations stand; not closed | Recommended by independent technical review 2026-11-06; owner signature pending |
| Use-time filesystem TOCTOU and real OS effect mediation absent | M3 | Mitigate in M3; not closed | Recommended by independent technical review 2026-11-06; owner signature pending |
| Trusted clocks for deadlines and expiry | M3 | Mitigate in M3; not closed | Recommended by independent technical review 2026-11-06; owner signature pending |
| Claude Code hooks are advisory, not an OS sandbox; crash behavior unobserved (until G3) | M5 | Mitigate in M5 together with R-G3; not closed | Recommended by independent technical review 2026-11-06; owner signature pending |
| Golden case 24 id misnamed (cosmetic) | M0 | Accept; rename only after independent review links are reconciled; not closed | Recommended by independent technical review 2026-11-06; owner signature pending |

## Go criteria

GO for M1 requires: G1, G2 and G3 complete with evidence; every row in the risk table decided; CI green on the decision commit; and this file updated with the owner's decision, date and commit SHA. Alternatively the owner may record an explicit, scoped risk acceptance for any open gate.

## PROPOSED amendment A-1: owner-directed, scoped M1 development GO (not decided)

**Status: APPROVED IN PRINCIPLE as a development-only acceptance framework (owner instruction, 2026-11-06). Not a GO decision. The Decision table below stays blank until the owner signs it.** [DECISION-LOG D-014](DECISION-LOG.md) remains PROPOSED until the owner's exact policy approval is attributable (criterion 5). Risk dispositions below are recommendations from the independent technical review; none is signed and no risk is CLOSED.

### What a development GO would and would not permit

| Permitted by a development GO | Not permitted (remains NO-GO) |
|---|---|
| Begin M1: Rust and Go prototypes, the parity suite in [M1-PARITY-SUITE](M1-PARITY-SUITE.md), the ADR-0002 bake-off | Any V2 release, tag or package publication |
| Use the 26 golden decisions as the **provisional** parity oracle | Running V2 against production data or privileged production workloads |
| Continue owner-led technical investigation and AI-assisted reviews | Claiming real host enforcement, fail-closed hooks, or production security for any component |
| | Importing any V1 rule as enforced V2 policy |
| | Marking SR-06, M0-05a or M0-01d as passed |

### Proposed gate dispositions (development scope only)

| Gate | Current requirement | Proposed disposition for the development GO | What remains required later |
|---|---|---|---|
| G1 / M0-05a | Named independent human verdicts on 26 cases | Replaced, for M1 development only, by the owner-led technical review plus AI-assisted reviews 1 to 4 and owner policy decisions (D-011). All 26 cases keep `PENDING INDEPENDENT REVIEW`; no human signoff is recorded or implied | Independent human verdicts before any V2 release. Any later DISAGREE re-opens the affected golden case and re-runs M1 parity |
| G2 / M0-01d | Human semantic review of 288 rules | Deferred to the migration milestones that consume rules (M6 rule registry, M7 retrieval, M12 import). All 288 stay `automated_first_pass` | Per-rule semantic review before a rule is imported. Hard guard: no V1 rule becomes authoritative or enforced V2 policy without review, re-binding to a V2 evaluator and passing tests (INV-20, A-07, D-004) |
| G3 / M0-01e, SR-06 | Live host verification | Deferred to the host-adapter integration milestone (M5). SR-06 stays **NOT VERIFIED**; sandbox preparation is recorded, not trials | The 69 priority experiments and the full SR-06 acceptance in [SR-06-TRIAL-PLAN](../audit/m0/SR-06-TRIAL-PLAN.md) section 9 before any claim of host enforcement |
| G4 / M0-05c | Owner decision | Unchanged: an attributable owner decision is required | n/a |

### Proposed development GO criteria (all must hold at the decision commit)

1. **CI green** on the decision commit: both jobs of `.github/workflows/v2-docs.yml` pass (M0 suite, audit drift, docs and schema checks); run URL recorded.
2. **Technical review evidence complete:** reviews 1 to 4 recorded under `docs/audit/m0/INDEPENDENT-REVIEW-M0*.md`; SR-01..SR-05 and SR-07..SR-12 DONE (harness) with production limits stated; owner-led technical review of this checklist recorded.
3. **No unresolved critical M0 reference-harness defect:** no open P0 harness finding in [TASKS](TASKS.md). (SR-06 is a host-verification gap, not a harness defect, and is covered by risk R-G3.)
4. **Every risk in the table above and in the additional risks below has a recorded decision** (accept for development, mitigate in a named milestone, or block).
5. **Attributable owner decision:** owner name, date, decision commit SHA, and a verifiable channel (signed commit, or a PR review or comment from the owner's account).
6. **Release and production remain NO-GO**, stated in the Decision table.

### Additional risks the owner must decide for a development GO

| ID | Risk | Owner milestone | Required negative tests before the risk can be closed | Decision | Decided by / date |
|---|---|---|---|---|---|
| R-G1 | No independent human has verified the 26 golden decisions. The author and the reviewers 1-4 are AI systems and may share blind spots; owner policy decisions (D-011) are not independent review | Before release | Independent verdicts on all 26; parity re-run on any correction | Accept for M1 development only; independent human golden review remains unresolved (REL-01) | Recommended by independent technical review 2026-11-06; owner signature pending |
| R-ORACLE | M1 Rust and Go prototypes inherit any error in the provisional oracle; parity agreement would not detect it | M1 | Golden cases re-reviewed independently; property tests and differential tests kept beside parity | Mitigate in M1: independent test properties, adversarial cases and cross-implementation checks beyond parity | Recommended by independent technical review 2026-11-06; owner signature pending |
| R-G2 | 288 V1 rules uncurated; some may be duplicated, conflicting or obsolete | M6/M7/M12 | INV-20 (remove validator, attempt policy publish); per-rule fixtures before import | Accept for M1; no unreviewed V1 rule may be imported as enforced policy (M6-RULES) | Recommended by independent technical review 2026-11-06; owner signature pending |
| R-G3 | Real Claude Code host behavior unobserved: crash, timeout, invalid output, subagent and MCP coverage, `onFailure` effect, V1 fail-open paths | M5 | The 69 priority experiments, including NC-1, and full SR-06 acceptance | Accept for M1; SR-06 must be completed before any host-enforcement claim (M5-SR06) | Recommended by independent technical review 2026-11-06; owner signature pending |
| R-SB | Sandbox limitations: shared rootful Docker daemon (container escape is host root), not a VM. Owner position: injection (P4), bypass (P5) and approval-provenance (P3) experiments are not authorized on this workstation and need a disposable VM or separate machine | M5 (before live trials) | Adversarial experiments only on a VM or separate test machine; container use for probe-only experiments only if the owner accepts it | Accept non-adversarial sandbox preparation only; adversarial and fault-injection trials deferred to stronger isolation (VM or separate machine) | Recommended by independent technical review 2026-11-06; owner signature pending |
| R-V1TEL | The V1 daemon attempts outbound connections to `mobile.events.data.microsoft.com` (62 blocked attempts in the sandbox), most likely ONNX Runtime 1.31.1 telemetry; stopped by `ORT_DISABLE_TELEMETRY=1` in a 10-minute controlled run; not proven at packet level | M7 (retrieval runtime) and before any credential | Packet- or library-level confirmation; V2 retrieval runtime must make no undeclared egress (egress test with an allowlist proxy) | Investigate now (diagnostic done 2026-11-06: ORT_DISABLE_TELEMETRY=1 stopped requests in a 10-minute window); prevent undeclared telemetry in V2 | Recommended by independent technical review 2026-11-06; owner signature pending |
| R-V1PKG | V1 imports `PyYAML` and `tokenizers` without declaring them (observed in the sandbox build) | M12 (migration tooling) | Migration and import tooling installs from a lock that is verified, not from V1's pyproject alone | Mitigate before any migration tooling release | Recommended by independent technical review 2026-11-06; owner signature pending |

Existing risks in the table above keep their milestones; their required negative tests (proposed) are: keys and rotation, permit and seal forgery after key rotation (M3/M5); SignoffGrant, a forged in-scope signoff refused without an authenticated approval-service record (M4); policy bundles, a damaged signature and a downgraded bundle refused (INV-05, INV-12, M6); RM-01, a sub-effect target swapped under one approval refused (M1/M3); SQLite ledger, crash during commit and on network filesystems (M3/M9); TOCTOU, symlink and path substitution between decision and use (INV-14, T-03, M3); trusted clocks, skewed or rolled-back clock cannot extend a grant or deadline (M3); hooks, as R-G3 (M5).

## Decision

| Field | Value |
|---|---|
| Decision (GO / NO-GO / GO with accepted risks) | |
| Owner | |
| Date | |
| Commit | |
| Accepted risks and scope | |
