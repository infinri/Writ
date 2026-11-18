mod boundary;
mod process;
pub(super) mod sockets;

use std::collections::{BTreeMap, BTreeSet};
use std::ffi::OsString;
use std::fmt::Write as _;
use std::fs::File;
use std::io::Read;
use std::time::{Duration, Instant};

use rustix::fs::{openat2, Mode, OFlags, ResolveFlags};

use super::{
    configured_processes, identity, inventory, read_scope, reconcile, Expected, Observed, Problem,
    Scope,
};

const COLLECTION_TIME: Duration = Duration::from_secs(15);

struct Request {
    name: String,
    boot_id: String,
    service_uid: u32,
    job_uid: u32,
    agents: BTreeSet<u32>,
    desktops: BTreeSet<u32>,
}

impl Request {
    fn parse(arguments: impl IntoIterator<Item = OsString>) -> Option<Self> {
        let mut args = arguments.into_iter();
        let mut next = || {
            args.next()?
                .into_string()
                .ok()
                .filter(|arg| arg.len() <= 1408)
        };
        let name = next()?;
        if name.is_empty()
            || name.len() > 63
            || !name
                .bytes()
                .all(|byte| byte.is_ascii_alphanumeric() || byte == b'-')
        {
            return None;
        }
        let boot_id = next()?;
        if !super::valid_boot_id(&boot_id) {
            return None;
        }
        let service_uid = uid(&next()?)?;
        let job_uid = uid(&next()?)?;
        let agents = uid_list(&next()?)?;
        let desktops = uid_list(&next()?)?;
        if args.next().is_some() {
            return None;
        }
        let mut all = BTreeSet::from([service_uid]);
        if ![job_uid]
            .into_iter()
            .chain(agents.iter().copied())
            .chain(desktops.iter().copied())
            .all(|id| all.insert(id))
        {
            return None;
        }
        Some(Self {
            name,
            boot_id,
            service_uid,
            job_uid,
            agents,
            desktops,
        })
    }
}

fn uid(value: &str) -> Option<u32> {
    if !value.bytes().all(|byte| byte.is_ascii_digit()) {
        return None;
    }
    value.parse().ok().filter(|&id| id != 0 && id != u32::MAX)
}

fn uid_list(value: &str) -> Option<BTreeSet<u32>> {
    let mut ids = BTreeSet::new();
    for value in value.split(',') {
        if ids.len() == 128 || !ids.insert(uid(value)?) {
            return None;
        }
    }
    Some(ids)
}

pub(super) fn run(arguments: impl IntoIterator<Item = OsString>) -> Result<String, String> {
    let request = Request::parse(arguments)
        .ok_or("expected VM BOOT_ID SERVICE_UID JOB_UID AGENT_UIDS DESKTOP_UIDS")?;
    let deadline = Instant::now() + COLLECTION_TIME;
    let files = boundary::admit(&request, deadline)?;
    collect(files, request, deadline).map_err(|problem| format!("collection {problem:?}"))
}

fn collect(
    mut files: boundary::Files,
    request: Request,
    deadline: Instant,
) -> Result<String, Problem> {
    let scope = read_scope(&mut files)?;
    let tasks = process::scan(&files.proc, &scope, deadline)?;
    let expected = Expected {
        service_uid: request.service_uid,
        job_uid: request.job_uid,
        agents: tasks.select_agents(&request.agents)?,
        desktops: request.desktops,
    };
    let pids = configured_processes(&expected)?;
    let mut sample = || {
        if scope != read_scope(&mut files)? {
            return Err(Problem::Changed);
        }
        let accounts = identity::read_accounts(&mut files)?;
        let observed_descriptors = sockets::descriptors(&tasks, deadline)?;
        let diag = sockets::diagnostic(deadline)?;
        let table = |name| read_at(&files.current, name, inventory::TABLE_BYTES, deadline);
        let endpoints = inventory::verify_endpoints(
            &table("net/tcp")?,
            &table("net/tcp6")?,
            &table("net/unix")?,
            &diag,
        )?;
        let inventory = inventory::collect_targets(endpoints, &expected.desktops, |inode| {
            observed_descriptors.holders(&tasks, inode)
        })?;
        tasks.recheck(&files.proc, &scope, deadline)?;
        let actual_scope = read_scope(&mut files)?;
        if scope != actual_scope {
            return Err(Problem::Changed);
        }
        let processes = pids
            .iter()
            .map(|pid| (*pid, tasks.processes[pid].clone()))
            .collect::<BTreeMap<_, _>>();
        let observed = Observed {
            accounts,
            processes,
            inventory,
            missing: BTreeSet::new(),
        };
        Ok(((actual_scope, observed), diag, observed_descriptors))
    };
    let (before, diag, descriptors) = sample()?;
    let (after, diag_after, descriptors_after) = sample()?;
    if diag != diag_after || descriptors != descriptors_after {
        return Err(Problem::Changed);
    }
    let observed = reconcile(&expected, before, after)?;
    let output = render(&request.name, &scope, &expected, &observed)?;
    check_deadline(deadline)?;
    Ok(output)
}

