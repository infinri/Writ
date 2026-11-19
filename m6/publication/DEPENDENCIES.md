# D-051 dependency review

The development lab pins `ed25519-dalek = 2.2.0` with default features disabled and `zeroize` enabled. The verified path uses pure Ed25519 and strict verification with separate signer and public trust interfaces. The supported graph enables no batch, prehash, randomness, serialization or hazard APIs. `curve25519-dalek` resolves with `digest` and `zeroize`. The lab reuses `rusqlite = 0.41.1` without default features and D-023's pinned static SQLite 3.54.1. A disposable example and its tests use the existing ledger and controlled effect store. The root and offline compiler manifests, locks and dependency exceptions are untouched.

This is a separate Cargo workspace. `Cargo.lock` records every registry checksum. The local checker pins both the complete lock and all-target package, edge, licence and feature resolution, then submits every locked registry package to OSV. The root workspace dependency gate still checks the original closures and reports the existing D-048/D-049 and RSA dispositions.

| New to this lock relative to the root workspace | Released | Licence | Activated path |
| --- | --- | --- | --- |
| `ed25519-dalek 2.2.0` | 2025-07-09 | BSD-3-Clause | Strict Ed25519 verification and the example/test signer |
| `curve25519-dalek 4.1.3` | 2024-06-18 | BSD-3-Clause | Edwards group arithmetic |
| `curve25519-dalek-derive 0.1.1` | 2023-10-31 | MIT/Apache-2.0 | Target-specific compile-time backend selection |
| `ed25519 2.2.3` | 2023-10-15 | Apache-2.0 OR MIT | Ed25519 signature type |
| `fiat-crypto 0.2.9` | 2024-05-12 | MIT OR Apache-2.0 OR BSD-1-Clause | Target/config-gated field arithmetic, MIT option |
| `rustc_version 0.4.1` | 2024-08-28 | MIT OR Apache-2.0 | Build script version detection |
| `semver 1.0.28` | 2026-04-04 | MIT OR Apache-2.0 | Build script version parsing |
| `signature 2.2.0` | 2023-11-12 | Apache-2.0 OR MIT | Signature API types |

Confirmed these eight crate versions, publication dates and checksums against their crates.io records. All were unyanked and more than 14 days old. Shared versions retain the root lock's checksums. Reviewed BSD-3-Clause for these two versions within the D-051 development lab, with their exact [Ed25519](licenses/ed25519-dalek-2.2.0.txt) and [Curve25519](licenses/curve25519-dalek-4.1.3.txt) licence and copyright notices. This review does not change any root allow-list or grant production adoption. Existing shared dependency notices remain in the [root inventory](../../THIRD-PARTY-NOTICES.md).

The OSV query returned no advisories for the lab lock. The selected versions include the RustSec fixes for [double-public-key signing](https://rustsec.org/advisories/RUSTSEC-2022-0093.html) and [scalar subtraction timing variability](https://rustsec.org/advisories/RUSTSEC-2024-0344.html). The lab uses no advisory exceptions. In particular, neither the MCP SDK nor Chrono is present in this lock.

```sh
cargo tree --manifest-path m6/publication/Cargo.toml --locked --target all -e normal,build,dev,features
python3 m6/publication/check_dependencies.py
python3 kernel/tools/check_deps.py --osv --sbom /tmp/writ-root-sbom.json
```
