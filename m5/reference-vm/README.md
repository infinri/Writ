# M5 reference VM: setup, trial and teardown (M5a-03, M5a-07)

**Status: PREPARED, NOT RUN.** M5a live verification is blocked until the owner designates a disposable Ubuntu 24.04 guest. The separate isolated containment CI runner is also required. See [local provisioning](PROVISIONING.md) for the image pin and VM lifecycle, and the [M5a gate review](../../docs/implementation/M5A-GATE-REVIEW.md) for the complete verification checklist.

D-046 requires a dedicated agent UID (default `writ-agent`) separate from `writ`, the human desktop user and the job user. The agent is in only its primary group and `writ-agents`. The required job account (default `writ-job`) has its own primary group and no supplementary groups. The constrained root launcher starts jobs under that account. Current probe JSON still contains the legacy `depends_on: "J-1 C-1 (agent uid provisional)"` annotation. Record the UID actually tested and keep the independent P-AGENT-DESKTOP verification open until it has observed a live disposable desktop session. Shared-user installations remain advisory.

## Safety

- Every script sources `common.sh` and refuses (exit 3, before any change) unless: the marker `/etc/writ-disposable-vm` exists, is a root-owned regular file not writable by group or others, is not a symbolic link, starts with the line `writ-disposable-vm/1` and contains `name=<vm name>`; `--designated-vm <vm name>` matches it; the user is root; `systemd-detect-virt --vm` reports a VM; `/etc/os-release` is Ubuntu 24.04. No script creates the marker.
- Nothing here installs a hypervisor, provisions a cloud VM, uses personal credentials, or changes the workstation (M5-D3, M5-D12). Take an unmarked VM snapshot before owner designation; rolling back to it is the authoritative reset.
- `kernel.apparmor_restrict_unprivileged_userns = 1` **stays set** (J-9). `setup.sh` stops (exit 4) if it is not 1, if cgroup v2 is missing, or if AppArmor is disabled. It leaves the user namespace restriction enabled. The `userns` grant goes only to the private bubblewrap copy through an AppArmor profile attached by path.

## What still needs verification

| Prerequisite | Gate it blocks |
|---|---|
| Working VM tooling and a guest the owner has designated | Reference VM observations for M5a. Confirm power-off rollback before the first Writ setup. |
| Isolated containment CI with a reviewed host provisioner | M5a-12. The reference guest never enters this runner pool. |
| A dedicated, revocable model credential on a separate approved trial guest | Later M5b-08 live-host observations. |
| A roaming FIDO2 key and a platform passkey under the owner's control | Later M5b-10 real-device observations. |

[Local provisioning](PROVISIONING.md) · [Containment CI](../containment-ci/README.md) · [Live-host readiness](../live-host/README.md) · [Real authenticators](../authenticator-trials/README.md)

## Commands

In a reviewed checkout of the exact trial SHA on an amd64 Ubuntu 24.04 build machine with the pinned toolchain and SQLite installed, build and stage the binaries. Keep the test-only launcher binaries separate from the ordinary launchers.

```bash
set -euo pipefail
test -z "$(git status --porcelain --untracked-files=no)" || exit 1
source m1/tools/env.sh
cargo build --release --locked -p writ-hostsys -p writ-service
cargo build --locked --profile launcher -p writ-containment
CARGO_TARGET_DIR=target/faults cargo build --locked --profile launcher -p writ-containment --features test-faults
stage=$(mktemp -d)
mkdir "$stage/faults"
git archive --format=tar HEAD m5/reference-vm | tar -xf - -C "$stage"
cp target/release/writ-probe target/release/writ-serviced "$stage/"
cp target/launcher/writ-job-launcher target/launcher/writ-cgroup-exec "$stage/"
cp target/faults/launcher/writ-job-launcher target/faults/launcher/writ-cgroup-exec "$stage/faults/"
git rev-parse HEAD > "$stage/commit.txt"
(cd "$stage" && find . -type f ! -name SHA256SUMS -print0 | sort -z | xargs -0 sha256sum > SHA256SUMS)
```

From the provisioning workstation, set `stage` to the verified artifact directory and use the `ref_ssh` connection defined in the provisioning guide. Run this transfer after owner designation from the reference VM directory so the guest key and pinned host fingerprint resolve correctly.

