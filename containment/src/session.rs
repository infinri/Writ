//! The launcher's event loop (J-11.5, M3, N4): one `poll` over the connection, the two output
//! pipes, the status pipe and a pidfd of the direct child, every descriptor non-blocking.
//! Deadlines come from the launcher's monotonic clock and configuration, never from `writ`'s
//! behaviour. Output is forwarded as data frames through a **bounded outbound buffer**; if
//! `writ` does not read and the buffer fills, or total output exceeds `output_max_bytes`, the
//! loop asks for a kill with reason `output_exceeded`. Output bytes are never interpreted.
//!
//! The loop never kills: it returns why the job must end and the caller performs the
//! termination (`cgroup.kill`, `populated 0`) in the fixed order of J-11.4. Before `started`
//! the status pipe is parsed for bubblewrap's `child-pid`; after `started` it is only drained
//! (bounded, then closed) and nothing read from it is used (N4). Output that arrives before
//! `started` is held and forwarded after it.

use crate::frame::{self, Decoder, Frame, Stream};
use crate::identity::StatusReader;
use crate::protocol;
use rustix::event::{poll, PollFd, PollFlags, Timespec};
use std::fs::File;
use std::io::{self, ErrorKind, Read, Write};
use std::os::fd::{AsFd, OwnedFd};
use std::os::unix::net::UnixStream;
use std::time::Instant;
use writ_kernel::json::{self, Value};

/// Largest read from one descriptor per wakeup.
pub const CHUNK: usize = 16 * 1024;

/// The session's descriptors.
pub struct Channels {
    /// The connection with `writ` (non-blocking).
    pub conn: UnixStream,
    /// The job's stdout read end.
    pub stdout: Option<File>,
    /// The job's stderr read end.
    pub stderr: Option<File>,
    /// bubblewrap's status pipe read end.
    pub status: Option<File>,
    /// A pidfd of the direct child (readable when it exits).
    pub child: OwnedFd,
}

/// Bounds of one session.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct Bounds {
    /// `output_max_bytes` of the request.
    pub output_max: u64,
    /// Bounded outbound buffer.
    pub outbound_max: usize,
    /// Bytes of status read before `child-pid`.
    pub status_max: usize,
    /// Bytes of status drained after `started` before its read end is closed.
    pub status_drain: usize,
}

/// End of the start phase.
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum Start {
    /// bubblewrap reported its sandbox child.
    ChildPid(i32),
    /// Status timeout, malformed status, or the child exited first.
    Failed(String),
    /// `writ`'s connection reached EOF or failed, or sent something other than `cancel`.
    ConnLost,
    /// `writ` cancelled.
    Cancelled,
}

/// End of the running phase.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Run {
    /// The direct child exited (descendants may remain: the caller kills and verifies).
    ChildExited,
    /// The job must be killed for this reason.
    Kill(protocol::Reason),
    /// `writ`'s connection was lost: kill (no result can be delivered).
    ConnLost,
}

/// One session.
pub struct Session {
    ch: Channels,
    b: Bounds,
    job_id: String,
    out: Vec<u8>,
    held: Vec<u8>,
    started: bool,
    output: [u64; 2],
    over: bool,
    decoder: Decoder,
    status_reader: StatusReader,
    drained: usize,
    conn_open: bool,
}

fn set_nonblocking(f: &impl AsFd) -> io::Result<()> {
    let fl = rustix::fs::fcntl_getfl(f)?;
    rustix::fs::fcntl_setfl(f, fl | rustix::fs::OFlags::NONBLOCK)?;
    Ok(())
}

fn timeout(deadline: Instant) -> Timespec {
    let left = deadline.saturating_duration_since(Instant::now());
    Timespec {
        tv_sec: i64::try_from(left.as_secs()).unwrap_or(i64::MAX),
        tv_nsec: i64::from(left.subsec_nanos()),
    }
}

enum Got {
    Bytes(usize),
    Eof,
    WouldBlock,
}

