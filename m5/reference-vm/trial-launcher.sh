# Launcher probe trials (M5a-07; D-046 H-2; M5-INTERFACES J-9 revisions 4 to 6, J-11).
# PREPARED, NOT RUN. Sourced by trial.sh inside the designated disposable VM (after its guard);
# never executed on its own. Writes the reports that `writ-probe --launcher-reports DIR`
# evaluates (hostsys/src/jobprobes.rs): job-identity.txt, job-deny.txt, launcher-admit.txt,
# job-serial.txt, fd-status.txt. Every report holds the positive observations and the
# observations of its negative controls; anything not observed is written as `not_run`, which
# makes the probe fail (fail closed), never pass.
#
# Test-only builds (`--features test-faults`, never installed by setup.sh) are passed to
# trial.sh with --test-launcher and --test-exec; they replace the installed binaries only for
# the duration of a control run and are restored by launcher_restore (also in the exit trap).
#
# Requires from trial.sh: here, share, ev, summary, units (array), WRIT_LIBEXEC, agent_user,
# test_launcher, test_exec.

job_user=$(state_setting job_user)
reports="$share/launcher-reports"
install -d -m 0755 "$reports"
sock=/run/writ-launcher/launch.sock
runroot=/var/lib/writ/run
client="$here/launch-client.py"
job_uid=$(id -u "$job_user")
launcher_cfg=/etc/writ-launcher/launcher.json
launcher_backup="$share/launcher.json.orig"
cp -p "$launcher_cfg" "$launcher_backup"
dropin_dir=/run/systemd/system/writ-job-launcher@.service.d

launcher_restore() {
  [ -f "$launcher_backup" ] && cp -p "$launcher_backup" "$launcher_cfg"
  for b in writ-job-launcher writ-cgroup-exec; do
    [ -f "$share/$b.orig" ] && install -m 0755 -o root -g root "$share/$b.orig" "$WRIT_LIBEXEC/$b"
  done
  if [ -d "$dropin_dir" ]; then rm -rf -- "$dropin_dir"; systemctl daemon-reload; fi
  rm -rf -- "$runroot"/run-trial-* 2>/dev/null || true
}
cp -p "$WRIT_LIBEXEC/writ-job-launcher" "$share/writ-job-launcher.orig"
cp -p "$WRIT_LIBEXEC/writ-cgroup-exec" "$share/writ-cgroup-exec.orig"

# mkrun ID SCRIPT: a writ-owned source run-ID/prog (0640, read by group writ).
mkrun() {
  local d="$runroot/run-$1"
  rm -rf -- "$d"
  install -d -m 0750 -o writ -g writ "$d"
  printf '#!/bin/sh\n%s\n' "$2" > "$d/prog"
  chown writ:writ "$d/prog"; chmod 0640 "$d/prog"
}

# request ID [jq-free overrides as KEY=VALUE for limits]: writes $share/req-ID.json.
request() {
  local id="$1"; shift
  python3 - "$id" "$share/req-$id.json" "$@" <<'PY'
import json, sys
jid, out, *over = sys.argv[1:]
limits = {"wall_time_ms": 20000, "memory_max_bytes": 268435456, "pids_max": 64,
          "cpu_max_quota_us": 50000, "cpu_max_period_us": 100000, "output_max_bytes": 1048576,
          "input_max_bytes": 1048576, "file_size_max_bytes": 1048576, "nofile_max": 64,
          "sigpending_max": 64}
req = {"contract": "writ-launch/1", "job_id": jid, "runtime_profile": "busybox", "program": "prog",
       "args": [], "env": [["PATH", "/usr/bin:/bin"]], "limits": limits}
for o in over:
    k, v = o.split("=", 1)
    if k in limits:
        limits[k] = int(v)
    else:
        req[k] = v
json.dump(req, open(out, "w"))
PY
}

