# M1 benchmark harness (M1-06a)

Harness for the M1-06 measurement protocol in [M1-PARITY-SUITE](../../docs/implementation/M1-PARITY-SUITE.md#measurement-protocol). **No benchmark has been run with it.** Python standard library only; reuses the M1-05 broker generators in `m1/props/wprops`.

## Modes

| Mode | Command | Guard | Records |
|---|---|---|---|
| Dry run (plumbing check, any environment) | `python3 m1/bench/bench.py --dry-run --seed 20261107 --rust m1/rust/target/release/proto --go m1/go/proto --out dry.json` | evaluated and reported, not enforced | every workload on tiny inputs, all correctness checks, environment; every timing and memory value is `null`; `not_a_benchmark: true` |
| Measurement | same without `--dry-run` (optionally `--cpu-list 2-3`) | **enforced**: refuses (exit 3, nothing run or written) unless every condition holds | protocol profile; raw samples in `<out>.samples.json.gz` (digest recorded) |

Guard conditions (all read-only from sysfs and procfs; the harness never changes system settings): mains adapter online; `performance` governor on every cpufreq policy; boost state readable (recorded, not forced); 1-minute load average at most `--max-load` (1.0); CPU temperature readable and at most `--max-start-temp-c` (60 C) at start; no uncommitted changes under `m1/`; repository commit known; `rustc` and `go` on PATH and the Go binary's build info match the versions pinned in `m1/tools/install-toolchains.sh` (`source m1/tools/env.sh` first). The owner sets the governor (`sudo cpupower frequency-set -g performance`); start and end thermals, power, governors, EPP, platform profile and load are recorded.

Exit codes: 0 done and every correctness check passed; 2 usage or missing binary; 3 guard refused; 4 a correctness check failed (result written, `valid_for_comparison: false`).

## Workloads and checks

| Name | Command | Protocol profile | Correctness gate during the run |
|---|---|---|---|
| A: policy decisions | `decide-batch`, `bench-decide` | 1,000 warm-up + 10,000 decisions per process, 10 runs; generated from the 26 golden cases varying request ids, idempotency keys, tenant (20%) and resource (50%) | Rust output equals Go output per case; every `bench-decide` decision equals the same prototype's `decide` output |
| B-mem, H-mem | as B and H with the in-memory ledger | same | same; labelled simulated, never a substitute for B or H |
| B-cold | as B with `--concurrency-profile cold` (D-027 supplemental: worker connections created after release) | same | same; reported separately, never combined with B (which runs the `primary` profile) |
| B: approval races | `broker-scenario --ledger sqlite` (fresh database directory per process, D-023) | 100 sessions, each one concurrent group of 10 executes with shared grants and shared or conflicting keys, 10 runs | every observed group equals a permitted serialization (M1-05 model) and broker run invariants hold |
| G: hostile input | `decide-batch` | 1,000 lines, 5 runs: symlink paths (created in a disposable directory), invalid UTF-8, lone surrogates, 1 MiB content and resource, shell composition, unknown kind and capability | exact Rust and Go equality, identical across repeats |
| H: duplicates and uncertain receipts | `broker-scenario --ledger sqlite` | 1,000 scenarios, 5 runs, 7 templates (duplicate after effect, after reserve crash, before reserve crash, after failure, uncertain receipt and reconcile, granted duplicate then exhausted, restart between) | exact Rust and Go equality, identical across repeats |
| Cold start | `decide` | 30 launches per prototype to first answered line | answers identical |
| Concurrency | `decide-batch` | 10,000 decisions split across 1, 10, 100, 1,000 simultaneously started processes | all answered, Rust equals Go |

Runs alternate which prototype goes first. Peak RSS is a separate full run of each workload under `/usr/bin/time -v`. Percentiles are nearest-rank. Seeds: A uses `seed`, its warm-up `seed+1`, concurrency `seed+2`; B, G and H derive from `seed`; every workload's input digest is recorded.

**Kernel latency versus IPC.** `bench-decide` (both prototypes) answers `{"decision": <the decide output>, "kernel_ns": n}` where `n` is the monotonic time to produce the decision from the already parsed input, excluding JSON parsing, encoding and I/O. Batch wall time (launch to exit) includes process start, parsing and pipes. No model or network time is involved.

## Not covered (recorded in every result under `not_measured`)

- Workload E (chaos): not implemented.
- Every run first checks that both prototypes report the identical SQLite engine (3.54.1, source id, compile options) and the D-023 settings (`proto sqlite-info`); a difference fails the run.
- **Broker timing (M1-06d):** B, B-mem, H and H-mem also run through `bench-broker-scenario` in separate timed runs (same correctness checks on the unwrapped scenario output; H requires byte equality with the plain run). Reported per prototype under `broker_timing_ns`: per operation `total_ns` (end to end), `validate_ns` (broker request validation), `decide_ns` (kernel authorization), `key_lock_wait_ns`; per transaction `begin_ns` (lock acquisition and busy waits), `body_ns`, `end_ns` (commit or rollback); per line `parse_ns`, `open_ns`, `final_ns`, `run_ns`; and `process.overhead_ns` (process wall time minus in-process parse and run: process start, pipes, serialization). Concurrent-group operations are reported under the `concurrent.` prefix. D-027 adds `concurrent.ready_ns` (group ready point), `concurrent.worker.setup_ns` (worker connection setup, outside the operation's `total_ns` in `primary`) and `concurrent.worker.release_delay_ns` (`start_ns - ready_ns`: how long after the ready point each worker actually started; this exposes release simultaneity without changing it). Definitions: LEDGER-SQLITE.md "Per-operation timing".
- G symlinks: M1 kernels do no filesystem access, so symlink paths exercise string handling only.

## Result format

`writ-m1-bench/1`, [result.schema.json](result.schema.json); includes per (prototype, workload) records in the [evidence format](../../docs/testing/verification-and-benchmarks.md#evidence-artifact-format).

## Self-tests

`python3 -m unittest discover -s m1/bench/tests` (stats, guard on fake sysfs trees including a refused measurement that writes nothing, generators). With `WRIT_BENCH_RUST` and `WRIT_BENCH_GO` set it also runs the dry run and asserts no timing or memory value is recorded; with `jsonschema` installed (e.g. `m0/.venv/bin/python`) it validates the schema.