fn read_some(f: &mut impl Read, buf: &mut [u8]) -> Got {
    loop {
        match f.read(buf) {
            Ok(0) => return Got::Eof,
            Ok(n) => return Got::Bytes(n),
            Err(e) if e.kind() == ErrorKind::Interrupted => continue,
            Err(e) if e.kind() == ErrorKind::WouldBlock => return Got::WouldBlock,
            Err(_) => return Got::Eof,
        }
    }
}

/// Receive from `writ`; ancillary data (`SCM_RIGHTS`, `SCM_CREDENTIALS`) closes the
/// connection (J-11.2).
fn recv_conn(conn: &UnixStream, buf: &mut [u8]) -> Got {
    loop {
        match writ_hostsys::sock::recv_plain(conn, buf) {
            Ok(0) => return Got::Eof,
            Ok(n) => return Got::Bytes(n),
            Err(e) if e.kind() == ErrorKind::Interrupted => continue,
            Err(e) if e.kind() == ErrorKind::WouldBlock => return Got::WouldBlock,
            Err(_) => return Got::Eof,
        }
    }
}

impl Session {
    /// A session over `ch` (every descriptor is made non-blocking).
    pub fn new(ch: Channels, b: Bounds, job_id: &str) -> io::Result<Session> {
        ch.conn.set_nonblocking(true)?;
        for f in [&ch.stdout, &ch.stderr, &ch.status].into_iter().flatten() {
            set_nonblocking(f)?;
        }
        Ok(Session {
            ch,
            b,
            job_id: job_id.to_string(),
            out: Vec::new(),
            held: Vec::new(),
            started: false,
            output: [0, 0],
            over: false,
            decoder: Decoder::new(false),
            status_reader: StatusReader::new(b.status_max),
            drained: 0,
            conn_open: true,
        })
    }

    /// Bytes of stdout and stderr read.
    pub fn output_bytes(&self) -> [u64; 2] {
        self.output
    }

    /// Bytes waiting for `writ`.
    pub fn outbound(&self) -> usize {
        self.out.len()
    }

    /// Queue a control frame; `false` when it does not fit the outbound bound.
    pub fn send_control(&mut self, v: &Value) -> bool {
        let Ok(text) = json::canonical(v) else {
            return false;
        };
        let Some(f) = frame::encode(frame::CONTROL, text.as_bytes()) else {
            return false;
        };
        // Control frames may exceed the bound by one frame so `finished` is never dropped.
        self.out.extend_from_slice(&f);
        true
    }

    /// Mark `started` queued: held output follows it.
    pub fn mark_started(&mut self) {
        self.started = true;
        let held = std::mem::take(&mut self.held);
        self.out.extend_from_slice(&held);
    }

    fn take_output(&mut self, stream: Stream, bytes: &[u8]) -> Option<protocol::Reason> {
        let i = usize::from(stream == Stream::Stderr);
        self.output[i] += bytes.len() as u64;
        if self.output[0] + self.output[1] > self.b.output_max {
            self.over = true;
            return Some(protocol::Reason::OutputExceeded);
        }
        let Some(payload) = frame::output_payload(stream, &self.job_id, bytes) else {
            return Some(protocol::Reason::OutputExceeded);
        };
        let Some(f) = frame::encode(frame::DATA, &payload) else {
            return Some(protocol::Reason::OutputExceeded);
        };
        let target = if self.started {
            &mut self.out
        } else {
            &mut self.held
        };
        target.extend_from_slice(&f);
        if self.out.len() + self.held.len() > self.b.outbound_max {
            self.over = true;
            return Some(protocol::Reason::OutputExceeded);
        }
        None
    }

    fn pump_output(&mut self, which: Stream) -> Option<protocol::Reason> {
        let mut buf = [0u8; CHUNK];
        let slot = match which {
            Stream::Stdout => &mut self.ch.stdout,
            Stream::Stderr => &mut self.ch.stderr,
        };
        let f = slot.as_mut()?;
        match read_some(f, &mut buf) {
            Got::Bytes(n) => self.take_output(which, &buf[..n]),
            Got::Eof => {
                *slot = None;
                None
            }
            Got::WouldBlock => None,
        }
    }

