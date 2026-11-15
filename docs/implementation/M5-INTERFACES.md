# M5 shared interfaces (lead-owned; D-045)

**Status (revision 2, 2026-11-15, after two independent reviews):**

| Section | Status |
|---|---|
| J-0 Layering, crates, dependencies | **BINDING (revised)** |
| J-1 Process and privilege model | **BLOCKED** on owner decisions C-1 and H-2 (J-10); the layout, single-owner composition and hardening rules below are the lead's design and do not change with either answer, but no consumer is integrated against J-1 until both are decided |
| J-2 Local RPC | **BINDING (revised)** |
| J-3 Key source | **BINDING (revised)** |
| J-4 Snapshot store | **BINDING (revised)** (owner-epoch fencing added) |
| J-5 Containment | **BINDING (revised)**, except the job identity (uid of job processes), which is **BLOCKED** on H-2 |
| J-6 Host identity | **BINDING (revised)** (lead decision on review H-4, per D-045 M5-D7) |
| J-7 `fs.write` recovery contract | **BINDING (revised)**: review fixes F1..F12 and L1..L4 applied; integration of the adapter still follows M5a-08 (recorded review) |
| J-8 Graceful shutdown | **BINDING (revised)** |
| J-9 Reference environment and probes | **BINDING (revised)** (lead decision M-5: Ubuntu 24.04 LTS) |
| J-10 Open questions | owner decisions C-1, H-2, Q-3; Q-1 and Q-2 answered |

Plan and tasks: [M5-START-PLAN](M5-START-PLAN.md); dependency facts: [M5-DEPENDENCY-REVIEW](M5-DEPENDENCY-REVIEW.md). Format follows [M4-INTERFACES](M4-INTERFACES.md). Names of crates, types, files, tables and refusal codes are binding unless the lead renames them; every new refusal code has an internal code and a generic external mapping (INV-44). Review finding ids (F*, L*, M-*, H-*) refer to the two independent reviews of revision 1 (`60f2012`).

## J-0 Layering, crate plan and dependencies

Rule: **kernel and broker stay free of OS, sandbox, MCP, network service and model SDK code.** The broker keeps storage-independent logic and traits; the M3 lifecycle gate and the test-only `LocalProcessSandbox` ([process.rs](../../broker/src/process.rs)) stay as they are, and the broker's source uses `rustix` for `time` only.

| Layer | Crate | May depend on | Must not contain | Enters the closure of |
|---|---|---|---|---|
| Kernel | `writ-kernel` | unchanged | anything M5 | everything |
| Broker logic | `writ-broker` | kernel, existing deps | OS calls beyond today's, sockets, Tokio, bubblewrap or cgroup handling, MCP, model code | everything trusted |
| Broker additions (types and traits only) | `writ-broker`: `keys` (key-source trait, `KeyPurpose`, validity), `effects` (non-transactional adapter contract text, routing by action kind, evidence reason), `refusal` (new codes), `host` (sealed identity fields) | as above | implementations | everything trusted |
| OS primitives | `writ-hostsys` | `rustix` (features below), kernel | business logic, SQL, network listeners | keys, persistence, fs adapter, containment, service, probe |
| Persistence | `writ-ledger-sqlite`: `snapshots` (`writ-snapshots-1`), `fs_journal` (`writ-fseffects-1`), ledger additions for the key registry and revocation (lead-owned schema change) | broker, rusqlite, `writ-hostsys` (deployment proof type only) | HTTP, sockets, sandbox | service |
| Key source | `writ-keys` | broker, `writ-hostsys` | network, environment reads, command-line key input | service, probe |
| `fs.write` adapter | `writ-fs-effects` | broker, persistence (journal), `writ-hostsys` | network | service |
| Containment | `writ-containment` (lib implements `broker::runner::Sandbox`; bin `writ-cgroup-exec`) | broker, `writ-hostsys` | network, SQL | service; the helper is in the trusted computing base |
| Local RPC | `writ-rpc` | kernel, Tokio (`rt`, `net`) | MCP, model code | service, hook client, CLI client |
| Service | `writ-service` (bin `writ-serviced`) | all trusted crates above, `writ-approval`, Tokio `signal` | MCP, model SDK | itself |
| Approval channel | `writ-approval`, `writ-approval-server` | unchanged; `writ-approval-server` adds Tokio `signal` (J-8) | `writ-approval` source never uses `tokio::signal` | unchanged |
| Probe | `writ-probe` (bin) | `writ-hostsys`, `writ-containment` | anything that opens trusted state for writing | operator procedure |
| Host contract (M5b) | `writ-host-contract` | kernel | OS, MCP, model | service, adapters |
| Host adapters (M5b) | `writ-host-claude` (lib: trusted normalizer; bin `writ-hook`: untrusted client), `writ-host-cli` | host contract, `writ-rpc` | keys (clients), MCP in the lib | lib: service; bins: agent side |
| MCP shim (M5b) | `writ-mcp-shim` (bin, agent side) | `writ-rpc`, `rmcp` if approved (M5b-02) | keys, ledger access | agent side only, never the service closure |
| Model side (M5b) | `writ-model-contract`, `writ-model-stub-a`, `writ-model-stub-b`, `writ-agent-runner-stub` | kernel, model contract | any provider SDK, network | none of kernel, broker or service |

**Dependencies (reconciled with [M5-DEPENDENCY-REVIEW](M5-DEPENDENCY-REVIEW.md) section 2; review L-6):**

