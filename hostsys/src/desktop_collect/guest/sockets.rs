use std::collections::{BTreeMap, BTreeSet};
use std::ffi::CStr;
use std::net::{IpAddr, Ipv4Addr, Ipv6Addr};
use std::time::{Duration, Instant};

use rustix::fd::AsFd;
use rustix::fs::{openat2, readlinkat_raw, Dir, Mode, OFlags, ResolveFlags};
use rustix::io::Errno;
use rustix::net::netlink::{SocketAddrNetlink, SOCK_DIAG};
use rustix::net::{
    bind, getsockname, recvfrom, sendto, socket_with, AddressFamily, RecvFlags, SendFlags,
    SocketFlags, SocketType,
};

use super::super::inventory::Holder;
use super::super::Problem;
use super::check_deadline;
use super::process::Scan;

const TASK_FDS: usize = 8192;
const TOTAL_FDS: usize = 65_536;
const LINK_BYTES: usize = 4096;

#[derive(Debug, Eq, PartialEq)]
pub(super) struct DescriptorView(BTreeMap<u64, BTreeSet<(u32, u32)>>);

impl DescriptorView {
    pub(super) fn holders(&self, scan: &Scan, inode: u64) -> Result<BTreeSet<Holder>, Problem> {
        let mut holders = BTreeSet::new();
        for (tid, _) in self.0.get(&inode).ok_or(Problem::Incomplete)? {
            let task = scan.tasks.get(tid).ok_or(Problem::Mismatch)?;
            let leader = scan.processes.get(&task.process).ok_or(Problem::Mismatch)?;
            let uid = task.credentials.uids[0];
            if task.credentials.uids != [uid; 4] || leader.uids != [uid; 4] {
                return Err(Problem::Ambiguous);
            }
            holders.insert(Holder {
                pid: task.process,
                start_ticks: leader.start,
                uid,
            });
        }
        if holders
            .first()
            .is_none_or(|first| holders.iter().any(|holder| holder.uid != first.uid))
        {
            return Err(Problem::Ambiguous);
        }
        Ok(holders)
    }
}

pub(super) fn descriptors(scan: &Scan, deadline: Instant) -> Result<DescriptorView, Problem> {
    let mut view = BTreeMap::<u64, BTreeSet<_>>::new();
    let mut remaining = TOTAL_FDS;
    for (&tid, task) in &scan.tasks {
        for (fd, inode) in task_descriptors(&task.directory, &mut remaining, deadline)? {
            view.entry(inode).or_default().insert((tid, fd));
        }
    }
    Ok(DescriptorView(view))
}

fn fd_problem(error: Errno) -> Problem {
    match error {
        Errno::NOENT | Errno::SRCH => Problem::Changed,
        _ => Problem::Read,
    }
}

fn task_descriptors(
    task: &impl AsFd,
    remaining: &mut usize,
    deadline: Instant,
) -> Result<BTreeMap<u32, u64>, Problem> {
    check_deadline(deadline)?;
    let fd_dir = openat2(
        task,
        "fd",
        OFlags::RDONLY | OFlags::DIRECTORY | OFlags::NOFOLLOW | OFlags::CLOEXEC,
        Mode::empty(),
        ResolveFlags::BENEATH | ResolveFlags::NO_SYMLINKS | ResolveFlags::NO_XDEV,
    )
    .map_err(fd_problem)?;
    let mut directory = Dir::new(fd_dir).map_err(fd_problem)?;
    let mut names = BTreeSet::new();
    let mut sockets = BTreeMap::new();
    let mut buffer = [0; LINK_BYTES];
    loop {
        check_deadline(deadline)?;
        let Some(entry) = directory.read() else { break };
        let entry = entry.map_err(fd_problem)?;
        let name = entry.file_name();
        if matches!(name.to_bytes(), b"." | b"..") {
            continue;
        }
        if names.len() >= TASK_FDS || *remaining == 0 {
            return Err(Problem::Limit);
        }
        *remaining -= 1;
        let fd = decimal(name.to_bytes())?;
        if !names.insert(fd) {
            return Err(Problem::Duplicate);
        }
        // Keep the iterator open while reading the names it supplied, including its own FD.
        if let Some(inode) =
            descriptor_inode(&directory.fd().map_err(fd_problem)?, name, &mut buffer)?
        {
            sockets.insert(fd, inode);
        }
    }
    Ok(sockets)
}

