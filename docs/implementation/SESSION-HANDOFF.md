# Session handoff

Read this first after compaction or in a new session, then [WORK-STATE](WORK-STATE.md).

## Current checkpoint (2026-11-10, D-024 done locally; CI fix pending push)

**This is the authoritative recovery entry point. It is not authorization to do anything; re-establish authorizations from the owner's newest message.**

### A. Project status

| Item | State |
|---|---|
| Objective | Writ V2: model- and domain-agnostic governance, retrieval and memory platform. A small pure **kernel** decides; a separate **broker** is the only path to governed effects (permits, grants, receipts); the AI proposes and never authorizes itself ([V2-ARCHITECTURE](../../V2-ARCHITECTURE.md), [master plan](MASTER-IMPLEMENTATION-PLAN.md)) |
| Why kernel and broker are separate | The kernel is pure and key-free (no I/O, injected time) so decisions are deterministic and testable; the broker holds keys and the ledger, re-decides at use time and binds every effect to the exact request (D-008, D-010, D-012) |
| Acceptance Gate Integrity | Protected workflow transitions need a trusted, version-bound acceptance record checked by the broker plus kernel structure checks; promotion separate from publish or deploy ([spec](../architecture/acceptance-gate-integrity.md), ADR-0008 **APPROVED IN PRINCIPLE**) |
| Branch | `v2/architecture-rebuild` (never `main`) |
| Published HEAD | `e1dc2e126bb9dd911662e058af0e1b16d71cc976` (D-022, D-023, M1-07 report). Its CI https://github.com/infinri/Writ/actions/runs/42836769090 **FAILED** (Rust linked the runner's system SQLite 3.46.1, E-029). Last green CI: `9e6f9f1`, run 42396690120 |
| Local commit not yet pushed (verify with `git log origin/v2/architecture-rebuild..HEAD`) | the D-024 commit: CI SQLite linkage fix, D-024 storage failures in both prototypes, language-neutral tests, Go `-trimpath`, `ReadID` removal, open-assumptions document. Push needs owner confirmation |
| Working tree | Only untracked `docs/audit/m0/hook-audit/` (never stage or publish) |
| Milestone | **M1** under D-014 (development only). Done: M1-00..M1-05, M1-06a harness, M1-06b (D-022), M1-06c (D-023 SQLite ledger), M1-06e (D-024, local). **M1-07 in progress** (facts collected; build times deferred). **M1-06 measurements blocked** (environment). M1-06d broker timing specified, gated on green CI; M1-08 not started. M0 closed for development only; release and production NO-GO |

### B. Owner decisions in force

D-001..D-013; **D-014** M0 development GO (16 risks open); D-015 Control Center; D-016 Acceptance Gate Integrity (ADR-0008 APPROVED IN PRINCIPLE, not ACCEPTED); **D-017..D-021** M1 semantics; **D-022** decide input validation (three-field `REQUEST_SCHEMA_INVALID` for malformed `now`, `state_revision` (0..2^53-1) or absent `request`; strict `YYYY-MM-DDTHH:MM:SSZ`; D-017 spelling failures stay `REQUEST_JSON_INVALID`); **D-023** SQLite-backed ledger required for official workload B (same schema, transactions, engine and settings in both; in-memory kept but never a substitute); **D-024** (2026-11-10) D-022 approved; Go `-trimpath`; M1 is Linux x86_64 only; storage failures fail closed with one sanitized response; `ReadID` removed; DEP-1 and C-1..C-6 stay open ([M1-OPEN-ASSUMPTIONS](M1-OPEN-ASSUMPTIONS.md)); independent fixture review preserved. ADR-0002 Rust vs Go: **OPEN** until M1-08. Fixture amendments 1..6.

Pending owner input: (1) DEP-1 and C-1..C-6 with proposed resolutions in [M1-OPEN-ASSUMPTIONS](M1-OPEN-ASSUMPTIONS.md); (2) independent human review of all fixtures (PENDING INDEPENDENT REVIEW); (3) push of the local D-024 commit.

### C. Current implementation

