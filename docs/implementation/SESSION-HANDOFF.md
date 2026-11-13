# Session handoff

Read this first after compaction or in a new session, then [WORK-STATE](WORK-STATE.md). This file is the authoritative recovery entry point. Only the section "Current recovery state" gives current instructions; everything under "Historical record" is preserved evidence and superseded instructions, never a current next step.

## Current recovery state (checkpoint 2026-11-13, after M3-07; M3 in progress, NOT complete; M3-08 blocked on owner decisions)

### 1. Repository and CI (verify on recovery with the commands in section 7)

| Item | State |
|---|---|
| Branch | `v2/architecture-rebuild` only; never `main`; fast-forward pushes only, never force |
| Verified code baseline | `6be251c89c7c52484b89f6dc14bb663c315d436c` (M3-05 transition service, D-036): CI https://github.com/infinri/Writ/actions/runs/43465393620 **success, all 6 jobs** (including the isolated process-kill step) |
| Earlier verified commits | D-035 `5fdcd30` run 43346923290 (6/6 success); docs `154be77` run 43347554447 (6/6 success); M3-04 code `8ead392` run 43340059204 (6/6 success) |
| M3-07 commits | `e52e6ca`..`7eb995f` (runner, process lifecycle, review fixes and tests, M3-08 gate preparation). CI on `7eb995f` (run 43474169870) **failed** in the production workspace job: the CI host leaks a descriptor without close-on-exec, so the sandbox refused every job (E-071). Fixed in `146260e` (tests and kill tests run through `m3/tools/run-with-clean-fds.sh`; the CI host leaked pipe descriptors 142 and 145): run https://github.com/infinri/Writ/actions/runs/43475168083 **success, 6/6**. **Verified code baseline: `146260e485795865e4b5cc72a1b0da0cb8e48778`.** The documentation commit recording this has its own CI run |
| Previous checkpoint | `82971fd` (docs) passed run https://github.com/infinri/Writ/actions/runs/43466394626, 6/6 |
| Required CI | workflow `.github/workflows/v2-docs.yml` ("V2 documentation integrity"), 6 jobs: docs and schemas; production Rust workspace (fmt, clippy, build, tests, isolated process-kill step, kernel parity, M2 evidence, dependency gate); M0 harness; M1 Rust parity; M1 Go parity; M1 properties and differential |
| Not required CI | "Publish Writ V2 architecture website": the site assembly job succeeds; the Pages deploy job fails on every push with `Branch "v2/architecture-rebuild" is not allowed to deploy to github-pages due to environment protection rules`. Not a code failure; deployment protections are not to be changed |
| Working tree | clean except untracked `docs/audit/m0/hook-audit/` (private owner material: never stage, publish, summarize or modify); no unpublished commits at checkpoint time |

### 2. Status by task (details: [TASKS](TASKS.md), evidence [TEST-EVIDENCE](TEST-EVIDENCE.md))

