use super::*;
use crate::frame::{parse_output, CONTROL, DATA};
use rustix::pipe::{pipe_with, PipeFlags};
use rustix::process::{pidfd_open, Pid, PidfdFlags};
use std::process::{Child, Command, Stdio};
use std::time::Duration;

struct Rig {
    session: Session,
    writ: UnixStream,
    status_w: Option<File>,
    child: Child,
}

impl Drop for Rig {
    fn drop(&mut self) {
        let _ = self.child.kill();
        let _ = self.child.wait();
    }
}

fn rig(script: &str, b: Bounds) -> Rig {
    let mut child = Command::new("/bin/sh")
        .args(["-c", script])
        .stdin(Stdio::null())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()
        .unwrap();
    let pid = Pid::from_raw(i32::try_from(child.id()).unwrap()).unwrap();
    let pidfd = pidfd_open(pid, PidfdFlags::empty()).unwrap();
    let (sr, sw) = pipe_with(PipeFlags::CLOEXEC).unwrap();
    let (conn, writ) = UnixStream::pair().unwrap();
    let ch = Channels {
        conn,
        stdout: Some(File::from(OwnedFd::from(child.stdout.take().unwrap()))),
        stderr: Some(File::from(OwnedFd::from(child.stderr.take().unwrap()))),
        status: Some(File::from(sr)),
        child: pidfd,
    };
    Rig {
        session: Session::new(ch, b, "run-1").unwrap(),
        writ,
        status_w: Some(File::from(sw)),
        child,
    }
}

fn bounds() -> Bounds {
    Bounds {
        output_max: 1 << 20,
        outbound_max: 1 << 20,
        status_max: 4096,
        status_drain: 1 << 16,
    }
}

fn soon(ms: u64) -> Instant {
    Instant::now() + Duration::from_millis(ms)
}

/// Every frame `writ` received.
fn frames(writ: &mut UnixStream) -> Vec<Frame> {
    writ.set_nonblocking(true).unwrap();
    let mut d = Decoder::new(true);
    let mut out = Vec::new();
    let mut buf = [0u8; 65536];
    loop {
        match writ.read(&mut buf) {
            Ok(0) => break,
            Ok(n) => out.extend(d.feed(&buf[..n]).unwrap()),
            Err(e) if e.kind() == ErrorKind::WouldBlock => break,
            Err(e) => panic!("{e}"),
        }
    }
    out
}

fn send(writ: &mut UnixStream, kind: u8, v: &[u8]) {
    writ.write_all(&frame::encode(kind, v).unwrap()).unwrap();
}

#[test]
fn start_reports_child_pid_and_holds_early_output() {
    let mut r = rig("echo early; sleep 5", bounds());
    r.status_w
        .as_mut()
        .unwrap()
        .write_all(b"{ \"child-pid\": 77,\n \"pid-namespace\": 1\n}\n")
        .unwrap();
    // Let the early output arrive first.
    std::thread::sleep(Duration::from_millis(100));
    assert_eq!(r.session.await_start(soon(2000)), Start::ChildPid(77));
    assert!(
        frames(&mut r.writ).is_empty(),
        "nothing is sent before started"
    );
    // Early output may still be in the pipe; read it during the running phase.
    assert!(r.session.send_control(&protocol::started("run-1")));
    r.session.mark_started();
    let _ = r.session.run(soon(300));
    let _ = r.session.flush(soon(500));
    let f = frames(&mut r.writ);
    assert!(
        matches!(&f[0], Frame::Control(p) if p.starts_with(b"{\"contract\":\"writ-launch-started/1\""))
    );
    let data: Vec<u8> = f
        .iter()
        .filter_map(|x| match x {
            Frame::Data(p) => parse_output(p).map(|(_, _, b)| b.to_vec()),
            _ => None,
        })
        .flatten()
        .collect();
    assert_eq!(data, b"early\n");
}

#[test]
fn start_fails_on_deadline_child_exit_or_bad_status() {
    let mut r = rig("sleep 5", bounds());
    assert!(matches!(r.session.await_start(soon(100)), Start::Failed(_)));
    let mut r = rig("exit 0", bounds());
    assert!(matches!(r.session.await_start(soon(3000)), Start::Failed(t) if t.contains("exited")));
    let mut r = rig("sleep 5", bounds());
    r.status_w
        .as_mut()
        .unwrap()
        .write_all(b"{ \"exit-code\": 1 }\n")
        .unwrap();
    assert!(matches!(
        r.session.await_start(soon(2000)),
        Start::Failed(_)
    ));
    let mut r = rig("sleep 5", bounds());
    r.status_w = None;
    assert!(matches!(r.session.await_start(soon(2000)), Start::Failed(t) if t.contains("closed")));
}

#[test]
fn writ_eof_cancel_or_data_frame_during_start() {
    let mut r = rig("sleep 5", bounds());
    r.writ.shutdown(std::net::Shutdown::Write).unwrap();
    assert_eq!(r.session.await_start(soon(2000)), Start::ConnLost);
    let mut r = rig("sleep 5", bounds());
    let c = json::canonical(&protocol::cancel("run-1")).unwrap();
    send(&mut r.writ, CONTROL, c.as_bytes());
    assert_eq!(r.session.await_start(soon(2000)), Start::Cancelled);
    let mut r = rig("sleep 5", bounds());
    send(&mut r.writ, DATA, b"x");
    assert_eq!(r.session.await_start(soon(2000)), Start::ConnLost);
}

