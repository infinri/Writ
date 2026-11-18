use std::collections::{BTreeMap, BTreeSet};
use std::str::FromStr;

#[cfg(test)]
use super::identity::Credentials;
use super::Problem;
#[cfg(test)]
use super::{read_text, valid_pid, Input, Source};

#[cfg(target_os = "linux")]
mod guest;
#[cfg(target_os = "linux")]
pub(super) use guest::{render, verify_endpoints};

pub(super) const TABLE_BYTES: usize = 4 * 1024 * 1024;
const TABLE_ROWS: usize = 16_384;
#[cfg(test)]
const OWNER_ROWS: usize = 65_536;
const UNIX_HEADER: &str = "Num RefCount Protocol Flags Type St Inode Path";
const TCP4_HEADER: &str =
    "sl local_address rem_address st tx_queue rx_queue tr tm->when retrnsmt uid timeout inode";
const TCP6_HEADER: &str =
    "sl local_address remote_address st tx_queue rx_queue tr tm->when retrnsmt uid timeout inode";

pub(super) type Inventory = BTreeMap<(u32, Kind), BTreeSet<Target>>;

#[derive(Clone, Copy, Debug, Eq, Ord, PartialEq, PartialOrd)]
pub(super) enum Kind {
    Tcp,
    Unix,
    Abstract,
}

#[derive(Debug, Eq, Ord, PartialEq, PartialOrd)]
pub(super) struct Target {
    endpoint: Endpoint,
    inode: u64,
    holders: BTreeSet<Holder>,
}

#[derive(Debug, Eq, Ord, PartialEq, PartialOrd)]
enum Endpoint {
    Tcp {
        family: TcpFamily,
        address: u128,
        port: u16,
    },
    #[cfg(target_os = "linux")]
    TcpObserved {
        address: std::net::IpAddr,
        port: u16,
        interface: u32,
        ipv6_only: Option<bool>,
        cookie: [u32; 2],
    },
    #[cfg(target_os = "linux")]
    UnixObserved {
        socket_type: u16,
        name: Vec<u8>,
        state: u8,
        shutdown: u8,
        cookie: [u32; 2],
    },
    #[cfg(test)]
    Unix { socket_type: u16, name: String },
}

#[derive(Clone, Copy, Debug, Eq, Ord, PartialEq, PartialOrd)]
enum TcpFamily {
    V4,
    V6,
}

#[derive(Clone, Copy, Debug, Eq, Ord, PartialEq, PartialOrd)]
pub(super) struct Holder {
    pub(super) pid: u32,
    pub(super) start_ticks: u64,
    pub(super) uid: u32,
}

pub(super) struct Socket {
    inode: u64,
    tcp_uid: Option<u32>,
    kind: Kind,
    endpoint: Endpoint,
}

#[cfg(test)]
pub(super) fn collect(
    source: &mut impl Source,
    desktops: &BTreeSet<u32>,
    selected_processes: &BTreeMap<u32, Credentials>,
) -> Result<Inventory, Problem> {
    let mut inodes = BTreeSet::new();
    let mut sockets = Vec::new();
    parse_unix(
        &read_text(source, Input::NetUnix, TABLE_BYTES)?,
        &mut inodes,
        &mut sockets,
    )?;
    for (input, family) in [
        (Input::NetTcp, TcpFamily::V4),
        (Input::NetTcp6, TcpFamily::V6),
    ] {
        parse_tcp(
            &read_text(source, input, TABLE_BYTES)?,
            family,
            &mut inodes,
            &mut sockets,
        )?;
    }

    let mut owners = parse_owners(
        &read_text(source, Input::SocketOwners, TABLE_BYTES)?,
        selected_processes,
    )?;
    collect_targets(sockets, desktops, |inode| {
        owners.remove(&inode).ok_or(Problem::Incomplete)
    })
}

