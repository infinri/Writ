//! Runner adapter unit tests (M3-07) with an in-memory runner store, fake
//! sandbox and test keys only.

use std::collections::BTreeMap;
use std::os::unix::fs::PermissionsExt;
use std::path::PathBuf;
use std::sync::atomic::{AtomicUsize, Ordering};
use std::sync::Mutex;
use std::time::Duration;

use super::*;
use crate::acceptance::verify_attestation;
use crate::clock::FixedClock;
use crate::effects::{EffectAdapter, EffectResult, VerifiedEffect};
use crate::keys::{MemoryKeys, RunnerAttestationKey};
use crate::permit::VerifiedPermit;
use crate::refusal::Refusal;
use crate::store::{StoreError, TxFailure, TxStage};
use crate::test_support::with;
use crate::workflow::{SourceUnavailable, StoredEntry, StoredPolicy, StoredWorkflow, WorkflowKey};
use writ_kernel::json::{self, Digest, Value};
use writ_kernel::time::Timestamp;

const RUNNER_KEY: [u8; 32] = [0x6b; 32];
const T: &str = "t1";
const P: &str = "p1";
const ENV: char = '2';

fn sha(c: char) -> String {
    format!("sha256:{}", c.to_string().repeat(64))
}

fn now() -> Timestamp {
    Timestamp::parse_strict("2026-11-07T11:00:00Z").unwrap()
}

fn parse(text: &str) -> Value {
    json::parse(text.as_bytes()).unwrap()
}

fn files() -> Vec<SnapshotFile> {
    vec![
        SnapshotFile {
            path: "src/lib.rs".into(),
            bytes: b"pub fn f() {}\n".to_vec(),
        },
        SnapshotFile {
            path: "Cargo.toml".into(),
            bytes: b"[package]\n".to_vec(),
        },
    ]
}

fn command() -> RegisteredCommand {
    RegisteredCommand {
        command_id: "cargo-test".into(),
        program: b"#!/bin/sh\nexit 0\n".to_vec(),
        args: vec!["--quiet".into()],
        env: vec![("PATH".into(), "/usr/bin".into())],
    }
}

fn requirements() -> Value {
    parse(&format!(
        r#"{{"acceptance_policy_id":"accept-code-review","acceptance_policy_version":"3","acceptance_policy_bundle_sha":"{}","criteria":[{{"criterion_id":"build","required_class":"mechanical"}},{{"criterion_id":"docs","required_class":"evidence"}}]}}"#,
        sha('c')
    ))
}

fn entry() -> Value {
    let v = parse(include_str!(
        "../../../docs/contracts/proposed/examples/evaluator-registry-entry.valid.json"
    ));
    with(
        &v,
        &["command", "command_digest"],
        json::s(&command().digest().unwrap()),
    )
}

fn request() -> Value {
    parse(&format!(
        r#"{{"contract_version":"2.0","request_id":"req-r1","trace_id":"trace-1","tenant_id":"{T}","principal":{{"id":"agent:session-1","verified_by":"writ-host-adapter:fixture","authn_method":"os_peer_credential","roles":["agent"]}},"project_id":"{P}","session_id":"session-1","agent_id":null,"parent_session_id":null,"idempotency_key":"run-1","expected_state_revision":null,"policy_bundle_sha":null,"deadline_at":null,"action":{{"kind":"runner.evaluate","resource":{{"uri":"writ-workflow://wf-1","canonical_resource_id":"workflow:{T}/{P}/wf-1"}},"action_digest":"{}","attributes":{{"workflow_id":"wf-1","from_state":"verifying","to_state":"accepted","criterion_id":"build","acceptance_record_id":"acc-1","evaluator_id":"ci-runner"}},"requested_capabilities":[]}}}}"#,
        sha('b')
    ))
}

// ---------------------------------------------------------------------------
// Fakes.

#[derive(Default)]
struct MemoryStore {
    runs: Mutex<BTreeMap<String, (RunRecord, Option<String>)>>,
    fail_start: Mutex<Option<TxStage>>,
    fail_finish: Mutex<Option<TxStage>>,
}

fn failure(stage: TxStage) -> TxFailure {
    let error = StoreError::Unavailable {
        category: "transaction runner".into(),
        code: "SQLITE_IOERR".into(),
    };
    if stage == TxStage::Commit {
        TxFailure::commit_unknown(error)
    } else {
        TxFailure::new(stage, error)
    }
}

impl RunnerStore for MemoryStore {
    fn start(
        &self,
        verify: &mut crate::effects::Verify<'_>,
    ) -> Result<Result<VerifiedEffect, Refusal>, TxFailure> {
        let fault = self.fail_start.lock().unwrap().take();
        if fault == Some(TxStage::Begin) {
            return Err(failure(TxStage::Begin));
        }
        let effect = match verify() {
            Ok(e) => e,
            Err(r) => return Ok(Err(r)),
        };
        let mut runs = self.runs.lock().unwrap();
        if runs.contains_key(effect.permit_id()) {
            return Ok(Err(Refusal::PermitReplayed));
        }
        if let Some(stage) = fault {
            return Err(failure(stage));
        }
        runs.insert(
            effect.permit_id().to_string(),
            (
                RunRecord {
                    permit_id: effect.permit_id().into(),
                    tenant_id: effect.tenant_id().into(),
                    idempotency_key: effect.idempotency_key().into(),
                    request_fingerprint: effect.request_fingerprint().into(),
                    state: RunState::Started,
                    attestation_id: None,
                    diagnostic: None,
                },
                None,
            ),
        );
        Ok(Ok(effect))
    }

