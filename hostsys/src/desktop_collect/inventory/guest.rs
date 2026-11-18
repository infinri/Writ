use std::collections::BTreeSet;
use std::fmt::Write;
use std::net::{IpAddr, Ipv4Addr, Ipv6Addr};

use super::super::guest::sockets::Diagnostic;
use super::{
    decimal, hex, parse_tcp, record_inode, table_rows, Endpoint, Inventory, Kind, Problem, Socket,
    TcpFamily, TABLE_ROWS, UNIX_HEADER,
};

pub(in crate::desktop_collect) fn verify_endpoints(
    tcp4: &[u8],
    tcp6: &[u8],
    unix: &[u8],
    diagnostic: &Diagnostic,
) -> Result<Vec<Socket>, Problem> {
    let mut inodes = BTreeSet::new();
    let mut sockets = Vec::new();
    for (table, family) in [(tcp4, TcpFamily::V4), (tcp6, TcpFamily::V6)] {
        let text = std::str::from_utf8(table).map_err(|_| Problem::Malformed)?;
        if !text.ends_with('\n') {
            return Err(Problem::Incomplete);
        }
        parse_tcp(text, family, &mut inodes, &mut sockets)?;
    }
    if sockets.len() != diagnostic.tcp.len() {
        return Err(Problem::Mismatch);
    }
    for socket in &mut sockets {
        let diag = diagnostic.tcp.get(&socket.inode).ok_or(Problem::Mismatch)?;
        let Endpoint::Tcp {
            family,
            address,
            port,
        } = socket.endpoint
        else {
            return Err(Problem::Mismatch);
        };
        let address = match family {
            TcpFamily::V4 => IpAddr::V4(Ipv4Addr::from((address as u32).to_ne_bytes())),
            TcpFamily::V6 => {
                let mut bytes = [0; 16];
                let network_words = address.to_be_bytes();
                for (dst, src) in bytes
                    .as_chunks_mut::<4>()
                    .0
                    .iter_mut()
                    .zip(network_words.as_chunks::<4>().0)
                {
                    *dst = u32::from_be_bytes(*src).to_ne_bytes();
                }
                IpAddr::V6(Ipv6Addr::from(bytes))
            }
        };
        if address != diag.address || port != diag.port || socket.tcp_uid != Some(diag.uid) {
            return Err(Problem::Mismatch);
        }
        socket.endpoint = Endpoint::TcpObserved {
            address,
            port,
            interface: diag.ifindex,
            ipv6_only: diag.ipv6_only,
            cookie: diag.cookie,
        };
    }
    verify_unix(unix, diagnostic, &mut inodes, &mut sockets)?;
    Ok(sockets)
}

fn verify_unix(
    table: &[u8],
    diagnostic: &Diagnostic,
    inodes: &mut BTreeSet<u64>,
    sockets: &mut Vec<Socket>,
) -> Result<(), Problem> {
    let header_end = table
        .iter()
        .position(|&byte| byte == b'\n')
        .ok_or(Problem::Incomplete)?;
    let header = std::str::from_utf8(&table[..=header_end]).map_err(|_| Problem::Malformed)?;
    let _rows = table_rows(header, UNIX_HEADER)?;
    let mut rest = &table[header_end + 1..];
    let mut named = 0;
    let mut rows = 0;
    while !rest.is_empty() {
        rows += 1;
        if rows > TABLE_ROWS {
            return Err(Problem::Limit);
        }
        let number = field(&mut rest)?
            .strip_suffix(':')
            .ok_or(Problem::Malformed)?;
        hex(number, 8, 16)?;
        hex(field(&mut rest)?, 8, 8)?;
        let protocol = hex(field(&mut rest)?, 8, 8)?;
        let flags = hex(field(&mut rest)?, 8, 8)?;
        let socket_type = hex(field(&mut rest)?, 4, 4)? as u16;
        let state = hex(field(&mut rest)?, 2, 2)?;
        if protocol != 0
            || !matches!(flags, 0 | 0x10000)
            || !matches!(socket_type, 1 | 2 | 5)
            || !(1..=4).contains(&state)
        {
            return Err(Problem::Unsupported);
        }
        let inode = decimal(field(&mut rest)?)?;
        record_inode(inodes, inode)?;
        if let Some(diag) = diagnostic.unix.get(&inode) {
            let expected_flags = if diag.state == 10 { 0x10000 } else { 0 };
            let expected_state = if diag.state == 1 { 3 } else { 1 };
            if inode == 0
                || socket_type != diag.socket_type
                || flags != expected_flags
                || state != expected_state
            {
                return Err(Problem::Mismatch);
            }
            let (kind, displayed) = match diag.name.first() {
                Some(0) => (Kind::Abstract, diag.name.as_slice()),
                Some(_) => (
                    Kind::Unix,
                    diag.name.strip_suffix(&[0]).ok_or(Problem::Malformed)?,
                ),
                None => return Err(Problem::Malformed),
            };
            rest = rest.strip_prefix(b" ").ok_or(Problem::Mismatch)?;
            let proc_name = rest.get(..displayed.len()).ok_or(Problem::Incomplete)?;
            if !proc_name.iter().copied().eq(displayed.iter().map(|&byte| {
                if byte == 0 {
                    b'@'
                } else {
                    byte
                }
            })) {
                return Err(Problem::Mismatch);
            }
            rest = &rest[displayed.len()..];
            sockets.push(Socket {
                inode,
                tcp_uid: None,
                kind,
                endpoint: Endpoint::UnixObserved {
                    socket_type,
                    name: diag.name.clone(),
                    cookie: diag.cookie,
                    state: diag.state,
                    shutdown: diag.shutdown,
                },
            });
            named += 1;
        }
        rest = rest.strip_prefix(b"\n").ok_or(Problem::Mismatch)?;
    }
    if named != diagnostic.unix.len() {
        return Err(Problem::Mismatch);
    }
    Ok(())
}

