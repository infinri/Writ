# Session handoff

Read this first after compaction or in a new session, then [WORK-STATE](WORK-STATE.md).

## Current checkpoint (2026-11-10; D-027 implemented locally, push and CI pending)

**This is the authoritative recovery entry point. It is not authorization to do anything beyond what is listed under "Authorized next task"; re-establish other authorizations from the owner's newest message.**

**After compaction (state 2026-11-10, night):** D-027 published (`52b31fe`), CI red on `41529d1` and `a0769b6` from the concurrent reconcile interleaving (E-039, E-041; not file corruption). Owner decisions D-028 (equivalent release protocol) and D-029 (process-local coordination gate, `STATE_CONFLICT`, decision B contention acceptance) are implemented and **verified locally (E-042)** and committed as one implementation commit plus a docs commit; both are approved for push. **Resume at:** confirm the push and CI on the exact implementation SHA; only if green mark D-027, D-028, D-029 IMPLEMENTED (DECISION-LOG, TASKS, this handoff, WORK-STATE). If CI is red, investigate; never relabel or hide errors. Then benchmark readiness (E-043: power now met; governor `powersave` and CPU temperature still refuse; owner configures). Never claim multi-process reconciliation safety. Do not restart M1 development or repeat any completed task in section C.

### A. Repository state (verify with the commands in section H)

| Item | State |
|---|---|
| Branch | `v2/architecture-rebuild` (never `main`) |
| Published remote HEAD | `988a840c2d69de2a371f13e07363845067b67282` (docs: E-036). CI https://github.com/infinri/Writ/actions/runs/42902590125 **success, all 5 jobs** (same code as `7a5a366`; SQLite 64-way campaign PASS 348 cases, 6,780 checks, 0 mismatches) |
| Last code commit | `7a5a366d13f0b514a9723c74343c167e54f0aebd` (D-026), CI https://github.com/infinri/Writ/actions/runs/42898828550 **success, all 5 jobs** |
| Local, unpushed | `d9054c9` (docs checkpoint), `52b31fe` (D-027 code: spec `m1-ledger-1a`, tests, both prototypes, harness), then the D-027 docs commit; push needs owner confirmation |
| Working tree | only untracked `docs/audit/m0/hook-audit/` (owner material: never stage, publish or summarize) |
| Milestone | **M1** under D-014 (development only). M0 closed for development only. Release and production **NO-GO** |

Commit chain since M1-05 (all published): `9e6f9f1` M1-06a harness; `a44c9cc` D-022; `935e4e3` D-023 SQLite ledger; `e1dc2e1` M1-07 report (CI failed: system SQLite linked); `3012940` D-024 plus CI fix; `6b7eab1` M1-06d broker timing; `68f4593` D-025; `8d1915c` docs; `d9815f8` guard refuses discharging battery (CI failed: busy timeouts, E-034); `1994b67` docs; `7a5a366` D-026; `988a840` docs.

### B. Architecture in one page

