use super::*;
use crate::testutil::Scratch;
use std::fs;
use std::os::unix::fs::PermissionsExt;

fn me() -> u32 {
    rustix::process::getuid().as_raw()
}

fn rules() -> Rules {
    Rules {
        owner_uid: me(),
        max_depth: 3,
        max_entries: 16,
        max_bytes: 1024,
    }
}

fn mode(p: &Path, m: u32) {
    fs::set_permissions(p, fs::Permissions::from_mode(m)).unwrap();
}

/// `<root>/src/run-job1/{prog, sub/data}` and an empty `<root>/job` destination.
fn tree() -> Scratch {
    let s = Scratch::new("source");
    let run = s.path().join("src/run-job1");
    fs::create_dir_all(run.join("sub")).unwrap();
    fs::write(run.join("prog"), b"#!/bin/sh\necho hi\n").unwrap();
    fs::write(run.join("sub/data"), b"payload").unwrap();
    for d in [s.path().join("src"), run.clone(), run.join("sub")] {
        mode(&d, 0o750);
    }
    mode(&run.join("prog"), 0o640);
    mode(&run.join("sub/data"), 0o640);
    fs::create_dir(s.path().join("job")).unwrap();
    mode(&s.path().join("job"), 0o711);
    s
}

/// The copy, then (when it succeeded) [`chown_tree`] to this process's own ids; returns the
/// copy result and the number of entries given away.
fn run_copy(s: &Scratch, program: &str, r: &Rules) -> (Result<Copied, CopyError>, usize) {
    let job = File::open(s.path().join("job")).unwrap();
    let res = copy(&s.path().join("src"), "job1", job.as_fd(), program, r);
    let owned = if res.is_ok() {
        let gid = rustix::process::getgid().as_raw();
        chown_tree(job.as_fd(), me(), gid, r.max_entries).unwrap()
    } else {
        0
    };
    (res, owned)
}

fn source_err(r: Result<Copied, CopyError>) -> String {
    match r {
        Err(CopyError::Source(t)) => t,
        other => panic!("expected a source refusal, got {other:?}"),
    }
}

#[test]
fn copies_tree_with_modes_and_owner_hook() {
    let s = tree();
    let (res, owned) = run_copy(&s, "prog", &rules());
    let c = res.unwrap();
    assert_eq!(c.entries, 3);
    assert_eq!(c.bytes, 18 + 7);
    // work, prog, sub, sub/data
    assert_eq!(owned, 4);
    let work = s.path().join("job/work");
    let m = |p: &str| fs::metadata(work.join(p)).unwrap().permissions().mode() & 0o7777;
    assert_eq!(m(""), 0o700);
    assert_eq!(m("prog"), 0o700);
    assert_eq!(m("sub"), 0o700);
    assert_eq!(m("sub/data"), 0o600);
    assert_eq!(fs::read(work.join("sub/data")).unwrap(), b"payload");
}

#[test]
fn program_must_be_a_copied_regular_file() {
    let s = tree();
    assert!(source_err(run_copy(&s, "missing", &rules()).0).contains("program"));
    let s = tree();
    assert!(source_err(run_copy(&s, "sub", &rules()).0).contains("program"));
}

#[test]
fn symlink_fifo_and_hardlink_entries_refuse() {
    let s = tree();
    std::os::unix::fs::symlink("/etc/passwd", s.path().join("src/run-job1/link")).unwrap();
    assert!(source_err(run_copy(&s, "prog", &rules()).0).contains("link"));

    let s = tree();
    rustix::fs::mknodat(
        CWD,
        s.path().join("src/run-job1/fifo").as_path(),
        FileType::Fifo,
        Mode::from_raw_mode(0o600),
        0,
    )
    .unwrap();
    assert!(source_err(run_copy(&s, "prog", &rules()).0).contains("fifo"));

    let s = tree();
    fs::hard_link(
        s.path().join("src/run-job1/prog"),
        s.path().join("other-name"),
    )
    .unwrap();
    assert!(source_err(run_copy(&s, "prog", &rules()).0).contains("nlink"));
}

#[test]
fn writable_or_foreign_entries_refuse() {
    let s = tree();
    mode(&s.path().join("src/run-job1/sub/data"), 0o660);
    assert!(source_err(run_copy(&s, "prog", &rules()).0).contains("writable"));
    let s = tree();
    mode(&s.path().join("src/run-job1/sub"), 0o757);
    assert!(source_err(run_copy(&s, "prog", &rules()).0).contains("writable"));
    let s = tree();
    let foreign = Rules {
        owner_uid: me().wrapping_add(1),
        ..rules()
    };
    assert!(source_err(run_copy(&s, "prog", &foreign).0).contains("owner"));
}

