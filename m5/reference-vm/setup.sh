#!/usr/bin/env bash
# Writ M5 reference-VM setup (M5a-03, M5a-07; D-045 M5-D2, M5-D3, M5-D4, M5-D12; D-046 C-1,
# H-2; J-1, J-9, J-11 revision 6). PREPARED, NOT RUN. Runs only inside the owner-designated
# disposable Ubuntu 24.04 VM.
#
# usage: setup.sh --designated-vm NAME --probe-binary PATH --image-name NAME --image-sha256 HEX
#                 --launcher-binary PATH --exec-binary PATH
#                 [--agent-user NAME] [--job-user NAME] [--jobs-fs-size SIZE]
#
# --job-user defaults to writ-job (D-046 H-2: required). --launcher-binary and --exec-binary
# are writ-job-launcher and writ-cgroup-exec built with `--profile launcher` (panic = abort,
# no test-faults; checked through --build-info). --jobs-fs-size (default 1G) sizes the
# dedicated /var/lib/writ-jobs filesystem (a loop-mounted ext4 image, nosuid,nodev).
#
# Keeps kernel.apparmor_restrict_unprivileged_userns = 1 and grants `userns` only to a private
# bubblewrap copy (root:writ-job 0750, digest recorded) through the two-profile AppArmor file
# of J-11.5a attached by path. Creates the J-1 identities and layout, the launcher units,
# tmpfiles entry, configuration and jobs filesystem, a pinned runtime profile (`busybox`),
# disables apport and sets a non-pipe kernel.core_pattern, records filesystem UUIDs and
# features, installs writ-probe, plants a probe canary (not a key) and records versions. Every
# change is listed in /var/lib/writ-reference-vm.state so teardown.sh can reverse it.
set -euo pipefail
here=$(cd "$(dirname "$0")" && pwd)
# shellcheck source=common.sh
. "$here/common.sh"
writ_parse_designated "$@"
writ_guard "$WRIT_DESIGNATED"

probe="" image_name="" image_sha="" agent_user=writ-agent job_user=writ-job
launcher_bin="" exec_bin="" jobs_fs_size=1G
set -- "${WRIT_ARGS[@]+"${WRIT_ARGS[@]}"}"
while [ "$#" -gt 0 ]; do
  case "$1" in
    --probe-binary) probe="$2"; shift 2 ;;
    --image-name) image_name="$2"; shift 2 ;;
    --image-sha256) image_sha="$2"; shift 2 ;;
    --agent-user) agent_user="$2"; shift 2 ;;
    --job-user) job_user="$2"; shift 2 ;;
    --launcher-binary) launcher_bin="$2"; shift 2 ;;
    --exec-binary) exec_bin="$2"; shift 2 ;;
    --jobs-fs-size) jobs_fs_size="$2"; shift 2 ;;
    *) echo "unknown argument $1" >&2; exit 2 ;;
  esac
done
[ -f "$probe" ] || { echo "--probe-binary must name the built writ-probe" >&2; exit 2; }
[ -n "$job_user" ] || { echo "--job-user is required (D-046 H-2)" >&2; exit 2; }
printf '%s' "$jobs_fs_size" | grep -Eqx '[1-9][0-9]*[MG]' || { echo "--jobs-fs-size must look like 512M or 2G" >&2; exit 2; }
for b in "$launcher_bin" "$exec_bin"; do
  [ -f "$b" ] || { echo "--launcher-binary and --exec-binary must name the built binaries" >&2; exit 2; }
  info=$("$b" --build-info) || { echo "$b --build-info failed" >&2; exit 2; }
  printf '%s\n' "$info" | grep -qx 'panic_abort=true' || { echo "$b is not built with panic = abort (use --profile launcher)" >&2; exit 2; }
  printf '%s\n' "$info" | grep -qx 'test_faults=false' || { echo "$b is a test-faults build; never installed" >&2; exit 2; }
done
[ -n "$image_name" ] || { echo "--image-name required" >&2; exit 2; }
printf '%s' "$image_sha" | grep -Eqx '[0-9a-f]{64}' || { echo "--image-sha256 must be 64 lowercase hex" >&2; exit 2; }
for u in "$agent_user" "$job_user"; do
  printf '%s' "$u" | grep -Eqx '[a-z_][a-z0-9_-]{0,30}' || { echo "invalid user name $u" >&2; exit 2; }
done
[ ! -e "$WRIT_STATE_FILE" ] || { echo "already set up ($WRIT_STATE_FILE exists); run teardown.sh first" >&2; exit 2; }
for p in /var/lib/writ /etc/writ /run/writ /srv/writ /etc/tmpfiles.d/writ.conf "$WRIT_LIBEXEC" "$WRIT_APPARMOR_PROFILE" \
         /etc/writ-launcher /var/lib/writ-jobs /var/lib/writ-jobs.img /usr/lib/writ-runtime /etc/tmpfiles.d/writ-launcher.conf \
         /etc/systemd/system/writ-job-launcher.socket /etc/systemd/system/writ-job-launcher@.service \
         /etc/systemd/system/writ-jobs.slice /etc/sysctl.d/60-writ-core.conf; do
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
state "setting job_user $job_user"

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
if ! getent passwd "$job_user" >/dev/null; then
  # Own primary group, no supplementary groups, no other member (J-1).
  useradd --system --user-group --no-create-home --home-dir /nonexistent --shell /usr/sbin/nologin "$job_user"
  state "user $job_user"
  state "group $job_user"
