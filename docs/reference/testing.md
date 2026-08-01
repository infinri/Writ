# Testing and benchmarks

How the suite is built, why it's shaped that way, and the traps to avoid when adding tests. Source of truth: `tests/conftest.py`, `tests/_corpus.py`, `tests/_daemon.py`, `tests/fixtures/`, `benchmarks/`.

## Running

```bash
make test     # pytest tests/ -x -q
make bench    # benchmarks/bench_targets.py (contractual perf floors)
make check    # test + bench + writ validate
```

367 test modules, ~5,700 collected tests. Always use the venv interpreter (`.venv/bin/python`): the system interpreter lacks `onnxruntime` and fails the embedding tests. Markers: `perf` (latency-floor tests), `integration` (needs a live `claude` CLI, gated behind `WRIT_INTEGRATION_TESTS=1`), `no_friction_isolation` (opts out of the log redirect).

## Isolation, forced at import time (`tests/conftest.py`)

- **`WRIT_PORT=8799`**: the suite never touches the interactive daemon on 8765 (the singleton tests use 8791). `tests/_daemon.py` owns the test daemon's lifecycle and realigns it if its cache dir diverges from the suite's.
- **`WRIT_CACHE_DIR`** defaults to a fresh `mkdtemp`: without it, subprocess tests would pollute the real `var/session` (the production default moved off `/tmp`, which made the install dir the fallback).
- **`WRIT_NO_AUTOSTART=1`**: a test hitting a dead port must not auto-start a real daemon that outlives the run answering `mode=""` forever (an order-dependent failure class).
- **Friction/log redirect** (autouse): `WRIT_FRICTION_LOG` and `WRIT_LOG_ROOT` point into the test's tmp dir so no test event lands in real streams.

## The anti-masking contracts

Roughly half the suite needs a reachable Neo4j. The rule, encoded in `tests/_corpus.py::classify_corpus_state`: **unreachable is the only legitimate skip; a reachable-but-empty graph must FAIL.** An empty graph previously masked a real regression as a skip. Corpus expectations: 280+ rules, and the exact methodology census (5 SubagentRole, 15 Playbook, 13 Skill, 20 Phase); the session-start probe self-heals a cold graph by re-importing `bible/`, and `pytest_sessionfinish` unconditionally restores the shipped corpus from `writ-corpus.cypher` (the earlier count-gated restore left methodology nodes missing after a run).

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

Published numbers are machine-relative: the recorded runs come from a 16-thread AMD Ryzen 9 7940HS with 31 GiB RAM and an uncapped Neo4j container (512M pagecache). `scale_benchmark.py` writes the exact environment into the "Measurement environment" section of `SCALE_BENCHMARK_RESULTS.md` on every run.

**Floors are floors, not targets** (`tests/fixtures/regression_floors.py`): the build fails below them; they were deliberately walked down as the corpus grew 4x, with each step's measurement recorded in the file's history table. Quote measured values with their dates, never the floors, when describing quality.

## Ground-truth fixtures

`tests/fixtures/ground_truth_queries.json` (193 queries: MRR on the 47 ambiguous, hit rate on all), `ground_truth_negatives.json` (20 negatives behind the abstention gate; its header records the measured finding that only *raw* cosine separates them), `ground_truth_proc.json` (signed-off 40-query methodology set with curation provenance). `*.candidates.json` files are drafts, explicitly not metrics of record. `tests/efficacy_suite/` holds the paired A/B tasks (a planted IDOR true-positive arm and a clean false-positive arm).
