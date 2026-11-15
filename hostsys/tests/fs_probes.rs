//! Real (unprivileged) filesystem probes in a private directory under the Cargo target tmp dir.
#![allow(clippy::unwrap_used, clippy::expect_used, clippy::panic, missing_docs)]

use std::path::PathBuf;
use writ_hostsys::probes::{probe_openat2, probe_rename, Config};
use writ_hostsys::readiness::{ControlKind, ControlResult};
use writ_hostsys::Status;

fn cfg(label: &str) -> Config {
    let root = PathBuf::from(env!("CARGO_TARGET_TMPDIR"))
        .join(format!("hostsys-{label}-{}", std::process::id()));
    std::fs::create_dir_all(&root).unwrap();
    Config {
        scratch: root.clone(),
        state_root: root.clone(),
        target_root: root,
        ..Config::reference()
    }
}

#[test]
fn openat2_flags_are_observed_with_a_discriminating_control() {
    let c = cfg("openat2");
    let r = probe_openat2(&c);
    assert_eq!(r.status, Status::Ok, "{r:?}");
    assert_eq!(r.control.kind, ControlKind::Runtime);
    assert_eq!(r.control.result, ControlResult::FailedAsExpected);
    assert_eq!(
        std::fs::read_dir(&c.scratch).unwrap().count(),
        0,
        "scratch removed"
    );
}

#[test]
fn rename_noreplace_is_observed_with_a_discriminating_control() {
    let c = cfg("rename");
    let r = probe_rename(&c);
    assert_eq!(r.status, Status::Ok, "{r:?}");
    assert_eq!(r.control.result, ControlResult::FailedAsExpected);
    assert_eq!(
        std::fs::read_dir(&c.scratch).unwrap().count(),
        0,
        "scratch removed"
    );
}

#[test]
fn absent_scratch_root_is_missing_not_inferred() {
    let c = Config {
        scratch: "/nonexistent/writ-probe-scratch".into(),
        ..Config::reference()
    };
    assert_eq!(probe_openat2(&c).status, Status::Missing);
    assert_eq!(probe_rename(&c).status, Status::Missing);
}
