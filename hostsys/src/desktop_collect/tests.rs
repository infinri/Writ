use super::*;

use std::io::{self, Cursor};

const SCOPE: &str = "11bf3505-57f1-4c24-882b-4e884bab6e20 101 102 103\n";
const PASSWD: &str = "writ:x:20:20::/var/lib/writ:/usr/sbin/nologin\n\
    writ-job:x:21:21::/nonexistent:/usr/sbin/nologin\n\
    agent:x:22:22::/home/agent:/bin/bash\n\
    human:x:1000:1000::/home/human:/bin/bash\n";
const GROUP: &str = "writ:x:20:\nwrit-job:x:21:\nagent:x:22:\n\
    writ-agents:x:23:agent\nhuman:x:1000:\n";
const TCP_HEADER: &str = "  sl  local_address rem_address   st tx_queue rx_queue tr tm->when retrnsmt   uid  timeout inode\n";
const TCP6_HEADER: &str =
    "sl local_address remote_address st tx_queue rx_queue tr tm->when retrnsmt uid timeout inode\n";
const UNIX: &str = "Num       RefCount Protocol Flags    Type St Inode Path\n\
    0000000000000000: 00000002 00000000 00010000 0001 01 50 /run/user/1000/bus\n\
    0000000000000000: 00000002 00000000 00010000 0001 01 51 @desktop\n";
const OWNERS: &str = "50 77 300 1000\n51 77 300 1000\n";
type Frame = BTreeMap<Input, String>;

struct Fixture {
    before: Frame,
    after: Frame,
    scope_reads: usize,
}

impl Source for Fixture {
    fn open(&mut self, input: Input) -> Result<Box<dyn Read + '_>, Problem> {
        if input == Input::Scope {
            self.scope_reads += 1;
        }
        let frame = if self.scope_reads <= 2 {
            &self.before
        } else {
            &self.after
        };
        let bytes = frame.get(&input).ok_or(Problem::Missing)?.as_bytes();
        Ok(Box::new(Cursor::new(bytes)))
    }
}

fn stat(start: u64) -> String {
    format!(
        "42 (agent) S {} {start} {}\n",
        ["0"; 18].join(" "),
        ["0"; 30].join(" ")
    )
}

fn fixture() -> (Fixture, Expected) {
    let before = BTreeMap::from([
        (Input::Scope, SCOPE.into()),
        (Input::Passwd, PASSWD.into()),
        (Input::Group, GROUP.into()),
        (Input::ProcStat(42), stat(150)),
        (
            Input::ProcStatus(42),
            "Tgid:\t42\nPid:\t42\nUid:\t22\t22\t22\t22\nGid:\t22\t22\t22\t22\nGroups:\t22 23\n"
                .into(),
        ),
        (Input::NetUnix, UNIX.into()),
        (Input::NetTcp, TCP_HEADER.into()),
        (Input::NetTcp6, TCP6_HEADER.into()),
        (Input::SocketOwners, OWNERS.into()),
    ]);
    (
        Fixture {
            after: before.clone(),
            before,
            scope_reads: 0,
        },
        Expected {
            service_uid: 20,
            job_uid: 21,
            agents: BTreeMap::from([(22, BTreeSet::from([42]))]),
            desktops: BTreeSet::from([1000]),
        },
    )
}

#[test]
fn reconciles_observed_facts_and_reports_absent_target_classes() -> Result<(), Problem> {
    let (mut source, expected) = fixture();
    let observed = collect(&mut source, &expected)?;
    assert_eq!(observed.accounts.accounts[&1000].home, "/home/human");
    assert_eq!(observed.processes[&42].start, 150);
    assert_eq!(observed.inventory[&(1000, Kind::Unix)].len(), 1);
    assert_eq!(observed.inventory[&(1000, Kind::Abstract)].len(), 1);
    assert_eq!(observed.missing, BTreeSet::from([(1000, Kind::Tcp)]));
    Ok(())
}

#[test]
fn changing_scope_credentials_or_inventory_is_unavailable() {
    for (input, value) in [
        (Input::Scope, SCOPE.replace(" 103", " 104")),
        (Input::ProcStat(42), stat(151)),
        (Input::Passwd, PASSWD.replace("/home/human", "/srv/human")),
        (Input::NetUnix, UNIX.replace("@desktop", "@new")),
        (Input::SocketOwners, OWNERS.replace("77 300", "77 301")),
    ] {
        let (mut source, expected) = fixture();
        source.after.insert(input, value);
        assert_eq!(
            collect(&mut source, &expected).err(),
            Some(Problem::Changed)
        );
    }
}

#[test]
fn expected_groups_do_not_substitute_for_observed_process_credentials() {
    let (mut source, expected) = fixture();
    for frame in [&mut source.before, &mut source.after] {
        frame.entry(Input::ProcStatus(42)).and_modify(|text| {
            *text = text.replace("22 23", "22 23 1000");
        });
    }
    assert_eq!(
        collect(&mut source, &expected).err(),
        Some(Problem::Mismatch)
    );
}

#[test]
fn selected_pids_use_the_same_linux_bounds_as_observed_holders() {
    let (mut source, mut expected) = fixture();
    expected.agents.insert(22, BTreeSet::from([1 << 22]));
    assert_eq!(
        collect(&mut source, &expected).err(),
        Some(Problem::Malformed)
    );
    assert_eq!(source.scope_reads, 0);
    assert!(valid_pid((1 << 22) - 1));
}

#[test]
fn primary_group_membership_and_absent_processes_are_checked() {
    let (mut source, expected) = fixture();
    for frame in [&mut source.before, &mut source.after] {
        frame.entry(Input::Passwd).and_modify(|text| {
            text.push_str("other:x:1001:1000::/home/other:/bin/bash\n");
        });
    }
    assert_eq!(
        collect(&mut source, &expected).err(),
        Some(Problem::Mismatch)
    );

    let (mut source, expected) = fixture();
    source.after.remove(&Input::ProcStatus(42));
    assert_eq!(
        collect(&mut source, &expected).err(),
        Some(Problem::Missing)
    );
}

#[test]
fn shared_reader_rejects_truncation_limits_and_errors_after_a_complete_line() {
    struct Partial(bool);

    impl Read for Partial {
        fn read(&mut self, buffer: &mut [u8]) -> io::Result<usize> {
            if !self.0 {
                self.0 = true;
                return (&b"line\n"[..]).read(buffer);
            }
            Err(io::ErrorKind::Interrupted.into())
        }
    }

    struct Bytes(&'static [u8]);

    impl Source for Bytes {
        fn open(&mut self, _: Input) -> Result<Box<dyn Read + '_>, Problem> {
            Ok(Box::new(self.0))
        }
    }

    impl Source for Partial {
        fn open(&mut self, _: Input) -> Result<Box<dyn Read + '_>, Problem> {
            Ok(Box::new(Partial(false)))
        }
    }

    assert_eq!(
        read_text(&mut Bytes(b"line"), Input::Scope, 5),
        Err(Problem::Incomplete)
    );
    assert_eq!(
        read_text(&mut Bytes(b"line\n"), Input::Scope, 4),
        Err(Problem::Limit)
    );
    assert_eq!(
        read_text(&mut Partial(false), Input::Scope, 16),
        Err(Problem::Read)
    );
}