- **Done:** M3-PLAN, M3-00 (workspace, per-crate dependency gate), M3-01 (single-owner SQLite ledger: canonical identity, owner lock, fencing epoch, create versus open), M3-02 (sealed ingress, use-time re-decision, policy gate, permits, `PERMIT_STALE`, contract semantic validation, trusted time), M3-03 (authenticated approvals, atomic single-use grants; E-064), M3-04 (execute lifecycle, separate effect store, stage evidence, kills; E-065, E-066), D-035 (E-067).
- **M3-05 implemented under D-036 (E-068):** verified locally (310 tests passed 3 times, kill tests 3/3, parity 320/320, gate 0 problems); exact-SHA CI on `6be251c` green (run 43465393620, 6/6). The 66 transition-scenarios and 6 protocol-transition cases are not yet run against production code (M3-08).
- **M3-06:** contracts (evaluator registry entry, runner attestation, EvaluationRecord scope) approved in D-034 with semantic validation in `broker::contracts`; the authenticated AcceptanceRecord contract `authenticated-acceptance-record/1` added and implemented under D-036. Remaining integration obligations, each open: (a) runner attestations produced by a real runner and verified end to end (M3-07); (b) EvaluationRecord scope documents are validated but not yet ingested or bound at use time (evaluations are carried inside the authenticated record); (c) registry publication by authorized humans (M6; today an operator interface with test authorities); (d) evidence-digest resolution against a catalog (M7); (e) human criteria refused until authenticated signoff (M4); (f) the proposed schemas in `docs/contracts/proposed/` are not promoted to `docs/contracts/` and the authenticated record has no JSON Schema file yet.
- **M3-07 implemented (E-069):** runner as a broker effect (`runner.evaluate`) producing `runner-attestation/1` under the registry-named runner key from trusted state; process-wide lifecycle gate plus unlock-before-close ownership locks (E-066 resolved in production code); `LocalProcessSandbox` (bounded, descriptors checked) is uncontained, so `RunnerRequirements::production()` refuses every job until a contained sandbox exists (M5); the uncontained profile is compiled only for tests. Reviewed by an independent subagent; findings fixed or documented (M3-ACCEPTANCE-CONTRACTS "Mechanical runner").
- **M3-08 in progress, BLOCKED ON OWNER DECISIONS (E-070):** command layer `persistence/sqlite/examples/m3_line_adapter.rs` and gate `m3/closure/` built; GATE FAIL: 59 exact, b-36 matched, 4 unexplained (sc-31, sc-52, sc-53, sc-54), 63 not drivable; promotion-permit cases sc-09, sc-13, sc-18..sc-25, sc-36, sc-38; owner questions OQ-1..OQ-12 in [M3-08-CLOSURE-MAP](M3-08-CLOSURE-MAP.md). Differential and property gate, chaos and storage-failure suites not started.
- **M3 is not complete.**

### 3. Decisions in force (do not request again)

D-030 Rust selected, **ADR-0002 ACCEPTED** (scoped to the trusted kernel and broker); D-031 kernel readings; D-032..D-034 M3 decisions; **D-035** (reservation retention, effect-store ownership, permit presentation, failure precedence, approver provenance) and **D-036** (authenticated records, no promotion permits, no client-asserted reconciliation, b-36 exception, trusted workflow inputs) approved and implemented. ADR-0008 **APPROVED IN PRINCIPLE**. Release and production **NO-GO**. Stop for any new semantic or trust-boundary decision.

### 4. Schemas, migrations and source map

| Store | Version | Entry points | Files |
|---|---|---|---|
| Ledger | `writ-ledger-5` | `SqliteLedger::create` (new), `SqliteLedger::open` (current version only, never initializes or upgrades), `SqliteLedger::upgrade` (explicit operator upgrade of exact `writ-ledger-3` or `writ-ledger-4`, keeps every row) | `persistence/sqlite/src/schema.sql` (frozen `writ-ledger-3` base), `schema_v4.sql`, `schema_v5.sql`, `lib.rs` |
| Effect store (controlled test adapter) | `writ-effects-2` | `SqliteEffectStore::create`/`open` bound to a `LedgerId`; `writ-effects-1` refused | `persistence/sqlite/src/effects.sql`, `effects.rs` |
| Approver provenance | `writ-provenance-1` | grants without it are non-executable | `broker/src/grants.rs` |
| Authenticated record | `authenticated-acceptance-record/1`, purpose `writ.acceptance-record.v1` | `acceptance::verify_record` | `broker/src/acceptance.rs` |
| Runner store | `writ-runner-1` | `SqliteRunnerStore::create`/`open` bound to a `LedgerId` | `persistence/sqlite/src/runner.sql`, `runner_store.rs` |