    /// Write queued bytes; `false` when the connection failed.
    fn flush_some(&mut self) -> bool {
        while !self.out.is_empty() {
            match self.ch.conn.write(&self.out) {
                Ok(0) => return false,
                Ok(n) => {
                    self.out.drain(..n);
                }
                Err(e) if e.kind() == ErrorKind::Interrupted => continue,
                Err(e) if e.kind() == ErrorKind::WouldBlock => return true,
                Err(_) => return false,
            }
        }
        true
    }

    /// Read from `writ`: `None` while nothing decisive arrived; `Some(Ok(()))` for a cancel;
    /// `Some(Err(()))` for EOF, an error or any other frame.
    fn read_conn(&mut self) -> Option<Result<(), ()>> {
        let mut buf = [0u8; 4096];
        match recv_conn(&self.ch.conn, &mut buf) {
            Got::WouldBlock => None,
            Got::Eof => {
                self.conn_open = false;
                Some(Err(()))
            }
            Got::Bytes(n) => match self.decoder.feed(&buf[..n]) {
                Err(_) => Some(Err(())),
                // The first decoded frame decides: a cancel for this job, else close.
                Ok(frames) => frames.into_iter().next().map(|f| match f {
                    Frame::Control(p) if protocol::is_cancel(&p, &self.job_id) => Ok(()),
                    _ => Err(()),
                }),
            },
        }
    }

    fn drain_status(&mut self) {
        let mut buf = [0u8; 4096];
        let Some(f) = self.ch.status.as_mut() else {
            return;
        };
        match read_some(f, &mut buf) {
            Got::Bytes(n) => {
                // Discarded (N4).
                self.drained += n;
                if self.drained >= self.b.status_drain {
                    self.ch.status = None;
                }
            }
            Got::Eof => self.ch.status = None,
            Got::WouldBlock => {}
        }
    }

    fn poll_once(&mut self, deadline: Instant, want_status: bool) -> io::Result<[bool; 5]> {
        let conn_flags = if self.out.is_empty() {
            PollFlags::IN
        } else {
            PollFlags::IN | PollFlags::OUT
        };
        let mut fds: Vec<PollFd<'_>> = Vec::with_capacity(5);
        let mut map = [usize::MAX; 5];
        fds.push(PollFd::new(&self.ch.conn, conn_flags));
        map[0] = 0;
        fds.push(PollFd::new(&self.ch.child, PollFlags::IN));
        map[1] = 1;
        if let Some(f) = &self.ch.stdout {
            map[2] = fds.len();
            fds.push(PollFd::new(f, PollFlags::IN));
        }
        if let Some(f) = &self.ch.stderr {
            map[3] = fds.len();
            fds.push(PollFd::new(f, PollFlags::IN));
        }
        if want_status {
            if let Some(f) = &self.ch.status {
                map[4] = fds.len();
                fds.push(PollFd::new(f, PollFlags::IN));
            }
        }
        let t = timeout(deadline);
        match poll(&mut fds, Some(&t)) {
            Ok(_) => {}
            Err(rustix::io::Errno::INTR) => return Ok([false; 5]),
            Err(e) => return Err(e.into()),
        }
        let mut ready = [false; 5];
        for (slot, idx) in map.iter().enumerate() {
            if *idx != usize::MAX {
                ready[slot] = !fds[*idx].revents().is_empty();
            }
        }
        Ok(ready)
    }