- **Kernel:** pure, deterministic, key-free authorization (no I/O, injected time); strict JSON (D-017), D-006 canonical form; decides allow, deny, require_approval, invalid or unavailable with matched rule ids ([V2-ARCHITECTURE](../../V2-ARCHITECTURE.md)).
- **Broker:** the only path to governed effects. Adapter-sealed ingress (D-010), use-time re-decision, approval grants from authenticated humans on a separate channel (single use, bound to the full request, D-009), permits bound to the full request fingerprint and bundle (D-012), idempotency receipts and reconciliation; effect adapter simulated in M1.
- **SQLite ledger (D-023..D-027):** one pinned SQLite 3.54.1 static library linked by both prototypes; WAL, `synchronous=FULL`, `busy_timeout=5000`, `BEGIN IMMEDIATE` logical transactions (reserve, dispatch, adapter, observe, approve, reconcile, audit), per-key locks, durable receipts, grant uses, refusal audit, simulated adapter tables; fail closed on storage failures with one sanitized response ([LEDGER-SQLITE](../../m1/fixtures/broker/LEDGER-SQLITE.md), [BROKER](../../m1/fixtures/broker/BROKER.md)).
- **Acceptance Gate Integrity:** protected workflow transitions need a trusted, version-bound acceptance record bound to tenant, project, task, workflow, transition, artifact snapshot id and digest, policy version and state revision, checked by the broker plus kernel structure checks; promotion separate from publish or deploy; model-assisted findings advisory only ([spec](../architecture/acceptance-gate-integrity.md), ADR-0008 **APPROVED IN PRINCIPLE**).
- **Model independence:** model output is untrusted; kernel and broker import no model SDK; packs never grant permissions.
- **Bake-off:** equivalent Rust and Go prototypes from shared language-neutral specifications, implemented by separate agents that never read each other's code; ADR-0002 **OPEN** until M1-08; no winner chosen.
- **Writ Control Center:** roadmap (D-015, ADR-0007, `docs/control-center/`); not started.

### C. Completed work (do not repeat)

M1-00 toolchains; M1-01 parity driver; M1-02 fixtures (amendments 1..7, 12 sets, **447 cases**, all PENDING INDEPENDENT REVIEW); M1-03 Rust and M1-04 Go prototypes; M1-05 property, metamorphic, concurrency and differential harness; M1-06a benchmark harness (guarded; dry run only); M1-06b D-022; M1-06c D-023 SQLite ledger; M1-06e D-024 storage failures; M1-06d broker timing (`bench-broker-scenario`); M1-06f D-025 corrections; M1-06g D-026 concurrency oracle; M1-06h D-027 code and local verification (`52b31fe`, E-038; only push and CI remain). M1-07 facts collected ([report](M1-07-BUILD-ASSESSMENT.md)); M1-07 build timings **not** done (guarded).

### D. Decisions in force ([DECISION-LOG](DECISION-LOG.md))

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
| **D-027 standardized concurrent-worker start** | **APPROVED, IMPLEMENTED LOCALLY** (`52b31fe`, E-038; CI pending); premise corrected after approval (see DECISION-LOG D-027 correction) |

### E. Verified evidence ([TEST-EVIDENCE](TEST-EVIDENCE.md), latest E-037)

At `7a5a366` (CI run 42898828550, success, all 5 jobs; and locally, E-035): Rust 58 unit + 3 integration tests; Go vet clean, race tests ok in 6 packages; both link SQLite 3.54.1 (source id `2026-10-16 14:45:20 555e07ef...`), identical settings; fixtures **447/447** in memory and on SQLite for each prototype; regressions 11/11; `m1/ledger_tests` **30** OK (13 storage failures, 3 timing integrity, 14 D-025); D-026 oracle negative tests **14** OK; memory property CI profile seeds 20261107 and 1; Rust-SQLite versus Go-SQLite broker campaigns with 64-way groups PASS (CI 348 cases, 6,780 checks; local 2,584 cases, 52,920 checks; 0 mismatches); mutation smoke 9 of 9 mutants caught for each (including `masked-effect`); harness self-tests 18 OK; benchmark dry run 0 correctness failures; M0 281 passed; drift, driver and docs checks OK. Availability (preliminary, not comparable before D-027): E-034 Rust 89 / Go 6 timeouts of 12,800; E-036 CI Rust 2 of 1,408 at 64-way. Guarantee level: **Tested (unit or reference) only**; no test result is a real-host security certification. **No official benchmark measurement exists.**

### F. Authorized next task: D-027 (M1-06h); steps 1 to 5 DONE locally, step 6 pending

**Requirements (DECISION-LOG D-027):** primary profile: for every `concurrent` group, open, configure and verify every dedicated worker connection before releasing the common start barrier; release only when all are ready; no write transaction (`BEGIN IMMEDIATE`) before release; connection setup excluded from operation `total_ns` and reported separately. Supplemental cold-connection profile: worker connections created after release (setup inside the operation), selected explicitly and reported separately. Keep busy timeout 5 s, 64-way stress tests, D-026 checks. Do not tune either prototype beyond this rule.

