# writ-broker dependency policy (M3-00, M3-02)

Reviewed separately from the kernel (`kernel/DEPENDENCIES.md` stays in force for `writ-kernel`, whose closure the gate keeps at exactly its reviewed set). The broker holds keys and talks to storage only through `broker::store`; it must not depend on a storage driver (`rusqlite` and `libsqlite3-sys` are forbidden here) or on async, network or model-SDK crates. Dependencies are added only when code uses them, with the same rules as the kernel (exact pins, at least 14 days old at adoption, licence and advisory review, separate development dependencies, `Cargo.lock` and `--locked`).

**Gate:** `kernel/tools/check_deps.py` with `broker/deps-allowlist.json`.

## Runtime dependencies (M3-02)

| Crate | Pin | Features | Licence | Released | Purpose |
|---|---|---|---|---|---|
| `writ-kernel` | path (workspace member) | none | MIT | n/a | use-time re-decision (`decision::decide`), contracts, D-006 canonical form and digest |
| `hmac` | `=0.12.1` | default off | MIT OR Apache-2.0 | 2022-02-17 | HMAC-SHA256 ingress seals (D-010) and permit MACs (SR-07); `verify_slice` compares tags in constant time |
| `sha2` | `=0.10.8` | default off | MIT OR Apache-2.0 | (as reviewed for the kernel) | SHA-256 for HMAC; same version as `writ-kernel` |
| `rustix` | `=1.1.6` | default off; `time` only | Apache-2.0 WITH LLVM-exception OR Apache-2.0 OR MIT (package-specific approval) | 2026-10-27 | safe `clock_gettime(CLOCK_BOOTTIME)` for the trusted-time elapsed source (M3-TRUSTED-TIME revision 2) |

**Why not the standard library or custom code:** the standard library has no HMAC or SHA-256; a hand-written HMAC and constant-time comparison would be new security-critical code without the review history of the RustCrypto implementations. `hmac` and `sha2` were the M1 bake-off choices (M1-07) and are pinned to the M1-reviewed versions.

**Transitive additions over the kernel closure:** `subtle` 2.6.1 (released 2024-06-24, BSD-3-Clause; constant-time equality, pulled in by the `digest` `mac` feature that `hmac` enables). Every other transitive package (`digest`, `block-buffer`, `crypto-common`, `generic-array`, `typenum`, `version_check`, `cfg-if`, `cpufeatures`, `libc`) is the kernel's reviewed set at the same versions. Broker closure before trusted time: 12 registry packages (18 with `rustix`, below).

**Review record (2026-11-12):** versions and checksums equal those reviewed in M1-07 (`m1/rust/Cargo.lock`) and those published on crates.io (checksums compared 2026-11-12); both new packages are far older than 14 days. `python3 kernel/tools/check_deps.py --osv` on 2026-11-12 reported no advisory for any locked package. Development dependencies: none (test keys and fixtures live only in `#[cfg(test)]` code).

**Licence:** `subtle` is BSD-3-Clause (recorded as a fact in M1-07, never added to an approved list). It is approved in `broker/deps-allowlist.json` for the broker only, **pending owner confirmation**; no other crate uses it directly.

## Trusted time: `rustix` (owner decision 2026-11-12)

**Why:** effective time is `base + elapsed`, and the elapsed clock must keep counting through system suspend (Linux `CLOCK_BOOTTIME`). `std::time::Instant` is not documented to count suspend, and the workspace forbids `unsafe` (no direct `libc` call). `rustix` exposes `clock_gettime_dynamic(DynamicClockId::Boottime)` as a safe, fallible function; the broker uses nothing else from it.

**Pin and features:** `rustix = { version = "=1.1.6", default-features = false, features = ["time"] }`. `std` is not needed (the clock API is in the `no_std` core of the crate). Effective features, from `cargo tree --target all -e normal,build -f "{p} {f}"` on 2026-11-12, are identical in the broker's standalone build and in the combined release (normal, build) and test (plus dev) builds of all production members: `rustix` `time`; `linux-raw-sys` `auxvec, elf, errno, general, ioctl, no_std`; `bitflags` none; `errno` none (default features off); `windows-sys` `Win32, Win32_Foundation, Win32_Networking, Win32_Networking_WinSock, Win32_System, Win32_System_Diagnostics, Win32_System_Diagnostics_Debug, default`. No other member enables further `rustix` features.

