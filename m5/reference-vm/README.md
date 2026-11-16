# M5 reference VM: setup, trial and teardown (M5a-03, M5a-07)

**Status: PREPARED, NOT RUN.** These scripts exist for the owner-designated disposable VM of D-045 M5-D12. They were never executed on a workstation; the live run is **blocked externally** until the VM exists, and is recorded as blocked, not passed. Interfaces: [M5-INTERFACES](../../docs/implementation/M5-INTERFACES.md) J-1 (identities, layout; **BLOCKED** on owner decisions C-1 and H-2) and J-9 revision 2 (reference image, probes); decision [D-045](../../docs/implementation/DECISION-LOG.md) (M5-D2, M5-D3, M5-D12).

**Job identity (D-046 H-2, M5a-07).** The job user (`--job-user`, default `writ-job`) is always created, with its own primary group and no supplementary groups; jobs run only as that user through the root job launcher (`launcher/`, M5-INTERFACES J-11). **Agent user.** The agent user (`--agent-user`, default `writ-agent`) is a parameter. Probes that test the agent identity (P-AGENT-USERNS, P-AGENT-DENY, P-HARDLINK) report `depends_on: "J-1 C-1 (agent uid provisional)"`; their results are valid for the user tested, and must be repeated if C-1 changes who the agent is (for example a dedicated agent uid, or the human's desktop user with `--desktop-uid` display and session-bus checks under C-1 option (a)).

## Safety

- Every script sources `common.sh` and refuses (exit 3, before any change) unless: the marker `/etc/writ-disposable-vm` exists, is a root-owned regular file not writable by group or others, starts with the line `writ-disposable-vm/1` and contains `name=<vm name>`; `--designated-vm <vm name>` matches it; the user is root; `systemd-detect-virt --vm` reports a VM; `/etc/os-release` is Ubuntu 24.04. No script creates the marker.
- Nothing here installs a hypervisor, provisions a cloud VM, uses personal credentials, or changes the workstation (M5-D3, M5-D12). Take a VM snapshot before step 3; rolling back to it is the authoritative reset.
- `kernel.apparmor_restrict_unprivileged_userns = 1` **stays set** (J-9). `setup.sh` stops (exit 4) if it is not 1, if cgroup v2 is missing, or if AppArmor is disabled; it never changes kernel policy. The `userns` grant goes only to the private bubblewrap copy through an AppArmor profile attached by path.

## Reference image

Ubuntu 24.04 LTS, amd64, official cloud image (`ubuntu-24.04-server-cloudimg-amd64.img` from `cloud-images.ubuntu.com/releases/24.04/`), with the published `SHA256SUMS` checked by the owner and the SHA-256 recorded with `--image-sha256` (J-9). Readiness is never inferred from this choice; only the probes decide.

## Commands

On a build machine (the workstation is fine for building; nothing runs there):

```sh
source m1/tools/env.sh
cargo build --release --locked -p writ-hostsys        # target/release/writ-probe
cargo build --locked --profile launcher -p writ-containment   # target/launcher/writ-job-launcher, writ-cgroup-exec (panic = abort)
# test-only builds for the P-JOB-IDENTITY controls (never installed by setup.sh):
CARGO_TARGET_DIR=target/faults cargo build --locked --profile launcher -p writ-containment --features test-faults
sha256sum target/release/writ-probe target/launcher/writ-job-launcher target/launcher/writ-cgroup-exec   # record; compare in the VM
```

Inside the VM, after copying `m5/reference-vm/` and `writ-probe` in (for example to `/root/writ/`):

1. Snapshot the fresh VM (`fresh`).
2. As root, create the marker by hand (choose the VM name):
   ```sh
   printf 'writ-disposable-vm/1\nname=writ-ref-1\n' > /etc/writ-disposable-vm && chmod 0644 /etc/writ-disposable-vm
   ```
3. Setup:
   ```sh
   /root/writ/reference-vm/setup.sh --designated-vm writ-ref-1 --probe-binary /root/writ/writ-probe \
     --image-name ubuntu-24.04-server-cloudimg-amd64.img --image-sha256 <64 hex> \
     --launcher-binary /root/writ/writ-job-launcher --exec-binary /root/writ/writ-cgroup-exec [--agent-user writ-agent] [--job-user writ-job]
   ```
4. Trial:
   ```sh
   /root/writ/reference-vm/trial.sh --designated-vm writ-ref-1 \
     --test-launcher /root/writ/faults/writ-job-launcher --test-exec /root/writ/faults/writ-cgroup-exec
   ```
5. Copy `/root/writ-evidence/` off the VM.
6. Teardown (then roll back to `fresh` anyway):
   ```sh
   /root/writ/reference-vm/teardown.sh --designated-vm writ-ref-1
   ```

## What each script does

| Script | Does |
|---|---|
| `setup.sh` | installs `bubblewrap` (archive, not setuid); creates groups `writ` and `writ-agents`, the system user `writ` (no login shell, home `/var/lib/writ`), the agent user (in `writ-agents` only; refuses if it is in `sudo`, `adm`, `systemd-journal`, `writ`, `docker`, `lxd`, `disk` and similar) and the optional job user; creates the J-1 layout: `/var/lib/writ` and `ledger/`, `stores/`, `keys/`, `run/` 0700 `writ:writ`, `/srv/writ` 0755 `root:root`, `/srv/writ/targets` 0755 `writ:writ`, `/etc/writ` 0750 `root:writ`, `/run/writ` 0750 `writ:writ-agents` and `/run/writ/state` 0700 `writ:writ` (tmpfiles.d), `/usr/local/libexec/writ` 0755 `root:root`; copies `/usr/bin/bwrap` to `/usr/local/libexec/writ/bwrap` (`root:writ` 0750) and loads `/etc/apparmor.d/writ-bwrap` granting `userns` to that path only, recording both digests in `/etc/writ`; writes `/etc/writ/filesystems` (UUID and `dumpe2fs -h` features of the filesystems of `/var/lib/writ` and `/srv/writ/targets`), `/etc/writ/reference-image` and a random probe canary (not a key); installs `writ-probe`; records versions before and after. Every change is appended to `/var/lib/writ-reference-vm.state`. |
| `trial.sh` | starts a placeholder service as `writ` (`LimitCORE=0`, holding `/run/writ/admin.sock` 0600; a stand-in for `writ-serviced` until M5a-04) and exports its journal; prepares a `writ`-owned hard-link source in `/var/tmp` and a bind mount inside a test root; runs `writ-probe agent-deny` as the agent user; runs `writ-probe` as `writ` in its own delegated unit (`systemd-run -p Delegate=yes`, `--cgroup-delegated`, `--xdev-root`); then the VM-only negative controls below; every change is restored by an exit trap. |
| `teardown.sh` | stops leftover units; unloads the AppArmor profile; removes, newest first, exactly what the state file lists (purges `bubblewrap` only if setup installed it); verifies nothing is left. |

VM-only negative controls in `trial.sh` (each probe's runtime control runs inside every `writ-probe` report):

| Control | Change | Expected |
|---|---|---|
| private copy without the AppArmor profile | `--bwrap` pointing at an unprofiled `root:writ` 0750 copy | P-USERNS `missing` |
| test uid granted the profile | agent-side attempts as a temporary user in group `writ` (may execute the private copy) | P-AGENT-USERNS `missing` |
| no delegation | probe unit without `Delegate=yes` | P-CGROUP `missing` |
| unsupported filesystem | state, scratch and target roots on a tmpfs | P-FS-TYPE and P-RENAME `missing` |
| hard links unprotected | `sysctl fs.protected_hardlinks=0` | P-HARDLINK `missing` |
| planted default ACL | `setfacl -d -m u::rwx /var/lib/writ/stores` (when `setfacl` exists) | P-ACL `missing` |
| loosely moded canary key | a `writ`-owned 0644 copy of the canary passed with `--extra-read` | P-AGENT-DENY `missing` |
| planted canary | canary in the placeholder service's environment | P-NO-SECRET `missing` |

## Evidence to record (in TEST-EVIDENCE, with the exact SHA built)

From `/root/writ-evidence/<time>-setup/`: `versions-before.txt`, `versions-after.txt` (kernel, kernel package, systemd, bubblewrap version, system and private binary digests, AppArmor profile digest and load state, util-linux, coreutils, glibc, apparmor package, cgroup controllers, the J-9 sysctls, LSMs, filesystem source, type, UUID and options of `/var/lib` and `/srv`), `digests.txt`, `apparmor-writ-bwrap.txt`, `filesystems.txt`, `layout.txt`, `identities.txt`, `state.txt`.

From `/root/writ-evidence/<time>-trial/`: `baseline.json` (the `writ-readiness/1` report), `agent-deny.json`, each `*-control.json` with its `.exit` and `.stderr`, `versions.txt`, `digests.txt` and `summary.txt` (one PASS or FAIL line per expectation). Record: every probe's status in the baseline; every runtime control `failed_as_expected`; every VM control's targeted probe `missing`; `governed_ready`; the image name and SHA-256; the VM product and version; the agent user name and that it is provisional (C-1).

From `/root/writ-evidence/<time>-teardown/`: `after.txt` (no `LEFT` line).

Never record key material, the canary value, or credentials in evidence or chat.

## Service (M5a-04; PREPARED, NOT RUN)

`service-install.sh` (after `setup.sh` with `--job-user writ-job`, and a disposable test human user for `--desktop-user`) installs `writ-serviced` (`root:root` 0755), `/etc/writ/service.json` (`root:writ` 0640, no secret), provisions `ingress/vm-1` and `permit/vm-1` as `writ` (OS random source, temporary file, `fsync`, rename, directory `fsync`; material never printed), runs `writ-serviced --init` as `writ` and installs `writ.service` with the J-1 hardening (not started). Build it with `cargo build --release --locked -p writ-service` (`target/release/writ-serviced`).

Then, inside the VM: start it with `systemctl start writ.service`; `journalctl -u writ.service` must show `serving (unverified)` (no deployment plan configured yet: the snapshot store is not immutable and governed work refuses). To bind a deployment proof: add a `deployment` member with the fact plan to the configuration, run `runuser -u writ -- writ-serviced --print-static-facts --config /etc/writ/service.json`, merge the printed object into the `writ-probe` report as `static_facts`, record the report's D-006 digest as `deployment.report_digest`, restart. VM checks to record: the agent uid cannot connect `admin.sock` or read `/var/lib/writ` (P-AGENT-DENY); `keys.revoke` as root over `admin.sock` records `uid:0`; a second `writ-serviced` or `writ-approval-server` on the same ledger exits with `OWNED_ELSEWHERE`.

Recorded limitation: until socket activation (J-8, M5a-11) the service binds `host.sock` itself with mode 0700, so the agent cannot connect (fails closed). `writ.service` adds `/run/writ` to `ReadWritePaths` for that reason; it goes away with the socket units. `teardown.sh` stops and disables `writ.service` and removes every file `service-install.sh` recorded.

## Job launcher (M5a-07; D-046 H-2; M5-INTERFACES J-11)

Files under `launcher/`, installed **only** by `setup.sh` inside the designated VM:

| File | Installed as | Purpose |
|---|---|---|
| `writ-job-launcher.socket` | `/etc/systemd/system/` | `launch.sock` (`root:writ` 0660), `Accept=yes`, `MaxConnections=1`, explicit trigger and poll limits (J-11.2, L3) |
| `writ-job-launcher@.service` | `/etc/systemd/system/` | root instance per connection, `StandardInput=socket` (see the deviation note in `containment/src/launcher.rs`), `CapabilityBoundingSet=CAP_SETUID CAP_SETGID CAP_CHOWN`, allow-list filesystem view, syscall filter, `Delegate=memory pids cpu`, `KillSignal=SIGKILL` |
| `writ-jobs.slice` | `/etc/systemd/system/` | aggregate ceilings; child of `writ.slice` (N6) |
| `writ-launcher.tmpfiles.conf` | `/etc/tmpfiles.d/writ-launcher.conf` | `/run/writ-launcher` `root:writ` 0750 (L6) |
| `apparmor-writ-bwrap` | `/etc/apparmor.d/writ-bwrap` | profiles `writ-bwrap` (private copy, `userns`) and `writ-job-child` (children: no `userns`, no capabilities) (J-11.5a) |

`setup.sh` also installs both binaries root-owned 0755 after checking `--build-info` (`panic_abort=true`, `test_faults=false`), makes the private bubblewrap `root:writ-job` 0750, makes `/var/lib/writ/run` 0750, creates the dedicated jobs filesystem (`/var/lib/writ-jobs.img`, ext4, mounted `loop,nosuid,nodev` at `/var/lib/writ-jobs`, 0711), a pinned runtime profile `busybox` under `/usr/lib/writ-runtime/` (manifest digest from `writ-job-launcher --runtime-manifest`), `/etc/writ-launcher/launcher.json` (`root:writ` 0640), disables apport and sets `kernel.core_pattern = core`, and records the digests in `/etc/writ/launcher.sha256`.

`trial.sh` sources `trial-launcher.sh`, which drives jobs through `launch.sock` with `launch-client.py` (run as the identity under test) and writes the reports `writ-probe --launcher-reports` evaluates for P-JOB-IDENTITY (with the skip-setgroups and gate-EOF test builds), P-JOB-DENY (sandbox and escape view, per host location), P-LAUNCHER-ADMIT, P-JOB-SERIAL and the P-FD status pipe. Anything a step cannot observe is written `not_run`, which fails the probe. Known gap: the P-FD status-pipe negative control (a build that trusts the status pipe after `started`, with a job able to reach the monitor's descriptor) is not prepared, so P-FD stays failed until it is.

**Verify in VM** (J-11.10), each to be recorded in TEST-EVIDENCE with the pinned version and source reference before the launcher is integrated: bubblewrap 0.9.0 `--block-fd` read location, EOF behaviour and descriptor closing; `--json-status-fd` emitting `child-pid` before blocking; `std` `CommandExt::exec` group clearing on Rust 1.99.1; AppArmor stacking, `px`/`ix` and attachment under `no_new_privs`; `TemporaryFileSystem=/run` with socket activation and `StandardInput=socket` passing no other descriptor; `SystemCallFilter` groups against bubblewrap and the runtime profile; `ProtectKernelTunables=yes` leaving the delegated subtree writable; apport and `core_pattern`; the pre-gate process count (`pre_gate_processes`, N5); `TemporaryFileSystem=/dev/shm /dev/mqueue` over `PrivateDevices=yes`; the `/proc/key-users` baseline after the helper's `setuid`; bubblewrap closing 3 and 4 before executing the program; `--remount-ro /` in `writ-bwrap/1`.
