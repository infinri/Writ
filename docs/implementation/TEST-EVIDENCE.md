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

## Not yet executed

- SR-06 live-host experiments (0 of 69; sandbox torn down) and full SR-06 acceptance; rule semantic review (M0-01d); independent human golden review (M0-05a).
- Any Rust or Go prototype build, and any performance benchmark (toolchain smoke builds only, E-016).