pub(super) fn collect_targets(
    sockets: Vec<Socket>,
    desktops: &BTreeSet<u32>,
    mut owners: impl FnMut(u64) -> Result<BTreeSet<Holder>, Problem>,
) -> Result<Inventory, Problem> {
    let mut inventory = Inventory::new();
    for socket in sockets {
        let holders = owners(socket.inode)?;
        let uid = holders.first().ok_or(Problem::Incomplete)?.uid;
        if holders.iter().any(|holder| holder.uid != uid) {
            return Err(Problem::Ambiguous);
        }
        if socket.tcp_uid.is_some_and(|tcp_uid| tcp_uid != uid) {
            return Err(Problem::Mismatch);
        }
        if desktops.contains(&uid) {
            #[cfg(test)]
            if socket.kind == Kind::Unix
                && matches!(&socket.endpoint, Endpoint::Unix { name, .. } if !name.starts_with('/'))
            {
                return Err(Problem::Unsupported);
            }
            inventory
                .entry((uid, socket.kind))
                .or_default()
                .insert(Target {
                    endpoint: socket.endpoint,
                    inode: socket.inode,
                    holders,
                });
        }
    }
    Ok(inventory)
}

#[cfg(test)]
fn parse_unix(
    text: &str,
    inodes: &mut BTreeSet<u64>,
    sockets: &mut Vec<Socket>,
) -> Result<(), Problem> {
    for (index, mut row) in table_rows(text, UNIX_HEADER)?.enumerate() {
        if index >= TABLE_ROWS {
            return Err(Problem::Limit);
        }
        let num = take_field(&mut row)?
            .strip_suffix(':')
            .ok_or(Problem::Malformed)?;
        hex(num, 8, 16)?;
        hex(take_field(&mut row)?, 8, 8)?;
        let protocol = hex(take_field(&mut row)?, 8, 8)?;
        let flags = hex(take_field(&mut row)?, 8, 8)?;
        let socket_type = hex(take_field(&mut row)?, 4, 4)? as u16;
        let state = hex(take_field(&mut row)?, 2, 2)?;
        if protocol != 0
            || !matches!(flags, 0 | 0x10000)
            || !matches!(socket_type, 1 | 2 | 5)
            || !(1..=4).contains(&state)
        {
            return Err(Problem::Unsupported);
        }
        let inode = decimal(take_field(&mut row)?)?;
        record_inode(inodes, inode)?;
        if row.is_empty() {
            // An unnamed socket has no address a peer can use to connect.
            continue;
        }
        if inode == 0 {
            return Err(Problem::Incomplete);
        }
        let name = row.strip_prefix(' ').ok_or(Problem::Malformed)?;
        if name.is_empty() || name.len() > 108 || name.chars().any(char::is_control) {
            return Err(Problem::Unsupported);
        }
        // The fixture source must guarantee that proc display names faithfully
        // represent addresses. Proc cannot distinguish an abstract address from
        // a relative filename starting with '@', or an embedded NUL from '@'.
        let kind = if name.starts_with('@') {
            Kind::Abstract
        } else {
            Kind::Unix
        };
        sockets.push(Socket {
            inode,
            tcp_uid: None,
            kind,
            endpoint: Endpoint::Unix {
                socket_type,
                name: name.to_owned(),
            },
        });
    }
    Ok(())
}

