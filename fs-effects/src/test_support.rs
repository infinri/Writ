//! Test environment: a root `R` (`staging` 0700, `tree` 0755) and a state directory in a
//! fresh directory under the system temporary directory (local ext4 on the test hosts; the
//! adapter refuses tmpfs), the real filesystem UUID found through `/dev/disk/by-uuid`, a
//! SQLite ledger and controlled effect store, an in-memory immutable snapshot store, and a
//! broker with the `fs.write` adapter registered for root `r1`.

#![allow(clippy::unwrap_used, clippy::expect_used, clippy::panic)]

use std::collections::BTreeMap;
use std::os::unix::fs::{MetadataExt, PermissionsExt};
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::Mutex;

use writ_broker::clock::{ClockError, Drift, EffectiveClock, Reading};

use writ_broker::execute::{Broker, Execution};
use writ_broker::ingress::{verify_ingress, Seal, VerifiedRequest};
use writ_broker::keys::{IngressKey, MemoryKeyRing, PermitKey};
use writ_broker::policy::{ActivePolicy, PolicyState};
use writ_broker::runner::{snapshot_digest, SnapshotContent, SnapshotFile};
use writ_broker::workflow::SourceUnavailable;
use writ_broker::writ_kernel::contracts::StateRevision;
use writ_broker::writ_kernel::json::{self, Value};
use writ_broker::writ_kernel::policy::load_bundle;
use writ_broker::writ_kernel::time::Timestamp;
use writ_hostsys::fsfx;
use writ_ledger_sqlite::{OpRow, OutcomeRow, SqliteEffectStore, SqliteLedger, TargetRow};

use crate::ident;
use crate::{FsWriteAdapter, RootConfig};

pub(crate) const ADAPTER: &str = "writ-host-adapter:fixture";
pub(crate) const TENANT: &str = "fixture-tenant";
pub(crate) const PROJECT: &str = "fixture-project";
pub(crate) const ROOT: &str = "r1";
pub(crate) const SNAP: &str = "snap-fixture";

/// An injected effective clock.
pub(crate) struct TestClock(Mutex<Timestamp>);

impl EffectiveClock for TestClock {
    fn read(&self) -> Result<Reading, ClockError> {
        Ok(Reading {
            effective: *self.0.lock().unwrap(),
            wall: None,
            drift: Drift::Normal,
            drift_changed: false,
        })
    }
}

/// In-memory immutable snapshots: one snapshot per test with fixed content.
pub(crate) struct MemSnapshots {
    files: Mutex<BTreeMap<String, Vec<SnapshotFile>>>,
    pub(crate) immutable: AtomicBool,
}

impl MemSnapshots {
    fn new() -> MemSnapshots {
        let files = vec![
            SnapshotFile {
                path: "a.txt".into(),
                bytes: b"alpha content\n".to_vec(),
            },
            SnapshotFile {
                path: "b.txt".into(),
                bytes: b"bravo content, longer than alpha\n".to_vec(),
            },
            SnapshotFile {
                path: "same.txt".into(),
                bytes: b"alpha content\n".to_vec(),
            },
        ];
        let mut m = BTreeMap::new();
        m.insert(SNAP.to_string(), files);
        MemSnapshots {
            files: Mutex::new(m),
            immutable: AtomicBool::new(true),
        }
    }

    pub(crate) fn manifest(&self) -> String {
        snapshot_digest(&self.files.lock().unwrap()[SNAP]).unwrap()
    }

    pub(crate) fn bytes(&self, path: &str) -> Vec<u8> {
        self.files.lock().unwrap()[SNAP]
            .iter()
            .find(|f| f.path == path)
            .unwrap()
            .bytes
            .clone()
    }
}

impl SnapshotContent for MemSnapshots {
    fn content(
        &self,
        tenant_id: &str,
        project_id: &str,
        snapshot_id: &str,
    ) -> Result<Option<Vec<SnapshotFile>>, SourceUnavailable> {
        if tenant_id != TENANT || project_id != PROJECT {
            return Ok(None);
        }
        Ok(self.files.lock().unwrap().get(snapshot_id).cloned())
    }

    fn immutable(&self) -> bool {
        self.immutable.load(Ordering::SeqCst)
    }
}

/// The filesystem UUID of the filesystem holding `path`, from `/dev/disk/by-uuid`.
pub(crate) fn fs_uuid_of(path: &Path) -> String {
    let dev = std::fs::metadata(path).unwrap().dev();
    for entry in std::fs::read_dir("/dev/disk/by-uuid").expect("/dev/disk/by-uuid is required") {
        let entry = entry.unwrap();
        if fsfx::block_device(&entry.path()) == Some(dev) {
            return entry.file_name().to_string_lossy().into_owned();
        }
    }
    panic!("no /dev/disk/by-uuid entry for the test filesystem")
}