Broker modules (`broker/src/`): `ingress`, `authorize`, `policy` (gate), `clock` (trusted time), `permit`, `keys` (ingress, permit, acceptance-record and runner keys), `grants` (approvals, T-reserve, effect prerequisites), `execute` (lifecycle, reconcile, present_permit), `effects` (adapter interface), `acceptance` (records, attestations, use-time checks), `workflow` (promotion, administration, sources, snapshot store), `runner` (interfaces, `adapter`), `process` (lifecycle gate, `OwnerLock`, `LocalProcessSandbox`), `contracts` (semantic validation), `store` (ledger traits), `refusal`. SQLite crate (`persistence/sqlite/src/`): `lib.rs` (ledger, grants, execute transactions), `workflow.rs` (workflow transactions, prerequisite check), `effects.rs`, `runner_store.rs`; tests `grant_tests`, `execute_tests`, `d035_tests`, `transition_tests`, `runner_tests`, `kill_tests` (ignored in the parallel run), `tests/initialization.rs`, `tests/ownership.rs`, `tests/process_lifecycle.rs`. M3-08 gate: `persistence/sqlite/examples/m3_line_adapter.rs`, `m3/closure/`.

### 5. Production obligations not met (never claim otherwise)

A contained runner sandbox (filesystem, network, process-tree and resource containment; production configuration refuses every job until then; M5); process-group kill of job descendants (needs a dependency decision); sweeping leftover runner work directories and an operator path for `started` runs; acceptance service and its key provisioning (M4/M6); production key management for ingress, permit, acceptance-record and runner keys (M5, M9); a service-controlled immutable snapshot store (INV-40; interface only); authenticated operators and approvers (M4; interfaces with test authorities only); evidence catalog (M7); signed acceptance policies and registry publication (M6); real external effect adapters, each with its own concurrency, replay, authorization and recovery contract; power-loss durability (M9); multi-owner coordination (PostgreSQL profile, M10); real-host verification (M5, SR-06); independent human review of golden cases and fixtures (REL-01) and of the 288 V1 rules (M6-RULES).

### 6. Authorized next action and continuation checklist

Authorized (no new approval needed): remaining M3-06 integration that does not need a decision, M3-08 work that does not depend on OQ-1..OQ-12, reviewed fast-forward pushes and exact-SHA CI verification. **Blocked:** M3-08 closure needs the owner's answers to OQ-1..OQ-12 ([M3-08-CLOSURE-MAP](M3-08-CLOSURE-MAP.md)).

1. Recover: run section 7 "state" commands; confirm HEAD equals `origin/v2/architecture-rebuild`; confirm the M3-07 commit's CI by run id (record it in section 1 and E-069 if not yet recorded).
2. If the owner answered OQ-1..OQ-12: record a decision (D-037) and implement it in the command layer and `m3/closure/exceptions.json` (exact cases and outputs, negative tests), then rerun the gate: `cargo build --release --locked -p writ-ledger-sqlite --example m3_line_adapter && python3 m3/closure/check.py --proto target/release/examples/m3_line_adapter --report <out.json>`.
3. Independently of the answers: M3-06 (b) ingest EvaluationRecord scope documents and bind them at use time if it needs no new semantics (otherwise ask); (f) a JSON Schema for `authenticated-acceptance-record/1` in `docs/contracts/proposed/` (promotion out of `proposed/` is an owner action); the M3-08 differential and property gate (like M2-06) and chaos and storage-failure suites against the production broker.
4. After each step: update TEST-EVIDENCE, TASKS, this file and WORK-STATE; commit explicit paths only; fast-forward push; verify CI on the exact SHA.

### 7. Commands

