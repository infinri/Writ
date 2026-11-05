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
| G3 / M0-01e, SR-06 | Isolated live Claude Code host verification | `docs/audit/m0/LIVE-HOST-VERIFICATION.md` | Open, no trial run |
| G4 / M0-05c | Owner go/no-go record | this file | Open |

## Risks requiring an owner decision (accept, mitigate later, or block)

| Risk | Owner milestone | Decision | Decided by / date |
|---|---|---|---|
| Ingress seal and permit keys are in-process; no provisioning, rotation or registration (SR-01, SR-07) | M3/M5 | | |
| SignoffGrant provenance caller-asserted in M0 (SR-12, D-013) | M4 | | |
| Policy bundles unsigned; signature verification absent | M6 | | |
| Composite multi-effect requests not individually bound (RM-01) | M1/M3 | | |
| SQLite ledger: no network filesystem, power-loss or interrupted-commit guarantees (E-008) | M3/M9 | | |
| Use-time filesystem TOCTOU and real OS effect mediation absent | M3 | | |
| Trusted clocks for deadlines and expiry | M3 | | |
| Claude Code hooks are advisory, not an OS sandbox; crash behavior unobserved (until G3) | M5 | | |
| Golden case 24 id misnamed (cosmetic) | M0 | | |

## Go criteria

GO for M1 requires: G1, G2 and G3 complete with evidence; every row in the risk table decided; CI green on the decision commit; and this file updated with the owner's decision, date and commit SHA. Alternatively the owner may record an explicit, scoped risk acceptance for any open gate.

## Decision

| Field | Value |
|---|---|
| Decision (GO / NO-GO / GO with accepted risks) | |
| Owner | |
| Date | |
| Commit | |
| Accepted risks and scope | |
