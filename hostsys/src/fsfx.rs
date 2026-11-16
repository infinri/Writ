//! Filesystem primitives of the `fs.write` adapter (M5a-09; D-045 M5-D8;
//! [M5-INTERFACES](../../docs/implementation/M5-INTERFACES.md) J-7.2).
//!
//! `writ-fs-effects` reaches `rustix` only through this module (J-0). Every descriptor is
//! opened with `O_CLOEXEC` (L-g); every resolution below a trusted directory descriptor uses
//! `openat2` with `RESOLVE_BENEATH | RESOLVE_NO_SYMLINKS | RESOLVE_NO_MAGICLINKS |
//! RESOLVE_NO_XDEV`; every `statx` requests `STATX_BTIME` and `STATX_MNT_ID`, and a result
//! without either is an observation failure ([`FxError::NoBirthTime`],
//! [`FxError::NoMountId`]), never an identity without birth time (L-c).
//!
//! This module holds no policy: callers compare identities, owners, modes and mounts.

use std::fs::File;
use std::io::{Read, Write};
use std::os::fd::{AsFd, OwnedFd};
use std::os::unix::fs::MetadataExt;
use std::path::Path;

use rustix::fs::{
    flistxattr, flock, fstatfs, ioctl_getflags, makedev, openat2, renameat_with, statx, unlinkat,
    AtFlags, Dir, FlockOperation, Mode, OFlags, RenameFlags, ResolveFlags, StatxFlags, CWD,
};
use rustix::io::Errno;

/// `FS_CASEFOLD_FL` (not named by rustix's `IFlags`).
const FS_CASEFOLD_FL: u32 = 0x4000_0000;
/// `EXT4_SUPER_MAGIC` (`f_type` of ext2, ext3 and ext4).
pub const EXT4_SUPER_MAGIC: u64 = 0xEF53;
/// `XFS_SUPER_MAGIC`.
pub const XFS_SUPER_MAGIC: u64 = 0x5846_5342;

/// Why a primitive failed.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum FxError {
    /// The system call failed with this `errno`.
    Errno(i32),
    /// `statx` did not report `STATX_BTIME`.
    NoBirthTime,
    /// `statx` did not report `STATX_MNT_ID`.
    NoMountId,
}

impl FxError {
    /// `true` for `ENOENT`.
    pub fn is_not_found(self) -> bool {
        self == FxError::Errno(Errno::NOENT.raw_os_error())
    }

    /// `true` for `EEXIST`.
    pub fn is_exists(self) -> bool {
        self == FxError::Errno(Errno::EXIST.raw_os_error())
    }

    /// A short sanitized name (`errno <n>`, `no_btime`, `no_mnt_id`).
    pub fn code(self) -> String {
        match self {
            FxError::Errno(n) => format!("errno {n}"),
            FxError::NoBirthTime => "no_btime".into(),
            FxError::NoMountId => "no_mnt_id".into(),
        }
    }
}

impl From<Errno> for FxError {
    fn from(e: Errno) -> FxError {
        FxError::Errno(e.raw_os_error())
    }
}

impl From<std::io::Error> for FxError {
    fn from(e: std::io::Error) -> FxError {
        FxError::Errno(e.raw_os_error().unwrap_or(Errno::IO.raw_os_error()))
    }
}

/// `ENOSPC`, for callers that simulate it.
pub const ENOSPC: i32 = 28;
/// `EACCES`.
pub const EACCES: i32 = 13;
/// `EIO`.
pub const EIO: i32 = 5;
/// `EEXIST`.
pub const EEXIST: i32 = 17;

/// One `statx` observation (with birth time and mount id).
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct Observed {
    /// `st_dev` (never persisted, J-7.2).
    pub dev: u64,
    /// Inode number.
    pub ino: u64,
    /// Birth time, seconds.
    pub btime_sec: i64,
    /// Birth time, nanoseconds.
    pub btime_nsec: u32,
    /// Link count.
    pub nlink: u32,
    /// Owner uid.
    pub uid: u32,
    /// `st_mode` (type and permission bits).
    pub mode: u32,
    /// Size in bytes.
    pub size: u64,
    /// Mount id (never persisted, J-7.2).
    pub mnt_id: u64,
}

const S_IFMT: u32 = 0o170_000;

