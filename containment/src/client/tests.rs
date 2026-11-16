use super::*;
use crate::testutil::Scratch;
use std::os::unix::net::UnixListener;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Mutex};
use std::thread;
use writ_broker::runner::{Limits, RunnerRequirements};

struct Ready {
    ok: AtomicBool,
    withdrawn: Mutex<Vec<String>>,
}

impl Ready {
    fn new(ok: bool) -> Ready {
        Ready {
            ok: AtomicBool::new(ok),
            withdrawn: Mutex::new(Vec::new()),
        }
    }
}

impl Readiness for Ready {
    fn verified(&self) -> bool {
        self.ok.load(Ordering::SeqCst)
    }
    fn withdraw(&self, why: &str) {
        self.ok.store(false, Ordering::SeqCst);
        self.withdrawn.lock().unwrap().push(why.to_string());
    }
}

fn d(c: char) -> String {
    format!("sha256:{}", c.to_string().repeat(64))
}

fn pinned() -> Hello {
    Hello {
        launcher_digest: d('1'),
        exec_digest: d('2'),
        bwrap_digest: d('3'),
        bwrap_profile_digest: d('4'),
        apparmor_profile_digest: d('5'),
        runtime_profiles: vec![("py312".into(), d('6'))],
        maxima_digest: d('7'),
        instance: "1".into(),
        build: crate::protocol::BUILD_RELEASE.into(),
    }
}

/// What the fake launcher does after `hello`.
#[derive(Clone)]
enum Act {
    Hello(Hello),
    Refuse(LaunchCode),
    Lost,
    Finish {
        out: Vec<u8>,
        edit: fn(&mut Finished),
    },
    /// `started`, then wait for `cancel` from `writ`; then `finished` (reason `cancelled`,
    /// verified empty) when `finish`, else close the connection.
    AwaitCancel {
        finish: bool,
    },
}

fn send(c: &mut UnixStream, kind: u8, p: &[u8]) {
    let _ = c.write_all(&frame::encode(kind, p).unwrap());
}

fn ctl(c: &mut UnixStream, v: &json::Value) {
    send(c, frame::CONTROL, json::canonical(v).unwrap().as_bytes());
}

fn fake(listener: UnixListener, act: Act) -> thread::JoinHandle<Option<Request>> {
    thread::spawn(move || {
        let (mut c, _) = listener.accept().unwrap();
        let hello = match &act {
            Act::Hello(h) => h.clone(),
            _ => pinned(),
        };
        ctl(&mut c, &hello.to_value());
        let mut dec = Decoder::new(false);
        let mut buf = [0u8; 65536];
        let payload = loop {
            let n = match c.read(&mut buf) {
                Ok(0) | Err(_) => return None,
                Ok(n) => n,
            };
            if let Some(Frame::Control(p)) = dec.feed(&buf[..n]).unwrap().into_iter().next() {
                break p;
            }
        };
        let req = protocol::parse_request(
            &payload,
            &protocol::RequestBounds {
                max_args: 8,
                max_arg_bytes: 64,
                max_env: 8,
                max_env_bytes: 64,
            },
            &crate::protocol::tests::limits(),
            &["py312"],
        )
        .unwrap();
        match act {
            Act::Hello(_) => {}
            Act::Refuse(code) => ctl(&mut c, &protocol::refused(&req.job_id, code)),
            Act::Lost => {
                ctl(&mut c, &protocol::started(&req.job_id));
            }
            Act::AwaitCancel { finish } => {
                ctl(&mut c, &protocol::started(&req.job_id));
                let cancelled = loop {
                    let n = match c.read(&mut buf) {
                        Ok(0) | Err(_) => break false,
                        Ok(n) => n,
                    };
                    let frames = dec.feed(&buf[..n]).unwrap();
                    if frames.iter().any(
                        |f| matches!(f, Frame::Control(p) if protocol::is_cancel(p, &req.job_id)),
                    ) {
                        break true;
                    }
                };
                if !cancelled {
                    return None;
                }
                if finish {
                    let f = Finished {
                        job_id: req.job_id.clone(),
                        request_digest: req.digest.clone(),
                        reason: Reason::Cancelled,
                        exit_status: None,
                        signal: Some(9),
                        kill_used: true,
                        populated_zero: true,
                        memory_events: [0; 3],
                        pids_max_events: 0,
                        cpu_usage_usec: 10,
                        output_bytes: [0, 0],
                        digests: ResultDigests {
                            bwrap: d('3'),
                            bwrap_profile: d('4'),
                            apparmor_profile: d('5'),
                            runtime_profile: d('6'),
                            exec: d('2'),
                        },
                    };
                    ctl(&mut c, &f.to_value());
                }
            }
            Act::Finish { out, edit } => {
                ctl(&mut c, &protocol::started(&req.job_id));
                if !out.is_empty() {
                    let p = frame::output_payload(Stream::Stdout, &req.job_id, &out).unwrap();
                    send(&mut c, frame::DATA, &p);
                }
                let mut f = Finished {
                    job_id: req.job_id.clone(),
                    request_digest: req.digest.clone(),
                    reason: Reason::Exited,
                    exit_status: Some(0),
                    signal: None,
                    kill_used: true,
                    populated_zero: true,
                    memory_events: [0; 3],
                    pids_max_events: 0,
                    cpu_usage_usec: 10,
                    output_bytes: [out.len() as u64, 0],
                    digests: ResultDigests {
                        bwrap: d('3'),
                        bwrap_profile: d('4'),
                        apparmor_profile: d('5'),
                        runtime_profile: d('6'),
                        exec: d('2'),
                    },
                };
                edit(&mut f);
                ctl(&mut c, &f.to_value());
            }
        }
        Some(req)
    })
}

