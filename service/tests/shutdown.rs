//! J-8 bounded graceful shutdown of `writ-serviced` with real signals to a child process
//! (M5a-11; D-045 M5-D16), and socket activation with real inherited descriptors.
//!
//! On a workstation account the binary's J-1 startup identity check refuses (supplementary
//! groups), so each test re-executes this test binary in child mode, where the child runs
//! the same serving and shutdown code the binary runs after that check
//! ([`writ_service::daemon::run_with`]) with its own Tokio signal registration. The parent
//! sends `SIGTERM` and `SIGINT` with `kill(1)`. Inherited sockets are passed by a small Python
//! launcher that binds them, places them at descriptors 3 and 4, sets `LISTEN_PID`,
//! `LISTEN_FDS` and `LISTEN_FDNAMES` and executes the child (as systemd does).

#![allow(clippy::unwrap_used, clippy::expect_used, clippy::panic)]

use std::io::{BufRead, BufReader, Read};
use std::os::unix::fs::{MetadataExt, PermissionsExt};
use std::os::unix::net::{UnixListener, UnixStream};
use std::path::{Path, PathBuf};
use std::process::{Child, Command, ExitStatus, Stdio};
use std::sync::atomic::Ordering;
use std::sync::Arc;
use std::time::{Duration, Instant};

use writ_broker::writ_kernel::json::{self, Value};
use writ_hostsys::sock::ListenEnv;
use writ_rpc::frame::{read_frame, write_control, Frame, FrameLimits};
use writ_service::config::ServiceConfig;
use writ_service::{daemon, shutdown, Service, StartError};

const INGRESS_HEX: &str = "1111111111111111111111111111111111111111111111111111111111111111";
const PERMIT_HEX: &str = "4444444444444444444444444444444444444444444444444444444444444444";
const OTHER_UID: u32 = 4_000_001;
const CHILD: &str = "WRIT_SVC_SHUTDOWN_CHILD";
const CHILD_DIR: &str = "WRIT_SVC_SHUTDOWN_DIR";

fn me() -> u32 {
    std::fs::metadata("/proc/self").unwrap().uid()
}

fn my_gid() -> u32 {
    std::fs::metadata("/proc/self").unwrap().gid()
}

fn ancestors_pass(dir: &Path, uid: u32) -> bool {
    let mut p = PathBuf::from("/");
    let mut ok = true;
    for c in dir.components().skip(1) {
        let Ok(m) = std::fs::symlink_metadata(&p) else {
            return false;
        };
        ok &= m.is_dir() && (m.uid() == 0 || m.uid() == uid) && m.mode() & 0o022 == 0;
        p.push(c);
    }
    let m = std::fs::symlink_metadata(&p);
    ok && m.is_ok_and(|m| m.is_dir() && m.uid() == uid && m.mode() & 0o022 == 0)
}

/// The first base whose ancestors pass the key-directory check (as in `service_rpc.rs`).
fn base() -> PathBuf {
    ["XDG_RUNTIME_DIR", "HOME"]
        .into_iter()
        .filter_map(|v| std::env::var_os(v).map(PathBuf::from))
        .chain([PathBuf::from(env!("CARGO_TARGET_TMPDIR"))])
        .find(|d| d.is_absolute() && ancestors_pass(d, me()))
        .expect("no base directory whose ancestors pass the key-directory check")
}

struct Env {
    dir: PathBuf,
}

impl Env {
    fn new(name: &str) -> Env {
        let dir = base().join(format!("writ-svcstop-{}-{name}", std::process::id()));
        let _ = std::fs::remove_dir_all(&dir);
        std::fs::create_dir(&dir).unwrap();
        std::fs::set_permissions(&dir, std::fs::Permissions::from_mode(0o700)).unwrap();
        let keys = dir.join("keys");
        std::fs::create_dir(&keys).unwrap();
        std::fs::set_permissions(&keys, std::fs::Permissions::from_mode(0o700)).unwrap();
        for (purpose, hex) in [("ingress", INGRESS_HEX), ("permit", PERMIT_HEX)] {
            let path = keys.join(format!("{purpose}_k1.json"));
            std::fs::write(
                &path,
                format!(
                    r#"{{"contract":"writ-key/1","purpose":"{purpose}","key_id":"{purpose}/k1","not_before":"2026-01-01T00:00:00Z","not_after":"2030-01-01T00:00:00Z","material":"{hex}"}}"#
                ),
            )
            .unwrap();
            std::fs::set_permissions(&path, std::fs::Permissions::from_mode(0o600)).unwrap();
        }
        Env { dir }
    }