fn parse_tcp(
    text: &str,
    family: TcpFamily,
    inodes: &mut BTreeSet<u64>,
    sockets: &mut Vec<Socket>,
) -> Result<(), Problem> {
    let (header, width) = match family {
        TcpFamily::V4 => (TCP4_HEADER, 8),
        TcpFamily::V6 => (TCP6_HEADER, 32),
    };
    let mut slots = BTreeSet::new();
    for (index, row) in table_rows(text, header)?.enumerate() {
        if index >= TABLE_ROWS {
            return Err(Problem::Limit);
        }
        let columns: Vec<_> = row.split_ascii_whitespace().take(18).collect();
        if !matches!(columns.len(), 12 | 17) {
            return Err(Problem::Malformed);
        }
        let slot: u32 = decimal(columns[0].strip_suffix(':').ok_or(Problem::Malformed)?)?;
        if !slots.insert(slot) {
            return Err(Problem::Duplicate);
        }
        let (address, port) = hex_pair(columns[1], width, width, 4, 4)?;
        let remote = hex_pair(columns[2], width, width, 4, 4)?;
        let state = hex(columns[3], 2, 2)?;
        if !(1..=13).contains(&state) {
            return Err(Problem::Unsupported);
        }
        hex_pair(columns[4], 8, 8, 8, 8)?;
        let (timer, _) = hex_pair(columns[5], 2, 2, 8, 16)?;
        hex(columns[6], 8, 8)?;
        let uid = decimal(columns[7])?;
        if uid == u32::MAX {
            return Err(Problem::Malformed);
        }
        decimal::<u32>(columns[8])?;
        let inode = decimal(columns[9])?;
        decimal::<u32>(columns[10])?;
        hex(columns[11], 1, 16)?;
        if columns.len() == 17 {
            for value in &columns[12..16] {
                decimal::<u64>(value)?;
            }
            columns[16].parse::<i64>().map_err(|_| Problem::Malformed)?;
        } else if inode != 0 || !matches!((state, timer), (3 | 12, 1) | (5 | 6, 3)) {
            return Err(Problem::Incomplete);
        }
        record_inode(inodes, inode)?;
        if state == 10 {
            if inode == 0 {
                return Err(Problem::Incomplete);
            }
            if port == 0 || remote != (0, 0) {
                return Err(Problem::Malformed);
            }
            sockets.push(Socket {
                inode,
                tcp_uid: Some(uid),
                kind: Kind::Tcp,
                endpoint: Endpoint::Tcp {
                    family,
                    address,
                    port: port as u16,
                },
            });
        }
    }
    Ok(())
}

// Each row describes one process holding FDs for an inode. Its UID must be
// common to all four process UIDs. The fixture source must report all holders
// from the same namespace and decline mixed credentials.
#[cfg(test)]
fn parse_owners(
    text: &str,
    selected_processes: &BTreeMap<u32, Credentials>,
) -> Result<BTreeMap<u64, BTreeSet<Holder>>, Problem> {
    let mut processes = BTreeMap::new();
    let mut owners: BTreeMap<u64, BTreeSet<Holder>> = BTreeMap::new();
    for (index, row) in text.split_terminator('\n').enumerate() {
        if index >= OWNER_ROWS {
            return Err(Problem::Limit);
        }
        let mut fields = row.split_ascii_whitespace();
        let [Some(inode), Some(pid), Some(start_ticks), Some(uid), None] = [
            fields.next(),
            fields.next(),
            fields.next(),
            fields.next(),
            fields.next(),
        ] else {
            return Err(Problem::Malformed);
        };
        let inode = decimal(inode)?;
        let holder = Holder {
            pid: decimal(pid)?,
            start_ticks: decimal(start_ticks)?,
            uid: decimal(uid)?,
        };
        if inode == 0 || !valid_pid(holder.pid) || holder.uid == u32::MAX {
            return Err(Problem::Malformed);
        }
        if let Some(selected) = selected_processes.get(&holder.pid) {
            if selected.start != holder.start_ticks {
                return Err(Problem::Changed);
            }
            if selected.uids != [holder.uid; 4] {
                return Err(Problem::Mismatch);
            }
        }
        if processes
            .insert(holder.pid, holder)
            .is_some_and(|prior| prior != holder)
        {
            return Err(Problem::Changed);
        }
        if !owners.entry(inode).or_default().insert(holder) {
            return Err(Problem::Duplicate);
        }
    }
    Ok(owners)
}

fn table_rows<'a>(text: &'a str, header: &str) -> Result<impl Iterator<Item = &'a str>, Problem> {
    let mut lines = text.split_terminator('\n');
    let actual = lines.next().ok_or(Problem::Incomplete)?;
    if actual
        .split_ascii_whitespace()
        .ne(header.split_ascii_whitespace())
    {
        return Err(Problem::Unsupported);
    }
    Ok(lines)
}

