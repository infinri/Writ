# M5 reference VM: setup, trial and teardown (M5a-03)

**Status: PREPARED, NOT RUN.** These scripts exist for the owner-designated disposable VM of D-045 M5-D12. They were never executed on a workstation; the live run is **blocked externally** until the VM exists, and is recorded as blocked, not passed (M5a-03 acceptance). Interfaces: [M5-INTERFACES](../../docs/implementation/M5-INTERFACES.md) J-1 (identities, layout) and J-9 (reference environment, probes); decision [D-045](../../docs/implementation/DECISION-LOG.md) (M5-D2, M5-D3, M5-D12).

## Safety

- Every script sources `common.sh` and refuses (exit 3, before any change) unless: the marker `/etc/writ-disposable-vm` exists, is a root-owned regular file not writable by group or others, starts with the line `writ-disposable-vm/1` and contains `name=<vm name>`; `--designated-vm <vm name>` matches it; the user is root; `systemd-detect-virt --vm` reports a VM; `/etc/os-release` is Debian 13. No script creates the marker.
- Nothing here installs a hypervisor, provisions a cloud VM, uses personal credentials, or changes the workstation (M5-D3, M5-D12). Take a VM snapshot before step 3; rolling back to it is the authoritative reset.
- `setup.sh` stops (exit 4) instead of changing kernel policy: if cgroup v2 is not mounted or AppArmor restricts unprivileged user namespaces, the owner decides (M5-DEPENDENCY-REVIEW section 4).

## Reference image

Debian 13 "trixie", amd64, official generic cloud image (`debian-13-generic-amd64-<date>.qcow2` from `cloud.debian.org/images/cloud/trixie/`), with its published SHA-512 checked by the owner and its SHA-256 recorded with `--image-sha256` (J-9). Readiness is never inferred from this choice; only the probes decide.

## Commands

On a build machine (the workstation is fine for building; nothing runs there):

```sh
source m1/tools/env.sh
cargo build --release --locked -p writ-hostsys        # target/release/writ-probe
sha256sum target/release/writ-probe                   # record; compare in the VM
```

Inside the VM, after copying the repository's `m5/reference-vm/` and `writ-probe` in (for example to `/root/writ/`):

1. Snapshot the fresh VM (`fresh`).
2. As root, create the marker by hand (choose the VM name):
   ```sh
   printf 'writ-disposable-vm/1\nname=writ-ref-1\n' > /etc/writ-disposable-vm && chmod 0644 /etc/writ-disposable-vm
   ```
3. Setup:
   ```sh
   /root/writ/reference-vm/setup.sh --designated-vm writ-ref-1 --probe-binary /root/writ/writ-probe \
     --image-name debian-13-generic-amd64-<date>.qcow2 --image-sha256 <64 hex>
   ```
4. Trial:
   ```sh
   /root/writ/reference-vm/trial.sh --designated-vm writ-ref-1
   ```
5. Copy `/root/writ-evidence/` off the VM.
6. Teardown (then roll back to `fresh` anyway):
   ```sh
   /root/writ/reference-vm/teardown.sh --designated-vm writ-ref-1
   ```

## What each script does

| Script | Does | Reverses |
|---|---|---|
| `setup.sh` | installs `bubblewrap` (Debian archive, not setuid); creates system user and group `writ` (no login shell, home `/var/lib/writ`) and the test agent user `writ-agent` (not in group `writ`); creates the J-1 layout: `/var/lib/writ` and `ledger/`, `stores/`, `keys/`, `run/` 0700 `writ:writ`, `targets/` 0750, `/etc/writ` 0750 `root:writ`, `/run/writ` 0755 `writ:writ` (tmpfiles.d); writes `/etc/writ/reference-image` and a random probe canary `/etc/writ/probe-canary` (0640 `root:writ`, not a key); installs `writ-probe` to `/usr/local/libexec/writ/`; records versions before and after | each change is appended to `/var/lib/writ-reference-vm.state` |
| `trial.sh` | starts a placeholder service process as `writ` holding `/run/writ/admin.sock` (0600; a stand-in for `writ-serviced` until M5a-04); runs `writ-probe agent-deny` as `writ-agent`; runs `writ-probe` as `writ` in its own delegated unit (`systemd-run -p Delegate=yes`, `--cgroup-delegated`); then the VM-only negative controls, each restored at once and again on exit | every change restored by an exit trap |
| `teardown.sh` | stops leftover probe and placeholder units; removes, newest first, exactly what the state file lists (purges `bubblewrap` only if setup installed it); verifies nothing is left | n/a |

VM-only negative controls in `trial.sh` (the runtime controls run inside every `writ-probe` report):

| Control | Change | Expected |
|---|---|---|
| namespace creation disabled | `sysctl user.max_user_namespaces=0` | P-USERNS `missing` |
| no delegation | probe unit without `Delegate=yes` | P-CGROUP `missing` |
| unsupported filesystem | state and target roots on a tmpfs | P-FS-TYPE `missing` |
| mis-moded trusted directory | `/var/lib/writ/keys` 0755 | P-AGENT-DENY `missing` |
| planted canary | canary in the placeholder service's environment | P-NO-SECRET `missing` |

## Evidence to record (in TEST-EVIDENCE, with the exact SHA built)

From `/root/writ-evidence/<time>-setup/`: `versions-before.txt`, `versions-after.txt` (kernel, kernel package, systemd, bubblewrap version and SHA-256, util-linux, coreutils, glibc, cgroup controllers, sysctls, AppArmor, LSMs, `/var/lib` filesystem and mount options), `writ-probe.sha256`, `layout.txt`, `identities.txt`, `state.txt`.

From `/root/writ-evidence/<time>-trial/`: `baseline.json` (the `writ-readiness/1` report), `agent-deny.json`, each `*-control.json` with its `.exit` and `.stderr`, `versions.txt`, and `summary.txt` (one PASS or FAIL line per expectation). Record: every probe's status in the baseline; every runtime control `failed_as_expected`; every VM control's targeted probe `missing`; `governed_ready` (expected **false** in this build because P-PEERCRED reports `error` until peer credentials are probed with a reviewed implementation); the image name and SHA-256; and the VM product and version.

From `/root/writ-evidence/<time>-teardown/`: `after.txt` (no `LEFT` line).

Never record key material, the canary value, or credentials in evidence or chat.