    fn finish(&self, outcome: &RunOutcome) -> Result<(), TxFailure> {
        if let Some(stage) = self.fail_finish.lock().unwrap().take() {
            return Err(failure(stage));
        }
        let mut runs = self.runs.lock().unwrap();
        let (record, doc) = runs
            .get_mut(outcome.permit_id())
            .filter(|(r, _)| r.state == RunState::Started)
            .ok_or_else(|| failure(TxStage::Body))?;
        match outcome.attestation() {
            Some(a) => {
                record.state = RunState::Applied;
                record.attestation_id = Some(a.attestation_id().into());
                *doc = Some(a.document().to_string());
            }
            None => {
                record.state = RunState::Failed;
                record.diagnostic = outcome.diagnostic().map(|d| d.code().to_string());
            }
        }
        Ok(())
    }

    fn inspect(
        &self,
        verify: &mut crate::effects::Verify<'_>,
    ) -> Result<Result<Option<RunRecord>, Refusal>, TxFailure> {
        let effect = match verify() {
            Ok(e) => e,
            Err(r) => return Ok(Err(r)),
        };
        Ok(Ok(self.record(effect.permit_id())?))
    }

    fn record(&self, permit_id: &str) -> Result<Option<RunRecord>, TxFailure> {
        Ok(self
            .runs
            .lock()
            .unwrap()
            .get(permit_id)
            .map(|(r, _)| r.clone()))
    }
}

impl MemoryStore {
    fn document(&self, permit_id: &str) -> Option<Value> {
        self.runs
            .lock()
            .unwrap()
            .get(permit_id)
            .and_then(|(_, d)| d.as_deref().map(parse))
    }

    fn attestations(&self) -> usize {
        self.runs
            .lock()
            .unwrap()
            .values()
            .filter(|(_, d)| d.is_some())
            .count()
    }
}

struct State {
    workflow: Mutex<Option<StoredWorkflow>>,
    policy: Mutex<Option<StoredPolicy>>,
    entry: Mutex<Option<StoredEntry>>,
    down: Mutex<bool>,
}

impl State {
    fn new() -> State {
        State {
            workflow: Mutex::new(Some(StoredWorkflow {
                task_id: "task-1".into(),
                state: "verifying".into(),
                revision: 7,
                artifact_snapshot_id: "snap-1".into(),
                artifact_digest: snapshot_digest(&files()).unwrap(),
                last_promotion: None,
            })),
            policy: Mutex::new(Some(StoredPolicy {
                requirements: json::canonical(&requirements()).unwrap(),
                effect_prerequisites: Vec::new(),
            })),
            entry: Mutex::new(Some(StoredEntry {
                entry: json::canonical(&entry()).unwrap(),
                revoked: false,
            })),
            down: Mutex::new(false),
        }
    }

    fn set_entry(&self, v: &Value) {
        *self.entry.lock().unwrap() = Some(StoredEntry {
            entry: json::canonical(v).unwrap(),
            revoked: false,
        });
    }

    fn up(&self) -> Result<(), SourceUnavailable> {
        if *self.down.lock().unwrap() {
            Err(SourceUnavailable)
        } else {
            Ok(())
        }
    }
}

impl RunnerState for State {
    fn workflow(&self, key: &WorkflowKey) -> Result<Option<StoredWorkflow>, SourceUnavailable> {
        self.up()?;
        assert_eq!(
            (key.tenant_id.as_str(), key.workflow_id.as_str()),
            (T, "wf-1")
        );
        Ok(self.workflow.lock().unwrap().clone())
    }

    fn acceptance_policy(
        &self,
        _: &str,
        _: &str,
    ) -> Result<Option<StoredPolicy>, SourceUnavailable> {
        self.up()?;
        Ok(self.policy.lock().unwrap().clone())
    }

    fn registry_entry(
        &self,
        _: &str,
        _: &str,
        evaluator: &str,
    ) -> Result<Option<StoredEntry>, SourceUnavailable> {
        self.up()?;
        Ok(if evaluator == "ci-runner" {
            self.entry.lock().unwrap().clone()
        } else {
            None
        })
    }
}

struct Commands {
    command: Mutex<Option<RegisteredCommand>>,
    /// Answer with the stored command whatever id is asked for.
    any_id: Mutex<bool>,
    down: Mutex<bool>,
}

impl CommandRegistry for Commands {
    fn command(
        &self,
        _: &str,
        _: &str,
        id: &str,
    ) -> Result<Option<RegisteredCommand>, SourceUnavailable> {
        if *self.down.lock().unwrap() {
            return Err(SourceUnavailable);
        }
        let any = *self.any_id.lock().unwrap();
        Ok(self
            .command
            .lock()
            .unwrap()
            .clone()
            .filter(|c| any || c.command_id == id))
    }
}

