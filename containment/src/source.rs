//! The source copy (J-11.3, M4, M5, N9). The source is always `run-<job_id>` beneath the
//! source root (`/var/lib/writ/run`, which PID 1 binds read-only into the launcher's view); the
//! launcher reads it by **group** membership, never by DAC override. The walk is iterative (an
//! explicit stack), by descriptor:
//!
//! 1. `statx` with `AT_SYMLINK_NOFOLLOW` first; anything other than a directory or regular file
//!    refuses;
//! 2. `openat2` beneath the parent descriptor with `RESOLVE_BENEATH | RESOLVE_NO_SYMLINKS |
//!    RESOLVE_NO_MAGICLINKS | RESOLVE_NO_XDEV` and `O_NONBLOCK | O_NOFOLLOW | O_NOCTTY |
//!    O_CLOEXEC` (plus `O_DIRECTORY` for directories);
//! 3. `fstat`, requiring the same `st_dev` and `st_ino` as the `statx` (a swap between the two
//!    refuses) and re-checking type, owner and mode on the opened object.
//!
//! Every entry is owned by `writ`, not group- or other-writable, without an access or default
//! ACL; regular files have `nlink == 1`. Depth, entry count and **bytes actually read** are
//! bounded. Copies go to `<job dir>/work/` (directories 0700, files 0600, the program 0700) and
//! each copied entry is passed to the [`Owner`] hook (production: `fchown` to `writ-job`). Any
//! violation refuses before the helper is spawned.

use rustix::fs::{
    fgetxattr, fstat, makedev, mkdirat, openat2, statx, AtFlags, Dir, FileType, Mode, OFlags,
    ResolveFlags, StatxFlags, CWD,
};
use rustix::io::Errno;
use std::ffi::{CStr, CString};
use std::fs::File;
use std::io::{Read, Write};
use std::os::fd::{AsFd, BorrowedFd, OwnedFd};
use std::path::Path;

/// Resolve flags of every source open.
pub const RESOLVE: ResolveFlags = ResolveFlags::BENEATH
    .union(ResolveFlags::NO_SYMLINKS)
    .union(ResolveFlags::NO_MAGICLINKS)
    .union(ResolveFlags::NO_XDEV);

/// Rules of one copy.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct Rules {
    /// Required owner of every source entry (the `writ` uid).
    pub owner_uid: u32,
    /// Deepest directory nesting below `run-<job_id>`.
    pub max_depth: usize,
    /// Most entries.
    pub max_entries: usize,
    /// Most bytes read (the request's `input_max_bytes`).
    pub max_bytes: u64,
}

/// Why a copy failed.
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum CopyError {
    /// The source violates J-11.3 (`LAUNCH_SOURCE_INVALID`).
    Source(String),
    /// The destination could not be written (`LAUNCH_UNAVAILABLE`).
    Destination(String),
}

/// What the copy did.
#[derive(Clone, Copy, Debug, Default, PartialEq, Eq)]
pub struct Copied {
    /// Entries copied (directories and files, not counting `work/`).
    pub entries: usize,
    /// Bytes read from the source.
    pub bytes: u64,
}

/// Ownership hook applied to every created entry (and `work/` itself).
pub trait Owner {
    /// Give the created object `fd` to the job identity.
    fn own(&mut self, fd: BorrowedFd<'_>) -> std::io::Result<()>;
}

/// Production hook: `fchown` to the job uid and gid.
pub struct Chown {
    /// Job uid.
    pub uid: u32,
    /// Job gid.
    pub gid: u32,
}

impl Owner for Chown {
    fn own(&mut self, fd: BorrowedFd<'_>) -> std::io::Result<()> {
        rustix::fs::fchown(
            fd,
            Some(rustix::fs::Uid::from_raw(self.uid)),
            Some(rustix::fs::Gid::from_raw(self.gid)),
        )?;
        Ok(())
    }
}

fn src(e: impl std::fmt::Display) -> CopyError {
    CopyError::Source(e.to_string())
}

fn dst(e: impl std::fmt::Display) -> CopyError {
    CopyError::Destination(e.to_string())
}

/// Whether `fd` carries an access or default ACL (absent xattr support counts as none).
pub fn has_acl(fd: BorrowedFd<'_>) -> Result<bool, Errno> {
    let mut buf = [0u8; 256];
    for name in ["system.posix_acl_access", "system.posix_acl_default"] {
        match fgetxattr(fd, name, &mut buf[..]) {
            Ok(_) => return Ok(true),
            Err(Errno::RANGE) => return Ok(true),
            Err(Errno::NODATA) | Err(Errno::OPNOTSUPP) => {}
            Err(e) => return Err(e),
        }
    }
    Ok(false)
}

fn open_beneath(parent: BorrowedFd<'_>, name: &CStr, dir: bool) -> Result<OwnedFd, Errno> {
    let mut flags =
        OFlags::RDONLY | OFlags::NONBLOCK | OFlags::NOFOLLOW | OFlags::NOCTTY | OFlags::CLOEXEC;
    if dir {
        flags |= OFlags::DIRECTORY;
    }
    openat2(parent, name, flags, Mode::empty(), RESOLVE)
}

/// Identity and type of an entry from `statx` with `AT_SYMLINK_NOFOLLOW`.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct Expected {
    /// Device.
    pub dev: u64,
    /// Inode.
    pub ino: u64,
    /// Directory (else regular file).
    pub dir: bool,
}

