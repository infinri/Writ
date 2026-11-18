use std::ffi::CString;
use std::fs::File;
use std::io::{Cursor, Read};
use std::os::unix::fs::MetadataExt;
use std::time::{Duration, Instant};

use rustix::fs::{
    fstatfs, openat2, readlinkat, statx, AtFlags, Mode, OFlags, ResolveFlags, StatxFlags, CWD,
};
use rustix::process::{geteuid, getuid};

use super::{check_deadline, read_at, read_file, Request};
use crate::desktop_collect::{Input, Problem, Source};

const ACCOUNT_LIMIT: usize = 1024 * 1024;
const PROC_SUPER_MAGIC: i64 = 0x9fa0;
const INITIAL_PID_NS: u64 = 0xefff_fffc;
const INITIAL_USER_NS: u64 = 0xefff_fffd;

pub(super) struct Files {
    root: File,
    etc: File,
    marker: File,
    pub(super) proc: File,
    pub(super) current: File,
    init: File,
    vm: String,
    boot_id: String,
    deadline: Instant,
}

pub(super) fn admit(request: &Request, deadline: Instant) -> Result<Files, String> {
    let etc = open_dir(CWD, "/etc", false).map_err(|_| "cannot open /etc")?;
    let marker = verify_marker(&etc, &request.name, deadline)?;
    if !getuid().is_root() || !geteuid().is_root() {
        return Err("guest inventory requires root inside the designated VM".into());
    }
    verify_platform()?;
    let proc = open_dir(CWD, "/proc", false).map_err(|_| "cannot open guest procfs")?;
    if fstatfs(&proc)
        .map_err(|_| "cannot identify guest procfs")?
        .f_type as i64
        != PROC_SUPER_MAGIC
    {
        return Err("guest procfs required".into());
    }
    let current = open_dir(&proc, &std::process::id().to_string(), true)
        .map_err(|_| "collector is absent from the procfs PID view")?;
    let init = open_dir(&proc, "1", true).map_err(|_| "guest PID 1 is absent")?;
    let root = open_dir(CWD, "/", false).map_err(|_| "cannot pin guest root directory")?;
    let files = Files {
        root,
        etc,
        marker,
        proc,
        current,
        init,
        vm: request.name.clone(),
        boot_id: request.boot_id.clone(),
        deadline,
    };
    files
        .scope()
        .map_err(|problem| format!("guest scope {problem:?}"))?;
    Ok(files)
}

fn verify_marker(etc: &File, name: &str, deadline: Instant) -> Result<File, String> {
    let unavailable = "owner-designated guest marker absent or unreadable";
    let file = trusted_file(etc, "writ-disposable-vm", 128, deadline).map_err(|_| unavailable)?;
    let reader = file.try_clone().map_err(|_| unavailable)?;
    let marker = read_file(reader, 128, deadline).map_err(|_| unavailable)?;
    let expected = format!("writ-disposable-vm/1\nname={name}\n");
    if marker != expected.as_bytes() {
        return Err("owner-designated guest marker mismatch".into());
    }
    Ok(file)
}

fn open_dir(parent: impl rustix::fd::AsFd, path: &str, beneath: bool) -> Result<File, Problem> {
    let mut resolve = ResolveFlags::NO_SYMLINKS;
    if beneath {
        resolve |= ResolveFlags::BENEATH | ResolveFlags::NO_XDEV;
    }
    openat2(
        parent,
        path,
        OFlags::PATH | OFlags::DIRECTORY | OFlags::CLOEXEC,
        Mode::empty(),
        resolve,
    )
    .map(File::from)
    .map_err(|_| Problem::Read)
}

fn trusted_file(
    directory: &File,
    name: &str,
    limit: usize,
    deadline: Instant,
) -> Result<File, Problem> {
    check_deadline(deadline)?;
    let fd = openat2(
        directory,
        name,
        OFlags::RDONLY | OFlags::NONBLOCK | OFlags::CLOEXEC,
        Mode::empty(),
        ResolveFlags::NO_SYMLINKS | ResolveFlags::NO_XDEV | ResolveFlags::BENEATH,
    )
    .map_err(|_| Problem::Read)?;
    let file = File::from(fd);
    let metadata = file.metadata().map_err(|_| Problem::Read)?;
    if !metadata.is_file()
        || metadata.uid() != 0
        || metadata.gid() != 0
        || metadata.mode() & 0o022 != 0
        || (name == "writ-disposable-vm" && metadata.mode() & 0o7777 != 0o600)
        || metadata.len() > limit as u64
    {
        return Err(Problem::Unsupported);
    }
    Ok(file)
}

