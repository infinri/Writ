#!/usr/bin/env bash
# Writ M5 reference-VM teardown (M5a-03; D-045 M5-D3, M5-D12). PREPARED, NOT RUN.
# Runs only inside the owner-designated disposable Debian 13 VM.
#
# usage: teardown.sh --designated-vm NAME
#
# Reverses exactly what setup.sh recorded in /var/lib/writ-reference-vm.state, newest first:
# stops leftover probe units, removes files and directories, users and groups, and purges
# bubblewrap only if setup installed it. Evidence under /root/writ-evidence is kept (copy it
# off the VM first); rolling the VM back to its snapshot is the stronger reset.
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

for unit in $(systemctl list-units --all --plain --no-legend 'writ-probe-*' 'writ-placeholder-*' | awk '{print $1}'); do
  systemctl stop "$unit" || true
done

tac "$WRIT_STATE_FILE" | while read -r kind path; do
  case "$kind" in
    file) rm -f -- "$path" ;;
    dir) case "$path" in
           /var/lib/writ|/var/lib/writ/*|/etc/writ|/run/writ|"$WRIT_LIBEXEC") rm -rf -- "$path" ;;
           *) echo "refusing to remove unexpected directory $path" >&2 ;;
         esac ;;
    user)
      if getent passwd "$path" >/dev/null; then
        # writ's home is /var/lib/writ (removed above); writ-agent's home was created by setup.
        if [ "$path" = writ-agent ]; then userdel --remove "$path"; else userdel "$path"; fi
      fi ;;
    group) if getent group "$path" >/dev/null; then groupdel "$path"; fi ;;
    package) DEBIAN_FRONTEND=noninteractive apt-get purge -y "$path" ;;
    *) echo "unknown state entry: $kind $path" >&2 ;;
  esac
done
rm -f -- "$WRIT_STATE_FILE"

{
  for p in /var/lib/writ /etc/writ /run/writ /etc/tmpfiles.d/writ.conf "$WRIT_LIBEXEC"; do
    [ -e "$p" ] && echo "LEFT $p" || echo "removed $p"
  done
  getent passwd writ >/dev/null && echo "LEFT user writ" || echo "removed user writ"
  getent passwd writ-agent >/dev/null && echo "LEFT user writ-agent" || echo "removed user writ-agent"
  getent group writ >/dev/null && echo "LEFT group writ" || echo "removed group writ"
} | tee "$ev/after.txt"
! grep -q '^LEFT' "$ev/after.txt" || { echo "teardown incomplete; roll the VM back to its snapshot" >&2; exit 1; }
echo "teardown complete; evidence in $ev"
