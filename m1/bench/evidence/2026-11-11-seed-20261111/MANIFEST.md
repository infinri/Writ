# M1-06 official run, seed 20261111 (reproducibility bundle)

Tracked copy of the official M1-06 measurement (TEST-EVIDENCE E-046) and its M1-07 build timings (E-047). The local output directory `m1/bench/results/` stays git-ignored; this directory is the preserved evidence (owner decision 2026-11-11).

| File | SHA-256 | Content |
|---|---|---|
| `2026-11-11-m1-06.json` | `13ceb932d486d63d006709f684789a4db9cd30250a038e734c5f96536fbca93b` | result: environment at start and end, guard, toolchains and build commands, binary hashes, workload digests, percentiles, availability, correctness checks |
| `2026-11-11-m1-06.json.samples.json.gz` | `c93804134e982bee3ba3d8d9e0a297c1d379da6464d35a2a9f0f069ceb9cedb8` | every raw sample per workload and prototype (byte-identical to the harness output; its hash is also recorded inside the result) |
| `../../../assess/results/2026-11-11-build-times.json` | tracked separately | M1-07 build timings (5 clean and 5 incremental per language; the incremental case is not comparable, see the scorecard) |

**Provenance:** commit `138163d5777503a893f1263c31f45bd3a1644013` (clean `m1/` tree, CI run 42939567262 green); benchmark version `v2-0.1`, format `writ-m1-bench/1`, profile `protocol`; seed 20261111 (A warm-up 20261112, process concurrency 20261113); run 2026-11-11 01:35:32Z to 01:43:07Z. Binaries: Rust `sha256:e7afa261ff986312a1c4e08cb9b9abfe848336b9c8ce403582ea75b1ab7a1d85` (`cargo build --release --locked`, rustc and cargo 1.99.1), Go `sha256:4d853d89bb06fa63e274d36b4db82b31ebc5f2e0e3a7ff9254b4ba32e6077598` (`go build -trimpath -tags 'libsqlite3 sqlite_omit_load_extension'`, go 1.27.3, `CGO_ENABLED=1`); SQLite 3.54.1 static library r2 (identical engine check recorded). Workload input digests are in `workloads.<name>.digest` and `concurrency.digest`; fixture manifest `sha256:0aa9efe5974f85a7ed734753c7736d6fea6d5c969d90675ea139756b8100f996`.

**Command:** `source m1/tools/env.sh && python3 m1/bench/bench.py --seed 20261111 --rust m1/rust/target/release/proto --go m1/go/proto --out m1/bench/results/2026-11-11-m1-06.json`. Run order alternates which prototype goes first per run (recorded method in `m1/bench/README.md`).

**Sanitization (the only change from the harness output):** in the result JSON, the two absolute binary paths under the user's home directory were replaced by repo-relative paths (`m1/rust/target/release/proto`, `m1/go/proto`). The samples file is unmodified. A scan found no other absolute paths, user names, e-mail addresses, credentials, hostnames or private environment variables; the recorded environment is limited to CPU model, memory size, OS and kernel version, cpufreq settings, temperatures, load and power state.

**Verify:** `sha256sum m1/bench/evidence/2026-11-11-seed-20261111/*` must match the table; `python3 -c "import json,gzip; json.load(gzip.open('...samples.json.gz'))"` must parse.