pub(crate) fn mkdir(p: &Path, mode: u32) {
    std::fs::create_dir_all(p).unwrap();
    std::fs::set_permissions(p, std::fs::Permissions::from_mode(mode)).unwrap();
}

/// A test environment (module documentation).
pub(crate) struct Env {
    pub(crate) dir: PathBuf,
    pub(crate) cfg: RootConfig,
    pub(crate) snaps: MemSnapshots,
    /// Remove the directory when dropped (the creating process only).
    owned: bool,
}

impl Drop for Env {
    fn drop(&mut self) {
        if self.owned {
            let _ = std::fs::set_permissions(
                &self.cfg.state_dir,
                std::fs::Permissions::from_mode(0o700),
            );
            let _ = std::fs::remove_dir_all(&self.dir);
        }
    }
}

/// The ledger side of a test.
pub(crate) struct Stores {
    pub(crate) ledger: SqliteLedger,
    pub(crate) effects: SqliteEffectStore,
    pub(crate) active: ActivePolicy,
    pub(crate) clock: TestClock,
    pub(crate) key: PermitKey,
}

fn bundle() -> writ_broker::writ_kernel::policy::LoadedBundle {
    let mut doc = json::parse(include_bytes!(
        "../../docs/contracts/fixtures/policy/m0-reference.bundle.json"
    ))
    .unwrap();
    let statement = json::parse(
        br#"{"statement_id":"permit-fs-write","effect":"permit","action_kinds":["fs.write"],
        "resource_prefixes":["file:fixture-tenant/fixture-project/"],"reason_code":"FS_WRITE_PERMITTED"}"#,
    )
    .unwrap();
    if let Value::Obj(m) = &mut doc {
        if let Some(Value::Arr(caps)) = m.get_mut("known_capabilities") {
            caps.push(json::s("fs.write"));
        }
        if let Some(Value::Arr(st)) = m.get_mut("statements") {
            st.push(statement);
        }
    }
    load_bundle(&doc).unwrap()
}

impl Env {
    /// A fresh environment named `name` with its ledger, effect store and journal created.
    pub(crate) fn new(name: &str) -> Env {
        let dir = std::env::temp_dir().join(format!("writ-fsfx-{}-{name}", std::process::id()));
        let _ = std::fs::remove_dir_all(&dir);
        Env::at(&dir, true)
    }

    /// The environment at `dir` (an existing one when `fresh` is false).
    pub(crate) fn at(dir: &Path, fresh: bool) -> Env {
        let root = dir.join("R");
        if fresh {
            mkdir(dir, 0o755);
            mkdir(&root, 0o755);
            mkdir(&root.join("staging"), 0o700);
            mkdir(&root.join("tree"), 0o755);
            mkdir(&root.join("tree/sub"), 0o755);
            mkdir(&dir.join("state"), 0o700);
            std::fs::write(
                dir.join("cgroup.procs"),
                format!("{}\n", std::process::id()),
            )
            .unwrap();
        }
        let mut cfg = RootConfig::new(ROOT, &root, &dir.join("journal.db"), &fs_uuid_of(&root));
        cfg.state_dir = dir.join("state");
        cfg.service_cgroup_procs = Some(dir.join("cgroup.procs"));
        let env = Env {
            dir: dir.to_path_buf(),
            cfg,
            snaps: MemSnapshots::new(),
            owned: fresh,
        };
        if fresh {
            let ledger = SqliteLedger::create(&dir.join("ledger.db")).unwrap();
            SqliteEffectStore::create(&dir.join("effects.db"), ledger.ledger_id()).unwrap();
            let mut cfg = env.cfg.clone();
            cfg.create_journal = true;
            FsWriteAdapter::open(&cfg, ledger.ledger_id(), &env.snaps, &[]).unwrap();
        }
        env
    }

    pub(crate) fn tree(&self, rel: &str) -> PathBuf {
        self.cfg.path.join("tree").join(rel)
    }

    pub(crate) fn staging(&self) -> PathBuf {
        self.cfg.path.join("staging")
    }

    pub(crate) fn stores(&self) -> Stores {
        let ledger = SqliteLedger::open(&self.dir.join("ledger.db")).unwrap();
        let effects =
            SqliteEffectStore::open(&self.dir.join("effects.db"), ledger.ledger_id()).unwrap();
        Stores {
            ledger,
            effects,
            active: ActivePolicy::new(PolicyState::loaded(bundle())),
            clock: TestClock(Mutex::new(
                Timestamp::parse_rfc3339("2026-11-05T12:00:00Z").unwrap(),
            )),
            key: PermitKey::from_bytes(vec![0x44; 32]).unwrap(),
        }
    }

