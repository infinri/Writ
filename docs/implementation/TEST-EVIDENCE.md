# Writ V2 test evidence

Only results actually executed are recorded under **Executed**. Planned verification lives in [TASKS](TASKS.md) and [verification-and-benchmarks](../testing/verification-and-benchmarks.md).

**Environment (all entries unless stated):** AMD Ryzen 9 7940HS (16 threads), 30 GiB RAM, Ubuntu 24.04.3, Linux 6.17.0-40-generic x86_64, Python 3.12.3, venv `m0/.venv` with jsonschema 4.23.0, pytest 8.3.4, hypothesis 6.122.3, SQLite 3.45.1. No Neo4j, no network services, no model credentials.

## Executed

### E-001: M0 audit generators and tests

- **Revision:** committed as `5791cb7`
- **Commands:** `cd m0 && .venv/bin/python -m writ_m0.audit generate`, `... check`, `.venv/bin/python -m pytest`
- **Result:** generate wrote 7 artifacts; check current; pytest 18 passed, 0 failed.
- **Observed baseline facts:** 52 hook registrations across 12 events; 288 Rule nodes (32 mandatory, 33 with mechanical_enforcement_path); 468 nodes and 1,268 edges, 0 dangling; 1,139 tracked files at `ebdafe5`.

### E-002: Full M0 harness (contracts, evaluation, decision oracle, broker, independence, packs)

- **Revision:** `c213d7c`
- **Command:** `cd m0 && .venv/bin/python -m pytest`; `.venv/bin/python -m writ_m0.audit check`; `python3 scripts/check-v2-docs.py`
- **Result:** 201 passed, 0 failed (test_audit 19, test_broker 26, test_contracts 48, test_decision 36, test_evaluation 20, test_independence 52). Audit artifacts current (8 files). Docs check OK: 45 Markdown, 15 HTML, 17 schemas.
- **Concurrency:** `test_broker.py -k concurrent` repeated 15 times, 15/15 passed (16-thread grant race, 8-thread duplicate dispatch).
- **Mutation checks (manual, reverted after):** removing the date-time format checker failed 4 contract tests; removing `AND uses < max_uses` from grant consume failed 2 broker tests. Confirms those tests detect the defects they target.
- **Coverage:** not measured.

### E-003: First GitHub CI run of the M0 harness

- **Revision:** `487b95d47f6ac8b8a4480935ee8388d27b930191` (pushed to `origin/v2/architecture-rebuild`)
- **Run:** https://github.com/infinri/Writ/actions/runs/42094649411 (workflow file `.github/workflows/v2-docs.yml`, ubuntu-latest, Python 3.12 via setup-python)
- **Result:** both jobs success. Docs check: 45 Markdown, 15 HTML, 17 schemas. M0 tests: 201 passed in 7.21s. Audit check: 8 files current (pinned-SHA git reads worked with `fetch-depth: 0`).
- **Local vs CI:** identical counts (201 passed locally, E-002). Differences: CI installs with pip into the runner Python, not the uv venv; the workflow's display name shows as "V2 documentation integrity" in the run list (registered name) although the file says "V2 documentation and contract integrity".
- **Unrelated failure in the same push:** "Publish Writ V2 architecture website" run https://github.com/infinri/Writ/actions/runs/42094649959 failed at "Publish combined site to GitHub Pages": `Branch "v2/architecture-rebuild" is not allowed to deploy to github-pages due to environment protection rules.` The same failure occurred on the previous push (`f89b442`, run 42075753295). Repository settings issue, not caused by M0 changes.

### E-004: V1 regression port and grant validation fix

- **Revision:** working tree on `cd76fdd`, committed in the M0-01f commit (see `git log`)
- **Red first:** `test_grant_missing_a_binding_field_is_not_honored` failed 4/4 with `KeyError` before the fix (no effect applied).
- **After fix:** `cd m0 && .venv/bin/python -m pytest` 223 passed, 0 failed (202 before plus 21 in `test_v1_regressions.py`). Audit check current.
- **CI:** see E-005.

### E-005: CI on the review package and regression port

- **Revision:** `38ccbf6b36d5913cc4514374d6dfb4bd5094ca29`
- **Run:** https://github.com/infinri/Writ/actions/runs/42098268490
- **Result:** both jobs success. M0 tests 223 passed in 6.84s (matches local E-004; count includes the 21 tests in `test_v1_regressions.py`). Audit check: 8 files current. Docs check: 47 Markdown, 15 HTML, 17 schemas.
- **Unrelated:** Pages publish run https://github.com/infinri/Writ/actions/runs/42098268329 failed with the same environment protection rule as E-003.

### E-006: Security corrections SR-01, SR-02, SR-03, SR-07, SR-08, SR-09

- **Revision:** `f4489c9` (local, not pushed)
- **Red first (each observed before its fix):** SR-08 4 failures; SR-03 8 failures; SR-02 3 failures (substitution test: DID NOT RAISE); SR-07 3 failures; SR-01 unsealed request with trusted issuer string executed (DID NOT RAISE) plus 8 more ingress failures; SR-09 1 failure.
- **After all fixes:** `cd m0 && .venv/bin/python -m pytest` 256 passed, 0 failed. Concurrency tests (`-k concurrent`, now including concurrent permit replay) 10/10 runs passed.
- **Mutation:** replacing ingress verification with pass-through fails 9 of 10 ingress tests (the passing one is the positive case).
- **CI:** see E-007.

### E-007: CI on the security corrections

- **Revision:** `d3f722d11ba4b942bd067b87b18acb2d72ea2805`
- **Run:** https://github.com/infinri/Writ/actions/runs/42103334452
- **Result:** both jobs success. M0 tests 256 passed in 8.76s (matches local E-006). Audit check 8 files current. Docs check 48 Markdown, 15 HTML, 17 schemas.
- **Unrelated:** Pages publish run 42103334296 failed on the same environment protection rule as E-003.

### E-008: Cross-process and crash behavior of single-use permits (exploratory, not committed)

- **Revision:** `d3f722d` code, unmodified. Ad hoc script outside the repository (session scratchpad, `permit_xproc.py`), run with `m0/.venv/bin/python`.
- **Setup:** one SQLite ledger file (WAL, local ext4 disk); workers are separate OS processes (`multiprocessing`, fork) each opening their own `Ledger` and `PermitSigner`. The broker HMAC key was injected into each worker, because in M0 the key lives only in process memory (a second process otherwise cannot verify any permit).
- **Results:**
  - 20 trials x 16 processes racing to consume one permit: exactly 1 winner in 20/20 trials.
  - Worker killed with `os._exit(137)` immediately after `consume` and before any effect: a later replay from another process was refused.
  - A broker restarted with the default (new random) key refused an old, never-consumed permit.
- **What this does not show:** network or shared filesystems (SQLite locking is unreliable there); power loss (`synchronous` left at the driver default, not tested); a crash *inside* the commit; multi-host workers. Crash after consume but before the effect loses the attempt (at-most-once): the broker receipt stays `dispatched` and `reconcile` marks it `unknown_reconcile`, as covered by `test_crash_after_effect_leaves_reconcilable_receipt`.
- **Required limitations (owner, 2026-11-05; preserve in any report citing E-008):**
  1. Multi-process testing used manually distributed key material (key injected into each worker).
  2. Production permit-key provisioning and rotation are unimplemented.
  3. SQLite must not be assumed reliable for coordination over shared or network filesystems.
  4. Power-loss durability and interrupted database commits are unverified.
  5. A consumed permit whose effect did not execute must never be reported as a successful effect. In M0 such an attempt remains `dispatched` and reconciles to `unknown_reconcile`, never `observed_success`.
- **Gap for review:** M0 has no shared or persisted permit key, so "concurrent workers" is only a supported configuration once key management exists (M3). A committed multi-process test should be added after the security review.

### E-009: SR-10, SR-11, SR-05 remediation

- **Revision:** `f5a950e` (local, not pushed)
- **Red first:** SR-10 5 failures (altered request spent a valid permit: DID NOT RAISE); SR-11 3 failures (other principal and unrelated grant received the original receipt: DID NOT RAISE; receipt lacked fingerprint); SR-05 4 failures (contract and catalog API lacked project scope).
- **After:** `cd m0 && .venv/bin/python -m pytest` 270 passed, 0 failed; concurrency tests 5/5; audit check current; docs check OK.
- **CI:** run https://github.com/infinri/Writ/actions/runs/42123472509 at `957e32f`, 270 passed, audit and docs checks pass.

### E-010: SR-12 artifact and signoff scope

- **Revision:** `0315fde` (local, not pushed)
- **Red first:** 12 failures. Behavioral: 4 (artifact with another tenant or task evaluated `pass` with valid evidence, for code-review and research). Missing contract: 8 (Artifact had no `project_id`; SignoffGrant had no project or task; 2 existing signoff tests updated to the scoped constructor).
- **After:** `cd m0 && .venv/bin/python -m pytest` 280 passed, 0 failed; audit check current; docs check OK. Golden decision fixture unchanged.
- **CI:** run https://github.com/infinri/Writ/actions/runs/42126984730 at `e086341`, 280 passed, audit and docs checks pass.

### E-011: Gate preparation

- **Revision:** local, after `c3b37af`
- **Command:** `cd m0 && .venv/bin/python -m pytest` 281 passed, 0 failed (adds `TestRuleReviewBatches`: 288 rules partitioned once across 15 batches, all reviewer columns blank). Audit check current; docs check OK. `python -m writ_m0.audit.rule_review` refused a second run (exit 1) as designed.
- **No gate executed:** golden human review, rule review and live-host trials have not been performed.

### E-012: CI on the gate-preparation commits

- **Revision:** `6ab870d24f5b75fe83ddbc701ddb9fc000b88f3c` (`v2/architecture-rebuild`)
- **Run:** https://github.com/infinri/Writ/actions/runs/42129295198
- **Result:** both jobs success. M0 tests 281 passed (matches local E-011). Audit check 8 files current. Docs and schema check passed (70 Markdown, 15 HTML).
- **Push note:** four push attempts from this workstation were rejected with `remote: fatal error in commit_refs`; the branch was later found at `6ab870d` on GitHub. How the update landed was not determined.
- **Unrelated:** the Pages publish run on the same push (42129294844) failed, as on every earlier push (environment protection rule).