#[cfg(test)]
fn take_field<'a>(row: &mut &'a str) -> Result<&'a str, Problem> {
    let remaining = row.trim_start_matches(|c: char| c.is_ascii_whitespace());
    if remaining.is_empty() {
        return Err(Problem::Malformed);
    }
    let end = remaining
        .find(|c: char| c.is_ascii_whitespace())
        .unwrap_or(remaining.len());
    let (field, rest) = remaining.split_at(end);
    *row = rest;
    Ok(field)
}

fn record_inode(inodes: &mut BTreeSet<u64>, inode: u64) -> Result<(), Problem> {
    if inode != 0 && !inodes.insert(inode) {
        return Err(Problem::Duplicate);
    }
    Ok(())
}

fn decimal<T: FromStr>(value: &str) -> Result<T, Problem> {
    if value.is_empty() || !value.bytes().all(|byte| byte.is_ascii_digit()) {
        return Err(Problem::Malformed);
    }
    value.parse().map_err(|_| Problem::Malformed)
}

fn hex(value: &str, min: usize, max: usize) -> Result<u128, Problem> {
    if !(min..=max).contains(&value.len()) || !value.bytes().all(|byte| byte.is_ascii_hexdigit()) {
        return Err(Problem::Malformed);
    }
    u128::from_str_radix(value, 16).map_err(|_| Problem::Malformed)
}

fn hex_pair(
    value: &str,
    a_min: usize,
    a_max: usize,
    b_min: usize,
    b_max: usize,
) -> Result<(u128, u128), Problem> {
    let (first, second) = value.split_once(':').ok_or(Problem::Malformed)?;
    Ok((hex(first, a_min, a_max)?, hex(second, b_min, b_max)?))
}

#[cfg(test)]
mod tests {
    use std::io::{self, Cursor, Read};

    use super::*;

    const FILESYSTEM: &str = "00000000: 00000002 00000000 00010000 0001 01 101 /tmp/a b ";
    const ABSTRACT: &str = "00000000: 00000002 00000000 00000000 0002 01 102 @desktop";
    const UNNAMED: &str = "00000000: 00000002 00000000 00000000 0001 03 103";
    const TCP4: &str = "0: 0100007F:2710 00000000:0000 0A 00000000:00000000 00:00000000 00000000 1000 0 104 1 0 100 0 0 10 0";
    const TCP6: &str = "0: 00000000000000000000000001000000:2710 00000000000000000000000000000000:0000 0A 00000000:00000000 00:00000000 00000000 1000 0 105 1 0 100 0 0 10 0";
    const TIME_WAIT: &str =
        "1: 0100007F:2710 0100007F:2711 06 00000000:00000000 03:00000000 00000000 0 0 0 3 0";

    struct Fixture(BTreeMap<Input, String>);

