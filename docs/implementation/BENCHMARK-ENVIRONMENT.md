# Benchmark environment record (M0-04b)

**No benchmark has been run.** This file records the candidate reference machine so M1 results can cite it.

## Workstation recorded 2026-11-05

| Field | Value |
|---|---|
| CPU | AMD Ryzen 9 7940HS, 8 cores / 16 threads, max 5263 MHz |
| Memory | 30 GiB |
| OS | Ubuntu 24.04.3 LTS, Linux 6.17.0-40-generic x86_64 |
| CPU governor | powersave (must be set and recorded per run; laptop thermal and power state affect results) |
| Python | 3.12.3 (M0 oracle only) |
| SQLite (Python module) | 3.45.1 |
| C compiler | gcc 13.3.0 |
| Rust toolchain | **not installed** |
| Go toolchain | **not installed** |

## Required per M1 run

Commit SHAs of both prototypes and of the fixtures; compiler versions and flags; governor and power source; background load; workload generator seed; run count; raw latency samples or histogram file; peak RSS; cold-start samples. Use the evidence JSON format in [verification-and-benchmarks](../testing/verification-and-benchmarks.md#evidence-artifact-format).

A laptop is acceptable for development comparisons only if both runtimes are measured under identical conditions in the same session. Published selection evidence should repeat on a fixed-frequency machine.