struct Snapshots {
    files: Mutex<Option<Vec<SnapshotFile>>>,
    immutable: Mutex<bool>,
    down: Mutex<bool>,
}

impl SnapshotContent for Snapshots {
    fn content(
        &self,
        _: &str,
        _: &str,
        id: &str,
    ) -> Result<Option<Vec<SnapshotFile>>, SourceUnavailable> {
        assert_eq!(id, "snap-1");
        if *self.down.lock().unwrap() {
            return Err(SourceUnavailable);
        }
        Ok(self.files.lock().unwrap().clone())
    }

    fn immutable(&self) -> bool {
        *self.immutable.lock().unwrap()
    }
}

/// What the fake sandbox saw of one job.
#[derive(Clone, Debug, PartialEq, Eq)]
struct Seen {
    workdir_mode: u32,
    program_mode: u32,
    program: Vec<u8>,
    lib: Vec<u8>,
    args: Vec<String>,
    env: Vec<(String, String)>,
    workdir: PathBuf,
}

struct FakeSandbox {
    guarantees: Mutex<Result<SandboxGuarantees, SandboxUnavailable>>,
    result: Mutex<Result<JobOutput, JobFailure>>,
    runs: AtomicUsize,
    seen: Mutex<Vec<Seen>>,
}

impl FakeSandbox {
    fn new() -> FakeSandbox {
        FakeSandbox {
            guarantees: Mutex::new(Ok(
                RunnerRequirements::uncontained_for_disposable_tests().sandbox()
            )),
            result: Mutex::new(Ok(JobOutput {
                exit_status: 0,
                stdout: b"ok\n".to_vec(),
                stderr: Vec::new(),
            })),
            runs: AtomicUsize::new(0),
            seen: Mutex::new(Vec::new()),
        }
    }
}

impl Sandbox for FakeSandbox {
    fn guarantees(&self) -> Result<SandboxGuarantees, SandboxUnavailable> {
        *self.guarantees.lock().unwrap()
    }

    fn environment_digest(&self) -> String {
        sha(ENV)
    }

    fn run(&self, job: &Job<'_>) -> Result<JobOutput, JobFailure> {
        self.runs.fetch_add(1, Ordering::SeqCst);
        let mode = |p: &std::path::Path| std::fs::metadata(p).unwrap().permissions().mode() & 0o777;
        self.seen.lock().unwrap().push(Seen {
            workdir_mode: mode(job.workdir),
            program_mode: mode(job.program),
            program: std::fs::read(job.program).unwrap(),
            lib: std::fs::read(job.workdir.join("src/lib.rs")).unwrap(),
            args: job.args.to_vec(),
            env: job.env.to_vec(),
            workdir: job.workdir.to_path_buf(),
        });
        assert!(job.program.starts_with(job.workdir));
        self.result.lock().unwrap().clone()
    }
}

// ---------------------------------------------------------------------------
// The world.

struct W {
    store: MemoryStore,
    state: State,
    commands: Commands,
    snapshots: Snapshots,
    keys: MemoryKeys<RunnerAttestationKey>,
    sandbox: FakeSandbox,
    clock: FixedClock,
    root: PathBuf,
    requirements: RunnerRequirements,
}

impl W {
    fn new(name: &str) -> W {
        let root =
            std::env::temp_dir().join(format!("writ-runner-unit-{}-{name}", std::process::id()));
        let _ = std::fs::remove_dir_all(&root);
        std::fs::create_dir_all(&root).unwrap();
        let mut keys = MemoryKeys::new();
        keys.register(
            "runner-key-1",
            RunnerAttestationKey::from_bytes(RUNNER_KEY.to_vec()).unwrap(),
        );
        W {
            store: MemoryStore::default(),
            state: State::new(),
            commands: Commands {
                command: Mutex::new(Some(command())),
                any_id: Mutex::new(false),
                down: Mutex::new(false),
            },
            snapshots: Snapshots {
                files: Mutex::new(Some(files())),
                immutable: Mutex::new(true),
                down: Mutex::new(false),
            },
            keys,
            sandbox: FakeSandbox::new(),
            clock: FixedClock::new(now()),
            root,
            requirements: RunnerRequirements::uncontained_for_disposable_tests(),
        }
    }

    fn runner(&self) -> RunnerAdapter<'_, MemoryStore> {
        RunnerAdapter::new(
            &self.store,
            RunnerSources {
                state: &self.state,
                commands: &self.commands,
                snapshots: &self.snapshots,
                keys: &self.keys,
                sandbox: &self.sandbox,
                clock: &self.clock,
            },
            RunnerConfig {
                requirements: self.requirements,
                limits: Limits {
                    wall_time: Duration::from_secs(5),
                    max_output_bytes: 1 << 16,
                },
                freshness: Duration::from_secs(600),
                work_root: self.root.clone(),
            },
        )
    }

    /// Apply permit `permit` for `request`.
    fn apply_with(
        &self,
        permit: &str,
        request: &Value,
    ) -> Result<Result<EffectResult, Refusal>, TxFailure> {
        self.runner().apply(&mut || {
            VerifiedEffect::new(
                VerifiedPermit {
                    permit_id: permit.into(),
                    request_fingerprint: "fp-1".into(),
                    policy_bundle_sha: sha('9'),
                    expires_at: now().plus_seconds(30),
                },
                request,
            )
        })
    }

    fn apply(&self, permit: &str) -> Result<Result<EffectResult, Refusal>, TxFailure> {
        self.apply_with(permit, &request())
    }

    fn runs(&self) -> usize {
        self.sandbox.runs.load(Ordering::SeqCst)
    }

    fn diagnostic(&self, permit: &str) -> Option<String> {
        self.store.record(permit).unwrap().unwrap().diagnostic
    }

    fn root_empty(&self) -> bool {
        std::fs::read_dir(&self.root).unwrap().next().is_none()
    }
}

