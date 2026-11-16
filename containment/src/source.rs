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
//! bounded. Copies go to `<job dir>/work/` (directories 0700, files 0600, the program 0700);
//! after the whole copy succeeded [`chown_tree`] gives every entry to `writ-job`, children
//! before parents. Both walks hold only the directories on the current path open (M6). Any
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
    /// The walk ran out of descriptors (`EMFILE`/`ENFILE`): a request-level refusal
    /// (`LAUNCH_SOURCE_INVALID`) that does not withdraw readiness (M6).
    Descriptors(String),
}

/// What the copy did.
#[derive(Clone, Copy, Debug, Default, PartialEq, Eq)]
pub struct Copied {
    /// Entries copied (directories and files, not counting `work/`).
    pub entries: usize,
    /// Bytes read from the source.
    pub bytes: u64,
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
    let fd =
        open_beneath(parent, name, exp.dir).map_err(|e| src_errno(&format!("{what}: open"), e))?;
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
    let mut d = Dir::read_from(fd).map_err(|e| src_errno("directory", e))?;
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

/// One directory being copied: its source and destination descriptors and the names still
/// to visit. Only the directories on the current path are open, so the copy holds at most
/// `2 * (max_depth + 1)` directory descriptors plus one file pair, whatever the tree's width
/// or entry count (M6).
struct Level {
    sdir: OwnedFd,
    ddir: OwnedFd,
    names: std::vec::IntoIter<CString>,
    depth: usize,
    rel: String,
}

/// Copy `run-<job_id>` from `source_root` into `work` created beneath `job_dir`; `program` is
/// the request's canonical relative path, which must be a copied regular file. The copy is
/// depth-first and iterative (an explicit stack of [`Level`]s, no recursion). Ownership is
/// given to the job identity afterwards by [`chown_tree`].
pub fn copy(
    source_root: &Path,
    job_id: &str,
    job_dir: BorrowedFd<'_>,
    program: &str,
    rules: &Rules,
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
    .map_err(|e| src_errno("source root", e))?;
    let run = CString::new(format!("run-{job_id}")).map_err(src)?;
    let (top, is_dir) = open_checked(root.as_fd(), &run, rules.owner_uid)?;
    drop(root);
    if !is_dir {
        return Err(src("run directory is not a directory"));
    }
    mkdirat(job_dir, "work", Mode::from_raw_mode(0o700)).map_err(|e| dst_errno("work", e))?;
    let work = open_beneath(job_dir, c"work", true).map_err(|e| dst_errno("work", e))?;
    let mut stats = Copied::default();
    let mut program_seen = false;
    let first = names(&top)?;
    let mut stack = vec![Level {
        sdir: top,
        ddir: work,
        names: first.into_iter(),
        depth: 0,
        rel: String::new(),
    }];
    while let Some(level) = stack.last_mut() {
        let Some(name) = level.names.next() else {
            stack.pop();
            continue;
        };
        let text = name
            .to_str()
            .map_err(|_| src("entry name is not UTF-8"))?
            .to_string();
        let path = if level.rel.is_empty() {
            text
        } else {
            format!("{}/{text}", level.rel)
        };
        stats.entries += 1;
        if stats.entries > rules.max_entries {
            return Err(src("too many entries"));
        }
        let (fd, dir) = open_checked(level.sdir.as_fd(), &name, rules.owner_uid)?;
        if dir {
            if level.depth + 1 > rules.max_depth {
                return Err(src(format!("{path}: too deep")));
            }
            mkdirat(&level.ddir, name.as_c_str(), Mode::from_raw_mode(0o700))
                .map_err(|e| dst_errno(&path, e))?;
            let d =
                open_beneath(level.ddir.as_fd(), &name, true).map_err(|e| dst_errno(&path, e))?;
            let below = names(&fd)?;
            let depth = level.depth + 1;
            stack.push(Level {
                sdir: fd,
                ddir: d,
                names: below.into_iter(),
                depth,
                rel: path,
            });
        } else {
            let is_program = path == program;
            program_seen |= is_program;
            let mode = if is_program { 0o700 } else { 0o600 };
            let out = openat2(
                &level.ddir,
                name.as_c_str(),
                OFlags::WRONLY | OFlags::CREATE | OFlags::EXCL | OFlags::NOFOLLOW | OFlags::CLOEXEC,
                Mode::from_raw_mode(mode),
                RESOLVE,
            )
            .map_err(|e| dst_errno(&path, e))?;
            let remaining = rules.max_bytes.saturating_sub(stats.bytes);
            let n = copy_bounded(fd, &out, remaining).map_err(|e| match e {
                Bounded::Limit => src("input_max_bytes exceeded"),
                Bounded::Read(e) => io_err(&path, "read", e, false),
                Bounded::Write(e) => io_err(&path, "write", e, true),
            })?;
            stats.bytes += n;
            rustix::fs::fchmod(&out, Mode::from_raw_mode(mode)).map_err(|e| dst_errno(&path, e))?;
        }
    }
    if !program_seen {
        return Err(src("program is not a regular file in the source"));
    }
    Ok(stats)
}

/// One directory whose entries are being given to the job identity.
struct Owned {
    dir: OwnedFd,
    names: std::vec::IntoIter<CString>,
}

/// Give `<job_dir>/work` and everything beneath it to `uid`/`gid`, **children before their
/// parent** (once a directory belongs to the job identity, root without `CAP_DAC_OVERRIDE`
/// can no longer traverse it), iteratively and holding only the directories on the current
/// path open (M6). The tree was created by the launcher itself (root-owned 0700 directories in
/// a root-owned 0711 job directory), and no `writ-job` process exists (residue checks), so
/// nothing can change it meanwhile; every open is still beneath the parent descriptor without
/// following links or crossing mounts. At most `max_entries` entries besides `work`. Returns
/// the number of entries given away (including `work`).
pub fn chown_tree(
    job_dir: BorrowedFd<'_>,
    uid: u32,
    gid: u32,
    max_entries: usize,
) -> std::io::Result<usize> {
    let (u, g) = (
        Some(rustix::fs::Uid::from_raw(uid)),
        Some(rustix::fs::Gid::from_raw(gid)),
    );
    let work = open_beneath(job_dir, c"work", true)?;
    let first = dir_names(&work)?;
    let mut stack = vec![Owned {
        dir: work,
        names: first.into_iter(),
    }];
    let mut count = 0usize;
    while let Some(level) = stack.last_mut() {
        let Some(name) = level.names.next() else {
            if let Some(done) = stack.pop() {
                rustix::fs::fchown(&done.dir, u, g)?;
                count += 1;
            }
            continue;
        };
        if count > max_entries {
            return Err(std::io::Error::other("too many entries to give away"));
        }
        let sx = statx(
            level.dir.as_fd(),
            name.as_c_str(),
            AtFlags::SYMLINK_NOFOLLOW,
            StatxFlags::TYPE,
        )?;
        if FileType::from_raw_mode(u32::from(sx.stx_mode)) == FileType::Directory {
            let sub = open_beneath(level.dir.as_fd(), &name, true)?;
            let below = dir_names(&sub)?;
            stack.push(Owned {
                dir: sub,
                names: below.into_iter(),
            });
        } else {
            rustix::fs::chownat(
                level.dir.as_fd(),
                name.as_c_str(),
                u,
                g,
                AtFlags::SYMLINK_NOFOLLOW,
            )?;
            count += 1;
        }
    }
    Ok(count)
}

fn dir_names(fd: &OwnedFd) -> std::io::Result<Vec<CString>> {
    let mut out = Vec::new();
    let mut d = Dir::read_from(fd)?;
    while let Some(e) = d.read() {
        let n = e?.file_name().to_owned();
        if n.as_bytes() != b"." && n.as_bytes() != b".." {
            out.push(n);
        }
    }
    Ok(out)
}

/// Descriptor exhaustion (`EMFILE`, `ENFILE`): a request-level refusal (M6), never a
/// readiness withdrawal. The launcher's own descriptor bound against the configured depth is
/// checked at start (a launcher-level fault refuses to serve instead).
pub fn exhausted(e: Errno) -> bool {
    e == Errno::MFILE || e == Errno::NFILE
}

fn src_errno(what: &str, e: Errno) -> CopyError {
    if exhausted(e) {
        CopyError::Descriptors(format!("{what}: {e}"))
    } else {
        src(format!("{what}: {e}"))
    }
}

fn dst_errno(what: &str, e: Errno) -> CopyError {
    if exhausted(e) {
        CopyError::Descriptors(format!("{what}: {e}"))
    } else {
        dst(format!("{what}: {e}"))
    }
}

fn io_err(what: &str, op: &str, e: std::io::Error, destination: bool) -> CopyError {
    match e.raw_os_error().map(Errno::from_raw_os_error) {
        Some(n) if exhausted(n) => CopyError::Descriptors(format!("{what}: {op} {e}")),
        _ if destination => dst(format!("{what}: {op} {e}")),
        _ => src(format!("{what}: {op} {e}")),
    }
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
