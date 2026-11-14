//! The production entry point `writ-approval-server` (Y3, D-043): missing or invalid
//! configuration and an unusable ledger fail closed with a non-zero exit before anything is
//! bound; a valid configuration serves the loopback page; the binary contains no test
//! verifier. Runs the real binary Cargo built for this test (`CARGO_BIN_EXE_*`, a normal,
//! non-`cfg(test)` build). Loopback only; no credential is used and nobody is enrolled.

#![allow(clippy::unwrap_used, clippy::expect_used, clippy::panic)]

use std::io::{BufRead, BufReader, Read, Write};
use std::net::{Ipv4Addr, TcpListener, TcpStream};
use std::path::{Path, PathBuf};
use std::process::{Command, Output, Stdio};

use writ_ledger_sqlite::SqliteLedger;

const BIN: &str = env!("CARGO_BIN_EXE_writ-approval-server");

fn free_port() -> u16 {
    TcpListener::bind((Ipv4Addr::LOCALHOST, 0))
        .unwrap()
        .local_addr()
        .unwrap()
        .port()
}

/// `true` when nothing listens on the port (it can be bound).
fn unbound(port: u16) -> bool {
    TcpListener::bind((Ipv4Addr::LOCALHOST, port)).is_ok()
}

fn temp_dir(name: &str) -> PathBuf {
    let dir = std::env::temp_dir().join(format!(
        "writ-approval-server-{name}-{}-{}",
        std::process::id(),
        free_port()
    ));
    std::fs::create_dir(&dir).unwrap();
    dir
}

fn remove(dir: &Path) {
    for entry in std::fs::read_dir(dir).unwrap() {
        std::fs::remove_file(entry.unwrap().path()).unwrap();
    }
    std::fs::remove_dir(dir).unwrap();
}

fn config(ledger: &Path, port: u16) -> Vec<String> {
    vec![
        "--ledger".into(),
        ledger.to_str().unwrap().into(),
        "--port".into(),
        port.to_string(),
        "--rp-id".into(),
        "localhost".into(),
        "--origin".into(),
        format!("http://localhost:{port}"),
        "--channel".into(),
        "approval-listener".into(),
        "--subject-issuer".into(),
        "writ-local".into(),
    ]
}

fn run(args: &[String]) -> Output {
    Command::new(BIN)
        .args(args)
        .stdin(Stdio::null())
        .output()
        .unwrap()
}

fn refused(out: &Output, code: i32, text: &str) {
    assert_eq!(out.status.code(), Some(code), "{out:?}");
    assert!(out.stdout.is_empty(), "nothing announced: {out:?}");
    let err = String::from_utf8_lossy(&out.stderr);
    assert!(err.contains(text), "{err}");
}

#[test]
fn missing_or_invalid_configuration_exits_non_zero_before_binding() {
    let port = free_port();
    let ledger = Path::new("/nonexistent/ledger.db");
    let good = config(ledger, port);
    refused(&run(&[]), 2, "missing required setting --ledger");
    for skip in (0..good.len()).step_by(2) {
        let mut args = good.clone();
        args.drain(skip..skip + 2);
        refused(
            &run(&args),
            2,
            "configuration refused: missing required setting",
        );
    }
    for (flag, value) in [
        ("--port", "0".to_string()),
        ("--rp-id", "example.com".into()),
        (
            "--origin",
            format!("http://localhost:{}", port.wrapping_add(1)),
        ),
        ("--origin", format!("http://127.0.0.1:{port}")),
        ("--channel", "two words".into()),
    ] {
        let mut args = good.clone();
        let i = args.iter().position(|a| a == flag).unwrap();
        args[i + 1] = value;
        refused(&run(&args), 2, "configuration refused: invalid value");
    }
    for extra in ["--test-verifier", "--verifier", "--insecure"] {
        let mut args = good.clone();
        args.push(extra.into());
        args.push("1".into());
        refused(&run(&args), 2, "configuration refused: unknown argument");
    }
    assert!(unbound(port));
}

#[test]
fn an_unusable_ledger_exits_non_zero_before_binding() {
    let dir = temp_dir("ledger");
    let port = free_port();
    // Absent: never created by the entry point.
    let absent = dir.join("absent.db");
    refused(&run(&config(&absent, port)), 1, "not serving: ledger");
    assert!(!absent.exists());
    // Not a ledger.
    let junk = dir.join("junk.db");
    std::fs::write(&junk, b"not a ledger").unwrap();
    refused(&run(&config(&junk, port)), 1, "not serving: ledger");
    // Owned by another owner (the ownership lock).
    let path = dir.join("owned.db");
    let owner = SqliteLedger::create(&path).unwrap();
    refused(&run(&config(&path, port)), 1, "OwnedElsewhere");
    drop(owner);
    assert!(unbound(port));
    remove(&dir);
}

#[test]
fn a_port_in_use_exits_non_zero() {
    let dir = temp_dir("port");
    let path = dir.join("ledger.db");
    drop(SqliteLedger::create(&path).unwrap());
    let taken = TcpListener::bind((Ipv4Addr::LOCALHOST, 0)).unwrap();
    let port = taken.local_addr().unwrap().port();
    refused(&run(&config(&path, port)), 1, "not serving: bind");
    drop(taken);
    remove(&dir);
}

#[test]
fn a_valid_configuration_serves_the_loopback_page() {
    let dir = temp_dir("serve");
    let path = dir.join("ledger.db");
    drop(SqliteLedger::create(&path).unwrap());
    let port = free_port();
    let mut child = Command::new(BIN)
        .args(config(&path, port))
        .stdin(Stdio::null())
        .stdout(Stdio::piped())
        .stderr(Stdio::null())
        .spawn()
        .unwrap();
    let mut line = String::new();
    BufReader::new(child.stdout.take().unwrap())
        .read_line(&mut line)
        .unwrap();
    assert!(
        line.contains(&format!("serving http://localhost:{port}"))
            && line.contains("verifier webauthn-rs passkey"),
        "{line}"
    );
    let mut s = TcpStream::connect((Ipv4Addr::LOCALHOST, port)).unwrap();
    write!(
        s,
        "GET /request/none HTTP/1.1\r\nHost: localhost:{port}\r\nConnection: close\r\n\r\n"
    )
    .unwrap();
    let mut answer = String::new();
    s.read_to_string(&mut answer).unwrap();
    assert!(answer.starts_with("HTTP/1.1 404"), "{answer}");
    assert!(answer.contains("No stored approval request has this id."));
    // The ledger is owned while serving.
    assert!(SqliteLedger::open(&path).is_err());
    child.kill().unwrap();
    child.wait().unwrap();
    remove(&dir);
}

#[test]
fn the_binary_contains_no_test_verifier() {
    let bytes = std::fs::read(BIN).unwrap();
    let has = |needle: &str| bytes.windows(needle.len()).any(|w| w == needle.as_bytes());
    // Positive control: the production verifier is linked.
    assert!(has("PasskeyVerifier"));
    for needle in [
        "SoftPasskey",
        "softpasskey",
        "webauthn_authenticator_rs",
        "TestVerifier",
        "m4_support",
        "approval_channel_for_tests",
        "bootstrap_owned_for_tests",
    ] {
        assert!(!has(needle), "{needle} in {BIN}");
    }
}
