//! Unix socket primitives for the J-2 and J-11.2 admission rules (this crate is the only one
//! that calls `SO_PEERCRED` and `recvmsg` directly; J-0): peer credentials read **before any
//! byte**, and a receive that refuses ancillary data (`SCM_RIGHTS`, `SCM_CREDENTIALS`). Any
//! ancillary message, or a truncated control buffer, is an error; descriptors passed by the
//! peer are closed on receipt.

use rustix::net::{recvmsg, RecvAncillaryBuffer, RecvFlags, ReturnFlags};
use std::io::{self, IoSliceMut};
use std::mem::MaybeUninit;
use std::os::fd::AsFd;

/// Peer credentials of a connected Unix socket.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct Peer {
    /// Peer uid.
    pub uid: u32,
    /// Peer gid.
    pub gid: u32,
    /// Peer pid (audit only, never authorization).
    pub pid: i32,
}

/// `SO_PEERCRED` of `fd`.
pub fn peer(fd: impl AsFd) -> io::Result<Peer> {
    let c = rustix::net::sockopt::socket_peercred(fd)?;
    Ok(Peer {
        uid: c.uid.as_raw(),
        gid: c.gid.as_raw(),
        pid: c.pid.as_raw_nonzero().get(),
    })
}

/// Receive bytes without accepting ancillary data: `Ok(0)` at EOF; an error of kind
/// `InvalidData` when the peer attached any ancillary message (the connection must be
/// closed); `WouldBlock` on a non-blocking socket without data.
pub fn recv_plain(fd: impl AsFd, buf: &mut [u8]) -> io::Result<usize> {
    let mut space =
        [MaybeUninit::<u8>::uninit(); rustix::cmsg_space!(ScmRights(8), ScmCredentials(1))];
    let mut control = RecvAncillaryBuffer::new(&mut space);
    let mut iov = [IoSliceMut::new(buf)];
    let m = recvmsg(fd, &mut iov, &mut control, RecvFlags::CMSG_CLOEXEC)?;
    // Dropping drained messages closes any received descriptor.
    let ancillary = control.drain().count();
    if ancillary > 0 || m.flags.contains(ReturnFlags::CTRUNC) {
        return Err(io::Error::new(
            io::ErrorKind::InvalidData,
            "ancillary data refused",
        ));
    }
    Ok(m.bytes)
}

#[cfg(test)]
#[allow(clippy::unwrap_used, clippy::expect_used, clippy::panic)]
mod tests {
    use super::*;
    use rustix::net::{sendmsg, SendAncillaryBuffer, SendAncillaryMessage, SendFlags};
    use std::io::{IoSlice, Write};
    use std::os::fd::AsFd;
    use std::os::unix::net::UnixStream;

    #[test]
    fn peer_is_this_process() {
        let (a, _b) = UnixStream::pair().unwrap();
        let p = peer(&a).unwrap();
        assert_eq!(p.uid, rustix::process::getuid().as_raw());
        assert_eq!(p.pid, i32::try_from(std::process::id()).unwrap());
    }

    #[test]
    fn plain_bytes_and_eof() {
        let (a, mut b) = UnixStream::pair().unwrap();
        b.write_all(b"abc").unwrap();
        drop(b);
        let mut buf = [0u8; 8];
        assert_eq!(recv_plain(&a, &mut buf).unwrap(), 3);
        assert_eq!(&buf[..3], b"abc");
        assert_eq!(recv_plain(&a, &mut buf).unwrap(), 0);
    }

    #[test]
    fn scm_rights_is_refused_and_the_descriptor_closed() {
        let (a, b) = UnixStream::pair().unwrap();
        let passed = std::fs::File::open("/dev/null").unwrap();
        let fds = [passed.as_fd()];
        let mut space = [MaybeUninit::<u8>::uninit(); rustix::cmsg_space!(ScmRights(1))];
        let mut control = SendAncillaryBuffer::new(&mut space);
        assert!(control.push(SendAncillaryMessage::ScmRights(&fds)));
        sendmsg(&b, &[IoSlice::new(b"x")], &mut control, SendFlags::empty()).unwrap();
        let mut buf = [0u8; 8];
        let e = recv_plain(&a, &mut buf).unwrap_err();
        assert_eq!(e.kind(), io::ErrorKind::InvalidData);
    }

    #[test]
    fn would_block_on_empty_nonblocking_socket() {
        let (a, _b) = UnixStream::pair().unwrap();
        a.set_nonblocking(true).unwrap();
        let mut buf = [0u8; 8];
        assert_eq!(
            recv_plain(&a, &mut buf).unwrap_err().kind(),
            io::ErrorKind::WouldBlock
        );
    }
}