# launch AS ID MODE: run the client as AS; output in $share/launch-ID.txt and its stdout bytes
# in $share/launch-ID.out.
launch() {
  local as="$1" id="$2" mode="${3:-normal}"
  local req="$share/req-$id.json"
  [ -f "$req" ] || req="$share/req-default.json"
  chmod 0644 "$req"
  if [ "$as" = root ]; then
    python3 "$client" --socket "$sock" --request "$req" --mode "$mode" --stdout-file "$share/launch-$id.out" > "$share/launch-$id.txt" 2>&1 || true
  else
    runuser -u "$as" -- python3 "$client" --socket "$sock" --request "$req" --mode "$mode" --stdout-file "$share/launch-$id.out" > "$share/launch-$id.txt" 2>&1 || true
  fi
  cp "$share/launch-$id.txt" "$ev/launch-$id.txt"
}

lget() { awk -F= -v k="$2" '$1 == k { sub(/^[^=]*=/, ""); print; exit }' "$share/launch-$1.txt"; }

# outcome ID: refused:<CODE>, finished, or closed.
outcome() { lget "$1" outcome; }

no_job_created() {
  # No writ-job process and an empty jobs filesystem after the attempt.
  if [ -z "$(ps -u "$job_user" -o pid= 2>/dev/null)" ] && [ -z "$(ls -A /var/lib/writ-jobs | grep -vx 'lost+found')" ]; then echo 0; else echo 1; fi
}

dropin_fault() {  # dropin_fault NAME: run launcher instances with WRIT_TEST_FAULT=NAME
  install -d -m 0755 "$dropin_dir"
  printf '[Service]\nEnvironment=WRIT_TEST_FAULT=%s\n' "$1" > "$dropin_dir/fault.conf"
  systemctl daemon-reload
}

use_test_builds() {
  install -m 0755 -o root -g root "$test_launcher" "$WRIT_LIBEXEC/writ-job-launcher"
  install -m 0755 -o root -g root "$test_exec" "$WRIT_LIBEXEC/writ-cgroup-exec"
}

since=$(date -u '+%Y-%m-%d %H:%M:%S')

# ---------------------------------------------------------------------------------------------
# P-JOB-IDENTITY
# ---------------------------------------------------------------------------------------------
r="$reports/job-identity.txt"
mkrun trial-ident 'for f in /proc/$$/fd/*; do printf "fd=%s:" "${f##*/}"; readlink "$f" || echo; done
while read -r k v rest; do case "$k" in CapEff:) echo "cap_eff=$v";; CapPrm:) echo "cap_prm=$v";; NoNewPrivs:) echo "nnp=$v";; esac; done < /proc/self/status
echo "apparmor=$(cat /proc/self/attr/current)"
echo "oom_score_adj=$(cat /proc/self/oom_score_adj)"
while read -r line; do case "$line" in
  "Max open files"*) set -- $line; echo "nofile=$4";;
  "Max file size"*) set -- $line; echo "fsize=$4";;
  "Max pending signals"*) set -- $line; echo "sigpending=$4";;
  "Max core file size"*) set -- $line; echo "core=$5";;
esac; done < /proc/self/limits
echo "cgroup=$(cat /proc/self/cgroup)"'
request trial-ident
launch writ trial-ident
{
  if journalctl -q --no-pager -o cat --since "$since" -u 'writ-job-launcher@*' | grep -q 'pre-gate verified: job trial-ident '; then echo pre_verified=1; else echo pre_verified=0; fi
  echo "result_reason=$(lget trial-ident result_reason)"
  echo "result_populated_zero=$(lget trial-ident result_populated_zero)"
  cat "$share/launch-trial-ident.out"
  echo want_nofile=64; echo want_fsize=1048576; echo want_sigpending=64
} > "$r"
if [ -n "$test_launcher" ] && [ -n "$test_exec" ]; then
  use_test_builds
  # Control (1): a helper that skips setgroups; the launcher's verification must refuse.
  dropin_fault skip-setgroups
  mkrun trial-skip 'echo ran > /work/marker'
  request trial-skip
  launch writ trial-skip
  echo "ctl_skip_reason=$(lget trial-skip result_reason)" >> "$r"
  if ls /var/lib/writ-jobs/job-*/work/marker >/dev/null 2>&1; then echo ctl_skip_job_ran=1 >> "$r"; else echo ctl_skip_job_ran=0 >> "$r"; fi
  # Control (2), gate EOF: force a verification failure (wrong pre-gate count) in a build that
  # closes the gate writer on that error path without killing; the job then runs unverified.
  dropin_fault gate-eof
  python3 - "$launcher_cfg" <<'PY'
