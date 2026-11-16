#!/usr/bin/env bash
# Writ M5 reference-VM readiness trial (M5a-03, M5a-07; D-045 M5-D2, M5-D12; D-046 H-2;
# J-9 revisions 2 to 6). PREPARED, NOT RUN. Runs only inside the owner-designated disposable
# Ubuntu 24.04 VM, after setup.sh.
#
# usage: trial.sh --designated-vm NAME [--test-launcher PATH --test-exec PATH]
#
# --test-launcher and --test-exec name writ-job-launcher and writ-cgroup-exec built with
# `--features test-faults` (never installed by setup.sh); they replace the installed binaries
# only during the P-JOB-IDENTITY negative controls (trial-launcher.sh) and are restored by the
# exit trap. Without them those controls are `not_run` and P-JOB-IDENTITY fails (fail closed).
#
# The operator (root) orchestrates; each writ-probe run uses the identity it describes:
#   - the service-side report runs as `writ` in its own delegated unit
#     (systemd-run -p Delegate=yes, --cgroup-delegated);
#   - the agent-side attempts run as the agent user recorded by setup (provisional, C-1).
# 1. Starts a placeholder service as `writ` (stand-in for writ-serviced until M5a-04) holding
#    /run/writ/admin.sock (0600), with LimitCORE=0; exports its journal for P-NO-SECRET.
# 2. Prepares a writ-owned hard-link source in /var/tmp and a bind mount for P-OPENAT2.
# 3. Runs the launcher probe trials (trial-launcher.sh; reports for P-JOB-IDENTITY,
#    P-JOB-DENY, P-LAUNCHER-ADMIT, P-JOB-SERIAL and the P-FD status pipe), `writ-probe
#    agent-deny` as the agent user, the baseline report as `writ`, and the sandbox probes as the
#    job user (bubblewrap is executable only by `writ-job`, J-1; their through-the-launcher
#    variants of the J-9 revision 4 amendment are pending, J-11.10).
# 4. Runs each VM-only negative control and checks that the probe it targets now fails.
# 5. Writes every report and a summary to /root/writ-evidence/<time>-trial/. Every change is
#    restored by an exit trap.
set -euo pipefail
here=$(cd "$(dirname "$0")" && pwd)
# shellcheck source=common.sh
. "$here/common.sh"
writ_parse_designated "$@"
writ_guard "$WRIT_DESIGNATED"
test_launcher="" test_exec=""
set -- "${WRIT_ARGS[@]+"${WRIT_ARGS[@]}"}"
while [ "$#" -gt 0 ]; do
  case "$1" in
    --test-launcher) test_launcher="$2"; shift 2 ;;
    --test-exec) test_exec="$2"; shift 2 ;;
    *) echo "unexpected argument $1" >&2; exit 2 ;;
  esac
done
for b in $test_launcher $test_exec; do
  "$b" --build-info | grep -qx 'test_faults=true' || { echo "$b is not a test-faults build" >&2; exit 2; }
done
[ -f "$WRIT_STATE_FILE" ] || { echo "run setup.sh first" >&2; exit 2; }

agent_user=$(state_setting agent_user)
[ -n "$agent_user" ] || { echo "agent user not recorded by setup" >&2; exit 2; }
probe="$WRIT_LIBEXEC/writ-probe"
canary=/etc/writ/probe-canary
ev=$(evidence_dir trial)
share=$(mktemp -d /var/tmp/writ-trial.XXXXXX)
chmod 0755 "$share"
summary="$ev/summary.txt"
: > "$summary"
hardlinks_before=$(sysctl -n fs.protected_hardlinks)
tmpfs_dir=/mnt/writ-tmpfs-control
xdev=/var/lib/writ/run/xdev-test
unprofiled="$WRIT_LIBEXEC/bwrap-unprofiled"
granted=writ-probe-granted
units=()

cleanup() {
  if declare -F launcher_restore >/dev/null; then launcher_restore || true; fi
  sysctl -q -w "fs.protected_hardlinks=$hardlinks_before" || true
  chmod 0700 /var/lib/writ/keys || true
  if command -v setfacl >/dev/null; then setfacl -k /var/lib/writ/stores 2>/dev/null || true; fi
  if mountpoint -q "$tmpfs_dir"; then umount "$tmpfs_dir" || true; fi
  rmdir "$tmpfs_dir" 2>/dev/null || true
  if mountpoint -q "$xdev/root/mnt"; then umount "$xdev/root/mnt" || true; fi
  rm -rf -- "$xdev" "$unprofiled"
  if getent passwd "$granted" >/dev/null; then userdel --remove "$granted" 2>/dev/null || true; fi
  for u in "${units[@]+"${units[@]}"}"; do systemctl stop "$u" 2>/dev/null || true; done
  rm -rf -- "$share"
}
trap cleanup EXIT