// ---------------------------------------------------------------------------
// Tests.

#[test]
fn a_run_produces_an_authenticated_attestation_bound_to_trusted_state() {
    let w = W::new("happy");
    assert_eq!(w.apply("permit-1"), Ok(Ok(EffectResult::Applied)));
    assert_eq!(w.runs(), 1);
    let record = w.store.record("permit-1").unwrap().unwrap();
    assert_eq!(record.state, RunState::Applied);
    assert_eq!(record.attestation_id.as_deref(), Some("att-permit-1"));
    assert_eq!(attestation_id_for("permit-1"), "att-permit-1");
    let doc = w.store.document("permit-1").unwrap();
    let verified = verify_attestation(&doc, &w.keys, now()).unwrap();
    assert_eq!(verified.attestation_id(), "att-permit-1");
    let s = |k: &str| doc.get(k).and_then(Value::as_str).unwrap().to_string();
    let digest = snapshot_digest(&files()).unwrap();
    assert_eq!(s("artifact_digest"), digest);
    assert_eq!(s("artifact_snapshot_id"), "snap-1");
    assert_eq!(s("task_id"), "task-1");
    assert_eq!(s("canonical_target"), "workflow:t1/p1/wf-1");
    assert_eq!(s("acceptance_policy_bundle_sha"), sha('c'));
    assert_eq!(s("command_digest"), command().digest().unwrap());
    assert_eq!(s("environment_digest"), sha(ENV));
    assert_eq!(s("started_at"), "2026-11-07T11:00:00Z");
    assert_eq!(s("finished_at"), "2026-11-07T11:00:00Z");
    assert_eq!(s("fresh_until"), "2026-11-07T11:10:00Z");
    assert_eq!(
        doc.get("input_digests"),
        Some(&Value::Arr(vec![json::s(&digest)]))
    );
    assert_eq!(doc.get("evidence_digests"), Some(&Value::Arr(vec![])));
    assert_eq!(doc.get("state_revision"), Some(&Value::Int(7)));
    assert_eq!(doc.get("exit_status"), Some(&Value::Int(0)));
    let out = JobOutput {
        exit_status: 0,
        stdout: b"ok\n".to_vec(),
        stderr: Vec::new(),
    };
    assert_eq!(s("output_digest"), output_digest(&out).unwrap());
    // The job ran on the materialized private copy, which is gone afterwards.
    let seen = w.sandbox.seen.lock().unwrap()[0].clone();
    assert_eq!((seen.workdir_mode, seen.program_mode), (0o700, 0o500));
    assert_eq!(seen.program, command().program);
    assert_eq!(seen.lib, b"pub fn f() {}\n");
    assert_eq!((seen.args, seen.env), (command().args, command().env));
    assert!(seen.workdir.ends_with("run-permit-1"));
    assert!(w.root_empty());
    // Completion evidence for reconciliation.
    let evidence = w.runner().evidence("permit-1").unwrap().unwrap();
    assert_eq!(evidence.result, EffectResult::Applied);
    assert!(evidence.binds(T, "run-1", "fp-1"));
}

#[test]
fn a_permit_runs_at_most_once() {
    let w = W::new("replay");
    assert_eq!(w.apply("permit-1"), Ok(Ok(EffectResult::Applied)));
    assert_eq!(w.apply("permit-1"), Ok(Err(Refusal::PermitReplayed)));
    assert_eq!(w.runs(), 1);
    assert_eq!(w.store.attestations(), 1);
    // A refused verification records nothing and runs nothing.
    let refused = w.runner().apply(&mut || Err(Refusal::PermitExpired));
    assert_eq!(refused, Ok(Err(Refusal::PermitExpired)));
    assert_eq!(w.runs(), 1);
}

