//! M3-07 mechanical runner end to end on the SQLite ledger and runner store: a
//! `runner.evaluate` request executed through the broker produces a stored, authenticated
//! attestation that a promotion accepts exactly once; untrusted or mismatched inputs
//! produce none; permits run at most once; storage faults leave uncertainty that
//! reconciliation never resolves without evidence. Test keys, sources and authorities only;
//! real runs use the uncontained `LocalProcessSandbox` (disposable test resources only).

#![allow(clippy::unwrap_used, clippy::expect_used, clippy::panic)]

use std::collections::BTreeMap;
use std::path::PathBuf;
use std::sync::atomic::{AtomicBool, AtomicUsize, Ordering};
use std::sync::Mutex;
use std::time::Duration;

use writ_broker::acceptance::{AcceptanceCode, MAC_ALG, RECORD_CONTRACT, RECORD_PURPOSE};
use writ_broker::execute::{Broker, Executed, Execution};
use writ_broker::keys::{AcceptanceRecordKey, MemoryKeys, PermitKey, RunnerAttestationKey};
use writ_broker::policy::{ActivePolicy, PolicyState};
use writ_broker::process::LocalProcessSandbox;
use writ_broker::refusal::Refusal;
use writ_broker::runner::{
    attestation_id_for, snapshot_digest, CommandRegistry, Job, JobFailure, JobOutput, LedgerClock,
    Limits, RegisteredCommand, RunState, RunnerAdapter, RunnerConfig, RunnerRequirements,
    RunnerSources, RunnerStore, Sandbox, SandboxGuarantees, SandboxUnavailable, SnapshotContent,
    SnapshotFile,
};
use writ_broker::store::StoreError;
use writ_broker::workflow::{AdminRequest, PromotionOutcome, SourceUnavailable, Trust};
use writ_broker::writ_kernel::contracts::StateRevision;
use writ_broker::writ_kernel::json::{self, Value};
use writ_broker::writ_kernel::policy::{load_bundle, LoadedBundle};
use writ_broker::writ_kernel::time::Timestamp;

use super::execute_tests::reconciled;
use super::grant_tests::{set, verify, TestClock};
use super::transition_tests::{
    alice, committed, parse, sha, sign, transition, wf, Docs, Operators, Snapshots, RECORD_KEY,
};
use super::{fault, SqliteEffectStore, SqliteLedger, SqliteRunnerStore};

const T: &str = "fixture-tenant";
const P: &str = "fixture-project";
const RUNNER_KEY: [u8; 32] = [0x6b; 32];

fn now() -> Timestamp {
    Timestamp::parse_strict("2026-11-07T11:00:00Z").unwrap()
}

fn files() -> Vec<SnapshotFile> {
    vec![
        SnapshotFile {
            path: "src/lib.rs".into(),
            bytes: b"pub fn f() {}\n".to_vec(),
        },
        SnapshotFile {
            path: "README".into(),
            bytes: b"snapshot one\n".to_vec(),
        },
    ]
}

fn digest() -> String {
    snapshot_digest(&files()).unwrap()
}

/// A registered `#!/bin/sh` script (runs under the host's `/bin/sh`).
fn script(body: &str) -> RegisteredCommand {
    RegisteredCommand {
        command_id: "cargo-test".into(),
        program: format!("#!/bin/sh\n{body}\n").into_bytes(),
        args: vec!["--quiet".into()],
        env: vec![("PATH".into(), "/usr/bin:/bin".into())],
    }
}

fn requirements() -> Value {
    parse(&format!(
        r#"{{"acceptance_policy_id":"accept-code-review","acceptance_policy_version":"3","acceptance_policy_bundle_sha":"{}","criteria":[{{"criterion_id":"build","required_class":"mechanical"}}]}}"#,
        sha('c')
    ))
}

fn entry(revision: i64, environment: &str, command: &RegisteredCommand) -> Value {
    let mut v = parse(include_str!(
        "../../../docs/contracts/proposed/examples/evaluator-registry-entry.valid.json"
    ));
    v = set(&v, &["scope", "tenant_id"], json::s(T));
    v = set(&v, &["scope", "project_id"], json::s(P));
    v = set(&v, &["registry_revision"], Value::Int(revision));
    v = set(&v, &["environment_digest"], json::s(environment));
    set(
        &v,
        &["command", "command_digest"],
        json::s(&command.digest().unwrap()),
    )
}