```bash
tar -C "$stage" -cf - . | ref_ssh 'sudo install -d -m 0700 /root/writ && sudo tar --no-same-owner -xf - -C /root/writ'
```

Inside the designated guest, enter `sudo -i`, verify the staged checksums and compare `commit.txt` to the reviewed SHA. The image digest below matches the verified image pin in [local provisioning](PROVISIONING.md).

```bash
set -euo pipefail
cd /root/writ
sha256sum --check --strict SHA256SUMS
cat commit.txt

./m5/reference-vm/setup.sh --designated-vm writ-ref-1 --probe-binary /root/writ/writ-probe \
  --image-name ubuntu-24.04-server-cloudimg-amd64.img \
  --image-sha256 04e8f2461a16a44e78a370284471f29616cdd9baf1893df25807b71f985b6c94 \
  --launcher-binary /root/writ/writ-job-launcher --exec-binary /root/writ/writ-cgroup-exec \
  --agent-user writ-agent --job-user writ-job

./m5/reference-vm/trial.sh --designated-vm writ-ref-1 \
  --test-launcher /root/writ/faults/writ-job-launcher --test-exec /root/writ/faults/writ-cgroup-exec
```

Run `trial.sh` before starting the real service sockets. Preserve a failing trial's evidence and investigate it within the disposable guest. For the real service and socket checks, install separately after the trial:

```bash
./m5/reference-vm/service-install.sh --designated-vm writ-ref-1 \
  --serviced-binary /root/writ/writ-serviced --desktop-user writ-human --approval-port 8844
systemctl start writ-host.socket writ-admin.socket writ-approval.socket writ.service
```

Record the service observations below and every applicable item in the full gate checklist. Review the evidence for secrets before exporting it, then run teardown and preserve its report.

```bash
./m5/reference-vm/teardown.sh --designated-vm writ-ref-1
```

