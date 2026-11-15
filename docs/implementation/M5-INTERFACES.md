# M5 shared interfaces (lead-owned; D-045)

**Status: DRAFT for lead review** (prepared by worker W-P at baseline `8fbf26d`; documentation and design only, no code). Nothing here is binding until the lead marks a section binding (M5a-01); no consumer is integrated against an unreviewed section. J-7 additionally needs an **independent review** before the `fs.write` adapter is integrated (D-045 M5-D8, M5a-08). Plan and tasks: [M5-START-PLAN](M5-START-PLAN.md). Format follows [M4-INTERFACES](M4-INTERFACES.md): each section states the interface, the rules a worker must keep, and what is a choice made within D-045 versus a question for the lead or owner (J-10).

Names of crates, types, files, tables and refusal codes are proposals; the lead may rename them. Every new refusal code gets an internal code and a generic external mapping (INV-44).

## J-0 Layering and crate plan

Rule: **kernel and broker stay free of OS, sandbox, MCP, network service and model SDK code.** The broker keeps only storage-independent logic and traits; it already contains the M3 process lifecycle gate and the test-only `LocalProcessSandbox` ([process.rs](../../broker/src/process.rs)), which stay as they are, and its `rustix` dependency stays at feature `time`. New OS code lives in new crates outside the broker closure.

| Layer | Crate (proposed) | May depend on | Must not contain | Enters the closure of |
|---|---|---|---|---|
| Kernel | `writ-kernel` | unchanged | anything M5 | everything |
| Broker logic | `writ-broker` | kernel, existing deps | OS calls beyond today's, sockets, Tokio, bubblewrap or cgroup handling, MCP, model code | everything trusted |
| Broker additions (types and traits only) | `writ-broker`: `keys` (key-source trait, `KeyPurpose`, validity), `effects` (non-transactional adapter contract text, adapter routing by action kind), `refusal` (new codes), `host` (sealed host-identity labels) | as above | implementations of the above | everything trusted |
| OS primitives | new `writ-hostsys` | `rustix` (`fs`, `process`; reviewed, M5a-02), kernel | business logic, SQL, network listeners | keys, persistence, fs adapter, containment, service, probe |
| Persistence | `writ-ledger-sqlite`: new `snapshots` module (`writ-snapshots-1`), `fs_journal` module (`writ-fseffects-1`), ledger additions for key revocation (schema change owned by the lead) | broker, rusqlite, `writ-hostsys` (deployment proof type only) | HTTP, sockets, sandbox | service |
| Key source | new `writ-keys` | broker, `writ-hostsys` | network, environment reads, command-line key input | service, probe |
| `fs.write` adapter | new `writ-fs-effects` | broker, persistence (journal), `writ-hostsys` | network | service |
| Containment | new `writ-containment` (lib: implements `broker::runner::Sandbox`; bin: `writ-cgroup-exec`) | broker, `writ-hostsys` | network, SQL | service; the helper binary is in the trusted computing base |
| Local RPC | new `writ-rpc` (framing, peer-credential admission, strict JSON through the kernel parser) | kernel, Tokio (`rt`, `net`, already reviewed) | MCP, model code | service, hook client, CLI client |
| Service | new `writ-service` (bin `writ-serviced`: composition root, signal handling) | all trusted crates above, `writ-approval`, Tokio `signal` (reviewed, M5a-02) | MCP, model SDK | itself |
| Approval channel | `writ-approval`, `writ-approval-server` | unchanged plus Tokio `signal` (J-8) | unchanged | unchanged |
| Probe | new `writ-probe` (bin) | `writ-hostsys`, `writ-containment` | anything that opens trusted state | operator procedure only |
| Host contract (M5b) | new `writ-host-contract` (envelope, coverage matrix, normalization interface) | kernel | OS, MCP, model | service, adapters |
| Host adapters (M5b) | new `writ-host-claude` (lib: trusted normalizer used by the service; bin `writ-hook`: untrusted hook client), new `writ-host-cli` (independent normalizer plus client) | host contract, `writ-rpc` | keys (clients), MCP in the lib | lib: service; bins: agent side |
| MCP shim (M5b) | new `writ-mcp-shim` (bin, agent side) | `writ-rpc`, the reviewed MCP implementation (M5b-02) | keys, ledger access | agent side only, **never the service closure** |
| Model side (M5b) | new `writ-model-contract`, `writ-model-stub-a`, `writ-model-stub-b`, `writ-agent-runner-stub` | kernel, model contract | any provider SDK, network | none of kernel, broker or service |

Dependency review needs (nothing adopted yet; D-041 process, exact allow-list entries per crate, licence and advisory checks, `check_deps.py` closures for every new crate):

- `rustix` features `fs` (`openat2` with resolve flags, `statx` with mount id, `renameat` with `RENAME_NOREPLACE`, `fstatfs`, `fsync`, `fgetxattr` for ACL detection) and `process` (process-group and pidfd signalling as secondary cleanup). `net` only if Tokio's peer-credential support is judged insufficient (J-2).
- Tokio `signal` (adds `signal-hook-registry`); possibly `sync` and `io-util` for the RPC listener (to be listed exactly in the review).
- An MCP implementation for the agent-side shim (a maintained Rust implementation, for example the official Rust MCP SDK, is a candidate only; the protocol version is pinned in the review; nothing adopted, M5b-02).
- External binaries recorded with the reference image (not Cargo dependencies): bubblewrap, systemd.

## J-1 Process and privilege model

