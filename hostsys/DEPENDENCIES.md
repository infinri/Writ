# writ-hostsys dependency policy (M5a-03, D-045)

`writ-hostsys` holds the host readiness probes and the `writ-probe` operator binary (J-0 "OS primitives" and "Probe" rows; one crate for now; J-0 also lists `writ-containment` as a dependency of `writ-probe`, which is future work (M5a-07)). It opens no trusted state and contains no business logic, SQL, network listener, MCP or model code. Same rules as the other members: exact pins, at least 14 days old at adoption, licence and advisory review, `Cargo.lock` and `--locked`.

**Gate:** `kernel/tools/check_deps.py` with `hostsys/deps-allowlist.json` (member `writ-hostsys`; `forbidden_anywhere` covers storage drivers, async and HTTP stacks, TLS libraries, MAC crates, WebAuthn and MCP).

## Runtime dependencies

| Crate | Pin | Features | Purpose |
|---|---|---|---|
| `writ-kernel` | path | none | strict JSON parser, D-006 canonical form, SHA-256 digest of the bubblewrap binary |
| `rustix` | `=1.1.6` | default off; `std`, `fs`, `process`, `net` | safe `openat2` with resolve flags, `statx` mount id and birth time, `statfs`, `renameat_with(RENAME_NOREPLACE, RENAME_EXCHANGE)`, `lgetxattr` and `setxattr` (ACL detection and the planted-ACL control); `getuid`, `getgid`, `kill_process_group`, `test_kill_process`; `socket_peercred` (`SO_PEERCRED`, P-PEERCRED; lead decision after M5-INTERFACES revision 2) |

No new registry package: the closure (`rustix`, `linux-raw-sys`, `bitflags`, `errno`, `libc`, `windows-sys`, `windows-link`) is already locked and approved for `writ-broker`, with the same versions and checksums ([M5-DEPENDENCY-REVIEW](../docs/implementation/M5-DEPENDENCY-REVIEW.md) section 2). J-0 allows `std, fs, process, thread, event, pipe, net` for this crate; `thread`, `event` and `pipe` are not used.

**Feature unification:** in combined builds of all members, `rustix` is compiled with `std, fs, process, net, time` plus what `writ-keys` requests (the broker's source still uses only `time`; J-0 source-scan rule), `linux-raw-sys` gains `prctl`, `net`, `netlink`, `if_ether`, `xdp`, and `bitflags` and `errno` gain `std`. No package is added; `check_deps.py` reports 0 problems.

External binaries used by the probes (not Cargo dependencies, recorded in every readiness report): the private bubblewrap copy and the system bubblewrap, `unshare`, `sudo`, `journalctl`, `/bin/sh`, `bash`, coreutils, util-linux `setsid`, `dd`, `timeout`, `dpkg-query`, `systemctl`, `getconf`.
