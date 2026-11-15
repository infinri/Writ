# M5 dependency review (M5a-00; D-045)

Status: **research complete 2026-11-15; nothing adopted yet.** Each adoption happens in the task that needs it, with the gate (`kernel/tools/check_deps.py`) at 0 problems and the allow-lists updated exactly. Raw outputs: [`m5-deps/`](m5-deps/) (selected; complete set kept outside the repository by the researcher).

## 1. RSA recheck (D-041 DR-3, D-045)

- `rsa` 0.10.0 published 2026-11-08 (patched for RUSTSEC-2023-0071); `crypto-glue` 0.2.2 (2026-10-08) is still the newest and requires `rsa ^0.10.0-rc.18` with feature `sha2`, which 0.10.0 removed; upstream fix `kanidm/crypto-glue` PR #49 open and unmerged; webauthn crates still 0.6.2.
- `cargo update -p rsa --precise 0.10.0`: fails (exit 101, missing `sha2` feature); `cargo update -p crypto-glue`, `-p webauthn-rs`: no change ([task1-cargo-update.txt](m5-deps/task1-cargo-update.txt)).
- OSV: `rsa` 0.10.0-rc.19 RUSTSEC-2023-0071 (fixed 0.10.0); `rsa` 0.10.0, `crypto-glue` 0.2.2, `webauthn-rs`/`-core` 0.6.2 none ([osv-task1-summary.txt](m5-deps/osv-task1-summary.txt)).
- **Outcome:** DEVELOPMENT-ONLY exception continues; release eligibility blocked (CO-DEP-01). Recheck when a `crypto-glue` release accepts `rsa` 0.10.x (adoption of 0.10.0 within 14 days needs the security-fix age exception).

## 2. Candidates

| Candidate | Use | Closure effect | Licence / advisories | Status |
|---|---|---|---|---|
| `rustix` =1.1.6 features `std, process, fs, net, thread, event, pipe, time` (default off) | runner supervisor and fs adapter crates (`net` only where the socket server lives) | no new packages (rustix, linux-raw-sys, bitflags already approved); features widen ([task2a-lock-diff.txt](m5-deps/task2a-lock-diff.txt)) | already approved licence; none | within D-045 M5-D4 (rustix process/fs features through review); adopt in M5a containment and fs adapter tasks |
| `tokio` =1.54.1 feature `signal` in `writ-approval-server` | graceful shutdown (M5-D16) | new package `signal-hook-registry` 1.4.8 (MIT OR Apache-2.0, 2025-12-25, no OSV); `errno` built on Linux ([task2b-lock-diff.txt](m5-deps/task2b-lock-diff.txt)) | none | within D-045 M5-D16; needs `signal` removed from `approval/deps-allowlist.json` `forbidden_features.tokio` (workspace feature unification) or per-member feature scoping |
| `rmcp` =3.9.0 features `server, transport-io` (no `macros`) | M5b Claude Code MCP tool path (M5-D9), separate adapter crate | 4 new packages (`rmcp`, `pastey`, `schemars_derive`, `serde_derive_internals`), several locked-unbuilt packages become built (chrono, schemars, indexmap 2, futures family) ([task2c-rmcp390-ws-review-linux.txt](m5-deps/task2c-rmcp390-ws-review-linux.txt)) | **`rmcp` licence Apache-2.0 (new, needs package-specific owner approval)**; 3.8.0 has RUSTSEC-2026-0462 (fixed 3.9.0); 3.9.0 published 2026-11-05 (14 days on 2026-11-19); `build.rs` runs `git config` only inside its own repository; one `unsafe` `fcntl` | M5b; protocol version pinned to 2025-11-25 via `supported_protocol_versions` ([task2c-rmcp390-handshake.txt](m5-deps/task2c-rmcp390-handshake.txt)); adoption awaits the licence approval and age |

Constraint found: `std` pidfd process creation is unstable on Rust 1.99.1 and `pre_exec` is unsafe ([task2a-std-pidfd.txt](m5-deps/task2a-std-pidfd.txt)), so with `unsafe_code` forbidden, child-side session, prctl and cgroup placement come from bubblewrap or systemd (`systemd-run` transient units), and descendant cleanup relies on `cgroup.kill` in a delegated subtree, not on process-group signals.

## 3. Host facts (this workstation, read-only; not the reference image)

Ubuntu 24.04.3 LTS, kernel 6.17.0-40-generic, systemd 255, bubblewrap 0.9.0 (not setuid), cgroup v2 with `nsdelegate` and `cpu memory pids` delegated to the user manager, openat2 `BENEATH|NO_SYMLINKS|NO_XDEV`, `statx` mount id, `pidfd_open`, `waitid(P_PIDFD)` and `SO_PEERCRED` working, Landlock ABI 7. **`kernel.apparmor_restrict_unprivileged_userns = 1` with no bubblewrap AppArmor profile: unprivileged user, PID and network namespaces fail** (`bwrap --unshare-user`: "setting up uid map: Permission denied") ([task3-host-facts.txt](m5-deps/task3-host-facts.txt), [task3-ns-landlock.txt](m5-deps/task3-ns-landlock.txt)). The readiness probe must detect this and refuse governed jobs; the reference image must provide namespace access for bubblewrap in a reviewed way (see section 4).

## 4. Items for the owner when dependent work reaches them (D-045: reported only then)

1. `rmcp` 3.9.0 Apache-2.0 licence (package-specific approval), when M5b MCP integration starts.
2. Reference-image namespace policy on Ubuntu 24.04: an AppArmor profile granting `userns` to `/usr/bin/bwrap` only (recommended; Ubuntu's documented mechanism) versus disabling the restriction system-wide (weaker); a setuid bubblewrap is not proposed. Needed when the containment task first runs in the reference VM.
