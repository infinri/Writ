//! J-8 bounded graceful shutdown of `writ-approval-server` with real signals to the real
//! binary (M5a-11; D-045 M5-D16; CO-M4X-05): stop accepting work (503 while the socket stays
//! open), drain the requests in progress, release ownership (a new owner opens at once), a
//! second signal and the hard deadline. Loopback only; no credential is used.

#![allow(clippy::unwrap_used, clippy::expect_used, clippy::panic)]

use std::io::{BufRead, BufReader, Read, Write};
use std::net::{Ipv4Addr, TcpListener, TcpStream};
use std::path::{Path, PathBuf};
use std::process::{Child, Command, ExitStatus, Stdio};
use std::time::{Duration, Instant};

use writ_ledger_sqlite::SqliteLedger;

const BIN: &str = env!("CARGO_BIN_EXE_writ-approval-server");

fn free_port() -> u16 {
    TcpListener::bind((Ipv4Addr::LOCALHOST, 0))
        .unwrap()
        .local_addr()
        .unwrap()
        .port()
}

struct Served {
    dir: PathBuf,
    ledger: PathBuf,
    port: u16,
    child: Child,
}

impl Served {
    fn start(name: &str, drain: u64) -> Served {
        let dir = std::env::temp_dir().join(format!(
            "writ-approval-shutdown-{name}-{}-{}",
            std::process::id(),
            free_port()
        ));
        std::fs::create_dir(&dir).unwrap();
        let ledger = dir.join("ledger.db");
        drop(SqliteLedger::create(&ledger).unwrap());
        let port = free_port();
        let mut child = Command::new(BIN)
            .args([
                "--ledger",
                ledger.to_str().unwrap(),
                "--port",
                &port.to_string(),
                "--rp-id",
                "localhost",
                "--origin",
                &format!("http://localhost:{port}"),
                "--channel",
                "approval-listener",
                "--subject-issuer",
                "writ-local",
                "--drain-seconds",
                &drain.to_string(),
            ])
            .stdin(Stdio::null())
            .stdout(Stdio::piped())
            .stderr(Stdio::piped())
            .spawn()
            .unwrap();
        let mut line = String::new();
        BufReader::new(child.stdout.take().unwrap())
            .read_line(&mut line)
            .unwrap();
        assert!(line.contains("serving"), "{line}");
        Served {
            dir,
            ledger,
            port,
            child,
        }
    }

    fn signal(&self, name: &str) {
        let ok = Command::new("kill")
            .args([&format!("-{name}"), &self.child.id().to_string()])
            .status()
            .unwrap();
        assert!(ok.success());
    }

    fn get(&self) -> String {
        let mut s = TcpStream::connect((Ipv4Addr::LOCALHOST, self.port)).unwrap();
        s.set_read_timeout(Some(Duration::from_secs(10))).unwrap();
        write!(
            s,
            "GET / HTTP/1.1\r\nHost: localhost:{}\r\nConnection: close\r\n\r\n",
            self.port
        )
        .unwrap();
        let mut out = String::new();
        let _ = s.read_to_string(&mut out);
        out
    }

    /// A request in progress until its connection closes: the headers of a login begin
    /// whose announced body never arrives (the handler awaits the body).
    fn hung_request(&self) -> TcpStream {
        let mut s = TcpStream::connect((Ipv4Addr::LOCALHOST, self.port)).unwrap();
        write!(
            s,
            "POST /login/begin HTTP/1.1\r\nHost: localhost:{p}\r\nOrigin: http://localhost:{p}\r\n\
             Sec-Fetch-Site: same-origin\r\nContent-Type: application/x-www-form-urlencoded\r\n\
             Content-Length: 64\r\n\r\nsubject=",
            p = self.port
        )
        .unwrap();
        s
    }

    /// Wait for 503 `stopping` (the shutdown has begun).
    fn await_stopping(&self) -> String {
        let until = Instant::now() + Duration::from_secs(5);
        loop {
            let r = self.get();
            if r.starts_with("HTTP/1.1 503") || Instant::now() > until {
                return r;
            }
            std::thread::sleep(Duration::from_millis(20));
        }
    }

    fn wait(&mut self, within: Duration) -> (ExitStatus, Duration) {
        let started = Instant::now();
        loop {
            if let Some(st) = self.child.try_wait().unwrap() {
                return (st, started.elapsed());
            }
            if started.elapsed() > within {
                let _ = self.child.kill();
                panic!("no exit within {within:?}");
            }
            std::thread::sleep(Duration::from_millis(10));
        }
    }