    fn host(&self) -> PathBuf {
        self.dir.join("host.sock")
    }

    fn admin(&self) -> PathBuf {
        self.dir.join("admin.sock")
    }

    /// Write `service.json` (agent `OTHER_UID`: the parser refuses an agent uid equal to
    /// the service uid; the child rebinds it to this uid) and create the stores.
    fn write_config(&self, activation: &str, drain: u64) -> ServiceConfig {
        let d = |p: &str| self.dir.join(p).to_str().unwrap().to_string();
        let doc = format!(
            r#"{{"contract":"writ-service-config/1",
            "service":{{"uid":{svc},"gid":{gid}}},
            "identities":{{"agent_uids":[{OTHER_UID}],"desktop_uids":[4000100],"job_uid":4000200}},
            "ledger":"{ledger}","snapshots":"{snaps}","keys":{{"dir":"{keys}"}},
            "sockets":{{"host":"{host}","admin":"{adm}","activation":"{activation}","host_gid":{gid}}},
            "host_bindings":[{{"uid":{OTHER_UID},"tenant_id":"acme","project_id":"web","principal_id":"agent:writ-agent","roles":["agent"],"adapter_id":"writ-host-adapter:cli","ingress_key_id":"ingress/k1"}}],
            "admin_uids":[0],
            "shutdown":{{"drain_seconds":{drain}}},
            "limits":{{"max_connections":8,"max_connections_per_uid":4,"idle_seconds":30,"request_seconds":5}}}}"#,
            svc = me(),
            gid = my_gid(),
            ledger = d("ledger.db"),
            snaps = d("snapshots.db"),
            keys = d("keys"),
            host = d("host.sock"),
            adm = d("admin.sock"),
        );
        std::fs::write(self.dir.join("service.json"), doc).unwrap();
        let c = load(&self.dir);
        if !self.dir.join("ledger.db").exists() {
            Service::initialize(&c).unwrap();
        }
        c
    }
}

impl Drop for Env {
    fn drop(&mut self) {
        let _ = std::fs::remove_dir_all(&self.dir);
    }
}

/// The configuration with this uid as the agent on `host.sock`.
fn load(dir: &Path) -> ServiceConfig {
    let mut c = ServiceConfig::load(&dir.join("service.json")).unwrap();
    c.agent_uids = vec![me()];
    c.host_bindings[0].uid = me();
    c
}

// ---------------------------------------------------------------------------------------
// Child mode.

/// The child process: serve with real signal handling until shut down, then exit with the
/// daemon's status. `held`: a request held in flight for 3 s from the start; `job`: a
/// request held until the job-termination token is set; `forever`: held for 60 s;
/// `straggler`: a task keeps a store handle for 60 s.
#[test]
fn child_entry() {
    let Some(mode) = std::env::var_os(CHILD) else {
        return;
    };
    let mode = mode.into_string().unwrap();
    let dir = PathBuf::from(std::env::var_os(CHILD_DIR).unwrap());
    let signals = shutdown::signals().unwrap();
    let config = load(&dir);
    let listen = ListenEnv::of_process();
    let code = daemon::run_with(config, signals, &listen, |running| {
        let hold = |d: Duration| {
            let guard = running.lifecycle().enter().unwrap();
            std::thread::spawn(move || {
                std::thread::sleep(d);
                drop(guard);
            });
        };
        match mode.as_str() {
            "held" => hold(Duration::from_secs(3)),
            "forever" => hold(Duration::from_secs(60)),
            "job" => {
                let guard = running.lifecycle().enter().unwrap();
                let token = Arc::clone(running.job_termination());
                std::thread::spawn(move || {
                    while !token.load(Ordering::SeqCst) {
                        std::thread::sleep(Duration::from_millis(10));
                    }
                    eprintln!("child: job terminated through the token");
                    drop(guard);
                });
            }
            "straggler" => {
                let handle = Arc::clone(running.service());
                std::thread::spawn(move || {
                    std::thread::sleep(Duration::from_secs(60));
                    drop(handle);
                });
            }
            _ => {}
        }
    });
    std::process::exit(i32::from(code));
}