### E-013: CI on SR-06 preparation, GO framework and signoff package

| Commit | Content | CI run | Result |
|---|---|---|---|
| `926b8b6` | SR-06 trial plan and sandbox setup | [42278124378](https://github.com/infinri/Writ/actions/runs/42278124378) | success, 281 passed, docs OK |
| `6007d09` | A-1 framework, risk dispositions, sandbox readiness evidence and teardown | [42308217716](https://github.com/infinri/Writ/actions/runs/42308217716) | success, 281 passed, audit 8 current, docs OK |
| `e8aa771` | signoff package (not signed) | [42309913797](https://github.com/infinri/Writ/actions/runs/42309913797) | success, 281 passed, audit 8 current, docs OK |

SR-06 sandbox facts (preflight PASS, telemetry diagnostic) are recorded in `docs/audit/m0/live-host/2026-11-06-sandbox-readiness/`; none is an SR-06 trial result. Plan corrections (600 s default timeout, `{not json}` probe) are plan revisions, not test evidence.

### E-014: CI on the M0 development-GO decision commit

- **Revision:** `38e2d1f31fb11a145cb7a109934e85b018dd1d60` (pushed alone, with its parent `df87cf4`; origin moved `e8aa771..38e2d1f`, no force)
- **Run:** https://github.com/infinri/Writ/actions/runs/42319056546 (head SHA verified equal to the decision commit)
- **Result:** both jobs success. M0 reference harness and audit drift: 281 passed, audit 8 files current. V2 documentation and schemas: docs check OK (74 Markdown in CI; local counts include untracked files), link and schema checks passed.
- **Unrelated:** Pages publish run 42319056484 failed: `Branch "v2/architecture-rebuild" is not allowed to deploy to github-pages due to environment protection rules.` Same pre-existing cause as E-003.
- **Gate meaning:** criterion 1 of amendment A-1 holds for the decision commit. The owner attestation (criterion 5) is still required; D-014 remains PROPOSED.

### E-015: Owner attestation verification (read-only) and docs CI

- **Attestation:** https://github.com/infinri/Writ/commit/38e2d1f31fb11a145cb7a109934e85b018dd1d60#commitcomment-208565071. Read via `gh api repos/infinri/Writ/commits/38e2d1f.../comments`: author `infinri`, `author_association` OWNER, created and last updated 2026-11-07T00:44:26Z; decision CI run 42319056546 completed 2026-11-07T00:34:59Z (head SHA `38e2d1f31fb11a145cb7a109934e85b018dd1d60`, success). Body names the decision SHA and the run URL; scope text matches the signoff-package template. The owner stated in session that it was posted personally.
- **Defects noted, not blocking:** date placeholder `(date)` left unfilled (GitHub's timestamp is the date of record); "AIsession" missing a space.
- **Limitation:** a web UI comment cannot be cryptographically distinguished from an API comment by the same account; Claude posted no comment.
- **Docs commits:** `ae7944f`, `606ab5c` pushed (origin `38e2d1f..606ab5c`, no force); CI run https://github.com/infinri/Writ/actions/runs/42320181315 success on `606ab5c`.

### E-016: M1-00 toolchain installation and smoke tests

- **Date:** 2026-11-07. **Script:** `m1/tools/install-toolchains.sh` (pinned versions, SHA-256 and GPG fingerprints).
- **Result:** Rust 1.99.1 archive SHA-256 `231fcae4...76cfe7` OK, good signature from primary key `108F66205EAEB0AAA8DD5E1C85AB96E6FA1BE5FE`; Go 1.27.3 archive SHA-256 `65efbc80...379b40` OK (matches go.dev JSON), good signature from primary key `EB4C1BFD4F042F6DDDCCEC917721F63BD38B4796`. Installed to `~/.local/writ-m1-toolchains`; no prior Rust or Go present; no sudo.
- **Versions:** `rustc 1.99.1 (58cf8f9c3 2026-10-13)`, LLVM 23.1.1; `cargo 1.99.1 (5f94df478 2026-08-27)`; `go version go1.27.3 linux/amd64`; `GOTOOLCHAIN=local`; Go telemetry mode `local` (no upload).
- **Smoke:** `m1/tools/smoke/run.sh` printed `rust-smoke ok sum=55`, `Hello, world!` (cargo), `go-smoke ok sum=55 sha256(writ)=bd998adf...46bc8adbb` (equal to `sha256sum`), `smoke: all passed`; `go vet` clean.
- Details: [BENCHMARK-ENVIRONMENT](BENCHMARK-ENVIRONMENT.md).

### E-017: M1-01 parity driver self-tests

- **Command:** `python3 -m unittest discover -s m1/driver/tests -v` (system Python 3.12.3; M0 adapter run with `m0/.venv/bin/python`).
- **Result:** 11 tests, OK. Correct stub passes all sets; faulty stubs (wrong field, missing answer, non-JSON line, crash exit 3, hang with 2 s timeout, duplicate id, nondeterministic output under `--repeat 3`) each make the driver exit 1 with the fault recorded; report carries manifest, fixture and executable digests; exported sets current. The M0 reference adapter answered 27 of 27 cases (1 canonical vector, 26 golden decisions) through the protocol. Agreement with the M0 oracle is not evidence that the golden expectations are right (R-ORACLE).

### E-018: M1-02 fixture construction checks (no prototype exists)

- **Date:** 2026-11-07. **Commands:** `python3 m1/fixtures/export_m0_fixtures.py` then `--check` (5 sets, 117 cases, current); `jsonschema` (m0 venv): `m1-transition.bundle.json` valid against `policy-bundle.schema.json`; 22 of 23 transition requests valid against `action-request.schema.json` (tx-23 invalid by design).
- **Consistency check after expectations were frozen:** `python3 m1/driver/parity.py --proto "m0/.venv/bin/python m1/adapters/m0_oracle_proto.py" --sets transition-decisions,golden-decisions,canonical-vector`: 23/23, 26/26, 1/1. This shows agreement with M0 semantics only, not correctness (R-ORACLE); expectations were not edited after the run.
- **Driver self-tests** with all five sets: 11 of 11 OK.
- **Not executed:** any implementation of `check-acceptance` or `transition-scenario` (67 cases have no implementation yet); any Rust or Go code; any benchmark.

### E-019: M1-02 amendment 1 (D-017) checks; review baseline CI

- **Review baseline:** `4a66364b355569ab7cb595417c957ef2f98b8b2d` pushed (fast-forward from `9d3296b`); CI https://github.com/infinri/Writ/actions/runs/42327533200 success.
- **Amendment (local, 2026-11-07):** `python3 m1/fixtures/export_m0_fixtures.py --check`: 6 sets current, 208 cases (canonical-vector 1, strict-json 22, golden 26, acceptance-binding 94, transition-decisions 35, transition-scenarios 30). `m0/.venv/bin/python -m unittest discover -s m1/driver/tests`: 22 tests OK, 0 skipped (11 driver, 11 fixture integrity incl. schema validation of both transition bundles and every non-negative transition request, duplicate keys present in every DUPLICATE_KEY case, INVALID_JSON cases rejected by an RFC 8259 parse).
- **Unchanged suites after the schema edits:** M0 `pytest` 281 passed; audit 8 files current; docs check OK (91 Markdown, 17 schemas).
- **Cross-check after expectations were frozen (not used to set any expectation):** M0 oracle through the driver: canonical-vector 1/1, golden 26/26, strict-json 5/22, transition-decisions 13/35 with 6 protocol errors (the M0 adapter does not accept input_text). The M0 harness does not implement D-017; in tx-04 it matched the deploy approval rule for a release request.
- **Test fix during the run:** the integrity test first failed on `cj-15-nan` because Python's `json.loads` accepts `NaN`; the test now rejects non-standard constants. The fixture was not changed.

### E-020: M1-02 amendment 2 (D-018) checks; amendment-1 baseline CI

- **Amendment-1 baseline:** `a97e8f14beef94b0d24aef092405cffab044ec5f` pushed (fast-forward from `4a66364`); CI https://github.com/infinri/Writ/actions/runs/42329531037 success.
- **Amendment 2 (local, 2026-11-07):** exporter `--check` current, 7 sets, 291 cases. `m0/.venv/bin/python -m unittest discover -s m1/driver/tests`: 24 tests OK (11 driver, 13 fixture integrity, incl. schema validation of 50+ scenario requests, duplicate keys present in every DUPLICATE_KEY case, NaN and Infinity cases rejected by an RFC 8259 parse, no request repeats its kind unless expected invalid, no acceptance or permit detail passes through the external mapping). Same suite with system Python 3.12.3: OK.
- **Unchanged suites:** M0 `pytest` 281 passed; audit 8 files current; docs check OK (91 Markdown, 17 schemas).
- **Depth cross-check after freezing:** the 14 depth and width vectors run through the D-006 reference module (`m0/writ_m0/canonical.py`): 14/14 agree, including canonical bytes and digests.
- **M0 adapter after freezing (divergence record, nothing changed):** canonical-vector 1/1, golden 26/26, strict-json 12/37, transition-decisions 13/45 with 16 protocol errors (adapter lacks input_text support).

### E-021: M1-02 amendment 3 (D-019) checks; revision-3 CI

- **Revision 3:** `226e4d2af7637f2bbb2cc06ca6faf8175819cd63` pushed (fast-forward from `a97e8f1`); CI https://github.com/infinri/Writ/actions/runs/42330526265 success.
- **Amendment 3 (local, 2026-11-07):** 8 sets, 382 cases; exporter `--check` current; two consecutive exports byte-identical (combined digest `3aade516...a1764c4`). `unittest discover -s m1/driver/tests`: 26 tests OK with `m0/.venv` (jsonschema present: all 6 transition bundles schema-valid, 50+ scenario and effect requests valid, broker seals verify exactly when ingress is expected to accept) and OK with system Python. M0 `pytest` 281 passed; audit 8 files current; docs check OK.
- **Test fixes during the run:** the external-mapping rule test failed when broker codes used a second generic code (`EFFECT_REFUSED`); resolved by using one generic code `PROMOTION_REFUSED` (assumption C-1), not by weakening the test.
- **Not executed:** any implementation of `broker-scenario`, `transition-scenario`, `check-acceptance`, `external-refusal` or the D-017..D-019 kernel extension; an executing M0 comparison for broker scenarios.

### E-022: M1-03 Rust and M1-04 Go prototypes, functional parity (no benchmarks)

- **Built independently** by two agents from the M1 specifications, each without access to the other's code; verified by re-running in this session at commit `311561a` plus working tree.
- **Rust 1.99.1:** `cargo build --release --locked` clean; `cargo test --release --locked` 8 passed; deps `sha2 =0.10.8`, `hmac =0.12.1` (+9 transitive, `Cargo.lock`); 2,811 lines in `src/`.
- **Go 1.27.3:** `go vet` clean; `go test -count=1 ./...` ok (sjson, transition, broker; the agent also ran `-race`); standard library only; 3,138 non-test lines.
- **Parity driver, all 8 sets:** Rust 382/382, Go 382/382, ok and hard gates ok. `--repeat 5` on transition-scenarios and broker-scenarios: both 109/109 ok after the driver fix below.
- **Driver fix:** `--repeat` compared raw stdout, so `sc-64` (compare `one_of`, two valid serializations, both observed in 200 to 300 isolated runs by each prototype) could be flagged nondeterministic. Now per case: exact equality for ordinary cases, membership in `one_of` for varying cases. Driver self-tests OK.
- **Doc fix:** BROKER.md said `receipts: {key: status}`; all 43 fixtures use counts per status (reported by the Rust agent); wording corrected, fixtures unchanged.
- **Differential finding (spec gap, not covered by fixtures):** `external-refusal` for `REQUEST_SCHEMA_INVALID`, `DEADLINE_EXCEEDED`, `STATE_REVISION_CONFLICT`: Rust passes them through, Go maps them to `PROMOTION_REFUSED`. Second gap reported by the agents: a transition under a bundle without `workflow_protection` and with no transition statements (unreachable by fixtures): Rust `unavailable` / `PROTECTION_UNAVAILABLE`, Go `unavailable` / `POLICY_BUNDLE_REJECTED`. Both need an owner decision and new cases; neither prototype was changed.
- **Not executed:** decide-batch workload A; property and differential generators (M1-05); any benchmark (on battery, uncontrolled governor).

### E-023: D-020 corrections in both prototypes; published M1 baseline CI

- **Published:** `a459d4b7a9044a251491240f79d476ee4f76095e` (fast-forward from `226e4d2`, containing `311561a`, `fd9d3bb` and a CI commit); CI https://github.com/infinri/Writ/actions/runs/42388252282: all four jobs success; new jobs build Rust 1.99.1 and Go 1.27.3 independently and each passes the parity driver 382/382 with `--repeat 2`.
- **Before the D-020 fixes** (398 cases): Rust failed exactly tx-58, ext-revision-conflict, ext-state-revision-conflict, b-44..b-46; Go failed exactly tx-58, ext-revision-conflict, ext-request-schema-invalid, ext-deadline-exceeded, b-44..b-46. No other case changed outcome.
- **After** (separate agents per codebase, each blind to the other; re-verified in this session): Rust `cargo test --release --locked` 19 passed, driver 398/398 `--repeat 2`; Go `go vet` clean, `go test -race -count=1` ok (broker, kernel, refusal, sjson, transition), driver 398/398 `--repeat 2`.
- **Spec gap closed:** both implementations independently persisted the refusal count and permit id sequence across `restart_broker`; BROKER.md now states it.

### E-024: M1-05 property and differential testing; defect fixes in both prototypes

- **Harness** (`m1/props/`, commit `2bf136f`): seeded generators, spec-derived oracles independent of both prototypes, Rust/Go differential runner, minimizer, mutation smoke test (8 mutants, each caught by at least one oracle on both prototypes). Agent campaigns: seeds 20261107 (3,000 cases per family) and 2 (2,500), concurrency groups of 16/32/64 repeated 10 times; every observed concurrent outcome matched a serial order; `promote_concurrent` always committed exactly once.
- **Defects found:** Rust compared JSON objects order-sensitively (false refusals, fail closed); Go returned `NUMBER_NEGATIVE_ZERO` for `-00`/`-01` instead of `INVALID_JSON`; Go panicked on `promote_concurrent` count -1. Plus protocol gaps (envelopes, malformed scenarios, unknown op spelling, grant label reuse silently resetting uses, permit lookups), now clarified in M1-PARITY-SUITE (proposed, pending owner confirmation). Driver line splitting fixed (U+2028).
- **After fixes (separate agents per codebase; re-verified in this session):** Rust `cargo test` 31 passed; Go `go vet` clean, `go test -race` ok (6 packages); both: fixtures 398/398 `--repeat 2`, regressions 11/11; property campaigns seed 20261107 with 1,000 cases per family: decide, acceptance, refusal, transition, broker 0 failed checks and 0 mismatches; json campaign 0 invariant failures, 250 differential mismatches, all in one cluster: `decide` output shape after a strict-parse failure (Rust returns the three compared fields; Go a full Decision; both `invalid`/`REQUEST_JSON_INVALID`). Owner decision pending.
- **Independence note:** for two points the clarifications leave open (extra envelope members, unknown dependency names), the Go agent aligned its choice with the observed Rust binary; agreement there is not independent evidence.
- **Not tested:** persistent storage, process isolation, crash recovery of a real store, real signatures and key management, real host enforcement; benchmarks.

### E-025: D-021 conformance; full M1-05 verification (pre-push, local)

- **Prototypes** (separate agents, blind to each other; re-verified in this session with pinned toolchains): Rust `cargo test --release --locked` 32 passed, 0 failed; Go `go vet` clean, `go test -race -count=1 ./...` ok in 6 packages (25 top-level tests per the agent).
- **Fixtures:** 12 sets, 414 cases: Rust 414/414 and Go 414/414 with `--repeat 2`; property regressions 11/11 each.
- **Property campaigns** (`m1/props/run.py --campaign all --cases 2000 --repeats 5`): seed 20261107 and seed 2: every family PASS, 0 differential mismatches, 0 failed checks. Seed 20261107 checks: json 16,596; decide 8,413; acceptance 10,040; refusal 9,154; transition 71,196; broker 49,355. Broker: 400 concurrency groups (16/32/64 operations), 183 with more than one observed serialization, all within the permitted set; transition concurrency likewise. CI profile seed 20261107: ok.
- **Mutation smoke** (`m1/props/mutants/smoke.py --seed 7 --cases 300`): exit 0 for Rust and for Go (exit 0 only if all 8 mutants are detected: dup-accept, ignore-artifact, double-grant, redispatch, leak-codes, scope-allow, forbid-ignored, replay-commit).
- **Other suites:** M0 `pytest` 281 passed; audit 8 files current; docs check OK; fixture drift current (12 files); driver and fixture self-tests OK.
- **Earlier published CI:** `5f670ddbd6c47439449fd3b3c70f13e0cb62fe6c`, https://github.com/infinri/Writ/actions/runs/42394272671 (4 jobs success).
- **Process note:** the Go agent attempted `git rm` of dead code (`internal/sjson/envelope.go` `ReadID`), against its instructions; the permission system blocked it; nothing was deleted or committed. The dead function remains, marked unused.

### E-026: M1-06a benchmark harness (no measurements)

- **Harness** `m1/bench/` (standard library only): workload generators A, B, G, H with recorded seeds and input digests; cold start; concurrency at 1/10/100/1000 processes; peak RSS via `/usr/bin/time -v`; nearest-rank percentiles, mean, stdev, run-to-run variance; environment capture (power, every cpufreq policy, boost, EPP, platform profile, thermals at start and end, load, hardware, toolchains, Go build info, Cargo release profile, commit, binary digests); result format `writ-m1-bench/1` with schema and evidence-format records. Correctness re-checked inside every run (Rust equals Go; `bench-decide` equals `decide`; workload B groups are permitted serializations).
- **Prototype additions:** `bench-decide` in both (decision plus in-process `kernel_ns`), `decide-batch` alias in Rust (Go already had it). Rust `cargo test --release --locked` 33 passed; Go `go vet` clean, `go test -race -count=1 ./...` ok (6 packages).
- **Guard verified to refuse in this environment** (2026-11-07): exit 3, no file written; reasons: governor `powersave` on all 16 policies, uncommitted `m1/` changes, and at other moments load above 1.0 and CPU 65.75 to 71.6 C. Mains adapter was online at the time (it was offline earlier the same day; the guard reads it live). With the toolchains removed from PATH it also refused for unpinned `rustc`/`go`.
- **Dry run** (`--dry-run --seed 20261107`): exit 0, 0 correctness failures over 13 check kinds, `not_a_benchmark: true`, every timing and memory field `null`, schema-valid (jsonschema 4.23.0 from the m0 venv). Negative control: a Go wrapper rewriting one reason code was caught (5 differential failures, exit 4, `valid_for_comparison: false`).
- **Self-tests:** `python3 -m unittest discover -s m1/bench/tests` 15 tests OK (system Python and m0 venv, dry run included).
- **Regression after the prototype change:** both 414/414 fixtures `--repeat 2`; regressions 11/11; property CI profile seeds 20261107 and 1 `ok: true`; mutation smoke 8/8 detected for each; M0 281 passed; driver self-tests OK; fixture drift current; docs check OK.
- **No benchmark measurement was taken or reported.**
- **Found while probing (not fixed; owner decision):** a `decide` input whose harness fields are malformed (missing `now`, or a non-integer `state_revision`) is answered differently: Rust returns the three-field `invalid`/`REQUEST_SCHEMA_INVALID`; Go returns a full Decision with `decided_at` `0001-01-01T00:00:00Z`. Both accept `state_revision: -1`. No fixture covers this; D-021 settles only strict-parse failures.

### E-027: D-022 decide input validation

- **Commit** `a44c9cc`. 30 new fixtures (`protocol-decide` `p22-*`, AMENDMENT-6); no earlier expectation changed; 12 sets, 444 cases.
- **Before the fix:** Rust 28/33 in `protocol-decide` (5 failures: offset, fraction, lowercase `now`; revisions -1 and -(2^53-1)); Go 11/33 (22 failures: full Decisions with fabricated metadata, `decided_at` `0001-01-01T00:00:00Z`).
- **After:** both 444/444 `--repeat 2`; Rust 34 tests, Go race ok; property CI profile seeds 20261107 and 1 all PASS; new invariant `decide.d022_harness_fields_validated_before_decision` 247/247 per prototype at seed 20261107, 3,000 cases (generator balance 1,040 valid / 1,682 invalid per 3,000 draws, including 60 valid year-0000 timestamps); regressions 11/11; mutation smoke 8/8 each.

### E-028: D-023 SQLite ledger in both prototypes (correctness only, no measurements)

- **Commit** `935e4e3`. Shared SQLite 3.54.1 built by `m1/tools/install-sqlite.sh` (archive and `sqlite3.c` SHA3-256 verified, source id checked), revision 2 with `HAVE_USLEEP`. Both `proto sqlite-info`: 3.54.1, identical source id and compile options, settings `journal_mode=wal`, `synchronous=2`, `busy_timeout=5000`, `foreign_keys=1`, `trusted_schema=0`; no dynamic `libsqlite3` (`ldd`); Rust links the `-r2` library (build output), Go build info shows the `-r2` `CGO_LDFLAGS` and tags.
- **Re-verified in this session after the rebuild:** Rust `cargo test` 38 unit + 3 integration passed (includes T-reserve atomicity via injected trigger failure, restart persistence, second-process `--ledger-db` continuation, 20 x 64-thread contention on one grant and one key, schema drift, sqlite-info); Go vet clean, `go test -race` ok in 6 packages (broker package 75.9 s: SQLite fixtures, settings, real `BEGIN IMMEDIATE` busy check, atomicity, restart, 64-goroutine contention x 20, second-process test).
- **Fixtures:** in-memory 444/444 and SQLite ledger 444/444 (`m1/tools/sqlite-ledger-proto.sh`), both prototypes, `--repeat 2`.
- **Differential, SQLite against SQLite:** `m1/props/run.py --campaign broker --seed 20261107 --cases 1000 --repeats 3`, Rust SQLite versus Go SQLite: PASS, 1,119 cases, 23,089 checks, 0 mismatches, 0 failed checks (includes 300 concurrent groups checked against permitted serializations and restart transparency).
- **Other:** memory property CI profile seeds 20261107 and 1: 6/6 campaigns PASS each; regressions 11/11; mutation smoke exit 0 each; harness self-tests 15 OK; dry run 0 correctness failures over 22 check kinds including identical-engine check; M0 281 passed; drift, driver and docs checks OK.
- **Uncertain-effect rule:** in both prototypes' own tests a `crash_after_dispatch` receipt answers `RECONCILIATION_REQUIRED` after restart and in a second process, and the effect count does not grow.
- **Simulated, not verified:** effect adapter (consumed permits and effect rows in the same file; no real external exactly-once execution), HMAC fixture keys, crashes (early returns, no process kill), power-loss durability (not tested).

### E-029: CI run of `e1dc2e1`; D-024 storage failures; trimpath; CI SQLite linkage fix

- **CI run 42836769090 on `e1dc2e126bb9dd911662e058af0e1b16d71cc976`: FAILURE** (docs, M0 and M1 Go jobs success; M1 Rust and M1 property jobs failed). Cause: on the GitHub runner `libsqlite3-sys` found a system `sqlite3.pc` through pkg-config (it probes pkg-config before using `SQLITE3_LIB_DIR`) and linked the runner's SQLite 3.46.1 instead of the pinned 3.54.1. Caught by the Rust `sqlite_info` test and by the harness identical-engine check (dry run 2 correctness failures). The new SQLite steps did execute: Go 444/444 on SQLite; Rust-SQLite versus Go-SQLite broker campaign PASS (348 cases, 6,780 checks, 0 mismatches, while Rust ran 3.46.1). Locally this machine has no pkg-config, so the fallback used the pinned library.
- **Fix (not yet in CI):** `install-sqlite.sh` writes a `sqlite3.pc` for the pinned library; `env.sh` and CI set `PKG_CONFIG_LIBDIR` to it; each M1 build job asserts `proto sqlite-info` reports 3.54.1 with the pinned source id and no dynamic `libsqlite3`.
- **D-024 storage failures** (`m1/ledger_tests/test_storage_failures.py`, 13 tests, expectations from the specification): before fixes Rust failed 7 (all open failures answered per step; a ledger with `schema_version` 2 was accepted and **executed an effect**; read-only ledger used) and Go failed 3 (missing table accepted; read-only file used; audit failure replaced the original refusal code). Fixed by separate agents, each blind to the other. After: 13/13 for both.
- **Full re-verification (this session, after the fixes):** Rust 42 unit + 3 integration tests; Go vet clean, race tests ok in 6 packages; both `sqlite-info` 3.54.1 with identical settings; fixtures 444/444 in memory and on SQLite and regressions 11/11 for both; property CI profile seeds 20261107 and 1 6/6 campaigns each; Rust-SQLite versus Go-SQLite broker campaign (seed 20261107, 1,000 cases, 3 repeats) PASS 1,119 cases, 23,089 checks, 0 mismatches; mutation smoke exit 0 each; harness self-tests 15 OK (guard now also requires Go `-trimpath=true` and the D-023 tags); M0 281 passed; drift, driver and docs checks OK.
- **Reproducibility (independently re-run):** Go release build with `-trimpath` identical across two source paths (5,926,944 bytes); without `-trimpath` not identical; Rust identical (2,301,984 bytes).
- **Go `ReadID` removed:** referenced only by its own test; no interface; `go vet` and tests pass after removal.

### E-030: `3012940` CI green; M1-06d broker timing (no measurements)

- **CI run https://github.com/infinri/Writ/actions/runs/42851712662 on `30129408ae09cd8c60bfbe2ec310ad926422b327`: SUCCESS, all 5 jobs** (docs, M0 harness, M1 Rust parity, M1 Go parity, M1 property). Executed and passed, from the job logs: both build jobs printed `3.54.1 2026-10-16 14:45:20 555e07ef...` (linked-engine assertion, no dynamic `libsqlite3`); Rust tests 42 + 3 (include the `sqlite_info` compile-option checks); fixtures 444/444 in memory and on SQLite for each prototype; regressions 11/11; property CI profile both seeds; `m1/ledger_tests` 13 storage-failure tests OK (a schema-version-2 ledger and a missing table never run a step); Rust-SQLite versus Go-SQLite broker campaign PASS 348 cases, 6,780 checks, 0 mismatches; mutation smoke exit 0 for both (each mutant run reports FAIL, which is the intended detection); harness self-tests 15 and dry run 0 correctness failures (identical-engine, compile-option and settings check included).
- **M1-06d** (`bench-broker-scenario`, LEDGER-SQLITE.md "Per-operation timing"; separate agents, each blind to the other): both report parse, open, run and final time per line; per operation end-to-end, broker validation, kernel decision and key-lock wait; per transaction begin (lock acquisition), body and end (commit or rollback) with the outcome. Behaviour change in both: a resumed `reserved` receipt now ends its write-free reserve transaction with `COMMIT` instead of `ROLLBACK` (spec reading "commit nothing", made explicit). Go: adapter checks moved inside T-adapter as specified (a tampered permit under a failed `BEGIN` now answers `LEDGER_UNAVAILABLE`, fail closed). Rust compared `broker-scenario` output on all 52 broker fixture lines against a HEAD build: byte-identical for both ledgers.
- **Local verification after M1-06d:** Rust 45 + 3 tests; Go vet clean, race tests ok in 6 packages; both engines 3.54.1 with identical settings; fixtures 444/444 both ledgers and regressions 11/11 for both; `m1/ledger_tests` 16 tests OK (13 storage, 3 timing integrity: scenario byte equality on all broker fixtures for both ledgers, shape, part-sum consistency, specification transaction names and outcomes for more than 100 steps); property CI profile 6/6 campaigns per seed; Rust-SQLite versus Go-SQLite broker campaign PASS 1,119 cases, 23,089 checks, 0 mismatches; mutation smoke exit 0 each; harness self-tests 16 OK; dry run 0 correctness failures over 28 check kinds with timed B, B-mem, H, H-mem and all timing values `null`; M0 281 passed; drift, driver and docs checks OK.
- **No benchmark measurement exists.** Timing values seen in tests are not results.

### E-031: `6b7eab1` CI green; D-025 corrections (local)

- **Pre-push check of `6b7eab1`:** fast-forward from `3012940`; 21 files, no secrets, build outputs or `hook-audit/`. Independent behaviour check: both prototypes built at `3012940` (separate worktree) and at `6b7eab1`; 982 deterministic case outputs (every fixture set, both ledgers, concurrent scenarios excluded as nondeterministic) byte-identical.
- **CI run https://github.com/infinri/Writ/actions/runs/42857245639 on `6b7eab12a35a82d52e11493c9531268aaf697530`: SUCCESS, all 5 jobs.** Logs: Rust 45 + 3 tests; engine assertion `3.54.1 2026-10-16 14:45:20 555e07ef...` in both build jobs; 444/444 in memory and on SQLite for each prototype; `m1/ledger_tests` 16 tests OK; Rust-SQLite versus Go-SQLite PASS 348 cases, 6,780 checks, 0 mismatches; harness self-tests 16 and dry run 0 correctness failures.
- **D-025 baseline before changes (spec-derived tests):** fixtures: Rust and Go each failed the 3 new DEP-1 cases (missing dependency defaulted to `up`); `m1/ledger_tests/test_d025.py` (14 tests): 17 failures across both prototypes: corrupted stored grants answered `GRANT_MISMATCH` (fail-closed, wrong code; never an allow) in both; WAL applied before verification (S-1) in both; created file left behind (S-2) in both; Rust stderr contained the database path.
- **After (separate agents, each blind to the other):** Rust 54 + 3 tests (new: DEP-1, S-1, S-2, diagnostics, C-4 with 13 corruptions, resumed reservation writes nothing, adapter cannot begin answers `LEDGER_UNAVAILABLE` before permit checks, check order inside the transaction, failing adapter `COMMIT` leaves no consumed id or effect); Go vet clean, race tests ok in 6 packages (new: DEP-1 on both ledgers, S-1, S-2, diagnostics, C-4, resume writes nothing, begin-failure precedence, check order, commit failure).
- **Full re-verification (this session):** both engines 3.54.1 identical settings; fixtures **447/447** in memory and on SQLite and regressions 11/11 for both; `m1/ledger_tests` **30 tests OK** (13 storage, 3 timing integrity, 14 D-025); property CI profile 6/6 campaigns per seed; Rust-SQLite versus Go-SQLite broker campaign PASS 1,119 cases, 23,089 checks, 0 mismatches; mutation smoke exit 0 each; harness self-tests 16 OK; M0 281 passed; drift, driver and docs checks OK.
- **Recorded divergence (not compared):** for self-detected failures Rust stderr names the nearest SQLite code, Go prints `io`; both contain no path or credential.

### E-032: `68f4593` CI green; benchmark guard check

- **Pre-push:** fast-forward from `6b7eab1`; 28 files, no secrets, binaries or `hook-audit/`.
- **CI run https://github.com/infinri/Writ/actions/runs/42887453309 on `68f4593a513b0ab7b6ac206ef00c8f9d067d7660`: SUCCESS, all 5 jobs.** Logs: engine assertion `3.54.1 2026-10-16 14:45:20 555e07ef...` in both build jobs; Rust 54 + 3 tests; fixtures 447/447 in memory and on SQLite for each prototype; `m1/ledger_tests` 30 tests OK; Rust-SQLite versus Go-SQLite PASS 348 cases, 6,780 checks, 0 mismatches; driver self-tests 26 OK; harness self-tests 16 and dry run 0 correctness failures.
- **Benchmark guard (2026-11-10, clean tree at `68f4593`):** REFUSED, exit 3, nothing run: governor `powersave` on 16 of 16 policies; 1-minute load 1.81. Passed: mains online, boost 1 (recorded), CPU 51 C, clean `m1/` tree, pinned toolchains, Go `-trimpath` and tags.

### E-033: M1-06 readiness after the governor change (no measurement)

- **Readiness run 2026-11-10 at `8d1915c`:** governor `performance` 16/16; correctness re-verified (fixtures 447/447 in memory and on SQLite for both prototypes, `m1/ledger_tests` 30 OK, dry run 0 correctness failures).
- **Guard REFUSED** (exit 3, nothing run) for `bench.py` and for `collect.py build-times`: 1-minute load 1.51 to 1.65; CPU 67.25 to 68.6 C. Found: battery discharging while the online supply is USB-C 5 V / 3 A; the guard only checked the mains flag, so it now also refuses a discharging battery (2 new self-tests; harness self-tests 18 OK). No benchmark or build-time measurement was taken.

### E-034: `d9815f8` CI red; busy timeouts under 64-way contention (owner decision needed)

- **CI run https://github.com/infinri/Writ/actions/runs/42890081319 on `d9815f8ad2ed399c7a84a76a71b3b6dfcfbbf0b5`: FAILURE** (docs, M0, M1 Rust parity, M1 Go parity success; M1 property job failed). All other required checks in the logs passed (engine 3.54.1 in both jobs, 447/447 both ledgers both prototypes, 30 ledger tests). Failed step: Rust-SQLite versus Go-SQLite broker campaign, `FAIL broker: 348 cases, 6780 checks, 0 mismatches, 2 failed checks`: `broker.concurrent_group_equals_a_serialization` failed twice for Rust on group `brc-000022-x64` (64 concurrent operations): 13 and 7 operations answered `LEDGER_UNAVAILABLE`. Safety held in both (grant uses within limits; effects applied equal consumed permits). `d9815f8` changed only the guard and docs; the same code passed CI at `68f4593`, so the failure is timing dependent.
- **Local reproduction:** 40 groups of 64 concurrent executes x 5 repetitions (12,800 operations per prototype), SQLite ledger: Rust 89 and Go 6 operations refused `LEDGER_UNAVAILABLE`; stderr in every case `transaction reserve` `SQLITE_BUSY` (the `BEGIN IMMEDIATE` of T-reserve exceeded the 5 s busy timeout). Fail-closed per D-024 rule 2 and S-3; the M1-05 serialization oracle does not model it.
- **Not changed** (owner decision required: no oracle change, busy timeout fixed at 5 s by D-025).

### E-035: D-026 concurrency oracle and availability evidence (no benchmark)

- **Implementation:** both prototypes report `error` (`{"code": "busy" | "other", "at": "begin" | "body" | "end"}`) on every timing transaction (separate agents; Rust 58 + 3 tests, Go race tests ok; each with busy-at-begin, body, commit-failure and null-on-ordinary-refusal tests). Checker (`m1/props/wprops/c_broker.py`): per-operation classification into the five D-026 categories; category 3 removed as an aborted no-op only with busy-at-`BEGIN` evidence on the first transaction and, for operation-unique keys, no receipt, receipt-event or grant-use rows in the group's own ledger file; category 4 modelled as reserved (grant use kept, resumable), dispatched without effect, or dispatched with effect; anything else fails; every `LEDGER_UNAVAILABLE` must be matched; without evidence a storage refusal is never harmless; refusal accounting adjusted only by audit-failure evidence. SQLite campaigns run each concurrent group in its own process and file (`run.py --ledger sqlite`).
- **Negative tests** (`m1/props/tests/test_contention_oracle.py`, 14): accepted verified abort; rejected masked effect, masked receipt, masked grant use, refusal without evidence, unmatched refusal, dropped committed dispatch, effect without adapter, unexpected error kinds, inconsistent evidence, abort with ledger rows; partial-progress models. With the classifier deliberately broken (every operation an abort) 8 of the 14 fail. New mutant `masked-effect` (forged busy-abort evidence while the effect stays applied): caught by `broker.concurrent_group_equals_a_serialization` for both prototypes; mutation smoke exit 0 for both (9 mutants).
- **Verification (this session):** fixtures 447/447 in memory and on SQLite and regressions 11/11 for both; `m1/ledger_tests` 30 OK; oracle tests 14 OK; memory property CI profile 6/6 per seed; SQLite broker campaigns with 16/32/64-way groups: seed 20261107 CI size PASS (348 cases, 6,780 checks), seed 20261107 full PASS (1,119 cases, 23,089 checks), seed 2 full PASS (1,117 cases, 23,051 checks), 0 mismatches, 0 failed checks; harness self-tests 18 OK; M0 281; drift, driver and docs checks OK.
- **Availability evidence (uncontrolled environment, not a benchmark;** `m1/props/results/2026-11-10-sqlite-broker-*.json`): in these three campaigns 0 lock-timeout refusals for both prototypes (for example seed 20261107 full, 64-way: 6,144 operations each); maximum reserve `BEGIN` wait Rust 4.94 s, Go 4.14 s (limit 5 s); median reserve `BEGIN` wait Rust about 1.08 ms, Go about 0.012 ms. The earlier sample (E-034: Rust 89, Go 6 of 12,800) stays recorded; one timing-dependent sample is not a language result.
- **Investigation (diagnostic, `m1/props/results/2026-11-10-contention-diagnostic.json`, 20 groups x 64 per prototype):** transaction hold times are equivalent (commit about 0.46 to 0.50 ms median in both, `synchronous=FULL` fsync); the difference is connection scheduling: per-operation time outside timed parts (connection setup) median Rust 10.9 ms, Go 0.2 ms, and Rust opens worker connections before the start barrier so all 64 reach `BEGIN IMMEDIATE` together (thundering herd, SQLite busy-handler back-off starves some writers towards 5 s; 10 timeouts in this run), while Go opens them after the barrier, which staggers starts (0 here). Both readings fit LEDGER-SQLITE.md ("all start together"); not tuned; owner decision recorded as pending.

### E-036: `7a5a366` CI green with real lock-contention aborts

- **CI run https://github.com/infinri/Writ/actions/runs/42898828550 on `7a5a366d13f0b514a9723c74343c167e54f0aebd`: SUCCESS, all 5 jobs.** Logs: engine 3.54.1 in both build jobs; 447/447 in memory and on SQLite for both; `m1/ledger_tests` 30 OK; oracle negative tests 14 OK; SQLite 64-way campaign PASS 348 cases, 6,780 checks, 0 mismatches, 0 failed checks; mutation smoke exit 0 (`masked-effect` caught for both); dry run 0 correctness failures.
- **Availability in that run (CI runner, not a benchmark):** Rust 64-way: 1,408 operations, 619 successful, **2 lock-timeout refusals at `reserve`** (0.142%), maximum reserve wait 5.009 s, both verified as aborted no-ops by the D-026 checker; Rust 16/32-way and Go at every level: 0 (Go 64-way maximum wait 1.43 s).

### E-037: `988a840` CI green (docs-only commit, same code as `7a5a366`)

- CI run https://github.com/infinri/Writ/actions/runs/42902590125 on `988a840c2d69de2a371f13e07363845067b67282`: SUCCESS, all 5 jobs; SQLite 64-way campaign PASS 348 cases, 6,780 checks, 0 mismatches, 0 failed checks.

### Correction to E-035 (2026-11-10, found during the pre-compaction recovery check)

E-035 stated that Go opens concurrent-worker connections after the start barrier. **That is wrong.** The code shows: **Go** (`m1/go/internal/broker/scenario.go`, `concurrent`): the main goroutine opens all worker connections sequentially with `led.fork()` (about 0.2 ms each in the diagnostic), then spawns one goroutine per operation, then releases them with `close(start)`; **Rust** (`m1/rust/src/broker.rs`, `dispatch_step`): each of the worker OS threads opens its own connection in parallel (`group_connection`, about 11 ms each under that contention), then all wait on a `std::sync::Barrier` and are released together. **Both open before release and both add connection setup to the operation's `total_ns`.** The measured timing figures in E-035 stand; the explanation does not. Remaining hypothesis (not verified): Rust's barrier releases all OS threads at once, while released goroutines are scheduled by the Go runtime onto a bounded number of threads, which staggers the first `BEGIN IMMEDIATE`. The owner's D-027 decision was based on the incorrect explanation; its rule (open, configure and verify before release; no write before release; cold profile separate) remains valid, and whether release simultaneity must also be equivalent is an open question.

### Note on E-034 to E-036 (D-027, 2026-11-10)

The timeout and lock-wait figures in E-034, E-035 and E-036 are **preliminary and not directly comparable** between the prototypes (both opened worker connections before release but counted setup inside `total_ns` and differ in release mechanism; see the correction above). D-027 standardizes the primary profile (implemented locally, E-038); the figures are kept unchanged as recorded.

### E-038: D-027 concurrency profiles implemented (local, `52b31fe`; CI pending)

- **Scope:** spec amendment `m1-ledger-1a` (LEDGER-SQLITE.md "Concurrency profiles"), language-neutral tests `m1/ledger_tests/test_d027.py` (written before the implementations; failed against the pre-D-027 binaries with the option unknown), Rust and Go implementations by two separate agents that did not read each other's code, harness support (`run.py --concurrency-profile`, bench `B-cold`, `ready_ns`, `setup_ns`, release-delay samples).
- **Verified by Claude in this session (all exit 0):** Rust `cargo test` 64 + 3 passed; Go vet clean, `go test -race` ok in 6 packages; fixtures **447/447** in memory and on SQLite for each prototype, `--repeat 2`; regressions 11/11 each; `m1/ledger_tests` **37** OK with both binaries (13 storage, 3 timing integrity, 14 D-025, 7 D-027); `m1/props/tests` 14 OK; memory property CI profile seeds 20261107 and 1 PASS; Rust-SQLite versus Go-SQLite broker campaigns (`--cases 1000 --repeats 3`, 16/32/64-way groups): primary seed 20261107 PASS 1,119 cases, 23,089 checks; primary seed 2 PASS 1,117 cases, 23,051 checks; **cold** seed 20261107 PASS 1,119 cases, 23,089 checks; 0 mismatches and 0 failed checks in each; mutation smoke 9 of 9 mutants caught for each prototype (including `masked-effect`); bench self-tests OK; bench dry run 0 correctness failures (not a benchmark); M0 281 passed; drift, driver and docs checks OK.
- **Own-code tests (Required tests item 9):** Rust: event trace per ledger file proves every setup end precedes the first barrier pass and first `BEGIN` (primary, 17 workers x 10) and every setup and `BEGIN` follows release (cold); setup failure, settings read-back, memory timing, option parsing. Go: trace hook proves the same for 16 workers x 5 in each profile; setup failure, read-back, memory timing, option parsing. Both prototype agents also confirmed their tests fail when the profile logic is swapped (Rust reported doing this mutation check explicitly).
- **Availability (not correctness, not a benchmark; uncontrolled host: governor `powersave`, battery discharging, load about 2.2):** lock-timeout refusals 0 of 11,040 (seed 20261107) and 0 of 11,376 (seed 2) operations for each prototype in primary, and 0 of 11,040 for each in cold. Not comparable with E-034..E-036 (different profile rules and host state).
- **Implementation differences recorded, not tuned (D-027 does not prescribe them):** Rust performs worker setup in parallel inside the worker OS threads before `std::sync::Barrier::wait`; Go performs setup sequentially in the calling goroutine before creating the goroutines and `close(start)`. Release mechanisms unchanged in both (open owner question). Rust worker setup opens without create (a vanished file is a `worker` failure); stderr lines differ in form (`worker: <CODE>` versus `ledger error: worker <CODE>`), both path-free; `restart_broker` inside a worker still uses each prototype's existing reopen path.
- **Guarantee level:** Tested (unit or reference) only. D-027 is marked IMPLEMENTED only after CI is green on the pushed SHA.

### E-039: `41529d1` CI red (Rust D-027 own-code test hit a concurrent reconcile interleaving)

- CI https://github.com/infinri/Writ/actions/runs/42915265108 on `41529d1ee80e3a3ea69a0b677d63a781e163919a`: **FAILURE**. Jobs: M0 success; Go parity success; property and differential (including SQLite 64-way campaign) success; docs success; **Rust parity failure**: `broker::tests::d027_primary_sets_up_every_connection_before_any_transaction` failed at repetition 9 with stderr `transaction observe: SQLITE_CORRUPT` (63 passed, 1 failed).
- **Cause (verified, pre-existing, not D-027 specific):** that test put a `reconcile` in the same `concurrent` group as `execute` operations. On the SQLite ledger `reconcile` (no key lock) can commit between an `execute`'s T-dispatch and T-observe; the receipt becomes `unknown_reconcile`, T-observe's exactly-one-row update matches nothing, and the operation answers `LEDGER_UNAVAILABLE` with a D-026 category 5 error. **Reproduced in both prototypes by Claude** with a language-neutral script (8 executes plus 8 reconciles in one group, 100 runs each on the D-028 working-tree binaries): Go 7 of 100 (`transaction observe io`), Rust 0 of 100 in that run (Rust hit it in CI and in its implementer's test). Fails closed (no second effect, no success reported), but the outcome is not a serialization of whole operations. No fixture or generator produces it (`gen_concurrent` groups contain only executes). **Owner decision needed.**
- D-027 is therefore **not** marked IMPLEMENTED (CI not green on the pushed SHA).

### E-040: D-028 release protocol (local, uncommitted verification of the working tree; not published)

- **Scope:** decision D-028, spec amendment `m1-ledger-1b`, updated `m1/ledger_tests/test_d027.py` and `test_timing_integrity.py`, Rust and Go implementations by the same two independent agents, harness (`concurrent.start_skew_ns`, `concurrent.worker.release_delay_ns` from the prototypes).
- **Verified by Claude:** Rust `cargo test` 65 + 3 passed; Go vet clean, build ok, `go test -race`: cmd/proto, kernel, refusal, sjson, transition ok, **internal/broker FAIL**: `TestSQLiteContention` (pre-D-026 strict 64-way test) got `GRANT_EXHAUSTED 62, LEDGER_UNAVAILABLE 1, effect_completed 1` (one real 5 s busy timeout on `reserve`; the Go agent saw the same in two full runs and 3 of 3 passes alone); fixtures 447/447 both ledgers both prototypes; regressions 11/11 each; `m1/ledger_tests` 37 OK; `m1/props/tests` 14 OK; memory property CI profile seeds 20261107 and 1 PASS; SQLite campaigns primary seed 20261107 PASS 1,119 cases, 23,089 checks; primary seed 2 PASS 1,117, 23,051; cold seed 20261107 PASS 1,119, 23,089; 0 mismatches each; mutation smoke 9/9 each; bench self-tests OK; dry run 0 correctness failures; M0 281 passed; drift, driver, docs OK.
- **Protocols:** Rust: coordinator spawns all threads; each worker sets up, records `ready_at`, increments a ready count under a mutex and waits on a condition variable; coordinator waits for all ready, records ready and release, sets open and `notify_all`. Go: coordinator starts all goroutines; each sets up, records `ready_at`, `ready.Done()` (WaitGroup; also deferred via `sync.OnceFunc` so a panic cannot block), waits on `<-gate`; coordinator `ready.Wait()`, records ready and release, `close(gate)`. Both: setup concurrently inside workers in `primary`; failed setups count as ready; own trace tests with 60 s bounded waits (all workers exist and ready before release, setup and read-back before release, no BEGIN or open transaction before release, cold setup after release, failing setup does not deadlock or affect others). Go additionally probes from an outside connection that no write lock or transaction is held at release; Rust records a per-connection open-transaction check at ready.
- **Status:** not committed as verified; D-028 NOT IMPLEMENTED until the owner resolves `TestSQLiteContention` and the reconcile interleaving, and CI is green.

### E-041: origin of the CI `SQLITE_CORRUPT` (investigation, 2026-11-10)

- **Not file corruption.** Rust `m1/rust/src/sqlite.rs` `transition()` returned the synthetic code `SQLITE_CORRUPT` when the exactly-one-row receipt status update matched zero rows; Go reported the same condition as `io`. No SQLite call returned `SQLITE_CORRUPT`.
- **Mechanism:** `reconcile` (no key lock) committed `dispatched` to `unknown_reconcile` between an in-flight `execute`'s T-dispatch and T-observe; T-observe then matched no row. Reproduced in Go (7 of 100; then 1 of 150 with the disposable file kept): `PRAGMA integrity_check` `ok`, `foreign_key_check` empty, receipts 7 `observed_success` plus 1 `unknown_reconcile`, 8 effects, evidence `{"code": "other", "at": "body"}`, stderr `transaction observe io`. CI E-039 (`41529d1`, run 42915265108) and run 42930625767 on `a0769b6` (same Rust D-027 tests, primary and cold, `transaction observe: SQLITE_CORRUPT`; other 4 jobs success) are this mechanism.
- **Correction (D-029):** process-local coordination gate; zero-row transitions reported as `STATE_CONFLICT` (still fail closed, category 5).

### E-042: D-028 and D-029 verified locally (2026-11-10)

- **Scope:** D-028 release protocol (spec `m1-ledger-1b`), D-029 coordination gate, `STATE_CONFLICT`, decision B contention acceptance (spec `m1-ledger-1c`), language-neutral `m1/ledger_tests/test_d029.py` (written first; failed against the earlier binaries: Go in-process race and both mislabelled conflicts), both prototypes by the same two independent agents, harness.
- **Verified by Claude in one run:** Rust `cargo test` 70 + 3 passed; Go vet clean, `go test -race` ok in all packages (broker 113 s); fixtures **447/447** in memory and on SQLite for each prototype, `--repeat 2`; regressions 11/11 each; `m1/ledger_tests` **43** OK with both binaries (13 storage, 3 timing, 14 D-025, 7 D-027/D-028, 6 D-029); `m1/props/tests` 14 OK; memory property CI profile seeds 20261107 and 1 PASS; SQLite campaigns primary seed 20261107 PASS 1,119 cases, 23,089 checks; primary seed 2 PASS 1,117, 23,051; cold seed 20261107 PASS 1,119, 23,089; 0 mismatches; reconcile/execute adversarial probe (8 executes plus 8 reconciles per group, 200 runs per prototype): **0** storage refusals; mutation smoke 9/9 each; bench self-tests OK; dry run 0 correctness failures; M0 281 passed; drift, driver, docs OK. Agents additionally ran their full suites 3 times each (Rust 70 + 3; Go race ok) and mutation-checked the gate (removing the reconcile gate makes the D-029 tests fail, reproducing `STATE_CONFLICT`).
- **Gate:** Rust `RwLock<()>` per broker, Go `sync.RWMutex`; shared for `execute` (after step 1, before the key lock, released at return on every path including panic) and `present_permit` (before T-adapter); exclusive for `reconcile`; never re-entrant; gate wait inside `total_ns`.
- **Decision B:** both 64-way contention tests accept only category 3 aborts proven by evidence and database rows and category 4 failures modelled as the crash state they imply, with group-wide accounting (dispatch events, consumed permits and effects equal committed transactions); negative tests reject fabricated evidence (hidden effect, committed receipt, grant use, non-busy error, rows inconsistent with the claimed stage). Go forces each category 4 stage with an outside lock (`TestSQLiteContentionFailureAfterProgress`); Rust covers category 4 by unit tests only (no real busy failure occurred in its runs). No accepted timeouts occurred in these runs (availability: 0).
- **Cross-process demonstration (not a guarantee):** `test_d029` two-process run observed no conflict in either prototype (state conflicts 0, `unknown_reconcile` 0); safety properties held. Multi-process reconciliation safety is not claimed.
- **Guarantee level:** Tested (unit or reference) only.

### E-043: Benchmark readiness after the full-power charger (read only, 2026-11-10)

- AC `ACAD` online; USB-C source 004 also online (5 V, 3 A max); battery `BAT1` **Not charging** at 100%, `current_now` 0. Sampled every 30 s for about 20 minutes under the E-042 test load (1-minute load up to 3.94, hottest sensor up to 71.6 C): 40 of 40 samples Not charging, current 0, so **not discharging under that load**. The charger wattage itself is not reported by sysfs.
- Guard (`m1/bench/wbench/envinfo.py`, read only): **refuses**: governor `powersave` on 16 of 16 policies; CPU 63.25 C above the 60 C start limit (idle reading); uncommitted changes under `m1/` at that time. Load 0.81 passed (limit 1.0). Top consumers: `clickhouse-server` about 11% CPU, IntelliJ `java` about 10%, Chrome, Claude sessions. No setting changed, no process stopped.

### E-044: `d99faa2` CI green (D-027, D-028, D-029)

- CI https://github.com/infinri/Writ/actions/runs/42936763654 on `d99faa2` (m1/ tree identical to implementation commit `c5effec`): success, all 5 jobs (E-044). Rust 70 + 3 tests passed; Go parity and SQLite parity 447 fixtures; property CI profile seeds 20261107 and 1 PASS; SQLite 64-way campaign PASS 348 cases, 6,780 checks, 0 mismatches; M0 and docs success. The push contained `c5effec` and `d99faa2` together, so CI ran on the head; `git diff c5effec d99faa2` touches only `docs/`.

### E-045: `612e1f4` CI green; readiness recheck (read only, 2026-11-11)

- CI https://github.com/infinri/Writ/actions/runs/42938794312 on `612e1f4d081ae549fc0c769b0995b1ad2faed731`: success, all 5 jobs.
- Binaries rebuilt from the clean committed tree at `612e1f4` (Rust release, Go `-trimpath`). Guard (read only): power met (Not charging, 100%); `m1/` tree clean; toolchain pins pass. **Refuses:** governor `powersave` (16 of 16); 1-minute load 1.16 above 1.0 (IntelliJ `java` about 300% CPU at that moment, ClickHouse about 11%); CPU 64.4 C above the 60 C start limit. No setting changed, no process stopped, no measurement taken.

### E-046: official M1-06 measurement (2026-11-11)

- **Authorization and guard:** owner authorized measurement only if every guard passed. Read-only readiness at `138163d` (CI https://github.com/infinri/Writ/actions/runs/42939567262 success, all 5 jobs): governor `performance` 16 of 16, AC online, battery Not charging (current 0), load 0.57, CPU 55.9 C, clean `m1/` tree, toolchain pins and Go `-trimpath` pass. The harness re-checked and recorded `guard.passed: true`, `valid_for_comparison: true`, `mode: measure`.
- **Command:** `python3 m1/bench/bench.py --seed 20261111 --rust m1/rust/target/release/proto --go m1/go/proto --out m1/bench/results/2026-11-11-m1-06.json` (exit 0; "correctness failures: 0" over 35 check kinds). Binaries: Rust `sha256:e7afa261ff986312a1c4e08cb9b9abfe848336b9c8ce403582ea75b1ab7a1d85`, Go `sha256:4d853d89bb06fa63e274d36b4db82b31ebc5f2e0e3a7ff9254b4ba32e6077598`.
- **Files (local; `m1/bench/results/` is git-ignored by design, commit pending owner decision):** `2026-11-11-m1-06.json` SHA-256 `13ceb932d486d63d006709f684789a4db9cd30250a038e734c5f96536fbca93b` (two absolute binary paths replaced by repo-relative paths after the run; nothing else changed), raw samples `2026-11-11-m1-06.json.samples.json.gz` SHA-256 `c93804134e982bee3ba3d8d9e0a297c1d379da6464d35a2a9f0f069ceb9cedb8`.
- **Environment recorded:** start CPU 52.25 C, load 0.53; end CPU 76.5 C, load 1.84; on AC, battery Not charging at start and end; background IntelliJ, ClickHouse, Chrome (not stopped).
- **Results:** [M1-08-SCORECARD](M1-08-SCORECARD.md). Lock-timeout refusals 0 of 10,000 for each prototype in B (primary) and B-cold.

### E-047: M1-07 build timings (2026-11-11)

- `python3 m1/assess/collect.py build-times --runs 5 --out m1/assess/results/2026-11-11-build-times.json` (guard passed inside the command: CPU 55.75 C, load 0.67, governor `performance`, battery Not charging, clean tree at `138163d`). `GOCACHE` was set to a dedicated scratch directory so the command's `go clean -cache` did not clear the owner's global Go cache.
- Clean build (median of 5): Rust 2.65 s (2.62 to 2.77), Go 3.69 s (3.67 to 3.87). Incremental after appending a newline to a kernel source: Rust 1.42 s (1.40 to 1.70), Go 0.08 s; the Go figure reflects an unchanged compiled package and a cached link, so the incremental case is not comparable (methodology limitation recorded in the scorecard).

### ENT-E01: enterprise readiness documentation verification (2026-11-10 America/Chicago)

- **Scope:** ENT-00 planning only; ENT-OWNER-01, readiness plan, master plan/index/task links and recovery state. Runtime, schemas, fixtures, original ignored benchmark evidence and ADR acceptance statuses unchanged.
- **Baseline:** fresh checkout `a91af1d775d59fab932a03506ea36384642cdae0`; exact-SHA CI run https://github.com/infinri/Writ/actions/runs/42943984397 verified all 5 jobs success. This does not establish CI for the new documentation commit.
- **Executed before changes:** `python3 scripts/check-v2-docs.py`: OK (90 Markdown, 15 HTML, 17 schemas, 3 packs, 3 tasks, 2 negative fixtures).
- **Executed after changes:** `python3 scripts/check-v2-docs.py`: OK (91 Markdown, 15 HTML, 17 schemas, 3 packs, 3 tasks, 2 negative fixtures); link and schema checks passed. `git diff --check`: exit 0. Documentation scope and additions reviewed for secrets, personal paths and accidental build/audit artifacts.
- **Limit:** no control implementation, original-host measurements, evidence preservation, security examination or certification occurred in this documentation session.

### E-048: Go "process overhead" explained (M1-08 confirmation B, 2026-11-11)

- **Verified explanation: an instrumentation boundary, not slower Go and not a defect.** Go's `RunProfile` recorded `run_ns` (`tm.Done()`) before the deferred `b.led.close()`, so closing the SQLite ledger (final WAL checkpoint with `synchronous=FULL`, removal of `-wal`/`-shm`) fell outside `run_ns`; Rust measured `run_ns` after the broker and its connection were dropped. Only SQLite workloads showed it (H 0.72 ms per line, B about 1 ms per scenario; in-memory workloads none).
- **Evidence (diagnostics, not measurements, governor `powersave`):** the same 1,000-line H input from a file: Go wall 6.76 s with in-process 6.09 s (untimed 0.67 s); Rust wall 7.06 s, in-process 7.04 s (untimed 0.02 s). A disposable Go copy closing before `tm.Done()`: untimed 0.032 s and 0.034 s (two runs), wall unchanged. After the fix in the repository: Go wall 6.75 s, untimed 0.036 s; Rust wall 6.92 s, untimed 0.031 s. Excluded: serialization, output buffering, process start and shutdown, garbage collection, the harness.
- **Correction:** spec clarified (`run_ns` ends after the ledger is closed); Go fixed by its agent with `TestRunCoversLedgerClose` (fails on the old order); Rust already conformed, close made explicit with `run_timed_returns_with_the_ledger_closed`. The E-046 B and H per-operation figures are unaffected; only Go's `process.overhead_ns` and `run_ns` attribution in E-046 were inequivalent.

### E-049: chaos workload E with real process kills (M1-08 confirmation D, 2026-11-11)

- `m1/ledger_tests/test_chaos.py` (language-neutral): seeded SIGKILL of a prototype process running approvals, sequential and 8- to 32-way concurrent executes on a file ledger; restart in a new process on the same file; retry every request, reconcile, retry again; database invariants after the kill and after the restart; a ledger made unwritable after a kill fails closed with the file unchanged.
- **Campaign (seed 20261111, 50 iterations per prototype):** Rust 38 killed mid-run (12 finished first), receipts at kill 4,010 observed, 9 dispatched, 4 reserved; Go 49 killed (1 finished first), 3,851 observed, 5 dispatched, 4 reserved. After restart: every dispatched receipt answered `RECONCILIATION_REQUIRED` (Rust 9, Go 5), every reserved receipt resumed without a second grant use (4 and 4), every observed one replayed. **0 invariant violations**: integrity ok, no grant overuse, no duplicate dispatch, no effect without a consumed permit, no second effect per permit, no `STATE_CONFLICT` or `CORRUPT`; fail-closed on an unwritable ledger for both.
- **Not covered:** power loss and OS crash durability; a reconcile in another live process (D-029 gate is process-local; unresolved architectural risk); real external effects.

### E-050: verification of the M1-08 confirmation code (2026-11-11, commit `16640b3`)

- Rust 71 + 3 passed; Go vet clean, race tests ok in all packages; fixtures 447/447 both ledgers both prototypes; regressions 11/11; `m1/ledger_tests` 45 OK (includes chaos default 8 iterations); oracle tests 14 OK; memory property CI seeds 20261107 and 1 PASS; SQLite campaigns primary seeds 20261107 and 2 and cold PASS (1,119 / 1,117 / 1,119 cases, 0 mismatches); reconcile/execute adversarial probe 0 refusals (200 runs each); mutation smoke 9/9 each; bench self-tests and dry run OK; M0, drift, driver, docs OK.
- Harness additions: per-run environment samples every 5 s (temperature, current and average clocks against the hardware maximum, governor, load, battery state; `thermal_throttle` sysfs is not exposed on this AMD host, recorded as such); binary paths recorded repo-relative; M1-07 incremental build now a real edit (private function with a per-run constant in the kernel source) with per-run isolated `GOCACHE` and proof of recompilation (plumbing checked with timings discarded).

### E-051: rebase onto the enterprise-readiness commit; real-edit build validation (2026-11-11)

- **Rebase:** local `9a689c8`, `16640b3`, `567e088` rebased onto remote `a8d1a2d` (enterprise security and SOC 2 readiness docs) as `37db802`, `bf94cdf`, `4002c0c`; originals kept on branch `backup/m1-08-pre-rebase-567e088`; conflicts only in SESSION-HANDOFF and TEST-EVIDENCE, resolved keeping both the enterprise-readiness direction and the M1-08 results; `git diff` of `m1/` between original and rebased: empty.
- **Real-edit build experiment revised (owner: an unused helper is insufficient):** the incremental edit now inserts, as the first statement of a decision-path function (Rust `kernel::bundle_sha`, Go `kernel.LoadPolicy`), a store of a distinctive per-run constant (`0x5752495401070000 + run`) into a retained sink (`std::hint::black_box`; a package variable). Plumbing check (guard stubbed in-process, timings discarded, not a measurement): both languages recompiled the kernel (cargo `Compiling writ-m1-proto`; `go build -v` lists the kernel package), executable hash changed, the constant's 8 bytes absent before and **present in the relinked executable after**; both edited binaries passed **447/447** fixtures (behaviour preserved). Go builds use an empty isolated `GOCACHE` per run; Rust clean builds an empty target directory with registry sources cached.

### E-052: official M1-06 repeat runs, seeds 20261121 and 20261131 (2026-11-12)

- **Guard:** owner set governor `performance`; a read-only watcher re-checked the full guard immediately before each run (log in the seed 20261121 bundle): run 2 CPU 46.1 C, load 0.55, PASS; run 3 CPU 51.5 C, load 0.83, PASS; clean tree at `bbdd092` (CI run 43085390484 green, all 5 jobs); binaries rebuilt there (Rust `sha256:486a1827...`, Go `sha256:15c7b885...`, full hashes in the manifests). Each result records `guard.passed: true`, `valid_for_comparison: true`, 0 correctness failures over 35 check kinds; 85 and 90 in-run environment samples (peak CPU 78.4 C and 82.1 C; battery Charging throughout; no throttle counter on this host).
- **Commands:** `python3 m1/bench/bench.py --seed 20261121 ...` and `--seed 20261131 ...` (outputs under ignored `m1/bench/results/`; tracked bundles `m1/bench/evidence/2026-11-12-seed-20261121/` and `...-20261131/` with SHA-256 manifests, commit `a1b153c`).
- **Repeatability (three run sets, CV across sets):** kernel p50 Rust 13.0 to 13.2 µs (CV 0.7%), Go 22.3 to 22.4 µs (0.2%); kernel p99 Rust 19.0 to 20.8 µs, Go 90.3 to 98.6 µs; kernel throughput Rust 32.3k to 32.8k/s, Go 23.6k to 24.5k/s; RSS, cold start, process concurrency and hostile input reproduce within 1 to 8%; SQLite broker useful throughput equal within 3%; storage refusals 0 in 60,000 B and B-cold operations per prototype. Full table: [M1-08-SCORECARD](M1-08-SCORECARD.md).

### E-053: M1-07 real-edit build timings (2026-11-12)

- `python3 m1/assess/collect.py build-times --runs 5 --out m1/assess/results/2026-11-12-build-times-real-edit.json` at `bbdd092` (guard passed inside the command: CPU 48.25 C, load 0.78, governor `performance`; `GOCACHE` pointed at a scratch directory; every Go build used its own empty isolated cache).
- Clean median: Rust 2.50 s (2.48 to 2.59), Go 3.54 s (3.47 to 3.68). Real-edit incremental median: Rust 1.31 s (1.28 to 1.38), Go 0.49 s (0.48 to 0.50). All 10 incremental runs proved recompilation and relinking (kernel recompiled, executable hash changed, edit constant absent before and present after). Supersedes the newline-only incremental figures of E-047.

### E-054: order-controlled process-concurrency correction (2026-11-12)

- **Finding (final evidence check):** the process-concurrency workload ran Rust first at every level, once, in all three earlier run sets; those multi-process results are **not order-controlled** (original bundles unchanged).
- **Correction:** `992383a` (CI run 43245077338 green, all 5 jobs): two paired rounds per level on identical inputs (Rust then Go, Go then Rust), per-round order and samples recorded, regression test `ConcurrencyOrder`; sizes, levels, correctness checks and guards unchanged.
- **Re-measurement (only this workload, `--workloads concurrency`, seeds 20261111, 20261121, 20261131; each after a full guard pass: CPU 54.3 / 54.0 / 50.3 C, load 0.81 / 0.72 / 0.57):** 0 correctness failures (Rust equals Go in every round). Medians of 6 samples: Rust/Go 1.48x (k = 1), 1.76x (10), 2.17x (100), 2.33x (1,000); every Rust sample above every Go sample at each level; order effect about 3 to 5%. Each run lasts about 5 s, so the 5 s monitor recorded only 1 or 2 in-run samples (start and end recorded; CPU reached about 80 C). Bundle with SHA-256 manifest: `m1/bench/evidence/2026-11-12-concurrency-order-corrected/` (`34450c6`). The Go binary hash differs from the `bbdd092` build only through Go's embedded VCS stamp (source diff empty; Rust byte-identical).

### E-055: final evidence check before ADR-0002 acceptance (2026-11-12)

- Publication: `a1b153c`, `841eabf` pushed (fast-forward from `bbdd092`); CI run 43240338281 success, all 5 jobs on `841eabf`.
- All three run-set bundles: every file matches its manifest SHA-256; the samples hash recorded inside each result matches; seeds match; clean tree; guard enforced and passed; `valid_for_comparison` true; 0 failures in 35 check kinds; successful throughput equals successful operations divided by wall time (refusals separate). Binaries rebuilt at `bbdd092` reproduce the recorded run 2 and 3 hashes exactly.
- Order alternation verified in code for A, B, B-cold, B-mem, G, H, H-mem and cold start; process concurrency was not (E-054, corrected).
- Build evidence: all 10 real-edit runs recompiled and relinked (constant absent before, present after, hash changed); edited binaries re-checked: 447/447 fixtures on both ledgers for both languages.
- Kept explicit: the E-048 Go `run_ns` boundary correction and run set 1's non-comparable Go `run_ns` and process overhead in SQLite workloads.

### E-056: M2-00 to M2-06, production kernel `writ-kernel` (2026-11-12)

- **M2-00, M2-01** (`80cc88d`, CI run 43253469776 green, 6 jobs including the new "M2 production kernel"): root workspace (member `kernel` only; `m0/`, `m1/` excluded and still building), `rust-toolchain.toml` 1.99.1, `unsafe_code` forbidden, unwrap/expect/panic/todo/dbg denied; dependency gate `kernel/tools/check_deps.py` (10 locked packages: `sha2` 0.10.8 and its transitive set, identical to the M1-reviewed versions; licences approved; OSV 0 advisories 2026-11-12; SBOM artifact; negative tests). `hmac` forbidden (kernel key-free).
- **M2-02 to M2-05** (implementation agent, verified by Claude): strict JSON and canonical form, typed contracts with tenant and project binding, decision evaluation, transition decisions, acceptance structure checks, external refusal mapping; 4,848 lines in `kernel/src`. Claude re-ran: `cargo fmt --check` OK; `cargo clippy -D warnings` clean; `cargo test`: 49 unit, 10 integration (8 fixture sets, manifest partition, bundle contract), 10 property-style tests, all pass; **320/320 kernel fixture cases** (canonical-vector 1, strict-json 37, golden-decisions 26, transition-decisions 62, acceptance-binding 97, external-refusal 63, protocol-decide 33, protocol-acceptance 1), each twice with identical output; no unwrap, expect, panic or print outside test modules; no file, network, environment, clock or key code in `kernel/src`. The other 127 M1 cases (broker-scenarios 46, protocol-broker 9, transition-scenarios 66, protocol-transition 6) are **M3 obligations**, not claimed.
- **M2-06 differential:** through the dev-only adapter, the M1 parity driver gives 320/320 with `--repeat 2`; M1 property campaigns `json`, `decide`, `acceptance`, `refusal` (CI profile, seeds 20261107 and 1) against the M1 Rust prototype and against the M1 Go prototype: decide, acceptance and refusal **0 mismatches** (about 3,600 cases and 16,000 checks per seed per reference); json mismatches are only the classified groups (750 M3 cross-command cases, 1 adapter-envelope case), gate `kernel/tools/differential.py` reports 0 unclassified problems.
- **Readings needing owner confirmation (no fixture disagreed; recorded in `kernel/FIXTURE-MAP.md`):** `revoked_evaluator_ids` use the evaluator-name pattern (stricter, as both prototypes); `external-refusal` on unparsable input answers `INPUT_JSON_INVALID` and a missing or non-string code maps to `PROMOTION_REFUSED`; extra members in the `decide` and `check-acceptance` command inputs are ignored.

## Not yet executed

- SR-06 live-host experiments (0 of 69; sandbox torn down) and full SR-06 acceptance; rule semantic review (M0-01d); independent human golden review (M0-05a).
- Targeted M1-08 repeatability, Go-overhead investigation, compiler-relevant incremental builds and real interruption workload; official first measurements already recorded in E-046/E-047.
- Enterprise control implementation, operational-effectiveness evidence and independent SOC 2 examination (ENT-01..ENT-08).