fn render(
    name: &str,
    scope: &Scope,
    expected: &Expected,
    observed: &Observed,
) -> Result<String, Problem> {
    let mut output = String::from("writ-desktop-inventory/1\n");
    writeln!(
        output,
        "scope {name} {} {} {} {}",
        scope.boot_id, scope.pid_namespace, scope.user_namespace, scope.net_namespace
    )
    .map_err(|_| Problem::Limit)?;
    for (role, uid) in [("service", expected.service_uid), ("job", expected.job_uid)]
        .into_iter()
        .chain(expected.agents.keys().map(|&uid| ("agent", uid)))
        .chain(expected.desktops.iter().map(|&uid| ("desktop", uid)))
    {
        let primary = observed.accounts.accounts[&uid].primary;
        write!(output, "account {role} {uid} {primary}").map_err(|_| Problem::Limit)?;
        for (&gid, group) in &observed.accounts.groups {
            if group.listed.contains(&uid) {
                write!(output, " {gid}").map_err(|_| Problem::Limit)?;
            }
        }
        output.push('\n');
    }
    for (&pid, creds) in &observed.processes {
        write!(output, "process {pid} {}", creds.start).map_err(|_| Problem::Limit)?;
        for id in creds
            .uids
            .into_iter()
            .chain(creds.gids)
            .chain(creds.groups.iter().copied())
        {
            write!(output, " {id}").map_err(|_| Problem::Limit)?;
        }
        output.push('\n');
    }
    inventory::render(&mut output, &observed.inventory)?;
    for (uid, kind) in &observed.missing {
        writeln!(output, "missing {uid} {kind:?}").map_err(|_| Problem::Limit)?;
    }
    output.push_str("end\n");
    Ok(output)
}

fn check_deadline(deadline: Instant) -> Result<(), Problem> {
    if Instant::now() >= deadline {
        return Err(Problem::Limit);
    }
    Ok(())
}

fn read_at(
    directory: &File,
    path: &str,
    limit: usize,
    deadline: Instant,
) -> Result<Vec<u8>, Problem> {
    check_deadline(deadline)?;
    let fd = openat2(
        directory,
        path,
        OFlags::RDONLY | OFlags::NONBLOCK | OFlags::CLOEXEC,
        Mode::empty(),
        ResolveFlags::BENEATH | ResolveFlags::NO_SYMLINKS | ResolveFlags::NO_XDEV,
    )
    .map_err(|_| Problem::Read)?;
    read_file(File::from(fd), limit, deadline)
}

fn read_file(mut file: File, limit: usize, deadline: Instant) -> Result<Vec<u8>, Problem> {
    let mut bytes = Vec::new();
    let mut buffer = [0; 4096];
    loop {
        check_deadline(deadline)?;
        let len = buffer.len().min(limit + 1 - bytes.len());
        let n = file.read(&mut buffer[..len]).map_err(|_| Problem::Read)?;
        if n == 0 {
            check_deadline(deadline)?;
            return Ok(bytes);
        }
        bytes.extend_from_slice(&buffer[..n]);
        if bytes.len() > limit {
            return Err(Problem::Limit);
        }
    }
}