fn trusted(
    directory: &File,
    name: &str,
    limit: usize,
    deadline: Instant,
) -> Result<Vec<u8>, Problem> {
    read_file(
        trusted_file(directory, name, limit, deadline)?,
        limit,
        deadline,
    )
}

fn verify_platform() -> Result<(), String> {
    let timeout = Duration::from_secs(2);
    let detect = |mode| {
        crate::exec::run_argv(&["/usr/bin/systemd-detect-virt", mode], timeout)
            .map_err(|_| "virtualization detection unavailable".to_owned())
    };
    let vm = detect("--vm")?;
    let container = detect("--container")?;
    if !vm.success()
        || !matches!(vm.stdout.as_str(), "kvm\n" | "qemu\n")
        || container.code != Some(1)
        || container.stdout != "none\n"
    {
        return Err("designated KVM or QEMU guest required".into());
    }
    Ok(())
}

impl Files {
    fn scope(&self) -> Result<Vec<u8>, Problem> {
        let boot = read_at(&self.proc, "sys/kernel/random/boot_id", 37, self.deadline)?;
        let hostname = read_at(&self.proc, "sys/kernel/hostname", 64, self.deadline)?;
        if boot.strip_suffix(b"\n") != Some(self.boot_id.as_bytes())
            || hostname.strip_suffix(b"\n") != Some(self.vm.as_bytes())
        {
            return Err(Problem::Mismatch);
        }
        let pid = self.namespace("pid")?;
        let user = self.namespace("user")?;
        let net = self.namespace("net")?;
        self.namespace("mnt")?;
        self.namespace("uts")?;
        self.verify_root()?;
        let marker =
            verify_marker(&self.etc, &self.vm, self.deadline).map_err(|_| Problem::Changed)?;
        same_file(&self.marker, &marker)?;
        if pid != INITIAL_PID_NS || user != INITIAL_USER_NS {
            return Err(Problem::Unsupported);
        }
        let mountinfo = read_at(&self.current, "mountinfo", 1024 * 1024, self.deadline)?;
        let (_, mount_id, major, minor) = file_identity(&self.proc)?;
        let proc_device = format!("{major}:{minor}");
        proc_mount(&mountinfo, mount_id, &proc_device)?;
        let nss = trusted(&self.etc, "nsswitch.conf", 64 * 1024, self.deadline)?;
        local_accounts(&nss)?;
        let vendor = open_dir(&self.root, "usr/lib", true)?;
        let os = trusted(&vendor, "os-release", 16 * 1024, self.deadline)?;
        if !ubuntu(&os)? {
            return Err(Problem::Unsupported);
        }
        Ok(format!("{} {pid} {user} {net}\n", self.boot_id).into_bytes())
    }

    fn verify_root(&self) -> Result<(), Problem> {
        check_deadline(self.deadline)?;
        // This one procfs magic link refers to the already pinned PID 1.
        let init_root = openat2(
            &self.init,
            "root",
            OFlags::PATH | OFlags::DIRECTORY | OFlags::CLOEXEC,
            Mode::empty(),
            ResolveFlags::empty(),
        )
        .map(File::from)
        .map_err(|_| Problem::Read)?;
        let current_root = open_dir(CWD, "/", false)?;
        let current_etc = open_dir(&self.root, "etc", true)?;
        same_file(&self.root, &init_root)?;
        same_file(&self.root, &current_root)?;
        same_file(&self.etc, &current_etc)
    }

    fn namespace(&self, name: &str) -> Result<u64, Problem> {
        check_deadline(self.deadline)?;
        let path = format!("ns/{name}");
        let current = readlinkat(&self.current, &path, Vec::new()).map_err(|_| Problem::Read)?;
        let init = readlinkat(&self.init, &path, Vec::new()).map_err(|_| Problem::Read)?;
        if current != init {
            return Err(Problem::Mismatch);
        }
        ns_id(&current, name)
    }
}

