# Testing and benchmarks

How the suite is built, why it's shaped that way, and the traps to avoid when adding tests. Source of truth: `tests/conftest.py`, `tests/_corpus.py`, `tests/_daemon.py`, `tests/fixtures/`, `benchmarks/`.

## Running

```bash
make test            # starts the disposable Neo4j, then pytest tests/ --maxfail=10 -q
make test-graph-up   # create or start that instance and warm it (idempotent)
make test-graph-down # stop it, keeping its data
make bench           # benchmarks/bench_targets.py (contractual perf floors)
make check           # test + bench + writ validate
```

398 test modules, 7,137 collected tests. Always use the venv interpreter (`.venv/bin/python`): the system interpreter lacks `onnxruntime` and fails the embedding tests. Markers: `perf` (latency-floor tests), `integration` (needs a live `claude` CLI, gated behind `WRIT_INTEGRATION_TESTS=1`), `no_friction_isolation` (opts out of the log redirect).

`--maxfail=10`, not `-x`: on a suite this size `-x` reports exactly one failure per run, so reaching green costs one run per failure at minutes each. Ten gives the whole picture and still refuses to grind through a broken suite.

### `make test` is an end-of-program command

**During a cycle, run only the test files that cycle touches. Exactly ONE full-suite run happens, at the very end, and the orchestrator runs it.** Two full-suite invocations inside a single cycle once cost 785 seconds of a 2,495-second run and surfaced nothing the touched files had not.

**Pass every path to ONE pytest invocation** rather than invoking pytest once per path. `pytest_sessionstart` (`tests/conftest.py`) runs a Neo4j warmth probe on every invocation, so N processes pay that probe N times; one process pays it once no matter how many paths follow it.

```bash
# One probe, one interpreter start, one collection:
.venv/bin/python -m pytest tests/test_a.py tests/test_b.py tests/test_c.py -q
# Three of each, for exactly the same tests:
.venv/bin/python -m pytest tests/test_a.py -q
.venv/bin/python -m pytest tests/test_b.py -q
.venv/bin/python -m pytest tests/test_c.py -q
```

**`-k` is not a narrower run.** A `-k` expression selects *after* collection, so it still collects all 7,137 tests and pays the whole import cost before deselecting; only a path argument keeps tests out of the collection. Use paths, and `-k` only to pick within paths you already named.

## Isolation, forced at import time (`tests/conftest.py`)