/// Each mutation of the trusted inputs refuses with `diagnostic`, runs
/// nothing and stores no attestation.
#[test]
fn untrusted_or_mismatched_inputs_never_run() {
    type Mutate = fn(&mut W);
    let cases: Vec<(&str, Mutate, &str)> = vec![
        (
            "program",
            |w| {
                let mut c = command();
                c.program = b"#!/bin/sh\nexit 1\n".to_vec();
                *w.commands.command.lock().unwrap() = Some(c);
            },
            "RUNNER_COMMAND_DIGEST_MISMATCH",
        ),
        (
            "args",
            |w| {
                let mut c = command();
                c.args.push("--skip".into());
                *w.commands.command.lock().unwrap() = Some(c);
            },
            "RUNNER_COMMAND_DIGEST_MISMATCH",
        ),
        (
            "unregistered",
            |w| *w.commands.command.lock().unwrap() = None,
            "RUNNER_COMMAND_UNREGISTERED",
        ),
        (
            "content",
            |w| {
                let mut f = files();
                f[0].bytes = b"pub fn g() {}\n".to_vec();
                *w.snapshots.files.lock().unwrap() = Some(f);
            },
            "ARTIFACT_INTEGRITY_VIOLATION",
        ),
        (
            "snapshot-unknown",
            |w| *w.snapshots.files.lock().unwrap() = None,
            "ARTIFACT_INTEGRITY_VIOLATION",
        ),
        (
            "reserved-path",
            |w| {
                let mut f = files();
                f.push(SnapshotFile {
                    path: PROGRAM_NAME.into(),
                    bytes: vec![],
                });
                *w.snapshots.files.lock().unwrap() = Some(f);
            },
            "RUNNER_SNAPSHOT_INVALID",
        ),
        (
            "mutable",
            |w| *w.snapshots.immutable.lock().unwrap() = false,
            "ARTIFACT_INTEGRITY_UNAVAILABLE",
        ),
        (
            "revoked",
            |w| {
                let mut e = w.state.entry.lock().unwrap();
                e.as_mut().unwrap().revoked = true;
            },
            "ACCEPTANCE_EVALUATOR_REVOKED",
        ),
        (
            "unregistered-evaluator",
            |w| *w.state.entry.lock().unwrap() = None,
            "EVALUATOR_UNREGISTERED",
        ),
        (
            "scope",
            |w| {
                w.state
                    .set_entry(&with(&entry(), &["scope", "project_id"], json::s("p2")))
            },
            "EVALUATOR_BINDING_MISMATCH",
        ),
        (
            "class",
            |w| {
                let e = with(&entry(), &["class"], json::s("evidence"));
                w.state.set_entry(&with(&e, &["trust"], json::s("E")))
            },
            "EVALUATOR_BINDING_MISMATCH",
        ),
        (
            "criterion",
            |w| {
                w.state.set_entry(&with(
                    &entry(),
                    &["criteria"],
                    Value::Arr(vec![json::s("lint")]),
                ))
            },
            "EVALUATOR_BINDING_MISMATCH",
        ),
        (
            "environment",
            |w| {
                w.state
                    .set_entry(&with(&entry(), &["environment_digest"], json::s(&sha('7'))))
            },
            "RUNNER_ENVIRONMENT_MISMATCH",
        ),
        (
            "unknown-key",
            |w| {
                w.state.set_entry(&with(
                    &entry(),
                    &["attestation", "key_id"],
                    json::s("runner-key-9"),
                ))
            },
            "ATTESTATION_UNAUTHENTICATED",
        ),
        (
            "revoked-key",
            |w| w.keys.revoke("runner-key-1"),
            "ATTESTATION_UNAUTHENTICATED",
        ),
        (
            "key-ring-down",
            |w| w.keys.set_unavailable(true),
            "ATTESTATION_KEY_UNAVAILABLE",
        ),
        (
            "production",
            |w| w.requirements = RunnerRequirements::production(),
            "RUNNER_SANDBOX_INSUFFICIENT",
        ),
        (
            "sandbox-down",
            |w| *w.sandbox.guarantees.lock().unwrap() = Err(SandboxUnavailable),
            "RUNNER_SANDBOX_UNAVAILABLE",
        ),
        (
            "workflow-state",
            |w| {
                let mut wf = w.state.workflow.lock().unwrap();
                wf.as_mut().unwrap().state = "planning".into();
            },
            "RUNNER_WORKFLOW_STATE",
        ),
        (
            "workflow-unknown",
            |w| *w.state.workflow.lock().unwrap() = None,
            "WORKFLOW_UNKNOWN",
        ),
        (
            "policy",
            |w| *w.state.policy.lock().unwrap() = None,
            "ACCEPTANCE_POLICY_UNAVAILABLE",
        ),
        (
            "state-down",
            |w| *w.state.down.lock().unwrap() = true,
            "RUNNER_STATE_UNAVAILABLE",
        ),
        (
            "clock",
            |w| w.clock.fail(crate::clock::ClockError::Overflow),
            "TIME_UNAVAILABLE",
        ),
        (
            "work-root",
            |w| w.root = w.root.join("missing"),
            "RUNNER_WORKDIR_UNAVAILABLE",
        ),
    ];
    refused_cases("neg", cases);
}

/// A named mutation of the world and the diagnostic it must produce.
type Case = (&'static str, fn(&mut W), &'static str);

/// Run each case on a fresh world: the mutation makes the run fail with
/// `diagnostic`, nothing runs and no attestation is stored.
fn refused_cases(prefix: &str, cases: Vec<Case>) {
    for (name, mutate, diagnostic) in cases {
        let mut w = W::new(&format!("{prefix}-{name}"));
        mutate(&mut w);
        refused(&w, name, diagnostic);
    }
}

