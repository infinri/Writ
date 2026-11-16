# Shared guard and helpers for the Writ M5 reference-VM scripts (M5a-03; D-045 M5-D3, M5-D12;
# M5-INTERFACES J-1, J-9 revision 2). Sourced by setup.sh, teardown.sh and trial.sh; never
# executed on its own.
#
# Every script refuses to run (exit 3, before any change) unless ALL of these hold:
#   1. the marker file /etc/writ-disposable-vm exists, is a regular root-owned file not writable
#      by group or others, and its first line is exactly "writ-disposable-vm/1";
#   2. its line "name=<vm name>" equals the value given with --designated-vm <vm name>;
#   3. the effective user is root;
#   4. systemd-detect-virt --vm reports a virtual machine;
#   5. /etc/os-release says Ubuntu 24.04 (the J-9 revision 2 reference image).
# The owner creates the marker by hand inside the freshly provisioned VM (README step 2); no
# script creates it. A workstation never has it.

WRIT_VM_MARKER=/etc/writ-disposable-vm
WRIT_STATE_FILE=/var/lib/writ-reference-vm.state
WRIT_LIBEXEC=/usr/local/libexec/writ
WRIT_PRIVATE_BWRAP=$WRIT_LIBEXEC/bwrap
WRIT_APPARMOR_PROFILE=/etc/apparmor.d/writ-bwrap
WRIT_EVIDENCE_ROOT=/root/writ-evidence

refuse() {
  echo "REFUSED: $*" >&2
  echo "These scripts run only inside the owner-designated disposable Ubuntu 24.04 VM (see m5/reference-vm/README.md)." >&2
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
  # A literal line comparison (M5a-13 L12): the name is never a pattern.
  grep -qxF -- "name=$designated" "$WRIT_VM_MARKER" || refuse "marker does not name this VM as '$designated'"
  [ "$(id -u)" = 0 ] || refuse "must run as root inside the VM"
  local virt
  virt=$(systemd-detect-virt --vm 2>/dev/null || true)
  [ -n "$virt" ] && [ "$virt" != none ] || refuse "not a virtual machine (systemd-detect-virt --vm: '${virt:-unknown}')"
  # shellcheck disable=SC1091
  . /etc/os-release
  [ "${ID:-}" = ubuntu ] && [ "${VERSION_ID:-}" = 24.04 ] || refuse "not Ubuntu 24.04 (found ${PRETTY_NAME:-unknown})"
  echo "guard: designated disposable VM '$designated' ($virt, $PRETTY_NAME)"
}

# Parse --designated-vm out of "$@"; the remaining arguments are left in WRIT_ARGS (array).
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

# Whether teardown may remove PATH recorded as KIND (M5a-13 L12): absolute, without `.`, `..`
# or empty components, and inside the locations setup.sh and service-install.sh record.
writ_path_allowed() {
  local kind="$1" p="$2"
  case "$p" in /*) ;; *) return 1 ;; esac
  case "$p/" in *//*|*/./*|*/../*) return 1 ;; esac
  case "$kind" in
    file)
      case "$p" in
        /etc/tmpfiles.d/writ.conf|/etc/tmpfiles.d/writ-launcher.conf|/etc/writ/?*|"$WRIT_LIBEXEC"/?*|\
        /var/lib/writ/?*|/var/lib/writ-jobs.img|/etc/writ-launcher/?*|/etc/sysctl.d/60-writ-core.conf|\
        /etc/systemd/system/writ.service|/etc/systemd/system/writ-host.socket|\
        /etc/systemd/system/writ-admin.socket|/etc/systemd/system/writ-approval.socket|\
        /etc/systemd/system/writ-job-launcher.socket|/etc/systemd/system/writ-job-launcher@.service|\
        /etc/systemd/system/writ-jobs.slice) return 0 ;;
      esac ;;
    dir)
      case "$p" in
        /var/lib/writ|/var/lib/writ/?*|/etc/writ|/run/writ|/srv/writ|/srv/writ/?*|"$WRIT_LIBEXEC"|\
        /var/lib/writ-jobs|/etc/writ-launcher|/run/writ-launcher|/usr/lib/writ-runtime) return 0 ;;
      esac ;;
    apparmor) [ "$p" = "$WRIT_APPARMOR_PROFILE" ] && return 0 ;;
    sysctlfile) [ "$p" = /etc/sysctl.d/60-writ-core.conf ] && return 0 ;;
    mount|fstab) [ "$p" = /var/lib/writ-jobs ] && return 0 ;;
  esac
  return 1
}

# The bytes of STRING as lowercase hex (state-file safe: no spaces, any byte but NUL).
writ_hex() { printf '%s' "$1" | od -An -v -tx1 | tr -d ' \n'; }

# The string whose hex is HEX (refuses anything but pairs of hex digits).
writ_unhex() {
  printf '%s' "$1" | grep -Eqx '([0-9a-f]{2})*' || return 1
  printf '%b' "$(printf '%s' "$1" | sed 's/../\\x&/g')"
}

# Value recorded by setup.sh in the state file ("setting <key> <value>").
state_setting() {
  awk -v k="$1" '$1 == "setting" && $2 == k { print $3 }' "$WRIT_STATE_FILE" | tail -n 1
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
    echo "bwrap_system_sha256=$(sha256sum /usr/bin/bwrap 2>/dev/null | cut -d' ' -f1)"
    echo "bwrap_private_sha256=$(sha256sum "$WRIT_PRIVATE_BWRAP" 2>/dev/null | cut -d' ' -f1)"
    echo "bwrap_private_mode=$(stat -c '%a %U:%G' "$WRIT_PRIVATE_BWRAP" 2>/dev/null || echo absent)"
    echo "apparmor_profile_sha256=$(sha256sum "$WRIT_APPARMOR_PROFILE" 2>/dev/null | cut -d' ' -f1)"
    echo "apparmor_profile_loaded=$(grep -c '^writ-bwrap ' /sys/kernel/security/apparmor/profiles 2>/dev/null || echo unknown)"
    dpkg-query -W -f 'package ${Package}=${Version}\n' bubblewrap systemd util-linux coreutils libc6 apparmor 2>/dev/null
    echo "cgroup_fs=$(stat -f -c %T /sys/fs/cgroup)"
    echo "cgroup_controllers=$(cat /sys/fs/cgroup/cgroup.controllers)"
    for s in kernel.apparmor_restrict_unprivileged_userns kernel.apparmor_restrict_unprivileged_unconfined \
             user.max_user_namespaces kernel.yama.ptrace_scope fs.protected_symlinks fs.protected_hardlinks \
             fs.suid_dumpable kernel.core_pattern net.ipv4.ip_unprivileged_port_start; do
      echo "sysctl $s=$(sysctl -n "$s" 2>/dev/null || echo absent)"
    done
    echo "lsm=$(cat /sys/kernel/security/lsm 2>/dev/null || echo unreadable)"
    for p in /var/lib /srv; do
      echo "fs $p $(findmnt -no SOURCE,FSTYPE,UUID,OPTIONS --target "$p")"
    done
  } > "$out"
}