- **`WRIT_PORT=8799`**: the suite never touches the interactive daemon on 8765 (the singleton tests use 8791). `tests/_daemon.py` owns the test daemon's lifecycle and realigns it if its cache dir diverges from the suite's.
- **`WRIT_CACHE_DIR`** defaults to a fresh `mkdtemp`: without it, subprocess tests would pollute the real `var/session` (the production default moved off `/tmp`, which made the install dir the fallback).
- **`WRIT_NO_AUTOSTART=1`**: a test hitting a dead port must not auto-start a real daemon that outlives the run answering `mode=""` forever (an order-dependent failure class).
- **`WRIT_NEO4J_URI` / `WRIT_NEO4J_USER` / `WRIT_NEO4J_PASSWORD` plus `WRIT_TEST_GRAPH=1`**: the suite talks to its own Neo4j instance on port 7688, never the one the interactive daemon serves. See [the section below](#the-suite-runs-against-its-own-neo4j-instance).
- **Friction/log redirect** (autouse): `WRIT_FRICTION_LOG` and `WRIT_LOG_ROOT` point into the test's tmp dir so no test event lands in real streams.

## The anti-masking contracts

Roughly half the suite needs a reachable Neo4j. The rule, encoded in `tests/_corpus.py::classify_corpus_state`: **unreachable is the only legitimate skip; a reachable-but-empty graph must FAIL.** An empty graph previously masked a real regression as a skip. Corpus expectations: 280+ rules, and the exact methodology census (5 SubagentRole, 15 Playbook, 13 Skill, 20 Phase). On an isolated run (the default, below) the session-start preflight warms a cold instance from the tracked `writ-corpus.cypher` and `pytest_sessionfinish` skips the restore, because a throwaway instance has nothing to repair. Under `WRIT_TEST_NO_ISOLATION=1` the old behaviour stands unchanged: the probe self-heals from `bible/`, and `pytest_sessionfinish` restores the shipped corpus from `writ-corpus.cypher` on every run (the earlier count-gated restore left methodology nodes missing after a run).

## The suite runs against its own Neo4j instance

**This is the default for the whole suite, not an opt-in for destructive tests.** `tests/conftest.py` points every Neo4j resolution at a disposable instance on host bolt port **7688** at module import, before pytest imports a single test module, and marks it disposable with `WRIT_TEST_GRAPH=1`. Import time is the only time that works: at least seventeen modules bind `NEO4J_URI = get_neo4j_uri()` at their own import, so an override applied any later is read by a module that already cached the production URI.

The instance has one recipe and it lives in `scripts/test-graph.sh` (container `writ-test-neo4j`, image `neo4j:5`, host bolt 7688, `neo4j/writtestpass`). Read the script rather than copying its `docker run` line anywhere: a retyped recipe is how a "test" instance ends up on the production port.

```bash
make test-graph-up            # create when absent, start when stopped, wait for bolt,
                              # replay writ-corpus.cypher only if it had to do either
make test-graph-down          # stop it; the data stays, so the next `up` starts warm
bash scripts/test-graph.sh status
```

`make test` depends on `test-graph-up`, so the documented entry point never fails for a missing container. **Bare `pytest` starts nothing and refuses instead**, at session start, before the first test, in three cases: the resolved URI is the production `(host, port)`; the disposable instance does not answer; the instance answers but the corpus replay left it below the census. A refusal costs one command to clear. The rejected alternative, skipping, is cheap to produce and indistinguishable from a green run, which this repo has already paid for twice.

Why the whole suite and not just the destructive tests: a full wipe (`clear_all(preserve_labels=frozenset())`) deletes the runtime records: `Memory`, `Decision`, `FileChange`, `Commit`. Rules come back from `bible/` or `writ-corpus.cypher`; **a `Decision` record has no source and cannot be rebuilt at all.** Running the suite against the interactive instance destroyed them (the graph held 2 `Memory` nodes against 98 on-disk memory files, with `Decision`/`FileChange`/`Commit` at 0). `clear_all` **refuses** an everything-wipe unless the target instance is explicitly disposable, raising `FullWipeRefused` before issuing any statement; the bare `clear_all()` default and every partial preserve set are unaffected. Neo4j Community serves one database per instance, so isolation can only mean a separate **instance on another bolt port**.

Both halves of that permission are required and neither is sufficient alone: `WRIT_TEST_GRAPH` is one sticky global that an old shell profile can leave set, and a differing URI alone would let a config typo authorize a wipe with no human involved. Instances are compared by `(host, port)` with loopback aliases collapsed, so `bolt://127.0.0.1:7687` cannot masquerade as "not production". CI learned that the hard way: the marker was never set there AND the service published 7687, so the `disposable_graph`-gated tests skipped in every run from the day they were written until the test job moved to 7688.

`WRIT_NEO4J_URI` / `WRIT_NEO4J_USER` / `WRIT_NEO4J_PASSWORD` override `writ.toml` for any process, and conftest sets all three with `setdefault`, so an outer environment that already names an instance still wins (that is how CI points at its own service). The password matters as much as the URI: the disposable container uses `writtestpass` while a local `writ.toml` carries the production password, and redirecting the URI alone produces an auth failure that reads as "Neo4j unreachable" and skips half the suite. `get_production_neo4j_uri()` deliberately ignores all three: if the override fed both sides of the comparison, setting it would make every instance look non-production.

### The opt-out, and what it costs

`WRIT_TEST_NO_ISOLATION=1` restores the pre-isolation behaviour exactly: no env forced, no preflight, the end-of-suite corpus restore back on, `scripts/test-graph.sh` a no-op that exits 0. It exists for two honest reasons. A hard requirement with no escape hatch is the thing people work around by commenting out the conftest line, and a machine with no docker daemon must still be able to run the roughly half of the suite that never touches Neo4j.

The costs, stated rather than discovered:

- The suite runs against whatever `writ.toml` configures, which on a developer machine is the **live** graph the interactive daemon serves. Every corpus wipe in the suite lands there, and the end-of-suite replay is the only thing that puts the corpus back.
- `disposable_graph`-gated tests skip, so `tests/test_db_category.py` and `test_graph_dump.py`'s `TestCypherDumpRoundTrip` / `TestRecordPreservationOnReplay` do not run at all.
- Runtime records are only as safe as the guard inside `clear_all`. That guard is real, but it is the last line rather than the design.

The reasoning behind the default, the alternatives it rejected, and the connection-level tripwire it replaced are recorded in [ADR-test-graph-isolation](../adr/ADR-test-graph-isolation.md).

## Traps when writing tests

- **Pinning `WRIT_CACHE_DIR` everywhere is the masking shape** that hid a real bash-default bug for weeks: every test that would have exercised the real default had pinned it. `test_session_cache_dir_parity.py` closes that instance; a new default-path resolver deserves its own parity test, not another pin.
- **Never assert on documentation prose.** Doc staleness is not a code defect; doc-content assertions were removed deliberately (2026-07-31). The generated reference pages are checked by `make docs-check`, not pytest.
- **Don't trust module docstrings for feature state.** Many carry stale RED-phase TDD narratives ("expected to FAIL") from before their feature shipped.
- **Counts live in `test_phase51_doc_counts.py`** (node types, edge types, modes, hooks, endpoints), derived from source; bump there when adding one.
- **Port 8765 is intentionally hardcoded in exactly one test** (`test_advance_phase_token_gate.py`, live-daemon integration); everywhere else use `tests/fixtures/net.py::free_port` and the fixture factories (`server_routes.py`, `session_state.py`, `bitbucket.py` mock transport: no live HTTP in unit tests).
- **Dev credentials are asymmetric on purpose**: tests may hardcode the dev Neo4j credentials, while `test_config_integration.py::TestCliNoHardcodedCreds` enforces that production code never does.
- **`tests/fixtures/gamed_artifacts/trivially_bad/`** encodes named gaming patterns for the quality gates: plans that are empty sections, lorem ipsum, or TODO placeholders; design docs with every header present and every body empty; tests with no assertions, `assert True`, or assertions only on a `Mock()`'s own internals. Use these when a gate must prove it rejects a hollow artifact.

## Benchmarks

- `benchmarks/bench_targets.py`: 14 pass/fail contractual targets against a live migrated graph (integrity < 500 ms, ingest < 2 s, cold start < 3.5 s, memory < 2 GB, retrieval floors, per-stage latencies with warmup). Never wipes the DB; skips with instructions if the corpus is empty.
- `benchmarks/scale_benchmark.py`: the synthetic 80/500/1K/10K curve; wipes and restores under `_corpus_safety` guards (refuses to run if unexported graph-first candidates exist, then snapshots the exact live graph to `var/benchmark-graph-snapshot.cypher` and replays it afterward; a rebuild from `bible/` is NOT a faithful restore). After any wipe/restore, restart the daemon (`systemctl --user restart writ-server`) or `/health` reads degraded.
- `benchmarks/methodology_bench.py`: read-only methodology retrieval vs blocker thresholds.
- `benchmarks/run_benchmarks.py`: traversal latency at 1K/10K synthetic nodes (advisory print, not asserted; same snapshot/restore discipline).

Published numbers are machine-relative: the recorded runs come from a single mid-range developer machine with an uncapped Neo4j container (512M pagecache). `scale_benchmark.py` writes the exact environment into the "Measurement environment" section of `SCALE_BENCHMARK_RESULTS.md` on every run.

**Floors are floors, not targets** (`tests/fixtures/regression_floors.py`): the build fails below them; they were deliberately walked down as the corpus grew 4x, with each step's measurement recorded in the file's history table. Quote measured values with their dates, never the floors, when describing quality.

## Ground-truth fixtures

`tests/fixtures/ground_truth_queries.json` (193 queries: MRR on the 47 ambiguous, hit rate on all), `ground_truth_negatives.json` (20 negatives behind the abstention gate; its header records the measured finding that only *raw* cosine separates them), `ground_truth_proc.json` (signed-off 40-query methodology set with curation provenance). `*.candidates.json` files are drafts, explicitly not metrics of record. `tests/efficacy_suite/` holds the paired A/B tasks (a planted IDOR true-positive arm and a clean false-positive arm).