fn decimal<T: std::str::FromStr>(bytes: &[u8]) -> Result<T, Problem> {
    if bytes.is_empty()
        || (bytes.len() != 1 && bytes.starts_with(b"0"))
        || !bytes.iter().all(u8::is_ascii_digit)
    {
        return Err(Problem::Malformed);
    }
    std::str::from_utf8(bytes)
        .map_err(|_| Problem::Malformed)?
        .parse()
        .map_err(|_| Problem::Malformed)
}

fn descriptor_inode(
    directory: &impl AsFd,
    name: &CStr,
    buffer: &mut [u8],
) -> Result<Option<u64>, Problem> {
    let len = readlinkat_raw(directory, name, &mut *buffer).map_err(fd_problem)?;
    if len == buffer.len() {
        return Err(Problem::Limit);
    }
    if len == 0 {
        return Err(Problem::Incomplete);
    }
    let Some(body) = buffer[..len].strip_prefix(b"socket:") else {
        return Ok(None);
    };
    let digits = body
        .strip_prefix(b"[")
        .and_then(|body| body.strip_suffix(b"]"))
        .ok_or(Problem::Malformed)?;
    let inode = decimal(digits)?;
    if inode == 0 {
        return Err(Problem::Incomplete);
    }
    Ok(Some(inode))
}

const NL_HEADER: usize = 16;
const NL_REQUEST: u16 = 1;
const NL_MULTI: u16 = 2;
const NL_DUMP_INTR: u16 = 0x10;
const NL_DUMP: u16 = 0x300;
const NL_ERROR: u16 = 2;
const NL_DONE: u16 = 3;
const NL_OVERRUN: u16 = 4;
const SOCK_DIAG_BY_FAMILY: u16 = 20;
const TCP_LISTEN: u8 = 10;
const IPPROTO_TCP: u8 = 6;
const INET_DIAG_SKV6ONLY: u16 = 11;
const UNIX_DIAG_NAME: u16 = 0;
const UNIX_DIAG_SHUTDOWN: u16 = 6;
const UDIAG_SHOW_NAME: u32 = 1;
const DIAG_ROWS: usize = 16_384;
const DIAG_BYTES: usize = 4 * 1024 * 1024;
const DIAG_DATAGRAMS: usize = 4096;
const DATAGRAM_BYTES: usize = 32 * 1024;

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
#[repr(u8)]
enum Family {
    Unix = 1,
    Tcp4 = 2,
    Tcp6 = 10,
}

#[derive(Debug, Default, Eq, PartialEq)]
pub(in crate::desktop_collect) struct Diagnostic {
    pub(in crate::desktop_collect) tcp: BTreeMap<u64, Tcp>,
    pub(in crate::desktop_collect) unix: BTreeMap<u64, Unix>,
}

#[derive(Debug, Eq, PartialEq)]
pub(in crate::desktop_collect) struct Tcp {
    pub(in crate::desktop_collect) address: IpAddr,
    pub(in crate::desktop_collect) port: u16,
    pub(in crate::desktop_collect) uid: u32,
    pub(in crate::desktop_collect) cookie: [u32; 2],
    pub(in crate::desktop_collect) ifindex: u32,
    pub(in crate::desktop_collect) ipv6_only: Option<bool>,
}

#[derive(Debug, Eq, PartialEq)]
pub(in crate::desktop_collect) struct Unix {
    pub(in crate::desktop_collect) socket_type: u16,
    pub(in crate::desktop_collect) state: u8,
    pub(in crate::desktop_collect) shutdown: u8,
    pub(in crate::desktop_collect) name: Vec<u8>,
    pub(in crate::desktop_collect) cookie: [u32; 2],
}