/// The authenticated record `id` for verifying to accepted at `revision` on the snapshot,
/// requiring attestation `att` from `ci-runner` in `environment`.
fn record(id: &str, revision: i64, snapshot: (&str, &str), att: &str, environment: &str) -> Value {
    let mut r = parse(&format!(
        r#"{{"contract_version":"m1-draft-3","acceptance_record_id":"{id}","tenant_id":"{T}","project_id":"{P}","task_id":"task-1","workflow_id":"wf-1","transition":{{"from_state":"verifying","to_state":"accepted"}},"artifact_snapshot_id":"{}","artifact_digest":"{}","acceptance_policy_id":"accept-code-review","acceptance_policy_version":"3","acceptance_policy_bundle_sha":"{}","state_revision":{revision},"status":"accepted","issued_at":"2026-11-07T10:00:00Z","expires_at":"2026-11-07T12:00:00Z","criteria":[{{"criterion_id":"build","mandatory":true,"required_class":"mechanical","status":"pass","evaluator_id":"ci-runner","evaluator_trust":"M","fresh_until":"2026-11-07T11:30:00Z"}}],"record_digest":"{}"}}"#,
        snapshot.0,
        snapshot.1,
        sha('c'),
        sha('0')
    ));
    let d = json::digest(&r.without("record_digest")).unwrap();
    r = set(&r, &["record_digest"], json::s(d.as_str()));
    let doc = json::obj([
        (
            "auth",
            json::obj([
                ("alg", json::s(MAC_ALG)),
                ("key_id", json::s("acc-key-1")),
                ("mac", json::s(&"0".repeat(64))),
            ]),
        ),
        ("contract_version", json::s(RECORD_CONTRACT)),
        (
            "evaluations",
            Value::Arr(vec![json::obj([
                ("attestation_id", json::s(att)),
                ("criterion_id", json::s("build")),
                ("environment_digest", json::s(environment)),
                ("evaluation_id", json::s("eval-build")),
                ("evaluator_id", json::s("ci-runner")),
                ("evaluator_version", json::s("1.4.0")),
                ("evidence_digests", Value::Arr(vec![])),
                ("implementation_digest", json::s(&sha('1'))),
                ("registry_revision", Value::Int(12)),
            ])]),
        ),
        ("issuer", json::s("acceptance-service")),
        ("mode", json::s("governed")),
        ("purpose", json::s(RECORD_PURPOSE)),
        ("record", r),
    ]);
    sign(&doc, &RECORD_KEY)
}

fn bundle() -> LoadedBundle {
    let mut b = parse(include_str!(
        "../../../m1/fixtures/transition/m1-transition.bundle.json"
    ));
    let mut statements = b.get("statements").unwrap().as_arr().unwrap().to_vec();
    statements.push(parse(
        r#"{"statement_id":"permit-runner","effect":"permit","action_kinds":["runner.evaluate"],"resource_prefixes":["workflow:fixture-tenant/fixture-project/"],"reason_code":"RUNNER_PERMITTED"}"#,
    ));
    b = set(&b, &["statements"], Value::Arr(statements));
    let mut known = b
        .get("known_capabilities")
        .unwrap()
        .as_arr()
        .unwrap()
        .to_vec();
    known.push(json::s("runner.evaluate"));
    b = set(&b, &["known_capabilities"], Value::Arr(known));
    load_bundle(&b).unwrap()
}

/// A sealed runner request with idempotency key `key` for record `record`.
fn evaluate(key: &str, record: &str) -> Value {
    parse(&format!(
        r#"{{"contract_version":"2.0","request_id":"req-{key}","trace_id":"trace-1","tenant_id":"{T}","principal":{{"id":"agent:session-1","verified_by":"writ-host-adapter:fixture","authn_method":"os_peer_credential","roles":["agent"]}},"project_id":"{P}","session_id":"session-1","agent_id":null,"parent_session_id":null,"idempotency_key":"{key}","expected_state_revision":null,"policy_bundle_sha":null,"deadline_at":null,"action":{{"kind":"runner.evaluate","resource":{{"uri":"writ-workflow://wf-1","canonical_resource_id":"workflow:{T}/{P}/wf-1"}},"action_digest":"{}","attributes":{{"workflow_id":"wf-1","from_state":"verifying","to_state":"accepted","criterion_id":"build","acceptance_record_id":"{record}","evaluator_id":"ci-runner"}},"requested_capabilities":[]}}}}"#,
        sha('b')
    ))
}

fn accept(key: &str, record: &str, expected: i64, snapshot: (&str, &str)) -> Value {
    transition(
        key,
        "verifying",
        "accepted",
        expected,
        Some((record, snapshot.0, snapshot.1)),
    )
}

// ---------------------------------------------------------------------------
// Test sources and a fake sandbox.