#[test]
fn bounds_on_depth_entries_and_bytes_read() {
    let s = tree();
    let shallow = Rules {
        max_depth: 0,
        ..rules()
    };
    assert!(source_err(run_copy(&s, "prog", &shallow).0).contains("too deep"));
    let s = tree();
    let few = Rules {
        max_entries: 2,
        ..rules()
    };
    assert!(source_err(run_copy(&s, "prog", &few).0).contains("entries"));
    let s = tree();
    let small = Rules {
        max_bytes: 24,
        ..rules()
    };
    assert!(source_err(run_copy(&s, "prog", &small).0).contains("input_max_bytes"));
    let s = tree();
    let exact = Rules {
        max_bytes: 25,
        ..rules()
    };
    assert!(run_copy(&s, "prog", &exact).0.is_ok());
}

#[test]
fn run_directory_must_exist_and_job_id_is_checked() {
    let s = tree();
    let job = File::open(s.path().join("job")).unwrap();
    let r = copy(&s.path().join("src"), "job2", job.as_fd(), "prog", &rules());
    assert!(source_err(r).contains("run-job2"));
    let r = copy(&s.path().join("src"), "../x", job.as_fd(), "prog", &rules());
    assert!(matches!(r, Err(CopyError::Source(_))));
}

#[test]
fn swap_between_statx_and_open_refuses() {
    let s = tree();
    let dir = File::open(s.path().join("src/run-job1")).unwrap();
    let exp = stat_entry(dir.as_fd(), c"prog").unwrap();
    // Another writ-owned file replaces the entry after the statx.
    fs::rename(
        s.path().join("src/run-job1/sub/data"),
        s.path().join("src/run-job1/prog"),
    )
    .unwrap();
    let r = open_expected(dir.as_fd(), c"prog", &exp, me());
    assert!(
        matches!(&r, Err(CopyError::Source(t)) if t.contains("changed between statx and open")),
        "{r:?}"
    );
    // Without a swap the same check passes.
    let exp = stat_entry(dir.as_fd(), c"prog").unwrap();
    assert!(open_expected(dir.as_fd(), c"prog", &exp, me()).is_ok());
}

/// M6: the copy holds a bounded number of descriptors (not one per entry or per pending
/// directory). The inner test runs in a child process with `RLIMIT_NOFILE` 128 over a tree of
/// more than 1024 entries.
#[test]
fn copy_of_a_wide_tree_holds_few_descriptors() {
    let exe = std::env::current_exe().unwrap();
    let out = std::process::Command::new("/bin/sh")
        .arg("-c")
        .arg("ulimit -n 128 && exec \"$0\" --exact source::tests::wide_tree_under_low_nofile --ignored --test-threads=1")
        .arg(&exe)
        .output()
        .unwrap();
    assert!(
        out.status.success(),
        "{}{}",
        String::from_utf8_lossy(&out.stdout),
        String::from_utf8_lossy(&out.stderr)
    );
    assert!(String::from_utf8_lossy(&out.stdout).contains("1 passed"));
}

#[test]
#[ignore = "run by copy_of_a_wide_tree_holds_few_descriptors under ulimit -n 128"]
fn wide_tree_under_low_nofile() {
    let s = tree();
    let run = s.path().join("src/run-job1");
    for i in 0..1100 {
        let d = run.join(format!("d{i:04}"));
        fs::create_dir(&d).unwrap();
        mode(&d, 0o750);
        fs::write(d.join("f"), b"x").unwrap();
        mode(&d.join("f"), 0o640);
        let f = run.join(format!("f{i:04}"));
        fs::write(&f, b"y").unwrap();
        mode(&f, 0o640);
    }
    let r = Rules {
        max_entries: 5000,
        max_bytes: 1 << 20,
        ..rules()
    };
    let (res, owned) = run_copy(&s, "prog", &r);
    assert_eq!(res.unwrap().entries, 3 + 3300);
    assert_eq!(owned, 1 + 3 + 3300);
}

#[test]
fn descriptor_exhaustion_is_a_request_level_refusal() {
    assert!(exhausted(Errno::MFILE) && exhausted(Errno::NFILE));
    assert!(!exhausted(Errno::ACCESS));
    assert!(matches!(
        src_errno("x", Errno::MFILE),
        CopyError::Descriptors(_)
    ));
    assert!(matches!(
        dst_errno("x", Errno::NFILE),
        CopyError::Descriptors(_)
    ));
    assert!(matches!(
        dst_errno("x", Errno::NOSPC),
        CopyError::Destination(_)
    ));
    assert!(matches!(
        io_err("x", "read", std::io::Error::from_raw_os_error(24), false),
        CopyError::Descriptors(_)
    ));
}
