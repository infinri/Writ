#!/usr/bin/env bash
# Writ M5 reference-VM setup (M5a-03; D-045 M5-D2, M5-D3, M5-D12; J-1, J-9 revision 2).
# PREPARED, NOT RUN. Runs only inside the owner-designated disposable Ubuntu 24.04 VM.
#
# usage: setup.sh --designated-vm NAME --probe-binary PATH --image-name NAME --image-sha256 HEX
#                 [--agent-user NAME] [--job-user NAME]
#
# --agent-user (default writ-agent) and --job-user (default none) are PROVISIONAL: J-1 is
# blocked on owner decisions C-1 (agent uid) and H-2 (job identity). The job user is created
# only when given (H-2 option (a)); nothing uses it yet.
#
# Keeps kernel.apparmor_restrict_unprivileged_userns = 1 and grants `userns` only to a private
# bubblewrap copy (root:writ 0750, digest recorded) through an AppArmor profile attached by
# path (J-9). Creates the J-1 identities and layout, records filesystem UUIDs and features,
# installs writ-probe, plants a probe canary (not a key) and records versions. Every change is
# listed in /var/lib/writ-reference-vm.state so teardown.sh can reverse it.
set -euo pipefail
here=$(cd "$(dirname "$0")" && pwd)
# shellcheck source=common.sh
. "$here/common.sh"
writ_parse_designated "$@"
writ_guard "$WRIT_DESIGNATED"

probe="" image_name="" image_sha="" agent_user=writ-agent job_user=""
set -- "${WRIT_ARGS[@]+"${WRIT_ARGS[@]}"}"
while [ "$#" -gt 0 ]; do
  case "$1" in
    --probe-binary) probe="$2"; shift 2 ;;
    --image-name) image_name="$2"; shift 2 ;;
    --image-sha256) image_sha="$2"; shift 2 ;;
    --agent-user) agent_user="$2"; shift 2 ;;
    --job-user) job_user="$2"; shift 2 ;;
    *) echo "unknown argument $1" >&2; exit 2 ;;
  esac
done
[ -f "$probe" ] || { echo "--probe-binary must name the built writ-probe" >&2; exit 2; }
[ -n "$image_name" ] || { echo "--image-name required" >&2; exit 2; }
printf '%s' "$image_sha" | grep -Eqx '[0-9a-f]{64}' || { echo "--image-sha256 must be 64 lowercase hex" >&2; exit 2; }
for u in "$agent_user" $job_user; do
  printf '%s' "$u" | grep -Eqx '[a-z_][a-z0-9_-]{0,30}' || { echo "invalid user name $u" >&2; exit 2; }
done
[ ! -e "$WRIT_STATE_FILE" ] || { echo "already set up ($WRIT_STATE_FILE exists); run teardown.sh first" >&2; exit 2; }
for p in /var/lib/writ /etc/writ /run/writ /srv/writ /etc/tmpfiles.d/writ.conf "$WRIT_LIBEXEC" "$WRIT_APPARMOR_PROFILE"; do
  [ ! -e "$p" ] || { echo "$p already exists; setup only runs on a fresh image" >&2; exit 2; }
done

# 1. Required host policy (J-9): cgroup v2, and the AppArmor userns restriction stays set.
[ "$(stat -f -c %T /sys/fs/cgroup)" = cgroup2fs ] || { echo "cgroup v2 unified hierarchy not mounted" >&2; exit 4; }
[ "$(sysctl -n kernel.apparmor_restrict_unprivileged_userns 2>/dev/null || echo 0)" = 1 ] \
  || { echo "kernel.apparmor_restrict_unprivileged_userns is not 1; J-9 requires it set; stop and ask the owner" >&2; exit 4; }
aa-enabled -q 2>/dev/null || [ "$(cat /sys/module/apparmor/parameters/enabled 2>/dev/null)" = Y ] \
  || { echo "AppArmor not enabled" >&2; exit 4; }

ev=$(evidence_dir setup)
record_versions "$ev/versions-before.txt"
state() { echo "$1" >> "$WRIT_STATE_FILE"; }
: > "$WRIT_STATE_FILE"
chmod 0600 "$WRIT_STATE_FILE"
state "setting agent_user $agent_user"
[ -z "$job_user" ] || state "setting job_user $job_user"

# 2. bubblewrap from the archive (not setuid on Ubuntu); the system binary stays restricted.
if ! dpkg-query -W -f '${Status}' bubblewrap 2>/dev/null | grep -q 'install ok installed'; then
  apt-get update
  DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends bubblewrap
  state "package bubblewrap"
fi
[ ! -u /usr/bin/bwrap ] || { echo "/usr/bin/bwrap is setuid; not supported" >&2; exit 4; }

# 3. Identities (J-1, provisional names for the agent and job users).
if ! getent group writ >/dev/null; then groupadd --system writ; state "group writ"; fi
if ! getent group writ-agents >/dev/null; then groupadd --system writ-agents; state "group writ-agents"; fi
if ! getent passwd writ >/dev/null; then
  useradd --system --gid writ --home-dir /var/lib/writ --no-create-home --shell /usr/sbin/nologin writ
  state "user writ"
fi
if ! getent passwd "$agent_user" >/dev/null; then
  useradd --create-home --shell /bin/bash --groups writ-agents "$agent_user"
  state "user $agent_user"
fi
if [ -n "$job_user" ] && ! getent passwd "$job_user" >/dev/null; then
  useradd --system --no-create-home --home-dir /nonexistent --shell /usr/sbin/nologin "$job_user"
  state "user $job_user"
