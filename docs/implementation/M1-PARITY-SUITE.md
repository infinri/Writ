# M1 Rust/Go parity suite and benchmark protocol (M0-04a)

**Status:** specification. Nothing here has been run in Rust or Go. No result numbers exist. Governs [ADR-0002](../decisions/ADR-0002-runtime-bakeoff.md).

## Principle

Both prototypes consume the **same language-neutral fixtures** and must reproduce the M0 reference oracle (`m0/writ_m0/`, [D-001](DECISION-LOG.md)) exactly. Any disagreement between the prototypes, or with the hand-reasoned golden expectations, is a release stop, not a vote. The oracle itself is checked against hand-written expectations, not the other way round.

## Hard-gate fixture sets

| Set | Source | Pass condition |
|---|---|---|
| Canonical digest | fixed vector in `m0/tests/test_contracts.py::TestCanonical::test_known_vector`; rules in D-006 | Byte-identical canonical form and digest; same rejections (floats, unsafe ints, lone surrogates, depth > 64) |
| Contract validation | `docs/contracts/*.schema.json`, positive and negative fixtures | Same accept/reject per fixture, strict RFC 3339 date-time |
| Golden decisions | `docs/contracts/fixtures/decisions/m0-golden.json` (26 cases, RFC 7396 patches; all pending independent review) | Identical `status`, `reason_code`, `matched_rule_ids` |
| Bundle loading | `docs/contracts/fixtures/policy/m0-reference.bundle.json` and damaged variants from `test_decision.py::TestBundleLoading` | Same bundle sha; same rejections |
| Broker semantics | scenarios in `m0/tests/test_broker.py` | Sealed-ingress refusal (D-010); deadline at use time; capability-set binding; single-use permits; single-use grant under 16-way race; no double dispatch; IDEMPOTENCY_CONFLICT; crash leaves `dispatched`, reconcile yields `unknown_reconcile`; policy change invalidates grants |
| Host normalization | scenarios in `m0/tests/test_independence.py::TestHostNormalization` | Same canonical resource id and action digest for both host extractors; symlink escapes refused |
| Cross-domain evaluation | `docs/contracts/fixtures/domains/` | Same EvaluationRecord status per task and negative case |
| Property tests | Hypothesis properties in `test_contracts.py`, `test_decision.py` | Ported as property tests with at least the same example counts |

A prototype that is faster but fails any row does not pass (testing doc, "A faster unsafe run does not pass").

## Acceptance Gate Integrity readiness (ADR-0008 approved in principle; semantics m1-draft-2, D-017)

M1 must show that each prototype can carry the [Acceptance Gate Integrity](../architecture/acceptance-gate-integrity.md) boundaries without building the evaluation platform. The sets below have **hand-reasoned expectations written before either prototype exists**, are PENDING INDEPENDENT REVIEW, and are specified in `m1/fixtures/` ([review package](M1-02-FIXTURE-REVIEW.md), [amendment 1](../../m1/fixtures/AMENDMENT-1.md)):

| Set | Command | Content | Pass condition |
|---|---|---|---|
| strict-json | `canonical` | Duplicate keys (nested, escaped), alternate and out-of-range numbers, invalid JSON, surrogates, depth | Identical canonical bytes or identical error code |
| transition-decisions | `decide` | Exact-transition authorization: `workflow:<tenant>/<project>/<workflow_id>` resource, typed `from_state` and `to_state`, closed attribute set, structural composite refusal, unscoped transition permits rejected at bundle load, strict parsing of requests | Same status, reason code and matched rules |
| acceptance-binding | `check-acceptance` | Pure check of a record against a trusted, version-bound requirement set and broker state: completeness, trust class, revocation, freshness, scope, artifact and snapshot, policy, revision, expiry, replay, strict parsing | Identical `ok` or refusal code (and `json_error`) |
| transition-scenarios | `transition-scenario` | Promotion transactions: permit bound to full request fingerprint and record, compare-and-set, single-use records, idempotency, 16-way race, refusal without side effects | Identical step results and final state |

Additional properties for M1-05 (not fixture values): any single-field mutation of a bound field refuses (at least 1,000 generated examples per prototype); approval request ids differ when `to_state` differs; generated duplicate-key and number-spelling inputs are rejected identically by both parsers.

What these sets do **not** test: authentic evaluators, record issuance and signatures (boundary 2), and governed execution and artifact immutability (boundary 4). See the boundary table in `m1/fixtures/transition/SCENARIOS.md`.

The M0 Python harness is a driver participant only for `canonical-vector` and `golden-decisions`; it does not implement D-017 (strict-json 5/22, transition-decisions 13/35 when run after the expectations were frozen).

## Evaluation dimensions (owner list, 2026-11-06)

