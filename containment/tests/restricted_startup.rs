//! Exercise the local startup primitives with the specific systemd syscall restriction.
#![allow(clippy::unwrap_used)]
use std::fs;
use std::os::unix::fs::symlink;
use std::path::Path;
use std::process::Command;
use writ_containment::{cgroup::Cgroups, cgroup::UnitPath, fdplan, launcher, source};

#[test]
fn restricted_startup() {
    if std::env::var_os("WRIT_RESTRICTED_TEST_CHILD").is_none() {
        let status = Command::new("python3")
            .arg(concat!(
                env!("CARGO_MANIFEST_DIR"),
                "/tests/restricted-startup.py"
            ))
            .arg(std::env::current_exe().unwrap())
            .status()
            .unwrap();
        assert!(status.success());
        return;
    }
    use rustix::fs::{openat2, Mode, OFlags, ResolveFlags, CWD};
    assert_eq!(
        openat2(
            CWD,
            "/",
            OFlags::DIRECTORY,
            Mode::empty(),
            ResolveFlags::empty()
        )
        .unwrap_err(),
        rustix::io::Errno::NOSYS
    );
    let extra = fs::File::open("/").unwrap();
    let proc = Path::new("/proc");
    assert!(launcher::take_connection(proc)
        .unwrap_err()
        .to_string()
        .contains("descriptor set"));
    drop(extra);
    let conn = launcher::take_connection(proc).unwrap();
    let slots = fdplan::Slots::reserve().unwrap();
    let listed = fdplan::list_own_fds(proc).unwrap();
    assert_eq!(
        listed,
        [0, 1, 2, 3, 4, std::os::fd::AsRawFd::as_raw_fd(&conn)]
    );
    drop(slots);

    let root = std::env::temp_dir().join(format!("writ-restricted-startup-{}", std::process::id()));
    fs::create_dir(&root).unwrap();
    fs::create_dir_all(root.join("slice/unit")).unwrap();
    fs::write(root.join("slice/unit/memory.max"), "42").unwrap();
    let unit = |rel: &str| UnitPath {
        rel: rel.to_owned(),
        instance: "1".into(),
    };
    let target = fs::File::open(&root).unwrap();
    let magic = format!(
        "{}/fd/{}",
        std::process::id(),
        std::os::fd::AsRawFd::as_raw_fd(&target)
    );
    assert!(Cgroups::open(Path::new("/proc"), &unit(&magic)).is_err());
    let cg = Cgroups::open(&root, &unit("slice/unit")).unwrap();
    assert_eq!(cg.unit_memory_max(), Some(42));
    symlink("slice", root.join("alias")).unwrap();
    symlink("unit", root.join("slice/alias")).unwrap();
    for bad in ["../slice", "/slice/unit", "alias/unit", "slice/alias"] {
        assert!(Cgroups::open(&root, &unit(bad)).is_err(), "{bad}");
    }
    assert!(Cgroups::open(&root.join("alias"), &unit("unit")).is_err());
    fs::rename(root.join("slice/unit/memory.max"), root.join("limit")).unwrap();
    symlink(root.join("limit"), root.join("slice/unit/memory.max")).unwrap();
    assert_eq!(cg.unit_memory_max(), None);
    let dir = fs::File::open(&root).unwrap();
    use std::os::fd::AsFd;
    assert!(
        matches!(source::open_checked(dir.as_fd(), c"limit", rustix::process::getuid().as_raw()),
            Err(source::CopyError::Source(message)) if message.contains("os error 38"))
    );
    fs::remove_dir_all(root).unwrap();
}