fi
bad=$(id -nG "$agent_user" | tr ' ' '\n' | grep -Ex 'writ|sudo|adm|admin|wheel|systemd-journal|docker|lxd|disk|shadow' || true)
[ -z "$bad" ] || { echo "$agent_user is in forbidden groups: $bad" >&2; exit 4; }

# 4. Layout and modes (J-1). No ACLs (verified by writ-probe P-ACL).
mkd() { install -d -m "$1" -o "$2" -g "$3" "$4"; state "dir $4"; }
mkd 0700 writ writ /var/lib/writ
mkd 0700 writ writ /var/lib/writ/ledger
mkd 0700 writ writ /var/lib/writ/stores
mkd 0700 writ writ /var/lib/writ/keys
mkd 0700 writ writ /var/lib/writ/run
mkd 0755 root root /srv/writ
mkd 0755 writ writ /srv/writ/targets
mkd 0750 root writ /etc/writ
cat > /etc/tmpfiles.d/writ.conf <<'EOF'
d /run/writ 0750 writ writ-agents -
d /run/writ/state 0700 writ writ -
EOF
state "file /etc/tmpfiles.d/writ.conf"
systemd-tmpfiles --create /etc/tmpfiles.d/writ.conf
state "dir /run/writ"

# 5. Private bubblewrap copy and its AppArmor userns grant (J-9). Profiles attach by path, so
#    /usr/bin/bwrap and any copy elsewhere stay restricted; the agent cannot execute this copy.
mkd 0755 root root "$WRIT_LIBEXEC"
install -m 0750 -o root -g writ /usr/bin/bwrap "$WRIT_PRIVATE_BWRAP"
state "file $WRIT_PRIVATE_BWRAP"
cat > "$WRIT_APPARMOR_PROFILE" <<EOF
abi <abi/4.0>,
include <tunables/global>

profile writ-bwrap $WRIT_PRIVATE_BWRAP flags=(unconfined) {
  userns,

  include if exists <local/writ-bwrap>
}
EOF
state "apparmor $WRIT_APPARMOR_PROFILE"
apparmor_parser -r "$WRIT_APPARMOR_PROFILE"
sha256sum "$WRIT_PRIVATE_BWRAP" | cut -d' ' -f1 > /etc/writ/bwrap.sha256
sha256sum "$WRIT_APPARMOR_PROFILE" | cut -d' ' -f1 > /etc/writ/apparmor-profile.sha256
chown root:writ /etc/writ/bwrap.sha256 /etc/writ/apparmor-profile.sha256
chmod 0640 /etc/writ/bwrap.sha256 /etc/writ/apparmor-profile.sha256
state "file /etc/writ/bwrap.sha256"
state "file /etc/writ/apparmor-profile.sha256"

# 6. Filesystem record (J-7.2, J-9 P-FS-TYPE): UUID and features, read as root.
: > /etc/writ/filesystems
for p in /var/lib/writ /srv/writ/targets; do
  src=$(findmnt -no SOURCE --target "$p"); fstype=$(findmnt -no FSTYPE --target "$p"); uuid=$(findmnt -no UUID --target "$p")
  case "$fstype" in
    ext4) feats=$(dumpe2fs -h "$src" 2>/dev/null | sed -n 's/^Filesystem features:[[:space:]]*//p' | tr ' ' ',') ;;
    xfs) feats=xfs ;;
    *) echo "$p is on $fstype, not on the allow-list (ext4 with journal or xfs)" >&2; exit 4 ;;
  esac
  echo "path=$p uuid=$uuid features=$feats" >> /etc/writ/filesystems
done
chown root:writ /etc/writ/filesystems; chmod 0640 /etc/writ/filesystems
state "file /etc/writ/filesystems"

# 7. Reference-image record and the probe canary (random, not a key).
{
  echo "image=$image_name"
  echo "sha256=$image_sha"
  echo "recorded_utc=$(date -u +%Y-%m-%dT%H:%M:%SZ)"
} > /etc/writ/reference-image
chown root:writ /etc/writ/reference-image; chmod 0640 /etc/writ/reference-image
state "file /etc/writ/reference-image"
printf 'WRIT-CANARY-%s\n' "$(head -c 24 /dev/urandom | od -An -tx1 | tr -d ' \n')" > /etc/writ/probe-canary
chown root:writ /etc/writ/probe-canary; chmod 0640 /etc/writ/probe-canary
state "file /etc/writ/probe-canary"

# 8. writ-probe (root-owned, executable by everyone; it holds no secret).
install -m 0755 -o root -g root "$probe" "$WRIT_LIBEXEC/writ-probe"
state "file $WRIT_LIBEXEC/writ-probe"

# 9. Evidence.
record_versions "$ev/versions-after.txt"
sha256sum "$WRIT_LIBEXEC/writ-probe" "$WRIT_PRIVATE_BWRAP" "$WRIT_APPARMOR_PROFILE" > "$ev/digests.txt"
cp "$WRIT_APPARMOR_PROFILE" "$ev/apparmor-writ-bwrap.txt"
cp /etc/writ/filesystems "$ev/filesystems.txt"
stat -c '%n %a %U:%G %F' /var/lib/writ /var/lib/writ/* /srv/writ /srv/writ/targets /etc/writ /etc/writ/* \
  /run/writ /run/writ/state "$WRIT_LIBEXEC" "$WRIT_LIBEXEC"/* > "$ev/layout.txt"
{ id writ; id "$agent_user"; [ -z "$job_user" ] || id "$job_user"; } > "$ev/identities.txt"
cp "$WRIT_STATE_FILE" "$ev/state.txt"
echo "setup complete; evidence in $ev"