struct Rig {
    _s: Scratch,
    cfg: ClientConfig,
    workdir: PathBuf,
}

fn rig() -> (Rig, UnixListener) {
    let s = Scratch::new("client");
    let socket = s.path().join("launch.sock");
    let listener = UnixListener::bind(&socket).unwrap();
    let run_root = s.path().join("run");
    let workdir = run_root.join("run-permit-1");
    std::fs::create_dir_all(&workdir).unwrap();
    let cfg = ClientConfig {
        socket,
        launcher_uid: rustix::process::getuid().as_raw(),
        run_root,
        runtime_profile: "py312".into(),
        pinned: pinned(),
        limits: crate::protocol::tests::limits(),
        hello_deadline: Duration::from_secs(5),
        slack: Duration::from_secs(5),
    };
    (
        Rig {
            _s: s,
            cfg,
            workdir,
        },
        listener,
    )
}

fn run(r: &Rig, ready: &Ready) -> Result<JobOutput, JobFailure> {
    run_with(r, LauncherSandbox::new(r.cfg.clone(), ready))
}

fn run_with(r: &Rig, sb: LauncherSandbox<'_>) -> Result<JobOutput, JobFailure> {
    let program = r.workdir.join(".writ-runner-program");
    let args = vec!["--check".to_string()];
    let env = vec![("LANG".to_string(), "C".to_string())];
    sb.run(&Job {
        program: &program,
        args: &args,
        env: &env,
        workdir: &r.workdir,
        limits: Limits {
            wall_time: Duration::from_secs(2),
            max_output_bytes: 1024,
        },
    })
}

#[test]
fn guarantees_only_while_readiness_is_verified() {
    let (r, _l) = rig();
    let ready = Ready::new(false);
    let sb = LauncherSandbox::new(r.cfg.clone(), &ready);
    assert_eq!(sb.guarantees(), Err(SandboxUnavailable));
    assert_eq!(run(&r, &ready), Err(JobFailure::Unavailable));
    ready.ok.store(true, Ordering::SeqCst);
    let g = sb.guarantees().unwrap();
    assert!(g.job_identity_separated);
    assert!(g.satisfies(&RunnerRequirements::production().sandbox()));
    assert!(crate::config::is_digest(&sb.environment_digest()));
}

#[test]
fn a_finished_job_maps_to_output_with_the_request_derived_from_the_run_directory() {
    let (r, l) = rig();
    let h = fake(
        l,
        Act::Finish {
            out: b"hi\n".to_vec(),
            edit: |_| {},
        },
    );
    let ready = Ready::new(true);
    let out = run(&r, &ready).unwrap();
    assert_eq!(out.stdout, b"hi\n");
    assert_eq!(out.exit_status, 0);
    let req = h.join().unwrap().unwrap();
    assert_eq!(req.job_id, "permit-1");
    assert_eq!(req.program, ".writ-runner-program");
    assert_eq!(req.limits.wall_time_ms, 2000);
    assert_eq!(req.limits.output_max_bytes, 1024);
    assert!(ready.verified());
}

#[test]
fn hello_that_differs_withdraws_readiness_and_sends_nothing() {
    let (r, l) = rig();
    let mut other = pinned();
    other.bwrap_digest = d('9');
    let h = fake(l, Act::Hello(other));
    let ready = Ready::new(true);
    assert_eq!(run(&r, &ready), Err(JobFailure::Unavailable));
    assert!(!ready.verified());
    assert_eq!(h.join().unwrap(), None, "no request was sent");
}

#[test]
fn refusals_are_definitive() {
    let (r, l) = rig();
    let _h = fake(l, Act::Refuse(LaunchCode::SourceInvalid));
    let ready = Ready::new(true);
    assert_eq!(run(&r, &ready), Err(JobFailure::Spawn));
    assert!(ready.verified());
    let (r, l) = rig();
    let _h = fake(l, Act::Refuse(LaunchCode::Unavailable));
    assert_eq!(run(&r, &ready), Err(JobFailure::Unavailable));
    assert!(!ready.verified(), "LAUNCH_UNAVAILABLE withdraws readiness");
}

#[test]
fn lost_connection_after_the_request_is_uncertain() {
    let (r, l) = rig();
    let _h = fake(l, Act::Lost);
    let ready = Ready::new(true);
    assert_eq!(run(&r, &ready), Err(JobFailure::Uncertain));
}

