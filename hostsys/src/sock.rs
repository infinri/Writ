//! Unix socket and process-identity primitives (M5a-04, M5a-07; J-1, J-2, J-11.2): peer
//! credentials of a connected Unix socket read **before any byte**, a receive path that
//! refuses ancillary data (`SCM_RIGHTS`, `SCM_CREDENTIALS`), and the identity of the running
//! process for the `writ-serviced` startup check. The one module for both the service and the
//! job launcher (the former `socket` module of M5a-07 is merged here).
//!
//! No listener lives here; the service owns its sockets. `SO_PEERCRED` and `recvmsg` are
//! called with `rustix` `net` (this crate is the only one that calls them directly, J-0).

use std::io::{self, IoSliceMut, Read};
use std::mem::MaybeUninit;
use std::os::fd::{AsFd, OwnedFd};

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

/// Bytes of control buffer offered to `recvmsg`: room for eight passed descriptors and one
/// credentials message, so that any `SCM_RIGHTS` or `SCM_CREDENTIALS` is seen (and its
/// descriptors closed at once); anything larger is truncated, which is refused too.
const CONTROL_BYTES: usize = rustix::cmsg_space!(ScmRights(8), ScmCredentials(1));

/// Receive up to `buf.len()` bytes from the stream socket `socket`. A message carrying
/// ancillary data (descriptors or credentials), or whose ancillary data was truncated, is
/// refused with `InvalidData` (J-2, J-11.2; the connection must be closed); received
/// descriptors are closed before returning (`MSG_CMSG_CLOEXEC` in the meantime). `Ok(0)` at
/// EOF; `WouldBlock` on a non-blocking socket without data.
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

/// Send `bytes` with one descriptor attached (`SCM_RIGHTS`). Used by the probes and tests to
/// check that a receiver refuses ancillary data (J-2); the service never sends descriptors.
pub fn send_with_descriptor(
    socket: impl AsFd,
    bytes: &[u8],
    fd: std::os::fd::BorrowedFd<'_>,
) -> io::Result<usize> {
    use rustix::net::{sendmsg, SendAncillaryBuffer, SendAncillaryMessage, SendFlags};
    let mut space = [MaybeUninit::<u8>::uninit(); CONTROL_BYTES];
    let mut control = SendAncillaryBuffer::new(&mut space);
    let fds = [fd];
    if !control.push(SendAncillaryMessage::ScmRights(&fds)) {
        return Err(io::Error::from(io::ErrorKind::InvalidInput));
    }
    sendmsg(
        socket,
        &[io::IoSlice::new(bytes)],
        &mut control,
        SendFlags::empty(),
    )
    .map_err(io::Error::from)
}