struct Content {
    files: Mutex<BTreeMap<String, Vec<SnapshotFile>>>,
    immutable: AtomicBool,
}

impl SnapshotContent for Content {
    fn content(
        &self,
        _: &str,
        _: &str,
        id: &str,
    ) -> Result<Option<Vec<SnapshotFile>>, SourceUnavailable> {
        Ok(self.files.lock().unwrap().get(id).cloned())
    }

    fn immutable(&self) -> bool {
        self.immutable.load(Ordering::SeqCst)
    }
}

struct Commands(Mutex<RegisteredCommand>);

impl CommandRegistry for Commands {
    fn command(
        &self,
        _: &str,
        _: &str,
        id: &str,
    ) -> Result<Option<RegisteredCommand>, SourceUnavailable> {
        let c = self.0.lock().unwrap().clone();
        Ok((c.command_id == id).then_some(c))
    }
}

struct Fake {
    result: Mutex<Result<JobOutput, JobFailure>>,
    runs: AtomicUsize,
}

impl Sandbox for Fake {
    fn guarantees(&self) -> Result<SandboxGuarantees, SandboxUnavailable> {
        Ok(RunnerRequirements::uncontained_for_disposable_tests().sandbox)
    }

    fn environment_digest(&self) -> String {
        sha('2')
    }

    fn run(&self, _: &Job<'_>) -> Result<JobOutput, JobFailure> {
        self.runs.fetch_add(1, Ordering::SeqCst);
        self.result.lock().unwrap().clone()
    }
}

// ---------------------------------------------------------------------------
// The world.

struct W {
    dir: PathBuf,
    ledger: SqliteLedger,
    effects: SqliteEffectStore,
    runner: SqliteRunnerStore,
    active: ActivePolicy,
    clock: TestClock,
    key: PermitKey,
    docs: Docs,
    snapshots: Snapshots,
    content: Content,
    commands: Commands,
    record_keys: MemoryKeys<AcceptanceRecordKey>,
    runner_keys: MemoryKeys<RunnerAttestationKey>,
    fake: Fake,
    real: bool,
    requirements: RunnerRequirements,
    wall_time: Duration,
}

impl W {
    /// The world with workflow wf-1 promoted to `verifying` at revision 1, policy and
    /// registry entry (for the chosen sandbox's environment) set up.
    fn new(name: &str, real: bool, command: RegisteredCommand) -> W {
        let dir = std::env::temp_dir().join(format!("writ-m307-{}-{name}", std::process::id()));
        let _ = std::fs::remove_dir_all(&dir);
        std::fs::create_dir_all(dir.join("work")).unwrap();
        let ledger = SqliteLedger::create(&dir.join("ledger.db")).unwrap();
        let effects =
            SqliteEffectStore::create(&dir.join("effects.db"), ledger.ledger_id()).unwrap();
        let runner = SqliteRunnerStore::create(&dir.join("runner.db"), ledger.ledger_id()).unwrap();
        let mut record_keys = MemoryKeys::new();
        record_keys.register(
            "acc-key-1",
            AcceptanceRecordKey::from_bytes(RECORD_KEY.to_vec()).unwrap(),
        );
        let mut runner_keys = MemoryKeys::new();
        runner_keys.register(
            "runner-key-1",
            RunnerAttestationKey::from_bytes(RUNNER_KEY.to_vec()).unwrap(),
        );
        let w = W {
            dir,
            ledger,
            effects,
            runner,
            active: ActivePolicy::new(PolicyState::loaded(bundle())),
            clock: TestClock::at(now()),
            key: PermitKey::from_bytes(vec![0x44; 32]).unwrap(),
            docs: Docs::default(),
            snapshots: Snapshots::default(),
            content: Content {
                files: Mutex::new(BTreeMap::from([("snap-1".to_string(), files())])),
                immutable: AtomicBool::new(true),
            },
            commands: Commands(Mutex::new(command)),
            record_keys,
            runner_keys,
            fake: Fake {
                result: Mutex::new(Ok(JobOutput {
                    exit_status: 0,
                    stdout: b"ok\n".to_vec(),
                    stderr: Vec::new(),
                })),
                runs: AtomicUsize::new(0),
            },
            real,
            requirements: RunnerRequirements::uncontained_for_disposable_tests(),
            wall_time: Duration::from_secs(20),
        };
        for (id, d) in [("snap-1", digest()), ("snap-2", sha('e'))] {
            w.snapshots.content.lock().unwrap().insert(id.into(), d);
        }
        w.admin(AdminRequest::CreateWorkflow {
            key: wf(),
            task_id: "task-1".into(),
            initial_state: "planning".into(),
            artifact_snapshot_id: "snap-1".into(),
            artifact_digest: digest(),
        })
        .unwrap();
        w.admin(AdminRequest::ActivateAcceptancePolicy {
            tenant_id: T.into(),
            project_id: P.into(),
            requirements: requirements(),
            effect_prerequisites: Vec::new(),
        })
        .unwrap();
        w.register(12, &w.environment());
        assert_eq!(
            w.promote(&transition("k0", "planning", "verifying", 0, None)),
            Ok(committed(1))
        );
        w
    }

