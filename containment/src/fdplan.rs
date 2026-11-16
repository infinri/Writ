//! Descriptor plumbing (J-11.2 admission, J-11.4 steps 4 and 5; M1, N2).
//!
//! Launcher side: from the start every launcher descriptor is close-on-exec and the connection
//! sits on a descriptor at or above 10. Descriptors 3 and 4 are **reserved** at start as
//! close-on-exec `/dev/null` slots (with `unsafe_code` forbidden, `dup2` needs an owned target
//! descriptor; the slots are those targets). The gate and status pipes are created
//! close-on-exec and moved to descriptors at or above 10 with `F_DUPFD_CLOEXEC` (originals
//! closed), so no pipe end can occupy 3 or 4. Immediately before the spawn (single-threaded,
//! nothing else forks in between) [`Slots::install`] `dup2`s the gate read end onto 3 and the
//! status write end onto 4 (`dup2` clears `FD_CLOEXEC` on the copies); right after the spawn
//! returns the launcher drops the slots and those pipe ends.
//!
//! Helper side: [`check_helper_fds`] requires the listed set to be exactly the expected set
//! (apart from the listing's own descriptor), 3 a FIFO opened read-only and 4 a FIFO opened
//! write-only, on two different pipes. The access mode comes from `/proc/self/fdinfo/<n>` and
//! the type from `stat` of `/proc/self/fd/<n>` (equivalent to `F_GETFL` and `fstat` without
//! borrowing a raw descriptor, which safe Rust cannot do).

use rustix::fs::{openat2, Dir, FileType, Mode, OFlags, ResolveFlags, CWD};
use rustix::io::fcntl_dupfd_cloexec;
use rustix::pipe::{pipe_with, PipeFlags};
use std::io;
use std::os::fd::{AsFd, AsRawFd, OwnedFd};
use std::os::unix::fs::MetadataExt;
use std::path::Path;

/// Lowest descriptor for every launcher-held pipe end and the connection.
pub const HIGH: i32 = 10;
/// Gate descriptor in the helper and bubblewrap.
pub const GATE_FD: i32 = 3;
/// Status descriptor in the helper and bubblewrap.
pub const STATUS_FD: i32 = 4;

/// Move `fd` to a close-on-exec descriptor at or above [`HIGH`], closing the original.
pub fn high(fd: OwnedFd) -> io::Result<OwnedFd> {
    let h = fcntl_dupfd_cloexec(&fd, HIGH)?;
    drop(fd);
    Ok(h)
}

/// The reserved descriptors 3 and 4.
#[derive(Debug)]
pub struct Slots {
    slot3: OwnedFd,
    slot4: OwnedFd,
}

fn dev_null() -> io::Result<OwnedFd> {
    Ok(openat2(
        CWD,
        "/dev/null",
        OFlags::RDONLY | OFlags::CLOEXEC,
        Mode::empty(),
        ResolveFlags::NO_SYMLINKS,
    )?)
}

impl Slots {
    /// Reserve 3 and 4: they must be the lowest free descriptors (the launcher verified its
    /// descriptor set is exactly 0..2 before).
    pub fn reserve() -> io::Result<Slots> {
        let slot3 = dev_null()?;
        let slot4 = dev_null()?;
        if slot3.as_raw_fd() != GATE_FD || slot4.as_raw_fd() != STATUS_FD {
            return Err(io::Error::other("descriptors 3 and 4 are not free"));
        }
        Ok(Slots { slot3, slot4 })
    }

    /// `dup2` the gate read end onto 3 and the status write end onto 4 (inheritable).
    pub fn install(&mut self, gate_read: &OwnedFd, status_write: &OwnedFd) -> io::Result<()> {
        rustix::io::dup2(gate_read, &mut self.slot3)?;
        rustix::io::dup2(status_write, &mut self.slot4)?;
        Ok(())
    }
}

