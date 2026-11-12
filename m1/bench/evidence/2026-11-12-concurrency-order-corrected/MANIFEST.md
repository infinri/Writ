# Process-concurrency correction bundle (order-controlled, 2026-11-12)

**Supersedes** the process-concurrency (multi-process throughput) results in the bundles `2026-11-11-seed-20261111`, `2026-11-12-seed-20261121` and `2026-11-12-seed-20261131`. Those results are **not order-controlled**: the harness then ran Rust first at every level, once (found in the final evidence check, 2026-11-12). The original bundles are preserved unchanged; every other workload in them alternated order and remains valid.

**Correction:** commit `992383a` (CI run 43245077338 green, all 5 jobs): at every level, two paired rounds on identical inputs, Rust then Go, then Go then Rust; per-round order, wall times and rates recorded; regression test `ConcurrencyOrder` in `m1/bench/tests/test_bench.py`. Workload size, levels (1, 10, 100, 1,000 processes; 10,000 decisions), correctness checks (Rust equals Go in every round) and guards unchanged. Only this workload was re-measured (`--workloads concurrency`), with the same three seeds as the superseded runs (input digest per seed below; seed 20261111's digest equals the original run's).

| File | SHA-256 |
|---|---|
| `2026-11-12-conc-corrected-seed-20261111.json` | `0ed677f454fb4fcffa9865b28080e4af58497c559518143e684f213bef9023bd` |
| `2026-11-12-conc-corrected-seed-20261111.json.samples.json.gz` | `64babff498ccf191bf4da671946657ad131319df918a685f0267ff4cd8ed8e7c` |
| `2026-11-12-conc-corrected-seed-20261121.json` | `5f2d6d399d9d437469e9931d4d705bbaf63f529aa1059ab104d2229a68eeb273` |
| `2026-11-12-conc-corrected-seed-20261121.json.samples.json.gz` | `f2788207751b60a53d4958f86436cbb4f0211983efb2a898a5abf54a0dbc010d` |
| `2026-11-12-conc-corrected-seed-20261131.json` | `9054944988671017ccd91a7eae2421f4d12cac5dd599d841a028658b6857cb51` |
| `2026-11-12-conc-corrected-seed-20261131.json.samples.json.gz` | `7192ca09da7caf5d6448c74b3dfeb2166bafdc9e7e477460599b5923d7cb4105` |
| `official-run-log.txt` | `bf8c0cac673ab9752e1062cdf1b9f5972b12fcc224b7738b886f5a7ed2997426` |

**Binaries:** Rust `sha256:486a18276b0369afe964d01dc6faa988ecadf1bca8aefcf3aac4baf6c0bda387`, Go `sha256:024fd315adf3c24cf58821d218e76ab44b7f5ba6d9b7f37198465f8e0eca9540` (rebuilt at `992383a`; prototype source identical to `bbdd092`, `git diff bbdd092 992383a -- m1/rust m1/go` empty). The Rust binary is byte-identical to the `bbdd092` build; the Go binary differs only because Go embeds the build's VCS stamp (`vcs.revision`, `vcs.time`; `vcs.modified=true` from an untracked owner directory outside `m1/`), so a Go binary hash identifies the commit it was built at, not only its source. Toolchains rustc and cargo 1.99.1, go 1.27.3; governor `performance`; AC online, battery Charging.

| Seed | Started (UTC) | Input digest | Start CPU / load (guard PASS) | End CPU | In-run samples | Correctness failures |
|---|---|---|---|---|---|---|
| 20261111 | 2026-11-12T15:18:32Z | `sha256:383f3e344189d2cd2ad959ae2cef96c0100407276d3aae5305aa31b31d63652b` | 54.5 C / 0.81 | 79.625 C | 1 | 0 |
| 20261121 | 2026-11-12T15:22:37Z | `sha256:002d1c59192e9fd9c2a0c5b47d54f8bfe036390839b9f98b4b345abc0004dd45` | 54.25 C / 0.72 | 82.0 C | 1 | 0 |
| 20261131 | 2026-11-12T15:27:42Z | `sha256:24f6dd8605ddd536c27d297a41d894372ed6932130fefc9d66f1ea11500946de` | 50.75 C / 0.57 | 81.0 C | 2 | 0 |

Each run lasts about 5 s, so the 5 s environment sampler records only 1 or 2 in-run samples; start and end readings are recorded. CPU rises to about 80 C during the 1,000-process burst. This host exposes no thermal-throttle counter.

**Result (decisions/s, median of 6 samples per prototype per level: 3 seeds x 2 rounds; range in parentheses):**

| Processes | Rust | Go | Rust/Go |
|---|---|---|---|
| 1 | 31,754 (27,377 to 31,827) | 21,494 (19,431 to 21,525) | 1.48x |
| 10 | 150,782 (149,375 to 151,635) | 85,638 (84,938 to 132,187) | 1.76x |
| 100 | 198,550 (193,273 to 202,382) | 91,600 (88,204 to 92,750) | 2.17x |
| 1000 | 55,789 (51,379 to 57,887) | 23,941 (23,696 to 24,109) | 2.33x |

Order effect: each prototype's rate relative to its own median was 1.00 (Rust first) and 0.97 (Rust second); 1.04 (Go first) and 0.99 (Go second); small against the differences.

**Command:** `python3 m1/bench/bench.py --workloads concurrency --seed <seed> --rust m1/rust/target/release/proto --go m1/go/proto --out m1/bench/results/2026-11-12-conc-corrected-seed-<seed>.json`, each started by a watcher only after a full guard pass (`official-run-log.txt`). **Verify:** `sha256sum` matches the table.