```bash
# state
git branch --show-current && git fetch -q origin && git status --short
git log --oneline -8 && git rev-parse HEAD origin/v2/architecture-rebuild
gh run list --branch v2/architecture-rebuild --workflow v2-docs.yml --limit 5
# production workspace (pinned toolchain and SQLite from m1/tools/env.sh)
source m1/tools/env.sh
cargo fmt --all --check && cargo clippy --workspace --all-targets --locked -- -D warnings
m3/tools/run-with-clean-fds.sh cargo test --workspace --locked   # sandbox needs no inherited fd above 2 (E-071)
m3/tools/run-with-clean-fds.sh cargo test -p writ-ledger-sqlite --locked --lib kill_tests -- --ignored --test-threads=1   # alone, single-threaded (E-066)
python3 -m unittest discover -s m3/closure   # M3-08 gate self-tests (the gate itself currently fails, E-070)
python3 kernel/tools/check_deps.py --osv --sbom /tmp/sbom.json
cargo build --release --locked --example m1_line_adapter && python3 m1/driver/parity.py --proto "$PWD/target/release/examples/m1_line_adapter" --manifest m1/fixtures/manifest.json --repeat 2 --sets canonical-vector,strict-json,golden-decisions,transition-decisions,acceptance-binding,external-refusal,protocol-decide,protocol-acceptance
python3 -m unittest discover -s kernel/tools && python3 scripts/check-v2-docs.py
```

### 8. Requires owner approval

Any new semantic or trust-boundary decision; any change to D-017..D-036 or frozen fixture expectations; changing deployment or branch protections; anything touching `main`, V1 trees, the M0 harness or `hook-audit/`; marking ADR-0008 ACCEPTED; any release or production action; benchmarks or CPU and system settings; stopping the owner's processes. Reviewed fast-forward pushes within M3 are authorized.

## Historical record (superseded; not current instructions)

The sections below are preserved as written at earlier checkpoints (mostly 2026-11-10 and 2026-11-12). Statements such as "ADR-0002 OPEN", "Next task: M2 completion", the M1-08 directive, `a91af1d` as published baseline and "choosing Rust or Go" requiring approval are superseded by section 1 to 8 above.

### Historical A. Repository state (verify with the commands in section H)

| Item | State |
|---|---|
| Branch | `v2/architecture-rebuild` (never `main`) |
| Verified published baseline | `aff6ce3` docs checkpoint, CI run 43343727179 **success, all 6 jobs**; code `8ead392`, CI run 43340059204 **success, all 6 jobs** (incl. isolated process-kill step); D-035 `5fdcd30`, CI run 43346923290 **success, all 6 jobs** |
| Last code commit | `8ead392` (test isolation fix); M3-04 `3c92d42`, M3-03 `844407e`, trusted time `1e193cc` |
| This update | M3 authorized (bounded); [M3-START-PLAN](M3-START-PLAN.md); use git and CI to verify publication |
| Working tree | untracked `docs/audit/m0/hook-audit/` (private owner material: never stage, publish or summarize); backup branch `backup/m1-08-pre-rebase-567e088` is local only |
| Milestone | **M3 in progress, not complete** (M3-00..M3-05 and D-035 implemented; M3-07 next, then M3-08). Release and production **NO-GO** |

Commit chain since M1-05 (all published): `9e6f9f1` M1-06a harness; `a44c9cc` D-022; `935e4e3` D-023 SQLite ledger; `e1dc2e1` M1-07 report (CI failed: system SQLite linked); `3012940` D-024 plus CI fix; `6b7eab1` M1-06d broker timing; `68f4593` D-025; `8d1915c` docs; `d9815f8` guard refuses discharging battery (CI failed: busy timeouts, E-034); `1994b67` docs; `7a5a366` D-026; `988a840` docs; `d9054c9` docs checkpoint; `52b31fe` D-027; `41529d1` docs (CI red, E-039); `a0769b6` docs (CI red, same cause); `c5effec` D-028, D-029; `d99faa2` docs (CI green, E-044); `612e1f4` docs; `138163d` readiness docs; `a91af1d` official results and scorecard.

### Historical B. Architecture in one page