record_versions "$ev/versions.txt"
sha256sum "$probe" "$WRIT_PRIVATE_BWRAP" "$WRIT_APPARMOR_PROFILE" > "$ev/digests.txt"

# Placeholder service: binds admin.sock 0600 and sleeps. $1 = unit name, rest = extra
# properties. Sets PLACEHOLDER_PID (runs in this shell so cleanup sees the unit).
start_placeholder() {
  local unit="$1"; shift
  rm -f /run/writ/admin.sock
  systemd-run --quiet --collect --unit "$unit" -p User=writ -p Group=writ -p UMask=0077 -p LimitCORE=0 "$@" \
    /usr/bin/python3 -c 'import os,socket,time
p="/run/writ/admin.sock"
os.umask(0o177)
s=socket.socket(socket.AF_UNIX); s.bind(p); os.chmod(p,0o600); s.listen(1)
print("placeholder ready", flush=True)
time.sleep(1800)'
  units+=("$unit")
  local i=0
  while [ "$i" -lt 50 ] && [ ! -S /run/writ/admin.sock ]; do sleep 0.1; i=$((i + 1)); done
  [ -S /run/writ/admin.sock ] || { echo "placeholder did not bind admin.sock" >&2; exit 1; }
  PLACEHOLDER_PID=$(systemctl show -p MainPID --value "$unit")
  [ "${PLACEHOLDER_PID:-0}" != 0 ] || { echo "placeholder has no main pid" >&2; exit 1; }
}

# agent_deny NAME USER [extra writ-probe agent-deny args...]: report readable by writ.
agent_deny() {
  local name="$1" user="$2"; shift 2
  runuser -u "$user" -- env TMPDIR=/var/tmp "$probe" agent-deny --service-pid "$pid" --service-unit "$unit" \
    --hardlink-source "$share/link/src" "$@" > "$share/$name.json"
  chmod 0644 "$share/$name.json"
  cp "$share/$name.json" "$ev/$name.json"
}

# run_probe NAME [systemd property args...] -- [writ-probe args...] (as writ; RUN_AS overrides)
run_probe() {
  local name="$1"; shift
  local as="${RUN_AS:-writ}"
  local props=()
  while [ "$#" -gt 0 ] && [ "$1" != -- ]; do props+=("$1"); shift; done
  [ "$#" -gt 0 ] && shift
  local u="writ-probe-$name-$$"
  units+=("$u")
  set +e
  systemd-run --quiet --wait --pipe --collect --unit "$u" -p User="$as" -p Group="$as" "${props[@]+"${props[@]}"}" \
    -- "$probe" --canary-file "$canary" "$@" > "$ev/$name.json" 2> "$ev/$name.stderr"
  echo $? > "$ev/$name.exit"
  set -e
}

# expect NAME PROBE-ID STATUS
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

# 1. Placeholder service and its exported journal.
unit="writ-placeholder-$$.service"
start_placeholder "$unit"
pid=$PLACEHOLDER_PID
echo "placeholder_pid=$pid unit=$unit agent_user=$agent_user" >> "$summary"
sleep 1
journalctl -q --no-pager -o cat -u "$unit" > "$share/journal.txt" || true
chmod 0644 "$share/journal.txt"

# 2. Hard-link source (writ-owned, in a directory the agent can reach) and the bind mount.
install -d -m 0755 "$share/link"
install -m 0644 -o writ -g writ /dev/null "$share/link/src"
install -d -m 0700 -o writ -g writ "$xdev" "$xdev/root" "$xdev/root/mnt" "$xdev/src"
install -m 0600 -o writ -g writ /dev/null "$xdev/src/f"
mount --bind "$xdev/src" "$xdev/root/mnt"

# 3. Launcher trials, agent-side attempts, the baseline report and the job-side sandbox probes.
# shellcheck source=trial-launcher.sh
. "$here/trial-launcher.sh"
agent_deny agent-deny "$agent_user"
common=(--service-pid "$pid" --agent-report "$share/agent-deny.json" --log-file "$share/journal.txt"
        --launcher-reports "$reports")
run_probe baseline -p Delegate=yes -- --cgroup-delegated --xdev-root "$xdev" "${common[@]}"
for id in P-AGENT-USERNS P-CGROUP P-OPENAT2 P-RENAME P-FS-TYPE P-ACL P-HARDLINK P-PEERCRED \
          P-AGENT-DENY P-NO-SECRET P-JOB-IDENTITY P-JOB-DENY P-LAUNCHER-ADMIT P-JOB-SERIAL; do
  expect baseline "$id" ok
