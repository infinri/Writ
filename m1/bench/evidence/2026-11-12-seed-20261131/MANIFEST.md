# M1-06 repeat run, seed 20261131 (reproducibility bundle)

Official repeat run for the M1-08 confirmation (owner decision 2026-11-11, seeds 20261121 and 20261131 approved). Unmodified harness output (binary paths are recorded repo-relative by the harness since `bf94cdf`; no sanitization was needed).

| File | SHA-256 |
|---|---|
| `2026-11-12-m1-06-seed-20261131.json` | `053123112a44057636f138674684169b17255ca3d5cfc1c8f8270f7c049aa27d` |
| `2026-11-12-m1-06-seed-20261131.json.samples.json.gz` | `5423d4958807b106f4d4233ea4600f9edafa9005200c048da31cb1d5828f40ff` |

**Provenance:** commit `bbdd092e7499c1b81ff31e6d971b630d090accfe` (clean `m1/` tree; CI run 43085390484 green, all 5 jobs); benchmark version `v2-0.1`, format `writ-m1-bench/1`, profile `protocol`; seed 20261131; run 2026-11-12T14:03:15Z to 2026-11-12T14:10:17Z. Binaries: Rust `sha256:486a18276b0369afe964d01dc6faa988ecadf1bca8aefcf3aac4baf6c0bda387`, Go `sha256:15c7b885488bfbc7d1a03e2b638319f0142353bde935c99d50f770b599d041d4` (`-trimpath`, tags `libsqlite3 sqlite_omit_load_extension`); toolchains rustc and cargo 1.99.1, go 1.27.3; SQLite 3.54.1 r2. Includes the corrected `run_ns` boundary (E-048).

**Guard:** passed at the start (`guard.passed: True`, `valid_for_comparison: True`); start CPU 51.875 C, load 0.83; governor `performance`; AC online, battery Charging. During the run: 85 environment samples every 5 s (`environment_samples`: temperature, current and average clocks against the hardware maximum, governor, load, battery state); end CPU 81.875 C. This AMD host exposes no `thermal_throttle` counter (recorded). Correctness failures: 0.

**Command:** `source m1/tools/env.sh && python3 m1/bench/bench.py --seed 20261131 --rust m1/rust/target/release/proto --go m1/go/proto --out m1/bench/results/2026-11-12-m1-06-seed-20261131.json`, started by a read-only watcher that re-checked the full guard immediately before (log: `official-run-log.txt` in the seed 20261121 bundle). Run order alternates which prototype goes first per run.

**Verify:** `sha256sum` of each file must match the table.
