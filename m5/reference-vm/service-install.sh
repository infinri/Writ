#!/usr/bin/env bash
# Writ M5 reference-VM service installation (M5a-04; D-045 M5-D3, M5-D6; D-046; M5-INTERFACES
# J-1, J-2, J-3, J-9, J-11.8). PREPARED, NOT RUN. Runs only inside the owner-designated
# disposable Ubuntu 24.04 VM, after setup.sh, as root.
#
# usage: service-install.sh --designated-vm NAME --serviced-binary PATH --desktop-user NAME
#                           [--tenant ID] [--project ID] [--approval-port N]
#
# Installs writ-serviced (root:root 0755) and its configuration /etc/writ/service.json
# (root:writ 0640, no secret); provisions one ingress and one permit key as the service user
# (material from the OS random source, written to a temporary file in the key directory,
# fsync, rename, fsync of the directory; the material is never printed or recorded); creates
# the ledger and the snapshot store as the service user (writ-serviced --init); installs
# writ.service with the J-1 hardening (not started: the operator starts it after recording
# the readiness report, see README "Service"). Every change is appended to the setup state
# file so teardown.sh reverses it.
#
# Recorded limitations of this revision: writ-serviced binds host.sock and admin.sock itself
# (umask 0077: mode 0700 writ:writ), so the agent cannot reach host.sock until socket
# activation (J-8, writ-host.socket with SocketGroup=writ-agents, M5a-11) is integrated: the
# installation fails closed for the agent, never open. No job launcher (J-11, M5a-07).
set -euo pipefail
here=$(cd "$(dirname "$0")" && pwd)
# shellcheck source=common.sh
. "$here/common.sh"
writ_parse_designated "$@"
writ_guard "$WRIT_DESIGNATED"
[ -f "$WRIT_STATE_FILE" ] || { echo "run setup.sh first ($WRIT_STATE_FILE absent)" >&2; exit 2; }

serviced="" desktop_user="" tenant=writ-ref project=reference approval_port=""
set -- "${WRIT_ARGS[@]+"${WRIT_ARGS[@]}"}"
while [ "$#" -gt 0 ]; do
  case "$1" in
    --serviced-binary) serviced="$2"; shift 2 ;;
    --desktop-user) desktop_user="$2"; shift 2 ;;
    --tenant) tenant="$2"; shift 2 ;;
    --project) project="$2"; shift 2 ;;
    --approval-port) approval_port="$2"; shift 2 ;;
    *) echo "unknown argument $1" >&2; exit 2 ;;
  esac
done
[ -f "$serviced" ] || { echo "--serviced-binary must name the built writ-serviced" >&2; exit 2; }
agent_user=$(state_setting agent_user)
job_user=$(state_setting job_user)
[ -n "$agent_user" ] && [ -n "$job_user" ] || { echo "setup.sh must have created the agent and job users (J-1, D-046 H-2)" >&2; exit 2; }
[ -n "$desktop_user" ] && getent passwd "$desktop_user" >/dev/null || { echo "--desktop-user must name the disposable test human user" >&2; exit 2; }
for v in "$tenant" "$project"; do
  printf '%s' "$v" | grep -Eqx '[a-z0-9][a-z0-9._-]{0,63}' || { echo "invalid tenant or project $v" >&2; exit 2; }
done
[ -z "$approval_port" ] || printf '%s' "$approval_port" | grep -Eqx '[1-9][0-9]{0,4}' || { echo "invalid --approval-port" >&2; exit 2; }
for p in /usr/local/libexec/writ/writ-serviced /etc/writ/service.json /etc/systemd/system/writ.service \
         /var/lib/writ/ledger/ledger.db /var/lib/writ/stores/snapshots.db; do
  [ ! -e "$p" ] || { echo "$p already exists; install only once per setup" >&2; exit 2; }
done
state() { echo "$1" >> "$WRIT_STATE_FILE"; }
ev=$(evidence_dir service-install)

uid_of() { id -u "$1"; }
writ_uid=$(uid_of writ); writ_gid=$(getent group writ | cut -d: -f3)
agent_uid=$(uid_of "$agent_user"); job_uid=$(uid_of "$job_user"); desktop_uid=$(uid_of "$desktop_user")

# 1. Binary (root-owned; writ cannot replace it).
install -m 0755 -o root -g root "$serviced" /usr/local/libexec/writ/writ-serviced
state "file /usr/local/libexec/writ/writ-serviced"

