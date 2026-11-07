# Session handoff

Read this first after compaction or in a new session, then [WORK-STATE](WORK-STATE.md).

## Current checkpoint (2026-11-07, end of M1-05)

**This is the authoritative recovery entry point. It is not authorization to do anything; re-establish authorizations from the owner's newest message.**

### A. Project status

| Item | State |
|---|---|
| Objective | Writ V2: model- and domain-agnostic governance, retrieval and memory platform. A small pure **kernel** decides; a separate **broker** is the only path to governed effects (permits, grants, receipts); the AI proposes and never authorizes itself ([V2-ARCHITECTURE](../../V2-ARCHITECTURE.md), [master plan](MASTER-IMPLEMENTATION-PLAN.md)) |
| Why kernel and broker are separate | The kernel is pure and key-free (no I/O, injected time) so decisions are deterministic and testable; the broker holds keys and the ledger, re-decides at use time and binds every effect to the exact request (D-008, D-010, D-012) |
| Acceptance Gate Integrity | Second boundary beside action authorization: protected workflow transitions need a trusted, version-bound acceptance record checked by the broker, plus kernel structure checks; promotion is separate from publish or deploy effects ([spec](../architecture/acceptance-gate-integrity.md), ADR-0008 APPROVED IN PRINCIPLE) |
| Branch | `v2/architecture-rebuild` (never `main`) |
| Last code and CI commit | `fe7de57a430355a529bc39088e659b5f5f915f39` (D-021 conformance plus required property CI job). CI https://github.com/infinri/Writ/actions/runs/42394888125: 5 jobs success (docs, M0 harness, M1 Rust parity, M1 Go parity, M1 property and differential). A later docs-only commit carries this handoff |
| Working tree | Only untracked `docs/audit/m0/hook-audit/` (never stage or publish without separate authorization) |
| Milestone | **M1** active under D-014 (development only). M1-00..M1-05 done; **M1-06 benchmarks blocked by environment**; M1-07, M1-08 not started. M0 closed for development only; release and production NO-GO |

Published milestone commits (all on origin): `38e2d1f` M0 decision; `6810704` D-014 acceptance; `226e4d2` M1-02 revision 3; `311561a` amendment 3; `fd9d3bb` Rust and Go prototypes; `a459d4b` prototype CI; `caadd82` D-020; `2bf136f` M1-05 harness; `5f670dd` M1-05 fixes; `d101b0b` D-021; `fe7de57` required property CI and continuity rule; plus this handoff commit.

### B. Owner decisions in force

D-001..D-013 (M0 harness and semantics); **D-014** M0 development GO (attested 2026-11-07, 16 risks open); D-015 Control Center; D-016 Acceptance Gate Integrity requirement (ADR-0008 **APPROVED IN PRINCIPLE**, not ACCEPTED); **D-017..D-021** M1 semantics (strict JSON and identifiers; exact transitions and promotion permits; requirement completeness; model-assisted advisory only; protected states and edges at kernel and broker; downstream effect prerequisites; refusal mapping; transition support; strict protocol responses). ADR-0002 Rust vs Go: **OPEN** until M1-08. Fixture amendments: `m1/fixtures/AMENDMENT-1.md`..`AMENDMENT-5.md`.

Pending owner input: (1) whether a declared broker dependency absent from `initial.dependencies` may default to `up` (both prototypes do; not settled by D-021); (2) assumptions C-1..C-6 in [M1-02 review package](M1-02-FIXTURE-REVIEW.md); (3) removal of dead Go code `m1/go/internal/sjson/envelope.go` `ReadID` (needs file deletion approval); (4) independent human review of all fixture expectations (all PENDING INDEPENDENT REVIEW).

### C. Current implementation

