# M1-07: build, dependency and packaging assessment (in progress)

**Status:** IN PROGRESS (2026-11-07). Input to [ADR-0002](../decisions/ADR-0002-runtime-bakeoff.md) (OPEN). **No language is chosen here**; the M1-08 scorecard combines this with M1-06 measurements and the correctness gates. Every row is labelled **Measured** (reproducible from the commands below, evidence file cited), **Fact** (from documentation or metadata) or **Judgment** (engineering opinion by Claude, not evidence).

Evidence: [`m1/assess/results/2026-11-07-facts.json`](../../m1/assess/results/2026-11-07-facts.json), produced by `source m1/tools/env.sh && python3 m1/assess/collect.py facts --out ...` at commit `935e4e336274a45e1d8661f0af537c9a2cd8841d` (clean `m1/` tree) on the workstation in [BENCHMARK-ENVIRONMENT](BENCHMARK-ENVIRONMENT.md). Both prototypes include the D-023 SQLite ledger and link the same static SQLite 3.54.1 (build revision 2).

## Not yet measured (deferred, guarded)

Compile and incremental build **times** are comparative measurements and depend on CPU frequency and thermals. `python3 m1/assess/collect.py build-times` refuses to run unless the M1-06 benchmark guard passes; it refused on 2026-11-07 (governor `powersave`, load, temperature). No build-time number is reported anywhere. CI job durations exist but run on shared, unpinned GitHub runners and include toolchain setup; they are not evidence for this comparison.

## Facts and measurements

| Criterion | Rust | Go | Label |
|---|---|---|---|
| Toolchain | rustc and cargo 1.99.1, standalone installer, SHA-256 and GPG verified; 616 MB installed | go 1.27.3, SHA-256 and GPG verified; 283 MB installed | Measured (`du`) / Fact |
| Extra build requirement for the SQLite broker | C compiler for the shared SQLite library (both); `libsqlite3-sys` build script (uses `pkg-config`/`vcpkg` crates only as build dependencies) | C compiler and cgo (`CGO_ENABLED=1`); build tags `libsqlite3 sqlite_omit_load_extension` mandatory (untagged build fails by design) | Fact |
| Resolved dependencies (normal and build, excluding dev) | 20 crates: 3 direct (`sha2`, `hmac`, `rusqlite`), 3 build-only (`pkg-config`, `vcpkg`, `version_check`) | 1 module (`github.com/mattn/go-sqlite3 v1.14.52`), no transitive modules; everything else is the standard library | Measured (`cargo metadata`, `go list -m all`) |
| Licences | MIT, MIT OR Apache-2.0, BSD-3-Clause (`subtle`); SQLite public domain | MIT (`go-sqlite3`), BSD-3-Clause (Go standard library); SQLite public domain | Fact (crate metadata, module LICENSE) |
| Known advisories (OSV, 2026-11-07) | none for any resolved crate | none for `go-sqlite3` v1.14.52 or the go1.27.3 standard library | Measured (api.osv.dev) |
| Dependency release age | all at least 14 days old after pinning `smallvec` 1.16.3 (cargo first resolved 1.16.5, published 1 day earlier) | `go-sqlite3` v1.14.52 published 2026-09-05 | Fact |
| Vendored source carried by the drivers (not compiled; the shared library is used) | `libsqlite3-sys` 21 MB in the registry (bundled SQLite and bindings) | `go-sqlite3` 11 MB in the module cache (bundled SQLite) | Measured (`du`) |
| Clean-build reproducibility (two builds, source at different absolute paths, same machine and caches) | **identical** with the default release command | **differs** without `-trimpath` (paths embedded); **identical** with `-trimpath`, which is now the required release and benchmark build (D-024; re-verified 2026-11-10 after the D-024 changes: 5,926,944 bytes, identical) | Measured |
| Release binary size | 2,286,176 bytes; 1,945,560 stripped | 5,947,336 bytes (5,920,400 with `-trimpath`); 4,370,392 stripped | Measured |
| Dynamic libraries | `libgcc_s`, `libc`, loader; no `libsqlite3` | `libc`, loader; no `libsqlite3` | Measured (`ldd`) |
| Cross-target build from this host (with the SQLite broker) | `aarch64-unknown-linux-gnu`: fails (target standard library not installed; also needs a C cross compiler for SQLite) | linux/arm64, darwin/arm64, windows/amd64: all fail (cgo needs a C cross compiler: no `clang`, aarch64 assembler, `windows.h`) | Measured |
| Targets the compiler supports | 331 (`rustc --print target-list`) | 47 (`go tool dist list`) | Measured |
| Source size (physical lines incl. comments) | 4,357 total: 3,521 non-test, 836 test | 5,812 total: 4,390 non-test, 1,422 test | Measured (method in evidence file) |
| Tests | 38 unit + 3 integration | 38 top-level test functions (6 packages, race detector) | Measured |
| `unsafe` in prototype code | none (`unsafe` keyword absent) | none (`unsafe.` absent outside tests) | Measured (grep) |
| CI reliability (`V2 documentation integrity` workflow, this branch) | 30 of 30 recorded runs succeeded; M1 Rust parity job passed in every run since it was added | same; M1 Go parity job passed in every run since it was added | Measured (`gh run list`) |