- **Kernel:** pure, deterministic, key-free authorization (no I/O, injected time); strict JSON (D-017), D-006 canonical form; decides allow, deny, require_approval, invalid or unavailable with matched rule ids ([V2-ARCHITECTURE](../../V2-ARCHITECTURE.md)).
- **Broker:** the only path to governed effects. Adapter-sealed ingress (D-010), use-time re-decision, approval grants from authenticated humans on a separate channel (single use, bound to the full request, D-009), permits bound to the full request fingerprint and bundle (D-012), idempotency receipts and reconciliation; effect adapter simulated in M1.
- **SQLite ledger (D-023..D-027):** one pinned SQLite 3.54.1 static library linked by both prototypes; WAL, `synchronous=FULL`, `busy_timeout=5000`, `BEGIN IMMEDIATE` logical transactions (reserve, dispatch, adapter, observe, approve, reconcile, audit), per-key locks, durable receipts, grant uses, refusal audit, simulated adapter tables; fail closed on storage failures with one sanitized response ([LEDGER-SQLITE](../../m1/fixtures/broker/LEDGER-SQLITE.md), [BROKER](../../m1/fixtures/broker/BROKER.md)).
- **Acceptance Gate Integrity:** protected workflow transitions need a trusted, version-bound acceptance record bound to tenant, project, task, workflow, transition, artifact snapshot id and digest, policy version and state revision, checked by the broker plus kernel structure checks; promotion separate from publish or deploy; model-assisted findings advisory only ([spec](../architecture/acceptance-gate-integrity.md), ADR-0008 **APPROVED IN PRINCIPLE**).
- **Model independence:** model output is untrusted; kernel and broker import no model SDK; packs never grant permissions.
- **Bake-off:** equivalent Rust and Go prototypes from shared language-neutral specifications, implemented by separate agents that never read each other's code; ADR-0002 **OPEN** until final owner decision after targeted M1-08 confirmation; Rust is the provisional leader and preferred direction.
- **Writ Control Center:** roadmap (D-015, ADR-0007, `docs/control-center/`); not started.

### Historical C. Completed work (do not repeat)

M1-00 toolchains; M1-01 parity driver; M1-02 fixtures (amendments 1..7, 12 sets, **447 cases**, all PENDING INDEPENDENT REVIEW); M1-03 Rust and M1-04 Go prototypes; M1-05 property, metamorphic, concurrency and differential harness; M1-06a guarded benchmark harness; M1-06b D-022; M1-06c D-023 SQLite ledger; M1-06e D-024 storage failures; M1-06d broker timing (`bench-broker-scenario`); M1-06f D-025 corrections; M1-06g D-026 concurrency oracle; D-027..D-029 implemented and CI green (E-044); official M1-06 and M1-07 measurements recorded (E-046/E-047). M1-07 facts collected ([report](M1-07-BUILD-ASSESSMENT.md)); newline-only incremental timings are invalid for comparison and require replacement.

### Historical D. Decisions in force ([DECISION-LOG](DECISION-LOG.md))

| Decision | Status |
|---|---|
| D-001..D-013 | accepted (M0) |
| D-014 | M0 development GO (16 risks open) |
| D-015 Control Center, D-016 Acceptance Gate Integrity | accepted; ADR-0008 APPROVED IN PRINCIPLE (not ACCEPTED) |
| D-017 strict JSON, identifiers, exact transitions, permits, completeness | implemented, tested |
| D-018 final semantics A1..A8; D-019 protection in bundle, effect prerequisites; D-020 refusal mapping, transition support | implemented, tested |
| D-021 strict protocol responses | implemented, tested |
| D-022 decide input validation | implemented, tested, approved (D-024) |
| D-023 SQLite ledger required for workload B | implemented, tested |
| D-024 trimpath, Linux x86_64 only, storage failures fail closed, `ReadID` removed | implemented, tested |
| D-025 DEP-1, C-1..C-6, C-4 grant integrity, adapter precedence, S-1, S-2, sanitized stderr | implemented, tested |
| D-026 verified lock-contention aborts, availability separate from correctness | implemented, tested, CI green at `7a5a366` |
| D-027 standardized concurrent-worker start | implemented (`52b31fe`), refined by D-028 |
| D-028 equivalent release protocol | implemented (`c5effec`), CI green E-044 |
| D-029 process-local coordination gate, `STATE_CONFLICT`, verified contention acceptance | implemented (`c5effec`), CI green E-044; process-local only |