    fn fake(name: &str) -> W {
        W::new(name, false, script("exit 0"))
    }

    fn sandbox(&self) -> &dyn Sandbox {
        if self.real {
            &LocalProcessSandbox
        } else {
            &self.fake
        }
    }

    fn environment(&self) -> String {
        self.sandbox().environment_digest()
    }

    fn register_entry(&self, entry: Value) {
        self.admin(AdminRequest::RegisterEvaluator { entry })
            .unwrap();
    }

    fn register(&self, revision: i64, environment: &str) {
        let command = self.commands.0.lock().unwrap().clone();
        self.register_entry(entry(revision, environment, &command));
    }

    fn admin(&self, request: AdminRequest) -> Result<(), Refusal> {
        Broker::new(
            &self.active,
            &self.clock,
            &self.ledger,
            &self.effects,
            &self.key,
        )
        .administer(&Operators, &alice(), &self.snapshots, request)
    }

    /// Run `f` with the broker carrying the runner.
    fn with_broker<R>(
        &self,
        f: impl FnOnce(&Broker<'_, SqliteLedger, SqliteEffectStore>) -> R,
    ) -> R {
        let clock = LedgerClock::new(&self.clock, &self.ledger);
        let runner = RunnerAdapter::new(
            &self.runner,
            RunnerSources {
                state: &self.ledger,
                commands: &self.commands,
                snapshots: &self.content,
                keys: &self.runner_keys,
                sandbox: self.sandbox(),
                clock: &clock,
            },
            RunnerConfig {
                requirements: self.requirements,
                limits: Limits {
                    wall_time: self.wall_time,
                    max_output_bytes: 1 << 16,
                },
                freshness: Duration::from_secs(600),
                work_root: self.dir.join("work"),
            },
        );
        let b = Broker::new(
            &self.active,
            &self.clock,
            &self.ledger,
            &self.effects,
            &self.key,
        )
        .with_runner(&runner);
        f(&b)
    }

    fn execute(&self, request: &Value) -> Execution {
        self.with_broker(|b| b.execute(&verify(request), StateRevision::new(1).unwrap(), None))
    }

    fn trust(&self) -> Trust<'_> {
        Trust {
            records: &self.docs,
            attestations: &self.runner,
            record_keys: &self.record_keys,
            runner_keys: &self.runner_keys,
            snapshots: &self.snapshots,
        }
    }

    fn promote(&self, request: &Value) -> Result<PromotionOutcome, Refusal> {
        self.with_broker(|b| b.promote(&self.trust(), &verify(request), None).result)
    }

    /// Execute a runner request with key `key` for record `record`; returns the result and
    /// the attestation id derived from its permit.
    fn run(&self, key: &str, record: &str) -> (Result<Executed, Refusal>, String) {
        let e = self.execute(&evaluate(key, record));
        let id = e
            .permit
            .as_ref()
            .map_or_else(String::new, |p| attestation_id_for(p.permit_id()));
        (e.result, id)
    }

    fn runner_count(&self, sql: &str) -> i64 {
        self.runner
            .db()
            .conn
            .query_row(sql, [], |r| r.get(0))
            .unwrap()
    }

    fn attestations(&self) -> i64 {
        self.runner_count("SELECT count(*) FROM attestations")
    }

    fn diagnostic(&self, permit: &str) -> Option<String> {
        self.runner.record(permit).unwrap().unwrap().diagnostic
    }

    fn receipt(&self, key: &str) -> String {
        self.ledger
            .db()
            .conn
            .query_row(
                "SELECT status FROM receipts WHERE idempotency_key = ?1",
                [key],
                |r| r.get(0),
            )
            .unwrap()
    }

    fn state(&self) -> (String, i64) {
        self.ledger
            .db()
            .conn
            .query_row(
                "SELECT state, revision FROM workflows WHERE workflow_id = 'wf-1'",
                [],
                |r| Ok((r.get(0)?, r.get(1)?)),
            )
            .unwrap()
    }