**Plan:**

1. Spec first (Claude): amend LEDGER-SQLITE.md "concurrent" and "Per-operation timing": name the profiles (proposal: default primary; supplemental via an explicit flag such as `--concurrency-profile cold`, plus a `concurrent` step timing member for connection setup, for example `setup_ns` per operation and `barrier_release_ns` per group); record exact field names before any code.
2. Language-neutral tests (Claude): extend `m1/ledger_tests/test_timing_integrity.py` (or add `test_d027.py`): in the primary profile every worker's first transaction `begin_ns` starts after the barrier (setup outside `total_ns`, setup field present for every operation); the cold profile reports setup inside `total_ns`; outputs unchanged on all broker fixtures; D-026 categories unchanged.
3. Prototypes (two separate agents, each blind to the other, briefs as for D-026). **Current code (verified 2026-11-10; corrects E-035):** Rust `m1/rust/src/broker.rs` `dispatch_step` (line 683) spawns one OS thread per operation; each thread calls `group_connection` (line 666, `l.connect()`) in parallel, then `barrier.wait()` (`std::sync::Barrier`, line 688), and adds setup to `total_ns`. Go `m1/go/internal/broker/scenario.go` `concurrent` (line 533) opens every worker with `led.fork()` (line 548; `sqlite.go` `fork` line 623, `reopen`) sequentially in the calling goroutine, then spawns goroutines that wait on `<-start` (line 565), releases with `close(start)`, and adds setup to `total_ns`. **Both already open before release.** Remaining D-027 work in each: verify each worker connection (settings read back) before release; exclude setup from `total_ns` and report it in the new setup field; implement the cold-connection profile; tests proving no `BEGIN` before release.
4. Harness: `m1/props/wprops/c_broker.py` (run groups in the primary profile; optionally record cold-profile availability separately) and `m1/bench/bench.py` (workload B primary profile; supplemental cold profile reported separately); keep availability evidence separate from correctness.
5. Verify (all must pass): Rust `cargo test`; Go vet and race tests; fixtures 447/447 both ledgers both prototypes; regressions; `m1/ledger_tests` all; `m1/props/tests`; memory property CI profile seeds 20261107 and 1; SQLite campaign `python3 m1/props/run.py --campaign broker --ledger sqlite --seed 20261107 --cases 1000 --repeats 3 --rust ... --go ...` and seed 2; mutation smoke both; bench self-tests and dry run; M0; drift, driver, docs checks. Record timeout counts per profile as availability evidence (not a benchmark).
6. **Pending:** E-038 recorded and code committed separately (`52b31fe`); ask the owner before pushing; verify CI on the exact SHA; only then mark D-027 IMPLEMENTED in DECISION-LOG, TASKS, this handoff and WORK-STATE.

**Done (2026-11-10):** steps 1 to 5. Field names as implemented: `--concurrency-profile primary|cold`; group `ready_ns`; worker `setup_ns`, `start_ns`; bench `B-cold`, `concurrent.worker.release_delay_ns`. Implementation differences (not tuned): Rust parallel setup in worker threads, Go sequential setup in the caller (LEDGER-SQLITE.md implementation notes).

**Acceptance:** both profiles implemented in both prototypes; spec-derived tests prove the primary profile's start rule; all suites above green locally and in CI; earlier timeout evidence kept and labelled preliminary.

**Open question for the owner (raise before or with the D-027 report):** release simultaneity differs (Rust OS-thread barrier releases all at once; Go goroutines released by `close(start)` are scheduled by the runtime); the earlier explanation of the timeout difference was wrong (TEST-EVIDENCE "Correction to E-035"). Do not change release mechanics without a decision.

