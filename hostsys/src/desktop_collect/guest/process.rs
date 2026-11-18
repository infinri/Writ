use std::collections::{BTreeMap, BTreeSet};
use std::fs::File;
use std::time::Instant;

use rustix::fs::{fstat, openat2, readlinkat_raw, Dir, Mode, OFlags, ResolveFlags};
use rustix::io::Errno;

use super::super::identity::{stat_start, status_credentials, Credentials};
use super::super::{valid_pid, Problem, Scope};
use super::{check_deadline, read_at};

const MAX_PROCESSES: usize = 4096;
const MAX_TASKS: usize = 8192;
const MAX_GROUP_ENTRIES: usize = 262_144;
const STAT_BYTES: usize = 4096;
const STATUS_BYTES: usize = 1024 * 1024;

pub(super) struct Scan {
    pub(super) processes: BTreeMap<u32, Credentials>,
    pub(super) tasks: BTreeMap<u32, Task>,
}

pub(super) struct Task {
    pub(super) process: u32,
    pub(super) directory: File,
    pub(super) credentials: Credentials,
}

fn process_error(error: Errno) -> Problem {
    match error {
        Errno::NOENT | Errno::SRCH => Problem::Changed,
        _ => Problem::Read,
    }
}

fn open_directory(dir: &File, path: &str, access: OFlags) -> Result<File, Problem> {
    openat2(
        dir,
        path,
        access | OFlags::DIRECTORY | OFlags::CLOEXEC,
        Mode::empty(),
        ResolveFlags::BENEATH | ResolveFlags::NO_SYMLINKS | ResolveFlags::NO_XDEV,
    )
    .map(File::from)
    .map_err(process_error)
}

fn enumerate(
    directory: &File,
    process_root: bool,
    limit: usize,
    deadline: Instant,
) -> Result<BTreeMap<u32, u64>, Problem> {
    check_deadline(deadline)?;
    let reader = open_directory(directory, ".", OFlags::RDONLY)?;
    let mut entries = Dir::new(reader).map_err(process_error)?;
    let mut ids = BTreeMap::new();
    let mut examined = 0;
    loop {
        check_deadline(deadline)?;
        let Some(entry) = entries.next() else {
            break;
        };
        examined += 1;
        if examined > limit + if process_root { 128 } else { 2 } {
            return Err(Problem::Limit);
        }
        let entry = entry.map_err(process_error)?;
        let name = entry.file_name().to_bytes();
        if matches!(name, b"." | b"..") {
            continue;
        }
        if process_root && !name.first().is_some_and(u8::is_ascii_digit) {
            continue;
        }
        if name.first() == Some(&b'0') || !name.iter().all(u8::is_ascii_digit) {
            return Err(Problem::Malformed);
        }
        let id = std::str::from_utf8(name)
            .ok()
            .and_then(|name| name.parse().ok())
            .filter(|&id| valid_pid(id))
            .ok_or(Problem::Malformed)?;
        if ids.insert(id, entry.ino()).is_some() {
            return Err(Problem::Duplicate);
        }
        if ids.len() > limit {
            return Err(Problem::Limit);
        }
    }
    check_deadline(deadline)?;
    Ok(ids)
}

fn walk_tasks(
    proc: &File,
    deadline: Instant,
    mut visit: impl FnMut(u32, u32, File) -> Result<(), Problem>,
) -> Result<(), Problem> {
    let processes = enumerate(proc, true, MAX_PROCESSES, deadline)?;
    if !processes.contains_key(&1) {
        return Err(Problem::Incomplete);
    }
    let mut remaining = MAX_TASKS;
    for (process, inode) in processes {
        check_deadline(deadline)?;
        let process_dir = open_directory(proc, &process.to_string(), OFlags::PATH)?;
        if fstat(&process_dir).map_err(process_error)?.st_ino != inode {
            return Err(Problem::Changed);
        }
        let task_dir = open_directory(&process_dir, "task", OFlags::PATH)?;
        let tids = enumerate(&task_dir, false, remaining, deadline)?;
        if !tids.contains_key(&process) {
            return Err(Problem::Changed);
        }
        remaining -= tids.len();
        for (tid, inode) in tids {
            check_deadline(deadline)?;
            let directory = open_directory(&task_dir, &tid.to_string(), OFlags::PATH)?;
            if fstat(&directory).map_err(process_error)?.st_ino != inode {
                return Err(Problem::Changed);
            }
            visit(process, tid, directory)?;
        }
    }
    check_deadline(deadline)
}

fn read_proc_text(
    dir: &File,
    name: &str,
    limit: usize,
    deadline: Instant,
) -> Result<String, Problem> {
    let bytes = read_at(dir, name, limit, deadline)?;
    if bytes.last() != Some(&b'\n') {
        return Err(Problem::Incomplete);
    }
    if bytes.contains(&0) {
        return Err(Problem::Malformed);
    }
    String::from_utf8(bytes).map_err(|_| Problem::Unsupported)
}