struct Daemon {
    child: Child,
    stdout: BufReader<std::process::ChildStdout>,
}

fn child_command(env: &Env, mode: &str) -> Command {
    let mut cmd = Command::new(std::env::current_exe().unwrap());
    cmd.args(["--exact", "child_entry", "--test-threads=1", "--nocapture"])
        .env(CHILD, mode)
        .env(CHILD_DIR, &env.dir)
        .stdin(Stdio::null())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped());
    cmd
}

impl Daemon {
    fn spawn(mut cmd: Command) -> Daemon {
        let mut child = cmd.spawn().unwrap();
        let stdout = BufReader::new(child.stdout.take().unwrap());
        Daemon { child, stdout }
    }

    /// Wait for the readiness line; `false` when the child ended first.
    fn ready(&mut self) -> bool {
        let mut line = String::new();
        loop {
            line.clear();
            if self.stdout.read_line(&mut line).unwrap() == 0 {
                return false;
            }
            if line.contains("writ-serviced: serving") {
                return true;
            }
        }
    }

    fn signal(&self, name: &str) {
        let ok = Command::new("kill")
            .args([&format!("-{name}"), &self.child.id().to_string()])
            .status()
            .unwrap();
        assert!(ok.success());
    }

    fn wait(&mut self, within: Duration) -> (ExitStatus, Duration, String) {
        let started = Instant::now();
        loop {
            if let Some(st) = self.child.try_wait().unwrap() {
                let mut err = String::new();
                self.child
                    .stderr
                    .take()
                    .unwrap()
                    .read_to_string(&mut err)
                    .unwrap();
                return (st, started.elapsed(), err);
            }
            if started.elapsed() > within {
                let _ = self.child.kill();
                panic!("no exit within {within:?}");
            }
            std::thread::sleep(Duration::from_millis(10));
        }
    }

    fn running(&mut self) -> bool {
        self.child.try_wait().unwrap().is_none()
    }
}

impl Drop for Daemon {
    fn drop(&mut self) {
        let _ = self.child.kill();
        let _ = self.child.wait();
    }
}

fn connect(path: &Path) -> UnixStream {
    let s = UnixStream::connect(path).unwrap();
    s.set_read_timeout(Some(Duration::from_secs(5))).unwrap();
    s
}

fn op(name: &str) -> Value {
    json::obj([("op", json::s(name))])
}

fn reply(s: &mut UnixStream) -> Option<Value> {
    match read_frame(s, &FrameLimits::host()) {
        Ok(Frame::Control(v)) => Some(v),
        _ => None,
    }
}

fn error_of(v: &Value) -> String {
    v.as_obj().unwrap()["error"].as_str().unwrap().to_string()
}

fn ok(v: &Value) -> bool {
    v.as_obj().unwrap().get("ok") == Some(&Value::Bool(true))
}

/// A new owner opens the ledger at once after the exit.
fn new_owner_opens(env: &Env) {
    let svc = Service::open(load(&env.dir)).unwrap();
    drop(svc);
}

// ---------------------------------------------------------------------------------------
// Tests.

#[test]
fn sigterm_or_sigint_when_idle_releases_ownership_and_exits_zero() {
    for sig in ["TERM", "INT"] {
        let env = Env::new(&format!("idle-{sig}"));
        env.write_config("bind", 10);
        let mut d = Daemon::spawn(child_command(&env, "idle"));
        assert!(d.ready());
        assert!(env.host().exists() && env.admin().exists());
        match Service::open(load(&env.dir)) {
            Err(StartError::Ledger(e)) => {
                assert_eq!(e, writ_broker::store::StoreError::OwnedElsewhere)
            }
            _ => panic!("the ledger is owned while serving"),
        }
        d.signal(sig);
        let (st, took, err) = d.wait(Duration::from_secs(10));
        assert_eq!(st.code(), Some(0), "{sig}: {err}");
        assert!(took < Duration::from_secs(3), "{sig}: {took:?}");
        assert!(err.contains("stopped (drained true"), "{err}");
        // The sockets it bound itself are removed; the ledger opens at once.
        assert!(!env.host().exists() && !env.admin().exists());
        new_owner_opens(&env);
    }
}