/// `permit-1` fails with `diagnostic`, runs nothing, stores no attestation
/// and stays consumed.
fn refused(w: &W, name: &str, diagnostic: &str) {
    assert_eq!(w.apply("permit-1"), Ok(Ok(EffectResult::Failed)), "{name}");
    assert_eq!(
        w.diagnostic("permit-1").as_deref(),
        Some(diagnostic),
        "{name}"
    );
    assert_eq!(w.runs(), 0, "{name}");
    assert_eq!(w.store.attestations(), 0, "{name}");
    let evidence = w.runner().evidence("permit-1").unwrap().unwrap();
    assert_eq!(evidence.result, EffectResult::Failed, "{name}");
    // The permit is consumed: it never runs, even after the input is fixed.
    assert_eq!(
        w.apply("permit-1"),
        Ok(Err(Refusal::PermitReplayed)),
        "{name}"
    );
}

#[test]
fn the_request_names_only_and_exactly_what_to_evaluate() {
    let base = request();
    let attrs = ["action", "attributes"];
    let cases = [
        with(&base, &["action", "kind"], json::s("file.write")),
        with(
            &base,
            &["action", "resource", "canonical_resource_id"],
            json::s("workflow:t1/p1/wf-2"),
        ),
        with(
            &base,
            &["action", "attributes", "artifact_digest"],
            json::s(&sha('b')),
        ),
        with(
            &base,
            &["action", "attributes", "criterion_id"],
            Value::Int(1),
        ),
        with(
            &base,
            &["action", "attributes", "to_state"],
            json::s("verifying"),
        ),
        with(
            &base,
            &["action", "requested_capabilities"],
            Value::Arr(vec![json::s("network")]),
        ),
        with(&base, &attrs, Value::Obj(Default::default())),
    ];
    for (i, req) in cases.iter().enumerate() {
        let w = W::new(&format!("req-{i}"));
        assert_eq!(
            w.apply_with("permit-1", req),
            Ok(Ok(EffectResult::Failed)),
            "{i}"
        );
        assert_eq!(
            w.diagnostic("permit-1").as_deref(),
            Some("RUNNER_REQUEST_INVALID"),
            "{i}"
        );
        assert_eq!(w.runs(), 0);
    }
    // A criterion the active policy does not require mechanically.
    let w = W::new("req-docs");
    let docs = with(
        &base,
        &["action", "attributes", "criterion_id"],
        json::s("docs"),
    );
    assert_eq!(
        w.apply_with("permit-1", &docs),
        Ok(Ok(EffectResult::Failed))
    );
    assert_eq!(
        w.diagnostic("permit-1").as_deref(),
        Some("RUNNER_CRITERION_NOT_REQUIRED")
    );
}

#[test]
fn job_failures_produce_no_attestation_and_a_nonzero_exit_is_attested() {
    for (failure, diagnostic) in [
        (JobFailure::TimedOut, "RUNNER_JOB_TIMED_OUT"),
        (JobFailure::OutputExceeded, "RUNNER_JOB_OUTPUT_EXCEEDED"),
        (JobFailure::Spawn, "RUNNER_JOB_SPAWN"),
        (JobFailure::Signaled, "RUNNER_JOB_SIGNALED"),
        (JobFailure::Io, "RUNNER_JOB_IO"),
        (JobFailure::Unavailable, "RUNNER_JOB_UNAVAILABLE"),
    ] {
        let w = W::new(&format!("job-{diagnostic}"));
        *w.sandbox.result.lock().unwrap() = Err(failure);
        assert_eq!(w.apply("permit-1"), Ok(Ok(EffectResult::Failed)));
        assert_eq!(w.diagnostic("permit-1").as_deref(), Some(diagnostic));
        assert_eq!((w.runs(), w.store.attestations()), (1, 0));
        assert!(w.root_empty(), "work directory removed after {diagnostic}");
    }
    let w = W::new("job-exit");
    *w.sandbox.result.lock().unwrap() = Ok(JobOutput {
        exit_status: 3,
        stdout: Vec::new(),
        stderr: b"failed\n".to_vec(),
    });
    assert_eq!(w.apply("permit-1"), Ok(Ok(EffectResult::Applied)));
    let doc = w.store.document("permit-1").unwrap();
    assert_eq!(doc.get("exit_status"), Some(&Value::Int(3)));
    assert!(verify_attestation(&doc, &w.keys, now()).is_ok());
}

#[test]
fn storage_failures_never_run_twice_and_a_started_run_is_no_evidence() {
    // T-start fails at begin, in the body or at commit: nothing ran.
    for stage in [TxStage::Begin, TxStage::Body, TxStage::Commit] {
        let w = W::new(&format!("start-{}", stage.as_str()));
        *w.store.fail_start.lock().unwrap() = Some(stage);
        let failed = w.apply("permit-1").unwrap_err();
        assert_eq!(failed.stage, stage);
        assert_eq!(failed.outcome_unknown, stage == TxStage::Commit);
        assert_eq!(w.runs(), 0);
        assert_eq!(w.store.record("permit-1").unwrap(), None);
    }
    // T-finish fails after the job: the run stays started, is never run
    // again, and is no completion evidence (the receipt stays uncertain).
    for stage in [TxStage::Begin, TxStage::Body, TxStage::Commit] {
        let w = W::new(&format!("finish-{}", stage.as_str()));
        *w.store.fail_finish.lock().unwrap() = Some(stage);
        assert_eq!(w.apply("permit-1").unwrap_err().stage, stage);
        assert_eq!(w.runs(), 1);
        let record = w.store.record("permit-1").unwrap().unwrap();
        assert_eq!(record.state, RunState::Started);
        assert_eq!(record.evidence(), None);
        assert_eq!(w.runner().evidence("permit-1"), Ok(None));
        let inspected = w.runner().inspect(&mut || {
            VerifiedEffect::new(
                VerifiedPermit {
                    permit_id: "permit-1".into(),
                    request_fingerprint: "fp-1".into(),
                    policy_bundle_sha: sha('9'),
                    expires_at: now().plus_seconds(30),
                },
                &request(),
            )
        });
        assert_eq!(inspected, Ok(Ok(None)));
        assert_eq!(w.apply("permit-1"), Ok(Err(Refusal::PermitReplayed)));
        assert_eq!(w.runs(), 1);
        assert_eq!(w.store.attestations(), 0);
        assert!(w.root_empty());
    }
}

