//! The gate (J-11.4, H1). bubblewrap's `--block-fd 3` returns on **any** read result,
//! including EOF, so closing the write end releases the job exactly like writing to it. The
//! write end is therefore never closed, on any code path the launcher takes, before
//! `cgroup.kill` has been written and `populated 0` observed for `<unit>/job`:
//!
//! - [`Gate::release`] writes one byte and keeps the writer open;
//! - [`Gate::close_after`] is the only way to close it and requires a
//!   [`KillProof`](crate::cgroup::KillProof);
//! - dropping a gate that was not closed that way (an error path, a forgotten branch) **leaks**
//!   the descriptor instead of closing it, so it stays open until process exit (the uncertain
//!   crash case of J-11.6, after which systemd kills the instance cgroup).
//!
//! The test-only feature `test-faults` adds [`Gate::close_unverified_for_fault`] for the
//! P-JOB-IDENTITY gate-EOF negative control; it is never in an installed binary.

use crate::cgroup::KillProof;
use std::fs::File;
use std::io::Write;
use std::os::fd::{AsFd, BorrowedFd, IntoRawFd, OwnedFd};

/// The gate writer.
#[derive(Debug)]
pub struct Gate {
    writer: Option<OwnedFd>,
    released: bool,
}

impl Gate {
    /// Hold `writer` (the gate pipe's write end, close-on-exec).
    pub fn new(writer: OwnedFd) -> Gate {
        Gate {
            writer: Some(writer),
            released: false,
        }
    }

    /// The writer, for descriptor plumbing checks (it is never handed to a child).
    pub fn as_fd(&self) -> Option<BorrowedFd<'_>> {
        self.writer.as_ref().map(AsFd::as_fd)
    }

    /// Whether the job was released by a written byte.
    pub fn released(&self) -> bool {
        self.released
    }

    /// Release the job: write one byte. The writer stays open.
    pub fn release(&mut self) -> std::io::Result<()> {
        let fd = self
            .writer
            .as_ref()
            .ok_or_else(|| std::io::Error::from(std::io::ErrorKind::BrokenPipe))?;
        let mut f = File::from(fd.try_clone()?);
        f.write_all(&[1])?;
        self.released = true;
        Ok(())
    }

    /// Close the writer after a verified kill.
    pub fn close_after(mut self, _proof: &KillProof) {
        drop(self.writer.take());
    }

    /// **Test-only fault** (feature `test-faults`): close the writer without a kill, as a
    /// faulty error path would. Used only by the P-JOB-IDENTITY gate-EOF negative control.
    #[cfg(feature = "test-faults")]
    pub fn close_unverified_for_fault(mut self) {
        drop(self.writer.take());
    }
}

impl Drop for Gate {
    fn drop(&mut self) {
        if let Some(fd) = self.writer.take() {
            // Never close an unverified gate: leak the descriptor until process exit.
            let _ = fd.into_raw_fd();
        }
    }
}

#[cfg(test)]
#[allow(clippy::unwrap_used, clippy::expect_used, clippy::panic)]
mod tests {
    use super::*;
    use rustix::pipe::{pipe_with, PipeFlags};
    use std::io::Read;

    fn read_state(r: &OwnedFd) -> &'static str {
        let mut f = File::from(r.try_clone().unwrap());
        let mut b = [0u8; 1];
        match f.read(&mut b) {
            Ok(0) => "eof",
            Ok(_) => "byte",
            Err(e) if e.kind() == std::io::ErrorKind::WouldBlock => "blocked",
            Err(e) => panic!("{e}"),
        }
    }

    #[test]
    fn dropping_an_unverified_gate_does_not_release_the_job() {
        let (r, w) = pipe_with(PipeFlags::CLOEXEC | PipeFlags::NONBLOCK).unwrap();
        let gate = Gate::new(w);
        drop(gate);
        // A reader blocked on --block-fd sees neither a byte nor EOF.
        assert_eq!(read_state(&r), "blocked");
    }

    #[test]
    fn release_writes_a_byte_and_keeps_the_writer() {
        let (r, w) = pipe_with(PipeFlags::CLOEXEC | PipeFlags::NONBLOCK).unwrap();
        let mut gate = Gate::new(w);
        gate.release().unwrap();
        assert!(gate.released());
        assert_eq!(read_state(&r), "byte");
        assert_eq!(read_state(&r), "blocked");
        gate.close_after(&KillProof::for_tests());
        assert_eq!(read_state(&r), "eof");
    }

    #[test]
    fn close_after_a_verified_kill_gives_eof() {
        let (r, w) = pipe_with(PipeFlags::CLOEXEC | PipeFlags::NONBLOCK).unwrap();
        let gate = Gate::new(w);
        assert!(gate.as_fd().is_some());
        gate.close_after(&KillProof::for_tests());
        assert_eq!(read_state(&r), "eof");
    }
}