    impl Source for Fixture {
        fn open(&mut self, input: Input) -> Result<Box<dyn Read + '_>, Problem> {
            let contents = self.0.get(&input).ok_or(Problem::Missing)?;
            Ok(Box::new(Cursor::new(contents.as_bytes())))
        }
    }

    fn fixture() -> Fixture {
        Fixture(BTreeMap::from([
            (Input::NetUnix, format!("{UNIX_HEADER}\n{FILESYSTEM}\n{ABSTRACT}\n{UNNAMED}\n")),
            (Input::NetTcp, format!("{TCP4_HEADER}\n{TCP4}\n{TIME_WAIT}\n")),
            (Input::NetTcp6, format!("{TCP6_HEADER}\n{TCP6}\n")),
            (
                Input::SocketOwners,
                "101 40 542 1000\n102 40 542 1000\n104 40 542 1000\n104 41 543 1000\n105 42 544 1000\n".into(),
            ),
        ]))
    }

    fn sample(source: &mut impl Source) -> Result<Inventory, Problem> {
        collect(source, &BTreeSet::from([1000]), &BTreeMap::new())
    }

    #[test]
    fn collects_both_tcp_families_and_addressed_unix_for_desktop_uids() -> Result<(), Problem> {
        let mut fixture = fixture();
        let unix = fixture.0.get_mut(&Input::NetUnix).ok_or(Problem::Missing)?;
        unix.push_str(
            "00000000: 00000002 00000000 00010000 0001 01 123 /run/user/1000/owned-by-root\n",
        );
        fixture
            .0
            .get_mut(&Input::SocketOwners)
            .ok_or(Problem::Missing)?
            .push_str("123 1 23 0\n");

        let inventory = sample(&mut fixture)?;
        assert_eq!(inventory.len(), 3);
        let tcp: Vec<_> = inventory[&(1000, Kind::Tcp)].iter().collect();
        assert_eq!(tcp.len(), 2);
        assert_eq!(
            tcp[0].endpoint,
            Endpoint::Tcp {
                family: TcpFamily::V4,
                address: 0x0100007f,
                port: 10000
            }
        );
        assert_eq!(tcp[0].holders.len(), 2);
        assert_eq!(
            tcp[1].endpoint,
            Endpoint::Tcp {
                family: TcpFamily::V6,
                address: 0x01000000,
                port: 10000
            }
        );
        let unix = inventory[&(1000, Kind::Unix)]
            .first()
            .ok_or(Problem::Missing)?;
        assert_eq!(
            unix.endpoint,
            Endpoint::Unix {
                socket_type: 1,
                name: "/tmp/a b ".into()
            }
        );
        let abstract_socket = inventory[&(1000, Kind::Abstract)]
            .first()
            .ok_or(Problem::Missing)?;
        assert_eq!(
            abstract_socket.endpoint,
            Endpoint::Unix {
                socket_type: 2,
                name: "@desktop".into()
            }
        );
        Ok(())
    }

    #[test]
    fn canonicalizes_order_but_keeps_replacement_identity() -> Result<(), Problem> {
        let expected = sample(&mut fixture())?;
        let mut fixture = fixture();
        fixture.0.insert(
            Input::NetUnix,
            format!("{UNIX_HEADER}\n{UNNAMED}\n{ABSTRACT}\n{FILESYSTEM}\n"),
        );
        let owners = fixture
            .0
            .get_mut(&Input::SocketOwners)
            .ok_or(Problem::Missing)?;
        let reversed = owners.lines().rev().collect::<Vec<_>>().join("\n");
        *owners = format!("{reversed}\n");
        assert_eq!(sample(&mut fixture)?, expected);

        let owners = fixture
            .0
            .get_mut(&Input::SocketOwners)
            .ok_or(Problem::Missing)?;
        *owners = owners.replace("42 544 1000", "42 545 1000");
        assert_ne!(sample(&mut fixture)?, expected);
        Ok(())
    }

    #[test]
    fn blocks_missing_cross_uid_and_conflicting_owners() -> Result<(), Problem> {
        let cases = [
            ("", Problem::Incomplete),
            ("104 50 600 42\n", Problem::Ambiguous),
            ("999 41 544 1000\n", Problem::Changed),
            ("101 40 542 1000\n", Problem::Duplicate),
            ("101 0 542 1000\n", Problem::Malformed),
            ("101 4194304 542 1000\n", Problem::Malformed),
            ("101 50 600 4294967295\n", Problem::Malformed),
        ];
        for (changes, expected) in cases {
            let mut fixture = fixture();
            let owners = fixture
                .0
                .get_mut(&Input::SocketOwners)
                .ok_or(Problem::Missing)?;
            if changes.is_empty() {
                owners.clear();
            } else {
                owners.push_str(changes);
            }
            assert_eq!(sample(&mut fixture), Err(expected));
        }

        let mut fixture = fixture();
        let owners = fixture
            .0
            .get_mut(&Input::SocketOwners)
            .ok_or(Problem::Missing)?;
        *owners = owners.replace("105 42 544 1000", "105 42 544 1001");
        assert_eq!(sample(&mut fixture), Err(Problem::Mismatch));
        Ok(())
    }

    #[test]
    fn corroborates_selected_processes_even_for_untracked_sockets() -> Result<(), Problem> {
        let mut fixture = fixture();
        fixture
            .0
            .get_mut(&Input::SocketOwners)
            .ok_or(Problem::Missing)?
            .push_str("999 43 545 1001\n");
        let mut selected = BTreeMap::from([(
            43,
            Credentials {
                uids: [1001; 4],
                gids: [1001; 4],
                groups: BTreeSet::new(),
                start: 545,
            },
        )]);
        let desktops = BTreeSet::from([1000]);
        assert!(collect(&mut fixture, &desktops, &selected).is_ok());
        selected.get_mut(&43).ok_or(Problem::Missing)?.start = 544;
        assert_eq!(
            collect(&mut fixture, &desktops, &selected),
            Err(Problem::Changed)
        );
        let credentials = selected.get_mut(&43).ok_or(Problem::Missing)?;
        credentials.start = 545;
        credentials.uids[3] = 1000;
        assert_eq!(
            collect(&mut fixture, &desktops, &selected),
            Err(Problem::Mismatch)
        );
        Ok(())
    }

    #[test]
    fn blocks_missing_truncated_or_malformed_tables_and_duplicate_inodes() {
        let cases = [
            (Input::NetTcp6, None, Problem::Missing),
            (
                Input::NetTcp6,
                Some(format!("{TCP6_HEADER}\n{TCP6}")),
                Problem::Incomplete,
            ),
            (
                Input::NetTcp6,
                Some(format!("{TCP6_HEADER}\n{TCP4}\n")),
                Problem::Malformed,
            ),
            (
                Input::NetTcp6,
                Some(format!("{TCP6_HEADER}\n{}\n", TCP6.replace("0A", "4A"))),
                Problem::Unsupported,
            ),
            (
                Input::NetUnix,
                Some(format!("{UNIX_HEADER}\n{FILESYSTEM}\n{FILESYSTEM}\n")),
                Problem::Duplicate,
            ),
            (
                Input::NetUnix,
                Some(format!(
                    "{UNIX_HEADER}\n{}\n{ABSTRACT}\n",
                    FILESYSTEM.replace("/tmp/a b ", "relative")
                )),
                Problem::Unsupported,
            ),
        ];
        for (input, replacement, expected) in cases {
            let mut fixture = fixture();
            if let Some(replacement) = replacement {
                fixture.0.insert(input, replacement);
            } else {
                fixture.0.remove(&input);
            }
            assert_eq!(sample(&mut fixture), Err(expected));
        }
    }

    #[test]
    fn bounds_input_bytes_rows_and_propagates_read_errors() -> Result<(), Problem> {
        let mut fixture = fixture();
        let unix = fixture.0.get_mut(&Input::NetUnix).ok_or(Problem::Missing)?;
        *unix = "X".repeat(TABLE_BYTES + 1);
        assert_eq!(sample(&mut fixture), Err(Problem::Limit));
        let unix = fixture.0.get_mut(&Input::NetUnix).ok_or(Problem::Missing)?;
        let row = format!("{}\n", UNNAMED.replace("103", "0"));
        *unix = format!("{UNIX_HEADER}\n{}", row.repeat(TABLE_ROWS + 1));
        assert_eq!(sample(&mut fixture), Err(Problem::Limit));

        struct Unreadable;

        impl Read for Unreadable {
            fn read(&mut self, _buf: &mut [u8]) -> io::Result<usize> {
                Err(io::Error::other("fixture failure"))
            }
        }

        impl Source for Unreadable {
            fn open(&mut self, _: Input) -> Result<Box<dyn Read + '_>, Problem> {
                Ok(Box::new(Unreadable))
            }
        }

        assert_eq!(sample(&mut Unreadable), Err(Problem::Read));
        Ok(())
    }
}
