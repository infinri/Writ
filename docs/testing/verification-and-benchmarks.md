# V2 verification, adversarial testing and performance program

**Status:** required tests and thresholds, not executed results.

## Test hierarchy

| Level | Main focus | Required proof |
|---|---|---|
| Contracts | Schemas, canonicalization, invalid payload | Cross-language identical JSON fixtures |
| Unit | Pure reducer/policy resolution | Deterministic replay, property-based invariants |
| Differential | Rust vs Go, V1 baseline where applicable | Same verdict/evidence on the same inputs |
| Integration | Real host, broker, SQLite/Postgres, Neo4j | Verified interception and actual effect semantics |
| Adversarial | Principal forgery, approval replay, command path escapes | Negative control that fails the wrong implementation |
| Chaos | Crash, duplicate, lost network, policy cache corruption | Safe effects and durable reconciliation |
| Load | Throughput, p99, peak memory, startup, fairness | Reproducible benchmark manifest |
| Security audit | Pen testing, dependency/SBOM, privilege boundaries | Reviewed risk register and sign-off |

## Golden fixture matrix

Build an initial corpus with at least: 100 state transition cases; 100 conflicting policy cases; 100 approval binding cases; 100 resource alias/path cases; 52 host-hook registration behavior mappings; all 288 V1 rule classifications (whether imported or discarded); representative query/Recall corpora; 10 cross-tenant isolation attempts; and failure injection at every broker effect stage.

Fixture statuses: expected_allow, expected_deny, expected_requires_approval, expected_invalid, expected_unavailable. Independent reviewers own oracle decisions for high-risk fixtures. Disagreement between two implementations is a release stop, not a majority vote.

## Language bake-off rubric

Hard gate: **100% invariant, property, fuzz seed, authorization test pass**. Weighted nonsecurity selection after passing: runtime p95/p99 (20%), bounded RSS/cold-start (20%), operability/deployment (20%), Neo4j/retrieval integration cost (15%), developer productivity/diagnostics (15%), long-term ecosystem/support (10%). These are **initial weights**; publish both scores and sensitivity analysis before language ADR locks. Run same corpus/OS/CPU, warmup and concurrency; report runs, variance and error bars.

## Initial performance budgets (hypotheses)

- Pure in-memory policy/state decision: p95 < 5 ms, p99 < 15 ms.
- Warm local adapter request to decision: p95 < 30 ms, for no graph/network work.
- Minimal kernel cold startup: < 500 ms on documented reference machine.
- All relevant memory and CPU recorded per concurrent session and per 10k actions.
- Retrieval: no regression from V1 agreed golden quality, and explicit memory/refresh budgets.
- Tool authorization overhead: report absolute duration and proportion of complete agent turn; never hide model/network cost.

These budgets can be revised after M0 measurement with an ADR, not silently relabeled 'passed.'

## Reference workloads

A. 10k no-effect policy requests with rule conflict and tenant scope.  
B. 100 sessions with 10 simultaneous tool requests and approval races.  
C. 1k sessions/500 PRs per day replay from realistic event distributions; this is a planning stress profile, not proof of enterprise scale.  
D. 100k/1m doctrine+memory records with bounded graph fan-out and retrieval abstention.  
E. Chaos matrix: lose graph, ledger, kernel, host, broker and telemetry separately.  
F. Test public/provider-adapter behavior changes across supported host versions.  
G. Symlink, invalid UTF-8, 1MB content, shell composition, unsupported tool dispatch.  
H. Duplicate request before/after effect and uncertain receipt, with irreversible action simulation.

## CI pipeline target

Lint/style -> schema validation -> cross-runtime golden tests -> unit/property tests -> isolated integration -> adversarial controls -> benchmark drift check -> docs/link/HTML validation -> package/release attestations. Parallel integration suites must have isolated state stores; never share test Neo4j graph across shards. Record revision of tests and policy bundles in artifacts.

## Release criteria

All P0/P1 security findings resolved or risk accepted by explicit authority with limited scope. No privilege escalation via a UI 'ask' conversion. Full effect coverage inventory published. Strict fail-closed behavior on unavailable policy/broker/receipt store. Proven tenant isolation including graph neighbors. Restore and reconciliation drills pass. No unverified 'mechanical' rule labels. Production SLOs measured with actual workloads, not invented.

## Evidence artifact format

~~~json
{
  "commit": "<SHA>", "benchmark_version": "v2-0.1",
  "hardware": "<model, memory, OS>", "runtime": "<version>",
  "workload": "<fixture digest>", "sessions": 100,
  "latency_ms": {"p50": null, "p95": null, "p99": null},
  "rss_mb": null, "throughput_per_sec": null,
  "test_results": {"passed": null, "failed": null},
  "notes": "No numbers until executed"
}
~~~