/// Set the process umask to 0077 (J-2: the service never creates group- or
/// other-accessible files itself) and return the previous mask's bits.
pub fn restrict_umask() -> u32 {
    rustix::process::umask(rustix::fs::Mode::from_raw_mode(0o077)).as_raw_mode()
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

/// The socket-activation environment systemd sets for a service with `Sockets=` (J-8):
/// `LISTEN_PID`, `LISTEN_FDS` and `LISTEN_FDNAMES` (`sd_listen_fds(3)`).
#[derive(Clone, Debug, Default, PartialEq, Eq)]
pub struct ListenEnv {
    /// `LISTEN_PID`.
    pub pid: Option<String>,
    /// `LISTEN_FDS`.
    pub fds: Option<String>,
    /// `LISTEN_FDNAMES` (colon-separated `FileDescriptorName=` values).
    pub names: Option<String>,
}

impl ListenEnv {
    /// The variables of this process (read once; nothing else of the environment is used).
    pub fn of_process() -> ListenEnv {
        let var = |k: &str| std::env::var(k).ok();
        ListenEnv {
            pid: var("LISTEN_PID"),
            fds: var("LISTEN_FDS"),
            names: var("LISTEN_FDNAMES"),
        }
    }
}

/// Why inherited sockets were refused (the service then refuses to start, J-8).
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum ActivationRefused {
    /// No `LISTEN_PID` or `LISTEN_FDS`: not socket-activated.
    NotActivated,
    /// `LISTEN_PID` is not this process.
    OtherProcess,
    /// `LISTEN_FDS` is not a count from 1 to [`MAX_INHERITED`].
    Count,
    /// `LISTEN_FDNAMES` is absent, does not name every descriptor, repeats a name or uses a
    /// character outside `[A-Za-z0-9._-]`.
    Names,
    /// `pidfd_open` or `pidfd_getfd` failed.
    Takeover(io::ErrorKind),
}

/// The first inherited descriptor (`SD_LISTEN_FDS_START`).
pub const LISTEN_FDS_START: i32 = 3;
/// Most inherited descriptors accepted.
pub const MAX_INHERITED: usize = 16;

/// Take over the sockets systemd passed to this process (J-8; lead decision after M5a-04):
/// with `unsafe_code` forbidden, a raw descriptor number cannot become an `OwnedFd`
/// directly, so each one is duplicated through this process's own pidfd with
/// `pidfd_getfd` (Linux 5.6; always permitted on the caller's own thread group). The
/// duplicates are close-on-exec. The original descriptors stay open, without
/// close-on-exec, for the life of the process (recorded residual: closing a raw number
/// needs `unsafe`; `writ-serviced` spawns no child process).
pub fn take_inherited(env: &ListenEnv) -> Result<Vec<(String, OwnedFd)>, ActivationRefused> {
    take_inherited_from(env, LISTEN_FDS_START)
}

fn take_inherited_from(
    env: &ListenEnv,
    first: i32,
) -> Result<Vec<(String, OwnedFd)>, ActivationRefused> {
    use rustix::process::{getpid, pidfd_getfd, pidfd_open, PidfdFlags, PidfdGetfdFlags};
    let (Some(pid), Some(fds)) = (env.pid.as_deref(), env.fds.as_deref()) else {
        return Err(ActivationRefused::NotActivated);
    };
    let digits = |t: &str| !t.is_empty() && t.bytes().all(|b| b.is_ascii_digit());
    if !digits(pid) || pid.parse::<i32>().ok() != Some(getpid().as_raw_nonzero().get()) {
        return Err(ActivationRefused::OtherProcess);
    }
    let count = match fds.parse::<usize>() {
        Ok(n) if digits(fds) && (1..=MAX_INHERITED).contains(&n) => n,
        _ => return Err(ActivationRefused::Count),
    };
    let names: Vec<&str> = env.names.as_deref().unwrap_or("").split(':').collect();
    let name_ok = |n: &&str| {
        !n.is_empty()
            && n.len() <= 255
            && n.bytes()
                .all(|b| b.is_ascii_alphanumeric() || matches!(b, b'.' | b'_' | b'-'))
    };
    let distinct = names
        .iter()
        .enumerate()
        .all(|(i, n)| !names[..i].contains(n));
    if names.len() != count || !names.iter().all(name_ok) || !distinct {
        return Err(ActivationRefused::Names);
    }
    let pidfd = pidfd_open(getpid(), PidfdFlags::empty())
        .map_err(|e| ActivationRefused::Takeover(io::Error::from(e).kind()))?;
    let mut out = Vec::with_capacity(count);
    for (i, name) in names.into_iter().enumerate() {
        let target = first + i32::try_from(i).map_err(|_| ActivationRefused::Count)?;
        let fd = pidfd_getfd(&pidfd, target, PidfdGetfdFlags::empty())
            .map_err(|e| ActivationRefused::Takeover(io::Error::from(e).kind()))?;
        out.push((name.to_string(), fd));
    }
    Ok(out)
}

/// What a received listening socket must be.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum ListenKind {
    /// `AF_UNIX`, `SOCK_STREAM`, listening.
    UnixStream,
    /// `AF_INET`, `SOCK_STREAM`, listening.
    TcpV4,
}

