#![allow(clippy::unwrap_used)]

use std::fs::{self, File};
use std::os::unix::fs::symlink;
use std::path::PathBuf;
use std::sync::atomic::{AtomicU64, Ordering};

use super::*;

const SEQUENCE: u32 = 3;
const PORT_ID: u32 = 4321;

fn dump(family: Family) -> Dump {
    Dump::new(family, SEQUENCE, PORT_ID)
}

fn message(kind: u16, flags: u16, body: &[u8]) -> Vec<u8> {
    let len = NL_HEADER + body.len();
    let mut bytes = Vec::new();
    bytes.extend_from_slice(&(len as u32).to_ne_bytes());
    bytes.extend_from_slice(&kind.to_ne_bytes());
    bytes.extend_from_slice(&flags.to_ne_bytes());
    bytes.extend_from_slice(&SEQUENCE.to_ne_bytes());
    bytes.extend_from_slice(&PORT_ID.to_ne_bytes());
    bytes.extend_from_slice(body);
    bytes.resize(len.next_multiple_of(4), 0);
    bytes
}

fn attribute(kind: u16, value: &[u8]) -> Vec<u8> {
    let len = 4 + value.len();
    let mut bytes = Vec::new();
    bytes.extend_from_slice(&(len as u16).to_ne_bytes());
    bytes.extend_from_slice(&kind.to_ne_bytes());
    bytes.extend_from_slice(value);
    bytes.resize(len.next_multiple_of(4), 0);
    bytes
}

fn unix_row(inode: u32, name: &[u8]) -> Vec<u8> {
    let mut body = vec![Family::Unix as u8, 1, TCP_LISTEN, 0];
    body.extend_from_slice(&inode.to_ne_bytes());
    body.extend_from_slice(&17_u32.to_ne_bytes());
    body.extend_from_slice(&9_u32.to_ne_bytes());
    body.extend(attribute(UNIX_DIAG_NAME, name));
    body.extend(attribute(UNIX_DIAG_SHUTDOWN, &[0]));
    body.extend(attribute(UNIX_DIAG_UID, &1042_u32.to_ne_bytes()));
    body
}

#[test]
fn diagnostic_keeps_bytes_that_unix_proc_displays_identically() {
    let mut view = Diagnostic::default();
    let mut dump = dump(Family::Unix);
    let two_rows = [
        message(SOCK_DIAG_BY_FAMILY, NL_MULTI, &unix_row(14, b"\0a\0@\xff")),
        message(SOCK_DIAG_BY_FAMILY, NL_MULTI, &unix_row(15, b"\0a@@\xff")),
        message(SOCK_DIAG_BY_FAMILY, NL_MULTI, &unix_row(16, b"@a@@\xff\0")),
    ]
    .concat();
    assert_eq!(dump.parse(&two_rows, &mut view), Ok(false));
    assert_eq!(
        dump.parse(&message(NL_DONE, NL_MULTI, &[0; 4]), &mut view),
        Ok(true)
    );
    assert_eq!(view.unix[&14].name, b"\0a\0@\xff");
    assert_eq!(view.unix[&15].name, b"\0a@@\xff");
    assert_eq!(view.unix[&16].name, b"@a@@\xff\0");
    assert_eq!(view.unix[&14].cookie, [17, 9]);
    assert_eq!(view.unix[&14].uid, 1042);
}

#[test]
fn unix_diagnostic_requests_and_requires_one_exact_uid() {
    let request = request(Family::Unix, SEQUENCE, PORT_ID);
    assert_eq!(&request[28..32], &0x41_u32.to_ne_bytes());
    let mut row = unix_row(8, b"\0a");
    let mut view = Diagnostic::default();
    row.extend(attribute(UNIX_DIAG_UID, &1042_u32.to_ne_bytes()));
    assert_eq!(read_unix(&row, &mut view), Err(Problem::Duplicate));
    row.truncate(row.len() - 16);
    assert_eq!(read_unix(&row, &mut view), Err(Problem::Incomplete));
    let mut invalid = row.clone();
    invalid.extend(attribute(UNIX_DIAG_UID, &u32::MAX.to_ne_bytes()));
    assert_eq!(read_unix(&invalid, &mut view), Err(Problem::Incomplete));
    for width in [0, 2, 5, 8] {
        let mut malformed = row.clone();
        malformed.extend(attribute(UNIX_DIAG_UID, &vec![0; width]));
        assert_eq!(read_unix(&malformed, &mut view), Err(Problem::Malformed));
    }
}