impl Source for Files {
    fn open(&mut self, input: Input) -> Result<Box<dyn Read + '_>, Problem> {
        let bytes = match input {
            Input::Scope => self.scope()?,
            Input::Passwd => trusted(&self.etc, "passwd", ACCOUNT_LIMIT, self.deadline)?,
            Input::Group => trusted(&self.etc, "group", ACCOUNT_LIMIT, self.deadline)?,
            #[cfg(test)]
            _ => return Err(Problem::Unsupported),
        };
        Ok(Box::new(Cursor::new(bytes)))
    }
}

fn file_identity(fd: &File) -> Result<(u64, u64, u32, u32), Problem> {
    let mask = StatxFlags::INO | StatxFlags::MNT_ID;
    let identity = statx(fd, "", AtFlags::EMPTY_PATH, mask).map_err(|_| Problem::Read)?;
    if identity.stx_mask & mask.bits() != mask.bits() {
        return Err(Problem::Unsupported);
    }
    Ok((
        identity.stx_ino,
        identity.stx_mnt_id,
        identity.stx_dev_major,
        identity.stx_dev_minor,
    ))
}

fn same_file(first: &File, second: &File) -> Result<(), Problem> {
    if file_identity(first)? != file_identity(second)? {
        return Err(Problem::Changed);
    }
    Ok(())
}

fn ns_id(link: &CString, name: &str) -> Result<u64, Problem> {
    let text = link.to_str().map_err(|_| Problem::Malformed)?;
    let id = text
        .strip_prefix(name)
        .and_then(|text| text.strip_prefix(":["))
        .and_then(|text| text.strip_suffix(']'))
        .ok_or(Problem::Malformed)?;
    if !id.bytes().all(|byte| byte.is_ascii_digit()) {
        return Err(Problem::Malformed);
    }
    id.parse::<u64>()
        .ok()
        .filter(|&id| id != 0)
        .ok_or(Problem::Malformed)
}

fn proc_mount(bytes: &[u8], mount_id: u64, device: &str) -> Result<(), Problem> {
    let mut found = false;
    for line in lines(bytes)? {
        let (before, after) = line.split_once(" - ").ok_or(Problem::Malformed)?;
        let mut fields = before.split_ascii_whitespace();
        let [Some(id), Some(_), Some(dev), Some(root), Some(path), Some(options)] =
            std::array::from_fn(|_| fields.next())
        else {
            return Err(Problem::Malformed);
        };
        if path == "/proc" {
            let mut superblock = after.split_ascii_whitespace();
            let [Some("proc"), Some("proc"), Some(superopts), None] =
                std::array::from_fn(|_| superblock.next())
            else {
                return Err(Problem::Unsupported);
            };
            if found
                || root != "/"
                || dev != device
                || id.parse::<u64>().map_err(|_| Problem::Malformed)? != mount_id
                || !options.split(',').chain(superopts.split(',')).all(|opt| {
                    matches!(
                        opt,
                        "rw" | "ro"
                            | "relatime"
                            | "nosuid"
                            | "nodev"
                            | "noexec"
                            | "hidepid=0"
                            | "hidepid=off"
                    )
                })
            {
                return Err(Problem::Unsupported);
            }
            found = true;
        } else if path.starts_with("/proc/") && path != "/proc/sys/fs/binfmt_misc" {
            return Err(Problem::Unsupported);
        }
    }
    if !found {
        return Err(Problem::Incomplete);
    }
    Ok(())
}

fn local_accounts(bytes: &[u8]) -> Result<(), Problem> {
    if bytes
        .iter()
        .any(|byte| byte.is_ascii_control() && !matches!(byte, b'\n' | b'\t'))
    {
        return Err(Problem::Unsupported);
    }
    let mut seen = [false; 3];
    for line in lines(bytes)? {
        let line = line
            .split_once('#')
            .map_or(line, |(line, _)| line)
            .trim_ascii();
        if line.is_empty() {
            continue;
        }
        let (key, rule) = line.split_once(':').ok_or(Problem::Malformed)?;
        let index = match key.trim_ascii() {
            "passwd" => 0,
            "group" => 1,
            "initgroups" => 2,
            _ => continue,
        };
        if seen[index] || rule.trim_ascii() != "files" {
            return Err(Problem::Unsupported);
        }
        seen[index] = true;
    }
    if !seen[0] || !seen[1] {
        return Err(Problem::Incomplete);
    }
    Ok(())
}