# 2. Configuration (no secret). admin_uids defaults to [0] (J-11.8).
approval=""
[ -z "$approval_port" ] || approval=",\"approval\":{\"port\":$approval_port,\"channel\":\"approval-listener\",\"subject_issuer\":\"writ-local\"}"
cat > /etc/writ/service.json <<EOF
{"contract":"writ-service-config/1",
 "service":{"uid":$writ_uid,"gid":$writ_gid},
 "identities":{"agent_uids":[$agent_uid],"desktop_uids":[$desktop_uid],"job_uid":$job_uid},
 "ledger":"/var/lib/writ/ledger/ledger.db",
 "snapshots":"/var/lib/writ/stores/snapshots.db",
 "keys":{"dir":"/var/lib/writ/keys"},
 "sockets":{"host":"/run/writ/host.sock","admin":"/run/writ/admin.sock"},
 "host_bindings":[{"uid":$agent_uid,"tenant_id":"$tenant","project_id":"$project","principal_id":"agent:$agent_user","roles":["agent"],"adapter_id":"writ-host-adapter:cli","ingress_key_id":"ingress/vm-1"}]$approval}
EOF
chown root:writ /etc/writ/service.json; chmod 0640 /etc/writ/service.json
state "file /etc/writ/service.json"

# 3. Keys, provisioned as writ (J-3). The material never leaves the key directory.
provision() {
  local purpose="$1" label="$2" name tmp
  name="${purpose}_${label}.json"
  tmp=".${name}.tmp"
  runuser -u writ -- sh -c '
    set -eu
    umask 077
    cd /var/lib/writ/keys
    hex=$(head -c 32 /dev/urandom | od -An -tx1 | tr -d " \n")
    nb=$(date -u +%Y-%m-%dT%H:%M:%SZ)
    na=$(date -u -d "+90 days" +%Y-%m-%dT%H:%M:%SZ)
    printf "{\"contract\":\"writ-key/1\",\"purpose\":\"%s\",\"key_id\":\"%s/%s\",\"not_before\":\"%s\",\"not_after\":\"%s\",\"material\":\"%s\"}" \
      "$1" "$1" "$2" "$nb" "$na" "$hex" > "$3"
    unset hex
    sync "$3"
    mv "$3" "$4"
    sync .
  ' sh "$purpose" "$label" "$tmp" "$name"
  state "file /var/lib/writ/keys/$name"
}
provision ingress vm-1
provision permit vm-1

# 4. Ledger and snapshot store, created by the service user (J-1 single owner).
runuser -u writ -- /usr/local/libexec/writ/writ-serviced --init --config /etc/writ/service.json
for f in ledger/ledger.db ledger/ledger.db-wal ledger/ledger.db-shm ledger/ledger.db.owner \
         stores/snapshots.db stores/snapshots.db-wal stores/snapshots.db-shm stores/snapshots.db.owner; do
  state "file /var/lib/writ/$f"
done

# 5. writ.service (J-1 hardening, revision 4). Not enabled or started here.
cat > /etc/systemd/system/writ.service <<'EOF'
[Unit]
Description=Writ trusted service (writ-serviced)
After=local-fs.target

[Service]
Type=simple
User=writ
Group=writ
SupplementaryGroups=
UMask=0077
ExecStart=/usr/local/libexec/writ/writ-serviced --config /etc/writ/service.json
NoNewPrivileges=yes
ProtectSystem=strict
ReadWritePaths=/var/lib/writ /srv/writ /run/writ
ProtectHome=yes
PrivateTmp=yes
PrivateDevices=yes
ProtectKernelTunables=yes
ProtectKernelModules=yes
ProtectKernelLogs=yes
ProtectClock=yes
ProtectHostname=yes
ProtectControlGroups=yes
RestrictNamespaces=yes
LockPersonality=yes
RestrictRealtime=yes
RestrictSUIDSGID=yes
MemoryDenyWriteExecute=yes
SystemCallArchitectures=native
RestrictAddressFamilies=AF_UNIX AF_INET
CapabilityBoundingSet=
LimitCORE=0
KillMode=mixed
TimeoutStopSec=45
Restart=on-failure
# PrivateUsers is not set: peer credentials of agent connections would read as the overflow
# uid (J-1). No Delegate= (job cgroups belong to the launcher, J-11).

[Install]
WantedBy=multi-user.target
EOF
chmod 0644 /etc/systemd/system/writ.service
state "file /etc/systemd/system/writ.service"
state "unit writ.service"
systemctl daemon-reload

# 6. Static facts for the readiness report (J-9): printed by writ-serviced as writ for the
#    configured plan; the operator merges them into the writ-probe report and pins its digest
#    (README "Service"). This configuration has no deployment plan yet, so nothing is printed
#    and the service runs with label "unverified" (snapshot store not immutable).
stat -c '%n %a %U:%G %F' /usr/local/libexec/writ/writ-serviced /etc/writ/service.json \
  /var/lib/writ/keys /var/lib/writ/keys/* /var/lib/writ/ledger/* /var/lib/writ/stores/* \
  /etc/systemd/system/writ.service > "$ev/layout.txt"
sha256sum /usr/local/libexec/writ/writ-serviced /etc/writ/service.json /etc/systemd/system/writ.service > "$ev/digests.txt"
cp /etc/writ/service.json "$ev/service.json"
echo "service installed (not started); evidence in $ev"
