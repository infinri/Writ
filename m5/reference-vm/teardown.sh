#!/usr/bin/env bash
# Writ M5 reference-VM teardown (M5a-03, M5a-07; D-045 M5-D3, M5-D12). PREPARED, NOT RUN.
# Runs only inside the owner-designated disposable Ubuntu 24.04 VM.
#
# usage: teardown.sh --designated-vm NAME
#
# Reverses exactly what setup.sh recorded in /var/lib/writ-reference-vm.state, newest first:
# stops leftover probe and placeholder units, stops and removes the launcher units, unmounts
# the jobs filesystem and removes its fstab line, restores kernel.core_pattern and apport,
# unloads the AppArmor profiles, removes files and directories, users and groups, and purges
# packages only if setup installed them. Evidence
# under /root/writ-evidence is kept (copy it off the VM first); rolling the VM back to its
# snapshot is the stronger reset.
set -euo pipefail
here=$(cd "$(dirname "$0")" && pwd)
# shellcheck source=common.sh
. "$here/common.sh"
writ_parse_designated "$@"
writ_guard "$WRIT_DESIGNATED"
[ "${#WRIT_ARGS[@]}" = 0 ] || { echo "unexpected arguments: ${WRIT_ARGS[*]}" >&2; exit 2; }
[ -f "$WRIT_STATE_FILE" ] || { echo "nothing to tear down ($WRIT_STATE_FILE absent)" >&2; exit 0; }

ev=$(evidence_dir teardown)
cp "$WRIT_STATE_FILE" "$ev/state-before.txt"
agent_user=$(state_setting agent_user)
cp "$WRIT_STATE_FILE" "$ev/state-copy.txt"
state_setting() {
  awk -v k="$1" '$1 == "setting" && $2 == k { print $3 }' "$ev/state-copy.txt" | tail -n 1
}

for unit in $(systemctl list-units --all --plain --no-legend 'writ-probe-*' 'writ-placeholder-*' 'writ-job-launcher@*' 'writ-escape-*' | awk '{print $1}'); do
  systemctl stop "$unit" || true
done

tac "$WRIT_STATE_FILE" | while read -r kind path extra; do
  case "$kind" in
    setting) ;;
    file) rm -f -- "$path"
          case "$path" in /etc/systemd/system/*) systemctl daemon-reload || true ;; esac ;;
    unit)
      case "$path" in
        writ.service) systemctl disable --now "$path" 2>/dev/null || true ;;  # service-install.sh (M5a-04)
        writ-job-launcher.socket|writ-job-launcher@.service|writ-jobs.slice)
          systemctl disable --now "$path" 2>/dev/null || true
          rm -f -- "/etc/systemd/system/$path"
          systemctl daemon-reload ;;
        *) echo "refusing to stop unexpected unit $path" >&2 ;;
      esac ;;
    mount) if mountpoint -q "$path"; then umount "$path"; fi ;;
    fstab)
      [ "$path" = /var/lib/writ-jobs ] && sed -i '\|^/var/lib/writ-jobs.img /var/lib/writ-jobs |d' /etc/fstab ;;
    sysctlfile)
      rm -f -- "$path"
      [ -z "$extra" ] || sysctl -q -w "kernel.core_pattern=$(printf '%s' "$extra" | tr '_' ' ')" || true ;;
    enable) systemctl enable --now "$path" || true ;;
    apparmor)
      if [ -f "$path" ]; then apparmor_parser -R "$path" || true; fi
      rm -f -- "$path" ;;
    dir) case "$path" in
           /var/lib/writ|/var/lib/writ/*|/etc/writ|/run/writ|/srv/writ|/srv/writ/*|"$WRIT_LIBEXEC"|\
           /var/lib/writ-jobs|/etc/writ-launcher|/run/writ-launcher|/usr/lib/writ-runtime) rm -rf -- "$path" ;;
           *) echo "refusing to remove unexpected directory $path" >&2 ;;
         esac ;;
    user)
      if getent passwd "$path" >/dev/null; then
        # writ's home is /var/lib/writ (removed above); the agent user's home was created by setup.
        if [ "$path" = "$agent_user" ]; then userdel --remove "$path"; else userdel "$path"; fi
      fi ;;
    group) if getent group "$path" >/dev/null; then groupdel "$path"; fi ;;
    package) DEBIAN_FRONTEND=noninteractive apt-get purge -y "$path" ;;
    *) echo "unknown state entry: $kind $path" >&2 ;;
  esac
done
rm -f -- "$WRIT_STATE_FILE"

{
  for p in /var/lib/writ /etc/writ /run/writ /srv/writ /etc/tmpfiles.d/writ.conf "$WRIT_LIBEXEC" "$WRIT_APPARMOR_PROFILE" \
           /etc/writ-launcher /var/lib/writ-jobs /var/lib/writ-jobs.img /usr/lib/writ-runtime /run/writ-launcher \
           /etc/tmpfiles.d/writ-launcher.conf /etc/systemd/system/writ-job-launcher.socket \
           /etc/systemd/system/writ-job-launcher@.service /etc/systemd/system/writ-jobs.slice /etc/sysctl.d/60-writ-core.conf /etc/systemd/system/writ.service; do
    if [ -e "$p" ]; then echo "LEFT $p"; else echo "removed $p"; fi
  done
  if grep -q '^writ-bwrap ' /sys/kernel/security/apparmor/profiles 2>/dev/null; then echo "LEFT apparmor profile writ-bwrap"; else echo "removed apparmor profile writ-bwrap"; fi
  if grep -q '^writ-job-child ' /sys/kernel/security/apparmor/profiles 2>/dev/null; then echo "LEFT apparmor profile writ-job-child"; else echo "removed apparmor profile writ-job-child"; fi
  if grep -q '/var/lib/writ-jobs' /etc/fstab; then echo "LEFT fstab line /var/lib/writ-jobs"; else echo "removed fstab line /var/lib/writ-jobs"; fi
  job_user=$(state_setting job_user)
  for u in writ "$agent_user" ${job_user:+"$job_user"}; do
    if getent passwd "$u" >/dev/null; then echo "LEFT user $u"; else echo "removed user $u"; fi
  done
  for g in writ writ-agents; do
    if getent group "$g" >/dev/null; then echo "LEFT group $g"; else echo "removed group $g"; fi
  done
} | tee "$ev/after.txt"
! grep -q '^LEFT' "$ev/after.txt" || { echo "teardown incomplete; roll the VM back to its snapshot" >&2; exit 1; }
echo "teardown complete; evidence in $ev"
