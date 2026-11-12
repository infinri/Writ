# M1-08: Rust versus Go scorecard (for owner decision)

**Status:** FINAL DRAFT FOR OWNER DECISION (2026-11-12). Targeted confirmation complete (owner decision 2026-11-11: A repeatability, B Go overhead, C real-edit builds, D chaos). **Recommendation: Rust** for the trusted kernel and broker (section "Recommendation"). Rust is the owner's provisional leader; [ADR-0002](../decisions/ADR-0002-runtime-bakeoff.md) stays **OPEN** until the owner's final decision. ADR-0008 APPROVED IN PRINCIPLE; release and production NO-GO.

Labels: **Measured** (official guarded runs or recorded command output), **Fact** (verifiable property), **Judgment** (engineering opinion for the owner to weigh).

## Hard gates (ADR-0002: pass/fail before any cost comparison)

| Gate | Rust | Go | Evidence |
|---|---|---|---|
| 447 fixtures, both ledgers | pass | pass | E-042, E-050; CI on every published SHA |
| Property, metamorphic, differential (memory and SQLite, both concurrency profiles) | pass, 0 mismatches | pass, 0 mismatches | E-042, E-050 |
| Concurrency correctness (D-026 oracle, D-028 release protocol, D-029 coordination gate) | pass | pass | E-042 |
| Chaos with real SIGKILL and restart (50 iterations each) | 0 violations | 0 violations | E-049 |
| Mutation smoke (9 mutants) | 9/9 caught | 9/9 caught | E-050 |
| Correctness checks during all three official runs (35 check kinds) | 0 failures | 0 failures | E-046, E-052 |
| Security guarantees on a real host | **not verified** | **not verified** | SESSION-HANDOFF section G |

Both pass every gate M1 can test; neither is a differentiator on correctness. Real-host security is unverified for both.

## Measurement conditions (Measured)

One host: AMD Ryzen 9 7940HS, 16 logical CPUs, 31,393 MiB, Ubuntu 24.04.3, Linux 6.17.0-40; governor `performance` (amd-pstate-epp), boost on; on AC. Three independent official run sets, each after a full guard pass (load below 1.0, CPU below 60 C, clean tree, pinned toolchains, Go `-trimpath`):

| Run set | Commit | Seed | Start CPU / load | Peak CPU during run | Battery | Bundle |
|---|---|---|---|---|---|---|
| 1 | `138163d` | 20261111 | 52.3 C / 0.53 | 76.5 C (end) | Not charging, 100% | `m1/bench/evidence/2026-11-11-seed-20261111/` |
| 2 | `bbdd092` | 20261121 | 46.6 C / 0.55 | 78.4 C | Charging | `m1/bench/evidence/2026-11-12-seed-20261121/` |
| 3 | `bbdd092` | 20261131 | 51.9 C / 0.83 | 82.1 C | Charging | `m1/bench/evidence/2026-11-12-seed-20261131/` |

Runs 2 and 3 sampled temperature, clocks, governor, load and battery every 5 s (85 to 90 samples each); mean current clock across the 16 cores ranged 2.1 to 4.65 GHz (idle cores included); the host exposes no thermal-throttle counter, so throttling can be bounded only by clocks, not proven absent. The harness alternates which prototype runs first in every run. Background applications (IntelliJ, ClickHouse, Chrome) were running and not stopped. Run set 1 predates the `run_ns` boundary correction (E-048); see "Comparability".

## Repeated results (Measured; value per run set 1 / 2 / 3; CV is the coefficient of variation across run sets)

