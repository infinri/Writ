# writ-kernel dependency policy (M2-01)

**Owner decision 2026-11-12:** the standard library plus a reviewed, minimal allow-list. The kernel is **key-free**: no MAC, signature, key-management or crypto-provider crates (`hmac` is explicitly excluded; authentication and signature verification belong at their external trust boundaries). No I/O, async, database, network, serialization-framework or model-SDK crates in the kernel.

## Rules

1. Runtime and development dependencies are separate (`[dependencies]` versus `[dev-dependencies]`); development dependencies never ship and are reviewed the same way.
2. Every new dependency needs a written justification here (why the standard library is insufficient, why a maintained library beats a custom replacement), its pinned exact version (`=x.y.z`), enabled features, licence, transitive dependencies, advisory status and release age (at least 14 days old when adopted, as in M1).
3. `Cargo.lock` is committed; every build and test runs with `--locked`.
4. The gate `kernel/tools/check_deps.py` checks each workspace member's own dependency closure against its allow-list (since M3-00: kernel, broker, SQLite ledger), so this kernel list stays exact; it fails on any package in the kernel closure not on `kernel/deps-allowlist.json` (exact version; checksum for direct dependencies), a forbidden crate, or an unapproved licence; CI also queries OSV for advisories and publishes an SBOM artifact.
5. Upgrades are deliberate: change the pin, update the allow-list and this file, and record the review in TEST-EVIDENCE.

## Approved runtime dependency

| Crate | Version | Features | Licence | Purpose |
|---|---|---|---|---|
| `sha2` | `=0.10.8` | none (`default-features = false`) | MIT OR Apache-2.0 | SHA-256 for D-006 canonical digests (bundle and request fingerprints); implementing SHA-256 by hand would be an unnecessary custom replacement of a maintained, audited library |

**Transitive (locked):** `digest` 0.10.7, `block-buffer` 0.10.4, `crypto-common` 0.1.7, `generic-array` 0.14.7, `typenum` 1.20.1 (all MIT OR Apache-2.0 except `generic-array`, MIT), `cfg-if` 1.0.5, `cpufeatures` 0.2.17 (MIT OR Apache-2.0), `libc` 0.2.191 (MIT OR Apache-2.0; only on aarch64 Linux and Android through `cpufeatures`), build-time only `version_check` 0.9.5 (MIT/Apache-2.0).

**Review record (2026-11-12):** the same versions and checksums as the set reviewed in M1-07 (release age at least 14 days, OSV clean 2026-11-07); OSV re-queried 2026-11-12 through the gate: no advisories for any locked package. Development dependencies: none.
