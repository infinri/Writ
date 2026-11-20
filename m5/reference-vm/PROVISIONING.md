# Local Ubuntu 24.04 VM provisioning (M5a-03, M5a-12, D-047)

## Workstation preflight

At the 2026-11-20 checkpoint the owner's laptop screenshot showed x86_64, 13 GiB available RAM, 256 GiB available on the home filesystem and a KVM device owned by root:kvm. QEMU, qemu-img and cloud-localds were missing. Repeat the checks below as the ordinary login after installation to confirm effective KVM access and current capacity. The Debian 13 development container has a separate filesystem and no KVM device. The isolated CI runner is a fourth environment and requires its own owner-controlled host.

The owner must provide `qemu-system-x86_64`, `qemu-img` and `cloud-localds` on that Ubuntu workstation (packages `qemu-system-x86`, `qemu-utils`, `cloud-image-utils`), and a trusted cloud-image keyring from `ubuntu-keyring`. Use the ordinary login for all host commands. The reference allocation is 2 vCPUs, 4 GiB RAM and a 24 GiB qcow2 disk. Require 6 GiB available host RAM and 30 GiB free at the selected filesystem before provisioning. Stop if a prerequisite fails.

```bash
set -euo pipefail
umask 077
vm_dir="${XDG_DATA_HOME:-$HOME}/writ-vms"
ref=writ-ref-1
test "$(id -u)" -ne 0
test "$(uname -m)" = x86_64
test -d "${vm_dir%/*}" && test -w "${vm_dir%/*}"
[[ "$vm_dir" = /* && "$vm_dir" != *,* ]]
for tool in qemu-system-x86_64 qemu-img cloud-localds curl gpgv ssh ssh-keygen ssh-keyscan python3; do
  command -v "$tool"
done
test -r /usr/share/keyrings/ubuntu-cloudimage-keyring.gpg
python3 - <<'PYTHON'
import fcntl
import socket
with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
    listener.bind(("127.0.0.1", 22224))
with open("/dev/kvm", "rb+", buffering=0) as device:
    version = fcntl.ioctl(device, 0xae00)
    print(f"KVM API {version}")
    if version != 12:
        raise SystemExit(1)
PYTHON
test "$(nproc)" -ge 2
test "$(awk '/^MemAvailable:/ {print $2}' /proc/meminfo)" -ge $((6 * 1024 * 1024))
df -Pk "${vm_dir%/*}"
test "$(df -Pk "${vm_dir%/*}" | awk 'NR == 2 {print $4}')" -ge $((30 * 1024 * 1024))
qemu-system-x86_64 --version
```