    fn work_empty(&self) -> bool {
        std::fs::read_dir(self.dir.join("work"))
            .unwrap()
            .next()
            .is_none()
    }
}

fn acceptance(code: AcceptanceCode) -> Refusal {
    Refusal::Acceptance(code)
}

// ---------------------------------------------------------------------------
// End to end.

#[test]
fn a_real_run_attests_and_its_attestation_satisfies_exactly_one_promotion() {
    let w = W::new("real", true, script("echo evaluated; exit 0"));
    let (result, att) = w.run("r1", "acc-1");
    assert_eq!(result, Ok(Executed::EffectCompleted));
    assert_eq!(att, "att-permit-1");
    assert_eq!(w.receipt("r1"), "observed_success");
    assert_eq!(w.attestations(), 1);
    assert!(w.work_empty());
    let run = w.runner.record("permit-1").unwrap().unwrap();
    assert_eq!(run.state, RunState::Applied);
    assert_eq!(run.attestation_id.as_deref(), Some(att.as_str()));
    // The attestation binds the real output.
    let doc = writ_broker::workflow::AttestationSource::attestation(&w.runner, T, P, &att)
        .unwrap()
        .unwrap();
    let out = JobOutput {
        exit_status: 0,
        stdout: b"evaluated\n".to_vec(),
        stderr: Vec::new(),
    };
    assert_eq!(
        doc.get("output_digest").and_then(Value::as_str),
        writ_broker::runner::output_digest(&out).as_deref()
    );
    assert_eq!(
        writ_broker::workflow::AttestationSource::attestation(&w.runner, T, "other", &att),
        Ok(None)
    );
    // The acceptance service issues the record naming the attestation; the promotion
    // verifies and consumes it.
    let env = w.environment();
    let snap = ("snap-1", digest());
    w.docs
        .put("acc-1", record("acc-1", 1, (snap.0, &snap.1), &att, &env));
    assert_eq!(
        w.promote(&accept("k1", "acc-1", 1, (snap.0, &snap.1))),
        Ok(committed(2))
    );
    assert_eq!(w.state(), ("accepted".into(), 2));
    // An identical execution retry replays without a second run; the permit is replayed.
    let (again, _) = w.run("r1", "acc-1");
    assert!(matches!(again, Ok(Executed::Replayed(_))));
    assert_eq!(w.runner_count("SELECT count(*) FROM runs"), 1);
    let permit = w.execute(&evaluate("r1", "acc-1")).permit;
    assert!(permit.is_none());
    // The attestation can never be used again (another record, after a direct test reset
    // of the workflow).
    w.ledger
        .db()
        .conn
        .execute("UPDATE workflows SET state = 'verifying', revision = 1", [])
        .unwrap();
    w.docs
        .put("acc-2", record("acc-2", 1, (snap.0, &snap.1), &att, &env));
    assert_eq!(
        w.promote(&accept("k2", "acc-2", 1, (snap.0, &snap.1))),
        Err(acceptance(AcceptanceCode::AttestationBindingMismatch))
    );
    let same_record = record("acc-1", 1, (snap.0, &snap.1), &att, &env);
    w.docs.put("acc-1", same_record);
    assert!(w
        .promote(&accept("k3", "acc-1", 1, (snap.0, &snap.1)))
        .is_err());
    assert_eq!(w.state(), ("verifying".into(), 1));
}

#[test]
fn a_presented_runner_permit_never_runs_again() {
    let w = W::fake("present");
    let e = w.execute(&evaluate("r1", "acc-1"));
    assert_eq!(e.result, Ok(Executed::EffectCompleted));
    let permit = e.permit.unwrap();
    let refusal = w.with_broker(|b| b.present_permit(&permit, &evaluate("r1", "acc-1")));
    assert_eq!(refusal, Refusal::PermitReplayed);
    assert_eq!(w.fake.runs.load(Ordering::SeqCst), 1);
}

#[test]
fn a_nonzero_exit_is_attested_and_refused_by_the_promotion() {
    let w = W::new("exit", true, script("echo failing >&2; exit 3"));
    let (result, att) = w.run("r1", "acc-1");
    assert_eq!(result, Ok(Executed::EffectCompleted));
    assert_eq!(w.attestations(), 1);
    let env = w.environment();
    let d = digest();
    w.docs
        .put("acc-1", record("acc-1", 1, ("snap-1", &d), &att, &env));
    assert_eq!(
        w.promote(&accept("k1", "acc-1", 1, ("snap-1", &d))),
        Err(acceptance(AcceptanceCode::AttestationFailed))
    );
    assert_eq!(w.state(), ("verifying".into(), 1));
}