/// Check the family, type and listening state of a received socket (J-8: the service
/// verifies every received socket before it serves; address and mode are checked by the
/// caller on the converted listener).
pub fn check_listening(fd: impl AsFd, kind: ListenKind) -> io::Result<()> {
    use rustix::net::sockopt::{socket_acceptconn, socket_domain, socket_type};
    use rustix::net::{AddressFamily, SocketType};
    let family = match kind {
        ListenKind::UnixStream => AddressFamily::UNIX,
        ListenKind::TcpV4 => AddressFamily::INET,
    };
    let fd = fd.as_fd();
    if socket_domain(fd)? != family
        || socket_type(fd)? != SocketType::STREAM
        || !socket_acceptconn(fd)?
    {
        return Err(io::Error::from(io::ErrorKind::InvalidInput));
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
    fn would_block_on_an_empty_nonblocking_socket() {
        let (a, _b) = UnixStream::pair().unwrap();
        a.set_nonblocking(true).unwrap();
        let mut buf = [0u8; 8];
        assert_eq!(
            recv_plain(&a, &mut buf).unwrap_err().kind(),
            io::ErrorKind::WouldBlock
        );
    }

    #[test]
    fn eight_descriptors_and_credentials_fit_and_are_refused() {
        let (a, b) = UnixStream::pair().unwrap();
        let held: Vec<UnixStream> = (0..8).map(|_| UnixStream::pair().unwrap().0).collect();
        let fds: Vec<_> = held.iter().map(|s| s.as_fd()).collect();
        let mut space = [MaybeUninit::<u8>::uninit(); CONTROL_BYTES];
        let mut control = SendAncillaryBuffer::new(&mut space);
        assert!(control.push(SendAncillaryMessage::ScmRights(&fds)));
        sendmsg(&b, &[IoSlice::new(b"x")], &mut control, SendFlags::empty()).unwrap();
        let mut buf = [0u8; 16];
        let err = recv_plain(&a, &mut buf).unwrap_err();
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

    fn env(pid: &str, fds: &str, names: Option<&str>) -> ListenEnv {
        ListenEnv {
            pid: Some(pid.into()),
            fds: Some(fds.into()),
            names: names.map(str::to_string),
        }
    }

    fn me() -> String {
        std::process::id().to_string()
    }

    #[test]
    fn inherited_descriptors_are_duplicated_through_the_own_pidfd() {
        use std::os::fd::AsRawFd;
        use std::os::unix::net::UnixListener;
        let dir = std::env::temp_dir().join(format!("writ-sock-takeover-{}", me()));
        let _ = std::fs::remove_dir_all(&dir);
        std::fs::create_dir(&dir).unwrap();
        // Each listener's own descriptor number stands in for 3 (the test harness owns 3).
        let a = UnixListener::bind(dir.join("a.sock")).unwrap();
        let b = UnixListener::bind(dir.join("b.sock")).unwrap();
        let mut got = Vec::new();
        for (l, name) in [(&a, "host"), (&b, "admin")] {
            let mut one = take_inherited_from(&env(&me(), "1", Some(name)), l.as_raw_fd()).unwrap();
            assert_eq!(one.len(), 1);
            assert_eq!(one[0].0, name);
            got.push((one.remove(0), l.as_raw_fd()));
        }
        for (((_, fd), original), path) in got.into_iter().zip(["a.sock", "b.sock"]) {
            assert_ne!(fd.as_raw_fd(), original);
            let flags = rustix::io::fcntl_getfd(&fd).unwrap();
            assert!(flags.contains(rustix::io::FdFlags::CLOEXEC));
            check_listening(&fd, ListenKind::UnixStream).unwrap();
            assert_eq!(
                check_listening(&fd, ListenKind::TcpV4).unwrap_err().kind(),
                io::ErrorKind::InvalidInput
            );
            let l = UnixListener::from(fd);
            assert_eq!(
                l.local_addr().unwrap().as_pathname(),
                Some(dir.join(path).as_path())
            );
        }
        // A connected (not listening) socket is refused.
        let (c, _d) = UnixStream::pair().unwrap();
        assert!(check_listening(&c, ListenKind::UnixStream).is_err());
        drop((a, b));
        std::fs::remove_dir_all(&dir).unwrap();
    }

    #[test]
    fn a_foreign_or_malformed_activation_environment_is_refused() {
        use ActivationRefused as R;
        let none = ListenEnv::default();
        assert_eq!(take_inherited(&none).unwrap_err(), R::NotActivated);
        let other = (std::process::id() + 1).to_string();
        for (e, want) in [
            (env(&other, "1", Some("host")), R::OtherProcess),
            (env("+1", "1", Some("host")), R::OtherProcess),
            (env(&me(), "0", Some("")), R::Count),
            (env(&me(), "17", Some("host")), R::Count),
            (env(&me(), " 1", Some("host")), R::Count),
            (env(&me(), "2", Some("host")), R::Names),
            (env(&me(), "1", None), R::Names),
            (env(&me(), "2", Some("host:host")), R::Names),
            (env(&me(), "1", Some("ho st")), R::Names),
            (env(&me(), "2", Some("host:")), R::Names),
        ] {
            assert_eq!(take_inherited(&e).unwrap_err(), want, "{e:?}");
        }
    }

    #[test]
    fn this_workstation_process_reads_its_identity() {
        let id = process_identity().unwrap();
        assert_eq!(id.ruid, rustix::process::getuid().as_raw());
    }
}