pub(super) fn diagnostic(deadline: Instant) -> Result<Diagnostic, Problem> {
    check_deadline(deadline)?;
    let socket = socket_with(
        AddressFamily::NETLINK,
        SocketType::RAW,
        SocketFlags::CLOEXEC | SocketFlags::NONBLOCK,
        Some(SOCK_DIAG),
    )
    .map_err(|_| Problem::Read)?;
    let kernel = SocketAddrNetlink::new(0, 0);
    bind(&socket, &kernel).map_err(|_| Problem::Read)?;
    let local: SocketAddrNetlink = getsockname(&socket)
        .map_err(|_| Problem::Read)?
        .try_into()
        .map_err(|_| Problem::Malformed)?;
    if local.pid() == 0 || local.groups() != 0 {
        return Err(Problem::Mismatch);
    }

    let mut diagnostic = Diagnostic::default();
    for (family, sequence) in [(Family::Tcp4, 1), (Family::Tcp6, 2), (Family::Unix, 3)] {
        check_deadline(deadline)?;
        let request = request(family, sequence, local.pid());
        let sent =
            sendto(&socket, &request, SendFlags::empty(), &kernel).map_err(|_| Problem::Read)?;
        if sent != request.len() {
            return Err(Problem::Incomplete);
        }

        let mut dump = Dump::new(family, sequence, local.pid());
        let mut buffer = [0; DATAGRAM_BYTES];
        loop {
            check_deadline(deadline)?;
            let (copied, received, sender) =
                match recvfrom(&socket, buffer.as_mut_slice(), RecvFlags::TRUNC) {
                    Ok(result) => result,
                    Err(Errno::INTR) => continue,
                    Err(Errno::AGAIN) => {
                        let remaining = deadline.saturating_duration_since(Instant::now());
                        std::thread::sleep(remaining.min(Duration::from_millis(5)));
                        continue;
                    }
                    Err(Errno::NOBUFS) => return Err(Problem::Incomplete),
                    Err(_) => return Err(Problem::Read),
                };
            if received != copied {
                return Err(Problem::Incomplete);
            }
            let sender = SocketAddrNetlink::try_from(sender.ok_or(Problem::Incomplete)?)
                .map_err(|_| Problem::Malformed)?;
            if sender != kernel {
                return Err(Problem::Mismatch);
            }
            if dump.parse(&buffer[..copied], &mut diagnostic)? {
                break;
            }
        }
    }
    check_deadline(deadline)?;
    Ok(diagnostic)
}

fn request(family: Family, sequence: u32, port_id: u32) -> Vec<u8> {
    // Linux UAPI: nlmsghdr, followed by unix_diag_req or inet_diag_req_v2.
    let mut request = vec![0; if family == Family::Unix { 40 } else { 72 }];
    let len = request.len() as u32;
    request[..4].copy_from_slice(&len.to_ne_bytes());
    request[4..6].copy_from_slice(&SOCK_DIAG_BY_FAMILY.to_ne_bytes());
    request[6..8].copy_from_slice(&(NL_REQUEST | NL_DUMP).to_ne_bytes());
    request[8..12].copy_from_slice(&sequence.to_ne_bytes());
    request[12..16].copy_from_slice(&port_id.to_ne_bytes());
    request[16] = family as u8;
    if family == Family::Unix {
        request[20..24].copy_from_slice(&u32::MAX.to_ne_bytes());
        request[28..32].copy_from_slice(&UDIAG_SHOW_NAME.to_ne_bytes());
        request[32..40].fill(0xff);
    } else {
        request[17] = IPPROTO_TCP;
        request[20..24].copy_from_slice(&(1_u32 << TCP_LISTEN).to_ne_bytes());
        request[64..72].fill(0xff);
    }
    request
}

struct Dump {
    family: Family,
    sequence: u32,
    port_id: u32,
    rows: usize,
    bytes: usize,
    datagrams: usize,
    unix_inodes: BTreeSet<u64>,
}

