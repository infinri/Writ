# writ-broker dependency policy (M3-00, M3-02)

Reviewed separately from the kernel (`kernel/DEPENDENCIES.md` stays in force for `writ-kernel`, whose closure the gate keeps at exactly its reviewed set). The broker holds keys and talks to storage only through `broker::store`; it must not depend on a storage driver (`rusqlite` and `libsqlite3-sys` are forbidden here) or on async, network or model-SDK crates. Dependencies are added only when code uses them, with the same rules as the kernel (exact pins, at least 14 days old at adoption, licence and advisory review, separate development dependencies, `Cargo.lock` and `--locked`).

**Gate:** `kernel/tools/check_deps.py` with `broker/deps-allowlist.json`.

## Runtime dependencies (M3-02)

| Crate | Pin | Features | Licence | Released | Purpose |
|---|---|---|---|---|---|
| `writ-kernel` | path (workspace member) | none | MIT | n/a | use-time re-decision (`decision::decide`), contracts, D-006 canonical form and digest |
| `hmac` | `=0.12.1` | default off | MIT OR Apache-2.0 | 2022-02-17 | HMAC-SHA256 ingress seals (D-010) and permit MACs (SR-07); `verify_slice` compares tags in constant time |
| `sha2` | `=0.10.8` | default off | MIT OR Apache-2.0 | (as reviewed for the kernel) | SHA-256 for HMAC; same version as `writ-kernel` |

**Why not the standard library or custom code:** the standard library has no HMAC or SHA-256; a hand-written HMAC and constant-time comparison would be new security-critical code without the review history of the RustCrypto implementations. `hmac` and `sha2` were the M1 bake-off choices (M1-07) and are pinned to the M1-reviewed versions.

**Transitive additions over the kernel closure:** `subtle` 2.6.1 (released 2024-06-24, BSD-3-Clause; constant-time equality, pulled in by the `digest` `mac` feature that `hmac` enables). Every other transitive package (`digest`, `block-buffer`, `crypto-common`, `generic-array`, `typenum`, `version_check`, `cfg-if`, `cpufeatures`, `libc`) is the kernel's reviewed set at the same versions. Broker closure: 12 registry packages.

**Review record (2026-11-12):** versions and checksums equal those reviewed in M1-07 (`m1/rust/Cargo.lock`) and those published on crates.io (checksums compared 2026-11-12); both new packages are far older than 14 days. `python3 kernel/tools/check_deps.py --osv` on 2026-11-12 reported no advisory for any locked package. Development dependencies: none (test keys and fixtures live only in `#[cfg(test)]` code).

**Licence:** `subtle` is BSD-3-Clause (recorded as a fact in M1-07, never added to an approved list). It is approved in `broker/deps-allowlist.json` for the broker only, **pending owner confirmation**; no other crate uses it directly.

**Open gate items (owner decision, M3-02):**

1. The gate computes closures from the workspace-unified `cargo metadata` resolve. With `hmac` in the workspace, `digest` gains its `mac` feature and lists `subtle` as a dependency, so the gate counts `writ-kernel` at 11 packages, although `cargo tree -p writ-kernel -e normal,build` (per-package feature resolution) still shows exactly the 10 reviewed packages and the kernel source uses no MAC.
2. `writ-ledger-sqlite` depends on `writ-broker` (for `broker::store`), so the gate attributes the broker's whole closure, including `hmac`, to it, and its allow-list forbids `hmac` and lists none of the SHA-2 packages.
