# Local Ubuntu 24.04 VM provisioning (M5a-03, M5a-12, D-047)

## Workstation preflight

Run as the ordinary workstation user. The destination must be on a filesystem with room for the guests and evidence.

```bash
uname -m
lscpu | grep -E 'Architecture|Virtualization|Hypervisor'
command -v qemu-system-x86_64 qemu-img cloud-localds curl gpgv ssh ssh-keygen
ls -l /dev/kvm
test -r /dev/kvm && test -w /dev/kvm
free -h
df -h "${XDG_DATA_HOME:-$HOME}"
```

This procedure uses amd64 hardware virtualization and an existing ordinary-user QEMU/KVM setup. Require at least 2 vCPUs, 4 GiB guest RAM and a 24 GiB virtual disk for the reference guest. Budget 4 vCPUs, 6 GiB and 24 GiB for a containment runner provisioned on a separate runner host. Keep at least 2 GiB of available host RAM above the selected allocation, and 30 GiB of free space per active guest for the base image, overlay growth and evidence. Measure host resources again before boot. The workstation preflight on 2026-11-17 found AMD-V, approximately 9.3 GiB available RAM and 36 GiB available disk. The ordinary login opened `/dev/kvm` successfully and returned KVM API version 12. QEMU, `qemu-img`, `cloud-localds` and libvirt clients are absent. For this workstation, the owner can install Ubuntu packages `qemu-system-x86`, `qemu-utils` and `cloud-image-utils`. An existing ordinary-user KVM host or an equivalent disposable Ubuntu 24.04 guest with rollback also meets the prerequisite. Run the preflight on that host and record its QEMU version with `qemu-system-x86_64 --version`.

## Verify the clean image

