use std::collections::VecDeque;
use std::io::{Cursor, Read};

use super::*;

const PASSWD: &str = "agent:x:42:53:Agent:/home/agent:/bin/bash\ndesktop:x:43:54::/home/desktop:\n";
const GROUP: &str = "agent:x:53:\ndesktop:x:54:\nwrit-agents:x:55:agent\n";
const PID: u32 = 123;
const STATUS: &str = "Name:\tagent\nTgid:\t123\nPid:\t123\nUid:\t42\t43\t44\t45\nGid:\t53\t54\t55\t56\nGroups:\t53 55 \n";

struct Fixture(VecDeque<(Input, Result<Vec<u8>, Problem>)>);

impl Fixture {
    fn new(rows: impl IntoIterator<Item = (Input, impl AsRef<[u8]>)>) -> Self {
        Self(
            rows.into_iter()
                .map(|(input, bytes)| (input, Ok(bytes.as_ref().to_vec())))
                .collect(),
        )
    }
}

impl Source for Fixture {
    fn open(&mut self, input: Input) -> Result<Box<dyn Read + '_>, Problem> {
        let (expected, result) = self.0.pop_front().ok_or(Problem::Missing)?;
        assert_eq!(input, expected);
        Ok(Box::new(Cursor::new(result?)))
    }
}

fn accounts(passwd: &str, groups: &str) -> Result<Accounts, Problem> {
    read_accounts(&mut Fixture::new([
        (Input::Passwd, passwd),
        (Input::Group, groups),
    ]))
}

fn stat(pid: u32, comm: &str, start: u64) -> String {
    format!(
        "{pid} ({comm}) S {} {start} {}\n",
        ["-1"; 18].join(" "),
        ["0"; 30].join(" ")
    )
}

fn process(before: &str, status: &str, after: &str) -> Result<Credentials, Problem> {
    read_process(
        &mut Fixture::new([
            (Input::ProcStat(PID), before),
            (Input::ProcStatus(PID), status),
            (Input::ProcStat(PID), after),
        ]),
        PID,
    )
}

#[test]
#[allow(clippy::unwrap_used)]
fn accounts_preserve_homes_primary_groups_and_listed_members() {
    let result = accounts(PASSWD, GROUP).unwrap();
    assert_eq!(
        result.accounts[&42],
        Account {
            name: "agent".into(),
            primary: 53,
            home: "/home/agent".into(),
        }
    );
    assert_eq!(result.accounts[&43].primary, 54);
    assert_eq!(result.accounts[&43].home, "/home/desktop");
    assert_eq!(result.groups[&53].listed, BTreeSet::new());
    assert_eq!(
        result.groups[&55],
        Group {
            name: "writ-agents".into(),
            listed: BTreeSet::from([42]),
        }
    );
}

#[test]
fn rejects_duplicate_accounts_groups_and_members() {
    for (passwd, groups) in [
        (format!("{PASSWD}agent:x:44:53::/:\n"), GROUP.into()),
        (format!("{PASSWD}other:x:42:53::/:\n"), GROUP.into()),
        (PASSWD.into(), format!("{GROUP}agent:x:56:\n")),
        (PASSWD.into(), format!("{GROUP}alias:x:55:\n")),
        (PASSWD.into(), GROUP.replace("55:agent", "55:agent,agent")),
    ] {
        assert_eq!(accounts(&passwd, &groups), Err(Problem::Duplicate));
    }
}

#[test]
fn rejects_unknown_references_and_bad_database_framing() {
    for (passwd, groups, problem) in [
        (
            PASSWD.into(),
            GROUP.replace("55:agent", "55:missing"),
            Problem::Mismatch,
        ),
        (
            PASSWD.replace("42:53", "42:56"),
            GROUP.into(),
            Problem::Mismatch,
        ),
        (
            PASSWD.into(),
            GROUP.replace("55:agent", "55:agent,"),
            Problem::Malformed,
        ),
        (
            PASSWD.replace("42:53", "+42:53"),
            GROUP.into(),
            Problem::Malformed,
        ),
        (
            PASSWD.replace("/bin/bash", "/bin/bash:extra"),
            GROUP.into(),
            Problem::Malformed,
        ),
        (PASSWD.into(), GROUP.replace(':', "\r:"), Problem::Malformed),
        (PASSWD.into(), format!("{GROUP}+\n"), Problem::Unsupported),
        (PASSWD.trim_end().into(), GROUP.into(), Problem::Incomplete),
        (PASSWD.into(), String::new(), Problem::Incomplete),
    ] {
        assert_eq!(accounts(&passwd, &groups), Err(problem));
    }
}