    fn stderr(&mut self) -> String {
        let mut e = String::new();
        self.child
            .stderr
            .take()
            .unwrap()
            .read_to_string(&mut e)
            .unwrap();
        e
    }
}

impl Drop for Served {
    fn drop(&mut self) {
        let _ = self.child.kill();
        let _ = self.child.wait();
        let _ = std::fs::remove_dir_all(&self.dir);
    }
}

fn owner_opens(ledger: &Path) {
    // A new owner opens immediately after the exit (no retry, no wait).
    let l = SqliteLedger::open(ledger).unwrap();
    drop(l);
}

#[test]
fn sigterm_when_idle_releases_ownership_and_exits_zero() {
    for sig in ["TERM", "INT"] {
        let mut s = Served::start(&format!("idle-{sig}"), 10);
        assert!(s.get().starts_with("HTTP/1.1 200"));
        assert!(
            SqliteLedger::open(&s.ledger).is_err(),
            "owned while serving"
        );
        s.signal(sig);
        let (st, took) = s.wait(Duration::from_secs(8));
        assert_eq!(st.code(), Some(0), "{sig}");
        assert!(
            took < Duration::from_secs(3),
            "idle: no drain wait ({took:?})"
        );
        owner_opens(&s.ledger);
        let err = s.stderr();
        assert!(
            err.contains("stopping") && err.contains("ownership released"),
            "{err}"
        );
    }
}

#[test]
fn while_draining_new_requests_answer_503_and_the_port_stays_held() {
    let mut s = Served::start("drain", 2);
    let hung = s.hung_request();
    std::thread::sleep(Duration::from_millis(100));
    let t0 = Instant::now();
    s.signal("TERM");
    let r = s.await_stopping();
    assert!(r.starts_with("HTTP/1.1 503"), "{r}");
    assert!(r.contains("\"error\":\"stopping\""), "{r}");
    // The listening socket stays open: nobody else can bind the port while stopping.
    assert!(TcpListener::bind((Ipv4Addr::LOCALHOST, s.port)).is_err());
    assert!(s.child.try_wait().unwrap().is_none(), "still draining");
    // The hung request does not finish: the drain period ends, then release (exit 0).
    let (st, _) = s.wait(Duration::from_secs(10));
    assert_eq!(st.code(), Some(0));
    assert!(t0.elapsed() >= Duration::from_secs(2), "{:?}", t0.elapsed());
    owner_opens(&s.ledger);
    let err = s.stderr();
    assert!(err.contains("stopped (drained false)"), "{err}");
    drop(hung);
}

#[test]
fn a_second_signal_exits_at_once_non_zero() {
    let mut s = Served::start("second", 30);
    let hung = s.hung_request();
    std::thread::sleep(Duration::from_millis(100));
    s.signal("TERM");
    assert!(s.await_stopping().starts_with("HTTP/1.1 503"));
    s.signal("INT");
    let (st, took) = s.wait(Duration::from_secs(5));
    assert_eq!(st.code(), Some(4));
    assert!(took < Duration::from_secs(3), "{took:?}");
    // The operating system released the lock.
    owner_opens(&s.ledger);
    assert!(s.stderr().contains("second signal"));
    drop(hung);
}

/// The hard deadline (drain plus 5 s) through the watchdog in a child process: this test
/// binary re-executes itself in child mode, registers the signals, arms the watchdog with a
/// zero drain period and then never finishes its shutdown.
#[test]
fn the_hard_deadline_exits_non_zero() {
    if std::env::var_os("WRIT_SHUTDOWN_CHILD").is_some() {
        let rx = writ_approval_server::shutdown::signals().unwrap();
        writ_approval_server::shutdown::arm(rx, Duration::ZERO).unwrap();
        std::thread::sleep(Duration::from_secs(60));
        panic!("the watchdog did not end the process");
    }
    let started = Instant::now();
    let out = Command::new(std::env::current_exe().unwrap())
        .args([
            "--exact",
            "the_hard_deadline_exits_non_zero",
            "--test-threads=1",
            "--nocapture",
        ])
        .env("WRIT_SHUTDOWN_CHILD", "1")
        .output()
        .unwrap();
    let took = started.elapsed();
    assert_eq!(out.status.code(), Some(5), "{out:?}");
    assert!(
        took >= Duration::from_secs(5) && took < Duration::from_secs(20),
        "{took:?}"
    );
    assert!(String::from_utf8_lossy(&out.stderr).contains("hard deadline"));
}