| Workload and metric | Rust | Go | Difference (judgment of significance) |
|---|---|---|---|
| A kernel decision p50 | 13.2 / 13.0 / 13.2 µs (CV 0.7%) | 22.3 / 22.3 / 22.4 µs (0.2%) | **Rust 41% lower, stable** |
| A kernel decision p95 | 16.8 / 16.3 / 16.5 µs | 38.6 / 40.0 / 40.0 µs | **Go 2.4x** |
| A kernel decision p99 | 20.8 / 20.2 / 19.0 µs (3.9%) | 90.3 / 96.1 / 98.6 µs (3.7%) | **Go 4.3x to 5.2x** |
| A kernel throughput | 32.3k / 32.8k / 32.6k /s | 24.5k / 24.1k / 23.6k /s | **Rust +35%** |
| A peak RSS | 2.7 / 2.9 / 2.9 MB | 12.6 / 12.1 / 12.1 MB | **Go 4.3x** |
| Cold start p50 | 0.75 / 0.68 / 0.71 ms | 2.09 / 1.98 / 2.00 ms | **Go 2.9x** |
| Process concurrency, k = 10 | 152k / 151k / 151k /s | 86.0k / 86.0k / 85.7k /s | **Rust 1.8x** |
| Process concurrency, k = 100 | 168k / 201k / 184k /s (7.4%) | 74.7k / 91.6k / 83.7k /s (8.3%) | **Rust 2.2x** |
| Process concurrency, k = 1,000 | 49.5k / 54.4k / 51.3k /s | 21.5k / 24.8k / 22.8k /s | **Rust 2.3x** |
| G hostile input | 2,837 / 2,483 / 2,673 lines/s | 1,283 / 1,202 / 1,295 lines/s | **Rust 2.1x** |
| B approval races, SQLite, primary: successful ops/s | 95.5 / 100.7 / 95.3 | 93.0 / 101.7 / 95.8 | equal (within 3%) |
| B execute total p50 | 5.33 / 5.28 / 5.43 ms | 4.87 / 4.38 / 4.66 ms | Go 10 to 17% lower (note 1) |
| B execute total p99 | 180.6 / 181.1 / 179.7 ms | 181.6 / 181.4 / 179.9 ms | equal |
| B kernel decide p99 inside execute | 16.0 / 20.5 / 21.5 µs | 239 / 256 / 271 µs | **Go 12x to 15x** (GC and scheduling under concurrency; not verified by experiment) |
| B peak RSS | 7.2 / 7.3 / 7.3 MB | 23.5 / 21.9 / 23.1 MB | **Go 3.2x** |
| B-cold (supplemental): successful ops/s | 96.6 / 104.3 / 98.9 | 98.6 / 102.9 / 97.1 | equal |
| B-cold execute p50 | 5.94 / 5.84 / 5.96 ms | 5.26 / 4.98 / 5.17 ms | Go 12 to 15% lower (note 1) |
| H duplicates and uncertain receipts, SQLite: lines/s | 153 / 144 / 152 | 156 / 156 / 156 | equal (Go slightly steadier) |
| H execute p50 / p99 | 1.05 / 1.04 / 1.05 ms; 7.3 / 6.2 / 7.0 ms | 1.07 ms (all); 7.4 / 7.5 / 6.9 ms | equal |
| H untimed process overhead p50 (run sets 2 and 3 only, corrected boundary) | 23.1 / 22.1 ms | 29.3 / 26.7 ms | equal; run set 1 Go value withdrawn (E-048) |
| Storage refusals (lock timeouts, other) in B and B-cold | 0 in all 60,000 operations | 0 in all 60,000 operations | equal |

**Successful versus refused operations.** B groups mix authorized and ordinarily refused operations by design. Successful operations per 10,000: Rust 5,441 / 5,567 / 4,779, Go 5,388 / 5,555 / 4,723 (B); each group is verified against a permitted serialization; useful throughput counts only successful operations; storage refusals were 0.

**Notes.**

1. **Broker median latency (Measured, explanation partly verified).** Rust's median `BEGIN IMMEDIATE` wait is about 1.065 ms in every run set versus Go's about 9.5 µs, with equal p95 and p99. Rust releases workers about 11x more simultaneously (start skew p50 4.7 to 4.9 µs versus 53 to 56 µs), so more writers collide at `BEGIN` and SQLite's busy handler sleeps about 1 ms. Go's lower median execute latency in B and B-cold therefore reflects the start protocol (D-028 equalized the logical protocol, not OS scheduling), not cheaper transactions (reserve body p50: Rust about 26 µs, Go about 35 µs). It does not change useful throughput.
2. **Go process overhead (resolved, E-048).** The 0.7 s per 1,000-line H run in run set 1 was an instrumentation boundary (Go closed the ledger after recording `run_ns`). After the correction both are about 22 to 29 ms.

## Builds (M1-07; Measured, 5 runs each, medians)

| | Rust | Go |
|---|---|---|
| Clean build (empty target directory; empty isolated `GOCACHE`) | 2.50 s (2.48 to 2.59) | 3.54 s (3.47 to 3.68) |
| Incremental after a real, retained, behaviour-preserving kernel edit | 1.31 s (1.28 to 1.38) | 0.49 s (0.48 to 0.50) |