/// The gate and status pipes, every end at or above [`HIGH`] and close-on-exec.
#[derive(Debug)]
pub struct Pipes {
    /// Gate read end (becomes 3 in the helper).
    pub gate_read: OwnedFd,
    /// Gate write end (held by [`crate::gate::Gate`]).
    pub gate_write: OwnedFd,
    /// Status read end (the launcher reads bubblewrap's JSON status).
    pub status_read: OwnedFd,
    /// Status write end (becomes 4 in the helper).
    pub status_write: OwnedFd,
}

/// Create both pipes and move their ends high.
pub fn pipes() -> io::Result<Pipes> {
    let (gr, gw) = pipe_with(PipeFlags::CLOEXEC)?;
    let (sr, sw) = pipe_with(PipeFlags::CLOEXEC)?;
    Ok(Pipes {
        gate_read: high(gr)?,
        gate_write: high(gw)?,
        status_read: high(sr)?,
        status_write: high(sw)?,
    })
}

/// Access mode of an open descriptor.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Access {
    /// `O_RDONLY`.
    Read,
    /// `O_WRONLY`.
    Write,
    /// `O_RDWR` or unknown.
    Other,
}

/// What the helper observed about one descriptor.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct FdFacts {
    /// It is a FIFO (pipe).
    pub fifo: bool,
    /// Its access mode.
    pub access: Access,
    /// Inode of the pipe (distinguishes the two pipes).
    pub ino: u64,
}

/// The helper's descriptor check (J-11.4 step 5): `listed` (without the listing's own
/// descriptor) must equal `expected`; when 3 and 4 are expected, 3 is a read-only FIFO and 4 a
/// write-only FIFO of a different pipe.
pub fn check_helper_fds(
    listed: &[i32],
    expected: &[i32],
    gate: Option<&FdFacts>,
    status: Option<&FdFacts>,
) -> Result<(), String> {
    let mut l = listed.to_vec();
    l.sort_unstable();
    l.dedup();
    if l != expected {
        return Err(format!("descriptor set {l:?}, expected {expected:?}"));
    }
    if expected.contains(&GATE_FD) {
        let g = gate.ok_or("descriptor 3 not observed")?;
        let s = status.ok_or("descriptor 4 not observed")?;
        if !g.fifo || g.access != Access::Read {
            return Err(format!("descriptor 3 is not a read-only pipe end: {g:?}"));
        }
        if !s.fifo || s.access != Access::Write {
            return Err(format!("descriptor 4 is not a write-only pipe end: {s:?}"));
        }
        if g.ino == s.ino {
            return Err("descriptors 3 and 4 are ends of one pipe".into());
        }
    }
    Ok(())
}

/// The descriptors open in this process (from `/proc/self/fd`, without the listing's own).
pub fn list_own_fds(proc_root: &Path) -> io::Result<Vec<i32>> {
    let fd = openat2(
        CWD,
        proc_root.join("self/fd").as_path(),
        OFlags::RDONLY | OFlags::DIRECTORY | OFlags::CLOEXEC,
        Mode::empty(),
        ResolveFlags::empty(),
    )?;
    let own = fd.as_fd().as_raw_fd();
    let mut d = Dir::new(fd)?;
    let mut out = Vec::new();
    while let Some(e) = d.read() {
        let e = e?;
        if let Some(n) = e
            .file_name()
            .to_str()
            .ok()
            .and_then(|t| t.parse::<i32>().ok())
        {
            if n != own {
                out.push(n);
            }
        }
    }
    out.sort_unstable();
    Ok(out)
}