| Component | Location | Build and test |
|---|---|---|
| Toolchains and SQLite | `m1/tools/` | `install-toolchains.sh` (Rust 1.99.1, Go 1.27.3), `install-sqlite.sh` (SQLite 3.54.1 static, revision 2, SHA3-256 verified); `source m1/tools/env.sh` |
| Rust prototype | `m1/rust/` (`sha2`, `hmac`, `rusqlite =0.40.2`; `smallvec` pinned 1.16.3) | `cd m1/rust && cargo build --release --locked && cargo test --release --locked` |
| Go prototype | `m1/go/` (standard library plus `mattn/go-sqlite3 v1.14.52`; tags mandatory) | `cd m1/go && T="libsqlite3 sqlite_omit_load_extension" && go vet -tags "$T" ./... && go build -trimpath -tags "$T" -o proto ./cmd/proto && go test -race -count=1 -tags "$T" ./...` |
| SQLite ledger spec | `m1/fixtures/broker/LEDGER-SQLITE.md`, `ledger.sql` | `proto broker-scenario --ledger sqlite --ledger-dir DIR` or `--ledger-db PATH`; `proto sqlite-info` |
| Storage-failure tests | `m1/ledger_tests/` (language-neutral, D-024) | `WRIT_RUST=<bin> WRIT_GO=<bin> python3 -m unittest discover -s m1/ledger_tests -v` |
| Fixtures | `m1/fixtures/` (12 sets, 444 cases) | `python3 m1/driver/parity.py --proto <bin> --manifest m1/fixtures/manifest.json --repeat 2`; SQLite: `WRIT_PROTO=<bin> python3 m1/driver/parity.py --proto m1/tools/sqlite-ledger-proto.sh --manifest m1/fixtures/manifest.json --repeat 2`; drift `python3 m1/fixtures/export_m0_fixtures.py --check` |
| Property harness | `m1/props/` | `python3 m1/props/run.py --profile ci --seed 20261107 --rust <bin> --go <bin> --out r.json`; SQLite broker: wrappers setting `WRIT_PROTO` around `m1/tools/sqlite-ledger-proto.sh` (see CI) |
| Benchmark harness | `m1/bench/` | dry run `python3 m1/bench/bench.py --dry-run --seed 20261107 --rust ... --go ... --out dry.json`; measurement without `--dry-run` (guarded); self-tests `python3 -m unittest discover -s m1/bench/tests` |
| M1-07 assessment | `m1/assess/collect.py`, [report](M1-07-BUILD-ASSESSMENT.md) | `python3 m1/assess/collect.py facts --out ...`; `build-times` (guarded) |
| CI | `.github/workflows/v2-docs.yml` | docs-v2, m0-harness, m1-rust and m1-go (each also runs all fixtures on SQLite), m1-props (CI profile seeds 20261107 and 1, SQLite broker campaign Rust vs Go, mutation smoke, harness self-tests and dry run) |

### D. Verified evidence ([TEST-EVIDENCE](TEST-EVIDENCE.md) E-016..E-028)

- E-029: CI of `e1dc2e1` failed (system SQLite linked by Rust on the runner); fix local. D-024: before Rust 7 and Go 3 of 13 storage tests failed (Rust executed an effect on a schema-version-2 ledger); after 13/13 each; full re-verification green locally.
- E-026 M1-06a harness (published, CI green). E-027 D-022: before Rust 5 and Go 22 of 30 new fixtures failed; after both 444/444. E-028 D-023: both 444/444 in memory and on SQLite; Rust SQLite vs Go SQLite broker campaign 0 mismatches over 23,089 checks; own persistence, second-process, atomicity and 64-way contention tests pass in each; mutation smoke, regressions, M0 281, docs checks pass. **The local commits `a44c9cc` and `935e4e3` have not run in CI yet.**
- Guarantee level: **Tested (unit or reference)** only. No benchmark measurement exists; dry runs are not measurements.

### E. Unverified security obligations (never claim otherwise)

Independent human review of the 26 golden cases and all M1 fixtures (REL-01); 288 V1 rules (M6-RULES); SR-06 live-host verification (M5-SR06); 16 D-014 risks open; real evaluator issuance, attestation and signatures; real snapshot immutability and credential isolation; durable production storage, process isolation and crash recovery (the SQLite ledger tests early-return crashes only, not process kills or power loss); trusted production clock; real external exactly-once effects (adapter simulated); real-host enforcement.

### F. Next tasks

