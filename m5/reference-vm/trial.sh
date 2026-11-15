#!/usr/bin/env bash
# Writ M5 reference-VM readiness trial (M5a-03; D-045 M5-D2, M5-D12; J-9). PREPARED, NOT RUN.
# Runs only inside the owner-designated disposable Debian 13 VM, after setup.sh.
#
# usage: trial.sh --designated-vm NAME
#
# 1. Starts a placeholder service process as `writ` (stand-in for writ-serviced until M5a-04)
#    that holds /run/writ/admin.sock (mode 0600).
# 2. Runs `writ-probe agent-deny` as `writ-agent` against it.
# 3. Runs `writ-probe` as `writ` inside its own delegated systemd unit (baseline).
# 4. Runs each VM-only negative control and checks that the probe it targets now fails:
#    user.max_user_namespaces=0 (P-USERNS), no Delegate= (P-CGROUP), tmpfs state root
#    (P-FS-TYPE), keys directory 0755 (P-AGENT-DENY), canary in the service environment
#    (P-NO-SECRET). Every change is restored on exit.
# 5. Writes every report and a summary to /root/writ-evidence/<time>-trial/.
set -euo pipefail
here=$(cd "$(dirname "$0")" && pwd)
# shellcheck source=common.sh
. "$here/common.sh"
writ_parse_designated "$@"
writ_guard "$WRIT_DESIGNATED"
[ "${#WRIT_ARGS[@]}" = 0 ] || { echo "unexpected arguments: ${WRIT_ARGS[*]}" >&2; exit 2; }
[ -f "$WRIT_STATE_FILE" ] || { echo "run setup.sh first" >&2; exit 2; }

probe="$WRIT_LIBEXEC/writ-probe"
canary=/etc/writ/probe-canary
ev=$(evidence_dir trial)
share=$(mktemp -d /var/tmp/writ-trial.XXXXXX)
chmod 0755 "$share"
summary="$ev/summary.txt"
: > "$summary"
userns_before=$(sysctl -n user.max_user_namespaces)
tmpfs_dir=/mnt/writ-tmpfs-control
units=()

cleanup() {
  sysctl -q -w "user.max_user_namespaces=$userns_before" || true
  chmod 0700 /var/lib/writ/keys || true
  if mountpoint -q "$tmpfs_dir"; then umount "$tmpfs_dir" || true; fi
  rmdir "$tmpfs_dir" 2>/dev/null || true
  for u in "${units[@]+"${units[@]}"}"; do systemctl stop "$u" 2>/dev/null || true; done
  rm -rf -- "$share"
}
trap cleanup EXIT

record_versions "$ev/versions.txt"
sha256sum "$probe" > "$ev/writ-probe.sha256"

# Placeholder service: binds admin.sock with mode 0600 and sleeps. $1 = unit name, rest = extra
# properties. Sets PLACEHOLDER_PID (called in this shell, not a subshell, so cleanup sees the unit).
start_placeholder() {
  local unit="$1"; shift
  rm -f /run/writ/admin.sock
  systemd-run --quiet --collect --unit "$unit" -p User=writ -p Group=writ "$@" \
    /usr/bin/python3 -c 'import os,socket,time
p="/run/writ/admin.sock"
os.path.exists(p) and os.unlink(p)
os.umask(0o177)
s=socket.socket(socket.AF_UNIX); s.bind(p); os.chmod(p,0o600); s.listen(1)
time.sleep(1800)'
  units+=("$unit")
  local i=0
  while [ "$i" -lt 50 ] && [ ! -S /run/writ/admin.sock ]; do sleep 0.1; i=$((i + 1)); done
  [ -S /run/writ/admin.sock ] || { echo "placeholder did not bind admin.sock" >&2; exit 1; }
  PLACEHOLDER_PID=$(systemctl show -p MainPID --value "$unit")
  [ "${PLACEHOLDER_PID:-0}" != 0 ] || { echo "placeholder has no main pid" >&2; exit 1; }
}

# run_probe NAME [systemd property args...] -- [writ-probe args...]
run_probe() {
  local name="$1"; shift
  local props=()
  while [ "$#" -gt 0 ] && [ "$1" != -- ]; do props+=("$1"); shift; done
  [ "$#" -gt 0 ] && shift
  local unit="writ-probe-$name-$$"
  units+=("$unit")
  set +e
  systemd-run --quiet --wait --pipe --collect --unit "$unit" -p User=writ -p Group=writ "${props[@]+"${props[@]}"}" \
    -- "$probe" --canary-file "$canary" "$@" > "$ev/$name.json" 2> "$ev/$name.stderr"
  echo $? > "$ev/$name.exit"
  set -e
}