- `rustix =1.1.6`, default features off. The reviewed maximum set is `std, process, fs, net, thread, event, pipe, time`. Per crate: `writ-hostsys` `std, fs, process, thread, event, pipe, net` (the only crate that calls `SO_PEERCRED` directly if Tokio's accessor is not used); `writ-fs-effects` and `writ-keys` reach `rustix` only through `writ-hostsys`; `writ-containment` `std, process, fs, pipe, event`; broker `time` (unchanged).
- **Feature unification note.** With resolver 2, Cargo builds one copy of `rustix` (and `tokio`) per target with the **union** of the features requested by every member built together, so in a workspace build the broker links a `rustix` compiled with the M5 features and `writ-approval` links a Tokio with `signal`. Closure claims are about packages, not features; the restriction "broker uses `time` only" and "`writ-approval` never uses `tokio::signal`" are **source-level** rules, checked by a source scan in the dependency gate. `signal` is removed from `approval/deps-allowlist.json` `forbidden_features.tokio` with that scan as its replacement (or per-member feature scoping if the gate supports it); the lead records which.
- Tokio `=1.54.1` feature `signal` (new package `signal-hook-registry` 1.4.8) in `writ-approval-server` and `writ-service` only.
- `rmcp =3.9.0` (`server`, `transport-io`) for `writ-mcp-shim` only, pending the package-specific Apache-2.0 licence approval and the age rule (M5b-02).
- **`writ-cgroup-exec`** is installed root-owned (`root:root`, mode `0755`, in a root-owned directory), never writable by `writ`; its SHA-256 is pinned in the service configuration and in the readiness report and verified by `writ-serviced` before first use and at every readiness recheck; a mismatch makes containment unavailable.
- External binaries recorded with the reference image, not Cargo dependencies: the private bubblewrap copy (J-9), systemd.
- `zeroize` (J-3) is a new dependency candidate, reviewed in M5a-02 before key-source implementation; without it, key buffers are overwritten on drop on a best-effort basis and the limitation is recorded.

## J-1 Process and privilege model (BLOCKED on C-1 and H-2)

**Identities.** Service user `writ`, group `writ` (system account, no login shell). Group `writ-agents` contains exactly the configured agent uids (and nothing else). The agent runs as an **agent uid**; whether that uid may be the human's desktop user is owner decision **C-1** (J-10). Runner job processes run as `writ` inside bubblewrap or as a separate job uid: owner decision **H-2** (J-10). The operator acts as root on the disposable reference host only, through install, provisioning and teardown scripts (M5-D3).

**Layout (no ACLs, no default ACLs anywhere below these roots; checked at start and by probes):**

| Path | Owner, mode | Contents | Agent access |
|---|---|---|---|
| `/var/lib/writ` | `writ:writ` 0700 | trusted state root | none |
| `/var/lib/writ/ledger/` | `writ:writ` 0700 | ledger, `-wal`, `-shm`, `.owner` | none |
| `/var/lib/writ/stores/` | `writ:writ` 0700 | effect store, `writ-snapshots-1`, `writ-fseffects-1` journals, `.owner` files | none |
| `/var/lib/writ/keys/` | `writ:writ` 0700 | key files 0600 or 0400 (J-3) | none |
| `/var/lib/writ/run/` | `writ:writ` 0700 | runner work directories | none |
| `/srv/writ/targets/<root id>/` | `writ:writ` 0755 or 0750 per configuration (never group- or other-writable) | `fs.write` target roots (J-7; review M-1) | read only if configured |
| `/etc/writ/` | `root:writ` 0750 | configuration (no secrets) | none |
| `/run/writ/` | `writ:writ-agents` 0750 | sockets (created by systemd socket units, J-8) | traverse |
| `/run/writ/host.sock` | `writ:writ-agents` 0660 | host ingress, snapshot ingest, status | connect, then admitted by uid (J-2) |
| `/run/writ/admin.sock` | `writ:writ` 0600 | operator operations | none |
| `/usr/local/libexec/writ/` | `root:root` 0755 | `writ-cgroup-exec` (0755) | execute only; useless without delegated cgroup write access |
| `/usr/local/libexec/writ/bwrap` | `root:writ` 0750 | private bubblewrap copy with the AppArmor `userns` grant (J-9) | none |

**One ledger owner.** `writ-serviced` (unit `writ.service`) owns the ledger through the `OwnerLock` path and owns every store bound to it. It composes in one process the broker, the stores, the approval listener (loopback TCP; WebAuthn authenticates the human), the sealing part of host adapters, snapshot ingest and the runner supervisor. `writ-approval-server` stays a standalone development entry point; it and `writ-serviced` cannot own one ledger at once. A later split reaches the owner over authenticated RPC, never opens the ledger (M5-D3).

**`writ.service` hardening (review M-7):** `User=writ`, `Group=writ`, `UMask=0077`, `NoNewPrivileges=yes`, `ProtectSystem=strict`, `ReadWritePaths=/var/lib/writ /srv/writ`, `ProtectHome=yes` (or `tmpfs`), `PrivateTmp=yes`, `PrivateDevices=yes`, `ProtectKernelTunables=yes`, `ProtectKernelModules=yes`, `ProtectKernelLogs=yes`, `ProtectClock=yes`, `ProtectHostname=yes`, `LockPersonality=yes`, `RestrictRealtime=yes`, `RestrictSUIDSGID=yes`, `MemoryDenyWriteExecute=yes`, `SystemCallArchitectures=native`, `RestrictAddressFamilies=AF_UNIX AF_INET AF_NETLINK` (netlink for bubblewrap's loopback setup in the job namespace), `CapabilityBoundingSet=` empty (plus `CAP_NET_BIND_SERVICE` only if J-8 option (b) is chosen), `LimitCORE=0`, `Delegate=yes`, `KillMode=mixed`, `TimeoutStopSec` above the J-8 hard deadline, `Restart=on-failure`. Not set, with reasons recorded: `ProtectControlGroups` (would make the delegated subtree read-only), `RestrictNamespaces` (bubblewrap needs user, mount, pid, net, ipc, uts and cgroup namespaces), `PrivateUsers` (breaks the uid model). **Startup uid check:** `writ-serviced` refuses to start unless real and effective uid and gid equal the configured `writ` ids, neither is 0, and the supplementary groups are exactly the configured ones.

**What the agent identity can reach:** `host.sock` (every request untrusted), the loopback approval page (every protected read or mutation needs a user-verified passkey), read-only target roots if configured; nothing under `/var/lib/writ`, `/etc/writ`, `admin.sock` or the private bubblewrap copy. It cannot signal or trace service processes; it can read `/proc/<pid>/cmdline`, so no secret is ever in a command line or environment. The full reach list is verified by P-AGENT-DENY (J-9), including the items of review M-4/H-3 (journal, core dumps, `/dev/shm`, tmp, abstract sockets, cgroup files, D-Bus and polkit, sudo and groups).

## J-2 Local RPC over Unix sockets (review M-6, L-1, L-4, L-5)

- **Sockets:** `SOCK_STREAM` sockets created by systemd socket units (`SocketUser=writ`, `SocketGroup=writ-agents`, `SocketMode=0660` for `host.sock`; `SocketGroup=writ`, `SocketMode=0600` for `admin.sock`) and passed to `writ-serviced` (J-8). The service runs with umask 0077 and never creates agent-reachable files itself.
- **Admission before any read:** on accept the service reads the peer credentials (`SO_PEERCRED`, Tokio `UnixStream::peer_cred` or `writ-hostsys`) and decides admission **before reading a single byte**. Refused: an unlisted uid; uid or gid 65534 (the overflow id seen for peers in another user namespace with unmapped ids); uid 0 on `host.sock`. The pid is recorded for audit only, never used for authorization.
- **Connection caps:** a global cap on open connections, a per-uid cap, per-connection idle and per-request deadlines; at the cap, new connections are accepted and closed at once (no read).
- **Frames:** 4-byte big-endian length, 1-byte frame type (`0x01` control, `0x02` ingest data), payload. The length is checked against the maximum for that frame type on that endpoint **before any allocation** (control 64 KiB, data 1 MiB); payloads are read into bounded buffers. A control payload is one UTF-8 strict JSON document parsed by the kernel parser (INV-41). Any violation closes the connection with a generic error (INV-18). One request in flight per connection. No ancillary data (`SCM_RIGHTS`, `SCM_CREDENTIALS`) is accepted; a message carrying it closes the connection.
- **Scope binding:** configuration maps each (socket, uid) to exactly **one** (tenant, project) binding; no request field selects or changes scope. A uid needing two projects gets two configured sockets (or is refused).
- **Authority follows the descriptor:** admission binds the connection to the uid that connected; whoever holds that connected descriptor acts with that uid's authority for the connection's lifetime. This is documented for clients: never pass a connected descriptor to a less trusted process.
- **Idempotency scoped by OS principal:** the broker idempotency key used for a request is derived from (`os_principal`, client key) inside the service, and `status.receipt` answers only for receipts of the same `os_principal`, tenant and project; two uids can neither collide nor probe each other's keys.
- **Clients verify the server:** the hook client, CLI client and MCP shim read `SO_PEERCRED` of their connection and refuse to send anything unless the peer uid is the configured `writ` uid.
- **What admission proves:** the OS principal of the connecting process only (M5-D7), recorded as `os_principal: "uid:<n>"`, `verified_by: "so_peercred"`.
- **Endpoints:** `host.sock`: `host.event`, `effect.request` (M5b), `snapshot.ingest.*` (J-4), `status.receipt`. `admin.sock`: `readiness`, `registry.*`, `keys.reload`, `keys.revoke` (J-10 Q-3), never dispositions that unblock or abandon.
- **Not MCP.** A Writ-internal protocol; MCP exists only in the agent-side shim.

## J-3 Key source (review M-3, L-2)

```text
broker::keys
  enum KeyPurpose { Ingress, Permit, AcceptanceRecord, RunnerAttestation }
  struct KeyId(String)                         // "<purpose>/<label>", [a-z0-9._-], at most 64 bytes
  struct KeyValidity { not_before, not_after }   // trusted time, whole seconds
  trait KeySource {
      fn key(&self, purpose: KeyPurpose, id: &KeyId, now: Timestamp) -> Result<KeyStatus<'_, Material>, KeysUnavailable>;
      fn issuing_key(&self, purpose: KeyPurpose, now: Timestamp, lifetime: Duration) -> Result<(KeyId, &Material), KeysUnavailable>;
  }
```

The purpose-typed rings (`KeyRing`, `AcceptanceKeyRing`, `RunnerKeyRing`) are implemented on top of a `KeySource`; `KeyStatus` gains `Expired` and `NotYetValid` (refuse like `Revoked`). Implementation `writ-keys::FileKeySource`:

- **File format** `writ-key/1`: strict JSON `{"contract":"writ-key/1","purpose","key_id","not_before","not_after","material"}`, `material` hex of at least 32 bytes. One key per file; **the file name must equal the encoded `key_id`** (`/` replaced by `_`, suffix `.json`), otherwise refused.
- **Loading:** open `/var/lib/writ/keys` with `openat2` (`RESOLVE_NO_SYMLINKS | RESOLVE_NO_MAGICLINKS`): directory owned by `writ`, mode 0700, no access or default ACL, every ancestor owned by root or `writ` and not group- or other-writable. Each file: `openat2` relative to that descriptor with `RESOLVE_BENEATH | RESOLVE_NO_SYMLINKS | RESOLVE_NO_MAGICLINKS | RESOLVE_NO_XDEV` and `O_RDONLY | O_NOFOLLOW | O_NONBLOCK | O_CLOEXEC`; `fstat`: regular file, owner `writ`, mode exactly 0600 or 0400, no ACL, `nlink == 1`, size at most 4 KiB; the read is bounded (at most the maximum plus one byte; more is refused). Failures refuse that key with an operator diagnostic (never the material).
- **Key registry and ids (ledger):** `key_registry` records every key id ever loaded with its purpose and the digest of its material (SHA-256 under a domain-separation prefix). An id presented again with different material, a material digest already registered under another id or purpose, and an id or material digest that was ever revoked are refused at load. **Ids are never reused.**
- **Revocation:** `key_revocations` (key id, material digest, purpose, revoked_at, actor, reason) in the ledger; **revoked material is refused under any id**. The revocation lookup runs **inside the ledger transaction that consumes the authenticated object**: for seals, inside T-reserve and again inside T-dispatch; for permits, inside the ledger transaction opened by the verification callback within the adapter transaction; for acceptance records and runner attestations, inside the promotion transaction. No cache. Revocation is recorded through `admin.sock` (`keys.revoke`, J-10 Q-3).
- **Issuing:** revoked, expired and not-yet-valid keys are never selected. `issuing_key` selects the valid key with the latest `not_before` whose `not_after` is at least `now + lifetime + margin` (lifetime of the object it authenticates; margin configured, default 60 s); if none qualifies, issuing refuses (fail closed) rather than producing an object that would outlive its key.
- **Key ids inside the MAC (versioned contracts):** permits gain `key_id` (new permit contract version) and seals gain `key_id` (new seal version, `{"adapter_id","key_id","mac"}`); in both, `key_id` is part of the MACed canonical bytes. Older versions are refused once the service runs with the file key source.
- **Rotation:** provision a new key with a later `not_before` and an overlap at least the longest lifetime of what the old key authenticated (permits 30 s; seals per request; records and attestations their freshness window); the old key becomes verify-only and then expires or is revoked.
- **Memory:** material is held in buffers zeroized on drop (`zeroize` after review, J-0); never in `Debug`, logs, readiness reports, evidence, command lines or environments; a canary scan in tests covers logs, reports and the repository.
- **Provisioning:** operator procedure on the reference host, as `writ`: material from the OS random source, temporary file in the key directory, `fsync`, rename, `fsync` of the directory; `keys.reload` loads it.
- **Failure:** unknown, revoked, expired, not-yet-valid, unreadable or integrity-failing keys refuse with the existing codes; an unreadable directory is `KeysUnavailable`.

## J-4 Snapshot store

`writ-snapshots-1`, a separate SQLite file owned by `writ-serviced`, using the ledger's identity and ownership pattern (canonical path, one hard link, `.owner` lock under the lifecycle gate, bound at creation to one `LedgerId`) and **owner-epoch fencing** like the ledger (the epoch is incremented at open and checked at the start of every write transaction; review M-2). WAL with `synchronous=FULL`.

- **Model.** `(tenant_id, project_id, snapshot_id)` with the `writ-snapshot-manifest/1` digest over `[{path, digest}]` sorted by path. `snapshot_id = "snap-" + hex(D-006 digest of {"contract":"writ-snapshot-id/1", tenant_id, project_id, manifest_digest})`. Tables `snapshots`, `snapshot_files`, `blobs` (by file digest). No `UPDATE` or `DELETE` in the API; triggers refuse updates.
- **Ingest (M5-D17).** `snapshot.ingest.begin` (scope from the connection binding), `snapshot.ingest.file {path, size}` plus data frames (digest computed as bytes arrive, sizes enforced), `snapshot.ingest.commit {claimed_manifest_digest optional}`: one transaction validates the manifest (valid paths per `valid_snapshot_path`, bounded length and depth, valid UTF-8, no control characters, no duplicates, no path that is a proper prefix directory of another, no two paths equal after Unicode normalization), computes the manifest digest, checks a claimed digest (mismatch refuses; never authority), inserts and commits. A crash before commit leaves nothing visible; temporary data is removed at open. No agent pathname is ever opened.
- **Bounds** per file, per snapshot, per session and per scope; exceeding one refuses `SNAPSHOT_LIMIT`.
- **Reads** re-verify every blob digest and the manifest digest on every consumption; mismatch fails closed.
- **Trust.** `immutable()` is true only for a store opened with a `VerifiedDeployment` (J-9); never a caller flag.
- **Retention.** No deletion in M5 (quotas bound growth); deletion with reference checks is deferred to CO-M4X-03 (M9).
- **Binding.** The broker's `Trust::snapshots` and the runner's `SnapshotContent` are the same instance.

## J-5 Containment

- **Readiness** comes only from the probes (J-9) run at start and rechecked periodically and before each job (delegated cgroup present and writable, `writ-cgroup-exec` and bubblewrap digests unchanged, runtime profile digest unchanged). No version comparison. A failing check sets the guarantee `false`; `RunnerRequirements::production()` refuses; no uncontained fallback.
- **Cgroup.** `writ.service` has `Delegate=yes`; the service moves itself into `<unit>/service` and creates `<unit>/jobs/` with `memory`, `pids`, `cpu` in `cgroup.subtree_control`. Per job `jobs/job-<permit id>` with `memory.max`, `memory.swap.max = 0`, `pids.max`, `cpu.max` written before any job process exists.
- **Entry before exec.** The supervisor spawns the root-owned, digest-pinned `writ-cgroup-exec` with the job cgroup path, the private bubblewrap path, the profile arguments and the program as separate arguments, a cleared environment and only descriptors 0..2. It verifies the cgroup path is beneath the delegated `jobs/` directory by descriptor, writes its own pid to `cgroup.procs`, verifies `/proc/self/cgroup`, then execs bubblewrap with `CommandExt::exec` (safe Rust; no `pre_exec`, consistent with M5-DEPENDENCY-REVIEW section 2). Every job process, including bubblewrap, starts inside the job cgroup before untrusted code runs.
- **Job identity: BLOCKED on H-2** (J-10): jobs as `writ` inside a user namespace, or as a separate unprivileged job uid through a narrowly scoped root-owned launcher.
- **bubblewrap profile `writ-bwrap/1`** (pinned; digested into `environment_digest`): unshare every namespace, new session, drop all capabilities, cleared environment plus the job environment, read-only binds of the pinned runtime profile only, minimal `/proc` and `/dev`, private tmpfs `/tmp`, read-write bind of the job work directory only. Never bound: `/var/lib/writ` (except the job's directory), `/run/writ`, `/etc/writ`, `/srv/writ`, `/sys/fs/cgroup`, the host `/proc`, any socket. `--die-with-parent` is convenience only. Landlock optional defence in depth.
- **Pinned runtime profiles** `writ-runtime/<name>`: exact package versions plus a manifest digest of every file under the read-only binds; command digest plus runtime profile digest plus bubblewrap profile digest plus the private bubblewrap binary digest form `environment_digest`. A change refuses until re-registration.
- **Termination:** `cgroup.kill`, then wait for `populated 0` (bounded); if it does not empty, the job is failed, the cgroup quarantined, readiness drops. Process-group signals are secondary only.
- **Supervisor or broker failure:** systemd kills the unit cgroup on stop; at start the supervisor kills and removes leftover `jobs/job-*` cgroups and sweeps `run-*` directories **before** accepting work; their runs stay `started` (uncertain, never re-run). Operator abandonment: J-10 Q-3.

## J-6 Host identity (lead decision on review H-4, per D-045 M5-D7)

Sealed requests built by the service-side adapter:

- **From configuration only:** `principal.id` and the principal's roles, from the per-uid configuration of the admitting socket; `principal.verified_by` (adapter id) from the configuration of that socket; tenant and project from the (socket, uid) binding (J-2); `os_principal` from peer credentials.
- **Generated by the service:** `session_id`, one per admitted connection (random, never taken from the host).
- **Always null in the sealed request:** `agent_id` and `parent_session_id`. Any host-provided values appear only under `host_asserted: {agent_id, host_session_id, parent_agent_id, parent_session_id, tool_name, host_version}`, which is recorded, fingerprinted and displayed with the label `host-asserted`, and is **never authority** although the adapter MACs it.
- **Without a trusted agent identity** (until M5b-06): agent-specific grant consumption and separation that depends on agent identity refuse `IDENTITY_UNVERIFIED` in the governed profile. They are never silently weakened to principal-level checks (review H-4; the D-042 S6 separation is kept, not reinterpreted). `delegation_chain` stays `unavailable`. The hook client and MCP shim hold no key.

## J-7 `fs.write` adapter recovery contract

### J-7.1 Scope, layout and canonical targets

- Action kind `fs.write`; resource `file:<tenant>/<project>/<root id>/<relative path>`; attributes bound by the action digest: `snapshot_id`, `manifest_digest`, `source_path`, `content_digest`, `mode` (`create_only` or `replace_if`), `expected_prior_digest` (`replace_if` only).
- **Root layout (F3):** each configured root `R` (under `/srv/writ/targets/`) contains exactly `R/staging` (0700) and `R/tree` (the published tree). Targets are resolved **only beneath `tree_fd`**; staged and displaced files live only in `staging`. Both are on the same filesystem and mount as `R` (checked at open).
- **Canonical relative paths (F3, F11):** valid UTF-8; components separated by single `/`; no leading or trailing `/`, no empty, `.` or `..` component, no NUL or control character; at most a configured length and depth. The **target key** is this exact byte string; no normalization is applied and none is needed because case-folding directories are refused (F11).
- M5 writes regular files into **existing** directories only; a missing parent, a directory, non-regular file or `nlink > 1` at the target is a precondition failure (J-7.5).
- **Overlap (F11):** configured roots must not be equal to, inside, or contain another root (checked at open by walking parents from each root descriptor with `..` and comparing identities); overlapping configuration refuses to open.

### J-7.2 Filesystem identity and the mount rule (F2, F11, F10)

- **Persisted identities** use `{fs_uuid, ino, btime}`: `fs_uuid` is the filesystem UUID (via `FS_IOC_GETFSUUID` only if a safe wrapper exists under `unsafe_code = "forbid"`; otherwise the superblock UUID recorded by the operator at root creation and verified at every open by resolving the root's `st_dev` to its block device and matching the udev `by-uuid` link); `ino` and `btime` (`statx` `STATX_BTIME`, required: a filesystem without birth time refuses) identify a file within that filesystem. Inode generation may replace `btime` if read safely. `st_dev` and `mnt_id` are **never persisted**.
- **Journal binding:** the journal records `fs_uuid`, the root's `{ino, btime}`, the ledger id and the root id; an open with any difference refuses.
- **Mount rule, re-derived at each open:** at open the adapter records the root's `mnt_id` (`statx` `STATX_MNT_ID`) for **this adapter lifetime only**. Every per-operation resolution uses `openat2` with `RESOLVE_BENEATH | RESOLVE_NO_SYMLINKS | RESOLVE_NO_MAGICLINKS | RESOLVE_NO_XDEV`, and every directory and file reached must report the same `mnt_id` and `fs_uuid`. Mount crossings are thereby refused explicitly (M5-D8).
- **Ownership and ACLs (F10):** at open and per operation, every directory on the path (`R`, `staging`, `tree`, each parent) is owned by `writ`, not group- or other-writable, and has **no access or default ACL** (`system.posix_acl_access`, `system.posix_acl_default` absent); staged and target files have no access ACL.
- **Locks (F11):** the journal's `.owner` lock, plus an exclusive `flock` on the descriptor of `R/staging` held for the adapter lifetime (refuses a second journal or adapter for the same root even through another path).
- **Filesystem allow-list:** ext4 or xfs, without the `casefold` feature (operator-recorded and probed, J-9); network, FUSE, overlay and tmpfs refuse.

### J-7.3 Exclusive-writer assumptions

| ID | Assumption | Enforced by | Detected by |
|---|---|---|---|
| E1 | Only the adapter writes under `R` | OS ownership, modes, no ACLs (J-7.2); `fs.protected_hardlinks = 1` (probed) | drift (J-7.6) |
| E2 | One adapter instance per root, and recovery never overlaps an apply | journal `.owner` lock, `flock` on `staging`, journal bound to ledger and root identity, the adapter-wide lock (F1) | open refuses on mismatch |
| E3 | Root and other `writ` processes do not write `R` | not enforceable | drift; taint |
| E4 | Atomic `renameat2` (`RENAME_NOREPLACE`, `RENAME_EXCHANGE`), durable `fsync`, stable `{ino, btime}` while a file exists | filesystem allow-list and probes | not in general; outside the allow-list the adapter refuses |
| E5 | No hard links into `R` from elsewhere | `nlink == 1` for targets, staged and displaced files; `fs.protected_hardlinks = 1` | per-operation `statx` |

### J-7.4 Journal and operation identity

`writ-fseffects-1`, owned SQLite file per root, WAL with **`synchronous=FULL`** (F6), owner-epoch fencing (M-2), bound to one ledger and one root identity.

- `ops`: `op_id` (`fsop-<permit id>`), `permit_id` (unique), tenant, idempotency key, request fingerprint, `target` (canonical key), mode, `expected_prior` (`{digest, fs_uuid, ino, btime}` or `absent`), `content_digest`, `snapshot_id`, `staging_name` (`<ledger id short>-<op_id>`, F11), `state` (`intent`, `staged`, `resolved`), `staged_identity` (`{fs_uuid, ino, btime, size, digest}`), timestamps.
- `outcomes`: `op_id`, `operation_evidence` (`completed`, `not_completed`, `unknown`), `desired_state` (`verified`, `differs`, `absent`, `unknown`; L1), `result` (`applied`, `failed`, `unknown`), `reason`, `target_identity_after`, `displaced_identity` (`replace_if`), recorded_at, boot id.
- `targets`: `target`, `blocked_by`, `tainted` and reason, `last_identity`.
- `root_state`: `unrecoverable_boot_id` (F12).
- Partial unique index: at most one op with `state != 'resolved'` per target.

**Operation-specific evidence.** The staged file is created by this operation (exclusive create under its unique staging name) and its `{ino, btime}` is durably recorded before the rename. Neither staged nor displaced files are unlinked before the op's resolution is committed (F5), so their identities cannot be reused while unresolved. Content equality is never evidence (M5-D8).

**`desired_state` (L1)** is computed for every recorded outcome: `verified` when the target's identity equals the staged identity and its re-read digest equals `content_digest`; `differs` when the target exists with another identity or digest; `absent` when there is no target; `unknown` when the target cannot be observed.

### J-7.5 Normal path, results and crash boundaries

**Concurrency (F1).** One **adapter-wide exclusive lock** is held by `apply` from T-intent to its return, and by recovery for its whole run. Recovery runs at journal open before any operation is accepted and before broker reconciliation reads evidence; an operator-requested recovery takes the same lock. Recovery skips any op whose `op_id` is in the in-memory set of live applies (defence in depth; the lock already excludes overlap).

**Results (F9; trait documentation amendment of `EffectAdapter::apply`).** After T-intent commits, `apply` returns:

- `Ok(Ok(Applied))` only after T-outcome `applied` is committed;
- `Ok(Ok(Failed))` only for precondition outcomes committed in T-intent or for `EEXIST` from `RENAME_NOREPLACE` (F8), each with a recorded `reason`: `TARGET_UNRESOLVED` (another op on the target is unresolved), `TARGET_TAINTED`, `TARGET_PRECONDITION_FAILED` (for `create_only` the target exists; for `replace_if` no regular single-link file or a prior digest different from `expected_prior_digest`; missing parent; directory or non-regular target). In each case the **permit is consumed** and the op is recorded resolved with result `failed` and `operation_evidence = not_completed` (nothing was attempted); the receipt becomes `observed_failure`; `EffectEvidence` gains the `reason`;
- **`Err(TxFailure { outcome_unknown: true, .. })` for every other failure after T-intent** (filesystem errors, verification failures, any commit failure); the op is left to recovery and the receipt stays `dispatched`.

Before T-intent commits, failures follow the existing contract (`LEDGER_UNAVAILABLE` before permit checks, D-025 B; refusals roll back).

| Step | Action | Boundary or failure |
|---|---|---|
| T-intent | begin; broker `verify`; `PERMIT_REPLAYED`; root not unrecoverable this boot (F12; else `Err` before consumption); target checks through `tree_fd` (precondition results above); `targets.blocked_by` null and not tainted; insert op `intent`, set `blocked_by`; commit | **B0** before commit: nothing durable. **B1** commit outcome unknown: **stop** (F6): no filesystem step; close and reopen the journal before anything reads it; return `TxFailure` unknown |
| Stage | read source bytes from the snapshot store (digests re-verified); `openat(staging_fd, staging_name, O_CREAT | O_EXCL | O_WRONLY | O_NOFOLLOW | O_CLOEXEC, 0600)`; write; `fsync`; re-read and compare `content_digest`; `statx` for `{ino, btime, size}`; check no ACL, `nlink == 1`; `fsync(staging_fd)` | **B2** partial file. **B3** before `fsync`. **B4** before T-staged. `EEXIST` at create (L2): drift, the existing file is **not** removed, target tainted (journal transaction), `TxFailure` unknown. Other errors: `TxFailure` unknown (recovery decides; nothing removed inline) |
| T-staged | `state = staged`, `staged_identity`; commit | **B5** commit unknown: stop, reopen the journal, `TxFailure` unknown |
| Recheck (F3) | `statx` at the staging name equals `staged_identity`; target through `tree_fd` equals the recorded prior identity (`absent`, or `{ino, btime}` and digest) | **B6**. A mismatch: no rename; re-observe via the R-table under the held lock (F8) and return `TxFailure` unknown unless the R-table commits a definitive row |
| Rename (F4) | `create_only`: `renameat2(staging_fd, staging_name, parent_fd, name, RENAME_NOREPLACE)`. `replace_if`: `renameat2(..., RENAME_EXCHANGE)`: the staged file becomes the target and the displaced prior file now sits at the staging name | **B7** before rename. **B8** after rename, before `fsync`. `EEXIST` from `RENAME_NOREPLACE`: not applied (atomic refusal), target tainted, T-outcome `failed` reason `drift` (F8). **Any other rename error**: re-observe via the R-table, never `failed` from the errno alone (F8) |
| Durability | `fsync(parent_fd)`, `fsync(staging_fd)` | **B9**. `EIO` from any `fsync`: mark the root unrecoverable for this boot (F12), `TxFailure` unknown |
| Verify | target `{ino, btime}` equals `staged_identity`, re-read digest equals `content_digest`; for `replace_if` the identity at the staging name equals the recorded prior identity (displaced check) | **B10**. Any mismatch or error: re-observe via the R-table (F8) |
| T-outcome | `outcomes` row (`completed`, `desired_state`, `applied`, identities), `targets.last_identity`, `blocked_by = null`, `state = resolved`; commit | **B11** commit unknown: reopen the journal, `TxFailure` unknown. **B12** after commit, before T-observe (existing broker semantics) |
| Cleanup (F5) | only after the resolution commit: unlink the staging name if it holds the recorded staged or displaced identity; `fsync(staging_fd)` | crash here: staging names of resolved ops are garbage-collected at open (only files whose identity equals a recorded staged or displaced identity of a resolved op; anything else is reported, never removed) |

The permit is consumed at T-intent; the op is never re-run and the receipt never redispatched (D-035 item 3).

### J-7.6 Recovery and reconciliation decision table

Recovery holds the adapter-wide lock (F1). Before reading, it closes and reopens the journal connection if any commit failed since open (F6). If the root is marked unrecoverable for the **current** boot id, recovery records nothing and every unresolved op stays unknown (F12): after a write-back `EIO`, Linux may drop dirty pages and report later `fsync` calls as successful, so same-boot observations are not trusted. Otherwise it makes the observed state durable (`fsync` of `tree` parents, `staging` and the target if present; an `EIO` here marks the root unrecoverable and stops), then observes **T** (target: `absent`, `= staged`, `= prior`, `other`) and **S** (staging name: `absent`, `= staged`, `= prior`, `other`), all by `{fs_uuid, ino, btime}`.

| # | Journal | Mode | Observation | Operation evidence | Result | Target after |
|---|---|---|---|---|---|---|
| R1a | `intent` | any | T `= prior`; S absent | `not_completed` (no rename precedes a durable T-staged) | `failed` | unblocked |
| R1b | `intent` | any | T `= prior`; S present (identity not recorded) | `not_completed` | `failed`; S **kept** (cannot tell an own partial file from a foreign one; L2) | blocked (reason `unrecorded staging file`) until operator disposition |
| R2 | `intent` | any | T differs from prior | `unknown` (exclusive-writer violated; F7) | `unknown` | **tainted** |
| R3 | `staged` | `create_only` | T `= staged`; S absent; digest equals; `nlink == 1` | `completed` | `applied` | unblocked; `last_identity` = staged |
| R3x | `staged` | `replace_if` | T `= staged`; S `= prior` (displaced) ; digest equals | `completed` (F4) | `applied`; displaced file removed only after the commit (F5) | unblocked |
| R3y | `staged` | `replace_if` | T `= staged`; S `other` or absent | `unknown`: the exchange displaced something other than the prior file, or the displaced file vanished | `unknown`; **keep** the displaced file | **tainted** |
| R4 | `staged` | any | T `= staged`; digest differs | `completed` but content changed afterwards | `unknown` | **tainted** |
| R5 | `staged` | any | S `= staged`; T `= prior` | `not_completed` | `failed`; staged file removed after the commit | unblocked |
| R6 | `staged` | any | S `= staged`; T neither prior nor staged | `unknown` (external change) | `unknown`; keep S | **tainted** |
| R7 | `staged` | `create_only` | S absent; T not staged | `unknown` | `unknown` | **tainted** |
| R8 | `staged` | any | S `other`; or T `= staged` with `nlink > 1`; or S and T both the staged identity | `unknown` | `unknown` | **tainted** |
| R9 | any | any | any descriptor, `statx`, read or `fsync` failure, identity or mount mismatch, root unrecoverable this boot | not observable | nothing recorded; retried later (after reboot for F12) | blocked |
| R10 | no op for the permit | n/a | n/a | n/a | nothing recorded: absence is not evidence (D-035 item 3; Q-2 answered) | n/a |
| R11 | `resolved` | any | at the next T-intent for the target, T differs from `last_identity` | n/a | the new op is `failed` `TARGET_TAINTED`; target tainted | **tainted** |

Rules: ambiguous evidence stays **unknown**; the adapter never converts `unknown` to `applied` or `failed`; a tainted or blocked target refuses every later op until an owner-confirmed disposition (J-10 Q-3), which never changes a recorded result or reconciles a receipt.

**Evidence routing (L3).** The broker routes `apply`, `inspect` and `evidence` by action kind to the `fs.write` adapter (as it does for the runner; permit ids are unique per ledger, and a record in two adapters is an integrity failure). `evidence` and `inspect` return evidence only for resolved ops with result `applied` or `failed`; for an unresolved or `unknown` op they return none, so the broker answers `RECONCILIATION_REQUIRED` and the receipt stays uncertain.

**R1 and R5 soundness.** Both rows depend on E1, E2 and E4 and on the protocol order (no rename before a durable T-staged; staged and displaced files kept until resolution). **Power loss (L4):** after a power loss, R5 means "not durably applied": the rename may have executed and its new content may have been transiently visible to readers of `tree` before the crash, but it did not survive; consumers rely on the journal outcome, never on content they observed.

**Repeated identical content and later writes.** Distinct ops have distinct staged identities, so identical bytes never produce shared evidence; `replace_if` to the same bytes is evidenced by identity like any other. A later op compares the target with `last_identity` (R11), so an external rewrite is detected even with equal bytes.

### J-7.7 Tests required (M5a-09, written first)

SIGKILL at each boundary B0..B12 then recovery, asserting the row reached; ambiguous commits at T-intent (must stop, F6), T-staged and T-outcome with both durable states; every row R1a..R11 constructed explicitly, including R3x and R3y; adapter-wide lock: recovery requested during an apply waits and skips live ops (F1); identity across remount and reboot (`mnt_id` changes, `{fs_uuid, ino, btime}` stable; F2); staging `EEXIST` keeps the file (L2); rename errors other than `EEXIST` reach the R-table (F8); `fsync` `EIO` (fault-injecting device such as `dm-error` or `dm-flakey` in the reference VM) marks the root unrecoverable and blocks R3 and R5 conclusions until reboot (F12); default ACL on a parent, access ACL on a target, casefold directory, overlapping roots, second adapter on the same root through another path (F10, F11); repeated identical content; later writes after applied, failed and unknown ops; path races (symlink swap, directory rename, bind mount, hard link, replacement between recheck and rename); `ENOSPC` and permission failures at each step; replayed permit; journal bound to another ledger or root; CO-PROP-01 (M5a-10).

## J-8 Graceful shutdown (review H-1, M-2)

**Sockets held across restarts.** Option (a), preferred: systemd socket activation (`writ-host.socket`, `writ-admin.socket`, `writ-approval.socket` for the loopback TCP port) holds the Unix sockets and the TCP port across service restarts, so no other process can bind them in between. Option (b): bind the approval port below `net.ipv4.ip_unprivileged_port_start` with `CAP_NET_BIND_SERVICE`, with a probe that the agent uid cannot bind it. In either case `writ-serviced` **refuses to start** if it does not receive or cannot bind every configured socket, and verifies the received sockets' type, address and mode.

Sequence (CO-M4X-05; M5-D16):

1. **SIGTERM or SIGINT:** enter `stopping`. Listening sockets **stay open**; new connections and new requests on open connections are answered with the generic refusal `SERVICE_STOPPING` (the loopback page answers 503). No new effect, job, ingest or ceremony begins.
2. **Drain** (configured, default 10 s, maximum 30 s): requests in progress finish. Ceremonies not finished are left (challenges stay issued and expire). Uncommitted ingest sessions are abandoned (nothing visible).
3. **Effects:** an `fs.write` in progress runs to its return within the drain period; whatever its state, recovery handles it (J-7.6).
4. **Runner jobs:** after the drain period, `cgroup.kill` and verified empty (J-5); the run is finished `failed` with `terminated_shutdown` if T-finish succeeds, otherwise stays `started`.
5. **Release ownership:** join every task holding a store handle, then drop the broker, the effect store, fs journals and snapshot store, then the ledger (each `OwnerLock` unlocked before close under the lifecycle gate); exit 0. Owner-epoch fencing on the snapshot store and fs journals (J-4, J-7.4) guarantees that a straggler holding an old handle cannot write after a new owner opens.
6. **Second signal or hard deadline** (drain plus 5 s): exit immediately non-zero; the OS releases locks; uncertain outcomes are preserved. `KillMode=mixed`, `TimeoutStopSec` above the hard deadline (systemd then kills the whole unit cgroup, including job cgroups).

Tests: SIGTERM during a ceremony, an ingest, each `fs.write` boundary and a running job; refusals answered while stopping; another process cannot bind the sockets during a restart; a new owner opens immediately after exit; second signal; hard deadline; no result changes because of shutdown except step 4.

## J-9 Reference environment and capability probes (review M-4, H-3, L-3; lead decision M-5)

**Reference image: Ubuntu 24.04 LTS (amd64, official cloud image)**, matching the dependency review host facts, as the owner-designated disposable VM with snapshot rollback. `kernel.apparmor_restrict_unprivileged_userns = 1` **stays set**. A private bubblewrap copy at `/usr/local/libexec/writ/bwrap` (`root:writ`, 0750, SHA-256 pinned in configuration and the readiness report) is the only binary that receives an AppArmor profile granting `userns`; profiles attach by path, so the system `/usr/bin/bwrap` and any copy the agent makes elsewhere stay restricted, and the agent cannot execute the private copy (not in group `writ`). A probe verifies that the **agent uid cannot create user namespaces** at all.

**Recorded versions (metadata only; the image digest is never a readiness input):** image name and SHA-256; kernel; systemd; bubblewrap package version and both binaries' digests; AppArmor profile text digest; util-linux, coreutils, glibc, SQLite library versions where linked; Rust toolchain; filesystem type, features (no `casefold`) and UUID of `/var/lib/writ` and `/srv/writ`; mount options; cgroup2 controllers delegated; sysctls `kernel.apparmor_restrict_unprivileged_userns`, `kernel.apparmor_restrict_unprivileged_unconfined`, `user.max_user_namespaces`, `kernel.yama.ptrace_scope`, `fs.protected_symlinks`, `fs.protected_hardlinks`, `fs.suid_dumpable`, `kernel.core_pattern`, `net.ipv4.ip_unprivileged_port_start`; Landlock ABI; runtime profile digests; Claude Code version (M5b).

**Probes.** Every probe has a positive run and a **negative control** that must make it fail; a probe whose negative control does not fail is itself failed.

| Probe | Checks | Negative control |
|---|---|---|
| P-USERNS | the service can run the private bubblewrap with every namespace | private copy without the AppArmor profile fails |
| P-AGENT-USERNS | the agent uid cannot create a user namespace (`unshare -U`, system `bwrap`, a copied `bwrap`) | a test uid granted the profile succeeds, proving the probe detects it |
| P-FS | inside a job: writes outside the work directory, reads of `/var/lib/writ`, `/etc/writ`, keys, `/srv/writ`, and access to `/run/writ` all fail | profile with an extra bind fails the probe |
| P-NET | inside a job: only loopback in a new namespace; connect to the host approval port and external addresses fail; host abstract sockets unreachable | job without network unsharing fails the probe |
| P-PID | host processes invisible from a job; no signal to service processes | job without PID unsharing |
| P-CGROUP | delegated subtree writable by the service; controllers enabled; `cgroup.kill` present; job cgroup files and the unit's cgroup files not writable from a job or by the agent uid | cgroup without delegation |
| P-KILL | setsid and double-fork survivors gone after `cgroup.kill`; `populated 0` | process-group kill alone leaves a survivor |
| P-LIMITS | memory OOM recorded in `memory.events`; `pids.max` stops a fork bomb; CPU quota observed | limits unset: the fork bomb exceeds the count |
| P-FD | job sees only descriptors 0..2 | spawn with an inherited extra descriptor is detected |
| P-OPENAT2 | `RESOLVE_BENEATH`, `RESOLVE_NO_SYMLINKS`, `RESOLVE_NO_XDEV` honoured; `statx` mount id and birth time present | bind mount inside a test root refused |
| P-RENAME | `RENAME_NOREPLACE` and `RENAME_EXCHANGE` on the target filesystem | tmpfs-backed test root reported unsupported for the allow-list |
| P-FS-TYPE | target root and stores on ext4 or xfs without `casefold`; UUID matches the recorded one | overlay or tmpfs refused |
| P-ACL | no access or default ACL on any trusted directory or file | a planted default ACL is detected |
| P-HARDLINK | `fs.protected_hardlinks = 1`; the agent cannot hard-link a `writ`-owned file | sysctl at 0 in the VM is detected |
| P-PEERCRED | peer credentials match the connecting uid; overflow uid refused | connection from a namespace with unmapped ids yields 65534 and is refused |
| P-AGENT-DENY | as the agent uid: read keys, ledger, stores, journals, configuration; write snapshot store, target roots, work root; connect `admin.sock`; `ptrace` the service; read `/proc/<service pid>/environ`; read core dumps (`LimitCORE=0`, `fs.suid_dumpable`, `core_pattern`, systemd-coredump storage); read the service's `/tmp` and `/dev/shm` objects (`PrivateTmp`); reach service abstract sockets; read service journal entries containing canaries; stop, restart or reconfigure `writ.service` through `systemctl`, D-Bus or polkit; membership in `sudo`, `adm`, `systemd-journal`, `writ`, `docker`, `lxd`, `disk` groups; any sudo rule | a canary key file planted with a deliberately loose mode **must be reported readable**, proving the probe can see a failure |
| P-NO-SECRET | no key material in command lines, environments, logs, journal or readiness reports | planted canary detected |

**`VerifiedDeployment` (static start-time check bound to the operator-run report).** The agent-uid probes can only be run by the operator (the service cannot act as the agent). The operator runs `writ-probe` as root on the reference host; it produces `writ-readiness/1` with every result and the static facts it observed (owners, modes, ACLs, group memberships, sysctls, binary digests, AppArmor profile digest, filesystem UUIDs). The report's digest is recorded in the service configuration. At start, `writ-serviced` performs the **static** checks it can perform as `writ` (ownership, modes, ACLs, group memberships of the configured agent uids, sysctls, binary and profile digests, filesystem identity, its own uid) and issues a `VerifiedDeployment` only when every probe in the configured report passed and every static fact equals the report's. It **rechecks periodically** (configured interval) and before each job; any difference withdraws the deployment proof, so `immutable()` turns false and containment reports missing guarantees until the operator re-runs the probes.

## J-10 Open questions and answers

**Owner decisions (new trust-boundary decisions; not resolved in this document):**

- **C-1 Approval display integrity when the agent and the human share a desktop user.** If the agent runs as the human's desktop user, it can drive the human's browser, read the session bus, inject input into the display and alter what the approval page shows, so "the human approved what was displayed" is not established against the agent even with WebAuthn user verification.
  - Options: (a) the governed profile requires a **dedicated agent uid** distinct from the human's desktop and browser user, with no access to the human's home directory, display sockets (X11, Wayland), session bus or browser profile, verified by probes (an additional P-AGENT-DESKTOP probe); (b) shared user allowed as an **owner-accepted residual**, with no claim that approvals are agent-proof; (c) approvals only on a separate device.
  - Recommendation: (a) for the governed profile; (b) only as a recorded, owner-accepted residual for the advisory profile, labelled; (c) as a later option.
- **H-2 Identity of runner job processes.** Jobs run as `writ` inside a user namespace share the service's uid outside the namespace; a sandbox escape would act as `writ`, with full access to trusted state and keys.
  - Options: (a) a **separate unprivileged job uid** (`writ-job`), entered through a narrowly scoped root-owned launcher (setuid or a privileged systemd helper) that does nothing but switch to that uid, enter the job cgroup and exec the private bubblewrap; (b) jobs as `writ` inside the user namespace, with an **owner disposition** accepting that a bubblewrap or kernel escape reaches trusted state.
  - Recommendation: (a); it adds a reviewed privileged component, which the owner must accept, and keeps an escape away from keys and the ledger.
- **Q-3 Operator dispositions (review L-7 split; owner to confirm):** key **revocations** may be recorded through `admin.sock` by the local operator procedure (revocation only reduces authority); dispositions that **unblock or abandon** (releasing a tainted, unknown or `unrecorded staging file` `fs.write` target; abandoning a `started` run) require an authenticated human `Operator` ceremony on the approval page (a new ceremony operation, versioned). Neither ever changes a recorded result or reconciles a receipt.

**Answered:**

- **Q-1 (loopback approval channel):** unchanged (D-040 O-12); display integrity is now C-1.
- **Q-2 (absence of an intent row as evidence):** **no.** D-035 item 3 stands (lead decision); changing it needs an owner amendment. J-7.6 R10.

**Choices within D-045 (lead-reviewed):** one owner process (J-1); Tokio peer credentials, `rustix` `net` only in `writ-hostsys` (J-2); key registry and revocation in the ledger, `key_id` in permit and seal MACs (J-3); snapshot ids from scope and manifest, no deletion in M5 (J-4); delegated cgroup subtree with the root-owned exec helper (J-5); no directory creation, identity evidence by `{fs_uuid, ino, btime}`, `RENAME_EXCHANGE` for `replace_if`, ext4 and xfs without casefold (J-7); socket activation preferred (J-8); refusal codes `SNAPSHOT_LIMIT`, `TARGET_UNRESOLVED`, `TARGET_TAINTED`, `TARGET_PRECONDITION_FAILED`, `IDENTITY_UNVERIFIED`, `SERVICE_STOPPING`.