### Historical E. Historical and current evidence ([TEST-EVIDENCE](TEST-EVIDENCE.md))

Current runtime/measurement records: E-042..E-047; published baseline CI `a91af1d`, run 42943984397 green. ENT-E01 records only readiness documentation checks. The next paragraph is historical evidence at `7a5a366`, not a current benchmark status.

At `7a5a366` (CI run 42898828550, success, all 5 jobs; and locally, E-035): Rust 58 unit + 3 integration tests; Go vet clean, race tests ok in 6 packages; both link SQLite 3.54.1 (source id `2026-10-16 14:45:20 555e07ef...`), identical settings; fixtures **447/447** in memory and on SQLite for each prototype; regressions 11/11; `m1/ledger_tests` **30** OK (13 storage failures, 3 timing integrity, 14 D-025); D-026 oracle negative tests **14** OK; memory property CI profile seeds 20261107 and 1; Rust-SQLite versus Go-SQLite broker campaigns with 64-way groups PASS (CI 348 cases, 6,780 checks; local 2,584 cases, 52,920 checks; 0 mismatches); mutation smoke 9 of 9 mutants caught for each (including `masked-effect`); harness self-tests 18 OK; benchmark dry run 0 correctness failures; M0 281 passed; drift, driver and docs checks OK. Availability (preliminary, not comparable before D-027): E-034 Rust 89 / Go 6 timeouts of 12,800; E-036 CI Rust 2 of 1,408 at 64-way. Guarantee level: **Tested (unit or reference) only**; no test result is a real-host security certification. **At that historical checkpoint no official measurement existed; E-046/E-047 now record the official measurements.**

### Historical F. Next task: M2 completion (see the top of this checkpoint and TASKS rows M2-00..M2-06)

Plan: [M2-START-PLAN](M2-START-PLAN.md). The completed M1-08 confirmation record follows for reference.

**Status (2026-11-12): ALL ITEMS DONE** (2: E-052; 4: E-053; 7: scorecard final draft with recommendation). Earlier status (2026-11-11): item 1 evidence bundle DONE (`m1/bench/evidence/2026-11-11-seed-20261111/`); item 3 DONE (E-048: instrumentation boundary, Go fixed); item 5 DONE (E-049: real SIGKILL campaign, 0 violations); item 4 tooling in place, edit must change retained compiled code before official timings (owner 2026-11-11); items 2 and 4 timings and item 7 pending: approved seeds 20261121 and 20261131, guard must pass before each run.

The owner supplied an explicit M1-08 review directive in session, then preferred Rust and delegated enterprise readiness planning. Preserve its bounded scope:

1. `a91af1d` is already published and all 5 jobs passed on that exact SHA; do not push it again or force-push. Inspect the original local tree and remote before any integration or later push. Preserve the full original evidence; inspect sizes/content/sensitivity and create a separate sanitized reproducibility bundle or archive/manifest while keeping raw local output ignored. Retain seed 20261111, raw samples/distributions, commit/binary/input/fixture hashes, toolchains/flags, environment, commands, run order, correctness and refusal counts. Never discard the only complete original copy.
2. Repeat the controlled comparison with at least two additional recorded seeds and independent run sets, alternating Rust/Go order. Retain raw data and run-to-run variation for kernel latency/throughput, memory, cold start, process throughput and SQLite broker metrics. Useful throughput counts successful operations separately from refusals.
3. Investigate the Go duplicates-workload process overhead with equivalent instrumentation and unchanged workloads. Distinguish serialization/buffering, startup/shutdown, instrumentation boundaries, GC, connection cleanup, defects and harness cost; report a verified cause or explicit unresolved limitation.
4. Replace newline-only build timings with a reproducible behavior-preserving, compiler-relevant edit in disposable clean source trees and isolated caches. Verify actual recompilation/relinking and retain repeated timings and commands.
5. Implement targeted M1 chaos/interruption coverage using disposable resources and real child-process termination/restart where feasible. Verify no grant overuse, unauthorized effect or duplicate dispatch after uncertainty; durable receipt/reconciliation and fail-closed storage behavior. A simulated early return is not a process kill. Keep the process-local reconciliation limitation unresolved and do not claim real-host certification.
6. Every new official comparative run requires full environmental readiness at its start: temperature, clocks/throttling indicators, load, power/battery and governor, plus clean recorded source/toolchains/binaries. Stop if any guard fails. Never bypass safeguards, change privileged settings or stop user applications. The existing owner directive authorizes these bounded runs conditional on the guard; no repeated permission request is needed within that scope. Do not run them on a replacement cloud host as if they reproduced the original environment.
7. Update the scorecard with correctness/security, repeated measurements, resources, dependencies, reproducibility, maintenance, broker behavior and explicit limitations. Separate measurements, facts and judgments; recommend Rust versus Go once targeted evidence is complete. Await the final owner decision before accepting ADR-0002. ADR-0008 remains APPROVED IN PRINCIPLE and production/release NO-GO.

Enterprise control implementation (ENT-01..ENT-08) belongs to later milestones and does not replace these investigations. Update handoff/evidence before compaction. This documentation checkout has not performed new comparative measurements, real process kills or preservation of the original ignored evidence.

### Historical G. Unverified security obligations and blockers (never claim otherwise)

Independent human review of the 26 golden cases and all M1 fixtures (REL-01); review of the 288 V1 rules (M6-RULES); SR-06 real-host verification (M5-SR06; adversarial trials only on a VM or separate machine); 16 D-014 risks open; authenticated approval service and evaluator issuance, attestation and signatures; real snapshot immutability and credential isolation; trusted production clock and key management; real process-kill and power-loss recovery (ledger crash tests are early returns); real external exactly-once effects (adapter simulated); remaining adversarial and host-bypass tests; real-host enforcement. ADR-0002 OPEN; ADR-0008 APPROVED IN PRINCIPLE; release and production NO-GO.

**Historical pre-measurement environment ([M1-06-READINESS](M1-06-READINESS.md)), not current readiness:** reading 2026-11-10: full-power charger not connected (battery discharging on a 5 V / 3 A USB-C supply, about 15 W); governor back to `powersave` (owner restored it); 1-minute load 3.52 (IntelliJ, 22 Docker containers, Chrome, Claude sessions); CPU 60 C (needs a fresh check). The guard (`m1/bench/wbench/envinfo.py`) must pass; never bypass or relax it; never change CPU settings or stop the owner's processes.

### Historical H. Commands