#[test]
fn a_real_run_past_its_wall_time_is_killed_and_attests_nothing() {
    let mut w = W::new("timeout", true, script("sleep 30"));
    w.wall_time = Duration::from_secs(1);
    let (result, _) = w.run("r1", "acc-1");
    assert_eq!(result, Ok(Executed::EffectFailed));
    assert_eq!(w.receipt("r1"), "observed_failure");
    assert_eq!(
        w.diagnostic("permit-1").as_deref(),
        Some("RUNNER_JOB_TIMED_OUT")
    );
    assert_eq!(w.attestations(), 0);
    assert!(w.work_empty());
}

#[test]
fn production_requirements_refuse_the_local_sandbox() {
    let mut w = W::new("production", true, script("exit 0"));
    w.requirements = RunnerRequirements::production();
    let (result, _) = w.run("r1", "acc-1");
    assert_eq!(result, Ok(Executed::EffectFailed));
    assert_eq!(
        w.diagnostic("permit-1").as_deref(),
        Some("RUNNER_SANDBOX_INSUFFICIENT")
    );
    assert_eq!(w.attestations(), 0);
}

#[test]
fn untrusted_or_mismatched_inputs_leave_no_attestation() {
    type Mutate = fn(&mut W);
    let cases: Vec<(&str, Mutate, &str)> = vec![
        (
            "program",
            |w| w.commands.0.lock().unwrap().program = b"#!/bin/sh\nexit 0 \n".to_vec(),
            "RUNNER_COMMAND_DIGEST_MISMATCH",
        ),
        (
            "args",
            |w| w.commands.0.lock().unwrap().args.push("--skip".into()),
            "RUNNER_COMMAND_DIGEST_MISMATCH",
        ),
        (
            "content",
            |w| {
                let mut f = files();
                f[1].bytes = b"snapshot changed\n".to_vec();
                w.content.files.lock().unwrap().insert("snap-1".into(), f);
            },
            "ARTIFACT_INTEGRITY_VIOLATION",
        ),
        (
            "mutable",
            |w| w.content.immutable.store(false, Ordering::SeqCst),
            "ARTIFACT_INTEGRITY_UNAVAILABLE",
        ),
        (
            "revoked",
            |w| {
                w.admin(AdminRequest::RevokeEvaluator {
                    tenant_id: T.into(),
                    project_id: P.into(),
                    evaluator_id: "ci-runner".into(),
                })
                .unwrap()
            },
            "ACCEPTANCE_EVALUATOR_REVOKED",
        ),
        (
            "class",
            |w| {
                let e = entry(13, &sha('2'), &script("exit 0"));
                let e = set(&e, &["class"], json::s("evidence"));
                w.register_entry(set(&e, &["trust"], json::s("E")))
            },
            "EVALUATOR_BINDING_MISMATCH",
        ),
        (
            "criterion",
            |w| {
                let e = entry(13, &sha('2'), &script("exit 0"));
                w.register_entry(set(&e, &["criteria"], Value::Arr(vec![json::s("lint")])))
            },
            "EVALUATOR_BINDING_MISMATCH",
        ),
        (
            "environment",
            |w| w.register(13, &sha('7')),
            "RUNNER_ENVIRONMENT_MISMATCH",
        ),
        (
            "unknown-key",
            |w| {
                let e = entry(13, &sha('2'), &script("exit 0"));
                w.register_entry(set(&e, &["attestation", "key_id"], json::s("runner-key-9")))
            },
            "ATTESTATION_UNAUTHENTICATED",
        ),
        (
            "revoked-key",
            |w| w.runner_keys.revoke("runner-key-1"),
            "ATTESTATION_UNAUTHENTICATED",
        ),
        (
            "production",
            |w| w.requirements = RunnerRequirements::production(),
            "RUNNER_SANDBOX_INSUFFICIENT",
        ),
        (
            "job-failure",
            |w| *w.fake.result.lock().unwrap() = Err(JobFailure::Spawn),
            "RUNNER_JOB_SPAWN",
        ),
        (
            "job-timeout",
            |w| *w.fake.result.lock().unwrap() = Err(JobFailure::TimedOut),
            "RUNNER_JOB_TIMED_OUT",
        ),
    ];
    for (name, mutate, diagnostic) in cases {
        let mut w = W::fake(&format!("neg-{name}"));
        mutate(&mut w);
        let (result, _) = w.run("r1", "acc-1");
        assert_eq!(result, Ok(Executed::EffectFailed), "{name}");
        assert_eq!(w.receipt("r1"), "observed_failure", "{name}");
        assert_eq!(
            w.diagnostic("permit-1").as_deref(),
            Some(diagnostic),
            "{name}"
        );
        assert_eq!(w.attestations(), 0, "{name}");
        assert!(w.work_empty(), "{name}");
        let ran = usize::from(name.starts_with("job-"));
        assert_eq!(w.fake.runs.load(Ordering::SeqCst), ran, "{name}");
    }
}