import json, sys
p = sys.argv[1]; c = json.load(open(p)); c["pre_gate_processes"] = 7; json.dump(c, open(p, "w"))
PY
  mkrun trial-eof 'echo ran > /work/marker; sleep 2'
  request trial-eof
  launch writ trial-eof
  sleep 1
  if ls /var/lib/writ-jobs/job-*/work/marker >/dev/null 2>&1; then echo ctl_eof_job_ran=1 >> "$r"; else echo ctl_eof_job_ran=0 >> "$r"; fi
  echo "ctl_eof_started=$(lget trial-eof started)" >> "$r"
  launcher_restore
  rm -rf -- /var/lib/writ-jobs/job-*
else
  printf 'ctl_skip_reason=not_run\nctl_skip_job_ran=not_run\nctl_eof_job_ran=not_run\nctl_eof_started=not_run\n' >> "$r"
fi
cp "$r" "$ev/report-job-identity.txt"

# ---------------------------------------------------------------------------------------------
# P-FD status pipe (N4)
# ---------------------------------------------------------------------------------------------
r="$reports/fd-status.txt"
# forge_status ID: while job ID runs, write a forged bubblewrap end line to descriptor 4 of every
# process in the launcher instance's job cgroup that holds the status pipe (the bubblewrap
# monitor). Done as root from the host, it stands in for an escaped job that can reach the
# monitor's descriptor (J-9 revision 6, N4); records how many writes succeeded.
forge_status() {
  local id="$1" n=0 i p f
  for i in $(seq 1 50); do
    for p in /sys/fs/cgroup/writ.slice/writ-jobs.slice/writ-job-launcher@*.service/job/cgroup.procs; do
      [ -f "$p" ] || continue
      for pid in $(cat "$p"); do
        f="/proc/$pid/fd/4"
        if [ "$(readlink "$f" 2>/dev/null | cut -c1-5)" = "pipe:" ] && printf '{ "exit-code": 99 }\n' > "$f" 2>/dev/null; then
          n=$((n + 1))
        fi
      done
    done
    [ "$n" -gt 0 ] && break
    sleep 0.1
  done
  echo "$n" > "$share/forged-$id.txt"
}
mkrun trial-fd 'w=unreachable; for f in /proc/[0-9]*/fd/4; do if printf "{ \"exit-code\": 99 }\n" > "$f" 2>/dev/null; then w=written; fi; done; echo "status_write=$w"; sleep 2; exit 7'
request trial-fd
forge_status trial-fd &
forger=$!
launch writ trial-fd
wait "$forger" || true
{
  cat "$share/launch-trial-fd.out"
  echo job_exit=7
  echo "host_forged_writes=$(cat "$share/forged-trial-fd.txt" 2>/dev/null || echo 0)"
  echo "result_reason=$(lget trial-fd result_reason)"
  echo "result_exit_status=$(lget trial-fd result_exit_status)"
  echo "result_populated_zero=$(lget trial-fd result_populated_zero)"
} > "$r"
if [ -n "$test_launcher" ] && [ -n "$test_exec" ]; then
  # Negative control: the test-only build that trusts the status pipe after started
  # (WRIT_TEST_FAULT=trust-status-after-started), with the monitor's descriptor reached by the
  # host-side forger. The probe must see a result that differs from the job's own exit.
  use_test_builds
  dropin_fault trust-status-after-started
  mkrun trial-fdctl 'sleep 2; exit 7'
  request trial-fdctl
  forge_status trial-fdctl &
  forger=$!
  launch writ trial-fdctl
  wait "$forger" || true
  ctl_status=$(lget trial-fdctl result_exit_status)
  echo "ctl_forged_writes=$(cat "$share/forged-trial-fdctl.txt" 2>/dev/null || echo 0)" >> "$r"
  echo "ctl_result_exit_status=$ctl_status" >> "$r"
  if [ "$(cat "$share/forged-trial-fdctl.txt" 2>/dev/null || echo 0)" -gt 0 ] && [ "$ctl_status" = 99 ]; then
    echo ctl_trusting_build=detected >> "$r"
  else
    echo ctl_trusting_build=missed >> "$r"
  fi
  launcher_restore
  rm -rf -- /var/lib/writ-jobs/job-*