/// Step 1: `statx` without following; anything but a directory or regular file refuses.
pub fn stat_entry(parent: BorrowedFd<'_>, name: &CStr) -> Result<Expected, CopyError> {
    let what = name.to_string_lossy();
    let sx = statx(
        parent,
        name,
        AtFlags::SYMLINK_NOFOLLOW,
        StatxFlags::TYPE | StatxFlags::MODE | StatxFlags::UID | StatxFlags::NLINK | StatxFlags::INO,
    )
    .map_err(|e| src(format!("{what}: statx {e}")))?;
    let dir = match FileType::from_raw_mode(u32::from(sx.stx_mode)) {
        FileType::Directory => true,
        FileType::RegularFile => false,
        other => {
            return Err(src(format!(
                "{what}: {other:?} is not a directory or regular file"
            )))
        }
    };
    Ok(Expected {
        dev: makedev(sx.stx_dev_major, sx.stx_dev_minor),
        ino: sx.stx_ino,
        dir,
    })
}

/// Steps 2 and 3: open beneath `parent` and require the `fstat` identity to equal `exp`, then
/// check type, owner, mode, link count and ACLs on the opened object.
pub fn open_expected(
    parent: BorrowedFd<'_>,
    name: &CStr,
    exp: &Expected,
    owner_uid: u32,
) -> Result<OwnedFd, CopyError> {
    let what = name.to_string_lossy();
    let fd = open_beneath(parent, name, exp.dir).map_err(|e| src(format!("{what}: open {e}")))?;
    let st = fstat(&fd).map_err(|e| src(format!("{what}: fstat {e}")))?;
    if st.st_dev != exp.dev || st.st_ino != exp.ino {
        return Err(src(format!("{what}: changed between statx and open")));
    }
    check_object(&fd, &st, exp.dir, owner_uid).map_err(|e| src(format!("{what}: {e}")))?;
    Ok(fd)
}

/// One entry: [`stat_entry`] then [`open_expected`]. Returns the descriptor and whether it is
/// a directory.
pub fn open_checked(
    parent: BorrowedFd<'_>,
    name: &CStr,
    owner_uid: u32,
) -> Result<(OwnedFd, bool), CopyError> {
    let exp = stat_entry(parent, name)?;
    Ok((open_expected(parent, name, &exp, owner_uid)?, exp.dir))
}

fn check_object(
    fd: &OwnedFd,
    st: &rustix::fs::Stat,
    dir: bool,
    owner_uid: u32,
) -> Result<(), String> {
    let ft = FileType::from_raw_mode(st.st_mode);
    if (dir && ft != FileType::Directory) || (!dir && ft != FileType::RegularFile) {
        return Err("type changed".into());
    }
    if st.st_uid != owner_uid {
        return Err(format!("owner {} is not {owner_uid}", st.st_uid));
    }
    if st.st_mode & 0o022 != 0 {
        return Err(format!(
            "mode {:o} is group- or other-writable",
            st.st_mode & 0o7777
        ));
    }
    if !dir && st.st_nlink != 1 {
        return Err(format!("nlink {}", st.st_nlink));
    }
    match has_acl(fd.as_fd()) {
        Ok(false) => Ok(()),
        Ok(true) => Err("carries an ACL".into()),
        Err(e) => Err(format!("ACL check failed: {e}")),
    }
}

fn names(fd: &OwnedFd) -> Result<Vec<CString>, CopyError> {
    let mut out = Vec::new();
    let mut d = Dir::read_from(fd).map_err(src)?;
    while let Some(e) = d.read() {
        let e = e.map_err(src)?;
        let n = e.file_name();
        if n.to_bytes() != b"." && n.to_bytes() != b".." {
            out.push(n.to_owned());
        }
    }
    out.sort();
    Ok(out)
}

