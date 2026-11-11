# M1-08: Rust versus Go scorecard (for owner review)

**Status:** DRAFT FOR OWNER REVIEW (2026-11-11). Input to [ADR-0002](../decisions/ADR-0002-runtime-bakeoff.md), which stays **OPEN**. **No language is chosen or recommended here**; the owner decides. ADR-0008 APPROVED IN PRINCIPLE; release and production NO-GO.

## Hard gates (ADR-0002 selection rule: pass/fail before any cost comparison)

| Gate | Rust | Go | Evidence |
|---|---|---|---|
| 447 fixtures, both ledgers | pass | pass | E-042, CI E-044/E-045 |
| Property, metamorphic, differential (memory and SQLite, both D-027 profiles) | pass, 0 mismatches | pass, 0 mismatches | E-042 |
| Concurrency correctness (D-026 oracle, D-028 release, D-029 gate) | pass | pass | E-042 |
| Mutation smoke (9 mutants) | 9/9 caught | 9/9 caught | E-042 |
| Correctness checks during the official measurement (35 check kinds) | 0 failures | 0 failures | E-046 |
| Security guarantees on a real host | **not verified** (tested only) | **not verified** (tested only) | SESSION-HANDOFF section G |

Both pass every gate that M1 can test. Neither has real-host security verification; that is open for both and is not a differentiator.

## Measurement conditions (E-046, E-047)

