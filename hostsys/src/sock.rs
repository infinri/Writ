//! Socket and process-identity primitives for the service (M5a-04; J-1, J-2): peer
//! credentials of a connected Unix socket, a receive path that refuses ancillary data, and
//! the identity of the running process for the `writ-serviced` startup check.
//!
//! No listener lives here; the service owns its sockets. `SO_PEERCRED` is read with
//! `rustix` `net` (the only crate that calls it directly, J-0).

use std::io::{self, IoSliceMut, Read};
use std::mem::MaybeUninit;
use std::os::fd::AsFd;

use rustix::net::{recvmsg, RecvAncillaryBuffer, RecvFlags, ReturnFlags};

/// Credentials of the peer of a connected Unix socket at `connect` time.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct Peer {
    /// Effective uid.
    pub uid: u32,
    /// Effective gid.
    pub gid: u32,
    /// Pid (audit only; never authorization).
    pub pid: i32,
}

/// `SO_PEERCRED` of `socket`.
pub fn peer_credentials(socket: impl AsFd) -> io::Result<Peer> {
    let cred = rustix::net::sockopt::socket_peercred(socket).map_err(io::Error::from)?;
    Ok(Peer {
        uid: cred.uid.as_raw(),
        gid: cred.gid.as_raw(),
        pid: cred.pid.as_raw_nonzero().get(),
    })
}

/// Bytes of control buffer offered to `recvmsg`: room for one small ancillary message, so
/// that any `SCM_RIGHTS` or `SCM_CREDENTIALS` is seen (and its descriptors closed at once).
const CONTROL_BYTES: usize = 64;

/// Receive up to `buf.len()` bytes from the stream socket `socket`. A message carrying
/// ancillary data (descriptors or credentials), or whose ancillary data was truncated, is
/// refused with `InvalidData` (J-2); received descriptors are closed before returning
/// (`MSG_CMSG_CLOEXEC` in the meantime).
pub fn recv_plain(socket: impl AsFd, buf: &mut [u8]) -> io::Result<usize> {
    let mut space = [MaybeUninit::<u8>::uninit(); CONTROL_BYTES];
    let mut control = RecvAncillaryBuffer::new(&mut space);
    let msg = loop {
        let mut iov = [IoSliceMut::new(buf)];
        match recvmsg(&socket, &mut iov, &mut control, RecvFlags::CMSG_CLOEXEC) {
            Ok(m) => break m,
            Err(rustix::io::Errno::INTR) => continue,
            Err(e) => return Err(io::Error::from(e)),
        }
    };
    // Draining drops (closes) any received descriptor.
    let ancillary = control.drain().count();
    if ancillary > 0 || msg.flags.contains(ReturnFlags::CTRUNC) {
        return Err(io::Error::from(io::ErrorKind::InvalidData));
    }
    Ok(msg.bytes)
}

/// A [`Read`] over a connected stream socket that refuses ancillary data ([`recv_plain`]).
pub struct PlainReader<'a, F: AsFd>(pub &'a F);

impl<F: AsFd> Read for PlainReader<'_, F> {
    fn read(&mut self, buf: &mut [u8]) -> io::Result<usize> {
        recv_plain(self.0, buf)
    }
}

/// The identity of this process.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct ProcessIdentity {
    /// Real uid.
    pub ruid: u32,
    /// Effective uid.
    pub euid: u32,
    /// Real gid.
    pub rgid: u32,
    /// Effective gid.
    pub egid: u32,
    /// Supplementary groups as `getgroups` reports them.
    pub groups: Vec<u32>,
}

/// The identity of the running process.
pub fn process_identity() -> io::Result<ProcessIdentity> {
    use rustix::process::{getegid, geteuid, getgid, getgroups, getuid};
    Ok(ProcessIdentity {
        ruid: getuid().as_raw(),
        euid: geteuid().as_raw(),
        rgid: getgid().as_raw(),
        egid: getegid().as_raw(),
        groups: getgroups()
            .map_err(io::Error::from)?
            .into_iter()
            .map(|g| g.as_raw())
            .collect(),
    })
}

/// Why the startup identity check refused (J-1 "Startup uid check").
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum IdentityRefused {
    /// Real or effective uid differs from the configured service uid.
    Uid,
    /// Real or effective gid differs from the configured service gid.
    Gid,
    /// The configured or running uid or gid is 0.
    Root,
    /// The process has a supplementary group other than its own gid.
    SupplementaryGroups,
}