fi
[ "$(id -G "$job_user" | wc -w)" = 1 ] || { echo "$job_user has supplementary groups" >&2; exit 4; }
bad=$(id -nG "$agent_user" | tr ' ' '\n' | grep -Ex 'writ|sudo|adm|admin|wheel|systemd-journal|docker|lxd|disk|shadow' || true)
[ -z "$bad" ] || { echo "$agent_user is in forbidden groups: $bad" >&2; exit 4; }

# 4. Layout and modes (J-1). No ACLs (verified by writ-probe P-ACL).
mkd() { install -d -m "$1" -o "$2" -g "$3" "$4"; state "dir $4"; }
mkd 0700 writ writ /var/lib/writ
mkd 0700 writ writ /var/lib/writ/ledger
mkd 0700 writ writ /var/lib/writ/stores
mkd 0700 writ writ /var/lib/writ/keys
# run/ is 0750 so the launcher reads sources by group writ through PID 1's bind (N9).
mkd 0750 writ writ /var/lib/writ/run
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
# Executable by the job uid only (D-046 H-2, J-1).
install -m 0750 -o root -g "$job_user" /usr/bin/bwrap "$WRIT_PRIVATE_BWRAP"
state "file $WRIT_PRIVATE_BWRAP"
# Two profiles (J-11.5a): writ-bwrap for the private copy, writ-job-child for its children.
install -m 0644 -o root -g root "$here/launcher/apparmor-writ-bwrap" "$WRIT_APPARMOR_PROFILE"
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

# 9. Job launcher (D-046 H-2; J-1, J-11). Binaries root-owned 0755; units, slice and tmpfiles
#    root-owned; configuration root:writ 0640; the jobs filesystem a dedicated ext4 image
#    mounted nosuid,nodev; a pinned runtime profile; apport off and a non-pipe core_pattern.
install -m 0755 -o root -g root "$launcher_bin" "$WRIT_LIBEXEC/writ-job-launcher"
state "file $WRIT_LIBEXEC/writ-job-launcher"
install -m 0755 -o root -g root "$exec_bin" "$WRIT_LIBEXEC/writ-cgroup-exec"
state "file $WRIT_LIBEXEC/writ-cgroup-exec"
truncate -s "$jobs_fs_size" /var/lib/writ-jobs.img
chmod 0600 /var/lib/writ-jobs.img
state "file /var/lib/writ-jobs.img"
mkfs.ext4 -q -F /var/lib/writ-jobs.img
install -d -m 0711 -o root -g root /var/lib/writ-jobs
state "dir /var/lib/writ-jobs"
echo "/var/lib/writ-jobs.img /var/lib/writ-jobs ext4 loop,nosuid,nodev 0 2" >> /etc/fstab
state "fstab /var/lib/writ-jobs"
mount /var/lib/writ-jobs
state "mount /var/lib/writ-jobs"
chown root:root /var/lib/writ-jobs; chmod 0711 /var/lib/writ-jobs
# Runtime profile `busybox` (static, no libraries): usr/bin/busybox and its applet links.
if ! dpkg-query -W -f '${Status}' busybox-static 2>/dev/null | grep -q 'install ok installed'; then
  DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends busybox-static
  state "package busybox-static"
fi
install -d -m 0755 -o root -g root /usr/lib/writ-runtime
state "dir /usr/lib/writ-runtime"
rt=/usr/lib/writ-runtime/busybox
install -d -m 0755 -o root -g root "$rt" "$rt/usr" "$rt/usr/bin"
install -m 0755 -o root -g root /bin/busybox "$rt/usr/bin/busybox"
for applet in sh cat ls id echo printf readlink sleep head grep find touch mkdir rm env true false; do
  ln -s busybox "$rt/usr/bin/$applet"
done
ln -s usr/bin "$rt/bin"
runtime_digest=$("$WRIT_LIBEXEC/writ-job-launcher" --runtime-manifest "$rt")
install -d -m 0750 -o root -g writ /etc/writ-launcher
state "dir /etc/writ-launcher"
writ_uid=$(id -u writ) job_uid=$(id -u "$job_user") job_gid=$(id -g "$job_user")
cat > /etc/writ-launcher/launcher.json <<EOF
{"contract":"writ-launcher-config/1",
 "writ_uid":$writ_uid,"job_uid":$job_uid,"job_gid":$job_gid,
 "maxima":{"wall_time_ms":600000,"memory_max_bytes":536870912,"pids_max":256,"cpu_max_quota_us":100000,
           "cpu_max_period_us":100000,"output_max_bytes":16777216,"input_max_bytes":268435456,
           "file_size_max_bytes":268435456,"nofile_max":1024,"sigpending_max":256},
 "request":{"max_args":64,"max_arg_bytes":4096,"max_env":64,"max_env_bytes":4096},
 "source":{"max_depth":32,"max_entries":20000},
 "runtime_profiles":[{"id":"busybox","digest":"$runtime_digest"}],
 "deadlines_ms":{"request":5000,"start":10000,"kill":5000,"cleanup":30000},
 "outbound_buffer_bytes":1048576,"status_drain_bytes":65536,"pre_gate_processes":2,
 "rlimits":{"nproc":256,"msgqueue":819200,"memlock":65536},
 "memory":{"launcher_bytes":67108864,"unit_max_bytes":603979776}}
