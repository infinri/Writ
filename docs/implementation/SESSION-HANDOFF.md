# Session handoff

Read this first after compaction or in a new session, then [WORK-STATE](WORK-STATE.md).

## Current checkpoint (2026-11-10; D-027 implemented locally, push and CI pending)

**This is the authoritative recovery entry point. It is not authorization to do anything beyond what is listed under "Authorized next task"; re-establish other authorizations from the owner's newest message.**

**After compaction (state 2026-11-11, after measurement):** D-027, D-028, D-029 implemented (CI green). **Official M1-06 measured** (E-046, seed 20261111, guard passed, 0 correctness failures) and **M1-07 build timings measured** (E-047) at `138163d`. **M1-08 scorecard drafted for owner review** ([M1-08-SCORECARD](M1-08-SCORECARD.md)); no language chosen, no recommendation. Raw results are local under the git-ignored `m1/bench/results/` (hashes in E-046). **Next task:** wait for the owner's ADR-0002 review; do not accept ADR-0002, do not rerun measurements without a new authorization and a passing guard. Do not restart M1 development or repeat any completed task in section C.

### A. Repository state (verify with the commands in section H)

| Item | State |
|---|---|
| Branch | `v2/architecture-rebuild` (never `main`) |
| Published remote HEAD | `612e1f4d081ae549fc0c769b0995b1ad2faed731` (docs: E-044, D-027..D-029 implemented); CI https://github.com/infinri/Writ/actions/runs/42938794312 **success, all 5 jobs** |
| Last code commit | `c5effec` (D-028, D-029), CI https://github.com/infinri/Writ/actions/runs/42936763654 on `d99faa2` (same m1/ tree) **success, all 5 jobs** (E-044) |
| Local, unpushed | this handoff refresh commit (docs only), if not yet pushed |
| Working tree | only untracked `docs/audit/m0/hook-audit/` (owner material: never stage, publish or summarize) |
| Milestone | **M1** under D-014 (development only). M0 closed for development only. Release and production **NO-GO** |

Commit chain since M1-05 (all published): `9e6f9f1` M1-06a harness; `a44c9cc` D-022; `935e4e3` D-023 SQLite ledger; `e1dc2e1` M1-07 report (CI failed: system SQLite linked); `3012940` D-024 plus CI fix; `6b7eab1` M1-06d broker timing; `68f4593` D-025; `8d1915c` docs; `d9815f8` guard refuses discharging battery (CI failed: busy timeouts, E-034); `1994b67` docs; `7a5a366` D-026; `988a840` docs; `d9054c9` docs checkpoint; `52b31fe` D-027; `41529d1` docs (CI red, E-039); `a0769b6` docs (CI red, same cause); `c5effec` D-028, D-029; `d99faa2` docs (CI green, E-044); `612e1f4` docs.

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
| D-027 standardized concurrent-worker start | implemented (`52b31fe`), refined by D-028 |
| D-028 equivalent release protocol | implemented (`c5effec`), CI green E-044 |
| D-029 process-local coordination gate, `STATE_CONFLICT`, verified contention acceptance | implemented (`c5effec`), CI green E-044; process-local only |

### E. Verified evidence ([TEST-EVIDENCE](TEST-EVIDENCE.md), latest E-037)

At `7a5a366` (CI run 42898828550, success, all 5 jobs; and locally, E-035): Rust 58 unit + 3 integration tests; Go vet clean, race tests ok in 6 packages; both link SQLite 3.54.1 (source id `2026-10-16 14:45:20 555e07ef...`), identical settings; fixtures **447/447** in memory and on SQLite for each prototype; regressions 11/11; `m1/ledger_tests` **30** OK (13 storage failures, 3 timing integrity, 14 D-025); D-026 oracle negative tests **14** OK; memory property CI profile seeds 20261107 and 1; Rust-SQLite versus Go-SQLite broker campaigns with 64-way groups PASS (CI 348 cases, 6,780 checks; local 2,584 cases, 52,920 checks; 0 mismatches); mutation smoke 9 of 9 mutants caught for each (including `masked-effect`); harness self-tests 18 OK; benchmark dry run 0 correctness failures; M0 281 passed; drift, driver and docs checks OK. Availability (preliminary, not comparable before D-027): E-034 Rust 89 / Go 6 timeouts of 12,800; E-036 CI Rust 2 of 1,408 at 64-way. Guarantee level: **Tested (unit or reference) only**; no test result is a real-host security certification. **No official benchmark measurement exists.**

### F. Next task: owner review of M1-08 (M1-06 and M1-07 measured, E-046, E-047)

The readiness steps below were completed on 2026-11-11 and are kept for any future measurement.

**Done:** D-027 (`52b31fe`), D-028 equivalent release protocol and D-029 process-local coordination gate, `STATE_CONFLICT`, verified contention acceptance (`c5effec`); CI green (E-044). Specs: LEDGER-SQLITE.md amendments `m1-ledger-1a`, `1b`, `1c`. Limitation: the gate is process-local; multi-process reconciliation safety is not claimed (`test_d029` cross-process demonstration checks only fail-closed safety).

**Readiness (read only, last 2026-11-11, E-043 and later):** power met (battery Not charging at 100%, current 0, also under about 20 minutes of test load); `m1/` tree clean at `612e1f4`; binaries rebuilt from it (Go `-trimpath`); toolchain pins pass. **Guard refuses:** governor `powersave` on 16 of 16 policies; 1-minute load 1.16 (IntelliJ `java` at about 300% CPU, ClickHouse about 11%); CPU 64.4 C above the 60 C start limit.

**Steps:**

1. Wait for the owner to confirm governor `performance` and a cooled CPU. Never change CPU settings, run sudo, or stop processes.
2. Rerun every readiness check read only (section H environment commands, and the guard: `cd m1/bench && python3 -c "from wbench import envinfo as e; env=e.capture(); print(e.guard(env, e.repository(), 1.0, 60.0))"` plus toolchain pins); confirm the battery is not discharging; clean committed tree; binaries built from the recorded commit.
3. If every guard passes, report readiness and **wait for the owner's explicit authorization** before official M1-06 (`python3 m1/bench/bench.py --seed <new recorded seed> ...`, M1-06-READINESS step 4) and M1-07 build timings. Correctness tests and `--dry-run` are allowed at any time; they are not measurements.
4. After measurements: M1-08 scorecard for owner review. Never choose Rust or Go.

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