/// The J-1 startup check: real and effective uid and gid equal the configured service ids,
/// neither is 0, and there is no supplementary group (a `getgroups` list that only repeats
/// the process's own gid counts as none).
pub fn check_service_identity(
    id: &ProcessIdentity,
    service_uid: u32,
    service_gid: u32,
) -> Result<(), IdentityRefused> {
    if service_uid == 0 || service_gid == 0 || id.ruid == 0 || id.euid == 0 {
        return Err(IdentityRefused::Root);
    }
    if id.ruid != service_uid || id.euid != service_uid {
        return Err(IdentityRefused::Uid);
    }
    if id.rgid != service_gid || id.egid != service_gid {
        return Err(IdentityRefused::Gid);
    }
    if id.groups.iter().any(|g| *g != service_gid) {
        return Err(IdentityRefused::SupplementaryGroups);
    }
    Ok(())
}

#[cfg(test)]
#[allow(clippy::unwrap_used, clippy::expect_used, clippy::panic)]
mod tests {
    use super::*;
    use rustix::net::{sendmsg, SendAncillaryBuffer, SendAncillaryMessage, SendFlags};
    use std::io::{IoSlice, Write};
    use std::os::unix::net::UnixStream;

    #[test]
    fn peer_credentials_report_this_process() {
        let (a, _b) = UnixStream::pair().unwrap();
        let p = peer_credentials(&a).unwrap();
        assert_eq!(p.uid, rustix::process::getuid().as_raw());
        assert_eq!(p.gid, rustix::process::getgid().as_raw());
        assert_eq!(p.pid, i32::try_from(std::process::id()).unwrap());
    }

    #[test]
    fn plain_bytes_are_received() {
        let (a, mut b) = UnixStream::pair().unwrap();
        b.write_all(b"hello").unwrap();
        let mut buf = [0u8; 16];
        let n = PlainReader(&a).read(&mut buf).unwrap();
        assert_eq!(&buf[..n], b"hello");
        drop(b);
        assert_eq!(PlainReader(&a).read(&mut buf).unwrap(), 0);
    }

    #[test]
    fn a_message_carrying_a_descriptor_is_refused_and_the_descriptor_closed() {
        let (a, b) = UnixStream::pair().unwrap();
        let (passed, _keep) = UnixStream::pair().unwrap();
        let mut space = [MaybeUninit::<u8>::uninit(); 64];
        let mut control = SendAncillaryBuffer::new(&mut space);
        let fds = [passed.as_fd()];
        assert!(control.push(SendAncillaryMessage::ScmRights(&fds)));
        sendmsg(&b, &[IoSlice::new(b"x")], &mut control, SendFlags::empty()).unwrap();
        let mut buf = [0u8; 16];
        let err = PlainReader(&a).read(&mut buf).unwrap_err();
        assert_eq!(err.kind(), io::ErrorKind::InvalidData);
    }

    #[test]
    fn the_startup_identity_check_requires_the_exact_unprivileged_service_ids() {
        let ok = ProcessIdentity {
            ruid: 990,
            euid: 990,
            rgid: 990,
            egid: 990,
            groups: vec![],
        };
        assert_eq!(check_service_identity(&ok, 990, 990), Ok(()));
        let own = ProcessIdentity {
            groups: vec![990],
            ..ok.clone()
        };
        assert_eq!(check_service_identity(&own, 990, 990), Ok(()));
        let cases = [
            (
                ProcessIdentity {
                    euid: 991,
                    ..ok.clone()
                },
                IdentityRefused::Uid,
            ),
            (
                ProcessIdentity {
                    ruid: 1000,
                    ..ok.clone()
                },
                IdentityRefused::Uid,
            ),
            (
                ProcessIdentity {
                    egid: 991,
                    ..ok.clone()
                },
                IdentityRefused::Gid,
            ),
            (
                ProcessIdentity {
                    groups: vec![990, 27],
                    ..ok.clone()
                },
                IdentityRefused::SupplementaryGroups,
            ),
            (
                ProcessIdentity {
                    euid: 0,
                    ..ok.clone()
                },
                IdentityRefused::Root,
            ),
        ];
        for (id, want) in cases {
            assert_eq!(check_service_identity(&id, 990, 990), Err(want), "{id:?}");
        }
        assert_eq!(
            check_service_identity(&ok, 0, 990),
            Err(IdentityRefused::Root)
        );
        assert_eq!(
            check_service_identity(&ok, 990, 0),
            Err(IdentityRefused::Root)
        );
    }

    #[test]
    fn this_workstation_process_reads_its_identity() {
        let id = process_identity().unwrap();
        assert_eq!(id.ruid, rustix::process::getuid().as_raw());
    }
}