#[test]
fn a_workflow_change_after_the_attestation_makes_the_promotion_refuse() {
    let w = W::fake("changed");
    let (result, att) = w.run("r1", "acc-1");
    assert_eq!(result, Ok(Executed::EffectCompleted));
    w.admin(AdminRequest::UpdateArtifact {
        key: wf(),
        artifact_snapshot_id: "snap-2".into(),
        artifact_digest: sha('e'),
    })
    .unwrap();
    assert_eq!(w.state(), ("verifying".into(), 2));
    let e = sha('e');
    w.docs
        .put("acc-1", record("acc-1", 2, ("snap-2", &e), &att, &sha('2')));
    assert_eq!(
        w.promote(&accept("k1", "acc-1", 2, ("snap-2", &e))),
        Err(acceptance(AcceptanceCode::AttestationBindingMismatch))
    );
    assert_eq!(w.state(), ("verifying".into(), 2));
}

#[test]
fn a_runner_request_without_a_runner_is_refused_before_reservation() {
    let w = W::fake("no-runner");
    let b = Broker::new(&w.active, &w.clock, &w.ledger, &w.effects, &w.key);
    let e = b.execute(
        &verify(&evaluate("r1", "acc-1")),
        StateRevision::new(1).unwrap(),
        None,
    );
    assert_eq!(e.result, Err(Refusal::RequestScopeMismatch));
    assert!(e.permit.is_none());
    assert_eq!(
        w.ledger
            .db()
            .conn
            .query_row(
                "SELECT count(*) FROM receipts WHERE idempotency_key = 'r1'",
                [],
                |r| { r.get::<_, i64>(0) }
            )
            .unwrap(),
        0
    );
}

// ---------------------------------------------------------------------------
// Storage faults.

const STAGES: [(fault::Stage, std::ffi::c_int, &str); 3] = [
    (
        fault::Stage::Begin,
        rusqlite::ffi::SQLITE_BUSY,
        "SQLITE_BUSY",
    ),
    (
        fault::Stage::Body,
        rusqlite::ffi::SQLITE_IOERR,
        "SQLITE_IOERR",
    ),
    (
        fault::Stage::Commit,
        rusqlite::ffi::SQLITE_FULL,
        "SQLITE_FULL",
    ),
];

fn unavailable(transaction: &str, code: &str) -> Refusal {
    Refusal::LedgerUnavailable(StoreError::Unavailable {
        category: format!("transaction {transaction}"),
        code: code.into(),
    })
}

#[test]
fn a_start_fault_runs_nothing_and_leaves_the_receipt_uncertain() {
    for (i, (stage, code, name)) in STAGES.into_iter().enumerate() {
        let w = W::fake(&format!("start-{i}"));
        w.runner.inject("runner_start", stage, code);
        let e = w.execute(&evaluate("r1", "acc-1"));
        assert_eq!(
            e.result,
            Err(unavailable("runner_start", name)),
            "{stage:?}"
        );
        let failure = e.evidence.failure().unwrap().1.clone();
        assert_eq!(failure.outcome_unknown, stage == fault::Stage::Commit);
        assert!(e.evidence.reconciliation_required());
        assert_eq!(w.fake.runs.load(Ordering::SeqCst), 0);
        assert_eq!(w.runner_count("SELECT count(*) FROM runs"), 0);
        assert_eq!(w.receipt("r1"), "dispatched");
        let (retry, _) = w.run("r1", "acc-1");
        assert_eq!(retry, Err(Refusal::ReconciliationRequired));
        assert_eq!(w.with_broker(|b| b.reconcile()), Ok(reconciled(0, 0, 1)));
        assert_eq!(w.receipt("r1"), "unknown_reconcile");
        assert_eq!(w.fake.runs.load(Ordering::SeqCst), 0);
    }
}