#[test]
fn propagates_source_failures_and_applies_database_limits() {
    for problem in [Problem::Missing, Problem::Incomplete, Problem::Read] {
        let mut source = Fixture(VecDeque::from([(Input::Passwd, Err(problem))]));
        assert_eq!(read_accounts(&mut source), Err(problem));
    }
    let mut source = Fixture::new([(Input::Passwd, [0xff, b'\n'])]);
    assert_eq!(read_accounts(&mut source), Err(Problem::Unsupported));
    assert_eq!(
        accounts(&"\n".repeat(ACCOUNT_ROWS + 1), GROUP),
        Err(Problem::Limit)
    );
    let oversized = format!("{}\n", "a".repeat(ACCOUNT_BYTES));
    assert_eq!(accounts(PASSWD, &oversized), Err(Problem::Limit));
}

#[test]
fn captures_all_credential_slots_between_matching_process_identities() {
    let before = stat(PID, "agent) role\nname", 200);
    let after = stat(PID, "agent", 200);
    assert_eq!(
        process(&before, STATUS, &after),
        Ok(Credentials {
            uids: [42, 43, 44, 45],
            gids: [53, 54, 55, 56],
            groups: BTreeSet::from([53, 55]),
            start: 200,
        })
    );
    let empty = STATUS.replace("53 55 ", "");
    assert!(matches!(process(&before, &empty, &after), Ok(creds) if creds.groups.is_empty()));
}

#[test]
fn rejects_pid_reuse_and_status_identity_mismatches() {
    let row = stat(PID, "agent", 200);
    assert_eq!(
        process(&row, STATUS, &stat(PID, "agent", 201)),
        Err(Problem::Changed)
    );
    let other = stat(PID + 1, "agent", 200);
    assert_eq!(process(&row, STATUS, &other), Err(Problem::Mismatch));
    assert_eq!(process(&other, STATUS, &row), Err(Problem::Mismatch));
    for field in ["Pid", "Tgid"] {
        let status = STATUS.replace(&format!("{field}:\t123"), &format!("{field}:\t124"));
        assert_eq!(process(&row, &status, &row), Err(Problem::Mismatch));
    }
}

#[test]
fn rejects_partial_malformed_and_unsupported_process_rows() {
    let row = stat(PID, "agent", 200);
    for (status, problem) in [
        (STATUS.trim_end().into(), Problem::Incomplete),
        (STATUS.replace("Groups:\t53 55 \n", ""), Problem::Incomplete),
        (
            format!("{STATUS}Uid:\t42\t43\t44\t45\n"),
            Problem::Duplicate,
        ),
        (STATUS.replace("53 55", "53 53"), Problem::Duplicate),
        (
            STATUS.replace("42\t43\t44\t45", "42\t43\t44"),
            Problem::Malformed,
        ),
        (
            STATUS.replace("42\t43\t44\t45", "42\t43\t44\t45\t46"),
            Problem::Malformed,
        ),
        (STATUS.replace("53 55", "53 +55"), Problem::Malformed),
        (STATUS.replace("53 55", "53 4294967295"), Problem::Malformed),
    ] {
        assert_eq!(process(&row, &status, &row), Err(problem));
    }
    let short = format!("{}\n", row.trim_end().trim_end_matches(" 0"));
    assert_eq!(process(&short, STATUS, &row), Err(Problem::Incomplete));
    let extra = format!("{} 0\n", row.trim_end());
    assert_eq!(process(&row, STATUS, &extra), Err(Problem::Unsupported));
    assert_eq!(
        process(&format!("{row}{row}"), STATUS, &row),
        Err(Problem::Malformed)
    );
    assert_eq!(
        process(&row.replace(" 200 ", " -200 "), STATUS, &row),
        Err(Problem::Malformed)
    );
}

#[test]
fn limits_process_rows_and_supplementary_group_count() {
    let row = stat(PID, "agent", 200);
    let values = (0..=SUPPLEMENTARY_GROUPS)
        .map(|gid| gid.to_string())
        .collect::<Vec<_>>()
        .join(" ");
    let status = STATUS.replace("53 55", &values);
    assert_eq!(process(&row, &status, &row), Err(Problem::Limit));
    let oversized = format!("{}\n", "0".repeat(STAT_BYTES));
    assert_eq!(process(&oversized, STATUS, &row), Err(Problem::Limit));
    let oversized = format!("{}\n", "0".repeat(STATUS_BYTES));
    assert_eq!(process(&row, &oversized, &row), Err(Problem::Limit));
}