done
jobscratch="$share/jobside"
install -d -m 0700 -o "$job_user" -g "$job_user" "$jobscratch"
RUN_AS="$job_user" run_probe jobside -p Delegate=yes -- --cgroup-delegated --scratch "$jobscratch" "${common[@]}"
for id in P-USERNS P-FS P-NET P-PID P-KILL P-LIMITS P-FD; do
  expect jobside "$id" ok
done
python3 - "$ev/baseline.json" >> "$summary" <<'PY'
import json, sys
r = json.load(open(sys.argv[1]))
for p in r["probes"]:
    c = p["negative_control"]
    if c["kind"] == "runtime":
        print(("PASS" if c["result"] == "failed_as_expected" else "FAIL") + f" baseline runtime control {p['id']}: {c['result']}")
# The baseline alone cannot run the bubblewrap probes (the private copy is executable only by
# writ-job); governed readiness is the union of baseline and jobside until the launcher-based
# variants exist (J-11.10), so this line is informational.
print(f"INFO baseline governed_ready={r['governed_ready']} (bubblewrap probes are in jobside)")
PY

# 4a. P-USERNS: a private copy without the AppArmor profile fails.
install -m 0750 -o root -g writ "$WRIT_PRIVATE_BWRAP" "$unprofiled"
run_probe userns-control -p Delegate=yes -- --bwrap "$unprofiled" "${common[@]}"
expect userns-control P-USERNS missing

# 4b. P-AGENT-USERNS: a test uid that may execute the private copy (group writ) gets a
#     namespace, so the probe must report it.
useradd --create-home --shell /bin/bash --groups writ "$granted"
agent_deny agent-deny-granted "$granted"
run_probe agent-userns-control -- --service-pid "$pid" --agent-report "$share/agent-deny-granted.json"
expect agent-userns-control P-AGENT-USERNS missing
userdel --remove "$granted"

# 4c. P-CGROUP: no delegation.
run_probe cgroup-control -- --cgroup-delegated "${common[@]}"
expect cgroup-control P-CGROUP missing

# 4d. P-FS-TYPE and P-RENAME: state root, scratch and target root on a tmpfs.
install -d -m 0755 "$tmpfs_dir"
mount -t tmpfs -o size=16m,mode=0700,uid="$(id -u writ)",gid="$(id -g writ)" tmpfs "$tmpfs_dir"
install -d -m 0700 -o writ -g writ "$tmpfs_dir/run" "$tmpfs_dir/targets"
run_probe fstype-control -- --state-root "$tmpfs_dir" --target-root "$tmpfs_dir/targets" "${common[@]}"
umount "$tmpfs_dir"
expect fstype-control P-FS-TYPE missing
expect fstype-control P-RENAME missing

# 4e. P-HARDLINK: protected_hardlinks off is detected.
sysctl -q -w fs.protected_hardlinks=0
run_probe hardlink-control -- "${common[@]}"
sysctl -q -w "fs.protected_hardlinks=$hardlinks_before"
expect hardlink-control P-HARDLINK missing

# 4f. P-ACL: a planted default ACL on a trusted directory (if setfacl exists; the runtime
#     control in every report plants one on a scratch directory regardless).
if command -v setfacl >/dev/null; then
  setfacl -d -m u::rwx /var/lib/writ/stores
  run_probe acl-control -- "${common[@]}"
  setfacl -k /var/lib/writ/stores
  expect acl-control P-ACL missing
else
  echo "INFO acl-control not run: setfacl absent (runtime control covers P-ACL)" >> "$summary"
fi

# 4g. P-AGENT-DENY: a loosely moded canary key readable by the agent must be reported.
install -d -m 0755 "$share/canary"
install -m 0644 -o writ -g writ "$canary" "$share/canary/canary.key"
agent_deny agent-deny-canary "$agent_user" --extra-read "$share/canary/canary.key"
run_probe agentdeny-control -- --service-pid "$pid" --agent-report "$share/agent-deny-canary.json"
expect agentdeny-control P-AGENT-DENY missing

# 4h. P-NO-SECRET: canary in the service environment.
systemctl stop "$unit"
start_placeholder "writ-placeholder-leaky-$$" -p "Environment=WRIT_TEST_CANARY=$(cat "$canary")"
run_probe nosecret-control -- --service-pid "$PLACEHOLDER_PID" --agent-report "$share/agent-deny.json"
expect nosecret-control P-NO-SECRET missing

cat "$summary"
if grep -q '^FAIL' "$summary"; then
  echo "trial: expectations not met; evidence in $ev" >&2
  exit 1
fi
echo "trial: every expectation met; evidence in $ev"
