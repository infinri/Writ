use std::collections::{BTreeMap, BTreeSet};

#[cfg(test)]
use super::valid_pid;
use super::{read_text, Input, Problem, Source};

const ACCOUNT_BYTES: usize = 1024 * 1024;
const ACCOUNT_ROWS: usize = 4096;
#[cfg(test)]
const STAT_BYTES: usize = 4096;
#[cfg(test)]
const STATUS_BYTES: usize = 1024 * 1024;
const SUPPLEMENTARY_GROUPS: usize = 65_536;

#[derive(Debug, Eq, PartialEq)]
pub(super) struct Accounts {
    pub(super) accounts: BTreeMap<u32, Account>,
    pub(super) groups: BTreeMap<u32, Group>,
}

#[derive(Debug, Eq, PartialEq)]
pub(super) struct Account {
    pub(super) name: String,
    pub(super) primary: u32,
    pub(super) home: String,
}

#[derive(Debug, Eq, PartialEq)]
pub(super) struct Group {
    pub(super) name: String,
    pub(super) listed: BTreeSet<u32>,
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub(super) struct Credentials {
    pub(super) uids: [u32; 4],
    pub(super) gids: [u32; 4],
    pub(super) groups: BTreeSet<u32>,
    pub(super) start: u64,
}

fn decimal(text: &str) -> bool {
    !text.is_empty() && text.bytes().all(|byte| byte.is_ascii_digit())
}

fn id(text: &str) -> Result<u32, Problem> {
    if !decimal(text) {
        return Err(Problem::Malformed);
    }
    let id = text.parse().map_err(|_| Problem::Malformed)?;
    if id == u32::MAX {
        return Err(Problem::Malformed);
    }
    Ok(id)
}

fn name(text: &str) -> Result<(), Problem> {
    if text.starts_with(['+', '-']) {
        return Err(Problem::Unsupported);
    }
    if text.is_empty()
        || text.starts_with('#')
        || text.contains(',')
        || text.chars().any(|c| c.is_control() || c.is_whitespace())
    {
        return Err(Problem::Malformed);
    }
    Ok(())
}

fn database_rows(text: &str) -> Result<impl Iterator<Item = &str>, Problem> {
    if text.bytes().filter(|&byte| byte == b'\n').count() > ACCOUNT_ROWS {
        return Err(Problem::Limit);
    }
    if text.chars().any(|c| c.is_control() && c != '\n') {
        return Err(Problem::Malformed);
    }
    Ok(text.split_terminator('\n').filter(|row| !row.is_empty()))
}

pub(super) fn read_accounts(source: &mut impl Source) -> Result<Accounts, Problem> {
    let passwd = read_text(source, Input::Passwd, ACCOUNT_BYTES)?;
    let mut accounts = BTreeMap::new();
    let mut users = BTreeMap::new();
    for row in database_rows(&passwd)? {
        if row.starts_with(['+', '-']) {
            return Err(Problem::Unsupported);
        }
        let mut fields = row.split(':');
        let [Some(user), Some(_), Some(uid), Some(gid), Some(_), Some(home), Some(_), None] =
            std::array::from_fn(|_| fields.next())
        else {
            return Err(Problem::Malformed);
        };
        name(user)?;
        let uid = id(uid)?;
        let account = Account {
            name: user.into(),
            primary: id(gid)?,
            home: home.into(),
        };
        if users.insert(user, uid).is_some() || accounts.insert(uid, account).is_some() {
            return Err(Problem::Duplicate);
        }
    }

    let group = read_text(source, Input::Group, ACCOUNT_BYTES)?;
    let mut groups = BTreeMap::new();
    let mut names = BTreeSet::new();
    for row in database_rows(&group)? {
        if row.starts_with(['+', '-']) {
            return Err(Problem::Unsupported);
        }
        let mut fields = row.split(':');
        let [Some(group), Some(_), Some(gid), Some(members), None] =
            std::array::from_fn(|_| fields.next())
        else {
            return Err(Problem::Malformed);
        };
        name(group)?;
        let gid = id(gid)?;
        let mut listed = BTreeSet::new();
        if !members.is_empty() {
            for member in members.split(',') {
                name(member)?;
                let uid = users.get(member).ok_or(Problem::Mismatch)?;
                if !listed.insert(*uid) {
                    return Err(Problem::Duplicate);
                }
            }
        }
        let group_row = Group {
            name: group.into(),
            listed,
        };
        if !names.insert(group) || groups.insert(gid, group_row).is_some() {
            return Err(Problem::Duplicate);
        }
    }

    if accounts.is_empty() || groups.is_empty() {
        return Err(Problem::Incomplete);
    }
    if accounts
        .values()
        .any(|account| !groups.contains_key(&account.primary))
    {
        return Err(Problem::Mismatch);
    }
    Ok(Accounts { accounts, groups })
}

pub(super) fn stat_start(text: &str, pid: u32) -> Result<u64, Problem> {
    let row = text.strip_suffix('\n').ok_or(Problem::Incomplete)?;
    let (reported_pid, tail) = row.split_once(" (").ok_or(Problem::Malformed)?;
    if id(reported_pid)? != pid {
        return Err(Problem::Mismatch);
    }
    let (comm, fields) = tail.rsplit_once(") ").ok_or(Problem::Malformed)?;
    if comm.len() > 63 {
        return Err(Problem::Malformed);
    }
    let mut fields = fields.split(' ');
    if !matches!(
        fields.next(),
        Some("R" | "S" | "D" | "Z" | "T" | "t" | "X" | "x" | "K" | "W" | "P" | "I")
    ) {
        return Err(Problem::Malformed);
    }

    let mut start = None;
    for field in 4..=52 {
        let value = fields.next().ok_or(Problem::Incomplete)?;
        if field == 22 {
            if !decimal(value) {
                return Err(Problem::Malformed);
            }
            start = Some(value.parse().map_err(|_| Problem::Malformed)?);
        } else if let Some(digits) = value.strip_prefix('-') {
            if !decimal(digits) || value.parse::<i64>().is_err() {
                return Err(Problem::Malformed);
            }
        } else if !decimal(value) || value.parse::<u64>().is_err() {
            return Err(Problem::Malformed);
        }
    }
    if fields.next().is_some() {
        return Err(Problem::Unsupported);
    }
    start.ok_or(Problem::Incomplete)
}

fn four_ids(text: &str) -> Result<[u32; 4], Problem> {
    let mut fields = text.split_ascii_whitespace();
    let mut ids = [0; 4];
    for value in &mut ids {
        *value = id(fields.next().ok_or(Problem::Malformed)?)?;
    }
    if fields.next().is_some() {
        return Err(Problem::Malformed);
    }
    Ok(ids)
}

fn supplementary(text: &str) -> Result<BTreeSet<u32>, Problem> {
    let mut groups = BTreeSet::new();
    for (index, value) in text.split_ascii_whitespace().enumerate() {
        if index >= SUPPLEMENTARY_GROUPS {
            return Err(Problem::Limit);
        }
        if !groups.insert(id(value)?) {
            return Err(Problem::Duplicate);
        }
    }
    Ok(groups)
}

pub(super) fn status_credentials(
    text: &str,
    pid: u32,
    tgid: u32,
    start: u64,
) -> Result<Credentials, Problem> {
    let mut fields = BTreeMap::new();
    for row in text.split_terminator('\n') {
        let (field, value) = row.split_once(':').ok_or(Problem::Malformed)?;
        if field.is_empty()
            || !field
                .bytes()
                .all(|byte| byte.is_ascii_alphanumeric() || byte == b'_')
            || value.chars().any(|c| c.is_control() && c != '\t')
        {
            return Err(Problem::Malformed);
        }
        if matches!(field, "Pid" | "Tgid" | "Uid" | "Gid" | "Groups")
            && fields
                .insert(field, value.trim_matches([' ', '\t']))
                .is_some()
        {
            return Err(Problem::Duplicate);
        }
    }

    let value = |field| fields.get(field).copied().ok_or(Problem::Incomplete);
    if id(value("Pid")?)? != pid || id(value("Tgid")?)? != tgid {
        return Err(Problem::Mismatch);
    }
    Ok(Credentials {
        uids: four_ids(value("Uid")?)?,
        gids: four_ids(value("Gid")?)?,
        groups: supplementary(value("Groups")?)?,
        start,
    })
}

#[cfg(test)]
pub(super) fn read_process(source: &mut impl Source, pid: u32) -> Result<Credentials, Problem> {
    if !valid_pid(pid) {
        return Err(Problem::Malformed);
    }
    let stat = read_text(source, Input::ProcStat(pid), STAT_BYTES)?;
    let start = stat_start(&stat, pid)?;
    let status = read_text(source, Input::ProcStatus(pid), STATUS_BYTES)?;
    let credentials = status_credentials(&status, pid, pid, start)?;
    let stat = read_text(source, Input::ProcStat(pid), STAT_BYTES)?;
    if stat_start(&stat, pid)? != start {
        return Err(Problem::Changed);
    }
    Ok(credentials)
}

#[cfg(test)]
mod tests;
