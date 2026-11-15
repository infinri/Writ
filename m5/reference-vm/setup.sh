#!/usr/bin/env bash
# Writ M5 reference-VM setup (M5a-03; D-045 M5-D2, M5-D3, M5-D12; J-1, J-9).
# PREPARED, NOT RUN. Runs only inside the owner-designated disposable Debian 13 VM.
#
# usage: setup.sh --designated-vm NAME --probe-binary PATH --image-name NAME --image-sha256 HEX
#
# Creates the `writ` system user and group and a separate test agent user `writ-agent` (not in
# group writ), the J-1 directory layout and modes, installs bubblewrap from the Debian archive,
# installs writ-probe, plants a probe canary (not a key), and records versions as evidence.
# Every change is listed in /var/lib/writ-reference-vm.state so teardown.sh can reverse it.
set -euo pipefail
here=$(cd "$(dirname "$0")" && pwd)
# shellcheck source=common.sh
. "$here/common.sh"
writ_parse_designated "$@"
writ_guard "$WRIT_DESIGNATED"

probe="" image_name="" image_sha=""
set -- "${WRIT_ARGS[@]+"${WRIT_ARGS[@]}"}"
while [ "$#" -gt 0 ]; do
  case "$1" in
    --probe-binary) probe="$2"; shift 2 ;;
    --image-name) image_name="$2"; shift 2 ;;
    --image-sha256) image_sha="$2"; shift 2 ;;
    *) echo "unknown argument $1" >&2; exit 2 ;;
  esac
done
[ -f "$probe" ] || { echo "--probe-binary must name the built writ-probe" >&2; exit 2; }
[ -n "$image_name" ] || { echo "--image-name required" >&2; exit 2; }
printf '%s' "$image_sha" | grep -Eqx '[0-9a-f]{64}' || { echo "--image-sha256 must be 64 lowercase hex" >&2; exit 2; }
[ ! -e "$WRIT_STATE_FILE" ] || { echo "already set up ($WRIT_STATE_FILE exists); run teardown.sh first" >&2; exit 2; }
for p in /var/lib/writ /etc/writ /run/writ /etc/tmpfiles.d/writ.conf "$WRIT_LIBEXEC"; do
  [ ! -e "$p" ] || { echo "$p already exists; setup only runs on a fresh image" >&2; exit 2; }
done

ev=$(evidence_dir setup)
record_versions "$ev/versions-before.txt"
state() { echo "$1" >> "$WRIT_STATE_FILE"; }
: > "$WRIT_STATE_FILE"
chmod 0600 "$WRIT_STATE_FILE"

# 1. Capabilities the reference image must provide; setup reports and stops, it does not
#    change kernel policy (the owner decides on any change, D-045 M5-D12).
[ "$(stat -f -c %T /sys/fs/cgroup)" = cgroup2fs ] || { echo "cgroup v2 unified hierarchy not mounted" >&2; exit 4; }
if [ "$(sysctl -n kernel.apparmor_restrict_unprivileged_userns 2>/dev/null || echo 0)" = 1 ]; then
  echo "AppArmor restricts unprivileged user namespaces on this image; stop and ask the owner (M5-DEPENDENCY-REVIEW section 4)" >&2
  exit 4
fi

# 2. bubblewrap from the Debian archive (not setuid on Debian).
if ! dpkg-query -W -f '${Status}' bubblewrap 2>/dev/null | grep -q 'install ok installed'; then
  apt-get update
  DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends bubblewrap
  state "package bubblewrap"
fi
[ ! -u /usr/bin/bwrap ] || { echo "/usr/bin/bwrap is setuid; not supported" >&2; exit 4; }

# 3. Identities (J-1): system account writ, no login shell, home /var/lib/writ (not created here).
if ! getent group writ >/dev/null; then groupadd --system writ; state "group writ"; fi
if ! getent passwd writ >/dev/null; then
  useradd --system --gid writ --home-dir /var/lib/writ --no-create-home --shell /usr/sbin/nologin writ
  state "user writ"
fi
if ! getent passwd writ-agent >/dev/null; then
  useradd --create-home --shell /bin/bash writ-agent
  state "user writ-agent"
fi
if id -nG writ-agent | tr ' ' '\n' | grep -qx writ; then echo "writ-agent must not be in group writ" >&2; exit 4; fi

# 4. Layout and modes (J-1). No ACLs.
mkd() { install -d -m "$1" -o "$2" -g "$3" "$4"; state "dir $4"; }
mkd 0700 writ writ /var/lib/writ
mkd 0700 writ writ /var/lib/writ/ledger
mkd 0700 writ writ /var/lib/writ/stores
mkd 0700 writ writ /var/lib/writ/keys
mkd 0700 writ writ /var/lib/writ/run
mkd 0750 writ writ /var/lib/writ/targets
mkd 0750 root writ /etc/writ
cat > /etc/tmpfiles.d/writ.conf <<'EOF'
d /run/writ 0755 writ writ -
EOF
state "file /etc/tmpfiles.d/writ.conf"
systemd-tmpfiles --create /etc/tmpfiles.d/writ.conf
state "dir /run/writ"
# Owners, modes and the absence of ACLs are verified by writ-probe (P-AGENT-DENY), not here.

# 5. Reference-image record (read by writ-probe into every report) and the probe canary.
{
  echo "image=$image_name"
  echo "sha256=$image_sha"
  echo "recorded_utc=$(date -u +%Y-%m-%dT%H:%M:%SZ)"
} > /etc/writ/reference-image
chown root:writ /etc/writ/reference-image; chmod 0640 /etc/writ/reference-image
state "file /etc/writ/reference-image"
# Canary: random, not a key, used only to show that P-NO-SECRET detects planted material.
printf 'WRIT-CANARY-%s\n' "$(head -c 24 /dev/urandom | od -An -tx1 | tr -d ' \n')" > /etc/writ/probe-canary
chown root:writ /etc/writ/probe-canary; chmod 0640 /etc/writ/probe-canary
state "file /etc/writ/probe-canary"

# 6. writ-probe.
install -d -m 0755 -o root -g root "$WRIT_LIBEXEC"
state "dir $WRIT_LIBEXEC"
install -m 0755 -o root -g root "$probe" "$WRIT_LIBEXEC/writ-probe"
state "file $WRIT_LIBEXEC/writ-probe"

# 7. Evidence.
record_versions "$ev/versions-after.txt"
sha256sum "$WRIT_LIBEXEC/writ-probe" > "$ev/writ-probe.sha256"
stat -c '%n %a %U:%G %F' /var/lib/writ /var/lib/writ/* /etc/writ /etc/writ/* /run/writ > "$ev/layout.txt"
id writ > "$ev/identities.txt"; id writ-agent >> "$ev/identities.txt"
cp "$WRIT_STATE_FILE" "$ev/state.txt"
echo "setup complete; evidence in $ev"
