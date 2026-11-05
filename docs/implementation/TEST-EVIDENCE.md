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

## Not yet executed

- Hook failure posture on a live host (M0-01e); rule semantic review (M0-01d).
- Any Rust or Go build, and any performance benchmark.