| Component | Location | Build and test |
|---|---|---|
| Toolchains | `m1/tools/` | `m1/tools/install-toolchains.sh` (Rust 1.99.1, Go 1.27.3, checksum and GPG verified, user space); `source m1/tools/env.sh` |
| Rust prototype | `m1/rust/` (deps sha2, hmac only) | `cd m1/rust && cargo build --release --locked && cargo test --release --locked` |
| Go prototype | `m1/go/` (standard library only) | `cd m1/go && go vet ./... && go build -o proto ./cmd/proto && go test -race -count=1 ./...` |
| Fixtures | `m1/fixtures/` (manifest, 12 sets, 414 cases; sources and SEMANTICS, SCENARIOS, BROKER specs) | `python3 m1/fixtures/export_m0_fixtures.py --check`; `python3 m1/driver/parity.py --proto <bin> --manifest m1/fixtures/manifest.json --repeat 2` |
| Driver | `m1/driver/` | `python3 -m unittest discover -s m1/driver/tests` |
| Property harness | `m1/props/` ([README](../../m1/props/README.md)) | CI profile: `python3 m1/props/run.py --profile ci --seed 20261107 --rust m1/rust/target/release/proto --go m1/go/proto --out r.json`; full: `--campaign all --seed 20261107 --cases 2000 --repeats 5`; regressions: `--manifest m1/props/regressions/manifest.json`; mutation smoke: `python3 m1/props/mutants/smoke.py --real <bin> --seed 7 --cases 300 --out s.json` |
| CI | `.github/workflows/v2-docs.yml` | jobs docs-v2, m0-harness, m1-rust, m1-go, m1-props (required; seeds 20261107 and 1; uploads reports) |

Protocol and semantics: [M1-PARITY-SUITE](M1-PARITY-SUITE.md) (incl. D-021 protocol semantics), `m1/fixtures/acceptance/SEMANTICS.md`, `m1/fixtures/transition/SCENARIOS.md`, `m1/fixtures/broker/BROKER.md`. Simulated in the prototypes (labelled in code and READMEs): HMAC fixture keys for seals and permits, in-memory ledger, injected crashes, credential-match humans, counter-only effects, injected clock.

### D. Verified evidence (details in [TEST-EVIDENCE](TEST-EVIDENCE.md) E-016..E-025)

- At `fe7de57a430355a529bc39088e659b5f5f915f39` locally (E-025) and in CI https://github.com/infinri/Writ/actions/runs/42394888125: Rust 32 unit tests pass; Go vet clean, race tests ok; both 414/414 fixtures with `--repeat 2`; both 11/11 property regressions; property campaigns seeds 20261107 and 2 (2,000 cases per family, 5 repeats): 0 mismatches, 0 failed checks; 400 broker concurrency groups (16/32/64), 183 with several observed serializations, all permitted; mutation smoke: all 8 mutants detected for both; M0 281 passed; docs and drift checks OK.
- Defects found and fixed in M1-05 (with regression cases): Rust order-sensitive object comparison; Go `-00`/`-01` code; Go crash on `count -1`; plus D-020 and D-021 divergences. Go once copied Rust on two unspecified points; superseded by D-021-derived tests.
- What M1-05 proved: both prototypes implement the specified logic identically on hand-written fixtures and on seeded generated inputs, with real in-process concurrency. Guarantee level: **Tested (unit or reference)** only.

### E. Unverified security obligations (never claim otherwise)

Independent human review of the 26 golden cases and all M1 fixtures (REL-01); semantic review of the 288 V1 rules (M6-RULES); SR-06 live-host verification (M5-SR06); the 16 risks accepted in D-014 remain open; real evaluator issuance, attestation and signatures; real snapshot immutability and credential isolation; durable storage, process isolation and crash recovery; trusted production clock; real-host enforcement.

### F. Next tasks