Power off and roll back or destroy the guest as described in [local provisioning](PROVISIONING.md#export-evidence-roll-back-and-destroy).

## What each script does

| Script | Does |
|---|---|
| `setup.sh` | installs `bubblewrap` (archive, not setuid); creates groups `writ` and `writ-agents`, the system user `writ` (no login shell, home `/var/lib/writ`), the agent user (in its primary group and `writ-agents`; refuses if it is in `sudo`, `adm`, `systemd-journal`, `writ`, `docker`, `lxd`, `disk` and similar) and the required job user; creates the J-1 layout: `/var/lib/writ` and `ledger/`, `stores/`, `keys/` 0700 `writ:writ`, `run/` 0750 `writ:writ`, `/srv/writ` 0755 `root:root`, `/srv/writ/targets` 0755 `writ:writ`, `/etc/writ` 0750 `root:writ`, `/run/writ` 0750 `writ:writ-agents` and `/run/writ/state` 0700 `writ:writ` (tmpfiles.d), `/usr/local/libexec/writ` 0755 `root:root`; copies `/usr/bin/bwrap` to `/usr/local/libexec/writ/bwrap` (`root:writ-job` 0750 by default) and loads `/etc/apparmor.d/writ-bwrap` granting `userns` to that path only, recording both digests in `/etc/writ`; writes `/etc/writ/filesystems` (UUID and `dumpe2fs -h` features of the filesystems of `/var/lib/writ` and `/srv/writ/targets`), `/etc/writ/reference-image` and a random probe canary (not a key); installs `writ-probe`; records versions before and after. Every change is appended to `/var/lib/writ-reference-vm.state`. |
| `trial.sh` | starts a probe fixture as `writ` (`LimitCORE=0`, holding `/run/writ/admin.sock` 0600) to exercise denial checks and the synthetic canary control. It exports the fixture journal; prepares a `writ`-owned hard-link source in `/var/tmp` and a bind mount inside a test root; runs `writ-probe agent-deny` as the agent user; runs `writ-probe` as `writ` in its own delegated unit (`systemd-run -p Delegate=yes`, `--cgroup-delegated`, `--xdev-root`); runs separate sandbox probes as the job UID, then the VM-only negative controls below; every change is restored by an exit trap. |
| `teardown.sh` | stops leftover units; unloads the AppArmor profile; removes, newest first, exactly what the state file lists (purges `bubblewrap` only if setup installed it); verifies nothing is left. |

VM-only negative controls in `trial.sh` (each probe's runtime control runs inside every `writ-probe` report):

| Control | Change | Expected |
|---|---|---|
| private copy without the AppArmor profile | `--bwrap` pointing at an unprofiled `root:writ` 0750 copy | P-USERNS `missing` |
| test uid granted the profile | current script uses group `writ`; the private copy is restricted to the job group | P-AGENT-USERNS `missing` required; resolve the group mismatch before relying on this control |
| no delegation | probe unit without `Delegate=yes` | P-CGROUP `missing` |
| unsupported filesystem | state, scratch and target roots on a tmpfs | P-FS-TYPE and P-RENAME `missing` |
| hard links unprotected | `sysctl fs.protected_hardlinks=0` | P-HARDLINK `missing` |
| planted default ACL | `setfacl -d -m u::rwx /var/lib/writ/stores` (when `setfacl` exists) | P-ACL `missing` |
| loosely moded canary key | a `writ`-owned 0644 copy of the canary passed with `--extra-read` | P-AGENT-DENY `missing` |
| planted canary | canary in the probe fixture's environment | P-NO-SECRET `missing` |

## Evidence to record (in TEST-EVIDENCE, with the exact SHA built)

From `/root/writ-evidence/<time>-setup/`: `versions-before.txt`, `versions-after.txt` (kernel, kernel package, systemd, bubblewrap version, system and private binary digests, AppArmor profile digest and load state, util-linux, coreutils, glibc, apparmor package, cgroup controllers, the J-9 sysctls, LSMs, filesystem source, type, UUID and options of `/var/lib` and `/srv`), `digests.txt`, `apparmor-writ-bwrap.txt`, `filesystems.txt`, `layout.txt`, `identities.txt`, `state.txt`.

From `/root/writ-evidence/<time>-trial/`: `baseline.json` (service-side `writ-readiness/1`), `jobside.json` (separate job-UID diagnostics), `agent-deny.json`, each `*-control.json` with its `.exit` and `.stderr`, `versions.txt`, `digests.txt` and `summary.txt` (one PASS or FAIL line per expectation). Record per-probe status in both reports, runtime control outcomes, the targeted VM control outcomes, `governed_ready` as emitted, the exact image name and SHA-256, hypervisor product and version, and the actual service, agent, desktop and job UIDs. The direct job-UID diagnostics do not supply the required launcher-mediated probes or a deployment proof. Treat absent checks as open. Deployment report validation requires each mandatory probe exactly once with status `ok`, including P-AGENT-DESKTOP. The probe CLI has no P-AGENT-DESKTOP collector. The [desktop probe obligation map](DESKTOP-PROBE.md) documents the fixture-only evaluator, pending guest collection and eight mandatory controls. Complete and independently review the live desktop and launcher evidence before enabling a deployment proof. [Probe IDs](../../hostsys/src/readiness.rs) · [Deployment report verification](../../hostsys/src/deployment.rs).

From `/root/writ-evidence/<time>-teardown/`: `after.txt` (no `LEFT` line).

Never record key material, the canary value, or credentials in evidence or chat.

## Service (M5a-04; PREPARED, NOT RUN)

`service-install.sh` (after `setup.sh` with `--job-user writ-job`, and a disposable test human user for `--desktop-user`) installs `writ-serviced` (`root:root` 0755), `/etc/writ/service.json` (`root:writ` 0640, no secret), provisions `ingress/vm-1` and `permit/vm-1` as `writ` (OS random source, temporary file, `fsync`, rename, directory `fsync`; material never printed), runs `writ-serviced --init` as `writ` and installs `writ.service` with the J-1 hardening (not started). Build it with `cargo build --release --locked -p writ-service` (`target/release/writ-serviced`).

Then, inside the VM: start it with `systemctl start writ.service`; `journalctl -u writ.service` must show `serving (unverified)` (with the default configuration, the service withholds the deployment proof and refuses governed work). The static fact diagnostic is `runuser -u writ -- /usr/local/libexec/writ/writ-serviced --print-static-facts --config /etc/writ/service.json` after supplying a reviewed deployment fact plan. A configured deployment proof requires a complete operator report, all required probes (including P-AGENT-DESKTOP), matching static facts and the canonical report digest. The current split trial outputs alone cannot supply that proof. VM checks to record: the agent uid cannot connect `admin.sock` or read `/var/lib/writ` (P-AGENT-DENY); `keys.revoke` as root over `admin.sock` records `uid:0`; a second `writ-serviced` or `writ-approval-server` on the same ledger exits with `OWNED_ELSEWHERE`.

Sockets (M5a-11, J-8; prepared, not run): `service-install.sh` installs the socket units under `service/`: `writ-host.socket` (`/run/writ/host.sock`, `writ:writ-agents` 0660, name `host`), `writ-admin.socket` (`/run/writ/admin.sock`, `writ:writ` 0600, name `admin`) and, with `--approval-port`, `writ-approval.socket` (from `writ-approval.socket.in`, `127.0.0.1:<port>`, name `approval`); `writ.service` lists them in `Sockets=` and `Requires=`, and the configuration sets `sockets.activation` to `systemd`, so `writ-serviced` takes them over through its own pidfd, verifies address, owner and mode, and refuses to start otherwise (it never binds them). The temporary `/run/writ` entry of `ReadWritePaths` is gone (only `/run/writ/state` stays writable). The socket units hold the sockets across service restarts. Start with `systemctl start writ-host.socket writ-admin.socket` (and `writ-approval.socket`), then `systemctl start writ.service`. VM checks to record (J-8): `systemctl stop writ.service` drains and exits 0 within the drain period (journal `stopped (drained ...)`), the agent uid cannot bind `/run/writ/host.sock` or the approval port while the service is stopped, a restart serves the same sockets, and sending TERM twice during an in-flight request triggers the second-signal exit (4). `trial.sh`'s admin.sock probe fixture refuses to run while `writ-admin.socket` is active. `teardown.sh` stops and disables `writ.service` and the socket units and removes every file `service-install.sh` recorded.

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

`trial.sh` sources `trial-launcher.sh`, which drives jobs through `launch.sock` with `launch-client.py` (run as the identity under test) and writes the reports `writ-probe --launcher-reports` evaluates for P-JOB-IDENTITY (with the skip-setgroups and gate-EOF test builds), P-JOB-DENY (sandbox and escape view, per host location), P-LAUNCHER-ADMIT, P-JOB-SERIAL and the P-FD status pipe. Anything a step cannot observe is written `not_run`, which fails the probe. P-FD status pipe (M5a-11, prepared, not run): while the job runs, the trial writes a forged `{ "exit-code": 99 }` line as root from the host to descriptor 4 of every process in the instance's `job` cgroup that holds the status pipe (the bubblewrap monitor), standing in for an escaped job that reaches the monitor's descriptor; the installed launcher must report the job's own exit (7). The negative control repeats this with the test-only build and fault `trust-status-after-started` (`--features test-faults`, a launcher that parses the status pipe after `started`), which must report 99; the probe fails if no forged write happened in either run.

**Verify in VM** (J-11.10), each to be recorded in TEST-EVIDENCE with the pinned version and source reference before the launcher is integrated: bubblewrap 0.9.0 `--block-fd` read location, EOF behaviour and descriptor closing; `--json-status-fd` emitting `child-pid` before blocking; `std` `CommandExt::exec` group clearing on Rust 1.99.1; AppArmor stacking, `px`/`ix` and attachment under `no_new_privs`; `TemporaryFileSystem=/run` with socket activation and `StandardInput=socket` passing no other descriptor; `SystemCallFilter` groups against bubblewrap and the runtime profile; `ProtectKernelTunables=yes` leaving the delegated subtree writable; apport and `core_pattern`; the pre-gate process count (`pre_gate_processes`, N5); `TemporaryFileSystem=/dev/shm /dev/mqueue` over `PrivateDevices=yes`; the `/proc/key-users` baseline after the helper's `setuid`; bubblewrap closing 3 and 4 before executing the program; `--remount-ro /` in `writ-bwrap/1`.
