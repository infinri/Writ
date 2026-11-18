//! Read-only desktop identity and socket inventory.

#[cfg(target_os = "linux")]
mod guest;
mod identity;
mod inventory;
#[cfg(test)]
mod tests;

use std::collections::{BTreeMap, BTreeSet};
use std::io::Read;

use identity::{Accounts, Credentials};
use inventory::{Inventory, Kind};

#[derive(Clone, Copy, Debug, Eq, Ord, PartialEq, PartialOrd)]
enum Input {
    Scope,
    Passwd,
    Group,
    #[cfg(test)]
    ProcStat(u32),
    #[cfg(test)]
    ProcStatus(u32),
    #[cfg(test)]
    NetUnix,
    #[cfg(test)]
    NetTcp,
    #[cfg(test)]
    NetTcp6,
    #[cfg(test)]
    SocketOwners,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
enum Problem {
    Missing,
    Unsupported,
    Incomplete,
    Read,
    Limit,
    Malformed,
    Duplicate,
    Ambiguous,
    Changed,
    Mismatch,
}

// EOF denotes a complete input. A source that cannot establish completeness must
// return Incomplete (or a read error). All inputs belong to the same guest scope.
trait Source {
    fn open(&mut self, input: Input) -> Result<Box<dyn Read + '_>, Problem>;
}

fn valid_pid(pid: u32) -> bool {
    (1..(1 << 22)).contains(&pid)
}

fn read_text(source: &mut impl Source, input: Input, limit: usize) -> Result<String, Problem> {
    let mut reader = source.open(input)?;
    let mut bytes = Vec::new();
    let mut buffer = [0; 4096];
    loop {
        let len = buffer.len().min(limit + 1 - bytes.len());
        let n = reader.read(&mut buffer[..len]).map_err(|_| Problem::Read)?;
        if n == 0 {
            break;
        }
        if n > len || bytes.len() + n > limit {
            return Err(Problem::Limit);
        }
        bytes.extend_from_slice(&buffer[..n]);
    }
    if !bytes.is_empty() && !bytes.ends_with(b"\n") {
        return Err(Problem::Incomplete);
    }
    let text = String::from_utf8(bytes).map_err(|_| Problem::Unsupported)?;
    if text.contains('\0') {
        return Err(Problem::Malformed);
    }
    Ok(text)
}

struct Expected {
    service_uid: u32,
    job_uid: u32,
    agents: BTreeMap<u32, BTreeSet<u32>>,
    desktops: BTreeSet<u32>,
}

#[derive(Debug)]
struct Observed {
    accounts: Accounts,
    processes: BTreeMap<u32, Credentials>,
    inventory: Inventory,
    missing: BTreeSet<(u32, Kind)>,
}

#[derive(Debug, PartialEq, Eq)]
struct Scope {
    boot_id: String,
    pid_namespace: u64,
    user_namespace: u64,
    net_namespace: u64,
}

fn valid_boot_id(boot_id: &str) -> bool {
    boot_id.len() == 36
        && boot_id.bytes().enumerate().all(|(i, byte)| match i {
            8 | 13 | 18 | 23 => byte == b'-',
            _ => byte.is_ascii_digit() || (b'a'..=b'f').contains(&byte),
        })
        && !boot_id.bytes().all(|byte| matches!(byte, b'0' | b'-'))
}

fn read_scope(source: &mut impl Source) -> Result<Scope, Problem> {
    let text = read_text(source, Input::Scope, 128)?;
    let mut fields = text.split_ascii_whitespace();
    let boot_id = fields.next().ok_or(Problem::Malformed)?;
    if !valid_boot_id(boot_id) {
        return Err(Problem::Malformed);
    }
    let mut namespace = || {
        let field = fields.next().ok_or(Problem::Malformed)?;
        if !field.bytes().all(|byte| byte.is_ascii_digit()) {
            return Err(Problem::Malformed);
        }
        field
            .parse::<u64>()
            .ok()
            .filter(|&id| id != 0)
            .ok_or(Problem::Malformed)
    };
    let scope = Scope {
        boot_id: boot_id.to_owned(),
        pid_namespace: namespace()?,
        user_namespace: namespace()?,
        net_namespace: namespace()?,
    };
    if fields.next().is_some() || text.lines().count() != 1 {
        return Err(Problem::Malformed);
    }
    Ok(scope)
}

fn configured_processes(expected: &Expected) -> Result<BTreeSet<u32>, Problem> {
    const MAX_ROLE_ACCOUNTS: usize = 128;
    const MAX_PROCESSES: usize = 256;

    if expected.agents.is_empty() || expected.desktops.is_empty() {
        return Err(Problem::Missing);
    }
    if expected.agents.len() > MAX_ROLE_ACCOUNTS || expected.desktops.len() > MAX_ROLE_ACCOUNTS {
        return Err(Problem::Limit);
    }
    let mut uids = BTreeSet::new();
    for &uid in [expected.service_uid, expected.job_uid]
        .iter()
        .chain(expected.agents.keys())
        .chain(&expected.desktops)
    {
        if uid == 0 || uid == u32::MAX {
            return Err(Problem::Malformed);
        }
        if !uids.insert(uid) {
            return Err(Problem::Duplicate);
        }
    }
    let mut pids = BTreeSet::new();
    for selected in expected.agents.values() {
        if selected.is_empty() {
            return Err(Problem::Missing);
        }
        if selected.len() > MAX_PROCESSES - pids.len() {
            return Err(Problem::Limit);
        }
        for &pid in selected {
            if !valid_pid(pid) {
                return Err(Problem::Malformed);
            }
            if !pids.insert(pid) {
                return Err(Problem::Duplicate);
            }
        }
    }
    Ok(pids)
}

#[cfg(test)]
fn snapshot(
    source: &mut impl Source,
    pids: &BTreeSet<u32>,
    desktops: &BTreeSet<u32>,
) -> Result<(Scope, Observed), Problem> {
    let scope = read_scope(source)?;
    let accounts = identity::read_accounts(source)?;
    let processes = pids
        .iter()
        .map(|&pid| Ok((pid, identity::read_process(source, pid)?)))
        .collect::<Result<_, Problem>>()?;
    let inventory = inventory::collect(source, desktops, &processes)?;
    if scope != read_scope(source)? {
        return Err(Problem::Changed);
    }
    Ok((
        scope,
        Observed {
            accounts,
            processes,
            inventory,
            missing: BTreeSet::new(),
        },
    ))
}

fn check_identities(expected: &Expected, observed: &Observed) -> Result<(), Problem> {
    let accounts = &observed.accounts.accounts;
    let groups = &observed.accounts.groups;
    let group_id = |name| {
        groups
            .iter()
            .find_map(|(&gid, group)| (group.name == name).then_some(gid))
            .ok_or(Problem::Missing)
    };
    let service_gid = group_id("writ")?;
    let job_gid = group_id("writ-job")?;
    let agents_gid = group_id("writ-agents")?;
    let service = accounts
        .get(&expected.service_uid)
        .ok_or(Problem::Missing)?;
    let job = accounts.get(&expected.job_uid).ok_or(Problem::Missing)?;
    if service.name != "writ"
        || job.name != "writ-job"
        || service.primary != service_gid
        || job.primary != job_gid
        || !groups[&service_gid].listed.is_empty()
        || !groups[&job_gid].listed.is_empty()
        || !groups[&agents_gid].listed.iter().eq(expected.agents.keys())
        || agents_gid == 0
        || accounts.values().any(|a| a.primary == agents_gid)
    {
        return Err(Problem::Mismatch);
    }

    let role_uids = [expected.service_uid, expected.job_uid]
        .into_iter()
        .chain(expected.desktops.iter().copied())
        .chain(expected.agents.keys().copied());
    for uid in role_uids {
        let primary = accounts.get(&uid).ok_or(Problem::Missing)?.primary;
        if primary == 0
            || accounts
                .iter()
                .any(|(&other, a)| other != uid && a.primary == primary)
            || groups[&primary].listed.iter().any(|&other| other != uid)
        {
            return Err(Problem::Mismatch);
        }
        let pids = expected.agents.get(&uid);
        if groups.iter().any(|(&gid, group)| {
            group.listed.contains(&uid) && gid != primary && !(pids.is_some() && gid == agents_gid)
        }) {
            return Err(Problem::Mismatch);
        }
        if let Some(pids) = pids {
            for pid in pids {
                let creds = &observed.processes[pid];
                if creds.uids != [uid; 4]
                    || creds.gids != [primary; 4]
                    || !creds.groups.contains(&agents_gid)
                    || creds
                        .groups
                        .iter()
                        .any(|&gid| gid != primary && gid != agents_gid)
                {
                    return Err(Problem::Mismatch);
                }
            }
        }
    }
    Ok(())
}

#[cfg(test)]
fn collect(source: &mut impl Source, expected: &Expected) -> Result<Observed, Problem> {
    let pids = configured_processes(expected)?;
    let (before_scope, before) = snapshot(source, &pids, &expected.desktops)?;
    let after = snapshot(source, &pids, &expected.desktops)?;
    reconcile(expected, (before_scope, before), after)
}

fn reconcile(
    expected: &Expected,
    (before_scope, before): (Scope, Observed),
    (after_scope, mut after): (Scope, Observed),
) -> Result<Observed, Problem> {
    if before_scope != after_scope
        || before.accounts != after.accounts
        || before.processes != after.processes
        || before.inventory != after.inventory
    {
        return Err(Problem::Changed);
    }
    check_identities(expected, &after)?;
    for &uid in &expected.desktops {
        for kind in [Kind::Tcp, Kind::Unix, Kind::Abstract] {
            if after
                .inventory
                .get(&(uid, kind))
                .is_none_or(BTreeSet::is_empty)
            {
                after.missing.insert((uid, kind));
            }
        }
    }
    Ok(after)
}

/// Collect read-only desktop inventory in the owner-designated reference guest.
/// The arguments are `VM BOOT_ID SERVICE_UID JOB_UID AGENT_UIDS DESKTOP_UIDS`,
/// with comma-separated UID lists. Output is inventory evidence only.
#[cfg(target_os = "linux")]
pub fn run_guest_inventory(
    arguments: impl IntoIterator<Item = std::ffi::OsString>,
) -> Result<String, String> {
    guest::run(arguments).map_err(|reason| format!("desktop inventory unavailable: {reason}"))
}