#[test]
fn produced_attestations_name_digests_never_supplied_by_the_request() {
    // The program is read back from the work directory and compared with the
    // registered bytes; the attestation's command digest is the registry's.
    let w = W::new("digests");
    assert_eq!(w.apply("permit-1"), Ok(Ok(EffectResult::Applied)));
    let doc = w.store.document("permit-1").unwrap();
    let program_digest = Digest::of_bytes(&command().program);
    assert!(command().digest().unwrap() != program_digest.as_str());
    assert_eq!(
        doc.get("command_digest").and_then(Value::as_str),
        entry()
            .get("command")
            .and_then(|c| c.get("command_digest"))
            .and_then(Value::as_str)
    );
}

#[test]
fn command_digest_refuses_ambiguous_environments() {
    let with = |env: &[(&str, &str)]| RegisteredCommand {
        env: env
            .iter()
            .map(|(k, v)| (k.to_string(), v.to_string()))
            .collect(),
        ..command()
    };
    assert!(with(&[("A", "1"), ("B", "2")]).digest().is_some());
    assert_eq!(
        with(&[("A", "1"), ("B", "2")]).digest(),
        with(&[("B", "2"), ("A", "1")]).digest()
    );
    for bad in [
        &[("A", "1"), ("A", "2")][..],
        &[("", "1")],
        &[("A=B", "1")],
        &[("A\0", "1")],
    ] {
        assert_eq!(with(bad).digest(), None, "{bad:?}");
    }
}

/// Each binding of the registry entry is checked on its own: one field
/// changed at a time refuses, runs nothing and stores no attestation.
#[test]
fn every_registry_binding_is_checked_on_its_own() {
    let cases: Vec<Case> = vec![
        (
            "evaluator-id",
            |w| {
                w.state
                    .set_entry(&with(&entry(), &["evaluator_id"], json::s("other-runner")))
            },
            "EVALUATOR_BINDING_MISMATCH",
        ),
        (
            "scope-tenant",
            |w| {
                w.state
                    .set_entry(&with(&entry(), &["scope", "tenant_id"], json::s("t2")))
            },
            "EVALUATOR_BINDING_MISMATCH",
        ),
        (
            "scope-project",
            |w| {
                w.state
                    .set_entry(&with(&entry(), &["scope", "project_id"], json::s("p2")))
            },
            "EVALUATOR_BINDING_MISMATCH",
        ),
        // The contract itself ties trust to class and fixes the algorithm, so
        // these entries are invalid before the adapter's own binding checks
        // (which remain as defence in depth) are reached.
        (
            "trust-alone",
            |w| w.state.set_entry(&with(&entry(), &["trust"], json::s("E"))),
            "EVALUATOR_REGISTRY_UNAVAILABLE",
        ),
        (
            "attestation-alg",
            |w| {
                w.state.set_entry(&with(
                    &entry(),
                    &["attestation", "alg"],
                    json::s("hmac-sha512"),
                ))
            },
            "EVALUATOR_REGISTRY_UNAVAILABLE",
        ),
        (
            "status-revoked-flag-active",
            |w| {
                let e = with(&entry(), &["status"], json::s("revoked"));
                w.state
                    .set_entry(&with(&e, &["revoked_at"], json::s("2026-11-02T00:00:00Z")));
                assert!(!w.state.entry.lock().unwrap().as_ref().unwrap().revoked);
            },
            "ACCEPTANCE_EVALUATOR_REVOKED",
        ),
        (
            "registered-command-id",
            |w| {
                // The registry answers with a command whose id differs from the
                // entry's but whose digest the entry names: only the id check
                // can refuse it.
                let other = RegisteredCommand {
                    command_id: "other-command".into(),
                    ..command()
                };
                w.state.set_entry(&with(
                    &entry(),
                    &["command", "command_digest"],
                    json::s(&other.digest().unwrap()),
                ));
                *w.commands.command.lock().unwrap() = Some(other);
                *w.commands.any_id.lock().unwrap() = true;
            },
            "RUNNER_COMMAND_DIGEST_MISMATCH",
        ),
    ];
    refused_cases("binding", cases);
}