fn ubuntu(bytes: &[u8]) -> Result<bool, Problem> {
    let mut id = None;
    let mut version = None;
    for line in lines(bytes)? {
        if let Some((key, value)) = line.split_once('=') {
            let slot = match key {
                "ID" => &mut id,
                "VERSION_ID" => &mut version,
                _ => continue,
            };
            if slot.replace(value).is_some() {
                return Err(Problem::Duplicate);
            }
        }
    }
    Ok(matches!(id, Some("ubuntu" | "\"ubuntu\""))
        && matches!(version, Some("24.04" | "\"24.04\"")))
}

fn lines(bytes: &[u8]) -> Result<impl Iterator<Item = &str>, Problem> {
    let text = std::str::from_utf8(bytes).map_err(|_| Problem::Malformed)?;
    if !text.ends_with('\n') || text.contains('\0') {
        return Err(Problem::Incomplete);
    }
    Ok(text.split_terminator('\n'))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn missing_marker_refuses_before_admission() -> std::io::Result<()> {
        let path = std::env::temp_dir().join(format!("writ-markerless-{}", std::process::id()));
        std::fs::create_dir(&path)?;
        let etc = File::open(&path)?;
        let result = verify_marker(&etc, "writ-ref-1", Instant::now() + Duration::from_secs(1));
        std::fs::remove_dir(&path)?;
        assert_eq!(
            result.err().as_deref(),
            Some("owner-designated guest marker absent or unreadable")
        );
        Ok(())
    }

    #[test]
    fn rejects_filtered_rebound_and_truncated_procfs() {
        let good = "22 1 0:19 / /proc rw,nosuid,nodev,noexec,relatime - proc proc rw\n";
        assert_eq!(proc_mount(good.as_bytes(), 22, "0:19"), Ok(()));
        assert_eq!(
            proc_mount(good.as_bytes(), 22, "0:20"),
            Err(Problem::Unsupported)
        );
        assert_eq!(
            proc_mount(good.as_bytes(), 23, "0:19"),
            Err(Problem::Unsupported)
        );
        for options in ["hidepid=2", "gid=123", "subset=pid"] {
            let filtered = format!("{},{options}\n", good.trim_end());
            assert_eq!(
                proc_mount(filtered.as_bytes(), 22, "0:19"),
                Err(Problem::Unsupported)
            );
        }
        let narrowed = good.replace("0:19 / /proc", "0:19 /1 /proc");
        assert_eq!(
            proc_mount(narrowed.as_bytes(), 22, "0:19"),
            Err(Problem::Unsupported)
        );
        let overlaid = format!("{good}23 22 0:19 /1 /proc/79 rw - proc proc rw\n");
        assert_eq!(
            proc_mount(overlaid.as_bytes(), 22, "0:19"),
            Err(Problem::Unsupported)
        );
        assert_eq!(
            proc_mount(good.trim_end().as_bytes(), 22, "0:19"),
            Err(Problem::Incomplete)
        );
    }

    #[test]
    fn accepts_only_a_complete_local_account_configuration() {
        assert_eq!(
            local_accounts(b"# guest\npasswd: files\ngroup: files # local\n"),
            Ok(())
        );
        assert_eq!(
            local_accounts(b"passwd: files\ngroup: files\ninitgroups: files\n"),
            Ok(())
        );
        for nss in [
            b"passwd: files systemd\ngroup: files\n".as_slice(),
            b"passwd: files\ngroup: files\ninitgroups: sss files\n",
            b"passwd: files\ngroup: files\ngroup: files\n",
            b"passwd: files [SUCCESS=return]\ngroup: files\n",
            "passwd: files\u{a0}\ngroup: files\n".as_bytes(),
            b"passwd: files\ngroup: files\n\x0binitgroups: sss\n",
        ] {
            assert_eq!(local_accounts(nss), Err(Problem::Unsupported));
        }
        assert_eq!(local_accounts(b"passwd: files\n"), Err(Problem::Incomplete));
    }
}