impl Observed {
    /// A directory.
    pub fn is_dir(&self) -> bool {
        self.mode & S_IFMT == 0o040_000
    }

    /// A regular file.
    pub fn is_regular(&self) -> bool {
        self.mode & S_IFMT == 0o100_000
    }

    /// Permission bits.
    pub fn perm(&self) -> u32 {
        self.mode & 0o7777
    }
}

fn mask() -> StatxFlags {
    StatxFlags::BASIC_STATS | StatxFlags::BTIME | StatxFlags::MNT_ID
}

fn observed(x: &rustix::fs::Statx) -> Result<Observed, FxError> {
    if x.stx_mask & StatxFlags::BTIME.bits() == 0 {
        return Err(FxError::NoBirthTime);
    }
    if x.stx_mask & StatxFlags::MNT_ID.bits() == 0 {
        return Err(FxError::NoMountId);
    }
    Ok(Observed {
        dev: makedev(x.stx_dev_major, x.stx_dev_minor),
        ino: x.stx_ino,
        btime_sec: x.stx_btime.tv_sec,
        btime_nsec: x.stx_btime.tv_nsec,
        nlink: x.stx_nlink,
        uid: x.stx_uid,
        mode: u32::from(x.stx_mode),
        size: x.stx_size,
        mnt_id: x.stx_mnt_id,
    })
}

/// `statx` of an open descriptor.
pub fn observe(fd: impl AsFd) -> Result<Observed, FxError> {
    observed(&statx(fd, "", AtFlags::EMPTY_PATH, mask())?)
}

/// `statx` of `name` (one component) in `dir`, not following a final symlink; `None` when
/// it does not exist.
pub fn observe_at(dir: impl AsFd, name: &str) -> Result<Option<Observed>, FxError> {
    match statx(dir, name, AtFlags::SYMLINK_NOFOLLOW, mask()) {
        Ok(x) => observed(&x).map(Some),
        Err(Errno::NOENT) => Ok(None),
        Err(e) => Err(e.into()),
    }
}

fn beneath() -> ResolveFlags {
    ResolveFlags::BENEATH
        | ResolveFlags::NO_SYMLINKS
        | ResolveFlags::NO_MAGICLINKS
        | ResolveFlags::NO_XDEV
}

const DIR_FLAGS: OFlags = OFlags::RDONLY
    .union(OFlags::DIRECTORY)
    .union(OFlags::NOFOLLOW)
    .union(OFlags::CLOEXEC);

/// Open the absolute directory `path` without following any symlink (`RESOLVE_NO_SYMLINKS`).
pub fn open_dir_path(path: &Path) -> Result<OwnedFd, FxError> {
    Ok(openat2(
        CWD,
        path,
        DIR_FLAGS,
        Mode::empty(),
        ResolveFlags::NO_SYMLINKS | ResolveFlags::NO_MAGICLINKS,
    )?)
}

/// Open the directory `rel` beneath `dir` (J-7.2 resolution flags).
pub fn open_dir_beneath(dir: impl AsFd, rel: &str) -> Result<OwnedFd, FxError> {
    Ok(openat2(dir, rel, DIR_FLAGS, Mode::empty(), beneath())?)
}

/// Open the parent of `dir` (`..`), for the overlap walk (J-7.1).
pub fn open_parent(dir: impl AsFd) -> Result<OwnedFd, FxError> {
    Ok(openat2(
        dir,
        "..",
        DIR_FLAGS,
        Mode::empty(),
        ResolveFlags::NO_SYMLINKS | ResolveFlags::NO_MAGICLINKS,
    )?)
}

/// Open the file `rel` beneath `dir` for reading (`O_NONBLOCK` so a FIFO never blocks).
pub fn open_read_beneath(dir: impl AsFd, rel: &str) -> Result<OwnedFd, FxError> {
    Ok(openat2(
        dir,
        rel,
        OFlags::RDONLY | OFlags::NOFOLLOW | OFlags::CLOEXEC | OFlags::NONBLOCK | OFlags::NOCTTY,
        Mode::empty(),
        beneath(),
    )?)
}

