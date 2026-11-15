# writ-hostsys dependency policy (M5a-03, D-045)

`writ-hostsys` holds the host readiness probes and the `writ-probe` operator binary (J-0 "OS primitives" and "Probe" rows; one crate for now, see the M5a-03 note in [TEST-EVIDENCE](../docs/implementation/TEST-EVIDENCE.md)). It opens no trusted state and contains no business logic, SQL, network listener, MCP or model code. Same rules as the other members: exact pins, at least 14 days old at adoption, licence and advisory review, `Cargo.lock` and `--locked`.

**Gate:** `kernel/tools/check_deps.py` with `hostsys/deps-allowlist.json` (member `writ-hostsys`; `forbidden_anywhere` covers storage drivers, async and HTTP stacks, TLS libraries, MAC crates, WebAuthn and MCP).

## Runtime dependencies

| Crate | Pin | Features | Purpose |
|---|---|---|---|
| `writ-kernel` | path | none | strict JSON parser, D-006 canonical form, SHA-256 digest of the bubblewrap binary |
| `rustix` | `=1.1.6` | default off; `std`, `fs`, `process` | safe `openat2` with resolve flags, `statx` mount id, `statfs`, `renameat_with(RENAME_NOREPLACE)`, `lgetxattr` (ACL detection); `getuid`, `getgid`, `kill_process_group`, `test_kill_process` |

No new registry package: the closure (`rustix`, `linux-raw-sys`, `bitflags`, `errno`, `libc`, `windows-sys`, `windows-link`) is already locked and approved for `writ-broker`, with the same versions and checksums ([M5-DEPENDENCY-REVIEW](../docs/implementation/M5-DEPENDENCY-REVIEW.md) section 2). The `thread` and `net` features listed as candidates in that review are not used. Consequence: `SO_PEERCRED` is not observable here (std's `peer_cred` is unstable on Rust 1.99.1), so probe P-PEERCRED reports `error` until it is implemented where peer credentials are reviewed (Tokio in `writ-rpc`, M5a-04, or `rustix` `net` after review).

**Feature unification:** in combined builds of all members, `rustix` is compiled with `std, fs, process, time` (the broker's code still uses only `time`), `linux-raw-sys` gains `prctl`, `bitflags` and `errno` gain `std`. No package is added; `check_deps.py` reports 0 problems.

External binaries used by the probes (not Cargo dependencies, recorded in every readiness report): bubblewrap, `/bin/sh`, `bash`, coreutils, util-linux `setsid`, `dd`, `timeout`, `dpkg-query`, `systemctl`, `getconf`.
