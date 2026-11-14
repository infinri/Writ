# Benchmark environment record (M0-04b)

**No benchmark has been run.** This file records the candidate reference machine so M1 results can cite it. The M1-06 harness ([`m1/bench/`](../../m1/bench/README.md)) captures all per-run fields below automatically and refuses to measure unless on mains power with the `performance` governor, a quiet and cool machine, a clean `m1/` tree and the pinned toolchains.

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
| glibc | 2.39 |
| Rust toolchain | 1.99.1 (installed 2026-11-07, M1-00; see below) |
| Go toolchain | 1.27.3 (installed 2026-11-07, M1-00; see below) |

## Required per M1 run

Commit SHAs of both prototypes and of the fixtures; compiler versions and flags; governor and power source; background load; workload generator seed; run count; raw latency samples or histogram file; peak RSS; cold-start samples. Use the evidence JSON format in [verification-and-benchmarks](../testing/verification-and-benchmarks.md#evidence-artifact-format).

A laptop is acceptable for development comparisons only if both runtimes are measured under identical conditions in the same session. Published selection evidence should repeat on a fixed-frequency machine.

## M1 toolchains (M1-00, installed 2026-11-07)

Installed by [`m1/tools/install-toolchains.sh`](../../m1/tools/install-toolchains.sh) into `~/.local/writ-m1-toolchains` (user space, no sudo, no shell rc or system changes); activated per shell with `source m1/tools/env.sh`. No earlier Rust or Go installation existed (`command -v` empty; no `~/.cargo`, `~/.rustup`, `~/go`, `/usr/local/go`, no distro packages).

| Item | Rust | Go |
|---|---|---|
| Version | rustc 1.99.1 (58cf8f9c3 2026-10-13), LLVM 23.1.1; cargo 1.99.1 (5f94df478 2026-08-27) | go1.27.3 linux/amd64 |
| Selection | Current stable per `static.rust-lang.org/dist/channel-rust-stable.toml` (date 2026-10-15) | Current stable per `go.dev/dl/?mode=json` |
| Archive | `rust-1.99.1-x86_64-unknown-linux-gnu.tar.xz` (210,515,736 bytes) from `static.rust-lang.org/dist/` | `go1.27.3.linux-amd64.tar.gz` (70,588,678 bytes) from `dl.google.com/go/` |
| SHA-256 | `231fcae47ee950621374725b5fc3b5441d2fbda2955ac45dd7dc816d8876cfe7` (matches the official `.sha256`) | `65efbc80fdc1fa2cd2631cad5b481777a68e8725f34e89f50fc6ac714e379b40` (matches go.dev JSON and the official `.sha256`) |
| Signature | Good GPG signature, Rust Language Tag and Release Signing Key, primary fingerprint `108F 6620 5EAE B0AA A8DD 5E1C 85AB 96E6 FA1B E5FE` | Good GPG signature, Google Linux Packages Signing Authority, primary fingerprint `EB4C 1BFD 4F04 2F6D DDCC EC91 7721 F63B D38B 4796` (signing subkey `0E22 5917 4146 70F4 442C 250D FD53 3C07 C264 648F`) |
| Components | rustc, cargo, rust-std (x86_64-unknown-linux-gnu), clippy, rustfmt (standalone installer, no rustup) | full Go distribution |
| Environment | `CARGO_HOME` under the toolchain prefix | `GOTOOLCHAIN=local` (no automatic toolchain downloads); `GOPATH` under the prefix |

Trust basis: keys were fetched over HTTPS from the vendors' own domains into a throwaway keyring, and their fingerprints are compared against values pinned in the install script; this is as strong as those published fingerprints, not an independent web-of-trust check.

Go telemetry: mode `local` (Go default): counters are written under `~/.config/go/telemetry`, and nothing is uploaded unless the mode is set to `on`. Left unchanged (no user-config edits); `go telemetry off` would stop local counting.

Smoke tests ([`m1/tools/smoke/run.sh`](../../m1/tools/smoke/run.sh), 2026-11-07): `rustc -O` threaded sum program, `cargo new` plus `cargo build --release --offline`, and Go `go vet` plus `go build` of a goroutine and SHA-256 program all built and ran; Go's `sha256("writ")` = `bd998adf...46bc8adbb` matched `sha256sum`. Output: `rust-smoke ok sum=55`, `Hello, world!`, `go-smoke ok sum=55`, `smoke: all passed`.

Power state at install: CPU governor `powersave`, AC adapter reported offline (on battery). Benchmarks must record and control both.