/// Create `name` in `dir` exclusively (`O_CREAT | O_EXCL | O_WRONLY | O_NOFOLLOW |
/// O_CLOEXEC`) with mode `0600`.
pub fn create_exclusive(dir: impl AsFd, name: &str) -> Result<OwnedFd, FxError> {
    Ok(openat2(
        dir,
        name,
        OFlags::CREATE | OFlags::EXCL | OFlags::WRONLY | OFlags::NOFOLLOW | OFlags::CLOEXEC,
        Mode::from_raw_mode(0o600),
        beneath(),
    )?)
}

/// Write all of `bytes` to `fd`.
pub fn write_all(fd: &OwnedFd, bytes: &[u8]) -> Result<(), FxError> {
    let mut f = File::from(fd.try_clone()?);
    f.write_all(bytes)?;
    Ok(())
}

/// Read `fd` from its current offset to the end, at most `limit` bytes (more is an error).
pub fn read_to_end(fd: OwnedFd, limit: u64) -> Result<Vec<u8>, FxError> {
    let mut out = Vec::new();
    File::from(fd)
        .take(limit.saturating_add(1))
        .read_to_end(&mut out)?;
    if out.len() as u64 > limit {
        return Err(FxError::Errno(Errno::FBIG.raw_os_error()));
    }
    Ok(out)
}

/// `fsync` of a file or directory descriptor.
pub fn fsync(fd: impl AsFd) -> Result<(), FxError> {
    Ok(rustix::fs::fsync(fd)?)
}

/// `renameat2(..., RENAME_NOREPLACE)`.
pub fn rename_noreplace(
    from_dir: impl AsFd,
    from: &str,
    to_dir: impl AsFd,
    to: &str,
) -> Result<(), FxError> {
    Ok(renameat_with(
        from_dir,
        from,
        to_dir,
        to,
        RenameFlags::NOREPLACE,
    )?)
}

/// `renameat2(..., RENAME_EXCHANGE)`.
pub fn rename_exchange(
    from_dir: impl AsFd,
    from: &str,
    to_dir: impl AsFd,
    to: &str,
) -> Result<(), FxError> {
    Ok(renameat_with(
        from_dir,
        from,
        to_dir,
        to,
        RenameFlags::EXCHANGE,
    )?)
}

/// Remove the file `name` from `dir`.
pub fn unlink_at(dir: impl AsFd, name: &str) -> Result<(), FxError> {
    Ok(unlinkat(dir, name, AtFlags::empty())?)
}

/// Take an exclusive `flock` without waiting; `Ok(false)` when another descriptor holds it.
pub fn try_lock_exclusive(fd: impl AsFd) -> Result<bool, FxError> {
    match flock(fd, FlockOperation::NonBlockingLockExclusive) {
        Ok(()) => Ok(true),
        Err(Errno::WOULDBLOCK) => Ok(false),
        Err(e) => Err(e.into()),
    }
}

/// Which POSIX ACL extended attributes `fd` carries: (`access`, `default`). A filesystem
/// without extended attributes (`EOPNOTSUPP`) carries none.
pub fn acls(fd: impl AsFd) -> Result<(bool, bool), FxError> {
    let mut buf = vec![0u8; 65536];
    let n = match flistxattr(fd, &mut buf[..]) {
        Ok(n) => n,
        Err(Errno::OPNOTSUPP) => return Ok((false, false)),
        Err(e) => return Err(e.into()),
    };
    let names: Vec<&[u8]> = buf
        .get(..n)
        .unwrap_or_default()
        .split(|b| *b == 0)
        .collect();
    Ok((
        names.contains(&&b"system.posix_acl_access"[..]),
        names.contains(&&b"system.posix_acl_default"[..]),
    ))
}

/// The filesystem type (`statfs` `f_type`).
pub fn fs_magic(fd: impl AsFd) -> Result<u64, FxError> {
    Ok(fstatfs(fd)?.f_type as u64)
}

/// Whether the directory `fd` has the case-folding flag (`FS_CASEFOLD_FL`).
pub fn casefold(fd: impl AsFd) -> Result<bool, FxError> {
    Ok(ioctl_getflags(fd)?.bits() & FS_CASEFOLD_FL != 0)
}

/// The names in the directory `fd` (without `.` and `..`).
pub fn list_dir(fd: impl AsFd) -> Result<Vec<String>, FxError> {
    let mut out = Vec::new();
    for entry in Dir::read_from(fd)? {
        let entry = entry?;
        let name = entry.file_name().to_bytes();
        if name == b"." || name == b".." {
            continue;
        }
        out.push(String::from_utf8_lossy(name).into_owned());
    }
    Ok(out)
}