```bash
# state
git branch --show-current && git fetch -q origin && git status --short
git log --oneline -8 && git rev-parse HEAD origin/v2/architecture-rebuild
gh run list --branch v2/architecture-rebuild --workflow v2-docs.yml --limit 3
# build and unit tests
source m1/tools/env.sh
(cd m1/rust && cargo build --release --locked && cargo test --release --locked)
(cd m1/go && T="libsqlite3 sqlite_omit_load_extension" && go vet -tags "$T" ./... && go build -trimpath -tags "$T" -o proto ./cmd/proto && go test -race -count=1 -tags "$T" ./...)
R=$PWD/m1/rust/target/release/proto; G=$PWD/m1/go/proto
# fixtures, both ledgers
for b in $R $G; do python3 m1/driver/parity.py --proto $b --manifest m1/fixtures/manifest.json --repeat 2; WRIT_PROTO=$b python3 m1/driver/parity.py --proto $PWD/m1/tools/sqlite-ledger-proto.sh --manifest m1/fixtures/manifest.json --repeat 2; python3 m1/driver/parity.py --proto $b --manifest m1/props/regressions/manifest.json; done
# language-neutral and oracle tests
WRIT_RUST=$R WRIT_GO=$G python3 -m unittest discover -s m1/ledger_tests -v
python3 -m unittest discover -s m1/props/tests -v
# properties (memory and SQLite) and mutation smoke
python3 m1/props/run.py --profile ci --seed 20261107 --rust $R --go $G --out /tmp/p.json
python3 m1/props/run.py --campaign broker --ledger sqlite --seed 20261107 --cases 300 --repeats 2 --rust $R --go $G --out /tmp/ps.json
python3 m1/props/mutants/smoke.py --real $R --seed 7 --cases 300 --out /tmp/sr.json; python3 m1/props/mutants/smoke.py --real $G --seed 7 --cases 300 --out /tmp/sg.json
# harness and other checks
WRIT_BENCH_RUST=$R WRIT_BENCH_GO=$G python3 -m unittest discover -s m1/bench/tests
python3 m1/bench/bench.py --dry-run --seed 20261107 --rust $R --go $G --out /tmp/dry.json
(cd m0 && .venv/bin/python -m pytest -q); python3 m1/fixtures/export_m0_fixtures.py --check; python3 -m unittest discover -s m1/driver/tests; python3 scripts/check-v2-docs.py
# environment (read only)
cat /sys/devices/system/cpu/cpufreq/policy*/scaling_governor | sort | uniq -c; cat /sys/class/power_supply/ACAD/online /sys/class/power_supply/BAT1/status /proc/loadavg
```

### Historical I. Requires owner approval

Every push (fast-forward only, never force); deleting files; changing frozen fixture expectations; any new decision or change to D-017..D-027; benchmark or build-time measurement runs outside the bounded M1-08 directive (guard must always pass); CPU or system settings; stopping the owner's processes; choosing Rust or Go; marking ADR-0002 or ADR-0008 ACCEPTED; anything touching `main`, V1 trees, the M0 harness or `hook-audit/`.

### Historical standing facts (2026-11-10)

1. **Objective:** Writ V2, a model- and domain-agnostic governance, retrieval and memory platform ([master plan](MASTER-IMPLEMENTATION-PLAN.md)).
2. **Key files:** [M0-GO-NO-GO](M0-GO-NO-GO.md), [M0-GO-SIGNOFF-PACKAGE](M0-GO-SIGNOFF-PACKAGE.md), [TASKS](TASKS.md), [DECISION-LOG](DECISION-LOG.md), [TEST-EVIDENCE](TEST-EVIDENCE.md), [SR-06-TRIAL-PLAN](../audit/m0/SR-06-TRIAL-PLAN.md), `m0/` harness, `docs/contracts/`.
3. **Do not reverse:** D-001..D-013 (accepted); D-014 ACCEPTED 2026-11-07 (development only; release standard revisited at release milestone); D-015, D-016. In particular D-008 precedence and scoped ids, D-009 no grant inheritance, D-010 adapter-sealed broker ingress with a key-free kernel, D-011 owner policy decisions (not review verdicts), D-012 full-request fingerprints and project-scoped evidence, D-013 artifact and signoff scope.
4. **Open gates:** 26 golden cases PENDING INDEPENDENT REVIEW (REL-01 before release); 288 rules unreviewed (M6-RULES before import); SR-06 NOT EXECUTED (M5-SR06; adversarial trials only on a VM or separate machine). 16 risks open with recommended dispositions, owner signature pending.
5. **Known items:** V1 undeclared deps PyYAML and tokenizers (R-V1PKG); V1 ONNX Runtime telemetry (R-V1TEL, V2-TEL); golden case 24 misnamed; RM-01 composite effects; production key management, signed policies, trusted clocks, TOCTOU and authenticated signoff provenance assigned to M3 to M6.
6. **Prohibited until further decisions:** public release, production deployment, privileged production workloads, unverified security or enforcement claims.