else
  echo ctl_trusting_build=not_run >> "$r"
fi
cp "$r" "$ev/report-fd-status.txt"

# ---------------------------------------------------------------------------------------------
# P-LAUNCHER-ADMIT
# ---------------------------------------------------------------------------------------------
r="$reports/launcher-admit.txt"
mkrun trial-admit 'true'
request default
cp "$share/req-default.json" "$share/req-trial-admit.json"
desktop=writ-trial-desktop
useradd --create-home --shell /bin/bash "$desktop"
{
  for who in agent:"$agent_user" job:"$job_user" desktop:"$desktop" root:root; do
    name=${who%%:*}; as=${who#*:}
    launch "$as" "peer-$name" normal
    if [ "$(lget "peer-$name" hello)" = 1 ]; then echo "peer_$name=hello"; else echo "peer_$name=closed_before_read"; fi
  done
  # A peer in a user namespace without mappings (root's own namespace view stays 0 outside).
  unshare -U python3 "$client" --socket "$sock" --request "$share/req-default.json" > "$share/launch-peer-overflow.txt" 2>&1 || true
  if [ "$(lget peer-overflow hello)" = 1 ]; then echo peer_overflow=hello; else echo peer_overflow=closed_before_read; fi
  launch writ peer-writ no-request
  if [ "$(lget peer-writ hello)" = 1 ]; then echo peer_writ=hello; else echo peer_writ=closed; fi
} > "$r"
userdel --remove "$desktop"
case_line() {  # case_line CASE ID MODE
  launch writ "$2" "$3"
  local o; o=$(outcome "$2")
  [ "$o" = finished ] && o="finished:$(lget "$2" result_reason)"
  echo "case_$1=$o" >> "$r"
  echo "case_$1_job_created=$(no_job_created)" >> "$r"
}
cp "$share/req-default.json" "$share/req-c1.json"; case_line before_hello c1 before-hello
cp "$share/req-default.json" "$share/req-c2.json"; case_line oversized c2 oversized
cp "$share/req-default.json" "$share/req-c3.json"; case_line unknown_field c3 unknown-field
cp "$share/req-default.json" "$share/req-c4.json"; case_line scm_rights c4 scm-rights
cp "$share/req-default.json" "$share/req-c5.json"; case_line data_frame c5 data-frame
cp "$share/req-default.json" "$share/req-c6.json"; case_line no_request c6 no-request
request c7 wall_time_ms=999999999; case_line limit_above c7 normal
request c8 runtime_profile=node22; case_line unknown_profile c8 normal
request Bad_ID; cp "$share/req-Bad_ID.json" "$share/req-c9.json"; case_line bad_job_id c9 normal
mkrun trial-sym 'true'; ln -s /etc/passwd "$runroot/run-trial-sym/link"; request trial-sym; case_line symlink_source trial-sym normal
mkrun trial-foreign 'true'; install -m 0640 -o root -g writ /dev/null "$runroot/run-trial-foreign/foreign"; request trial-foreign; case_line foreign_source trial-foreign normal
mkrun trial-acl 'true'
if command -v setfacl >/dev/null; then
  setfacl -m u:"$agent_user":r "$runroot/run-trial-acl/prog"; request trial-acl; case_line acl_source trial-acl normal
else
  echo "case_acl_source=not_run" >> "$r"; echo "case_acl_source_job_created=0" >> "$r"
fi
mkrun trial-hl 'true'; ln "$runroot/run-trial-hl/prog" "$runroot/run-trial-hl/second"; request trial-hl; case_line hardlink_source trial-hl normal
# Trigger limit: rapid connections from root (refused) until the socket unit fails.
for _ in $(seq 1 40); do timeout 1 python3 "$client" --socket "$sock" --request "$share/req-default.json" --deadline 0.2 >/dev/null 2>&1 || true; done
if [ "$(systemctl show -p ActiveState --value writ-job-launcher.socket)" = failed ]; then
  echo case_failed_socket_unit=unavailable >> "$r"
else
  echo "case_failed_socket_unit=$(systemctl show -p ActiveState --value writ-job-launcher.socket)" >> "$r"
fi
echo "case_failed_socket_unit_job_created=$(no_job_created)" >> "$r"
systemctl reset-failed writ-job-launcher.socket || true
systemctl start writ-job-launcher.socket
d=$(systemctl show -p DropInPaths --value writ-job-launcher.socket writ-job-launcher@.service writ-jobs.slice | tr -d '\n ')
echo "dropins=${d:-none}" >> "$r"
# Control: a configuration admitting the agent uid is detected by the pinned configuration digest.
python3 - "$launcher_cfg" "$(id -u "$agent_user")" <<'PY'
import json, sys
p = sys.argv[1]; c = json.load(open(p)); c["writ_uid"] = int(sys.argv[2]); json.dump(c, open(p, "w"))
PY
if sha256sum --quiet -c /etc/writ/launcher.sha256 >/dev/null 2>&1; then echo ctl_admit_agent=missed >> "$r"; else echo ctl_admit_agent=detected >> "$r"; fi
launcher_restore
cp "$r" "$ev/report-launcher-admit.txt"

# ---------------------------------------------------------------------------------------------
# P-JOB-DENY (inside the sandbox and the escape view)
# ---------------------------------------------------------------------------------------------
r="$reports/job-deny.txt"
svc_pid=${pid:-0}
deny_script='t() { if eval "$2" >/dev/null 2>&1; then echo "$1=allowed"; else echo "$1=denied"; fi; }
t read_state "cat /var/lib/writ/keys/* /var/lib/writ/ledger/*"
t write_state "touch /var/lib/writ/x"
t read_run "ls /var/lib/writ/run/"
t read_etc_writ "cat /etc/writ/probe-canary"
t read_etc_launcher "cat /etc/writ-launcher/launcher.json"
t read_srv "ls /srv/writ/targets/"
t read_home "ls /home/"
t read_run_user "ls /run/user/"
t connect_host_sock "test -S /run/writ/host.sock && nc -U -w1 /run/writ/host.sock </dev/null"
t connect_admin_sock "test -S /run/writ/admin.sock && nc -U -w1 /run/writ/admin.sock </dev/null"
t connect_launch_sock "test -S /run/writ-launcher/launch.sock && nc -U -w1 /run/writ-launcher/launch.sock </dev/null"
t connect_approval_port "nc -w1 127.0.0.1 8443 </dev/null"
t connect_host_abstract "grep -q @ /proc/net/unix && false"
t write_cgroup_procs "echo $$ > /sys/fs/cgroup/writ.slice/writ-jobs.slice/cgroup.procs"
t write_cgroup_limits "echo max > /sys/fs/cgroup/writ.slice/writ-jobs.slice/memory.max"
t write_slice "mkdir /sys/fs/cgroup/writ.slice/writ-jobs.slice/x"
t signal_service "kill -0 SVC_PID"
t signal_launcher "kill -0 1"
t trace_service "cat /proc/SVC_PID/environ"
t signal_agent "kill -0 AGENT_PID"'
agent_pid=$(pgrep -u "$agent_user" -o 2>/dev/null || echo 999999)
script=$(printf '%s' "$deny_script" | sed "s/SVC_PID/$svc_pid/g; s/AGENT_PID/$agent_pid/g")
mkrun trial-deny "$script"
request trial-deny
launch writ trial-deny
sed 's/^/sandbox_/' "$share/launch-trial-deny.out" > "$r"
# Escape view: the launcher unit's sandboxing, as writ-job, under the child label, no bubblewrap.
escape_props=()
while IFS= read -r line; do
  case "$line" in
    ProtectSystem=*|TemporaryFileSystem=*|BindReadOnlyPaths=*|BindPaths=*|ProtectHome=*|PrivateTmp=*|InaccessiblePaths=*|\
    PrivateDevices=*|PrivateIPC=*|PrivateNetwork=*|KeyringMode=*|SystemCallFilter=*|SystemCallErrorNumber=*|\
    ProtectKernel*=*|ProtectClock=*|ProtectHostname=*|LockPersonality=*|RestrictRealtime=*|RestrictSUIDSGID=*|\
    SystemCallArchitectures=*|NoNewPrivileges=*|UMask=*|LimitCORE=*)
      escape_props+=(-p "$line") ;;
  esac