## Defect history in the bake-off (correctness, not speed)

| Found by | Rust | Go |
|---|---|---|
| M1-05 property testing | JSON objects compared in member order (false refusals, fail-closed) | `-00`/`-01` gave the wrong code; panic on `count -1` (recovered as `INTERNAL_ERROR`, process survived) |
| D-021 fixtures | divergences on unspecified protocol points | same, plus earlier copying of Rust behaviour on two points (agreement only) |
| D-022 fixtures (30 cases) | 5 failures: accepted offset, fraction and lowercase `now`; accepted negative revisions | 22 failures: every malformed `now`, `state_revision` or `request` produced a full Decision with fabricated metadata (`decided_at` `0001-01-01T00:00:00Z`) |
| D-023 SQLite implementation | no failing test during development (implementer report); found the missing `HAVE_USLEEP` build option | no failing test during development (implementer report) |

Both prototypes were implemented by agents from the same specification; defect counts reflect specification gaps as much as language. **Judgment:** they do not, on their own, favour either language.

## Engineering judgments (not evidence; for the owner to weigh)

| Topic | Rust | Go |
|---|---|---|
| Packaging and deployment | Single static-ish binary (only `libgcc_s` and `libc`); reproducible by default | Single binary (only `libc`); needs `-trimpath` for reproducible builds; with cgo SQLite loses Go's usual trivial cross-compilation |
| SQLite integration | Mature typed driver; the shared library is linked through one build-script crate | Mature driver via cgo; a pure-Go driver (e.g. `modernc.org/sqlite`) would restore cross-compilation but runs a translated engine, so it could not use the shared library and would not be engine-identical (D-023) |
| Diagnostics and debugging | Compile-time ownership and exhaustive matching caught classes of errors before tests (the restart field-classification pattern); longer compile and steeper learning curve | Simple tooling, fast iteration, built-in race detector used in every CI run; nil maps and zero values made the fabricated-metadata defect (D-022) easy to write |
| Memory management and concurrency complexity | No garbage collector; explicit lock scopes; threads and connections per operation | Garbage collector; goroutines and `database/sql` connection handling need care (one dedicated connection per worker was required) |
| Suitability for the trusted kernel and broker | Strong fit for a small, pure, key-free kernel with strict parsing; type system helps keep untrusted and trusted data apart | Adequate; standard library covers crypto and HTTP without third-party code, which keeps the supply chain small |
| Future adapters, Neo4j, policy evaluation, Control Center APIs | Ecosystem exists (async runtimes, HTTP, Neo4j drivers) but adds dependencies; each needs review | Standard library HTTP and JSON cover most Control Center needs with no extra modules; Neo4j has an official Go driver |

## Remaining M1-07 work

1. Build timings: **done 2026-11-11** (E-047): clean median Rust 2.65 s, Go 3.69 s; the whitespace-only incremental case is not comparable (Go link cached); a semantic-edit incremental measurement is not done.
2. Cross-target builds: **deferred** (D-024: M1 targets Linux x86_64; no cross toolchains installed).
3. Owner review of the judgments above before M1-08.