Every incremental run proved recompilation and relinking: kernel recompiled (cargo `Compiling writ-m1-proto`; `go build -v` lists the kernel package), executable hash changed, the edit's per-run 64-bit constant absent before and present in the new executable; edited binaries passed 447/447 fixtures (E-051, E-053). Rust recompiles its single crate on any edit; Go recompiles one package and relinks. The earlier newline-only figure (Go 0.08 s) is superseded.

## Comparability (E-048)

Run set 1 is kept unchanged as recorded. Comparable across all three run sets: every wall time and throughput, kernel, cold start, process concurrency, G, per-operation broker fields, RSS. Not comparable: Go `line.run_ns` and `process.overhead_ns` in SQLite workloads of run set 1 (replaced by run sets 2 and 3).

## Facts and judgments beyond performance

| Criterion | Rust | Go | Label |
|---|---|---|---|
| Dependencies (supply chain) | 20 crates (3 direct: `sha2`, `hmac`, `rusqlite`; 3 build-only); no advisories (OSV 2026-11-07) | 1 module (`go-sqlite3`); standard library covers crypto | Fact; **Go smaller surface** |
| Reproducible builds | identical by default | identical only with `-trimpath` (now required) | Fact |
| Binary size | 2.3 MB | 5.9 MB | Measured |
| Runtime model | no garbage collector; explicit lock scopes | garbage collector; goroutines; `database/sql` needs one dedicated connection per worker | Fact |
| Cross-compilation with the C SQLite library | blocked (C cross compiler needed) | blocked (cgo) | Measured; equal |
| Defect history in the bake-off | no language-attributable pattern | same | Judgment (M1-07) |
| Iteration speed | slower incremental builds (1.3 s), steeper learning curve | faster incremental builds (0.5 s), simpler tooling, built-in race detector | Measured and judgment; **Go advantage** |
| Fit for a small, pure, strict kernel | type system and ownership keep trusted and untrusted data apart; caught field-classification errors at compile time | adequate; more runtime checks needed | Judgment |
| Operational resources | 3x to 5x less memory, 3x faster start | higher memory and start cost, tail latency from GC | Measured |

## Limitations and untested scenarios

One host and one hardware class; three run sets (a fourth host or longer soak runs not done); no thermal-throttle counter on this host; real-host security (SR-06) unverified; power-loss and OS-crash durability not tested; reconciliation across processes unprotected (D-029 gate is process-local: an unresolved architectural risk independent of language); the effect adapter is simulated; cross-target builds deferred (Linux x86_64 only, D-024).

## Recommendation (Judgment, for the owner's final decision)

**Choose Rust for the trusted kernel and broker.**

- Hard gates are equal, so ADR-0002's rule turns on long-term operating and maintenance cost under target workloads.
- On the kernel, the component every governed action passes through, Rust's advantage is large and stable across three run sets: 41% lower median and 4x to 5x lower p99 decision latency, 1.8x to 2.3x multi-process throughput, 2.1x hostile-input throughput, 3x to 5x less memory, 3x faster start. Under concurrency Go's in-broker decision tail is 12x to 15x Rust's.
- On SQLite-backed broker workloads the languages are equal in useful throughput and tail latency (durable commits dominate); Go's lower median there comes from scheduling at start, not cheaper work.
- Go's real advantages are a much smaller dependency surface, faster incremental builds and simpler tooling. They are maintenance conveniences, not risks to the trust boundary; the Rust dependency set is small, pinned, advisory-free and reproducible by default.

**Scope of this recommendation.** It covers only the tested trusted kernel and broker (policy decision, sealed ingress, approvals, permits, receipts, local SQLite ledger). It does **not** decide the language or design for PostgreSQL enterprise state and its future coordination and recovery gate, Neo4j, orchestration, adapters, or the Control Center ([ENTERPRISE-READINESS](../security/ENTERPRISE-READINESS.md)); those need their own evidence, and ADR-0002's "no dual-language core" applies to the core only.

## Owner decision needed

Accept or reject the Rust recommendation for ADR-0002 (or request further evidence: a second host, soak runs, more seeds). Claude does not mark ADR-0002 ACCEPTED.
