# Shared guard and helpers for the Writ M5 reference-VM scripts (M5a-03, D-045 M5-D3, M5-D12).
# Sourced by setup.sh, teardown.sh and trial.sh; never executed on its own.
#
# Every script refuses to run unless ALL of these hold:
#   1. the marker file /etc/writ-disposable-vm exists, is a regular root-owned file not writable
#      by group or others, and its first line is exactly "writ-disposable-vm/1";
#   2. its line "name=<vm name>" equals the value given with --designated-vm <vm name>;
#   3. the effective user is root (setup, teardown, trial all change system state);
#   4. systemd-detect-virt reports a virtual machine (not "none", not a container);
#   5. /etc/os-release says Debian 13 (trixie).
# The owner creates the marker by hand inside the freshly provisioned VM (README step 2); no
# script creates it. A workstation never has it.

WRIT_VM_MARKER=/etc/writ-disposable-vm
WRIT_STATE_FILE=/var/lib/writ-reference-vm.state
WRIT_LIBEXEC=/usr/local/libexec/writ
WRIT_EVIDENCE_ROOT=/root/writ-evidence

refuse() {
  echo "REFUSED: $*" >&2
  echo "These scripts run only inside the owner-designated disposable Debian 13 VM (see m5/reference-vm/README.md)." >&2
  exit 3
}

writ_guard() {
  local designated="$1"
  [ -n "$designated" ] || refuse "missing --designated-vm <vm name>"
  [ -f "$WRIT_VM_MARKER" ] && [ ! -L "$WRIT_VM_MARKER" ] || refuse "marker $WRIT_VM_MARKER absent"
  [ "$(stat -c %u "$WRIT_VM_MARKER")" = 0 ] || refuse "marker not owned by root"
  case "$(stat -c %a "$WRIT_VM_MARKER")" in
    600|640|644|400|440|444) ;;
    *) refuse "marker mode $(stat -c %a "$WRIT_VM_MARKER") is group- or other-writable or unexpected" ;;
  esac
  [ "$(head -n 1 "$WRIT_VM_MARKER")" = "writ-disposable-vm/1" ] || refuse "marker first line is not writ-disposable-vm/1"
  grep -qx "name=$designated" "$WRIT_VM_MARKER" || refuse "marker does not name this VM as '$designated'"
  [ "$(id -u)" = 0 ] || refuse "must run as root inside the VM"
  local virt
  virt=$(systemd-detect-virt --vm 2>/dev/null || true)
  [ -n "$virt" ] && [ "$virt" != none ] || refuse "not a virtual machine (systemd-detect-virt --vm: '${virt:-unknown}')"
  # shellcheck disable=SC1091
  . /etc/os-release
  [ "${ID:-}" = debian ] && [ "${VERSION_ID:-}" = 13 ] || refuse "not Debian 13 (found ${PRETTY_NAME:-unknown})"
  echo "guard: designated disposable VM '$designated' ($virt, $PRETTY_NAME)"
}

# Parse the common --designated-vm option out of "$@"; the remaining arguments are left in
# WRIT_ARGS (array).
writ_parse_designated() {
  WRIT_DESIGNATED=""
  WRIT_ARGS=()
  while [ "$#" -gt 0 ]; do
    case "$1" in
      --designated-vm) [ "$#" -ge 2 ] || refuse "--designated-vm needs a value"; WRIT_DESIGNATED="$2"; shift 2 ;;
      *) WRIT_ARGS+=("$1"); shift ;;
    esac
  done
}

evidence_dir() {
  local d
  d="$WRIT_EVIDENCE_ROOT/$(date -u +%Y%m%dT%H%M%SZ)-$1"
  install -d -m 0700 "$d"
  echo "$d"
}

record_versions() {
  local out="$1"
  {
    echo "date_utc=$(date -u +%Y-%m-%dT%H:%M:%SZ)"
    echo "os_release=$(. /etc/os-release && echo "$PRETTY_NAME")"
    echo "uname_r=$(uname -r)"
    echo "uname_v=$(uname -v)"
    echo "kernel_package=$(dpkg-query -W -f '${Package}=${Version}' "linux-image-$(uname -r)" 2>/dev/null || echo unknown)"
    echo "systemd=$(systemctl --version | head -n 1)"
    echo "bwrap=$(bwrap --version 2>/dev/null || echo absent)"
    echo "bwrap_sha256=$(sha256sum /usr/bin/bwrap 2>/dev/null | cut -d' ' -f1)"
    echo "bwrap_mode=$(stat -c '%a %U:%G' /usr/bin/bwrap 2>/dev/null)"
    dpkg-query -W -f 'package ${Package}=${Version}\n' bubblewrap systemd util-linux coreutils libc6 2>/dev/null
    echo "cgroup_fs=$(stat -f -c %T /sys/fs/cgroup)"
    echo "cgroup_controllers=$(cat /sys/fs/cgroup/cgroup.controllers)"
    for s in kernel.unprivileged_userns_clone user.max_user_namespaces kernel.yama.ptrace_scope \
             fs.protected_symlinks fs.protected_hardlinks kernel.apparmor_restrict_unprivileged_userns; do
      echo "sysctl $s=$(sysctl -n "$s" 2>/dev/null || echo absent)"
    done
    echo "apparmor=$(cat /sys/module/apparmor/parameters/enabled 2>/dev/null || echo absent)"
    echo "lsm=$(cat /sys/kernel/security/lsm 2>/dev/null || echo unreadable)"
    echo "var_lib_fs=$(stat -f -c %T /var/lib)"
    findmnt -no FSTYPE,OPTIONS --target /var/lib
  } > "$out"
}