| # | Dimension | How measured |
|---|---|---|
| 1 | Deterministic authorization verdict parity | Golden, transition and workload A sets; byte-identical decisions |
| 2 | Policy precedence and default deny | Golden cases 05 to 09, 18; transition set |
| 3 | Canonical serialization and digest consistency | Canonical digest set; record canonicalization |
| 4 | Tenant, project and principal isolation | Golden scope cases; transition and binding scope mutations |
| 5 | Invalid and malformed input | Contract negatives; workload G; malformed records |
| 6 | Approval replay and idempotency | Broker scenarios; transition replay |
| 7 | Property-based and differential tests independent of the Python oracle | Hand-reasoned acceptance sets; ported Hypothesis properties; Rust versus Go differential on generated inputs |
| 8 | Cold startup time | 30 launches to first decision |
| 9 | Memory footprint and allocation | Peak RSS per workload; allocator statistics where available |
| 10 | Decision latency and concurrency | p50, p95, p99 at 1, 10, 100, 1,000 sessions; kernel separate from IPC and ledger |
| 11 | Build complexity, dependency weight, operational ergonomics | Clean build time, dependency count and licences, binary size, diagnostics quality |
| 12 | Cross-platform packaging | Linux x86_64 measured; macOS and Windows build feasibility recorded, measured only if hardware is available |

Selection still follows the rule below: hard gates first, then the weighted rubric; speed alone never decides.

## Prototype interface

Each prototype ships one binary, `proto <command>`, driven by the language-neutral parity driver `m1/driver/parity.py` (M1-01). Every command uses the same JSON Lines envelope:

```text
stdin   one object per line: {"id": "<case id>", "input": <JSON value>}
                          or {"id": "<case id>", "input_text": "<raw JSON text the prototype must parse itself>"}
        any command may receive input_text; it is parsed with the strict rules (D-017) and is otherwise equivalent to input
stdout  exactly one object per input line, any order: {"id": "<case id>", "output": {...}}
exit    0 when every line was answered; a refusal or rejection is an output, never a crash
```

| Command | `input` / `input_text` | `output` |
|---|---|---|
| `canonical` | `input_text`: raw JSON (parsing is part of the test) | `{"canonical": "<utf8>", "digest": "sha256:..."}` or `{"error": "<CODE>"}` |
| `decide` | `{"bundle": {...} \| null, "state_revision": N, "now": "<rfc3339>", "request": {...}}` | Decision JSON (`status`, `reason_code`, `matched_rule_ids`, ...); strict-parse failure is `invalid` / `REQUEST_JSON_INVALID`; a bundle rejected at load is `unavailable` / `POLICY_BUNDLE_REJECTED` |
| `decide-batch` | same as `decide`, many lines (workload A) | Decisions |
| `broker-scenario` | scenario JSON | receipts and refusal codes |
| `check-acceptance` | `{"record": {...}, "current": {...}, "requirements": {...}}` | `{"ok": true}`, `{"refusal": "<CODE>"}` or `{"refusal": "INPUT_JSON_INVALID", "json_error": "<CODE>"}` |
| `transition-scenario` | `{"initial": {...}, "steps": [...]}` | `{"steps": [...], "final": {...}}` |

The driver records the prototype argv and executable digest, manifest and fixture-file digests, repository commit, per-set pass and fail counts, protocol errors (missing, duplicate, unknown or non-JSON lines, crash, timeout) and nondeterminism across `--repeat` runs. It exits 0 only if every selected set passes. Self-tests: `python3 -m unittest discover -s m1/driver/tests`.

Neither prototype may link a model-provider SDK, an AgentRunner, or a pack module into the decision path (INV-21, ADR-0006).

## Workload mapping (testing doc workloads A-H)

| Workload | In M1 | Notes |
|---|---|---|
| A: 10k policy decisions with conflicts and tenant scope | yes | Generated from golden cases by varying ids, tenants, resources; generator seed recorded |
| B: 100 sessions x 10 concurrent requests with approval races | yes | SQLite ledger, WAL, `BEGIN IMMEDIATE` semantics |
| C: 1k sessions / 500 PRs replay | no | Needs realistic event distributions; deferred to M5+ |
| D: 100k / 1m doctrine and memory records | no | Retrieval and graph, M7/M8 |
| E: chaos (lose ledger, kernel, broker separately) | partial | Ledger and broker kill only; graph and telemetry out of scope |
| F: host version changes | no | Requires real host adapters, M5 |
| G: symlink, invalid UTF-8, 1 MB content, unsupported tool | yes | Fixture generator must create symlinks in a disposable temp dir |
| H: duplicate before/after effect, uncertain receipt | yes | Same as broker scenarios |

## Measurement protocol

- Same machine, OS, CPU governor and power profile for both; record them in [BENCHMARK-ENVIRONMENT](BENCHMARK-ENVIRONMENT.md).
- Release builds; record compiler versions, flags and commit SHAs.
- Warm-up 1,000 decisions, then 10 runs of workload A; report p50/p95/p99, mean, standard deviation and run-to-run variance.
- Cold start: 30 process launches to first decision.
- Peak RSS via `/usr/bin/time -v` per workload.
- Concurrency at 1, 10, 100, 1,000 simulated sessions.
- Report kernel decision latency separately from IPC and ledger latency. No model or network time is part of this benchmark.
- Results use the evidence format in the testing doc; `null` until measured.

## Selection

Hard gates first; then the weighted rubric in the testing doc with sensitivity analysis. Record the scorecard and decision in ADR-0002 and set it ACCEPTED only then.