fn running(script: &str, b: Bounds) -> Rig {
    let mut r = rig(script, b);
    r.session.mark_started();
    r
}

#[test]
fn child_exit_and_output_forwarding() {
    let mut r = running("printf out; printf err >&2; exit 3", bounds());
    assert_eq!(r.session.run(soon(5000)), Run::ChildExited);
    r.session.drain_output();
    assert!(r.session.flush(soon(1000)));
    assert_eq!(r.session.output_bytes(), [3, 3]);
    let mut got = (Vec::new(), Vec::new());
    for f in frames(&mut r.writ) {
        if let Frame::Data(p) = f {
            let (s, id, b) = parse_output(&p).unwrap();
            assert_eq!(id, "run-1");
            match s {
                Stream::Stdout => got.0.extend_from_slice(b),
                Stream::Stderr => got.1.extend_from_slice(b),
            }
        }
    }
    assert_eq!(got, (b"out".to_vec(), b"err".to_vec()));
}

#[test]
fn wall_time_is_the_launchers_deadline() {
    let mut r = running("sleep 30", bounds());
    let t = Instant::now();
    assert_eq!(
        r.session.run(soon(150)),
        Run::Kill(protocol::Reason::TimedOut)
    );
    assert!(t.elapsed() < Duration::from_secs(5));
}

#[test]
fn output_limit_kills() {
    let b = Bounds {
        output_max: 100,
        ..bounds()
    };
    let mut r = running("head -c 1000 /dev/zero; sleep 5", b);
    assert_eq!(
        r.session.run(soon(5000)),
        Run::Kill(protocol::Reason::OutputExceeded)
    );
    assert!(r.session.output_exceeded());
}

#[test]
fn writ_not_reading_fills_the_bounded_buffer() {
    let b = Bounds {
        output_max: 64 << 20,
        outbound_max: 4096,
        ..bounds()
    };
    let mut r = running("head -c 16000000 /dev/zero; sleep 5", b);
    // writ never reads: the socket buffer fills, then the outbound bound.
    assert_eq!(
        r.session.run(soon(10_000)),
        Run::Kill(protocol::Reason::OutputExceeded)
    );
    assert!(r.session.outbound() > 0);
}

#[test]
fn cancel_eof_and_data_during_run() {
    let mut r = running("sleep 5", bounds());
    let c = json::canonical(&protocol::cancel("run-1")).unwrap();
    send(&mut r.writ, CONTROL, c.as_bytes());
    assert_eq!(
        r.session.run(soon(2000)),
        Run::Kill(protocol::Reason::Cancelled)
    );

    let mut r = running("sleep 5", bounds());
    let c = json::canonical(&protocol::cancel("run-2")).unwrap();
    send(&mut r.writ, CONTROL, c.as_bytes());
    assert_eq!(
        r.session.run(soon(2000)),
        Run::ConnLost,
        "cancel for another job"
    );

    let mut r = running("sleep 5", bounds());
    drop(std::mem::replace(
        &mut r.writ,
        UnixStream::pair().unwrap().0,
    ));
    assert_eq!(r.session.run(soon(2000)), Run::ConnLost);
    assert!(!r.session.conn_open());

    let mut r = running("sleep 5", bounds());
    send(&mut r.writ, DATA, b"input");
    assert_eq!(r.session.run(soon(2000)), Run::ConnLost);
}

#[test]
fn status_after_started_is_drained_and_never_used() {
    let mut r = running("sleep 0.3; exit 0", bounds());
    // An escaped job writing to the status pipe after started (N4).
    r.status_w
        .as_mut()
        .unwrap()
        .write_all(b"{ \"exit-code\": 99 }\n{ \"child-pid\": 1 }\n")
        .unwrap();
    assert_eq!(r.session.run(soon(5000)), Run::ChildExited);
    r.session.drain_output();
    assert!(r.session.flush(soon(500)));
    assert!(
        frames(&mut r.writ).is_empty(),
        "nothing derived from the status pipe"
    );
    assert_eq!(r.session.output_bytes(), [0, 0]);
}

/// Without the fault requested, even a test-faults build derives nothing from the status
/// pipe after `started`; the fault build's parser reads bubblewrap's `exit-code` line (the
/// P-FD negative control, prepared for the reference VM).
#[cfg(feature = "test-faults")]
#[test]
fn the_trusting_fault_build_is_off_unless_requested_and_parses_exit_code() {
    assert_eq!(std::env::var_os("WRIT_TEST_FAULT"), None);
    let mut r = running("sleep 0.3; exit 0", bounds());
    r.status_w
        .as_mut()
        .unwrap()
        .write_all(b"{ \"exit-code\": 99 }\n")
        .unwrap();
    assert_eq!(r.session.run(soon(5000)), Run::ChildExited);
    assert_eq!(r.session.trusted_exit(), None);
    use super::status_exit_code as p;
    assert_eq!(p(b"{ \"exit-code\": 99 }\n"), Some(99));
    assert_eq!(p(b"{\"child-pid\": 7}\n{\"exit-code\":-1}"), Some(-1));
    assert_eq!(p(b"{ \"child-pid\": 7 }"), None);
    assert_eq!(p(b"{ \"exit-code\": x }"), None);
}