**Identities.** Service user `writ` and group `writ` (system account, no login shell, no home outside `/var/lib/writ`). The agent runs as the human's user (`agent uid`), which is **not** in group `writ`. The operator acts as root on the disposable reference host only, through the install, provisioning and teardown scripts (M5-D3: no automatic user creation or permission change on the owner's workstation).

**Layout (owner `writ:writ` unless stated; no ACLs; checked at start, J-9 probe P-AGENT-DENY):**

| Path | Mode | Contents | Agent access |
|---|---|---|---|
| `/var/lib/writ` | 0700 | trusted state root | none |
| `/var/lib/writ/ledger/` | 0700 | ledger, `-wal`, `-shm`, `.owner` | none |
| `/var/lib/writ/stores/` | 0700 | effect store, `writ-snapshots-1`, `writ-fseffects-1` journals, `.owner` files | none |
| `/var/lib/writ/keys/` | 0700 | key files, 0600 (J-3) | none |
| `/var/lib/writ/run/` | 0700 | runner work directories `run-<permit id>` | none |
| `/var/lib/writ/targets/<root id>/` | 0755 or 0750 (configured per root; never group- or other-writable) | `fs.write` target roots (J-7) | read only if configured |
| `/etc/writ/` | 0750 `root:writ` | configuration (no secrets) | none |
| `/run/writ/` | 0755 | sockets | connect to `host.sock` only |
| `/run/writ/host.sock` | 0666, peer-credential admission (J-2) | host ingress, snapshot ingest, status | connect, admitted by uid allow-list |
| `/run/writ/admin.sock` | 0600 | operator operations (readiness, command registry, key reload) | none (connect refused by mode and by peer uid) |

**One ledger owner.** One process, `writ-serviced` (systemd unit `writ.service`, `User=writ`, `Delegate=yes` for the runner subtree, J-5), owns the ledger through the existing `OwnerLock` path and owns every store bound to that ledger. It composes, in one process:

- the broker (decide, execute, reconcile) and the stores;
- the approval listener (`writ-approval`, loopback TCP as in M4; WebAuthn is the human authentication, not the socket);
- the sealing part of host adapters (normalize, bind tenant and project from configuration and peer credentials, seal with the ingress key it holds in memory);
- snapshot ingest;
- the runner supervisor, which launches jobs into per-job cgroups (J-5).

`writ-approval-server` remains a standalone development entry point; it and `writ-serviced` cannot own the same ledger at once (`OWNED_ELSEWHERE`). No second process opens the ledger for writing; a later split (for example a separate approval process) must reach the owner over authenticated RPC, never open the ledger itself (M5-D3). *Choice within D-045:* composition in one owner process rather than RPC between separate owners, because it keeps a single ownership lock, a single fencing epoch and no new inter-process trust.

**What the agent identity can reach:** `host.sock` (admitted by uid; every request is untrusted input), the loopback approval page (every protected read or mutation needs a user-verified passkey, M4), read-only target roots if configured, and nothing under `/var/lib/writ`, `/etc/writ` or `admin.sock`. It cannot signal or trace service processes (different uid; `kernel.yama.ptrace_scope` recorded); it can read `/proc/<pid>/cmdline` of service processes, so no secret is ever placed in a command line, and no secret is placed in any environment (M5-D6). Runner jobs reach nothing outside the job (J-5).

## J-2 Local RPC over Unix sockets