Single host: AMD Ryzen 9 7940HS, 16 logical CPUs, 31,393 MiB, Ubuntu 24.04.3, Linux 6.17.0-40; governor `performance` (amd-pstate-epp, EPP `performance`, boost on, platform profile `balanced`); on AC, battery Not charging at 100%; start CPU 52.25 C, load 0.53; end CPU 76.5 C, load 1.84 (the benchmark's own load). Commit `138163d` (clean `m1/` tree, CI green); Rust binary `sha256:e7afa261...1a7a1d85`, Go binary `sha256:4d853d89...6e077598` (`-trimpath`); SQLite 3.54.1 identical in both. Seed 20261111 (concurrency 20261113). Benchmark harness version recorded in the result. Background applications were running (IntelliJ, ClickHouse, Chrome; not stopped). One host and one seed: differences under about 5% are within run-to-run noise (kernel p50 run-to-run CV: Rust 4.5%, Go 2.1%).

## Results

| Workload | Metric | Rust | Go | Difference |
|---|---|---|---|---|
| A kernel decisions (`bench-decide`, 100,000) | p50 / p95 / p99 latency | 13.2 / 16.8 / 20.8 µs | 22.3 / 38.6 / 90.3 µs | Rust p50 41% lower; Go tail (p99) 4.3x |
| A | batch throughput | 32,258 /s | 24,461 /s | Rust +32% |
| A | peak RSS | 2.8 MB | 12.9 MB | Go 4.6x |
| Cold start (30 launches, to first answer) | p50 / p95 | 0.75 / 0.89 ms | 2.09 / 2.27 ms | Go 2.8x |
| Process concurrency (10,000 decisions over k processes) | decisions/s at k = 1 / 10 / 100 / 1,000 | 31.8k / 152k / 168k / 49.5k | 24.1k / 86.0k / 74.7k / 21.5k | Rust 1.3x to 2.3x |
| G hostile input (1,000 lines) | lines/s; peak RSS | 2,837; 7.3 MB | 1,283; 18.7 MB | Rust 2.2x throughput |
| **B approval races, SQLite, primary (D-027/D-028)** (100 sessions x 10 runs, groups of 10) | batch wall p50; successful ops/s | 5.67 s; 95.5 | 5.79 s; 93.0 | within noise (about 2%) |
| B, SQLite | concurrent execute total p50 / p95 / p99 | 5.33 / 105 / 181 ms | 4.87 / 106 / 182 ms | p50 Go 9% lower; tails equal |
| B, SQLite | kernel decide p50 / p99 inside execute | 9.5 / 16.0 µs | 15.6 / 239 µs | Go tail 15x (GC and scheduling under concurrency, not verified) |
| B, SQLite | `BEGIN IMMEDIATE` wait p50 / p95 | 1.07 / 104 ms | 0.009 / 104 ms | see note 1 |
| B, SQLite | release delay p50 / p95; start skew p50 / p95 | 7.4 / 10.7 µs; 4.7 / 19 µs | 31.6 / 95 µs; 56 / 124 µs | Rust releases about 10x more simultaneously |
| B, SQLite | connection preparation (`ready_ns`) p50; worker setup p50 | 0.69 ms; 0.52 ms | 0.86 ms; 0.71 ms | Rust 20 to 26% lower |
| B, SQLite | peak RSS | 7.3 MB | 24.1 MB | Go 3.3x |
| B, SQLite | lock-timeout refusals; other storage refusals | 0 of 10,000; 0 | 0 of 10,000; 0 | equal |
| **B-cold** (supplemental, D-027) | batch wall p50; successful ops/s | 5.63 s; 96.6 | 5.47 s; 98.6 | within noise |
| B-cold | execute total p50 | 5.94 ms | 5.26 ms | Go 11% lower |
| B-cold | lock-timeout refusals | 0 | 0 | equal |
| H duplicates and uncertain receipts, SQLite (1,000 scenarios x 5) | lines/s; execute total p50 / p99 | 153; 1.05 / 7.31 ms | 156; 1.07 / 7.42 ms | within noise |
| H | peak RSS | 4.6 MB | 17.2 MB | Go 3.7x |
| H | process overhead per run (wall minus in-process time) p50 | 20 ms | 723 ms | see note 2 |
| B-mem, H-mem (in-memory model; simulated, not a substitute) | batch wall p50 | 63 ms; 81 ms | 56 ms; 106 ms | mixed |
| M1-07 clean build (5 runs, median) | wall | 2.65 s | 3.69 s | Rust 28% faster (note 3) |
| M1-07 incremental build after a kernel-file touch | wall | 1.42 s | 0.08 s | **not comparable** (note 3) |

Successful versus refused operations: in B every group mixes authorized and refused operations by design (shared grants, conflicting keys); successful operations were 5,441 (Rust) and 5,388 (Go) of 10,000, the rest ordinary refusals (for example `GRANT_EXHAUSTED`, `IDEMPOTENCY_CONFLICT`), each verified against a permitted serialization; storage refusals 0 for both. Successful throughput counts only successful operations.

**Notes (observations, not explanations verified by experiment):**

1. **SQLite lock wait at the median.** Rust's median `BEGIN IMMEDIATE` wait is about 1 ms, Go's about 9 µs; p95 and p99 are equal, and end-to-end execute latency is close. Rust's near-simultaneous release (start skew p50 4.7 µs versus 56 µs) makes more writers collide at `BEGIN`, and SQLite's busy handler then sleeps in millisecond steps. This is the release-simultaneity difference D-028 made measurable; it is a property of the start protocol, not of transaction cost (reserve body p50: Rust 26 µs, Go 36 µs).
2. **Go process overhead in H: resolved (E-048).** An instrumentation boundary: Go closed the SQLite ledger after recording `run_ns`, Rust before. Not extra work (Go wall time was not higher). Fixed and verified (untimed time now 0.036 s Go versus 0.031 s Rust per 1,000 lines).
3. **Build timings.** Rust clean builds compile 20 crates with registry sources cached; Go clean builds start from an empty dedicated build cache (`GOCACHE` pointed at a scratch directory so the owner's global cache was not cleared). The incremental case appends a newline to one kernel source file: Go's compiled output is then unchanged and the link is cached, so 0.08 s does not represent a real edit; Rust recompiles its single crate. A realistic incremental comparison needs a semantic edit (not measured).

## Sensitivity

- **Clear and large (well beyond noise):** kernel decision latency and tail, cold start, memory footprint (Rust 3x to 5x smaller everywhere), multi-process decision throughput, hostile-input throughput. These all favour Rust.
- **Not distinguishable:** SQLite-backed broker workloads (B, B-cold, H): end-to-end throughput and tail latency are within about 2 to 3%, because SQLite commits (`synchronous=FULL`) dominate. Go has a slightly lower median execute latency in B and B-cold.
- **Protocol-dependent:** the lock-wait median and start skew depend on release mechanics (D-028), not on transaction cost.
- **Not measured:** realistic incremental builds, real-host security, multi-process ledger use, workload E (chaos), other hardware, longer runs or other seeds.

## Non-performance criteria (from M1-07, unchanged)

See [M1-07-BUILD-ASSESSMENT](M1-07-BUILD-ASSESSMENT.md): dependencies (Rust 20 crates, Go 1 module), reproducibility (Rust by default, Go with `-trimpath`), binary size (Rust 2.3 MB, Go 5.9 MB), cross-compilation (both blocked by the C SQLite build), defect history (no language-attributable pattern), and the engineering judgments awaiting owner review.

## Owner decisions needed

1. Review this scorecard and the M1-07 judgments; decide ADR-0002 (or request more evidence: semantic incremental builds, longer or repeated runs, a second host, workload E).
2. Whether to commit the raw M1-06 result files (`m1/bench/results/` is git-ignored by design; local SHA-256 recorded in E-046).