done < /etc/systemd/system/writ-job-launcher@.service
escape_py='import ctypes, os, subprocess, sys
libc = ctypes.CDLL(None, use_errno=True)
def sc(name, nr, *a):
    r = libc.syscall(nr, *a)
    print(name + "=" + ("denied" if r < 0 else "allowed"))
sc("sys_keyctl", 250, 0, 0, 0, 0)          # keyctl(KEYCTL_GET_KEYRING_ID, KEY_SPEC_THREAD_KEYRING)
sc("sys_io_uring_setup", 425, 1, 0)
sc("sys_bpf", 321, 0, 0, 0)
sc("sys_perf_event_open", 298, 0, 0, -1, -1, 0)
sc("sys_userfaultfd", 323, 0)
try:
    os.unshare(os.CLONE_NEWUSER); print("unshare_user=allowed")
except OSError:
    print("unshare_user=denied")
r = subprocess.run(["/usr/local/libexec/writ/bwrap", "--unshare-user", "--ro-bind", "/", "/", "true"], capture_output=True)
print("exec_private_bwrap_userns=" + ("allowed" if r.returncode == 0 else "denied"))
'
escape() {  # escape NAME [extra systemd-run args...]: as writ-job in the launcher view
  local name="$1"; shift
  local u="writ-escape-$name-$$"
  units+=("$u")
  systemd-run --quiet --wait --pipe --collect --unit "$u" -p User="$job_user" -p Group="$job_user" \
    "${escape_props[@]}" "$@" -- /bin/sh -c "$script; python3 -c '$escape_py'" > "$share/escape-$name.txt" 2>/dev/null || true
  cp "$share/escape-$name.txt" "$ev/escape-$name.txt"
}
escape main -p AppArmorProfile=writ-job-child
sed 's/^/escape_/' "$share/escape-main.txt" >> "$r"
for loc in tmp:/tmp var_tmp:/var/tmp run:/run dev_shm:/dev/shm dev_mqueue:/dev/mqueue run_user:/run/user var_crash:/var/crash; do
  key=${loc%%:*}; path=${loc#*:}
  if [ -n "$(find "$path" -xdev -user "$job_user" 2>/dev/null | head -n 1)" ]; then echo "host_$key=present"; else echo "host_$key=absent"; fi >> "$r"
done
# Control (1): a loosely moded canary in the escape view is reported readable.
install -m 0644 -o root -g root /etc/writ/probe-canary /var/lib/writ-jobs/canary
script='cat /var/lib/writ-jobs/canary >/dev/null 2>&1 && echo read_canary=allowed || echo read_canary=denied'
escape canary -p AppArmorProfile=writ-job-child
rm -f /var/lib/writ-jobs/canary
if grep -qx 'read_canary=allowed' "$share/escape-canary.txt"; then echo ctl_canary=allowed; else echo ctl_canary=denied; fi >> "$r"
# Control (2): the same escape without the child label obtains a namespace through the private copy.
script='true'
escape nolabel
if grep -qx 'exec_private_bwrap_userns=allowed' "$share/escape-nolabel.txt"; then echo ctl_nolabel_userns=allowed; else echo ctl_nolabel_userns=denied; fi >> "$r"
# Control (3): each host location bound writable by a test-only escape profile is reported.
for loc in tmp:/tmp var_tmp:/var/tmp run:/run dev_shm:/dev/shm dev_mqueue:/dev/mqueue run_user:/run/user var_crash:/var/crash; do
  key=${loc%%:*}; path=${loc#*:}
  install -d -m 0777 "$path/writ-trial-$$" 2>/dev/null || true
  script="touch $path/writ-trial-$$/f"
  escape "bind-$key" -p AppArmorProfile=writ-job-child -p "BindPaths=$path/writ-trial-$$"
  if [ -n "$(find "$path/writ-trial-$$" -xdev -user "$job_user" 2>/dev/null | head -n 1)" ]; then echo "ctl_bind_$key=allowed"; else echo "ctl_bind_$key=denied"; fi >> "$r"
  rm -rf -- "${path:?}/writ-trial-$$"
done
cp "$r" "$ev/report-job-deny.txt"

# ---------------------------------------------------------------------------------------------
# P-JOB-SERIAL
# ---------------------------------------------------------------------------------------------
r="$reports/job-serial.txt"
mkrun trial-long 'sleep 5'
request trial-long
( launch writ trial-long ) &
long=$!
sleep 1
request default
launch writ second
if [ "$(lget second hello)" = 1 ]; then echo second_connection=served; else echo second_connection=refused; fi > "$r"
wait "$long" || true
sleep 1
{
  echo "after_job_procs=$(ps -u "$job_user" -o pid= 2>/dev/null | wc -l)"
  if grep -qx 'populated 0' /sys/fs/cgroup/writ.slice/writ-jobs.slice/cgroup.events; then echo after_slice_populated=0; else echo after_slice_populated=1; fi
  echo "after_jobs_entries=$(ls -A /var/lib/writ-jobs | grep -vxc 'lost+found')"
  if grep -Eq "^ *$job_uid:" /proc/key-users; then echo key_users_line=present; else echo key_users_line=absent; fi
} >> "$r"
# Carry-over between launcher instances (per-instance PrivateIPC, PrivateTmp, KeyringMode,
# TemporaryFileSystem): objects created in one escape-view instance are absent in the next.
script='true'
escape_py='import ctypes, os
libc = ctypes.CDLL(None, use_errno=True)
libc.shmget(0x5717, 4096, 0o1600)
rt = ctypes.CDLL("librt.so.1"); rt.mq_open(b"/writ-trial", os.O_CREAT | os.O_RDWR, 0o600, None)
libc.syscall(248, b"user", b"writ-trial", b"v", 1, -4)
open("/tmp/writ-trial", "w").write("x")
'
escape carry-create -p AppArmorProfile=writ-job-child
escape_py='import ctypes, os
libc = ctypes.CDLL(None, use_errno=True)
print("carryover_sysv=" + ("present" if libc.shmget(0x5717, 0, 0) >= 0 else "absent"))
print("carryover_mqueue=" + ("present" if os.path.exists("/dev/mqueue/writ-trial") else "absent"))
print("carryover_keyring=" + ("present" if libc.syscall(250, 10, b"user", b"writ-trial", 0) >= 0 else "absent"))
print("carryover_tmp=" + ("present" if os.path.exists("/tmp/writ-trial") else "absent"))
'
escape carry-check -p AppArmorProfile=writ-job-child
grep '^carryover_' "$share/escape-carry-check.txt" >> "$r"
mounts=$(findmnt -rn -o TARGET -t ext4,xfs,btrfs,tmpfs | tr '\n' ' ')
# shellcheck disable=SC2086
if [ -n "$(find $mounts -xdev -user "$job_user" 2>/dev/null | head -n 1)" ]; then echo after_writ_job_anywhere=1; else echo after_writ_job_anywhere=0; fi >> "$r"
for loc in tmp:/tmp var_tmp:/var/tmp run:/run dev_shm:/dev/shm dev_mqueue:/dev/mqueue run_user:/run/user var_crash:/var/crash; do
  key=${loc%%:*}; path=${loc#*:}
  if [ -n "$(find "$path" -xdev -user "$job_user" 2>/dev/null | head -n 1)" ]; then echo "find_$key=present"; else echo "find_$key=none"; fi >> "$r"
done
if systemctl is-enabled -q apport.service 2>/dev/null; then echo apport=enabled; else echo apport=disabled; fi >> "$r"
case "$(sysctl -n kernel.core_pattern)" in '|'*) echo core_pattern_pipe=1 ;; *) echo core_pattern_pipe=0 ;; esac >> "$r"
# Controls: each makes the launcher refuse LAUNCH_UNAVAILABLE or the probe's own find report it.
u="writ-trial-linger-$$"; units+=("$u")
systemd-run --quiet --unit "$u" -p User="$job_user" -- /bin/sleep 60
request default; launch writ ctl-linger
echo "ctl_lingering_proc=$(outcome ctl-linger | sed 's/^refused://')" >> "$r"
systemctl stop "$u" || true
runuser -u "$job_user" -- python3 -c 'import ctypes; ctypes.CDLL(None).syscall(248, b"user", b"writ-trial-ku", b"v", 1, -4)'
launch writ ctl-keys
echo "ctl_key_users=$(outcome ctl-keys | sed 's/^refused://')" >> "$r"
runuser -u "$job_user" -- python3 -c 'import ctypes; l=ctypes.CDLL(None); k=l.syscall(250, 10, b"user", b"writ-trial-ku", 0); l.syscall(250, 3, k)' || true
install -d -o "$job_user" -g "$job_user" /var/lib/writ-jobs/planted
launch writ ctl-jobs
echo "ctl_jobs_entry=$(outcome ctl-jobs | sed 's/^refused://')" >> "$r"
rm -rf /var/lib/writ-jobs/planted
for loc in tmp:/tmp var_tmp:/var/tmp run:/run dev_shm:/dev/shm run_user:/run/user var_crash:/var/crash; do
  key=${loc%%:*}; path=${loc#*:}
  install -m 0600 -o "$job_user" -g "$job_user" /dev/null "$path/writ-trial-planted"
  if [ -n "$(find "$path" -xdev -user "$job_user" 2>/dev/null | head -n 1)" ]; then echo "ctl_find_$key=found"; else echo "ctl_find_$key=missed"; fi >> "$r"
  rm -f -- "$path/writ-trial-planted"
done
runuser -u "$job_user" -- python3 -c 'import ctypes, os; ctypes.CDLL("librt.so.1").mq_open(b"/writ-trial-planted", os.O_CREAT | os.O_RDWR, 0o600, None)' || true
if [ -n "$(find /dev/mqueue -xdev -user "$job_user" 2>/dev/null | head -n 1)" ]; then echo ctl_find_dev_mqueue=found; else echo ctl_find_dev_mqueue=missed; fi >> "$r"
rm -f /dev/mqueue/writ-trial-planted
old_core=$(sysctl -n kernel.core_pattern)
sysctl -q -w 'kernel.core_pattern=|/bin/false'
case "$(sysctl -n kernel.core_pattern)" in '|'*) echo ctl_core_pipe=reported ;; *) echo ctl_core_pipe=missed ;; esac >> "$r"
sysctl -q -w "kernel.core_pattern=$old_core"
cp "$r" "$ev/report-job-serial.txt"

launcher_restore