impl Dump {
    fn new(family: Family, sequence: u32, port_id: u32) -> Self {
        Self {
            family,
            sequence,
            port_id,
            rows: 0,
            bytes: 0,
            datagrams: 0,
            unix_inodes: BTreeSet::new(),
        }
    }

    fn parse(&mut self, mut datagram: &[u8], view: &mut Diagnostic) -> Result<bool, Problem> {
        self.datagrams += 1;
        self.bytes += datagram.len();
        if self.datagrams > DIAG_DATAGRAMS || self.bytes > DIAG_BYTES {
            return Err(Problem::Limit);
        }
        if datagram.is_empty() {
            return Err(Problem::Incomplete);
        }
        while !datagram.is_empty() {
            let length = u32::from_ne_bytes(field(datagram, 0)?) as usize;
            let (message, remaining) = frame(datagram, length, NL_HEADER)?;
            let kind = u16::from_ne_bytes(field(message, 4)?);
            let flags = u16::from_ne_bytes(field(message, 6)?);
            let sequence = u32::from_ne_bytes(field(message, 8)?);
            let port_id = u32::from_ne_bytes(field(message, 12)?);
            if sequence != self.sequence || port_id != self.port_id {
                return Err(Problem::Mismatch);
            }
            if flags & NL_DUMP_INTR != 0 || matches!(kind, NL_ERROR | NL_OVERRUN) {
                return Err(Problem::Incomplete);
            }
            if flags != NL_MULTI {
                return Err(Problem::Unsupported);
            }
            let body = &message[NL_HEADER..];
            match kind {
                NL_DONE => {
                    if body != [0; 4] || !remaining.is_empty() {
                        return Err(Problem::Incomplete);
                    }
                    return Ok(true);
                }
                SOCK_DIAG_BY_FAMILY => {
                    self.rows += 1;
                    if self.rows > DIAG_ROWS {
                        return Err(Problem::Limit);
                    }
                    match self.family {
                        Family::Unix => {
                            let inode = read_unix(body, view)?;
                            if !self.unix_inodes.insert(inode) {
                                return Err(Problem::Duplicate);
                            }
                        }
                        family => read_tcp(body, family, view)?,
                    }
                }
                _ => return Err(Problem::Unsupported),
            }
            datagram = remaining;
        }
        Ok(false)
    }
}

fn field<const N: usize>(bytes: &[u8], offset: usize) -> Result<[u8; N], Problem> {
    bytes
        .get(offset..offset + N)
        .and_then(|field| field.try_into().ok())
        .ok_or(Problem::Incomplete)
}

fn frame(bytes: &[u8], len: usize, header: usize) -> Result<(&[u8], &[u8]), Problem> {
    if len < header {
        return Err(Problem::Malformed);
    }
    let aligned = len.checked_next_multiple_of(4).ok_or(Problem::Limit)?;
    if aligned > bytes.len() {
        return Err(Problem::Incomplete);
    }
    Ok((&bytes[..len], &bytes[aligned..]))
}

fn attributes(
    mut bytes: &[u8],
    mut visit: impl FnMut(u16, &[u8]) -> Result<(), Problem>,
) -> Result<(), Problem> {
    const HEADER: usize = 4;
    while !bytes.is_empty() {
        let len = usize::from(u16::from_ne_bytes(field(bytes, 0)?));
        let (attribute, tail) = frame(bytes, len, HEADER)?;
        let kind = u16::from_ne_bytes(field(attribute, 2)?);
        visit(kind, &attribute[HEADER..])?;
        bytes = tail;
    }
    Ok(())
}

fn cookie(body: &[u8], offset: usize) -> Result<[u32; 2], Problem> {
    let id = [
        u32::from_ne_bytes(field(body, offset)?),
        u32::from_ne_bytes(field(body, offset + 4)?),
    ];
    if matches!(id, [0, 0] | [u32::MAX, u32::MAX]) {
        return Err(Problem::Incomplete);
    }
    Ok(id)
}

