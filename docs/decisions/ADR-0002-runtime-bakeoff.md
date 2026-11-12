# ADR-0002: Rust versus Go kernel selection

**Status:** ACCEPTED · 2026-11-12 (owner decision) · scope: the tested trusted kernel and broker only · proposed 2026-11-05

## Candidates

**Rust:** strong predictable resource control, mature native Cedar, Wasmtime integration, excellent Tantivy compatibility; more build complexity and developer onboarding, Neo4j official driver parity must be verified before committing to native graph access.

**Go:** straightforward deployment and concurrency, official Neo4j driver, Temporal Go SDK, Cedar-Go and wazero options; garbage collection/peak-memory behavior and retrieval stack compatibility need realistic measurement.

Neither is automatically best in a workload dominated by model and graph network latency. Keep graph and workflow behind ports.

## Required bake-off

Build the same typed contracts, pure decision evaluator, state machine, local IPC, broker authorization stub and SQLite ledger in both stacks. Test exactly the same >10,000 golden actions with malformed fields, conflicting policies, replayed approvals, symlinks and concurrent requests. Measure cold/warm p50/p95/p99; RSS/heap; 1/10/100/1000 concurrent sessions; startup, binary size, diagnostics, build time and cross-platform packaging. Compare not just kernel CPU but end-to-end adapter-to-verdict. Kill backing services and record fail-safe behavior.

## Selection rule

Correctness and security invariants are **hard pass/fail gates**. Once both pass, choose the smallest long-term operating and maintenance cost under target workloads, informed by reproducible measurements. Record hardware, versions, workloads and scorecard in this ADR, then change status to ACCEPTED. No dual-language core forever.

## Implication

Documentation refers to the 'kernel' rather than assuming Rust crate APIs or Go package types until the bake-off is finished.

## Expanded M1 cross-domain scope

Rust and Go prototypes must process the exact same TaskSpec, ModelEvent and pack-fixture metadata **without depending on any model provider SDK or coding-only fields**. The managed AgentRunner is not part of the deterministic kernel performance benchmark; separate the cost of model inference from authorization decisions. Include one simulated external host task that never calls a Writ model.

## Decision (owner, 2026-11-12)

**Rust is selected for Writ V2's trusted kernel and broker.** The Go prototype and all benchmark evidence are preserved as comparison material; Writ will not maintain two production cores. Scorecard: [M1-08-SCORECARD](../implementation/M1-08-SCORECARD.md); decision record D-030 in [DECISION-LOG](../implementation/DECISION-LOG.md).

**Scope.** The tested trusted kernel and broker: policy decision, strict input handling, sealed ingress, approvals, permits, idempotency receipts, reconciliation and the local SQLite ledger. This decision does **not** establish PostgreSQL performance, select technologies for PostgreSQL enterprise state, Neo4j, orchestration, adapters or the Control Center, or certify production security. ADR-0008 remains APPROVED IN PRINCIPLE; release and production remain NO-GO.

**Hard gates (both prototypes passed every gate M1 can test):** 447 shared fixtures on both ledgers; seeded property, metamorphic and Rust-versus-Go differential campaigns in memory and on SQLite (both concurrency profiles, 0 mismatches); D-026 concurrency oracle, D-028 equivalent release protocol, D-029 process-local coordination gate; chaos with real SIGKILL and restart (50 iterations each, 0 invariant violations); mutation smoke 9 of 9 each; 0 correctness failures in every official measurement.

**Hardware and versions.** One host: AMD Ryzen 9 7940HS (16 logical CPUs), 31,393 MiB, Ubuntu 24.04.3, Linux 6.17.0-40, governor `performance`, on AC. rustc and cargo 1.99.1; go 1.27.3 (`-trimpath`, cgo); SQLite 3.54.1 static library shared by both. Evidence bundles with SHA-256 manifests: `m1/bench/evidence/` (run sets at `138163d` and `bbdd092`; order-controlled process-concurrency correction at `992383a`).

**Repeated measurements (three run sets; Rust versus Go).** Kernel decision p50 13.0 to 13.2 versus 22.3 to 22.4 µs; p99 19 to 21 versus 90 to 99 µs; kernel throughput 32.3k to 32.8k versus 23.6k to 24.5k decisions/s; peak memory 2.7 to 2.9 versus 12.1 to 12.6 MB (kernel) and about 7 versus 22 to 24 MB (broker); cold start 0.68 to 0.75 versus 1.98 to 2.09 ms; order-controlled process concurrency 1.48x, 1.76x, 2.17x and 2.33x Rust/Go at 1, 10, 100 and 1,000 processes; hostile input about 2.1x. SQLite-backed broker workloads: equal useful throughput and tail latency (durable commits dominate); Go's median execute latency 10 to 15% lower, attributable to start-protocol scheduling; storage refusals 0 for both. Builds: clean 2.50 versus 3.54 s; real-edit incremental 1.31 versus 0.49 s.

**Go's advantages (real tradeoffs, retained).** A much smaller dependency and supply-chain surface (1 module versus 20 crates; Go's standard library covers crypto); 2.7x faster incremental builds; simpler tooling and a built-in race detector; lower median broker latency in this start protocol.

**Why Rust.** With the hard gates equal, the selection rule asks for the smallest long-term operating and maintenance cost under target workloads. *Measured* resource advantages favour Rust on the component every governed action passes through (kernel latency and tail, throughput, memory, startup). The *projected* long-term operating-cost advantage (fewer resources per host, predictable tails, no garbage collector on the trust path) is an engineering judgment extrapolated from one host, not a measured cost. Go's dependency, build and tooling advantages are maintenance conveniences; Rust's dependency set must be kept pinned, reviewed and advisory-checked (ENT-R06).

**Bake-off scope actually covered (recorded honestly).** The "same >10,000 golden actions" requirement was met as 447 shared fixtures plus seeded generated campaigns (thousands of generated cases per run) rather than 10,000 hand-curated actions; cross-platform packaging was deferred (D-024: Linux x86_64 only; both blocked by the C SQLite build); killing backing services was covered by real process kills of the broker on a file ledger, not by killing a separate database service.

**Open limitations assigned to later acceptance gates.** Cross-process reconciliation safety (D-029 gate is process-local): M3 broker and the M10 PostgreSQL coordination and recovery gate. Real-host enforcement (SR-06): M5. Power-loss and OS-crash durability: M9 recovery drills. Single-host measurement and unavailable thermal-throttle counters: M10 load, chaos and total-cost benchmark on target hardware.