EOF
chown root:writ /etc/writ-launcher/launcher.json; chmod 0640 /etc/writ-launcher/launcher.json
state "file /etc/writ-launcher/launcher.json"
install -m 0644 -o root -g root "$here/launcher/writ-launcher.tmpfiles.conf" /etc/tmpfiles.d/writ-launcher.conf
state "file /etc/tmpfiles.d/writ-launcher.conf"
systemd-tmpfiles --create /etc/tmpfiles.d/writ-launcher.conf
state "dir /run/writ-launcher"
for u in writ-jobs.slice writ-job-launcher@.service writ-job-launcher.socket; do
  install -m 0644 -o root -g root "$here/launcher/$u" "/etc/systemd/system/$u"
  state "unit $u"
done
systemctl daemon-reload
systemctl enable --now writ-job-launcher.socket
[ "$(systemctl show -p ActiveState --value writ-job-launcher.socket)" = active ] || { echo "launcher socket not active" >&2; exit 4; }
[ -z "$(systemctl show -p DropInPaths --value writ-job-launcher.socket)" ] || { echo "drop-ins apply to the launcher socket" >&2; exit 4; }
if systemctl list-unit-files apport.service >/dev/null 2>&1 && systemctl is-enabled -q apport.service 2>/dev/null; then
  systemctl disable --now apport.service
  state "enable apport.service"
fi
echo "kernel.core_pattern = core" > /etc/sysctl.d/60-writ-core.conf
state "sysctlfile /etc/sysctl.d/60-writ-core.conf $(sysctl -n kernel.core_pattern | tr ' ' '_')"
sysctl -q -p /etc/sysctl.d/60-writ-core.conf
case "$(sysctl -n kernel.core_pattern)" in '|'*) echo "core_pattern is still a pipe" >&2; exit 4 ;; esac
sha256sum "$WRIT_LIBEXEC/writ-job-launcher" "$WRIT_LIBEXEC/writ-cgroup-exec" /etc/writ-launcher/launcher.json \
  /etc/systemd/system/writ-job-launcher.socket /etc/systemd/system/writ-job-launcher@.service \
  /etc/systemd/system/writ-jobs.slice > /etc/writ/launcher.sha256
chown root:writ /etc/writ/launcher.sha256; chmod 0640 /etc/writ/launcher.sha256
state "file /etc/writ/launcher.sha256"

# 10. Evidence.
record_versions "$ev/versions-after.txt"
sha256sum "$WRIT_LIBEXEC/writ-probe" "$WRIT_PRIVATE_BWRAP" "$WRIT_APPARMOR_PROFILE" \
  "$WRIT_LIBEXEC/writ-job-launcher" "$WRIT_LIBEXEC/writ-cgroup-exec" /etc/writ-launcher/launcher.json > "$ev/digests.txt"
echo "runtime_profile busybox $runtime_digest" >> "$ev/digests.txt"
cp /etc/writ-launcher/launcher.json "$ev/launcher.json"
systemctl show writ-job-launcher.socket -p ActiveState -p TriggerLimitIntervalUSec -p TriggerLimitBurst \
  -p PollLimitIntervalUSec -p PollLimitBurst -p DropInPaths > "$ev/launcher-socket.txt"
findmnt -no SOURCE,FSTYPE,UUID,OPTIONS,SIZE --target /var/lib/writ-jobs > "$ev/jobs-fs.txt"
cp "$WRIT_APPARMOR_PROFILE" "$ev/apparmor-writ-bwrap.txt"
cp /etc/writ/filesystems "$ev/filesystems.txt"
stat -c '%n %a %U:%G %F' /var/lib/writ /var/lib/writ/* /srv/writ /srv/writ/targets /etc/writ /etc/writ/* \
  /run/writ /run/writ/state "$WRIT_LIBEXEC" "$WRIT_LIBEXEC"/* /etc/writ-launcher /etc/writ-launcher/* \
  /run/writ-launcher /var/lib/writ-jobs /usr/lib/writ-runtime > "$ev/layout.txt"
{ id writ; id "$agent_user"; id "$job_user"; } > "$ev/identities.txt"
cp "$WRIT_STATE_FILE" "$ev/state.txt"
echo "setup complete; evidence in $ev"