Use an existing writable absolute parent for `vm_dir` with the required free space. If the tools are still missing on the Ubuntu workstation, the owner can run `sudo apt-get update && sudo apt-get install qemu-system-x86 qemu-utils cloud-image-utils ubuntu-keyring` there, then repeat the ordinary-user preflight. Keep VM paths free of commas. The host needs loopback TCP port 22224 free. [Ubuntu keyring file list](https://packages.ubuntu.com/noble/all/ubuntu-keyring/filelist).

## Verify the clean image

Run in the same workstation shell. Use a fresh base directory for the first run and retain the verification files and output. The [Canonical Noble build dated 2026-10-17](https://cloud-images.ubuntu.com/releases/noble/release-20261017/) currently publishes the pinned digest below. Authenticate its manifest and verify the full image on the provisioning host. If the dated build disappears, stop for review of a new signed pin. [Canonical image verification](https://ubuntu.com/docs/public-images/public-images-how-to/verify-image-checksum/).

```bash
image=ubuntu-24.04-server-cloudimg-amd64.img
base_url=https://cloud-images.ubuntu.com/releases/noble/release-20261017
image_sha=04e8f2461a16a44e78a370284471f29616cdd9baf1893df25807b71f985b6c94
mkdir -p "$vm_dir"
mkdir "$vm_dir/base"
cd "$vm_dir/base"
for file in SHA256SUMS SHA256SUMS.gpg "$image"; do
  curl --fail --silent --show-error --location --output "$file" "$base_url/$file"
done
gpgv --status-fd 1 --keyring /usr/share/keyrings/ubuntu-cloudimage-keyring.gpg \
  SHA256SUMS.gpg SHA256SUMS | tee signature.txt
grep -F '[GNUPG:] VALIDSIG D2EB44626FDDC30B513D5BB71A5D6C4C7DB87C81 ' signature.txt
grep -Fx "$image_sha *$image" SHA256SUMS
printf '%s *%s\n' "$image_sha" "$image" | sha256sum --check --strict
qemu-img info --output=json "$image" | tee image-info.json
python3 -c 'import json; image = json.load(open("image-info.json")); assert image["format"] == "qcow2" and "backing-filename" not in image'
chmod 0444 "$image"
```

## Create a disposable reference guest

Create a fresh overlay and NoCloud seed in the same workstation shell. The first boot uses restricted user networking. The seed contains only the disposable access public key and guest setup. [QEMU invocation](https://www.qemu.org/docs/master/system/invocation.html) and [cloud-init NoCloud](https://docs.cloud-init.io/en/latest/reference/datasources/nocloud.html).

```bash
mkdir "$vm_dir/$ref"
cd "$vm_dir/$ref"
qemu-img create -f qcow2 -F qcow2 -b "$vm_dir/base/$image" disk.qcow2 24G
ssh-keygen -q -t ed25519 -N '' -f guest-access
printf 'instance-id: %s\nlocal-hostname: %s\n' "$ref" "$ref" > meta-data
cat > user-data <<CLOUD
#cloud-config
ssh_pwauth: false
disable_root: true
users:
  - name: vm-operator
    shell: /bin/bash
    sudo: ALL=(ALL) NOPASSWD:ALL
    lock_passwd: true
    ssh_authorized_keys:
      - $(cat guest-access.pub)
CLOUD
cloud-localds seed.img user-data meta-data
qemu-img info --backing-chain disk.qcow2
start_reference() {
  qemu-system-x86_64 -name "$ref" -machine q35,accel=kvm -cpu host \
    -smp 2 -m 4096 -display none -serial stdio -monitor none \
    -drive file=disk.qcow2,if=virtio,format=qcow2 \
    -drive file=seed.img,media=cdrom,format=raw,readonly=on \
    -nic "user,model=virtio-net-pci,restrict=${1:-on},hostfwd=tcp:127.0.0.1:22224-:22"
}
start_reference
```

In a second workstation shell, pin the Ed25519 SSH host key against the fingerprint in the QEMU serial output. Set `console_fingerprint` to that exact `SHA256:...` value. Use this connection and leave the first shell attached to QEMU.

```bash
set -euo pipefail
umask 077
vm_dir="${XDG_DATA_HOME:-$HOME}/writ-vms"
ref=writ-ref-1
cd "$vm_dir/$ref"
read -r -p 'Guest console Ed25519 host fingerprint: ' console_fingerprint
ssh-keyscan -4 -T 5 -p 22224 -t ed25519 127.0.0.1 > known_hosts.candidate
test "$(ssh-keygen -lf known_hosts.candidate | awk '{print $2}')" = "$console_fingerprint"
mv known_hosts.candidate known_hosts
ref_ssh() {
  local -a tty=()
  if [[ "${1:-}" = -t ]]; then
    tty=(-t)
    shift
  fi
  ssh "${tty[@]}" -F /dev/null -o IdentitiesOnly=yes -o IdentityAgent=none -o ForwardAgent=no \
    -o StrictHostKeyChecking=yes -o GlobalKnownHostsFile=/dev/null \
    -o UserKnownHostsFile="$PWD/known_hosts" -i "$PWD/guest-access" \
    -p 22224 vm-operator@127.0.0.1 "$@"
}
ref_ssh 'cloud-init status --wait'
ref_ssh 'test "$(hostname)" = writ-ref-1 && test "$(systemd-detect-virt --vm)" = kvm && test ! -e /etc/writ-disposable-vm && test ! -L /etc/writ-disposable-vm'
ref_ssh 'sudo systemctl poweroff'
```

After QEMU exits, create the clean, unmarked snapshot in the first shell. For each snapshot, test a disk change across two boots before owner designation. In the first shell:

```bash
qemu-img snapshot -c fresh disk.qcow2
qemu-img snapshot -l disk.qcow2
start_reference
```

In the second workstation shell, wait for pinned SSH, write a harmless witness on the guest root filesystem, and record the witness digest on the workstation. The strict host-key check must succeed on every boot.

```bash
rollback_witness() {
  ref_ssh 'cloud-init status --wait && test "$(hostname)" = writ-ref-1 && test "$(systemd-detect-virt --vm)" = kvm'
  ref_ssh 'sudo bash -se' <<'WITNESS'
set -eu -o noclobber
umask 077
cat /proc/sys/kernel/random/uuid > /root/writ-rollback-witness
sync /root/writ-rollback-witness
sha256sum /root/writ-rollback-witness
WITNESS
  ref_ssh 'sudo systemctl poweroff'
}
rollback_check() {
  ref_ssh 'cloud-init status --wait && test "$(hostname)" = writ-ref-1 && test "$(systemd-detect-virt --vm)" = kvm'
  ref_ssh 'sudo test ! -e /root/writ-rollback-witness && sudo test ! -L /root/writ-rollback-witness && sudo test ! -e /etc/writ-disposable-vm && sudo test ! -L /etc/writ-disposable-vm'
}
rollback_witness > fresh-witness.log
cat fresh-witness.log
```

When QEMU exits, restore the snapshot in the first shell and boot.

```bash
qemu-img snapshot -a fresh disk.qcow2
qemu-img check disk.qcow2
start_reference
```

In the second shell, require `rollback_check` to exit zero and record the result before opening the owner designation session.

```bash
rollback_check
printf '%s fresh rollback verified\n' "$(date -u +%FT%TZ)" >> fresh-witness.log
```

## Owner designation and guest trials

From the workstation, the owner opens `ref_ssh -t 'sudo -i'`. In this guest root shell, check the current boot, ordinary guest namespaces and procfs before making any test setup changes. The collector compares the filesystem root and mount, UTS, PID, user and network namespaces against PID 1. The initial PID and user namespace inode numbers are 4026531836 and 4026531837. Its process sweep also requires all visible tasks to be inspectable and in the admitted PID, user and network namespaces.

```bash
set -euo pipefail
export LC_ALL=C
test "$(id -ru):$(id -u)" = 0:0
test "$(cat /proc/sys/kernel/hostname)" = writ-ref-1
test "$(systemd-detect-virt --vm)" = kvm
container_rc=0
container_kind=$(systemd-detect-virt --container) || container_rc=$?
test "$container_rc:$container_kind" = 1:none
. /usr/lib/os-release
test "$ID:$VERSION_ID" = ubuntu:24.04
test "$(stat -Lc '%d:%i' /)" = "$(stat -Lc '%d:%i' /proc/1/root)"
test -d /etc
test ! -L /etc
test "$(findmnt -n -o ID --target /etc)" = "$(findmnt -n -o ID --target /)"
for ns in mnt uts pid user net; do
  test "$(readlink "/proc/$$/ns/$ns")" = "$(readlink "/proc/1/ns/$ns")"
done
test "$(readlink /proc/1/ns/pid)" = 'pid:[4026531836]'
test "$(readlink /proc/1/ns/user)" = 'user:[4026531837]'
findmnt --target /proc --submounts -o TARGET,FSROOT,FSTYPE,VFS-OPTIONS,FS-OPTIONS
cat /proc/sys/kernel/random/boot_id
```

Require a single full procfs mounted at `/proc` with filesystem root `/`. Accept the default mount options (`rw` or `ro`, `relatime`, `nosuid`, `nodev`, `noexec`, optional `hidepid=0` or `hidepid=off`). The only allowed submount is `/proc/sys/fs/binfmt_misc`. Stop for filtered, overmounted or narrowed proc views. Keep the shell in the booted guest's normal root and namespaces.

The owner alone writes the marker after verifying the snapshot and these checks. Its exact two lines, including the final newline, are bound to the guest hostname. The file must be regular, root:root, mode 0600, under `/etc` without symlinks or a separate file mount. Run this as the owner in the verified guest root shell only.

```bash
test ! -e /etc/writ-disposable-vm
test ! -L /etc/writ-disposable-vm
(umask 077; set -o noclobber; printf 'writ-disposable-vm/1\nname=writ-ref-1\n' > /etc/writ-disposable-vm)
test "$(stat -c '%u:%g %a %F' /etc/writ-disposable-vm)" = '0:0 600 regular file'
```

## First guest inventory

Exit the guest SSH session after designation and leave that guest running. Build only the inventory executable at the reviewed exact SHA from a clean checkout on an amd64 Ubuntu 24.04 build system with the pinned repository toolchain available. Bring the private stage directory to the workstation if the build system is separate. The first inventory tests checkpoint `71593d9f9247ca7b52881b177e97b76a5f1a3f4c`. All six ordinary jobs passed in [run 44769052020](https://github.com/infinri/Writ/actions/runs/44769052020). Stage the independent comparison tool separately with its own source identity and digest. Use a fresh clean build checkout of the checkpoint. On that Ubuntu build machine use the reviewed repository installer `bash m1/tools/install-toolchains.sh` if the pinned tools are absent.

```bash
set -euo pipefail
umask 077
test -z "$(git status --porcelain)"
. /etc/os-release
test "$ID:$VERSION_ID:$(dpkg --print-architecture)" = ubuntu:24.04:amd64
source m1/tools/env.sh
test "$(rustc --version | cut -d' ' -f2)" = 1.99.1
test "$(cargo --version | cut -d' ' -f2)" = 1.99.1
test "$(git rev-parse HEAD)" = 71593d9f9247ca7b52881b177e97b76a5f1a3f4c
cargo build --locked -p writ-hostsys --bin writ-desktop-inventory
stage=$(mktemp -d)
install -m 0700 target/debug/writ-desktop-inventory "$stage/"
git rev-parse HEAD > "$stage/commit.txt"
(cd "$stage" && sha256sum writ-desktop-inventory commit.txt > SHA256SUMS)
printf 'Inventory stage: %s\n' "$stage"
```

In the workstation shell with `ref_ssh`, set `stage` to that verified directory and transfer the three files. Then open a new root shell in the same running guest with `ref_ssh -t 'sudo -i'`.

```bash
tar -C "$stage" -cf - writ-desktop-inventory commit.txt SHA256SUMS |
  ref_ssh 'sudo install -d -m 0700 /root/writ-inventory && sudo tar --no-same-owner -xf - -C /root/writ-inventory'
```

Stage the comparison tool from a separately reviewed revision containing this runbook. In the workstation shell with `ref_ssh`, set `review_repo` to that checkout. These commands keep its provenance separate from the checkpoint binary.

```bash
read -rp 'Absolute path to reviewed comparison-tool checkout: ' review_repo
read -rp 'Reviewed full comparison-tool commit: ' compare_sha
[[ "$compare_sha" =~ ^[0-9a-f]{40}$ ]]
tool_path=m5/reference-vm/compare-inventory.py
compare_stage=$(mktemp -d)
git -C "$review_repo" show "$compare_sha:$tool_path" > "$compare_stage/compare-inventory.py"
printf '%s\n' "$compare_sha" > "$compare_stage/tool-commit.txt"
(cd "$compare_stage" && sha256sum compare-inventory.py tool-commit.txt > TOOL-SHA256SUMS)
tar -C "$compare_stage" -cf - compare-inventory.py tool-commit.txt TOOL-SHA256SUMS |
  ref_ssh 'sudo tar --no-same-owner -xf - -C /root/writ-inventory'
```

Normalize only the three NSS account rules inside the guest. Verify that `/etc/nsswitch.conf`, `/etc/passwd`, `/etc/group` and `/usr/lib/os-release` are regular root:root files, on their normal filesystems, with no group or other write bits. The snapshot restores original NSS. The four throwaway role accounts deliberately have their own primary groups. Only the agent has the additional `writ-agents` membership.

```bash
set -euo pipefail
export LC_ALL=C
cd /root/writ-inventory
sha256sum --check --strict SHA256SUMS
test "$(cat commit.txt)" = 71593d9f9247ca7b52881b177e97b76a5f1a3f4c
install -d -m 0700 /root/writ-evidence
umask 077
evidence=$(mktemp -d /root/writ-evidence/inventory.XXXXXXXX)
root_mount=$(findmnt -n -o ID --target /)
test ! -L /usr
test ! -L /usr/lib
for file in /etc/nsswitch.conf /etc/passwd /etc/group /usr/lib/os-release; do
  test -f "$file"
  test ! -L "$file"
  test "$(stat -c '%u:%g' "$file")" = 0:0
  mode=$(stat -c '%a' "$file")
  test "$((8#$mode & 022))" -eq 0
  test "$(findmnt -n -o ID --target "$file")" = "$root_mount"
  stat -c '%u:%g %a %F %n' "$file"
done > "$evidence/trusted-files.txt"
cp -p /etc/nsswitch.conf /root/writ-inventory/nsswitch.before
sed -Ei '/^[[:space:]]*(passwd|group|initgroups)[[:space:]]*:/d' /etc/nsswitch.conf
printf 'passwd: files\ngroup: files\ninitgroups: files\n' >> /etc/nsswitch.conf
grep -E '^(passwd|group|initgroups):' /etc/nsswitch.conf | tee "$evidence/nss-rules.txt"
groupadd --system writ-agents
useradd --system --user-group --no-create-home --shell /usr/sbin/nologin writ
useradd --system --user-group --no-create-home --shell /usr/sbin/nologin writ-job
useradd --user-group --no-create-home --shell /usr/sbin/nologin --groups writ-agents writ-agent
useradd --user-group --create-home --shell /bin/bash writ-human
chmod 0700 /home/writ-human
for user in writ writ-job writ-agent writ-human; do id "$user"; done
```

Start a stable disposable agent process and a known endpoint holder under the disposable desktop UID. These are positive inventory inputs only. They create IPv4 and IPv6 loopback TCP listeners, one filesystem Unix socket and one abstract Unix socket. No real desktop session or access attempt is needed for this first inventory. Wait for the guest to become quiet before collecting.

```bash
cat > /home/writ-human/inventory-targets.py <<'PYTHON'
import signal
import socket

def listener(family, address):
    endpoint = socket.socket(family, socket.SOCK_STREAM)
    if family == socket.AF_INET6:
        endpoint.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 1)
    endpoint.bind(address)
    endpoint.listen()
    return endpoint

listeners = [
    listener(socket.AF_INET, ("127.0.0.1", 48124)),
    listener(socket.AF_INET6, ("::1", 48124)),
    listener(socket.AF_UNIX, "/home/writ-human/inventory.sock"),
    listener(socket.AF_UNIX, "\0writ-inventory"),
]
print("inventory endpoints ready", flush=True)
signal.pause()
PYTHON
chmod 0644 /home/writ-human/inventory-targets.py
systemd-run --quiet --unit=writ-inventory-agent --uid=writ-agent /usr/bin/sleep infinity
systemd-run --quiet --unit=writ-inventory-desktop --uid=writ-human \
  /usr/bin/python3 /home/writ-human/inventory-targets.py
systemctl is-active writ-inventory-agent writ-inventory-desktop
journalctl -u writ-inventory-desktop -n 10 --no-pager
```

Require the diagnostic commands below to succeed and to show the expected listeners and the desktop unit's PID. The kernel needs inet and Unix SOCK_DIAG handlers (including IPv6 TCP and the IPv6-only attribute). If unavailable, stop and check the booted kernel's `CONFIG_INET_DIAG`, `CONFIG_INET_TCP_DIAG`, `CONFIG_UNIX_DIAG`, IPv6 support and modules. If configured as modules and unavailable through autoload, the owner can preload `tcp_diag` and `unix_diag` in this guest with `modprobe`. Preserve AppArmor and the proc admission guards. Record the agent's main PID, start time and credentials from the independent observation below.

```bash
agent_pid=$(systemctl show -p MainPID --value writ-inventory-agent)
desktop_pid=$(systemctl show -p MainPID --value writ-inventory-desktop)
test "$agent_pid" -gt 1
test "$desktop_pid" -gt 1
{
  cat commit.txt
  sha256sum writ-desktop-inventory
  uname -r
  printf 'agent PID %s, desktop PID %s\n' "$agent_pid" "$desktop_pid"
  ps -p "$agent_pid,$desktop_pid" -o pid,uid,gid,lstart,comm
  for pid in "$agent_pid" "$desktop_pid"; do
    stat_row=$(cat "/proc/$pid/stat")
    read -r -a stat_fields <<< "${stat_row##*) }"
    printf 'PID %s start ticks %s\n%s\n' "$pid" "${stat_fields[19]}" "$stat_row"
    grep -E '^(Uid|Gid|Groups):' "/proc/$pid/status"
  done
  ss -4 -H -ltnep '( sport = :48124 )'
  ss -6 -H -ltnep '( sport = :48124 )'
  ss -x -H -lnep | grep -E '/home/writ-human/inventory[.]sock|@writ-inventory'
} > "$evidence/expected.txt"
cat "$evidence/expected.txt"
```

After checking the expected evidence, run the first inventory in the guest root shell. Read the boot ID immediately from this boot. Supply the numeric UIDs from files-only NSS. Leave stdout and stderr separate and always preserve the exit status.

```bash
boot_id=$(cat /proc/sys/kernel/random/boot_id)
service_uid=$(id -u writ)
job_uid=$(id -u writ-job)
agent_uid=$(id -u writ-agent)
desktop_uid=$(id -u writ-human)
rc=0
./writ-desktop-inventory writ-ref-1 "$boot_id" "$service_uid" "$job_uid" "$agent_uid" "$desktop_uid" \
  > "$evidence/inventory.txt" 2> "$evidence/inventory.stderr" || rc=$?
printf '%s\n' "$rc" > "$evidence/inventory.exit"
if test "$rc" -eq 0; then
  test "$(tail -n 1 "$evidence/inventory.txt")" = end
fi
```

In the same guest root shell, compare the fixture processes, four endpoints and their holders while the fixtures are still live. Preserve comparison failure alongside the inventory, including cases where a task changed during the independent sweep. The comparison checks the socket cookie encoding and reserved values. It does not independently obtain the socket cookie.

```bash
sha256sum --check --strict TOOL-SHA256SUMS
compare_rc=0
python3 -I compare-inventory.py "$evidence" > "$evidence/comparison.stdout" 2> "$evidence/comparison.stderr" || compare_rc=$?
printf '%s\n' "$compare_rc" > "$evidence/comparison.exit"
cp TOOL-SHA256SUMS tool-commit.txt SHA256SUMS commit.txt "$evidence/"
```

Require both recorded exits to be zero and review the raw observations below before recording success.

Count positive guest evidence only after exit 0 and the final `end` record. Compare the role UID rows and the agent process row with the observations in `expected.txt`, including PID, start ticks (proc stat field 22), all four UIDs and GIDs, and the supplementary group set. Match both TCP listener addresses and port, IPv6-only mode and both Unix endpoint addresses. Match each endpoint inode to a holder row containing the desktop PID and start ticks. The filesystem address hex is `2f686f6d652f777269742d68756d616e2f696e76656e746f72792e736f636b00` (trailing NUL). The abstract address hex is `00777269742d696e76656e746f7279` (leading NUL). Record the actual scope, image and binary checksums, status, exceptions and [inventory limits](DESKTOP-PROBE.md#guest-inventory). Preserve refusal and diagnostic output. Exit 3 means unavailable. Investigate guest process or socket churn, holders, inaccessible tasks and diagnostic availability in that guest before retrying. Export reviewed evidence and restore the snapshot when done.

## Later reference trials and isolated containment CI

The first inventory uses stand-in targets. The clean rollback discards the temporary inventory accounts. After restoring `fresh` with QEMU stopped, boot with restricted networking and run `rollback_check`. Also verify the inventory accounts and `/root/writ-inventory` are absent, then power off. Before the broader reference trials, boot the unmarked guest for package preparation with `start_reference off` and a host network policy that restricts that boot to approved package endpoints. On the guest, install the trial-only packages and create the desktop account again.

```bash
ref_ssh 'sudo apt-get update && sudo apt-get install -y --no-install-recommends acl apparmor-utils bubblewrap busybox-static dbus-x11 dmeventd dmsetup e2fsprogs gnome-keyring xauth xvfb'
ref_ssh 'sudo useradd -m -s /bin/bash writ-human && sudo chmod 0700 /home/writ-human'
ref_ssh 'sudo systemctl poweroff'
```

Wait for QEMU to exit, create a separate unmarked rollback snapshot with `qemu-img snapshot -c trial-ready disk.qcow2`, and boot it with restricted networking (`start_reference`). In the second shell run `rollback_witness > trial-ready-witness.log`. Once QEMU exits, run `qemu-img snapshot -a trial-ready disk.qcow2 && qemu-img check disk.qcow2 && start_reference` in the first shell. Require `rollback_check` in the second shell, confirm the prepared desktop account still exists and repeat the owner designation checks for that boot. The [reference trial commands](README.md#commands) then stage and run the broader checks. Confirm cgroup v2 with `cpu memory pids`, the AppArmor user namespace restriction set to `1`, and ext4 before those trials. Full desktop acceptance still needs the actual disposable session targets and negative controls listed in M5-INTERFACES J-9.

The [isolated containment CI gate](../containment-ci/README.md) requires a separate runner host and control repository, runner group, provisioner, exact gate wrapper and status App. Budget 4 vCPUs, 6 GiB guest RAM, a 24 GiB disk and 30 GiB free host disk per active runner, with 2 GiB available host RAM above the allocation. Each reviewed job attempt requires a fresh overlay and CI-only seed. Destroy it after success, failure or cancellation. The reference guest supplies only reference evidence.

## Export evidence, roll back and destroy

While the guest is running with restricted networking, review files within `/root/writ-evidence/` and assemble a root-owned tree at `/root/writ-evidence-sanitized/` containing only reviewed evidence files. Keep the exact SHA, per-check status and exit codes, required version and checksum facts, and failure diagnostics. Remove canaries, key material, credentials and any private content. Do this before and after teardown if the first export is needed to preserve failed trial output.

On the workstation, pull only the curated tree into a new archive, record its checksum and list its members without extracting them. Repeat with a new archive name after teardown if needed.

```bash
mkdir -p "$vm_dir/evidence"
archive="$vm_dir/evidence/${ref}-$(date -u +%Y%m%dT%H%M%SZ).tar"
ref_ssh 'sudo tar -C /root/writ-evidence-sanitized -cf - .' > "$archive"
tar -tf "$archive"
sha256sum "$archive" > "$archive.sha256"
sha256sum --check --strict "$archive.sha256"
# Compare the archive bytes to the reviewed files before powering off.
ref_ssh 'sudo tar -C /root/writ-evidence-sanitized -df -' < "$archive"
ref_ssh 'sudo systemctl poweroff'
```

After QEMU exits, either roll back for another owner-designated reference trial or destroy the guest. The snapshot discards post-designation guest state, including the marker. Restore `fresh` for inventory or `trial-ready` for a prepared trial. With QEMU stopped, run the inventory rollback as follows.

```bash
cd "$vm_dir/$ref"
qemu-img snapshot -a fresh disk.qcow2
qemu-img check disk.qcow2
```

For destruction, verify QEMU has exited and the evidence archive has been received. The following removes precisely this reference clone's files, including the private guest access key. It keeps the verified base and exported evidence.

```bash
cd "$vm_dir/$ref"
rm -i -- disk.qcow2 seed.img user-data meta-data guest-access guest-access.pub known_hosts
cd "$vm_dir"
rmdir -- "$ref"
```