    /// Start phase: wait (until `deadline`) for bubblewrap's `child-pid` on the status pipe.
    pub fn await_start(&mut self, deadline: Instant) -> Start {
        loop {
            if Instant::now() >= deadline {
                return Start::Failed("no child-pid before the start deadline".into());
            }
            let ready = match self.poll_once(deadline, true) {
                Ok(r) => r,
                Err(e) => return Start::Failed(format!("poll: {e}")),
            };
            if ready[0] {
                if !self.out.is_empty() && !self.flush_some() {
                    return Start::ConnLost;
                }
                match self.read_conn() {
                    Some(Ok(())) => return Start::Cancelled,
                    Some(Err(())) => return Start::ConnLost,
                    None => {}
                }
            }
            if ready[4] {
                let mut buf = [0u8; 1024];
                let got = match self.ch.status.as_mut() {
                    Some(f) => read_some(f, &mut buf),
                    None => Got::Eof,
                };
                match got {
                    Got::Bytes(n) => match self.status_reader.feed(&buf[..n]) {
                        Ok(Some(pid)) => return Start::ChildPid(pid),
                        Ok(None) => {}
                        Err(e) => return Start::Failed(e),
                    },
                    Got::Eof => return Start::Failed("status pipe closed before child-pid".into()),
                    Got::WouldBlock => {}
                }
            }
            for (i, s) in [(2, Stream::Stdout), (3, Stream::Stderr)] {
                if ready[i] && self.pump_output(s).is_some() {
                    return Start::Failed("output bound reached before start".into());
                }
            }
            if ready[1] {
                return Start::Failed("the helper or bubblewrap exited before start".into());
            }
        }
    }

    /// Running phase until the child exits or the job must be killed; `deadline` is the wall
    /// time.
    pub fn run(&mut self, deadline: Instant) -> Run {
        loop {
            if Instant::now() >= deadline {
                return Run::Kill(protocol::Reason::TimedOut);
            }
            let ready = match self.poll_once(deadline, true) {
                Ok(r) => r,
                Err(_) => return Run::Kill(protocol::Reason::StartFailed),
            };
            if ready[0] {
                if !self.out.is_empty() && !self.flush_some() {
                    return Run::ConnLost;
                }
                match self.read_conn() {
                    Some(Ok(())) => return Run::Kill(protocol::Reason::Cancelled),
                    Some(Err(())) => return Run::ConnLost,
                    None => {}
                }
            }
            for (i, s) in [(2, Stream::Stdout), (3, Stream::Stderr)] {
                if ready[i] {
                    if let Some(r) = self.pump_output(s) {
                        return Run::Kill(r);
                    }
                }
            }
            if ready[4] {
                self.drain_status();
            }
            if ready[1] {
                return Run::ChildExited;
            }
        }
    }

    /// After the kill was verified: read what remains in the output pipes (every writer is
    /// gone, so they reach EOF), bounded by the output limit.
    pub fn drain_output(&mut self) {
        for s in [Stream::Stdout, Stream::Stderr] {
            for _ in 0..1024 {
                let open = match s {
                    Stream::Stdout => self.ch.stdout.is_some(),
                    Stream::Stderr => self.ch.stderr.is_some(),
                };
                if !open || self.over {
                    break;
                }
                let before = self.output;
                if self.pump_output(s).is_some() {
                    break;
                }
                if before == self.output
                    && (match s {
                        Stream::Stdout => self.ch.stdout.is_some(),
                        Stream::Stderr => self.ch.stderr.is_some(),
                    })
                {
                    break;
                }
            }
        }
    }

    /// Whether the output limit or the outbound bound was exceeded.
    pub fn output_exceeded(&self) -> bool {
        self.over
    }

    /// Flush queued bytes until `deadline` (best effort); `true` when everything was written.
    pub fn flush(&mut self, deadline: Instant) -> bool {
        while !self.out.is_empty() {
            if Instant::now() >= deadline || !self.conn_open {
                return false;
            }
            let mut fds = [PollFd::new(&self.ch.conn, PollFlags::OUT)];
            let t = timeout(deadline);
            if poll(&mut fds, Some(&t)).is_err() {
                return false;
            }
            if !self.flush_some() {
                return false;
            }
        }
        true
    }

    /// Whether `writ`'s connection is still open (EOF not seen).
    pub fn conn_open(&self) -> bool {
        self.conn_open
    }
}

#[cfg(test)]
#[allow(clippy::unwrap_used, clippy::expect_used, clippy::panic)]
mod tests;