    pub(crate) fn adapter<'a>(
        &'a self,
        s: &Stores,
    ) -> Result<FsWriteAdapter<'a>, writ_broker::store::StoreError> {
        FsWriteAdapter::open(&self.cfg, s.ledger.ledger_id(), &self.snaps, &[])
    }

    /// A `create_only` request writing snapshot file `source` to `target`.
    pub(crate) fn create(&self, idem: &str, target: &str, source: &str) -> Value {
        self.request(idem, target, source, "create_only", None)
    }

    /// A `replace_if` request expecting the prior content `prior`.
    pub(crate) fn replace(&self, idem: &str, target: &str, source: &str, prior: &[u8]) -> Value {
        self.request(
            idem,
            target,
            source,
            "replace_if",
            Some(&ident::digest(prior)),
        )
    }

    pub(crate) fn request(
        &self,
        idem: &str,
        target: &str,
        source: &str,
        mode: &str,
        prior: Option<&str>,
    ) -> Value {
        let prior = prior.map_or(String::new(), |p| {
            format!(r#","expected_prior_digest":"{p}""#)
        });
        let text = format!(
            r#"{{"contract_version":"2.0","request_id":"req-{idem}","trace_id":"trace-1","tenant_id":"{TENANT}",
            "principal":{{"id":"agent:claude-session-1","verified_by":"{ADAPTER}","authn_method":"os_peer_credential","roles":["agent"]}},
            "project_id":"{PROJECT}","session_id":"session-1","agent_id":null,"parent_session_id":null,"idempotency_key":"{idem}",
            "expected_state_revision":null,"policy_bundle_sha":null,"deadline_at":null,
            "action":{{"kind":"fs.write","resource":{{"uri":"file:///{ROOT}/{target}","canonical_resource_id":"file:{TENANT}/{PROJECT}/{ROOT}/{target}"}},
            "action_digest":"sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
            "attributes":{{"snapshot_id":"{SNAP}","manifest_digest":"{}","source_path":"{source}",
            "content_digest":"{}","mode":"{mode}"{prior}}},"requested_capabilities":[]}}}}"#,
            self.snaps.manifest(),
            ident::digest(&self.snaps.bytes(source)),
        );
        json::parse(text.as_bytes()).unwrap()
    }
}

pub(crate) fn broker<'a>(
    s: &'a Stores,
    a: &'a FsWriteAdapter<'a>,
) -> Broker<'a, SqliteLedger, SqliteEffectStore> {
    Broker::new(&s.active, &s.clock, &s.ledger, &s.effects, &s.key).with_fs_root(ROOT, a)
}

pub(crate) fn verify(req: &Value) -> VerifiedRequest {
    let key = || IngressKey::from_bytes(vec![0x11; 32]).unwrap();
    let mut ring = MemoryKeyRing::new();
    ring.register(ADAPTER, key());
    let seal = Seal::create(req, ADAPTER, &key()).unwrap().to_value();
    verify_ingress(req, Some(&seal), &ring).unwrap()
}

pub(crate) fn exec<A: writ_broker::effects::EffectAdapter + ?Sized>(
    b: &Broker<'_, SqliteLedger, A>,
    req: &Value,
) -> Execution {
    b.execute(&verify(req), StateRevision::new(5).unwrap(), None)
}

pub(crate) fn status(s: &Stores, idem: &str) -> Option<String> {
    s.ledger.receipt_status(TENANT, idem).unwrap()
}

/// The op, outcome and target row recorded for `idem`'s permit (read directly).
pub(crate) fn journal_of(
    a: &FsWriteAdapter<'_>,
    permit_id: &str,
) -> (Option<OpRow>, Option<OutcomeRow>, Option<TargetRow>) {
    a.journal()
        .read("test", |tx| {
            let op = tx.op_by_permit(permit_id)?;
            let outcome = match &op {
                Some(o) => tx.outcome(&o.op_id)?,
                None => None,
            };
            let target = match &op {
                Some(o) => tx.target(&o.target)?,
                None => None,
            };
            Ok((op, outcome, target))
        })
        .unwrap()
}

/// The target row of `target`.
pub(crate) fn target_row(a: &FsWriteAdapter<'_>, target: &str) -> Option<TargetRow> {
    a.journal().read("test", |tx| tx.target(target)).unwrap()
}

/// The permit id of an execution.
pub(crate) fn permit(e: &Execution) -> String {
    e.permit.as_ref().unwrap().permit_id().to_string()
}

/// Files in `staging`.
pub(crate) fn staging_files(env: &Env) -> Vec<String> {
    let mut v: Vec<String> = std::fs::read_dir(env.staging())
        .unwrap()
        .map(|e| e.unwrap().file_name().to_string_lossy().into_owned())
        .collect();
    v.sort();
    v
}

pub(crate) fn applied() -> Result<writ_broker::execute::Executed, writ_broker::refusal::Refusal> {
    Ok(writ_broker::execute::Executed::EffectCompleted)
}

pub(crate) fn failed() -> Result<writ_broker::execute::Executed, writ_broker::refusal::Refusal> {
    Ok(writ_broker::execute::Executed::EffectFailed)
}
