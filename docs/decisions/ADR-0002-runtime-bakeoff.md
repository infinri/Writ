# ADR-0002: Rust versus Go kernel selection

**Status:** OPEN / BENCHMARK REQUIRED · 2026-11-05

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
