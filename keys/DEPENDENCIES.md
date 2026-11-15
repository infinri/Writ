# writ-keys dependency policy (M5a-05)

`writ-keys` is the local service-owned key source (D-045 M5-D6, [M5-INTERFACES](../docs/implementation/M5-INTERFACES.md) J-3). It reads key files and nothing else: no network, no environment reads, no command-line key input, no storage driver, no async runtime. Same rules as the other members: exact pins, at least 14 days old at adoption, licence and advisory review, `Cargo.lock` and `--locked`.

**Gate:** `kernel/tools/check_deps.py` with `keys/deps-allowlist.json`.

| Crate | Pin | Features | Licence | Purpose |
|---|---|---|---|---|
| `writ-broker` | path (workspace member) | none | MIT | `KeySource`, `KeyRegistry`, `Material` and the key types (J-3 contract) |
| `rustix` | `=1.1.6` | default off; `std, fs` | Apache-2.0 WITH LLVM-exception OR Apache-2.0 OR MIT (package-specific approval, 2026-11-12) | `openat2` with `RESOLVE_*`, `fstat`, `flistxattr`, directory listing from a descriptor; `unsafe_code` stays forbidden here |

**No new registry packages.** The closure (`rustix`, `linux-raw-sys`, `bitflags`, and the target-specific `libc`, `errno`, `windows-sys`, `windows-link`) is the set already approved for `writ-broker`, at the same versions and checksums. `std, fs` are inside the reviewed maximum `rustix` feature set of [M5-DEPENDENCY-REVIEW](../docs/implementation/M5-DEPENDENCY-REVIEW.md) section 2. `process` is not used: the service uid comes from configuration, not from `getuid`.

**Deviation from J-0, recorded:** J-0 routes `rustix` for `writ-keys` through `writ-hostsys`. That crate does not exist yet, so `writ-keys` takes `rustix` directly with the two features it needs. When `writ-hostsys` lands, the directory walk and the file checks move behind it and this entry is removed.

**Feature unification:** in a workspace build Cargo compiles one `rustix` with the union of features, so the broker links a `rustix` built with `std, fs`. The broker's own source still uses `time` only (J-0 lead decision 2: a source-scan check, added in M5a-02).

**Not adopted:** `zeroize`. Key buffers are overwritten on drop on a best-effort basis (`writ_broker::keys::overwrite`), recorded as a limitation (J-3 lead decision 3).