fn read_tcp(body: &[u8], family: Family, view: &mut Diagnostic) -> Result<(), Problem> {
    // Linux inet_diag_msg uses network byte order for addresses and ports.
    let attrs = body.get(72..).ok_or(Problem::Incomplete)?;
    if body[0] != family as u8 || body[1] != TCP_LISTEN {
        return Err(Problem::Mismatch);
    }
    let port = u16::from_be_bytes(field(body, 4)?);
    if port == 0 || body[6..8] != [0; 2] || body[24..40] != [0; 16] {
        return Err(Problem::Malformed);
    }
    let address = match family {
        Family::Tcp4 if body[12..24] == [0; 12] => IpAddr::V4(Ipv4Addr::from(field::<4>(body, 8)?)),
        Family::Tcp6 => IpAddr::V6(Ipv6Addr::from(field::<16>(body, 8)?)),
        _ => return Err(Problem::Malformed),
    };
    let inode = u64::from(u32::from_ne_bytes(field(body, 68)?));
    let uid = u32::from_ne_bytes(field(body, 64)?);
    if inode == 0 || uid == u32::MAX {
        return Err(Problem::Incomplete);
    }
    let mut ipv6_only = None;
    attributes(attrs, |kind, value| {
        if kind == INET_DIAG_SKV6ONLY {
            if ipv6_only.is_some() || family != Family::Tcp6 {
                return Err(Problem::Malformed);
            }
            ipv6_only = match value {
                [0] => Some(false),
                [1] => Some(true),
                _ => return Err(Problem::Malformed),
            };
        }
        Ok(())
    })?;
    if family == Family::Tcp6 && ipv6_only.is_none() {
        return Err(Problem::Incomplete);
    }
    if view
        .tcp
        .insert(
            inode,
            Tcp {
                address,
                port,
                uid,
                cookie: cookie(body, 44)?,
                ifindex: u32::from_ne_bytes(field(body, 40)?),
                ipv6_only,
            },
        )
        .is_some()
    {
        return Err(Problem::Duplicate);
    }
    Ok(())
}

fn read_unix(body: &[u8], view: &mut Diagnostic) -> Result<u64, Problem> {
    let attrs = body.get(16..).ok_or(Problem::Incomplete)?;
    if body[0] != Family::Unix as u8 {
        return Err(Problem::Mismatch);
    }
    let socket_type = u16::from(body[1]);
    if !matches!(socket_type, 1 | 2 | 5) || body[3] != 0 {
        return Err(Problem::Unsupported);
    }
    let inode = u64::from(u32::from_ne_bytes(field(body, 4)?));
    if inode == 0 {
        return Err(Problem::Incomplete);
    }
    let cookie = cookie(body, 8)?;
    let mut name = None;
    let mut shutdown = None;
    attributes(attrs, |kind, value| {
        match kind {
            UNIX_DIAG_NAME => {
                if name.is_some() {
                    return Err(Problem::Duplicate);
                }
                match value {
                    [0, ..] if value.len() <= 108 => {}
                    [_, .., 0] if value.len() <= 109 && !value[..value.len() - 1].contains(&0) => {}
                    _ => return Err(Problem::Malformed),
                }
                name = Some(value.to_vec());
            }
            UNIX_DIAG_SHUTDOWN => {
                if shutdown.is_some() {
                    return Err(Problem::Duplicate);
                }
                let [byte @ 0..=3] = value else {
                    return Err(Problem::Malformed);
                };
                shutdown = Some(*byte);
            }
            _ => return Err(Problem::Unsupported),
        }
        Ok(())
    })?;
    let shutdown = shutdown.ok_or(Problem::Incomplete)?;
    if let Some(name) = name {
        if view
            .unix
            .insert(
                inode,
                Unix {
                    socket_type,
                    name,
                    cookie,
                    state: body[2],
                    shutdown,
                },
            )
            .is_some()
        {
            return Err(Problem::Duplicate);
        }
    }
    Ok(inode)
}

#[cfg(test)]
mod tests;