| Package | Version | Released (crates.io) | Licence | Checksum (Cargo.lock = crates.io, compared 2026-11-12) | Compiled on x86_64 Linux |
|---|---|---|---|---|---|
| `rustix` | 1.1.6 | 2026-10-27 | Apache-2.0 WITH LLVM-exception OR Apache-2.0 OR MIT | `e5e76742e5294153d4dde1207dbec5bdd81eef6fabdc0d2118babadabfd858aa` | yes (direct; `linux_raw` backend) |
| `linux-raw-sys` | 0.12.1 | 2025-12-23 | Apache-2.0 WITH LLVM-exception OR Apache-2.0 OR MIT | `32a66949e030da00e8c7d4434b251670a91556f4144941d37452769c25d58a53` | yes |
| `bitflags` | 2.13.2 | 2026-09-10 | MIT OR Apache-2.0 | `3ded4057c258ba199e2d26386d3af3780957ecaee6c4ef4041c6b4b8b97c0b06` | yes (same version already approved for `writ-ledger-sqlite`) |
| `errno` | 0.3.14 | 2025-09-09 | MIT OR Apache-2.0 | `39cab71617ae0d63f51a36d69f866391735b51691dbda63cf6f96d042b63efeb` | no (libc-backend and Windows targets only) |
| `windows-sys` | 0.61.2 | 2025-10-06 | MIT OR Apache-2.0 | `ae137229bcbd6cdf0f7b80a31df61766145077ddf49416a728b02cb3921ff3fc` | no (Windows only) |
| `windows-link` | 0.2.1 | 2025-10-06 | MIT OR Apache-2.0 | `f0805222e57f7521d6a62e36fa9163bc891acd422f971defe97d64e70d0a4fe5` | no (Windows only) |
| `libc` | 0.2.191 | 2026-10-16 | MIT OR Apache-2.0 | (already approved) | no for the broker (libc-backend targets; `cpufeatures` on aarch64) |

**Release age:** `cargo update` first resolved `errno` 0.3.15, released 2026-11-12 (0 days); it was pinned to 0.3.14 with `cargo update -p errno --precise 0.3.14` (the newest release at least 14 days old). Every other package is at least 14 days old (`rustix` 1.1.6: 16 days).

**Licence:** `rustix` and `linux-raw-sys` are offered under `Apache-2.0 WITH LLVM-exception OR Apache-2.0 OR MIT`; the expression is approved for these two packages only (`licence_approved` in `broker/deps-allowlist.json`, owner decision 2026-11-12), not as a general licence. Writ uses them under the MIT option; their MIT notices are to be assembled with the other MIT notices before any release (M12). `THIRD-PARTY-NOTICES.md` is not changed by this review.

**Build script:** `rustix` has a `build.rs` that only probes the compiler (`rustc` from `RUSTC`, honouring `RUSTC_WRAPPER`) for language features and target configuration and emits `cfg` flags; it performs no network access or downloads.

**Advisories:** `python3 kernel/tools/check_deps.py --osv` on 2026-11-12: no advisory for any of the 25 locked packages. Closures: `writ-kernel` 10 (unchanged), `writ-broker` 18, `writ-ledger-sqlite` 8; combined release and test builds 25 packages each.

**Open gate items (owner decision, M3-02):**

1. The gate computes closures from the workspace-unified `cargo metadata` resolve. With `hmac` in the workspace, `digest` gains its `mac` feature and lists `subtle` as a dependency, so the gate counts `writ-kernel` at 11 packages, although `cargo tree -p writ-kernel -e normal,build` (per-package feature resolution) still shows exactly the 10 reviewed packages and the kernel source uses no MAC.
2. `writ-ledger-sqlite` depends on `writ-broker` (for `broker::store`), so the gate attributes the broker's whole closure, including `hmac`, to it, and its allow-list forbids `hmac` and lists none of the SHA-2 packages.