# expect NAME PROBE-ID STATUS: the report NAME has PROBE-ID with STATUS.
expect() {
  python3 - "$ev/$1.json" "$2" "$3" "$1" >> "$summary" <<'PY'
import json, sys
path, pid, want, name = sys.argv[1:]
try:
    r = json.load(open(path))
    got = next((p["status"] for p in r["probes"] if p["id"] == pid), "absent")
except Exception as e:
    got = f"unreadable ({e})"
print(("PASS" if got == want else "FAIL") + f" {name}: {pid} expected {want}, observed {got}")
PY
}

# 1-2. Placeholder service and agent-side attempts.
start_placeholder "writ-placeholder-$$"
pid=$PLACEHOLDER_PID
echo "placeholder_pid=$pid" >> "$summary"
runuser -u writ-agent -- "$probe" agent-deny --service-pid "$pid" > "$share/agent-deny.json"
chmod 0644 "$share/agent-deny.json"
cp "$share/agent-deny.json" "$ev/agent-deny.json"
common=(--service-pid "$pid" --agent-report "$share/agent-deny.json")

# 3. Baseline in a delegated unit.
run_probe baseline -p Delegate=yes -- --cgroup-delegated "${common[@]}"
for id in P-USERNS P-FS P-NET P-PID P-CGROUP P-KILL P-LIMITS P-FD P-OPENAT2 P-RENAME P-FS-TYPE P-AGENT-DENY P-NO-SECRET; do
  expect baseline "$id" ok
done
expect baseline P-PEERCRED error
python3 - "$ev/baseline.json" >> "$summary" <<'PY'
import json, sys
r = json.load(open(sys.argv[1]))
for p in r["probes"]:
    c = p["negative_control"]
    if c["kind"] == "runtime":
        print(("PASS" if c["result"] == "failed_as_expected" else "FAIL") + f" baseline runtime control {p['id']}: {c['result']}")
print(f"INFO baseline governed_ready={r['governed_ready']} (false expected while P-PEERCRED is not implemented)")
PY

# 4a. P-USERNS: namespace creation disabled by sysctl.
sysctl -q -w user.max_user_namespaces=0
run_probe userns-control -p Delegate=yes -- --cgroup-delegated "${common[@]}"
sysctl -q -w "user.max_user_namespaces=$userns_before"
expect userns-control P-USERNS missing

# 4b. P-CGROUP: no delegation.
run_probe cgroup-control -- --cgroup-delegated "${common[@]}"
expect cgroup-control P-CGROUP missing

# 4c. P-FS-TYPE: state and target roots on tmpfs.
install -d -m 0755 "$tmpfs_dir"
mount -t tmpfs -o size=16m,mode=0700,uid="$(id -u writ)",gid="$(id -g writ)" tmpfs "$tmpfs_dir"
install -d -m 0700 -o writ -g writ "$tmpfs_dir/run" "$tmpfs_dir/targets"
run_probe fstype-control -p Delegate=yes -- --state-root "$tmpfs_dir" "${common[@]}"
umount "$tmpfs_dir"
expect fstype-control P-FS-TYPE missing

# 4d. P-AGENT-DENY: mis-moded keys directory.
chmod 0755 /var/lib/writ/keys
run_probe agentdeny-control -p Delegate=yes -- "${common[@]}"
chmod 0700 /var/lib/writ/keys
expect agentdeny-control P-AGENT-DENY missing

# 4e. P-NO-SECRET: planted canary in the service environment.
systemctl stop "writ-placeholder-$$"
start_placeholder "writ-placeholder-leaky-$$" -p "Environment=WRIT_TEST_CANARY=$(cat "$canary")"
leaky=$PLACEHOLDER_PID
run_probe nosecret-control -- --service-pid "$leaky" --agent-report "$share/agent-deny.json"
expect nosecret-control P-NO-SECRET missing

cat "$summary"
if grep -q '^FAIL' "$summary"; then
  echo "trial: expectations not met; evidence in $ev" >&2
  exit 1
fi
echo "trial: every expectation met; evidence in $ev"
