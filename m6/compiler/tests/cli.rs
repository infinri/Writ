//! Exercise the executable with its normal file and standard-input entry points.
#![allow(clippy::unwrap_used)]

use std::io::Write;
use std::process::{Command, Stdio};

const CLI: &str = env!("CARGO_BIN_EXE_writ-policy-compile");

#[test]
fn file_and_stdin_emit_identical_canonical_reports_and_refusals_use_status_one() {
    let path = concat!(env!("CARGO_MANIFEST_DIR"), "/fixtures/deny.input.json");
    let file = Command::new(CLI).arg(path).output().unwrap();
    assert!(file.status.success());
    assert!(file.stderr.is_empty());

    let mut child = Command::new(CLI)
        .arg("-")
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .spawn()
        .unwrap();
    child
        .stdin
        .take()
        .unwrap()
        .write_all(include_bytes!("../fixtures/deny.input.json"))
        .unwrap();
    let piped = child.wait_with_output().unwrap();
    assert!(piped.status.success());
    assert_eq!(file.stdout, piped.stdout);

    let document = writ_kernel::json::parse(&file.stdout).unwrap();
    let canonical = writ_kernel::json::canonical(&document).unwrap();
    assert_eq!(file.stdout, format!("{canonical}\n").as_bytes());

    let refusal = Command::new(CLI)
        .arg("-")
        .stdin(Stdio::null())
        .output()
        .unwrap();
    assert_eq!(refusal.status.code(), Some(1));
    assert!(refusal.stderr.is_empty());
    let document = writ_kernel::json::parse(&refusal.stdout).unwrap();
    assert_eq!(
        document.get("status").and_then(|value| value.as_str()),
        Some("refused")
    );
}