#[test]
fn diagnostic_rejects_interrupted_and_untrusted_dump_frames() {
    let good = message(NL_DONE, NL_MULTI, &[0; 4]);
    let mut wrong_port = good.clone();
    wrong_port[12..16].copy_from_slice(&0_u32.to_ne_bytes());
    let mut wrong_sequence = good.clone();
    wrong_sequence[8..12].copy_from_slice(&0_u32.to_ne_bytes());
    for (frame, expected) in [
        (good[..good.len() - 1].to_vec(), Problem::Incomplete),
        (wrong_port, Problem::Mismatch),
        (wrong_sequence, Problem::Mismatch),
        (
            message(NL_DONE, NL_MULTI | NL_DUMP_INTR, &[0; 4]),
            Problem::Incomplete,
        ),
        (
            message(NL_DONE, NL_MULTI, &(-4_i32).to_ne_bytes()),
            Problem::Incomplete,
        ),
        (message(NL_DONE, NL_MULTI, &[]), Problem::Incomplete),
        (message(NL_ERROR, 0, &[]), Problem::Incomplete),
        (message(NL_OVERRUN, 0, &[]), Problem::Incomplete),
        (
            [good.as_slice(), good.as_slice()].concat(),
            Problem::Incomplete,
        ),
    ] {
        assert_eq!(
            dump(Family::Unix).parse(&frame, &mut Diagnostic::default()),
            Err(expected)
        );
    }
    let mut limit = dump(Family::Unix);
    limit.datagrams = DIAG_DATAGRAMS;
    assert_eq!(
        limit.parse(&good, &mut Diagnostic::default()),
        Err(Problem::Limit)
    );
}

#[test]
fn diagnostic_rejects_truncated_and_duplicate_names() {
    let mut view = Diagnostic::default();
    let mut row = unix_row(8, b"\0a");
    row.truncate(row.len() - 1);
    assert_eq!(read_unix(&row, &mut view), Err(Problem::Incomplete));
    let mut row = unix_row(8, b"\0a");
    row.extend(attribute(UNIX_DIAG_NAME, b"\0b"));
    assert_eq!(read_unix(&row, &mut view), Err(Problem::Duplicate));
    assert_eq!(
        read_unix(&unix_row(8, b"/a/b"), &mut view),
        Err(Problem::Malformed)
    );
    assert_eq!(
        read_unix(&unix_row(8, b"a\0b\0"), &mut view),
        Err(Problem::Malformed)
    );

    let row = unix_row(8, b"\0c");
    assert_eq!(read_unix(&row, &mut view), Ok(8));
    assert_eq!(read_unix(&row, &mut view), Err(Problem::Duplicate));
}

#[test]
fn diagnostic_tcp_checks_family_and_network_byte_order() {
    let mut v6 = vec![0; 72];
    v6[0] = Family::Tcp6 as u8;
    v6[1] = TCP_LISTEN;
    v6[4..6].copy_from_slice(&8080_u16.to_be_bytes());
    v6[8..24].copy_from_slice(&Ipv6Addr::LOCALHOST.octets());
    v6[40..44].copy_from_slice(&2_u32.to_ne_bytes());
    v6[44..48].copy_from_slice(&1_u32.to_ne_bytes());
    v6[64..68].copy_from_slice(&1001_u32.to_ne_bytes());
    v6[68..72].copy_from_slice(&31_u32.to_ne_bytes());

    let mut v4 = v6.clone();
    v4[0] = Family::Tcp4 as u8;
    v4[8..24].fill(0);
    v4[8..12].copy_from_slice(&Ipv4Addr::LOCALHOST.octets());
    v4[68..72].copy_from_slice(&32_u32.to_ne_bytes());
    let mut view = Diagnostic::default();
    assert_eq!(
        read_tcp(&v6, Family::Tcp4, &mut view),
        Err(Problem::Mismatch)
    );
    assert_eq!(
        read_tcp(&v6, Family::Tcp6, &mut view),
        Err(Problem::Incomplete)
    );
    v6.extend_from_slice(&5_u16.to_ne_bytes());
    v6.extend_from_slice(&INET_DIAG_SKV6ONLY.to_ne_bytes());
    v6.extend_from_slice(&[1, 0, 0, 0]);
    assert_eq!(read_tcp(&v6, Family::Tcp6, &mut view), Ok(()));
    assert_eq!(view.tcp[&31].ipv6_only, Some(true));
    assert_eq!(read_tcp(&v4, Family::Tcp4, &mut view), Ok(()));
    assert_eq!(view.tcp[&31].address, IpAddr::V6(Ipv6Addr::LOCALHOST));
    assert_eq!(view.tcp[&32].address, IpAddr::V4(Ipv4Addr::LOCALHOST));
    assert_eq!(view.tcp[&32].port, 8080);
    assert_eq!(view.tcp[&32].uid, 1001);
    assert_eq!(view.tcp[&32].ifindex, 2);
    assert_eq!(
        read_tcp(&v4, Family::Tcp4, &mut view),
        Err(Problem::Duplicate)
    );
}