fn field<'a>(row: &mut &'a [u8]) -> Result<&'a str, Problem> {
    while let Some(rest) = row.strip_prefix(b" ") {
        *row = rest;
    }
    let end = row
        .iter()
        .position(|&byte| matches!(byte, b' ' | b'\n'))
        .ok_or(Problem::Incomplete)?;
    if end == 0 {
        return Err(Problem::Malformed);
    }
    let value = std::str::from_utf8(&row[..end]).map_err(|_| Problem::Malformed)?;
    *row = &row[end..];
    Ok(value)
}

pub(in crate::desktop_collect) fn render(
    output: &mut String,
    inventory: &Inventory,
) -> Result<(), Problem> {
    for (&(uid, kind), targets) in inventory {
        for target in targets {
            write!(output, "endpoint {uid} {kind:?} {} ", target.inode)
                .map_err(|_| Problem::Limit)?;
            match &target.endpoint {
                Endpoint::UnixObserved {
                    socket_type,
                    name,
                    cookie,
                    state,
                    shutdown,
                } => {
                    write!(
                        output,
                        "{socket_type} {state} {shutdown} {:08x}{:08x} ",
                        cookie[0], cookie[1]
                    )
                    .map_err(|_| Problem::Limit)?;
                    for byte in name {
                        write!(output, "{byte:02x}").map_err(|_| Problem::Limit)?;
                    }
                }
                Endpoint::TcpObserved {
                    address,
                    port,
                    interface,
                    ipv6_only,
                    cookie,
                } => {
                    let mode = match ipv6_only {
                        Some(true) => "v6-only",
                        Some(false) => "dual-stack",
                        None => "v4",
                    };
                    write!(
                        output,
                        "{address} {port} {interface} {mode} {:08x}{:08x}",
                        cookie[0], cookie[1]
                    )
                    .map_err(|_| Problem::Limit)?;
                }
                _ => return Err(Problem::Mismatch),
            }
            output.push('\n');
            for holder in &target.holders {
                writeln!(
                    output,
                    "holder {} {} {}",
                    target.inode, holder.pid, holder.start_ticks
                )
                .map_err(|_| Problem::Limit)?;
            }
        }
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::desktop_collect::guest::sockets::{Tcp, Unix};
    use crate::desktop_collect::inventory::{collect_targets, Holder, TCP4_HEADER, TCP6_HEADER};

    #[test]
    fn independently_corroborates_tcp_and_exact_unix_addresses() -> Result<(), Problem> {
        let (tcp4, tcp6, unix, diagnostic) = fixture();
        let sockets = verify_endpoints(tcp4.as_bytes(), tcp6.as_bytes(), &unix, &diagnostic)?;
        let inventory = collect_targets(sockets, &BTreeSet::from([1000]), |_| {
            Ok(BTreeSet::from([Holder {
                pid: 40,
                start_ticks: 10,
                uid: 1000,
            }]))
        })?;
        let mut output = String::new();
        render(&mut output, &inventory)?;
        assert!(output.contains("endpoint 1000 Tcp 41 127.0.0.1 12345 0 v4 0000000100000003\n"));
        assert!(output.contains("endpoint 1000 Tcp 42 ::1 12345 0 v6-only 0000000200000003\n"));
        assert!(output.contains("endpoint 1000 Abstract 51 1 10 0 0000000500000006 0040000aff\n"));
        assert!(output.contains("endpoint 1000 Unix 52 1 10 0 0000000400000006 40406400\n"));
        Ok(())
    }

    #[test]
    fn missing_changed_and_extra_rows_are_unavailable() {
        let (tcp4, tcp6, mut unix, mut diagnostic) = fixture();
        diagnostic.unix.remove(&51);
        assert_eq!(
            verify_endpoints(tcp4.as_bytes(), tcp6.as_bytes(), &unix, &diagnostic).err(),
            Some(Problem::Mismatch)
        );
        let (tcp4, tcp6, _, mut diagnostic) = fixture();
        if let Some(tcp) = diagnostic.tcp.get_mut(&42) {
            tcp.uid = 1001;
        }
        assert_eq!(
            verify_endpoints(tcp4.as_bytes(), tcp6.as_bytes(), &unix, &diagnostic).err(),
            Some(Problem::Mismatch)
        );
        let (_, _, _, diagnostic) = fixture();
        unix.pop();
        assert!(verify_endpoints(tcp4.as_bytes(), tcp6.as_bytes(), &unix, &diagnostic).is_err());
        let (_, _, unix, diagnostic) = fixture();
        assert_eq!(
            verify_endpoints(
                tcp4.trim_end().as_bytes(),
                tcp6.as_bytes(),
                &unix,
                &diagnostic
            )
            .err(),
            Some(Problem::Incomplete)
        );
    }

    #[test]
    fn unix_state_disagreement_and_unowned_pending_accept_refuse() {
        let (tcp4, tcp6, mut unix, mut diagnostic) = fixture();
        if let Some(socket) = diagnostic.unix.get_mut(&51) {
            socket.state = 1;
        }
        assert_eq!(
            verify_endpoints(tcp4.as_bytes(), tcp6.as_bytes(), &unix, &diagnostic).err(),
            Some(Problem::Mismatch)
        );
        let (_, _, _, diagnostic) = fixture();
        unix.extend_from_slice(b"0000000000000000: 00000002 00000000 00000000 0001 02 0 @@d\n");
        assert_eq!(
            verify_endpoints(tcp4.as_bytes(), tcp6.as_bytes(), &unix, &diagnostic).err(),
            Some(Problem::Mismatch)
        );
    }

    fn fixture() -> (String, String, Vec<u8>, Diagnostic) {
        let mut tcp4 = format!("{TCP4_HEADER}\n0: {:08X}:3039 00000000:0000 0A 00000000:00000000 00:00000000 00000000 1000 0 41 1 0 100 0 0 10 0\n",
            u32::from_ne_bytes([127, 0, 0, 1]));
        tcp4.push_str(
            "1: 00000000:3039 00000000:0001 05 00000000:00000000 03:00000000 00000000 0 0 0 3 0\n",
        );
        let tcp6 = format!("{TCP6_HEADER}\n0: 000000000000000000000000{:08X}:3039 00000000000000000000000000000000:0000 0A 00000000:00000000 00:00000000 00000000 1000 0 42 1 0 100 0 0 10 0\n",
            u32::from_ne_bytes([0, 0, 0, 1]));
        let unix = [
            UNIX_HEADER.as_bytes(),
            b"\n",
            b"0000000000000000: 00000002 00000000 00010000 0001 01 51 @@@\n\xff\n",
            b"0000000000000000: 00000002 00000000 00010000 0001 01 52 @@d\n",
        ]
        .concat();
        let tcp = [
            (41, IpAddr::V4(Ipv4Addr::LOCALHOST), [1, 3]),
            (42, IpAddr::V6(Ipv6Addr::LOCALHOST), [2, 3]),
        ]
        .into_iter()
        .map(|(inode, address, cookie)| {
            (
                inode,
                Tcp {
                    address,
                    cookie,
                    ifindex: 0,
                    ipv6_only: address.is_ipv6().then_some(true),
                    port: 12345,
                    uid: 1000,
                },
            )
        })
        .collect();
        let named = [
            (51, b"\0@\0\n\xff".to_vec(), [5, 6]),
            (52, b"@@d\0".to_vec(), [4, 6]),
        ]
        .into_iter()
        .map(|(inode, name, cookie)| {
            (
                inode,
                Unix {
                    socket_type: 1,
                    name,
                    cookie,
                    state: 10,
                    shutdown: 0,
                },
            )
        })
        .collect();
        (tcp4, tcp6, unix, Diagnostic { tcp, unix: named })
    }
}