#[test]
fn a_fault_after_the_started_commit_never_reruns_and_reconciliation_records_nothing() {
    for (i, (stage, code, name)) in STAGES.into_iter().enumerate() {
        let w = W::fake(&format!("finish-{i}"));
        w.runner.inject("runner_finish", stage, code);
        let e = w.execute(&evaluate("r1", "acc-1"));
        assert_eq!(
            e.result,
            Err(unavailable("runner_finish", name)),
            "{stage:?}"
        );
        // The permit was consumed and the job ran: every finish failure reports an
        // unknown adapter outcome, so the possible progress includes the effect.
        let failure = e.evidence.failure().unwrap().1.clone();
        assert!(failure.outcome_unknown, "{stage:?}");
        assert_eq!(
            e.evidence.possible_progress().as_str(),
            "dispatched_with_effect"
        );
        assert!(e.evidence.reconciliation_required());
        assert_eq!(w.fake.runs.load(Ordering::SeqCst), 1);
        let record = w.runner.record("permit-1").unwrap().unwrap();
        assert_eq!(record.state, RunState::Started);
        assert_eq!(w.attestations(), 0);
        assert!(w.work_empty());
        // Never re-run: the retry needs reconciliation, the presented permit is not
        // applied, and reconciliation finds no completion evidence.
        let (retry, _) = w.run("r1", "acc-1");
        assert_eq!(retry, Err(Refusal::ReconciliationRequired));
        let permit = e.permit.unwrap();
        let presented = w.with_broker(|b| b.present_permit(&permit, &evaluate("r1", "acc-1")));
        assert_eq!(presented, Refusal::ReconciliationRequired);
        assert_eq!(w.with_broker(|b| b.reconcile()), Ok(reconciled(0, 0, 1)));
        assert_eq!(w.with_broker(|b| b.reconcile()), Ok(reconciled(0, 0, 0)));
        assert_eq!(w.receipt("r1"), "unknown_reconcile");
        assert_eq!(w.fake.runs.load(Ordering::SeqCst), 1);
        assert_eq!(
            w.runner.record("permit-1").unwrap().unwrap().state,
            RunState::Started
        );
    }
}

#[test]
fn reconciliation_finds_runner_evidence_for_a_runner_receipt() {
    let w = W::fake("reconcile");
    w.ledger
        .inject("observe", fault::Stage::Body, rusqlite::ffi::SQLITE_IOERR);
    let e = w.execute(&evaluate("r1", "acc-1"));
    assert_eq!(e.result, Err(unavailable("observe", "SQLITE_IOERR")));
    assert_eq!(w.receipt("r1"), "dispatched");
    assert_eq!(w.attestations(), 1);
    assert_eq!(w.with_broker(|b| b.reconcile()), Ok(reconciled(1, 0, 0)));
    assert_eq!(w.receipt("r1"), "observed_success");
    let (retry, _) = w.run("r1", "acc-1");
    assert!(matches!(retry, Ok(Executed::Replayed(_))));
    assert_eq!(w.fake.runs.load(Ordering::SeqCst), 1);
    // A failed run is evidence of an observed failure.
    let w = W::fake("reconcile-failed");
    *w.fake.result.lock().unwrap() = Err(JobFailure::Io);
    w.ledger
        .inject("observe", fault::Stage::Begin, rusqlite::ffi::SQLITE_BUSY);
    assert!(w.execute(&evaluate("r1", "acc-1")).result.is_err());
    assert_eq!(w.with_broker(|b| b.reconcile()), Ok(reconciled(0, 1, 0)));
    assert_eq!(w.receipt("r1"), "observed_failure");
}

#[test]
fn the_runner_store_is_owned_once_and_bound_to_its_ledger() {
    let w = W::fake("ownership");
    let path = w.dir.join("runner.db");
    let owned = SqliteRunnerStore::open(&path, w.ledger.ledger_id()).err();
    assert_eq!(
        owned,
        Some(StoreError::Unavailable {
            category: "runner".into(),
            code: "OWNED_ELSEWHERE".into()
        })
    );
    let W { runner, dir, .. } = w;
    drop(runner);
    let other = SqliteLedger::create(&dir.join("other.db")).unwrap();
    assert_eq!(
        SqliteRunnerStore::open(&path, other.ledger_id()).err(),
        Some(StoreError::Unavailable {
            category: "runner".into(),
            code: "LEDGER_MISMATCH".into()
        })
    );
    let reopened = SqliteRunnerStore::open(&path, &ledger_id_of(&dir));
    assert!(reopened.is_ok());
}

/// The ledger id stored in the world's ledger (read through a fresh open).
fn ledger_id_of(dir: &std::path::Path) -> super::LedgerId {
    let conn = rusqlite::Connection::open(dir.join("ledger.db")).unwrap();
    let id: String = conn
        .query_row("SELECT value FROM meta WHERE key = 'ledger_id'", [], |r| {
            r.get(0)
        })
        .unwrap();
    super::LedgerId::parse(&id).unwrap()
}