- **Transport:** `SOCK_STREAM` Unix sockets in `/run/writ/`. Frames: 4-byte big-endian length, then one UTF-8 strict JSON document parsed by the kernel's strict parser (INV-41). Maximum frame size per endpoint (control frames 64 KiB; ingest data frames 1 MiB); larger or malformed frames close the connection with a generic error (INV-18). One request in flight per connection; idle and per-request deadlines; bounded connection count per uid.
- **Admission:** on accept, the peer's credentials (`SO_PEERCRED`: pid, uid, gid) are read from the socket (Tokio `UnixStream::peer_cred`, already in the reviewed `net` feature; `rustix` `net` only if needed). Admission is by uid allow-list from configuration (`host.sock`: configured agent uids, each bound to tenant and project; `admin.sock`: root and `writ`). The pid is recorded for audit only and never used for authorization (pid reuse). Credentials are read once at accept and bound to the connection.
- **What admission proves:** the OS principal (uid) of the connecting process, nothing about which program, agent, human or session it is (M5-D7). It is recorded as `os_principal: "uid:<n>"` with `verified_by: "so_peercred"`.
- **Endpoints of `host.sock`:** `host.event` (unsealed host event, J-6), `effect.request` (broker-executed governed effect, M5b), `snapshot.ingest.*` (J-4), `status.receipt` (status of the caller's own requests by idempotency key, scoped to the bound tenant and project). Endpoints of `admin.sock`: `readiness`, `registry.*`, `keys.reload`, `operator.*` (J-10 Q-3).
- **Not MCP.** This is a Writ-internal protocol and is never called MCP; MCP exists only in the agent-side shim (J-0, M5-D9).

## J-3 Key source

```text
broker::keys
  enum KeyPurpose { Ingress, Permit, AcceptanceRecord, RunnerAttestation }
  struct KeyId(String)                       // "<purpose>/<label>", [a-z0-9._-], at most 64 bytes
  struct KeyValidity { not_before, not_after } // trusted time, whole seconds
  trait KeySource {
      fn key(&self, purpose: KeyPurpose, id: &KeyId, now: Timestamp) -> Result<KeyStatus<'_, Material>, KeysUnavailable>;
      fn issuing_key(&self, purpose: KeyPurpose, now: Timestamp) -> Result<(KeyId, &Material), KeysUnavailable>;
  }
```

The existing purpose-typed rings (`KeyRing`, `AcceptanceKeyRing`, `RunnerKeyRing`) are implemented on top of a `KeySource`; `KeyStatus` gains `Expired` and `NotYetValid` (both refuse, like `Revoked`). Implementation `writ-keys::FileKeySource`:

- **File format** `writ-key/1`: strict JSON `{"contract":"writ-key/1","purpose","key_id","not_before","not_after","material"}` with `material` hex of at least 32 bytes. One key per file, file name `<key_id with / replaced by _>.json`.
- **Loading (no-follow, descriptor-anchored):** open `/var/lib/writ/keys` once with `openat2` (`RESOLVE_NO_SYMLINKS | RESOLVE_NO_MAGICLINKS`), require a directory owned by the service uid, mode `0700`, no ACL, and every ancestor owned by root or the service uid and not group- or other-writable. Each file opened relative to that descriptor with `RESOLVE_BENEATH | RESOLVE_NO_SYMLINKS | RESOLVE_NO_XDEV` and `O_NOFOLLOW`, then `fstat`: regular file, owner service uid, mode exactly `0600` or `0400`, `nlink == 1`, size bounded. Any failure refuses that key and is reported to the operator as a key-integrity diagnostic (never the material).
- **Purpose separation:** the declared purpose must equal the purpose requested and the id prefix; the same material under two purposes or two ids is refused at load (digest comparison of material, compared in constant time).
- **Validity and rotation:** a key verifies only within `[not_before, not_after)` on trusted time; for each purpose exactly one key is the issuing key (the valid key with the latest `not_before`); others are verify-only until they expire. Rotation is provisioning a new file with a later `not_before` and an overlap at least the longest lifetime of what the old key authenticated (permits 30 s; seals per request; records and attestations their freshness window), then letting the old key expire or revoking it.
- **Permit key id (contract change, lead-owned):** permits gain `key_id` (versioned permit contract); without it rotation of the permit key needs trial verification against several keys, which this design avoids.
- **Revocation:** recorded in the ledger (`key_revocations`: key id, purpose, revoked_at, actor, reason), read on every lookup inside the same read as the use (no cache), so a revoked key refuses from the next lookup on. *Choice within D-045:* ledger rather than a file list, so revocation is ordered and audited with everything it affects. Who may record a revocation: J-10 Q-3.
- **Provisioning:** the operator procedure (script on the reference host) generates material from the OS random source as the service user, writes the file with the required mode atomically (temporary file in the same directory, `fsync`, rename, `fsync` of the directory) and never prints it; `keys.reload` on `admin.sock` loads new files. Material never appears in command-line arguments, environment, logs, `Debug`, readiness reports, evidence or the repository; a canary scan in tests checks logs, reports and the repository.
- **Failure:** unknown, revoked, expired, not-yet-valid, unreadable or integrity-failing keys refuse with the existing codes (`INGRESS_UNAUTHENTICATED`, `PERMIT_INVALID`, record and attestation refusals); an unreadable key directory is `KeysUnavailable` (fail closed).

## J-4 Snapshot store

`writ-snapshots-1`, a separate SQLite file owned by `writ-serviced`, using the ledger's identity and ownership pattern (canonical path, one hard link, `.owner` lock under the lifecycle gate, bound at creation to one `LedgerId`; another ledger is refused).

**Model.** A snapshot is `(tenant_id, project_id, snapshot_id)` with a canonical manifest: the `writ-snapshot-manifest/1` digest over `[{path, digest}]` sorted by path (unchanged from M3). `snapshot_id = "snap-" + hex(D-006 digest of {"contract":"writ-snapshot-id/1", tenant_id, project_id, manifest_digest})`, so the same content in the same scope has one id and different scopes never share an id. Tables: `snapshots` (id, tenant, project, manifest digest, file count, total bytes, created_at), `snapshot_files` (snapshot id, path, file digest, size), `blobs` (file digest primary key, bytes, size). No `UPDATE` or `DELETE` statement exists in the store's API; schema triggers refuse updates of these tables.

**Ingest (M5-D17, byte stream):**

1. `snapshot.ingest.begin {tenant/project from the connection binding, limits requested}`: the service opens an ingest session bounded by quotas (per-connection and per-scope concurrent sessions; time limit).
2. `snapshot.ingest.file {path, size}` followed by data frames: the service streams bytes into a temporary blob area of the store, computing SHA-256 as bytes arrive; refuses on exceeding the declared size, the per-file maximum or the total maximum.
3. `snapshot.ingest.commit {claimed_manifest_digest optional}`: inside one transaction the store validates the manifest (every path valid per `valid_snapshot_path`, at most a configured length and depth, no control characters, no duplicate, no path that is a proper prefix directory of another, Unicode normalization collisions refused), computes the manifest digest from the received bytes, compares a claimed digest if given (mismatch refuses; a claimed digest is never authority), inserts blobs (deduplicated by digest), files and the snapshot row, and commits. A crash before commit leaves nothing visible; leftover temporary data is removed at open.
4. The reply is `{snapshot_id, manifest_digest}` computed by the store.

No agent-supplied pathname is ever opened by the service (no working-copy import in M5).

**Bounds (configured, with conservative defaults set at implementation):** maximum file count, file size, total size, path length, path depth, sessions per scope, total store size per scope. Exceeding a bound refuses (`SNAPSHOT_LIMIT`, new code).

**Reads.** `SnapshotContent::content` and `SnapshotStore::check` read inside one read transaction and **re-verify actual bytes on every consumption**: each blob's SHA-256 equals its recorded digest and the recomputed manifest digest equals the snapshot's; any mismatch is an integrity failure (fail closed, operator diagnostic). `check` answers `Matches` only after this verification.

**Immutability and trust.** `immutable()` is not a constructor flag. It returns `true` only for a store opened with a `VerifiedDeployment` proof from `writ-hostsys` (private constructor; produced at service start only when probe P-AGENT-DENY and the ownership and mode checks of the store's directory pass for the running uid and the configured agent uids). A store opened without it answers `false`, so the runner and promotion refuse (`ARTIFACT_INTEGRITY_UNAVAILABLE`). No caller can set it.

**Retention.** *Choice within D-045:* M5 implements **no deletion**; growth is bounded by quotas. A deletion design (references from active approvals, jobs, promotions and workflows; tombstone, re-check of references in the owner process, then delete) is deferred to the retention work of CO-M4X-03 (M9) and must never invalidate an active reference (M5-D5).

**Binding.** The broker's `Trust::snapshots` and the runner's `SnapshotContent` are the same store instance in `writ-serviced` (removes the M3 limitation of two snapshot paths).

## J-5 Containment

**Readiness source.** The sandbox's `guarantees()` come only from capability probes run by the supervisor at start and re-checked cheaply before each job (delegated cgroup present and writable, bubblewrap binary digest unchanged, runtime profile digest unchanged). No kernel-version comparison is used anywhere (M5-D2). Any failing probe sets the affected guarantee to `false`; `RunnerRequirements::production()` then refuses (no uncontained fallback).

**Cgroup.** `writ.service` has `Delegate=yes`; the service process moves itself into `<unit cgroup>/service` (leaf, no-internal-process rule) and creates `<unit cgroup>/jobs/` with `memory`, `pids` and `cpu` enabled in `cgroup.subtree_control`. Per job: `jobs/job-<permit id>` with `memory.max`, `memory.swap.max = 0`, `pids.max`, `cpu.max` written before any job process exists.

**Entry before exec.** The supervisor spawns `writ-cgroup-exec` with the job cgroup path, the pinned bubblewrap path, the profile arguments and the program as separate arguments, a cleared environment and only descriptors 0..2. `writ-cgroup-exec` (safe Rust) verifies the path is beneath the delegated `jobs/` directory by descriptor, writes its own pid to `cgroup.procs`, verifies `/proc/self/cgroup`, then replaces itself with bubblewrap through `std::os::unix::process::CommandExt::exec` (safe). Hence every process of the job, including bubblewrap itself, starts inside the job cgroup before untrusted code runs. *Choice within D-045 ("cgroup-v2/systemd job unit"):* a delegated subtree managed by the supervisor rather than one transient systemd unit per job through D-Bus, which would add a D-Bus client dependency and give the agent-facing service a systemd management channel; the lead may choose transient units instead.

**bubblewrap profile `writ-bwrap/1` (pinned, its argument list digested into `environment_digest`):** `--unshare-all` (user, pid, net, ipc, uts, cgroup namespaces), `--new-session`, `--die-with-parent` (convenience only; never relied on for tree cleanup), `--cap-drop ALL`, `--clearenv` plus exactly the job environment, read-only binds of the pinned runtime profile only (for example `/usr` subset and the interpreter of the registered command), `--proc /proc`, `--dev /dev` (minimal), `--tmpfs /tmp`, read-write bind of the job work directory only, `--chdir` into it. Never bound: `/var/lib/writ` (except the job's own work directory), `/run/writ`, `/etc/writ`, `/sys/fs/cgroup`, the host `/proc`, any socket. Landlock is optional defence in depth (M5-D2), not part of the required boundary.

**Pinned runtime profiles.** A runtime profile `writ-runtime/<name>` lists exact package versions and the digest of a manifest of every file under its read-only binds; registered commands name a profile; the command digest plus the profile digest plus the bubblewrap profile digest form the sandbox `environment_digest`, compared with the registry entry. A changed runtime (package update) changes the digest and refuses until an operator re-registers (M5-D4: interpreters included).

**Termination and verification.** At the deadline, on output overflow, on cancellation and at shutdown: write `1` to `cgroup.kill`, then wait until `cgroup.events` reports `populated 0` (bounded wait; if it does not empty, the job is recorded failed, the cgroup is kept and quarantined, readiness drops to not ready and an operator diagnostic is raised), then remove the cgroup. Process-group signalling of the direct child is secondary cleanup only. Membership in the cgroup is inherited by every descendant regardless of `setsid` or double forks, which is what the tests verify.

**Supervisor or broker failure.** If `writ-serviced` dies, the job cgroups remain under the unit; systemd's stop or restart of the unit kills the whole unit cgroup; at start the supervisor kills and removes every leftover `jobs/job-*` cgroup **before** accepting work, sweeps leftover `run-*` work directories, and leaves the corresponding runs `started` (uncertain; never re-run). Operator path for `started` runs: J-10 Q-3 (recorded abandonment keeps the receipt uncertain; no client-asserted reconciliation, D-035 item 3, D-036).

**Refusal on missing guarantees.** Missing bubblewrap, unexpected bubblewrap digest, no delegation, a missing controller, no `cgroup.kill`, a probe failure or an unpinned runtime makes `guarantees()` report the missing guarantee as `false` (or `SandboxUnavailable`), so the runner refuses with the existing codes.

## J-6 Host identity (M5-D7)

A sealed request built by the service-side adapter carries two clearly separated groups:

- **Authenticated bindings** (authority may depend on them): `principal.verified_by` = adapter id; `os_principal` = `uid:<n>` from peer credentials; `tenant_id` and `project_id` from the configured binding of that uid and socket. Nothing in the host event can change them.
- **Host-asserted labels** (`host_asserted: {agent_id, host_session_id, parent_agent_id, tool_name, host_version}`): recorded, fingerprinted, displayed with the label `host-asserted` on approval and signoff pages, and **never authority**, even though the adapter MACs them (the seal proves which adapter sealed the request, not that the labels are true).

Rules (until M5b-06 provides a trusted session binding): grants are bound to authenticated bindings, never to a host-asserted label; an operation whose authorization depends on agent identity or delegation (agent-specific grant consumption, separation of duties over chain members) is refused (`IDENTITY_UNVERIFIED`, new code) in the governed profile or kept advisory and labelled; `delegation_chain` stays `unavailable`. The hook client and MCP shim hold no key; only the service seals.

## J-7 `fs.write` adapter recovery contract (for independent review, M5a-08)

### J-7.1 Scope and authorization

- Action kind `fs.write`; resource `file:<tenant>/<project>/<root id>/<relative path>`; attributes bound by the action digest: `snapshot_id`, `manifest_digest`, `source_path` (a file of that snapshot), `content_digest` (that file's digest), `mode` (`create_only` or `replace_if`), `expected_prior_digest` (required for `replace_if`, absent for `create_only`).
- Target roots are configured per (tenant, project): root id, absolute path, published file mode. The root and everything below it are owned by the service uid and never group- or other-writable (E1).
- M5 writes regular files into **existing** directories only; a missing parent, a directory or a non-regular file at the target, or a target with `nlink > 1`, fails the operation (no directory creation in M5: *choice within D-045* to avoid a second, directory-level recovery contract).

### J-7.2 Trusted descriptors and the mount rule

- At adapter open: `root_fd = openat2(AT_FDCWD, root path, O_DIRECTORY | O_RDONLY | O_CLOEXEC, RESOLVE_NO_SYMLINKS | RESOLVE_NO_MAGICLINKS)`; `fstat` and `statx` record `(st_dev, st_ino, mnt_id)`; require owner service uid, no group or other write, no ACL (`system.posix_acl_access` absent), filesystem type in the allow-list (ext4 or xfs, recorded by probe P-FS-TYPE; no network, FUSE or overlay filesystems). The staging directory `<root>/.writ-staging` (0700) is opened the same way and must have the same `st_dev` and `mnt_id` as the root.
- Every operation: `parent_fd = openat2(root_fd, parent, O_DIRECTORY | O_RDONLY | O_CLOEXEC, RESOLVE_BENEATH | RESOLVE_NO_SYMLINKS | RESOLVE_NO_MAGICLINKS | RESOLVE_NO_XDEV)`, then `statx` of `parent_fd` and of the target (`AT_SYMLINK_NOFOLLOW`) must report the root's `mnt_id` and `st_dev`, owner and mode rules. **Mount crossings are forbidden and enforced explicitly** by `RESOLVE_NO_XDEV` (refuses any mount point, including bind mounts, during resolution) plus the `mnt_id` equality check (detects a mount placed over the parent itself); beneath and no-symlink resolution alone are not relied on (M5-D8).
- All later steps use `parent_fd`, `staging_fd` and names relative to them; the adapter never reopens a path string.

### J-7.3 Exclusive-writer assumptions

| ID | Assumption | Enforced by | Detected by |
|---|---|---|---|
| E1 | Only the adapter writes under the root and staging directory | OS ownership and modes (service uid; agent uid has no write permission); checked at open and per operation on the descriptors | drift check (J-7.6) |
| E2 | Only one adapter instance per root at a time | the journal's ownership lock (`.owner`, as every owned store) and the journal bound to one ledger and one root identity `(st_dev, st_ino, mnt_id)` | journal open refuses a different root identity |
| E3 | Root (uid 0) and other service-user processes do not write the root | not enforceable | drift check; a detected change taints the target |
| E4 | The filesystem gives atomic `rename`, durable `fsync` of files and directories, stable inode numbers while a file exists | filesystem type allow-list and probes | not detectable in general; outside the allow-list the adapter refuses to open |
| E5 | No hard links into the root from elsewhere | `nlink == 1` required for targets and staged files | per-operation `statx` |

### J-7.4 Journal `writ-fseffects-1` and operation identity

Owned SQLite file per target root, bound to one ledger and one root identity. Tables (proposed):

- `ops`: `op_id` (`fsop-<permit id>`, primary key), `permit_id` (unique: consumption), tenant, idempotency key, request fingerprint, `target` (relative path), mode, `expected_prior` (`{digest, dev, ino}` for `replace_if`; `absent` for `create_only`), `content_digest`, `snapshot_id`, `staging_name` (`.writ-staging/<op_id>`, deterministic), `state` (`intent`, `staged`, `resolved`), `staged_identity` (`{dev, ino, size, digest}`, set at `staged`), timestamps.
- `outcomes`: `op_id`, `operation_evidence` (`completed`, `not_completed`, `unknown`), `desired_state` (`verified`, `differs`, `unknown`), `result` (`applied`, `failed`, `unknown`), `target_identity_after` (`{dev, ino, size, digest}` or absent), reason, recorded_at.
- `targets`: `target` (primary key), `blocked_by` (`op_id` or null), `tainted` (bool, reason), `last_identity` (`{dev, ino, size, digest}` after the last resolved operation, or `absent`).
- Partial unique index: at most one op with `state != 'resolved'` per target.

**Operation-specific evidence.** The staged file is created by this operation (exclusive create under a name derived from `op_id`) and its inode identity `(st_dev, st_ino)` is recorded durably **before** the rename. After the rename the target is that inode. While the operation is unresolved the staged inode is never unlinked by the adapter, so its inode number cannot be reused, and the target is blocked, so no other operation of the adapter touches it. Hence "target inode equals staged inode" is evidence that **this** rename happened, independent of content; "staged inode still at the staging name and target unchanged" is evidence that it did not. Content equality alone is never used as evidence (M5-D8).

### J-7.5 Normal path and crash boundaries

Inside `EffectAdapter::apply` (amended contract: consumption and intent are durable before the effect; outcome evidence is durable after it; no single transaction):

| Step | Action | Crash or failure here (boundary) |
|---|---|---|
| T-intent | journal transaction: begin (failure: `LEDGER_UNAVAILABLE`, no permit check disclosed, D-025 B); broker `verify`; `PERMIT_REPLAYED` if consumed; target checks on descriptors (parent exists, target state equals the mode's expectation: absent for `create_only`; for `replace_if` a regular file, `nlink == 1`, digest equal to `expected_prior_digest`, its `{dev, ino}` recorded); `targets.blocked_by` null and not tainted (otherwise `TARGET_UNRESOLVED` or `TARGET_TAINTED`, new codes, permit consumed, op recorded `failed` with operation evidence `not_completed`, since nothing was attempted); insert `ops` (`intent`), set `blocked_by`; commit | **B0** before commit: nothing durable, permit unconsumed. **B1** commit outcome unknown (`TxFailure` with unknown commit): the op may or may not exist |
| Stage | read the source bytes from the snapshot store (digest re-verified, J-4); `openat(staging_fd, staging_name, O_CREAT | O_EXCL | O_WRONLY | O_NOFOLLOW | O_CLOEXEC, mode)`; write; `fsync`; re-read through the descriptor and compare the digest with `content_digest`; `fstat` for `{dev, ino, size}`; `fsync(staging_fd)` | **B2** after create, partial content. **B3** written, before `fsync`. **B4** verified, before T-staged. Failures (`ENOSPC`, `EIO`, `EACCES`, digest mismatch): remove the staged file, then T-outcome `failed` (`not_completed`) |
| T-staged | journal transaction: `ops.state = staged`, `staged_identity`; commit | **B5** commit outcome unknown: the adapter does **not** continue to the rename; the op is left for recovery |
| Pre-rename recheck | on descriptors: target state equals the intent's recorded prior identity (`absent`, or `{dev, ino}` and digest equal) | **B6** before recheck. A mismatch means E1 or E3 was violated: remove nothing, taint the target, T-outcome `failed` with `not_completed` (the rename was not attempted) and reason `drift` |
| Rename | `create_only`: `renameat2(staging_fd, staging_name, parent_fd, name, RENAME_NOREPLACE)`; `replace_if`: `renameat(...)` (replaces). `EEXIST` for `create_only`: not applied (atomic refusal), taint the target, T-outcome `failed` (`not_completed`, reason `drift`) | **B7** after recheck, before rename. **B8** rename returned, before directory `fsync` |
| Durability | `fsync(parent_fd)` and `fsync(staging_fd)` | **B9** after `fsync`, before verification. `EIO` on `fsync`: leave for recovery (outcome unknown until recovery re-observes durable state) |
| Verify | `statx` of the target: `{dev, ino}` equal to `staged_identity`; re-read digest equal to `content_digest` | **B10** before T-outcome |
| T-outcome | journal transaction: `outcomes` (`completed`, `verified`, `applied`, `target_identity_after`), `targets.last_identity`, `blocked_by = null`, `ops.state = resolved`; commit; return `Applied` | **B11** commit outcome unknown: `TxFailure`; the receipt stays `dispatched`. **B12** after commit, before the broker's T-observe (existing broker semantics) |

The permit is consumed at T-intent; whatever happens later, the op is never re-run and the receipt is never redispatched (D-035 item 3).

### J-7.6 Recovery and reconciliation decision table

Recovery runs at journal open (before any new operation is accepted), on operator request, and before broker reconciliation reads evidence. For every op with `state != resolved` it first makes the observed state durable (`fsync` of the parent directory, the staging directory and the target if present) and then observes, through the trusted descriptors: **T** the target (`absent`, `inode = staged`, `inode = prior`, `other`) and **S** the staging name (`absent`, `inode = staged`, `present other`).

| # | Journal | Observation | Operation evidence | Desired state | Result recorded | Target after |
|---|---|---|---|---|---|---|
| R1 | `intent` (no staged identity) | T equals the recorded prior identity; S absent or present (any content) | `not_completed`: by protocol the rename happens only after T-staged is durably committed | `differs` (or n/a) | `failed`; remove S if present | unblocked |
| R2 | `intent` | T differs from the recorded prior identity | `not_completed` for this op (no rename without `staged`), but E1 or E3 violated | `unknown` | `failed`, reason `drift` | **tainted**, blocked |
| R3 | `staged` | T `inode = staged`, S absent, content digest equal to `content_digest`, `nlink == 1` | `completed` | `verified` | `applied` | unblocked, `last_identity` = staged |
| R4 | `staged` | T `inode = staged`, S absent, content digest differs | `completed` (the rename happened) but the content was changed afterwards | `differs` | `unknown` (evidence no longer trustworthy: E1 or E3 violated) | **tainted**, blocked |
| R5 | `staged` | S `inode = staged`, T equals the recorded prior identity | `not_completed` | `differs` | `failed`; remove S | unblocked |
| R6 | `staged` | S `inode = staged`, T differs from prior and is not the staged inode | `not_completed` for this op; external change | `unknown` | `failed`, reason `drift`; keep S for the operator | **tainted**, blocked |
| R7 | `staged` | S absent and T not the staged inode | cannot tell whether this rename happened and was then replaced, or the staged file was removed | `unknown` | `unknown` | **tainted**, blocked |
| R8 | `staged` | S present with another inode, or T `inode = staged` with `nlink > 1`, or both S and T are the staged inode | inconsistent | `unknown` | `unknown` | **tainted**, blocked |
| R9 | any | a descriptor, `statx`, read or `fsync` fails (`EIO`, `ENOSPC`, permission), or the root identity or mount id differs | not observable | `unknown` | nothing recorded; recovery retried later | blocked |
| R10 | journal holds no op for the permit | not applicable | see J-10 Q-2 (absence after permit expiry) | `unknown` | nothing recorded (today's rule: absence is not evidence, D-035 item 3) | n/a |
| R11 | `resolved` | T differs from `last_identity` at the next intent for that target | n/a (that op is resolved) | `differs` | the new op refuses `TARGET_TAINTED`; the target is tainted | **tainted** |

Rules: **ambiguous evidence stays `unknown`**; `unknown` is never converted to `applied` or `failed` by the adapter; a tainted or unknown target stays blocked against every later operation until an operator disposition (J-10 Q-3), which never changes a recorded result and never makes a receipt `observed_success` or `observed_failure`. `EffectAdapter::evidence` returns evidence only for ops with result `applied` or `failed`; for `unknown` it returns none, so the receipt stays uncertain (`dispatched` or `unknown_reconcile`).

**Why R1 and R5 may record `failed`.** The protocol never renames before `staged` is durable (R1) and the staged inode still at the staging name after durable observation means it was not renamed (R5); with E1, E2 and E4 holding, nothing else could have performed this operation's rename. These two rows depend on the exclusive-writer assumptions and are the rows the independent review must examine most closely.

**Repeated identical content and later writes.** Two operations writing the same bytes to one target have different staged inodes, so their evidence never coincides; `replace_if` with `expected_prior_digest == content_digest` still produces a new inode and is evidenced the same way. A later operation on a resolved target compares the current target identity with `last_identity` (R11) before its own intent, so an external rewrite in between is detected even when the bytes are equal (inode differs) and is never silently overwritten.

### J-7.7 Tests required (M5a-09, written first)

SIGKILL of the real adapter process at each boundary B0..B12 followed by restart and recovery, asserting the decision-table row reached; ambiguous commits at T-intent, T-staged and T-outcome (injected commit failure with unknown outcome, then both possible durable states); every row R1..R11 constructed explicitly; repeated identical content (two ops, same bytes; `replace_if` to the same bytes); later write after an applied, a failed and an unknown op; path races (symlink swap of a parent, directory rename, bind mount over a parent, hard link to the target, target replaced between recheck and rename by a test process with the service uid); `ENOSPC` (small loop or tmpfs-backed test filesystem where permitted, otherwise a quota) and permission failures at each filesystem step; replayed permit; journal bound to another ledger or root refused; second adapter instance refused; CO-PROP-01 properties (M5a-10).

## J-8 Graceful shutdown

For `writ-serviced` and `writ-approval-server` (CO-M4X-05; Tokio `signal`, M5-D16):

1. **SIGTERM or SIGINT:** enter `stopping`; stop accepting connections on every listener (close the listening sockets); new requests on open connections answer a generic `stopping` refusal (`SERVICE_STOPPING`, new code).
2. **Drain** for a bounded period (configured, default 10 s, maximum 30 s): requests in progress finish normally. Approval ceremonies not finished in time are left as they are (challenges stay issued and expire; never burned by shutdown). Snapshot ingest sessions not committed are abandoned (nothing visible; temporary data removed at next open).
3. **Effects:** an `fs.write` operation in progress runs to its next journal boundary within the drain period; whatever its state, it is safe for recovery (J-7.6). No new effect is dispatched after step 1.
4. **Runner jobs:** after the drain period, running jobs are terminated through `cgroup.kill` and verified empty (J-5); the run is finished `failed` with diagnostic `terminated_shutdown` when T-finish succeeds (nothing usable was produced), otherwise it stays `started` (uncertain). No new job starts after step 1.
5. **Release ownership:** drop the broker, then the effect, fs journal and snapshot stores, then the ledger (each `OwnerLock` unlocks before close under the lifecycle gate); exit 0.
6. **Second signal or hard deadline** (drain period plus 5 s): exit immediately with a non-zero status; the OS releases locks; every uncertain outcome is preserved for recovery. systemd's `TimeoutStopSec` is set above the hard deadline.

Tests: SIGTERM during a ceremony, an ingest, each `fs.write` boundary and a running job; lock released (a new owner opens at once); second signal; hard deadline; no receipt or run changes from uncertain to a result because of shutdown, except the job case in step 4.

## J-9 Reference environment and capability probes

**Reference image (choice within D-045 M5-D2):** **Debian 13 "trixie", amd64, official generic cloud image**, run as the owner-designated disposable VM with snapshot rollback. Reasons: cgroup v2 unified hierarchy by default, unprivileged user namespaces available by default (bubblewrap is not setuid on Debian), no distribution policy layer restricting unprivileged namespaces that bubblewrap would need special profiles for, a long-term-supported kernel, ext4 root filesystem. Readiness is established only by the probes below, never by these facts.

**Versions recorded in every readiness report (`writ-readiness/1`):** image name and SHA-256; `uname -r` and the kernel package version; `systemd --version`; `bwrap --version` and the SHA-256 of the bubblewrap binary; util-linux, coreutils, glibc and SQLite library versions where linked; Rust toolchain from `rust-toolchain.toml`; filesystem type and mount options of `/var/lib/writ`; cgroup2 mount and the controllers available to the delegated subtree; sysctls `kernel.unprivileged_userns_clone` (if present), `user.max_user_namespaces`, `kernel.yama.ptrace_scope`, `fs.protected_symlinks`, `fs.protected_hardlinks`; AppArmor or SELinux state; Landlock ABI (optional, informational); the runtime profile manifests and their digests; the Claude Code version for M5b.

**Probes (each an executed test with a pass or fail result and a negative control):**

| Probe | Checks | Negative control |
|---|---|---|
| P-USERNS | the service user can run the pinned bubblewrap with every namespace unshared | namespace creation disabled by sysctl in the VM |
| P-FS | inside a job: write outside the work directory, read of `/var/lib/writ`, `/etc/writ` and keys, and access to `/run/writ` sockets all fail | profile with an extra bind must fail the probe |
| P-NET | inside a job: no interface other than loopback in a new namespace; connect to the host loopback approval port and to an external address fail | job without `--unshare-net` fails the probe |
| P-PID | inside a job: host processes invisible; signals to service processes impossible | |
| P-CGROUP | delegated subtree writable by the service; `memory`, `pids`, `cpu` enabled; `cgroup.kill` present; job cgroup not writable from inside the job | cgroup v1 or no delegation |
| P-KILL | a job that `setsid`s and double-forks leaves no process after `cgroup.kill`; `populated 0` observed | process-group kill alone leaves a survivor (shows the probe discriminates) |
| P-LIMITS | memory limit triggers OOM kill recorded in `memory.events`; `pids.max` stops a fork bomb; CPU quota observed | |
| P-FD | inside a job only descriptors 0..2 are open | |
| P-OPENAT2 | `RESOLVE_BENEATH`, `RESOLVE_NO_SYMLINKS`, `RESOLVE_NO_XDEV` honoured on the target filesystem; `statx` reports a mount id | bind mount inside a test root must be refused |
| P-RENAME | `RENAME_NOREPLACE` supported on the target filesystem | |
| P-FS-TYPE | target root and stores on an allow-listed local filesystem | tmpfs or overlay target refused |
| P-PEERCRED | peer credentials of a connecting process match its uid | |
| P-AGENT-DENY | as the agent uid: read of keys, ledger, stores; write of snapshot store, target roots, work root; connect to `admin.sock`; `ptrace` of the service; read of `/proc/<service pid>/environ` all refused | a deliberately mis-moded test key file must be reported |
| P-NO-SECRET | no key material in service command lines, environments, logs or readiness reports (canary) | planted canary detected |

The report is produced by `writ-probe` (operator procedure) and by `writ-serviced` at start; `RunnerRequirements::production()` is satisfied only when every containment probe passes on the running host.

## J-10 Open questions

**Genuinely new trust-boundary questions (for the lead, and the owner where marked):**

- **Q-1 (owner):** Runner jobs and the published target roots are separated from the agent by OS user; the **human's** ability to approve depends on the loopback approval page, which any local process of any uid can reach. D-040 O-12 accepted loopback for the local profile; confirm that nothing changes now that trusted state is no longer readable by the human's user (the approval page becomes the only human channel into the service). Recommendation: unchanged (WebAuthn binds the human; the socket is not authentication).
- **Q-2 (lead, then independent review):** may the `fs.write` adapter treat **absence of an intent row for a permit that has expired** as authoritative `not applied` evidence (J-7.6 R10)? Absence is not evidence under D-035 item 3 today; with an owned journal, a permit that can never again be presented, and E1/E2, absence would be sound. Recommendation: keep today's rule in M5 (receipt stays uncertain; operator path), revisit only with the review's agreement.
- **Q-3 (owner):** **who records operator dispositions** (abandoning a `started` run, releasing a tainted or unknown `fs.write` target, revoking a key): an authenticated human `Operator` (or `Revoker`) through a new M4-style ceremony operation on the approval page, or a local operator procedure on `admin.sock` admitted by OS uid. Recommendation: an authenticated human ceremony for every disposition that unblocks or abandons, because these change what the system will do next; local procedure only for key provisioning on the disposable host. Dispositions never change a recorded result or reconcile a receipt in either case.

**Choices made within D-045 (recorded for the lead's review, not owner questions):** one owner process composing all services (J-1); Tokio peer credentials before `rustix` `net` (J-2); ledger-recorded key revocation and a permit `key_id` contract amendment (J-3); snapshot ids derived from scope and manifest, no deletion in M5 (J-4); delegated cgroup subtree with a safe exec helper instead of per-job transient units (J-5); no directory creation by `fs.write` in M5, inode-identity evidence, `ext4` and `xfs` as the filesystem allow-list (J-7); drain period defaults (J-8); Debian 13 as the reference image (J-9); new refusal codes `SNAPSHOT_LIMIT`, `TARGET_UNRESOLVED`, `TARGET_TAINTED`, `IDENTITY_UNVERIFIED`, `SERVICE_STOPPING` (names proposed; external mapping generic).
