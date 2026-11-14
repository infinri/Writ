# M1-06 repeat run, seed 20261121 (reproducibility bundle)

Official repeat run for the M1-08 confirmation (owner decision 2026-11-11, seeds 20261121 and 20261131 approved). Unmodified harness output (binary paths are recorded repo-relative by the harness since `bf94cdf`; no sanitization was needed).

| File | SHA-256 |
|---|---|
| `2026-11-12-m1-06-seed-20261121.json` | `29310cf131df09df6fab849edf52c6e3b07fe340a5f9752bef821037fd41bed2` |
| `2026-11-12-m1-06-seed-20261121.json.samples.json.gz` | `f704fccdc5839929dfe0732615a3619a4a5e08c9b06beed3caa481499775ad80` |
| `official-run-log.txt` | `16acba7158f780e6e2cefc376f467c997e9d3877374c3e023340f595bba0f811` |

**Provenance:** commit `bbdd092e7499c1b81ff31e6d971b630d090accfe` (clean `m1/` tree; CI run 43085390484 green, all 5 jobs); benchmark version `v2-0.1`, format `writ-m1-bench/1`, profile `protocol`; seed 20261121; run 2026-11-12T13:46:47Z to 2026-11-12T13:54:14Z. Binaries: Rust `sha256:486a18276b0369afe964d01dc6faa988ecadf1bca8aefcf3aac4baf6c0bda387`, Go `sha256:15c7b885488bfbc7d1a03e2b638319f0142353bde935c99d50f770b599d041d4` (`-trimpath`, tags `libsqlite3 sqlite_omit_load_extension`); toolchains rustc and cargo 1.99.1, go 1.27.3; SQLite 3.54.1 r2. Includes the corrected `run_ns` boundary (E-048).

**Guard:** passed at the start (`guard.passed: True`, `valid_for_comparison: True`); start CPU 46.625 C, load 0.55; governor `performance`; AC online, battery Charging. During the run: 90 environment samples every 5 s (`environment_samples`: temperature, current and average clocks against the hardware maximum, governor, load, battery state); end CPU 80.5 C. This AMD host exposes no `thermal_throttle` counter (recorded). Correctness failures: 0.

**Command:** `source m1/tools/env.sh && python3 m1/bench/bench.py --seed 20261121 --rust m1/rust/target/release/proto --go m1/go/proto --out m1/bench/results/2026-11-12-m1-06-seed-20261121.json`, started by a read-only watcher that re-checked the full guard immediately before (log: `official-run-log.txt` in the seed 20261121 bundle). Run order alternates which prototype goes first per run.

**Verify:** `sha256sum` of each file must match the table.
