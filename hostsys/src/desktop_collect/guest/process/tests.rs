#![allow(clippy::unwrap_used)]

use std::fs;
use std::os::unix::fs::symlink;
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicU64, Ordering};
use std::time::Duration;

use super::*;

static FIXTURE_ID: AtomicU64 = AtomicU64::new(0);

struct Fixture {
    path: PathBuf,
    proc: File,
}

impl Fixture {
    fn new() -> Self {
        let name = format!(
            "writ-process-fixture-{}-{}",
            std::process::id(),
            FIXTURE_ID.fetch_add(1, Ordering::Relaxed)
        );
        let path = std::env::temp_dir().join(name);
        fs::create_dir(&path).unwrap();
        let proc = File::open(&path).unwrap();
        let fixture = Self { path, proc };
        fixture.task(1, 1, 0, 1);
        fixture
    }

    fn task_path(&self, process: u32, tid: u32) -> PathBuf {
        self.path.join(format!("{process}/task/{tid}"))
    }

    fn task(&self, process: u32, tid: u32, uid: u32, start: u64) {
        let path = self.task_path(process, tid);
        fs::create_dir_all(path.join("ns")).unwrap();
        credentials(&path, process, tid, uid);
        let mut fields = vec!["0".to_owned(); 49];
        fields[22 - 4] = start.to_string();
        let stat = format!("{tid} (fixture) S {}\n", fields.join(" "));
        fs::write(path.join("stat"), stat).unwrap();
        for (name, id) in [("pid", 2), ("user", 3), ("net", 4)] {
            symlink(format!("{name}:[{id}]"), path.join("ns").join(name)).unwrap();
        }
    }

    fn scan(&self) -> Result<Scan, Problem> {
        scan(&self.proc, &scope(), deadline())
    }

    fn recheck(&self, before: &Scan) -> Result<(), Problem> {
        before.recheck(&self.proc, &scope(), deadline())
    }
}

impl Drop for Fixture {
    fn drop(&mut self) {
        let _ = fs::remove_dir_all(&self.path);
    }
}

fn credentials(path: &Path, process: u32, tid: u32, uid: u32) {
    let status = format!(
        "Pid:\t{tid}\nTgid:\t{process}\nUid:\t{uid}\t{uid}\t{uid}\t{uid}\nGid:\t1\t1\t1\t1\nGroups:\t1 2\n"
    );
    fs::write(path.join("status"), status).unwrap();
}

fn scope() -> Scope {
    Scope {
        boot_id: "unneeded in process fixtures".into(),
        pid_namespace: 2,
        user_namespace: 3,
        net_namespace: 4,
    }
}

fn deadline() -> Instant {
    Instant::now() + Duration::from_secs(5)
}

#[test]
fn selects_by_observed_agents_and_reads_each_threads_facts() {
    let fixture = Fixture::new();
    fixture.task(10, 10, 123, 20);
    fixture.task(10, 11, 123, 21);
    fixture.task(20, 20, 456, 22);
    let before = fixture.scan().unwrap();
    assert_eq!(
        before.select_agents(&BTreeSet::from([123, 999])).unwrap(),
        BTreeMap::from([(123, BTreeSet::from([10])), (999, BTreeSet::new())])
    );
    assert_eq!(before.tasks[&11].process, 10);
    assert_eq!(before.tasks[&11].credentials.start, 21);
    assert_eq!(fixture.recheck(&before), Ok(()));
}

#[test]
fn refuses_thread_only_agent_identity() {
    let fixture = Fixture::new();
    fixture.task(10, 10, 456, 20);
    fixture.task(10, 11, 123, 21);
    assert_eq!(
        fixture
            .scan()
            .unwrap()
            .select_agents(&BTreeSet::from([123])),
        Err(Problem::Ambiguous)
    );
}

#[test]
fn detects_disappearance_between_thread_enumeration_and_open() {
    let fixture = Fixture::new();
    fixture.task(10, 10, 123, 20);
    fixture.task(10, 11, 123, 21);
    let result = walk_tasks(&fixture.proc, deadline(), |_, tid, _| {
        if tid == 10 {
            fs::rename(fixture.task_path(10, 11), fixture.path.join("vanished")).unwrap();
        }
        Ok(())
    });
    assert_eq!(result, Err(Problem::Changed));
}

#[test]
fn detects_replaced_tid_even_with_same_start_ticks_and_credentials() {
    let fixture = Fixture::new();
    fixture.task(10, 10, 123, 20);
    fixture.task(10, 11, 123, 21);
    let before = fixture.scan().unwrap();
    fs::rename(fixture.task_path(10, 11), fixture.path.join("prior")).unwrap();
    fixture.task(10, 11, 123, 21);
    assert_eq!(fixture.recheck(&before), Err(Problem::Changed));
}

#[test]
fn detects_changed_process_membership() {
    let fixture = Fixture::new();
    fixture.task(10, 10, 123, 20);
    let before = fixture.scan().unwrap();
    fixture.task(10, 11, 123, 21);
    assert_eq!(fixture.recheck(&before), Err(Problem::Changed));
    let after = fixture.scan().unwrap();
    fs::rename(fixture.path.join("10"), fixture.path.join("prior")).unwrap();
    assert_eq!(fixture.recheck(&after), Err(Problem::Changed));
}

#[test]
fn detects_changed_credentials_and_namespace_in_pinned_task() {
    let fixture = Fixture::new();
    fixture.task(10, 10, 123, 20);
    let before = fixture.scan().unwrap();
    let path = fixture.task_path(10, 10);
    credentials(&path, 10, 10, 456);
    assert_eq!(fixture.recheck(&before), Err(Problem::Changed));
    credentials(&path, 10, 10, 123);
    fs::remove_file(path.join("ns/net")).unwrap();
    symlink("net:[90]", path.join("ns/net")).unwrap();
    assert_eq!(fixture.recheck(&before), Err(Problem::Changed));
}

#[test]
fn refuses_an_unreadable_task_input() {
    let fixture = Fixture::new();
    fixture.task(10, 10, 123, 20);
    let path = fixture.task_path(10, 10);
    fs::remove_file(path.join("status")).unwrap();
    symlink("stat", path.join("status")).unwrap();
    assert!(fixture.scan().is_err());
}

#[test]
fn refuses_a_capped_or_expired_enumeration() {
    let fixture = Fixture::new();
    fixture.task(10, 10, 123, 20);
    assert_eq!(
        enumerate(&fixture.proc, true, 1, deadline()),
        Err(Problem::Limit)
    );
    assert!(scan(&fixture.proc, &scope(), Instant::now()).is_err());
}