/// Invalid snapshot paths are refused before trusted time is read (the clock
/// fails, so a check that let them through would report `TIME_UNAVAILABLE`)
/// and nothing is written anywhere.
#[test]
fn invalid_snapshot_paths_are_refused_before_anything_is_written() {
    let abs = std::env::temp_dir().join(format!("writ-runner-abs-{}", std::process::id()));
    let _ = std::fs::remove_file(&abs);
    let bad = [
        "../escape".to_string(),
        abs.display().to_string(),
        "a//b".into(),
        "a/./b".into(),
        "./a".into(),
        "a/".into(),
        "src/lib.rs".into(),
        PROGRAM_NAME.into(),
        format!("{PROGRAM_NAME}/x"),
    ];
    for (i, path) in bad.into_iter().enumerate() {
        let mut w = W::new(&format!("path-{i}"));
        w.clock.fail(crate::clock::ClockError::Overflow);
        let mut f = files();
        f.push(SnapshotFile {
            path: path.clone(),
            bytes: b"x".to_vec(),
        });
        *w.snapshots.files.lock().unwrap() = Some(f);
        refused(&w, &path, "RUNNER_SNAPSHOT_INVALID");
        assert!(w.root_empty(), "{path}");
        assert!(!w.root.parent().unwrap().join("escape").exists(), "{path}");
        assert!(!abs.exists(), "{path}");
    }
}

/// A file and a directory of the same name fail closed during
/// materialization, whichever comes first, and the work directory is removed.
#[test]
fn a_file_and_directory_collision_fails_closed() {
    let file = |p: &str| SnapshotFile {
        path: p.into(),
        bytes: b"x".to_vec(),
    };
    for (i, (order, diagnostic)) in [
        // The file exists when its name is needed as a directory.
        (vec![file("a"), file("a/b")], "RUNNER_SNAPSHOT_INVALID"),
        // The directory exists when the file is created (exclusive create).
        (vec![file("a/b"), file("a")], "RUNNER_WORKDIR_UNAVAILABLE"),
    ]
    .into_iter()
    .enumerate()
    {
        assert!(snapshot_digest(&order).is_some());
        let w = W::new(&format!("collision-{i}"));
        *w.snapshots.files.lock().unwrap() = Some(order);
        refused(&w, diagnostic, diagnostic);
        assert!(w.root_empty(), "{diagnostic}");
    }
}

#[test]
fn unavailable_or_invalid_sources_never_run() {
    let cases: Vec<Case> = vec![
        (
            "snapshot-content-down",
            |w| *w.snapshots.down.lock().unwrap() = true,
            "ARTIFACT_INTEGRITY_UNAVAILABLE",
        ),
        (
            "command-registry-down",
            |w| *w.commands.down.lock().unwrap() = true,
            "RUNNER_COMMAND_REGISTRY_UNAVAILABLE",
        ),
        (
            "policy-not-json",
            |w| {
                w.state
                    .policy
                    .lock()
                    .unwrap()
                    .as_mut()
                    .unwrap()
                    .requirements = "{not json".into()
            },
            "ACCEPTANCE_POLICY_UNAVAILABLE",
        ),
        (
            "policy-not-requirements",
            |w| {
                w.state
                    .policy
                    .lock()
                    .unwrap()
                    .as_mut()
                    .unwrap()
                    .requirements = r#"{"criteria":[]}"#.into()
            },
            "ACCEPTANCE_POLICY_UNAVAILABLE",
        ),
        (
            "entry-not-json",
            |w| w.state.entry.lock().unwrap().as_mut().unwrap().entry = "{not json".into(),
            "EVALUATOR_REGISTRY_UNAVAILABLE",
        ),
        (
            "entry-not-an-entry",
            |w| {
                let Value::Obj(mut m) = entry() else {
                    panic!("an entry is an object")
                };
                m.remove("command");
                w.state.set_entry(&Value::Obj(m))
            },
            "EVALUATOR_REGISTRY_UNAVAILABLE",
        ),
    ];
    refused_cases("source", cases);
}

#[test]
fn an_unusable_work_directory_never_runs() {
    // A pre-existing run directory is never reused, and is left as it was.
    let w = W::new("workdir-exists");
    let existing = w.root.join("run-permit-1");
    std::fs::create_dir(&existing).unwrap();
    std::fs::write(existing.join("planted"), b"x").unwrap();
    refused(&w, "exists", "RUNNER_WORKDIR_UNAVAILABLE");
    assert_eq!(std::fs::read(existing.join("planted")).unwrap(), b"x");
    assert_eq!(std::fs::read_dir(&existing).unwrap().count(), 1);
    // A work root that is a symbolic link to a directory.
    let mut w = W::new("workdir-symlink");
    let target = w.root.join("target");
    std::fs::create_dir(&target).unwrap();
    let link = w.root.join("link");
    std::os::unix::fs::symlink(&target, &link).unwrap();
    w.root = link;
    refused(&w, "symlink", "RUNNER_WORKDIR_UNAVAILABLE");
    assert_eq!(std::fs::read_dir(&target).unwrap().count(), 0);
    // A work root that is a regular file.
    let mut w = W::new("workdir-file");
    let file = w.root.join("file");
    std::fs::write(&file, b"").unwrap();
    w.root = file;
    refused(&w, "file", "RUNNER_WORKDIR_UNAVAILABLE");
}