struct Fixture(PathBuf);

impl Fixture {
    fn new() -> Self {
        static NEXT: AtomicU64 = AtomicU64::new(0);
        let path = std::env::temp_dir().join(format!(
            "writ-desktop-descriptors-{}-{}",
            std::process::id(),
            NEXT.fetch_add(1, Ordering::Relaxed)
        ));
        fs::create_dir(&path).unwrap();
        let fixture = Self(path);
        fs::create_dir(fixture.0.join("fd")).unwrap();
        fixture
    }
}

impl Drop for Fixture {
    fn drop(&mut self) {
        let _ = fs::remove_dir_all(&self.0);
    }
}

#[test]
fn descriptors_refuse_unreadable_rows_and_exhausted_budget() {
    let fixture = Fixture::new();
    let path = fixture.0.join("fd");
    symlink("socket:[123]", path.join("5")).unwrap();
    symlink("pipe:[123]", path.join("6")).unwrap();
    let task = File::open(&fixture.0).unwrap();
    let deadline = Instant::now() + Duration::from_secs(10);
    assert_eq!(
        task_descriptors(&task, &mut 2, deadline),
        Ok([(5, 123)].into())
    );
    assert_eq!(
        task_descriptors(&task, &mut 1, deadline),
        Err(Problem::Limit)
    );
    assert_eq!(
        task_descriptors(&task, &mut 2, Instant::now()),
        Err(Problem::Limit)
    );
    File::create(path.join("7")).unwrap();
    assert_eq!(
        task_descriptors(&task, &mut 4, deadline),
        Err(Problem::Read)
    );
}

#[test]
fn descriptors_refuse_a_disappearing_row_and_truncated_link() {
    let fixture = Fixture::new();
    let fd_dir = fixture.0.join("fd");
    let item = fd_dir.join("7");
    symlink("socket:[456]", &item).unwrap();
    let mut dir = Dir::new(File::open(&fd_dir).unwrap()).unwrap();
    let entry = dir
        .find_map(|entry| {
            let entry = entry.unwrap();
            (entry.file_name().to_bytes() == b"7").then_some(entry)
        })
        .unwrap();
    let fd = dir.fd().unwrap();
    assert_eq!(
        descriptor_inode(&fd, entry.file_name(), &mut [0; 8]),
        Err(Problem::Limit)
    );
    fs::remove_file(&item).unwrap();
    assert_eq!(
        descriptor_inode(&fd, entry.file_name(), &mut [0; LINK_BYTES]),
        Err(Problem::Changed)
    );
}

#[test]
fn holders_collapse_only_consistent_thread_credentials() {
    use crate::desktop_collect::guest::process::Task;
    use crate::desktop_collect::identity::Credentials;

    let fixture = Fixture::new();
    let credentials = |uid| Credentials {
        uids: [uid; 4],
        gids: [uid; 4],
        groups: BTreeSet::new(),
        start: 92,
    };
    let task = |process, uid| Task {
        process,
        directory: File::open(&fixture.0).unwrap(),
        credentials: credentials(uid),
    };
    let mut scan = Scan {
        processes: [(21, credentials(1001)), (78, credentials(1002))].into(),
        tasks: [
            (21, task(21, 1001)),
            (22, task(21, 1001)),
            (78, task(78, 1002)),
        ]
        .into(),
    };
    let view = DescriptorView(
        [
            (10, [(21, 1), (22, 1), (22, 8)].into()),
            (11, [(21, 6), (78, 5)].into()),
        ]
        .into(),
    );
    let owners = view.holders(&scan, 10).unwrap();
    assert_eq!(owners.len(), 1);
    assert_eq!(owners.first().unwrap().pid, 21);
    assert_eq!(owners.first().unwrap().start_ticks, 92);
    assert_eq!(owners.first().unwrap().uid, 1001);
    assert_eq!(
        view.holders(&scan, 11)
            .unwrap()
            .iter()
            .map(|h| h.uid)
            .collect::<BTreeSet<_>>(),
        [1001, 1002].into()
    );
    assert_eq!(view.holders(&scan, 12), Err(Problem::Incomplete));

    scan.tasks.get_mut(&22).unwrap().credentials.uids[2] = 1042;
    assert_eq!(view.holders(&scan, 10), Err(Problem::Ambiguous));
}