/// The device number a block-device path (for example `/dev/disk/by-uuid/<uuid>`) resolves
/// to (`st_rdev`, following symlinks); `None` when it is missing or not a block device.
pub fn block_device(path: &Path) -> Option<u64> {
    use std::os::unix::fs::FileTypeExt;
    let m = std::fs::metadata(path).ok()?;
    m.file_type().is_block_device().then(|| m.rdev())
}

/// The current boot id (`/proc/sys/kernel/random/boot_id`), trimmed.
pub fn boot_id() -> Result<String, FxError> {
    let text = std::fs::read_to_string("/proc/sys/kernel/random/boot_id")?;
    Ok(text.trim().to_string())
}

/// The effective uid of this process.
pub fn euid() -> u32 {
    rustix::process::geteuid().as_raw()
}

/// The start time (clock ticks since boot, `/proc/<pid>/stat` field 22) of `pid`, `None` when
/// there is no such process.
pub fn process_start(pid: u32) -> Option<u64> {
    let text = std::fs::read_to_string(format!("/proc/{pid}/stat")).ok()?;
    let rest = text.get(text.rfind(')')? + 2..)?;
    rest.split(' ').nth(19)?.parse().ok()
}

#[cfg(test)]
#[allow(clippy::unwrap_used, clippy::expect_used, clippy::panic)]
mod tests {
    use super::*;

    fn tmp(name: &str) -> std::path::PathBuf {
        let d = std::env::temp_dir().join(format!("writ-fsfx-{}-{name}", std::process::id()));
        let _ = std::fs::remove_dir_all(&d);
        std::fs::create_dir_all(&d).unwrap();
        d
    }

    #[test]
    fn statx_reports_birth_time_and_mount_id_and_beneath_refuses_escapes() {
        let d = tmp("statx");
        std::fs::create_dir(d.join("sub")).unwrap();
        std::fs::write(d.join("sub/f"), b"x").unwrap();
        std::os::unix::fs::symlink("/etc", d.join("link")).unwrap();
        let root = open_dir_path(&d).unwrap();
        let o = observe(&root).unwrap();
        assert!(o.is_dir());
        let sub = open_dir_beneath(&root, "sub").unwrap();
        let f = observe_at(&sub, "f").unwrap().unwrap();
        assert!(f.is_regular() && f.nlink == 1 && f.size == 1);
        assert_eq!(observe_at(&sub, "missing").unwrap(), None);
        assert!(open_dir_beneath(&root, "link").is_err());
        assert!(open_dir_beneath(&root, "../").is_err());
        assert!(open_read_beneath(&sub, "../sub/f").is_err());
        let c = create_exclusive(&sub, "g").unwrap();
        write_all(&c, b"abc").unwrap();
        assert!(create_exclusive(&sub, "g").unwrap_err().is_exists());
        let r = open_read_beneath(&sub, "g").unwrap();
        assert_eq!(read_to_end(r, 3).unwrap(), b"abc");
        assert!(read_to_end(open_read_beneath(&sub, "g").unwrap(), 2).is_err());
        rename_noreplace(&sub, "g", &sub, "h").unwrap();
        assert!(rename_noreplace(&sub, "h", &sub, "f")
            .unwrap_err()
            .is_exists());
        rename_exchange(&sub, "h", &sub, "f").unwrap();
        assert_eq!(std::fs::read(d.join("sub/f")).unwrap(), b"abc");
        unlink_at(&sub, "h").unwrap();
        let mut names = list_dir(&sub).unwrap();
        names.sort();
        assert_eq!(names, vec!["f".to_string()]);
        assert_eq!(acls(&sub).unwrap(), (false, false));
        assert!(fs_magic(&root).is_ok());
        assert!(!casefold(&root).unwrap());
        let held = open_dir_beneath(&root, "sub").unwrap();
        assert!(try_lock_exclusive(&held).unwrap());
        let other = open_dir_path(&d.join("sub")).unwrap();
        assert!(!try_lock_exclusive(&other).unwrap());
        assert!(process_start(std::process::id()).is_some());
        assert!(!boot_id().unwrap().is_empty());
        std::fs::remove_dir_all(&d).unwrap();
    }
}