/// Copy `run-<job_id>` from `source_root` into `work` created beneath `job_dir`; `program` is
/// the request's canonical relative path, which must be a copied regular file.
pub fn copy(
    source_root: &Path,
    job_id: &str,
    job_dir: BorrowedFd<'_>,
    program: &str,
    rules: &Rules,
    owner: &mut dyn Owner,
) -> Result<Copied, CopyError> {
    if !crate::protocol::valid_job_id(job_id) {
        return Err(src("job_id"));
    }
    let root = openat2(
        CWD,
        source_root,
        OFlags::RDONLY | OFlags::DIRECTORY | OFlags::NOFOLLOW | OFlags::CLOEXEC,
        Mode::empty(),
        ResolveFlags::NO_SYMLINKS | ResolveFlags::NO_MAGICLINKS,
    )
    .map_err(|e| src(format!("source root: {e}")))?;
    let run = CString::new(format!("run-{job_id}")).map_err(src)?;
    let (top, is_dir) = open_checked(root.as_fd(), &run, rules.owner_uid)?;
    if !is_dir {
        return Err(src("run directory is not a directory"));
    }
    mkdirat(job_dir, "work", Mode::from_raw_mode(0o700)).map_err(|e| dst(format!("work: {e}")))?;
    let work = open_beneath(job_dir, c"work", true).map_err(|e| dst(format!("work: {e}")))?;
    owner
        .own(work.as_fd())
        .map_err(|e| dst(format!("own work: {e}")))?;
    let mut stats = Copied::default();
    let mut program_seen = false;
    // (source dir, destination dir, depth, relative path of the directory)
    let mut stack: Vec<(OwnedFd, OwnedFd, usize, String)> = vec![(top, work, 0, String::new())];
    while let Some((sdir, ddir, depth, rel)) = stack.pop() {
        for name in names(&sdir)? {
            let text = name
                .to_str()
                .map_err(|_| src("entry name is not UTF-8"))?
                .to_string();
            let path = if rel.is_empty() {
                text.clone()
            } else {
                format!("{rel}/{text}")
            };
            stats.entries += 1;
            if stats.entries > rules.max_entries {
                return Err(src("too many entries"));
            }
            let (fd, dir) = open_checked(sdir.as_fd(), &name, rules.owner_uid)?;
            if dir {
                if depth + 1 > rules.max_depth {
                    return Err(src(format!("{path}: too deep")));
                }
                mkdirat(&ddir, name.as_c_str(), Mode::from_raw_mode(0o700))
                    .map_err(|e| dst(format!("{path}: {e}")))?;
                let d = open_beneath(ddir.as_fd(), &name, true)
                    .map_err(|e| dst(format!("{path}: {e}")))?;
                owner
                    .own(d.as_fd())
                    .map_err(|e| dst(format!("own {path}: {e}")))?;
                stack.push((fd, d, depth + 1, path));
            } else {
                let is_program = path == program;
                program_seen |= is_program;
                let mode = if is_program { 0o700 } else { 0o600 };
                let out = openat2(
                    &ddir,
                    name.as_c_str(),
                    OFlags::WRONLY
                        | OFlags::CREATE
                        | OFlags::EXCL
                        | OFlags::NOFOLLOW
                        | OFlags::CLOEXEC,
                    Mode::from_raw_mode(mode),
                    RESOLVE,
                )
                .map_err(|e| dst(format!("{path}: {e}")))?;
                let remaining = rules.max_bytes.saturating_sub(stats.bytes);
                let n = copy_bounded(fd, &out, remaining).map_err(|e| match e {
                    Bounded::Limit => src("input_max_bytes exceeded"),
                    Bounded::Read(e) => src(format!("{path}: read {e}")),
                    Bounded::Write(e) => dst(format!("{path}: write {e}")),
                })?;
                stats.bytes += n;
                rustix::fs::fchmod(&out, Mode::from_raw_mode(mode))
                    .map_err(|e| dst(format!("{path}: chmod {e}")))?;
                owner
                    .own(out.as_fd())
                    .map_err(|e| dst(format!("own {path}: {e}")))?;
            }
        }
    }
    if !program_seen {
        return Err(src("program is not a regular file in the source"));
    }
    Ok(stats)
}

enum Bounded {
    Limit,
    Read(std::io::Error),
    Write(std::io::Error),
}

/// Copy at most `limit` bytes; reading one byte more than `limit` is a refusal. The bound is
/// applied to bytes actually read, never to `st_size`.
fn copy_bounded(from: OwnedFd, to: &OwnedFd, limit: u64) -> Result<u64, Bounded> {
    let mut f = File::from(from);
    let mut out = File::from(to.try_clone().map_err(Bounded::Write)?);
    let mut buf = vec![0u8; 64 * 1024];
    let mut total = 0u64;
    loop {
        let n = match f.read(&mut buf) {
            Ok(0) => return Ok(total),
            Ok(n) => n,
            Err(e) if e.kind() == std::io::ErrorKind::Interrupted => continue,
            Err(e) => return Err(Bounded::Read(e)),
        };
        total += n as u64;
        if total > limit {
            return Err(Bounded::Limit);
        }
        out.write_all(&buf[..n]).map_err(Bounded::Write)?;
    }
}

#[cfg(test)]
#[allow(clippy::unwrap_used, clippy::expect_used, clippy::panic)]
mod tests;