Use the [Canonical Noble build dated 2026-10-17](https://cloud-images.ubuntu.com/releases/noble/release-20261017/). Its signed manifest lists the amd64 cloud image SHA-256 as `04e8f2461a16a44e78a370284471f29616cdd9baf1893df25807b71f985b6c94`. A separate download matched that digest before provisioning. The manifest signature was verified against the installed Ubuntu cloud-image keyring, signing fingerprint `D2EB44626FDDC30B513D5BB71A5D6C4C7DB87C81`. Verify both the signature and image on the provisioning host. Obtain the keyring from a trusted Ubuntu installation or package source. [Canonical verification instructions](https://ubuntu.com/docs/public-images/public-images-reference/artifacts/).

```bash
set -euo pipefail
umask 077
vm_dir="${XDG_DATA_HOME:-$HOME/.local/share}/writ-vms"
image=ubuntu-24.04-server-cloudimg-amd64.img
base_url=https://cloud-images.ubuntu.com/releases/noble/release-20261017
image_sha=04e8f2461a16a44e78a370284471f29616cdd9baf1893df25807b71f985b6c94
mkdir -p "$vm_dir/base"
cd "$vm_dir/base"
for file in SHA256SUMS SHA256SUMS.gpg "$image"; do
  curl --fail --silent --show-error --output "$file" "$base_url/$file"
done
gpgv --keyring /usr/share/keyrings/ubuntu-cloudimage-keyring.gpg SHA256SUMS.gpg SHA256SUMS
grep -Fx "$image_sha *$image" SHA256SUMS
printf '%s *%s\n' "$image_sha" "$image" | sha256sum --check --strict
qemu-img info --output=json "$image"
chmod 0444 "$image"
```

Confirm `qemu-img info` reports `qcow2` and no backing file. Retain the dated URL, manifest, signature and verification output with the evidence. If Canonical removes the dated build, choose and review a new signed build and record its exact date and digest before creating guests.

## Create a disposable reference guest

The stopped definition consists of the overlay, cloud-init seed and boot command below. Keep paths free of commas for QEMU. The base image is shared read-only. Make the containment runner's disk separately on its isolated host using the same verified base and a fresh overlay for each job ID. Create the separate runner disk with `qemu-img create -f qcow2 -F qcow2 -b "$vm_dir/base/$image" "$runner_disk" 24G` using an absolute path unique to the approved job and attempt on the runner host. The runner procedure uses a fresh CI-only seed and [separate admission, networking and lifecycle controls](../containment-ci/README.md). [QEMU invocation](https://www.qemu.org/docs/master/system/invocation.html) and [cloud-init NoCloud](https://docs.cloud-init.io/en/latest/reference/datasources/nocloud.html).

```bash
ref=writ-ref-1
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
```

The provisioning operator can now boot the OS to install packages and stage reviewed build artifacts. Privileged Writ trials require the owner's manual designation in the next section. No shared directories, host devices, persistent credentials or agent forwarding are needed. Use restricted networking for the trial boot. The initial provisioning boot enables QEMU user networking for package installation. Use a host network policy that restricts access to approved package endpoints during that boot.

```bash
cd "$vm_dir/$ref"
start_reference() {
  qemu-system-x86_64 -name "$ref" -machine q35,accel=kvm -cpu host \
    -smp 2 -m 4096 -display none -serial stdio -monitor none \
    -drive file=disk.qcow2,if=virtio,format=qcow2 \
    -drive file=seed.img,media=cdrom,format=raw,readonly=on \
    -nic "user,model=virtio-net-pci,restrict=$1,hostfwd=tcp:127.0.0.1:22224-:22"
}
start_reference off
```

In a second workstation shell, define the connection and its working directory and check the SSH fingerprint against the cloud-init fingerprint in the QEMU serial output before accepting it.

```bash
vm_dir="${XDG_DATA_HOME:-$HOME/.local/share}/writ-vms"
ref=writ-ref-1
cd "$vm_dir/$ref"
ref_ssh() {
  ssh -F /dev/null -o IdentitiesOnly=yes -o IdentityAgent=none -o ForwardAgent=no \
    -o UserKnownHostsFile="$PWD/known_hosts" -i "$PWD/guest-access" \
    -p 22224 vm-operator@127.0.0.1 "$@"
}
ref_ssh 'cloud-init status --wait'
ref_ssh 'sudo apt-get update && sudo apt-get install -y --no-install-recommends acl apparmor-utils bubblewrap busybox-static dbus-x11 dmeventd dmsetup e2fsprogs gnome-keyring xauth xvfb'
ref_ssh 'sudo useradd -m -s /bin/bash writ-human && sudo chmod 0700 /home/writ-human'
ref_ssh 'systemd-detect-virt --vm; stat -fc %T /sys/fs/cgroup; cat /sys/fs/cgroup/cgroup.controllers; sysctl kernel.apparmor_restrict_unprivileged_userns; findmnt -no FSTYPE /'
ref_ssh 'sudo systemctl poweroff'
```

Expect `kvm`, `cgroup2fs` with `cpu memory pids`, user namespace restriction `1` and `ext4`. With QEMU stopped, snapshot the clean, unmarked guest. The snapshot includes the installed packages and disposable human account.

```bash
cd "$vm_dir/$ref"
qemu-img snapshot -c fresh disk.qcow2
qemu-img snapshot -l disk.qcow2
```

## Owner designation and guest trials

In the QEMU shell, run `start_reference on`. The owner opens an SSH session with `ref_ssh`, verifies `hostname`, virtualization and the guest image, then enters `sudo -i`. The owner alone runs the checks and writes the marker inside that guest after confirming that rollback is available.

```bash
(
  set -e
  test "$(id -u)" = 0
  test "$(hostname)" = writ-ref-1
  test "$(systemd-detect-virt --vm)" = kvm
  . /etc/os-release
  test "$ID:$VERSION_ID" = ubuntu:24.04
  test ! -e /etc/writ-disposable-vm
  test ! -L /etc/writ-disposable-vm
  umask 077
  set -o noclobber
  printf 'writ-disposable-vm/1\nname=writ-ref-1\n' > /etc/writ-disposable-vm
) || exit 1
stat -c '%U:%G %a %F' /etc/writ-disposable-vm
```

Expect `root:root 600 regular file`. Stop after any failed check. The marker belongs to this disposable guest and is absent from the clean snapshot and all seed images. Use the [reference trial commands](README.md#commands) for everything after designation. The separate `writ-human` account enables service configuration. The live desktop acceptance check still needs the actual session targets and negative controls listed in M5-INTERFACES J-9. A headless boot without those targets leaves P-AGENT-DESKTOP open.

## Export evidence, roll back and destroy

While the guest is running with restricted networking, review files within `/root/writ-evidence/` and assemble a root-owned tree at `/root/writ-evidence-sanitized/` containing only reviewed evidence files. Keep the exact SHA, per-check status and exit codes, required version and checksum facts, and failure diagnostics. Remove canaries, key material, credentials and any private content. Do this before and after teardown if the first export is needed to preserve failed trial output.

On the workstation, pull only the curated tree into a new archive, record its checksum and list its members without extracting them. Repeat with a new archive name after teardown if needed.

```bash
mkdir -p "$vm_dir/evidence"
archive="$vm_dir/evidence/${ref}-$(date -u +%Y%m%dT%H%M%SZ).tar"
ref_ssh 'sudo tar -C /root/writ-evidence-sanitized -cf - .' > "$archive"
tar -tf "$archive"
sha256sum "$archive" > "$archive.sha256"
ref_ssh 'sudo systemctl poweroff'
```

After QEMU exits, either roll back for another owner-designated reference trial or destroy the guest. The snapshot discards post-designation guest state, including the marker. To restore it while stopped, run:

```bash
cd "$vm_dir/$ref"
qemu-img snapshot -a fresh disk.qcow2
qemu-img check disk.qcow2
```

For destruction, verify QEMU has exited and the evidence archive has been received. The following removes precisely this reference clone's files, including the private guest access key. It keeps the verified base and exported evidence.

```bash
cd "$vm_dir/writ-ref-1"
rm -i -- disk.qcow2 seed.img user-data meta-data guest-access guest-access.pub known_hosts
cd "$vm_dir"
rmdir -- writ-ref-1
```

An isolated containment runner uses one new clone per reviewed job and is destroyed after each attempt, including failure and cancellation. It has separate registration, release criteria and external cleanup. The reference clone procedure stops short of registering a GitHub Actions runner.