#[test]
fn while_draining_new_work_is_refused_and_the_sockets_stay_held() {
    let env = Env::new("drain");
    env.write_config("bind", 10);
    let mut d = Daemon::spawn(child_command(&env, "held"));
    assert!(d.ready());
    // An ingest is open (uncommitted) on one connection, another connection is idle.
    let mut ingest = connect(&env.host());
    write_control(&mut ingest, &op("snapshot.ingest.begin")).unwrap();
    assert!(ok(&reply(&mut ingest).unwrap()));
    let mut idle = connect(&env.admin());
    let t0 = Instant::now();
    d.signal("TERM");
    // A new admitted connection is answered SERVICE_STOPPING without a request.
    let until = Instant::now() + Duration::from_secs(5);
    let refused = loop {
        let mut c = connect(&env.host());
        if let Some(v) = reply(&mut c) {
            break v;
        }
        assert!(Instant::now() < until, "no SERVICE_STOPPING answer");
        std::thread::sleep(Duration::from_millis(20));
    };
    assert_eq!(error_of(&refused), "SERVICE_STOPPING");
    // The listening sockets stay open: nobody else can bind them while stopping.
    assert!(UnixListener::bind(env.host()).is_err());
    assert!(UnixListener::bind(env.admin()).is_err());
    // A new request on an open connection is refused and the connection closed; the
    // uncommitted ingest is abandoned with it.
    write_control(&mut ingest, &op("snapshot.ingest.commit")).unwrap();
    assert_eq!(error_of(&reply(&mut ingest).unwrap()), "SERVICE_STOPPING");
    assert!(reply(&mut ingest).is_none(), "closed");
    assert!(d.running(), "the held request is still in progress");
    // The held request ends at 3 s; the drain observes it; exit 0.
    let (st, _, err) = d.wait(Duration::from_secs(15));
    assert_eq!(st.code(), Some(0), "{err}");
    assert!(
        err.contains("stopped (drained true, jobs ended true)"),
        "{err}"
    );
    assert!(t0.elapsed() >= Duration::from_secs(2), "{:?}", t0.elapsed());
    // The idle connection was shut down at release.
    assert!(reply(&mut idle).is_none());
    new_owner_opens(&env);
}

#[test]
fn after_the_drain_period_owned_jobs_are_terminated_through_the_token() {
    let env = Env::new("job");
    env.write_config("bind", 1);
    let mut d = Daemon::spawn(child_command(&env, "job"));
    assert!(d.ready());
    d.signal("TERM");
    let (st, took, err) = d.wait(Duration::from_secs(10));
    assert_eq!(st.code(), Some(0), "{err}");
    assert!(took >= Duration::from_secs(1), "{took:?}");
    assert!(err.contains("job terminated through the token"), "{err}");
    assert!(
        err.contains("stopped (drained false, jobs ended true)"),
        "{err}"
    );
    new_owner_opens(&env);
}

#[test]
fn a_second_signal_exits_at_once_non_zero() {
    let env = Env::new("second");
    env.write_config("bind", 30);
    let mut d = Daemon::spawn(child_command(&env, "forever"));
    assert!(d.ready());
    d.signal("TERM");
    std::thread::sleep(Duration::from_millis(300));
    assert!(d.running(), "draining");
    d.signal("INT");
    let (st, took, err) = d.wait(Duration::from_secs(5));
    assert_eq!(
        st.code(),
        Some(i32::from(shutdown::EXIT_SECOND_SIGNAL)),
        "{err}"
    );
    assert!(took < Duration::from_secs(3), "{took:?}");
    assert!(err.contains("second signal"), "{err}");
    // The operating system released the ownership lock.
    new_owner_opens(&env);
}

#[test]
fn the_hard_deadline_exits_non_zero_while_a_store_handle_is_held() {
    let env = Env::new("hard");
    env.write_config("bind", 1);
    let mut d = Daemon::spawn(child_command(&env, "straggler"));
    assert!(d.ready());
    let t0 = Instant::now();
    d.signal("TERM");
    let (st, _, err) = d.wait(Duration::from_secs(15));
    assert_eq!(
        st.code(),
        Some(i32::from(shutdown::EXIT_HARD_DEADLINE)),
        "{err}"
    );
    let took = t0.elapsed();
    assert!(
        took >= Duration::from_secs(6) && took < Duration::from_secs(10),
        "drain 1 s plus 5 s: {took:?}"
    );
    new_owner_opens(&env);
}

// ---------------------------------------------------------------------------------------
// Socket activation with real inherited descriptors.