1. **M1-06 benchmarks: BLOCKED by environment.** Verified 2026-11-07: AC adapter offline (on battery), CPU governor `powersave`, boost enabled; changing the governor needs `sudo cpupower` (owner action). Do not run comparative benchmarks until on mains power with a fixed governor (`performance`), boost policy recorded and thermals stable.
2. **M1-06a (allowed now, no measurements): write the benchmark harness** `m1/bench/` per [M1-PARITY-SUITE](M1-PARITY-SUITE.md) "Measurement protocol": workload A generator (10k decisions from golden cases, recorded seed), workload B (100 sessions x 10 concurrent with approval races), G, H; cold start (30 launches); peak RSS via `/usr/bin/time -v`; p50/p95/p99, mean, stdev; environment capture (governor, power, CPU, `rustc -vV`, `go version`, flags, commits) into [BENCHMARK-ENVIRONMENT](BENCHMARK-ENVIRONMENT.md) format; a guard that refuses to run when on battery or governor is not `performance`. Implement `decide-batch` workload support in both prototypes if needed (separately). Acceptance: harness dry-run on tiny inputs produces a schema-valid result file marked `not_a_benchmark: true`.
3. Then (owner prerequisites met): M1-06 measurement runs; M1-07 build, dependency and packaging assessment; M1-08 scorecard and ADR-0002 recommendation (owner decides; no winner before correctness gates and reproducible benchmarks).

### Claude must not (without new explicit owner instruction)

Choose Rust or Go; run comparative benchmarks on battery or uncontrolled governor; mark ADR-0008 ACCEPTED; change frozen expectations without a recorded decision; claim verified security or host enforcement; modify V1 or the M0 harness; stage `hook-audit/`; push without confirmation; force-push; touch `main`.

### First commands in a new session

```bash
git branch --show-current && git fetch -q origin && git status --short
git log --oneline -8 && git rev-parse HEAD origin/v2/architecture-rebuild
gh run list --branch v2/architecture-rebuild --workflow v2-docs.yml --limit 3
source m1/tools/env.sh && (cd m1/rust && cargo build --release --locked && cargo test --release --locked) && (cd m1/go && go build -o proto ./cmd/proto && go test -race -count=1 ./...)
python3 m1/driver/parity.py --proto "$PWD/m1/rust/target/release/proto" --manifest m1/fixtures/manifest.json && python3 m1/driver/parity.py --proto "$PWD/m1/go/proto" --manifest m1/fixtures/manifest.json
cat /sys/devices/system/cpu/cpu0/cpufreq/scaling_governor /sys/class/power_supply/ACAD/online
```

## Standing facts

1. **Objective:** Writ V2, a model- and domain-agnostic governance, retrieval and memory platform ([master plan](MASTER-IMPLEMENTATION-PLAN.md)).
2. **Key files:** [M0-GO-NO-GO](M0-GO-NO-GO.md), [M0-GO-SIGNOFF-PACKAGE](M0-GO-SIGNOFF-PACKAGE.md), [TASKS](TASKS.md), [DECISION-LOG](DECISION-LOG.md), [TEST-EVIDENCE](TEST-EVIDENCE.md), [SR-06-TRIAL-PLAN](../audit/m0/SR-06-TRIAL-PLAN.md), `m0/` harness, `docs/contracts/`.
3. **Do not reverse:** D-001..D-013 (accepted); D-014 ACCEPTED 2026-11-07 (development only; release standard revisited at release milestone); D-015, D-016. In particular D-008 precedence and scoped ids, D-009 no grant inheritance, D-010 adapter-sealed broker ingress with a key-free kernel, D-011 owner policy decisions (not review verdicts), D-012 full-request fingerprints and project-scoped evidence, D-013 artifact and signoff scope.
4. **Open gates:** 26 golden cases PENDING INDEPENDENT REVIEW (REL-01 before release); 288 rules unreviewed (M6-RULES before import); SR-06 NOT EXECUTED (M5-SR06; adversarial trials only on a VM or separate machine). 16 risks open with recommended dispositions, owner signature pending.
5. **Known items:** V1 undeclared deps PyYAML and tokenizers (R-V1PKG); V1 ONNX Runtime telemetry (R-V1TEL, V2-TEL); golden case 24 misnamed; RM-01 composite effects; production key management, signed policies, trusted clocks, TOCTOU and authenticated signoff provenance assigned to M3 to M6.
6. **Prohibited until further decisions:** public release, production deployment, privileged production workloads, unverified security or enforcement claims.