#[test]
fn result_digest_mismatch_or_nonempty_cgroup_withdraws_readiness() {
    for edit in [
        (|f: &mut Finished| f.request_digest = "sha256:x".into()) as fn(&mut Finished),
        |f| f.digests.bwrap = "sha256:x".into(),
        |f| f.populated_zero = false,
    ] {
        let (r, l) = rig();
        let _h = fake(l, Act::Finish { out: vec![], edit });
        let ready = Ready::new(true);
        assert_eq!(run(&r, &ready), Err(JobFailure::Unavailable));
        assert!(!ready.verified());
    }
}

#[test]
fn reasons_map_to_job_failures() {
    for (edit, want) in [
        (
            (|f: &mut Finished| {
                f.reason = Reason::TimedOut;
                f.exit_status = None;
            }) as fn(&mut Finished),
            JobFailure::TimedOut,
        ),
        (
            |f| f.reason = Reason::OutputExceeded,
            JobFailure::OutputExceeded,
        ),
        (|f| f.reason = Reason::Signaled, JobFailure::Signaled),
        (|f| f.reason = Reason::StartFailed, JobFailure::Spawn),
        (|f| f.output_bytes = [5, 0], JobFailure::Io),
    ] {
        let (r, l) = rig();
        let _h = fake(l, Act::Finish { out: vec![], edit });
        assert_eq!(run(&r, &Ready::new(true)), Err(want));
    }
}

#[test]
fn only_materialized_run_directories_become_requests() {
    let (r, _l) = rig();
    let ready = Ready::new(true);
    let sb = LauncherSandbox::new(r.cfg.clone(), &ready);
    let args: Vec<String> = vec![];
    let env: Vec<(String, String)> = vec![];
    let limits = Limits {
        wall_time: Duration::from_secs(1),
        max_output_bytes: 10,
    };
    let elsewhere = r.cfg.run_root.join("x/run-a");
    let bad_name = r.cfg.run_root.join("job-a");
    let bad_id = r.cfg.run_root.join("run-A_B");
    for w in [&elsewhere, &bad_name, &bad_id] {
        let p = w.join("prog");
        let job = Job {
            program: &p,
            args: &args,
            env: &env,
            workdir: w,
            limits,
        };
        assert!(sb.request(&job).is_none(), "{w:?}");
    }
    let outside = PathBuf::from("/bin/sh");
    let job = Job {
        program: &outside,
        args: &args,
        env: &env,
        workdir: &r.workdir,
        limits,
    };
    assert!(sb.request(&job).is_none());
    assert_eq!(
        run_dir(&r.cfg.run_root, "permit-1").as_deref(),
        Some(r.workdir.as_path())
    );
}

#[test]
fn no_launcher_is_not_run() {
    let (r, l) = rig();
    drop(l);
    assert_eq!(run(&r, &Ready::new(true)), Err(JobFailure::Unavailable));
}

/// J-8 step 4 (M5a-11): the service's job-termination token cancels a running job through
/// the launcher, which kills and verifies the cgroup empty; the run ends
/// `terminated_shutdown` (finished `failed` by T-finish), long before its wall time.
#[test]
fn the_termination_token_cancels_a_running_job_through_the_launcher() {
    let (mut r, l) = rig();
    r.cfg.slack = Duration::from_secs(30);
    let h = fake(l, Act::AwaitCancel { finish: true });
    let ready = Ready::new(true);
    let token = Arc::new(AtomicBool::new(false));
    let setter = {
        let token = Arc::clone(&token);
        thread::spawn(move || {
            thread::sleep(Duration::from_millis(200));
            token.store(true, Ordering::SeqCst);
        })
    };
    let started = Instant::now();
    let sb = LauncherSandbox::new(r.cfg.clone(), &ready).with_termination(Arc::clone(&token));
    assert_eq!(run_with(&r, sb), Err(JobFailure::TerminatedShutdown));
    assert!(
        started.elapsed() < Duration::from_secs(2),
        "before the wall time"
    );
    assert!(h.join().unwrap().is_some(), "the launcher received cancel");
    setter.join().unwrap();
    assert!(ready.verified());
}

#[test]
fn a_cancel_without_a_result_stays_uncertain() {
    let (r, l) = rig();
    let _h = fake(l, Act::AwaitCancel { finish: false });
    let ready = Ready::new(true);
    let token = Arc::new(AtomicBool::new(true));
    let sb = LauncherSandbox::new(r.cfg.clone(), &ready).with_termination(token);
    assert_eq!(run_with(&r, sb), Err(JobFailure::Uncertain));
}

#[test]
fn a_cancelled_result_this_side_did_not_ask_for_is_an_io_failure() {
    let (r, l) = rig();
    let _h = fake(
        l,
        Act::Finish {
            out: vec![],
            edit: |f| {
                f.reason = Reason::Cancelled;
                f.exit_status = None;
            },
        },
    );
    assert_eq!(run(&r, &Ready::new(true)), Err(JobFailure::Io));
}