const LAUNCHER: &str = r#"
import os, socket, sys
names, argv = sys.argv[1], sys.argv[2:]
socks = []
for spec in names.split(","):
    name, path, mode = spec.split("=")
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.bind(path)
    os.chmod(path, int(mode, 8))
    s.listen(16)
    socks.append((name, os.dup(s.fileno())))
    s.close()
high = [os.dup2(fd, 20 + i, inheritable=False) for i, (_, fd) in enumerate(socks)]
for i, fd in enumerate(high):
    os.dup2(fd, 3 + i, inheritable=True)
    os.set_inheritable(3 + i, True)
env = dict(os.environ, LISTEN_PID=str(os.getpid()), LISTEN_FDS=str(len(socks)),
           LISTEN_FDNAMES=":".join(n for n, _ in socks))
os.execve(argv[0], argv, env)
"#;

/// One inherited socket: (`FileDescriptorName`, path, octal mode).
type SocketSpec<'a> = (&'a str, &'a Path, &'a str);

fn activated(env: &Env, mode: &str, sockets: &[SocketSpec<'_>]) -> Daemon {
    let script = env.dir.join("launch.py");
    std::fs::write(&script, LAUNCHER).unwrap();
    let spec: Vec<String> = sockets
        .iter()
        .map(|(n, p, m)| format!("{n}={}={m}", p.display()))
        .collect();
    let child = child_command(env, mode);
    let mut cmd = Command::new("python3");
    cmd.arg("-I")
        .arg(&script)
        .arg(spec.join(","))
        .arg(child.get_program())
        .args(child.get_args());
    for (k, v) in child.get_envs() {
        if let Some(v) = v {
            cmd.env(k, v);
        }
    }
    cmd.stdin(Stdio::null())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped());
    Daemon::spawn(cmd)
}

#[test]
fn inherited_sockets_are_taken_over_verified_and_never_bound() {
    let env = Env::new("activated");
    env.write_config("systemd", 10);
    let (host, admin) = (env.host(), env.admin());
    let mut d = activated(
        &env,
        "idle",
        &[("host", &host, "660"), ("admin", &admin, "600")],
    );
    assert!(d.ready(), "{:?}", d.wait(Duration::from_secs(5)));
    let mut c = connect(&host);
    write_control(&mut c, &op("snapshot.ingest.begin")).unwrap();
    assert!(ok(&reply(&mut c).unwrap()));
    drop(c);
    d.signal("TERM");
    let (st, _, err) = d.wait(Duration::from_secs(10));
    assert_eq!(st.code(), Some(0), "{err}");
    // The socket files belong to the socket units: never removed by the service.
    assert!(host.exists() && admin.exists());
    new_owner_opens(&env);
}

#[test]
fn wrong_or_missing_inherited_sockets_refuse_to_start() {
    let env = Env::new("activation-refused");
    env.write_config("systemd", 10);
    let (host, admin) = (env.host(), env.admin());
    let cases: [(&[SocketSpec<'_>], &str); 4] = [
        (
            &[("host", &host, "600"), ("admin", &admin, "600")],
            "host: owner",
        ),
        (
            &[("host", &host, "660"), ("admin", &admin, "660")],
            "admin: owner",
        ),
        (&[("host", &host, "660")], "received sockets"),
        (
            &[("admin", &host, "660"), ("host", &admin, "600")],
            "bound to another address",
        ),
    ];
    for (sockets, want) in cases {
        let _ = std::fs::remove_file(&host);
        let _ = std::fs::remove_file(&admin);
        let mut d = activated(&env, "idle", sockets);
        assert!(!d.ready());
        let (st, _, err) = d.wait(Duration::from_secs(10));
        assert_eq!(st.code(), Some(1), "{want}: {err}");
        assert!(
            err.contains("socket activation") && err.contains(want),
            "{want}: {err}"
        );
    }
    // Not activated at all: refused (the service never falls back to binding).
    let _ = std::fs::remove_file(&host);
    let _ = std::fs::remove_file(&admin);
    let mut d = Daemon::spawn(child_command(&env, "idle"));
    assert!(!d.ready());
    let (st, _, err) = d.wait(Duration::from_secs(10));
    assert_eq!(st.code(), Some(1), "{err}");
    assert!(err.contains("no sockets were passed"), "{err}");
    assert!(!host.exists());
}
