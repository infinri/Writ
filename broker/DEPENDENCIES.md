# writ-broker dependency policy (M3-00)

Reviewed separately from the kernel (`kernel/DEPENDENCIES.md` stays in force for `writ-kernel`, whose closure the gate keeps at exactly its reviewed set). The broker holds keys and talks to storage only through `broker::store`; it must not depend on a storage driver (`rusqlite` and `libsqlite3-sys` are forbidden here) or on async, network or model-SDK crates. Dependencies are added only when code uses them, with the same rules as the kernel (exact pins, at least 14 days old at adoption, licence and advisory review, separate development dependencies, `Cargo.lock` and `--locked`).

**Current runtime dependencies:** none (M3-01 defines only the store interface). `hmac` and `sha2` for seals and permits will be added with M3-02 and reviewed here then. **Gate:** `kernel/tools/check_deps.py` with `broker/deps-allowlist.json`.