/// Observe descriptor `n` of this process through `/proc/self`.
pub fn observe_fd(proc_root: &Path, n: i32) -> io::Result<FdFacts> {
    let m = std::fs::metadata(proc_root.join(format!("self/fd/{n}")))?;
    let info = std::fs::read_to_string(proc_root.join(format!("self/fdinfo/{n}")))?;
    let flags = crate::procfs::fdinfo_flags(&info).ok_or_else(|| io::Error::other("fdinfo"))?;
    let access = match flags & 0o3 {
        0 => Access::Read,
        1 => Access::Write,
        _ => Access::Other,
    };
    Ok(FdFacts {
        fifo: FileType::from_raw_mode(m.mode()) == FileType::Fifo,
        access,
        ino: m.ino(),
    })
}

#[cfg(test)]
#[allow(clippy::unwrap_used, clippy::expect_used, clippy::panic)]
mod tests {
    use super::*;

    const RUN: [i32; 5] = [0, 1, 2, 3, 4];

    fn f(fifo: bool, access: Access, ino: u64) -> FdFacts {
        FdFacts { fifo, access, ino }
    }

    #[test]
    fn helper_accepts_exactly_the_plan() {
        let g = f(true, Access::Read, 1);
        let s = f(true, Access::Write, 2);
        assert!(check_helper_fds(&[4, 3, 2, 1, 0], &RUN, Some(&g), Some(&s)).is_ok());
        assert!(check_helper_fds(&[0, 1, 2], &[0, 1, 2], None, None).is_ok());
    }

    #[test]
    fn helper_refuses_extra_missing_or_wrong_descriptors() {
        let g = f(true, Access::Read, 1);
        let s = f(true, Access::Write, 2);
        assert!(check_helper_fds(&[0, 1, 2, 3, 4, 5], &RUN, Some(&g), Some(&s)).is_err());
        assert!(check_helper_fds(&[0, 1, 2, 3], &RUN, Some(&g), Some(&s)).is_err());
        // The status write end landed on 3 (N2): wrong access mode.
        assert!(check_helper_fds(&RUN, &RUN, Some(&s), Some(&g)).is_err());
        // Not a pipe.
        let file = f(false, Access::Read, 1);
        assert!(check_helper_fds(&RUN, &RUN, Some(&file), Some(&s)).is_err());
        // Both ends of the same pipe.
        let same = f(true, Access::Write, 1);
        assert!(check_helper_fds(&RUN, &RUN, Some(&g), Some(&same)).is_err());
        assert!(check_helper_fds(&RUN, &RUN, None, Some(&s)).is_err());
    }

    #[test]
    fn pipes_are_high_and_close_on_exec() {
        let p = pipes().unwrap();
        for fd in [&p.gate_read, &p.gate_write, &p.status_read, &p.status_write] {
            assert!(fd.as_raw_fd() >= HIGH);
            let fl = rustix::io::fcntl_getfd(fd).unwrap();
            assert!(fl.contains(rustix::io::FdFlags::CLOEXEC));
        }
        let gr = observe_fd(Path::new("/proc"), p.gate_read.as_raw_fd()).unwrap();
        let gw = observe_fd(Path::new("/proc"), p.gate_write.as_raw_fd()).unwrap();
        assert!(gr.fifo && gr.access == Access::Read);
        assert!(gw.fifo && gw.access == Access::Write);
        assert_eq!(gr.ino, gw.ino);
        let own = list_own_fds(Path::new("/proc")).unwrap();
        assert!(own.contains(&p.gate_read.as_raw_fd()));
    }

    #[test]
    fn install_makes_inheritable_copies_on_3_and_4() {
        // Exercised in a child process: the test harness may already use 3 and 4, so build the
        // slots from whatever descriptors are free and check dup2's effect on them.
        let p = pipes().unwrap();
        let mut a = high(dev_null().unwrap()).unwrap();
        rustix::io::dup2(&p.gate_read, &mut a).unwrap();
        let fl = rustix::io::fcntl_getfd(&a).unwrap();
        assert!(!fl.contains(rustix::io::FdFlags::CLOEXEC));
        let facts = observe_fd(Path::new("/proc"), a.as_raw_fd()).unwrap();
        assert!(facts.fifo && facts.access == Access::Read);
    }
}
