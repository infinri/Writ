# M1-06 benchmark readiness (workloads B and H)

**Status (2026-11-10): NOT READY TO MEASURE** (environment). Instrumentation verified; no official Rust versus Go measurement has been taken. Verified at published `6b7eab12a35a82d52e11493c9531268aaf697530`, CI https://github.com/infinri/Writ/actions/runs/42857245639 (all 5 jobs success).

| Requirement (owner, 2026-11-10) | Status | Evidence |
|---|---|---|
| Kernel and broker timings reported separately | met | `bench-broker-scenario` per operation: `decide_ns` (kernel authorization), `validate_ns` (broker request validation), `key_lock_wait_ns`, `total_ns`; harness `broker_timing_ns` keys `op.<op>.decide_ns`, `op.<op>.validate_ns`, `concurrent.op.execute.*` (dry run, 31 keys for B, 35 for H). Kernel-only decision latency for workload A via `bench-decide` |
| SQLite lock waiting and commit or rollback included | met | per transaction `begin_ns` (`BEGIN IMMEDIATE`, busy waits), `body_ns`, `end_ns` (commit or rollback) with `outcome`; harness keys `tx.<name>.begin_ns/body_ns/end_ns` |
| Parsing and protocol overhead distinguishable | met | per line `parse_ns`, `run_ns`, `open_ns`, `final_ns`; harness `process.overhead_ns` = process wall time minus in-process parse and run |
| Equivalent instrumentation in both prototypes | met (by specification and tests) | LEDGER-SQLITE.md "Per-operation timing" start and stop points; `m1/ledger_tests/test_timing_integrity.py` passes for both (shape, part sums, transaction names and outcomes); implemented by separate agents |
| Equivalent engine settings | met | identical SQLite 3.54.1 static library, source id, compile options and settings read back by `proto sqlite-info` in both (CI engine assertion; harness `sqlite.engine_and_settings_identical` check) |
| Timing does not change security or transactional behaviour | met | 982 deterministic case outputs byte-identical between `3012940` and `6b7eab1` builds for both prototypes, both ledgers, every command; timing-integrity scenario equality on every broker fixture; fixtures 444/444 both ledgers |
| Harness refuses unsuitable power, governor, load and thermal conditions | met | guard (`m1/bench/wbench/envinfo.py`) self-tests on fake sysfs trees (battery, powersave, mixed governors, load, temperature, unreadable boost, dirty tree, unpinned toolchains, Go without `-trimpath`); live refusal 2026-11-10 exit 3, nothing written |
| Environment passes every guard | **not met** | 2026-11-10, after the owner set the governor: governor `performance` on 16 of 16 policies (EPP `performance`), boost 1, platform profile `balanced`; **battery BAT1 discharging (100%) while the only online supply is USB-C 5 V / 3 A (about 15 W)**: the "mains" flag is set but the supply cannot carry the load; **1-minute load 1.29 to 1.65** (limit 1.0; other processes, mainly two `java` processes using about 4.7 cores, plus `clickhouse-server`); **CPU 61 to 68.6 C** (limit 60 C). Correctness at `8d1915c` (code of `68f4593`): fixtures 447/447 in memory and on SQLite for both, `m1/ledger_tests` 30 OK, dry run 0 correctness failures |

## Owner actions before M1-06

- **D-026 (decided):** busy-timeout refusals are verified aborted no-ops in the oracle and are reported as availability (B reports successful operations per second and refusal counts separately). **Still open:** concurrent-group start equivalence (E-035): Rust opens worker connections before the start barrier, Go after; decide before comparing availability. Load sources seen 2026-11-10 (no process was stopped): IntelliJ IDEA (`java`, bursts of several cores), Docker containers (22 running: Langfuse with ClickHouse, MinIO, Redis, Postgres, worker and web; n8n with Postgres and runners; OpenSearch; Dependency-Track; Neo4j `writ-neo4j`; MySQL; MongoDB; fitting-engine; foundry-agent), Chrome, Claude Code sessions.

0. Connect the full-power AC adapter (the battery must not be discharging; the guard refuses a discharging battery since 2026-11-10).
1. `sudo cpupower frequency-set -g performance` (done 2026-11-10; privileged; Claude does not change global CPU settings).
2. Stop background load (the `java` and `clickhouse-server` processes seen on 2026-11-10) until the 1-minute load average stays below 1.0; let the CPU idle below 60 C. Claude does not stop the owner's processes.
3. Run from a clean, committed tree with the pinned toolchains and SQLite (`source m1/tools/env.sh`), Go built with `-trimpath` and the D-023 tags.
4. `python3 m1/bench/bench.py --seed <new recorded seed> --rust m1/rust/target/release/proto --go m1/go/proto --out m1/bench/results/<date>.json`; exit 3 means the guard refused, exit 4 a correctness failure (the run is then invalid).