**Known risks:** busy timeouts still possible in the primary profile (all 64 writers start together; D-026 accepts verified aborts); Go `database/sql` connection reuse must stay one dedicated `*sql.Conn` per worker; per-group connection setup time differs between prototypes (report, do not tune).

**After D-027:** controlled M1-06 measurements and M1-07 build timings, only when every guard passes; then M1-08 scorecard for owner review. Never choose Rust or Go.

### G. Unverified security obligations and blockers (never claim otherwise)

Independent human review of the 26 golden cases and all M1 fixtures (REL-01); review of the 288 V1 rules (M6-RULES); SR-06 real-host verification (M5-SR06; adversarial trials only on a VM or separate machine); 16 D-014 risks open; authenticated approval service and evaluator issuance, attestation and signatures; real snapshot immutability and credential isolation; trusted production clock and key management; real process-kill and power-loss recovery (ledger crash tests are early returns); real external exactly-once effects (adapter simulated); remaining adversarial and host-bypass tests; real-host enforcement. ADR-0002 OPEN; ADR-0008 APPROVED IN PRINCIPLE; release and production NO-GO.

**Benchmark environment ([M1-06-READINESS](M1-06-READINESS.md)): NOT READY.** Last reading 2026-11-10: full-power charger not connected (battery discharging on a 5 V / 3 A USB-C supply, about 15 W); governor back to `powersave` (owner restored it); 1-minute load 3.52 (IntelliJ, 22 Docker containers, Chrome, Claude sessions); CPU 60 C (needs a fresh check). The guard (`m1/bench/wbench/envinfo.py`) must pass; never bypass or relax it; never change CPU settings or stop the owner's processes.

### H. Commands

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

### I. Requires owner approval

Every push (fast-forward only, never force); deleting files; changing frozen fixture expectations; any new decision or change to D-017..D-027; benchmark or build-time measurement runs (guard must also pass); CPU or system settings; stopping the owner's processes; choosing Rust or Go; marking ADR-0002 or ADR-0008 ACCEPTED; anything touching `main`, V1 trees, the M0 harness or `hook-audit/`.

## Standing facts

1. **Objective:** Writ V2, a model- and domain-agnostic governance, retrieval and memory platform ([master plan](MASTER-IMPLEMENTATION-PLAN.md)).
2. **Key files:** [M0-GO-NO-GO](M0-GO-NO-GO.md), [M0-GO-SIGNOFF-PACKAGE](M0-GO-SIGNOFF-PACKAGE.md), [TASKS](TASKS.md), [DECISION-LOG](DECISION-LOG.md), [TEST-EVIDENCE](TEST-EVIDENCE.md), [SR-06-TRIAL-PLAN](../audit/m0/SR-06-TRIAL-PLAN.md), `m0/` harness, `docs/contracts/`.
3. **Do not reverse:** D-001..D-013 (accepted); D-014 ACCEPTED 2026-11-07 (development only; release standard revisited at release milestone); D-015, D-016. In particular D-008 precedence and scoped ids, D-009 no grant inheritance, D-010 adapter-sealed broker ingress with a key-free kernel, D-011 owner policy decisions (not review verdicts), D-012 full-request fingerprints and project-scoped evidence, D-013 artifact and signoff scope.
4. **Open gates:** 26 golden cases PENDING INDEPENDENT REVIEW (REL-01 before release); 288 rules unreviewed (M6-RULES before import); SR-06 NOT EXECUTED (M5-SR06; adversarial trials only on a VM or separate machine). 16 risks open with recommended dispositions, owner signature pending.
5. **Known items:** V1 undeclared deps PyYAML and tokenizers (R-V1PKG); V1 ONNX Runtime telemetry (R-V1TEL, V2-TEL); golden case 24 misnamed; RM-01 composite effects; production key management, signed policies, trusted clocks, TOCTOU and authenticated signoff provenance assigned to M3 to M6.
6. **Prohibited until further decisions:** public release, production deployment, privileged production workloads, unverified security or enforcement claims.