1. **Push** the local D-024 commit after owner confirmation; verify CI on the exact SHA: the engine assertion must print 3.54.1 in both build jobs, the SQLite fixture runs, `m1/ledger_tests` and the Rust-versus-Go SQLite campaign must run and pass.
2. **M1-06d (after green CI, owner 2026-11-10):** implement `bench-broker-scenario` per LEDGER-SQLITE.md "Per-operation timing" in both prototypes (separate agents), then harness support in `m1/bench` (B and H timed through it, scenario equality checked); no measurements.
3. **M1-07 remaining:** build timings when the guard passes; cross-target decision (pending owner input 5).
4. **M1-06 measurements: BLOCKED.** Governor `powersave` (verified 2026-11-07; mains online). Owner: `sudo cpupower frequency-set -g performance`, idle and cool machine, then from a clean tree with pinned toolchains and SQLite: rebuild both, `python3 m1/bench/bench.py --seed <new recorded seed> --rust ... --go ... --out m1/bench/results/<date>.json` (guard must pass; exit 4 invalidates).
5. M1-08 scorecard and ADR-0002 recommendation after M1-06 and M1-07 (owner decides).

### Claude must not (without new explicit owner instruction)

Choose Rust or Go; run comparative benchmarks or build timings outside the guard; change global CPU settings; mark ADR-0008 or ADR-0002 ACCEPTED; change frozen expectations without a recorded decision; claim verified security, durability or host enforcement; modify V1 or the M0 harness; stage `hook-audit/`; push without confirmation; force-push; touch `main`.

### First commands in a new session

```bash
git branch --show-current && git fetch -q origin && git status --short
git log --oneline -8 && git rev-parse HEAD origin/v2/architecture-rebuild
gh run list --branch v2/architecture-rebuild --workflow v2-docs.yml --limit 3
source m1/tools/env.sh && (cd m1/rust && cargo build --release --locked && cargo test --release --locked) && (cd m1/go && T="libsqlite3 sqlite_omit_load_extension" && go build -trimpath -tags "$T" -o proto ./cmd/proto && go test -race -count=1 -tags "$T" ./...)
python3 m1/driver/parity.py --proto "$PWD/m1/rust/target/release/proto" --manifest m1/fixtures/manifest.json && python3 m1/driver/parity.py --proto "$PWD/m1/go/proto" --manifest m1/fixtures/manifest.json
python3 -m unittest discover -s m1/bench/tests
cat /sys/devices/system/cpu/cpu0/cpufreq/scaling_governor /sys/class/power_supply/ACAD/online
```

## Standing facts

1. **Objective:** Writ V2, a model- and domain-agnostic governance, retrieval and memory platform ([master plan](MASTER-IMPLEMENTATION-PLAN.md)).
2. **Key files:** [M0-GO-NO-GO](M0-GO-NO-GO.md), [M0-GO-SIGNOFF-PACKAGE](M0-GO-SIGNOFF-PACKAGE.md), [TASKS](TASKS.md), [DECISION-LOG](DECISION-LOG.md), [TEST-EVIDENCE](TEST-EVIDENCE.md), [SR-06-TRIAL-PLAN](../audit/m0/SR-06-TRIAL-PLAN.md), `m0/` harness, `docs/contracts/`.
3. **Do not reverse:** D-001..D-013 (accepted); D-014 ACCEPTED 2026-11-07 (development only; release standard revisited at release milestone); D-015, D-016. In particular D-008 precedence and scoped ids, D-009 no grant inheritance, D-010 adapter-sealed broker ingress with a key-free kernel, D-011 owner policy decisions (not review verdicts), D-012 full-request fingerprints and project-scoped evidence, D-013 artifact and signoff scope.
4. **Open gates:** 26 golden cases PENDING INDEPENDENT REVIEW (REL-01 before release); 288 rules unreviewed (M6-RULES before import); SR-06 NOT EXECUTED (M5-SR06; adversarial trials only on a VM or separate machine). 16 risks open with recommended dispositions, owner signature pending.
5. **Known items:** V1 undeclared deps PyYAML and tokenizers (R-V1PKG); V1 ONNX Runtime telemetry (R-V1TEL, V2-TEL); golden case 24 misnamed; RM-01 composite effects; production key management, signed policies, trusted clocks, TOCTOU and authenticated signoff provenance assigned to M3 to M6.
6. **Prohibited until further decisions:** public release, production deployment, privileged production workloads, unverified security or enforcement claims.