fn check_namespaces(dir: &File, scope: &Scope, deadline: Instant) -> Result<(), Problem> {
    check_deadline(deadline)?;
    let dir = open_directory(dir, "ns", OFlags::PATH)?;
    for (name, id) in [
        ("pid", scope.pid_namespace),
        ("user", scope.user_namespace),
        ("net", scope.net_namespace),
    ] {
        check_deadline(deadline)?;
        let mut buffer = [0u8; 64];
        let length = readlinkat_raw(&dir, name, &mut buffer[..]).map_err(process_error)?;
        if length == buffer.len() {
            return Err(Problem::Limit);
        }
        if &buffer[..length] != format!("{name}:[{id}]").as_bytes() {
            return Err(Problem::Changed);
        }
    }
    check_deadline(deadline)
}

fn observe(
    dir: &File,
    process: u32,
    tid: u32,
    scope: &Scope,
    deadline: Instant,
) -> Result<Credentials, Problem> {
    let start = stat_start(&read_proc_text(dir, "stat", STAT_BYTES, deadline)?, tid)?;
    let read_credentials = || {
        status_credentials(
            &read_proc_text(dir, "status", STATUS_BYTES, deadline)?,
            tid,
            process,
            start,
        )
    };
    let credentials = read_credentials()?;
    check_namespaces(dir, scope, deadline)?;
    if credentials != read_credentials()?
        || start != stat_start(&read_proc_text(dir, "stat", STAT_BYTES, deadline)?, tid)?
    {
        return Err(Problem::Changed);
    }
    Ok(credentials)
}

pub(super) fn scan(proc: &File, scope: &Scope, deadline: Instant) -> Result<Scan, Problem> {
    let mut tasks = BTreeMap::new();
    let mut processes = BTreeMap::new();
    let mut group_entries = 0;
    walk_tasks(proc, deadline, |process, tid, directory| {
        let credentials = observe(&directory, process, tid, scope, deadline)?;
        group_entries += credentials.groups.len();
        if group_entries > MAX_GROUP_ENTRIES {
            return Err(Problem::Limit);
        }
        if tid == process {
            processes.insert(process, credentials.clone());
        }
        if tasks
            .insert(
                tid,
                Task {
                    process,
                    directory,
                    credentials,
                },
            )
            .is_some()
        {
            return Err(Problem::Duplicate);
        }
        Ok(())
    })?;
    Ok(Scan { processes, tasks })
}

impl Scan {
    pub(super) fn select_agents(
        &self,
        uids: &BTreeSet<u32>,
    ) -> Result<BTreeMap<u32, BTreeSet<u32>>, Problem> {
        let mut selected: BTreeMap<_, BTreeSet<_>> =
            uids.iter().map(|&uid| (uid, BTreeSet::new())).collect();
        let mut relevant = BTreeSet::new();
        for task in self.tasks.values() {
            if task.credentials.uids.iter().any(|uid| uids.contains(uid)) {
                relevant.insert(task.process);
            }
        }
        for task in self.tasks.values() {
            if !relevant.contains(&task.process) {
                continue;
            }
            let leader = self
                .processes
                .get(&task.process)
                .ok_or(Problem::Incomplete)?;
            let credentials = &task.credentials;
            let uid = leader.uids[0];
            if credentials.uids != [uid; 4]
                || credentials.gids != leader.gids
                || credentials.groups != leader.groups
            {
                return Err(Problem::Ambiguous);
            }
            selected
                .get_mut(&uid)
                .ok_or(Problem::Ambiguous)?
                .insert(task.process);
        }
        Ok(selected)
    }

    pub(super) fn recheck(
        &self,
        proc: &File,
        scope: &Scope,
        deadline: Instant,
    ) -> Result<(), Problem> {
        let mut seen = 0;
        walk_tasks(proc, deadline, |process, tid, directory| {
            let before = self.tasks.get(&tid).ok_or(Problem::Changed)?;
            if before.process != process {
                return Err(Problem::Changed);
            }
            let old = fstat(&before.directory).map_err(process_error)?;
            let current = fstat(&directory).map_err(process_error)?;
            if (old.st_dev, old.st_ino) != (current.st_dev, current.st_ino)
                || before.credentials != observe(&before.directory, process, tid, scope, deadline)?
            {
                return Err(Problem::Changed);
            }
            seen += 1;
            Ok(())
        })?;
        if seen != self.tasks.len() {
            return Err(Problem::Changed);
        }
        Ok(())
    }
}

#[cfg(test)]
mod tests;
