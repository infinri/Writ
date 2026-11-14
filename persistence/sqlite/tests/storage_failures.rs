//! Storage failures on the production ledger (M3-08; D-037 items 7 and 8; M1 ledger tests
//! `test_read_only_file`, `test_read_only_ledger_dir`,
//! `test_unwritable_ledger_fails_closed_after_kill`).
//!
//! - A read-only ledger file, a read-only ledger directory, and a ledger made unwritable
//!   after its broker process was killed with SIGKILL: opening (or creating) fails closed and
//!   nothing in the ledger changes. Each test first proves, under the identity running the
//!   test, that the permission failure is real: it attempts the write directly and requires
//!   `PermissionDenied`. With elevated access (for example root, or `CAP_DAC_OVERRIDE`) that
//!   write succeeds and the test fails loudly instead of passing on a chmod that enforces
//!   nothing (D-037 item 8).
//! - Fully unavailable storage: every ledger operation fails (the refusal audit and the
//!   trusted-time floor included), injected by a wrapper at the storage boundary over a real
//!   ledger. This is a total outage of the ledger, unlike the adapter's C4 `ledger: down`
//!   (partial: lifecycle transactions only). The broker answers fail closed from its
//!   production paths (`TIME_UNAVAILABLE` where trusted time is needed first,
//!   `LEDGER_UNAVAILABLE` for reconciliation), writes no audit, applies no effect and changes
//!   no state, and the persisted time floor is unchanged.
//!
//! Real processes and disposable temporary directories only. The tests are serialized:
//! one spawns and kills a child process, and a fork duplicates every open descriptor of
//! this process (including another test's ownership lock) until the child execs.

#![allow(clippy::unwrap_used, clippy::expect_used, clippy::panic)]

use std::collections::BTreeMap;
use std::fs::{self, OpenOptions};
use std::io::{BufRead, BufReader, ErrorKind, Write};
use std::os::unix::fs::PermissionsExt;
use std::os::unix::process::ExitStatusExt;
use std::path::{Path, PathBuf};
use std::process::{Command, Stdio};
use std::sync::mpsc;
use std::sync::Mutex;
use std::time::Duration;

use rusqlite::{Connection, OpenFlags};
use std::sync::atomic::{AtomicU64, Ordering};

use writ_broker::approval_store::{
    approval_request_for, ApprovalLedger, BootstrapEnrollment, CeremonyBegin, CeremonyFinish,
    CeremonyOp, NewEnrollment, NewRole, OperatorSession, RegistrationBegin, RegistrationFinish,
    RegistrationLedger,
};
use writ_broker::authorize::authorize;
use writ_broker::clock::{
    observe, ClockError, ClockEvent, Drift, EffectiveClock, ObservedTime, Reading,
};
use writ_broker::execute::{
    Broker, DispatchIntent, Dispatched, Executed, FailureRecord, Observation, PendingReceipt,
    Reconciled, Resolution,
};
use writ_broker::grants::{
    approval_grant_label, Approval, ApprovalAuthority, ApprovalPresentation, ApproverRefusal,
    AuthenticatedApprover, GrantInsert, GrantLabel, NewGrant, ReserveOutcome, ReserveRequest,
    Verdict,
};
use writ_broker::human::{
    ActiveCredentials, ApprovalChannel, CeremonyStart, CeremonyState, HumanVerifier, Permission,
    RegistrationBinding, RegistrationStart, RegistrationVerifier, RelyingParty, SubjectId,
    VerifiedAssertion, VerifiedRegistration, VerifierCapability, VerifierError, ES256,
    WEBAUTHN_PASSKEY,
};
use writ_broker::ingress::{verify_ingress, Seal, VerifiedRequest};
use writ_broker::keys::{IngressKey, MemoryKeyRing, PermitKey};
use writ_broker::policy::{ActivePolicy, ExclusiveHold, PolicyState};
use writ_broker::refusal::Refusal;
use writ_broker::store::{Timestamp, TxFailure, TxStage};
use writ_broker::writ_kernel::contracts::StateRevision;
use writ_broker::writ_kernel::json::{self, Digest, Value};
use writ_broker::writ_kernel::policy::load_bundle;
use writ_ledger_sqlite::{
    BootstrapSession, ExecuteLedger, GrantLedger, LedgerStore, SqliteEffectStore, SqliteLedger,
    StoreError,
};

const CHILD: &str = "WRIT_STORAGE_FAILURE_CHILD";
const ADAPTER: &str = "writ-host-adapter:fixture";
const SEED: u64 = 20_261_113;

static SERIAL: Mutex<()> = Mutex::new(());

fn serial() -> std::sync::MutexGuard<'static, ()> {
    SERIAL
        .lock()
        .unwrap_or_else(std::sync::PoisonError::into_inner)
}

fn tmp(name: &str) -> PathBuf {
    let d = std::env::temp_dir().join(format!("writ-storage-fail-{}-{name}", std::process::id()));
    if d.exists() {
        restore(&d);
        let _ = fs::remove_dir_all(&d);
    }
    fs::create_dir_all(&d).unwrap();
    d
}

/// Make everything under `d` (and `d`) writable again so the directory can be removed.
fn restore(d: &Path) {
    let _ = fs::set_permissions(d, fs::Permissions::from_mode(0o755));
    if let Ok(entries) = fs::read_dir(d) {
        for e in entries.flatten() {
            let _ = fs::set_permissions(e.path(), fs::Permissions::from_mode(0o644));
        }
    }
}

fn chmod(p: &Path, mode: u32) {
    fs::set_permissions(p, fs::Permissions::from_mode(mode)).unwrap();
}

/// Proof that the identity running this test cannot write `file`: a direct write open
/// must fail with `PermissionDenied`. Anything else (elevated access) fails the test.
fn prove_file_unwritable(file: &Path) {
    match OpenOptions::new().write(true).open(file) {
        Err(e) if e.kind() == ErrorKind::PermissionDenied => {}
        Err(e) => panic!(
            "cannot prove {} is unwritable: write open failed with {e:?}, not PermissionDenied",
            file.display()
        ),
        Ok(_) => panic!(
            "PERMISSION PRECONDITION NOT MET: {} has mode {:o} but the test identity (uid {}) \
             opened it for writing; it has elevated file access (root or CAP_DAC_OVERRIDE), so \
             this test cannot exercise a read-only ledger. Run it as an unprivileged user.",
            file.display(),
            fs::metadata(file).unwrap().permissions().mode() & 0o777,
            uid()
        ),
    }
}

/// Proof that the identity running this test cannot create files in `dir`.
fn prove_dir_unwritable(dir: &Path) {
    let probe = dir.join("permission-probe");
    match OpenOptions::new().write(true).create_new(true).open(&probe) {
        Err(e) if e.kind() == ErrorKind::PermissionDenied => {}
        Err(e) => panic!(
            "cannot prove {} is unwritable: create failed with {e:?}, not PermissionDenied",
            dir.display()
        ),
        Ok(_) => {
            let _ = fs::remove_file(&probe);
            panic!(
                "PERMISSION PRECONDITION NOT MET: directory {} has mode {:o} but the test \
                 identity (uid {}) created a file in it; it has elevated file access (root or \
                 CAP_DAC_OVERRIDE), so this test cannot exercise a read-only ledger directory. \
                 Run it as an unprivileged user.",
                dir.display(),
                fs::metadata(dir).unwrap().permissions().mode() & 0o777,
                uid()
            )
        }
    }
}

fn uid() -> String {
    fs::read_to_string("/proc/self/status")
        .ok()
        .and_then(|s| {
            s.lines()
                .find(|l| l.starts_with("Uid:"))
                .map(|l| l.split_whitespace().nth(1).unwrap_or("?").to_string())
        })
        .unwrap_or_else(|| "?".into())
}

/// Every file in `dir` with its exact bytes.
fn files(dir: &Path) -> BTreeMap<String, Vec<u8>> {
    fs::read_dir(dir)
        .unwrap()
        .map(|e| {
            let e = e.unwrap();
            (
                e.file_name().to_string_lossy().into_owned(),
                fs::read(e.path()).unwrap(),
            )
        })
        .collect()
}

/// Every row of every table, read through a separate read-only connection at a quiescent
/// point (inspection only; the owner, if any, is idle).
fn dump(db: &Path) -> Vec<String> {
    let c = Connection::open_with_flags(db, OpenFlags::SQLITE_OPEN_READ_ONLY).unwrap();
    let tables: Vec<String> = c
        .prepare("SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name")
        .unwrap()
        .query_map([], |r| r.get(0))
        .unwrap()
        .collect::<Result<_, _>>()
        .unwrap();
    let mut out = Vec::new();
    for t in tables {
        let mut s = c.prepare(&format!("SELECT * FROM \"{t}\"")).unwrap();
        let n = s.column_count();
        let mut rows: Vec<String> = s
            .query_map([], |r| {
                (0..n)
                    .map(|i| r.get::<_, rusqlite::types::Value>(i))
                    .collect::<Result<Vec<_>, _>>()
            })
            .unwrap()
            .map(|r| format!("{t} {:?}", r.unwrap()))
            .collect();
        rows.sort();
        out.extend(rows);
    }
    out
}

fn epoch_in(db: &Path) -> u64 {
    let c = Connection::open_with_flags(db, OpenFlags::SQLITE_OPEN_READ_ONLY).unwrap();
    c.query_row(
        "SELECT value FROM meta WHERE key = 'owner_epoch'",
        [],
        |r| r.get::<_, String>(0),
    )
    .unwrap()
    .parse()
    .unwrap()
}

fn unavailable(category: &str, code: &str) -> StoreError {
    StoreError::Unavailable {
        category: category.into(),
        code: code.into(),
    }
}

// ---------------------------------------------------------------------------
// Requests, policy, clock and approval authority (test values only).

fn request(kind: &str, uri: &str, resource: &str, idem: &str) -> Value {
    let text = format!(
        r#"{{"contract_version":"2.0","request_id":"req-{idem}","trace_id":"trace-1","tenant_id":"fixture-tenant",
        "principal":{{"id":"agent:claude-session-1","verified_by":"{ADAPTER}","authn_method":"os_peer_credential","roles":["agent"]}},
        "project_id":"fixture-project","session_id":"session-1","agent_id":null,"parent_session_id":null,"idempotency_key":"{idem}",
        "expected_state_revision":null,"policy_bundle_sha":null,"deadline_at":null,
        "action":{{"kind":"{kind}","resource":{{"uri":"{uri}","canonical_resource_id":"{resource}"}},
        "action_digest":"sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa","attributes":{{}},"requested_capabilities":[]}}}}"#
    );
    json::parse(text.as_bytes()).unwrap()
}

/// `file.write`: allowed under the reference bundle.
fn file_write(idem: &str) -> Value {
    request(
        "file.write",
        "file:///work/src/app.py",
        "workspace:fixture-tenant/fixture-project/src/app.py",
        idem,
    )
}

/// `repository.publish` to main: `require_approval` under the reference bundle.
fn publish(idem: &str) -> Value {
    request(
        "repository.publish",
        "git://main",
        "repository:fixture-tenant/fixture-project/main",
        idem,
    )
}

fn verify(req: &Value) -> VerifiedRequest {
    let key = || IngressKey::from_bytes(vec![0x11; 32]).unwrap();
    let mut ring = MemoryKeyRing::new();
    ring.register(ADAPTER, key());
    let seal = Seal::create(req, ADAPTER, &key()).unwrap().to_value();
    verify_ingress(req, Some(&seal), &ring).unwrap()
}

fn policy() -> ActivePolicy {
    let doc = json::parse(include_bytes!(
        "../../../docs/contracts/fixtures/policy/m0-reference.bundle.json"
    ))
    .unwrap();
    ActivePolicy::new(PolicyState::loaded(load_bundle(&doc).unwrap()))
}

fn revision() -> StateRevision {
    StateRevision::new(5).unwrap()
}

fn permit_key() -> PermitKey {
    PermitKey::from_bytes(vec![0x44; 32]).unwrap()
}

struct TestClock(Timestamp);

impl EffectiveClock for TestClock {
    fn read(&self) -> Result<Reading, ClockError> {
        Ok(Reading {
            effective: self.0,
            wall: None,
            drift: Drift::Normal,
            drift_changed: false,
        })
    }
}

fn clock() -> TestClock {
    TestClock(Timestamp::parse_rfc3339("2026-11-05T12:00:00Z").unwrap())
}

/// Authenticates exactly `human:alice` with `cred-alice` on `approval-ui` (test only).
struct Alice;

impl ApprovalAuthority for Alice {
    fn authenticate(
        &self,
        p: &ApprovalPresentation<'_>,
    ) -> Result<AuthenticatedApprover, ApproverRefusal> {
        if p.channel != "approval-ui" {
            return Err(ApproverRefusal::UntrustedChannel);
        }
        if p.approver != "human:alice" || p.credential != b"cred-alice" {
            return Err(ApproverRefusal::Unauthenticated);
        }
        Ok(AuthenticatedApprover {
            id: "human:alice".into(),
            kind: writ_broker::grants::PrincipalKind::Human,
            channel: "approval-ui".into(),
            issuer: "storage-failure-test-authority".into(),
            evidence_ref: "test-authentication:1".into(),
        })
    }
}

/// The test verifier as the ledger's configured approval channel (M1).
fn test_channel() -> ApprovalChannel {
    let v = std::sync::Arc::new(TestVerifier);
    ApprovalChannel::new(v.clone(), v, "approval-listener").unwrap()
}

/// Test human verifier (test code only): unique ceremonies; `ok:<credential>` verifies.
struct TestVerifier;

static NONCE: AtomicU64 = AtomicU64::new(0);

impl HumanVerifier for TestVerifier {
    fn relying_party(&self) -> writ_broker::human::RelyingParty {
        writ_broker::human::RegistrationVerifier::relying_party(self)
    }

    fn begin(
        &self,
        _subject: &SubjectId,
        _credentials: &ActiveCredentials,
    ) -> Result<CeremonyStart, VerifierError> {
        let state = format!(
            "nonce-{}-{}",
            std::process::id(),
            NONCE.fetch_add(1, Ordering::SeqCst)
        )
        .into_bytes();
        Ok(CeremonyStart {
            client_options: Vec::new(),
            challenge_digest: Digest::of_bytes(&state),
            state: CeremonyState::from_bytes(state),
        })
    }

    fn finish(
        &self,
        cap: &VerifierCapability,
        state: &CeremonyState,
        response: &[u8],
        credentials: &ActiveCredentials,
    ) -> Result<VerifiedAssertion, VerifierError> {
        let text = std::str::from_utf8(response).map_err(|_| VerifierError::Malformed)?;
        let credential = text
            .strip_prefix("ok:")
            .ok_or(VerifierError::SignatureInvalid)?;
        if !credentials.ids().iter().any(|c| c == credential) {
            return Err(VerifierError::CredentialUnknown);
        }
        Ok(VerifiedAssertion::new(
            cap,
            credentials.subject().clone(),
            credential,
            true,
            WEBAUTHN_PASSKEY,
            Digest::of_bytes(state.as_bytes()),
            None,
        ))
    }
}

fn ceremony(ledger: &SqliteLedger, who: &SubjectId, op: &CeremonyOp, key: &str, now: ObservedTime) {
    let begin = ledger.begin_ceremony(&CeremonyBegin {
        op,
        subject: who,
        idempotency_key: key,
        now,
    });
    if let Ok(Ok(c)) = begin {
        let response = format!("ok:cred-{}", who.subject);
        let _ = ledger.finish_ceremony(&CeremonyFinish {
            op,
            challenge_id: &c.challenge_id,
            response: response.as_bytes(),
            idempotency_key: key,
            now,
        });
    }
}

/// Test registration verifier (test code only): the response is the credential id; ES256,
/// user verified.
impl RegistrationVerifier for TestVerifier {
    fn relying_party(&self) -> RelyingParty {
        RelyingParty {
            rp_id: "localhost".into(),
            origin: "http://localhost:8443".into(),
        }
    }

    fn begin_registration(&self, _subject: &SubjectId) -> Result<RegistrationStart, VerifierError> {
        let state = format!("reg-{}", NONCE.fetch_add(1, Ordering::SeqCst)).into_bytes();
        Ok(RegistrationStart {
            client_options: Vec::new(),
            state: CeremonyState::from_bytes(state),
        })
    }

    fn finish_registration(
        &self,
        cap: &VerifierCapability,
        binding: &RegistrationBinding,
        _state: &CeremonyState,
        response: &[u8],
    ) -> Result<VerifiedRegistration, VerifierError> {
        let credential = std::str::from_utf8(response).map_err(|_| VerifierError::Malformed)?;
        Ok(VerifiedRegistration::new(
            cap,
            binding,
            credential,
            format!("pk-{credential}").into_bytes(),
            ES256,
            true,
        ))
    }
}

/// One registration ceremony (S5, X8) for `who` with credential `cred-<subject>`.
fn register(
    ledger: &dyn RegistrationLedger,
    now: &dyn Fn() -> ObservedTime,
    who: &SubjectId,
    operator: Option<(&SqliteLedger, &OperatorSession)>,
) -> VerifiedRegistration {
    let issued = match operator {
        // D-043 Y2: on the serving ledger only the operator's authenticated
        // `RegistrationStart` ceremony creates the row.
        Some((serving, o)) => {
            let op = CeremonyOp::RegistrationStart {
                subject: who.clone(),
                operator: o.operator.clone(),
                operator_session: o.session_id.clone(),
            };
            let key = format!("start-{}", who.subject);
            let c = serving
                .begin_ceremony(&CeremonyBegin {
                    op: &op,
                    subject: &o.operator,
                    idempotency_key: &key,
                    now: now(),
                })
                .unwrap()
                .unwrap();
            let response = format!("ok:cred-{}", o.operator.subject);
            match serving
                .finish_ceremony(&CeremonyFinish {
                    op: &op,
                    challenge_id: &c.challenge_id,
                    response: response.as_bytes(),
                    idempotency_key: &key,
                    now: now(),
                })
                .unwrap()
                .unwrap()
                .result
            {
                writ_broker::approval_store::CeremonyResult::RegistrationStarted {
                    registration,
                } => registration,
                other => panic!("registration start: {other:?}"),
            }
        }
        None => ledger
            .begin_registration(&RegistrationBegin {
                subject: who,
                operator: None,
                now: now(),
            })
            .unwrap()
            .unwrap(),
    };
    let operator = operator.map(|(_, o)| o);
    ledger
        .finish_registration(&RegistrationFinish {
            registration_id: &issued.registration_id,
            response: format!("cred-{}", who.subject).as_bytes(),
            operator,
            now: now(),
        })
        .unwrap()
        .unwrap()
}

/// On the closed ledger `db`: the bootstrap administrator `storage-root` through the
/// separate bootstrap entry point (S5, X8), then the test approver `storage-approver`
/// (`approver` over every scope) through the enrollment ceremony.
fn enroll_storage_approver(db: &Path) {
    let clock = clock();
    let Ok(Ok(session)) = BootstrapSession::open(db)
        .map(|s| s.map(|s| s.with_approval_channel(test_channel()).unwrap()))
    else {
        panic!("bootstrap session");
    };
    let root = SubjectId::new("writ-local", "storage-root");
    let reg = register(&session, &|| session.observe(&clock).unwrap(), &root, None);
    let at = session.observe(&clock).unwrap();
    session
        .bootstrap(
            &BootstrapEnrollment {
                subject: root.clone(),
                credential: reg,
                linked_identities: vec![],
            },
            at,
        )
        .unwrap()
        .unwrap();
    let ledger = SqliteLedger::open(db)
        .unwrap()
        .with_approval_channel(test_channel())
        .unwrap();
    let now = || {
        let mut l: &SqliteLedger = &ledger;
        observe(&clock, &mut l).unwrap()
    };
    let approver = SubjectId::new("writ-local", "storage-approver");
    let session = OperatorSession {
        operator: root.clone(),
        session_id: "storage-root-session".into(),
    };
    let enrollment = NewEnrollment {
        subject: approver.clone(),
        kind: writ_broker::human::PrincipalKind::Human,
        credential: Some(register(
            &ledger,
            &now,
            &approver,
            Some((&ledger, &session)),
        )),
        linked_identities: vec![],
        roles: vec![NewRole {
            permission: Permission::Approver,
            tenant_id: "*".into(),
            project_id: "*".into(),
        }],
    };
    ceremony(
        &ledger,
        &root,
        &CeremonyOp::Enroll {
            enrollment,
            operator_session: session.session_id.clone(),
        },
        "enroll-storage-approver",
        now(),
    );
}

/// The M4 grant for `req` (D-042 X1: `writ-ledger-6` mints grants only in its approval
/// ceremony): records the approval request the broker records and has the test approver
/// (enrolled by [`enroll_storage_approver`]) approve it; a request already decided is left
/// as it is. Returns the request's grant label (which names a grant only once the request
/// was approved).
fn grant_for(
    ledger: &SqliteLedger,
    active: &ActivePolicy,
    clock: &TestClock,
    req: &Value,
) -> Result<GrantLabel, Refusal> {
    let mut l: &SqliteLedger = ledger;
    let now = observe(clock, &mut l).map_err(|e| e.refusal())?;
    let request = {
        let shared = active.shared();
        let inputs = shared.trusted_inputs(revision());
        let verified = verify(req);
        let authorized = authorize(&verified, &inputs, &now)?;
        approval_request_for(&authorized, now).ok_or(Refusal::NotApprovable)?
    };
    ledger
        .record_approval_request(&request)
        .map_err(|f| Refusal::LedgerUnavailable(f.error))?;
    let approver = SubjectId::new("writ-local", "storage-approver");
    let rid = request.request_id().to_string();
    ceremony(
        ledger,
        &approver,
        &CeremonyOp::Approve {
            request_id: rid.clone(),
        },
        &format!("approve-{rid}"),
        now,
    );
    Ok(approval_grant_label(&rid).unwrap())
}

/// `publish(idem)` with an action digest derived from `idem` (one M4 request per key).
fn publish_distinct(idem: &str) -> Value {
    let mut v = publish(idem);
    if let Value::Obj(m) = &mut v {
        if let Some(Value::Obj(a)) = m.get_mut("action") {
            a.insert(
                "action_digest".into(),
                json::s(Digest::of_bytes(idem.as_bytes()).as_str()),
            );
        }
    }
    v
}

/// The M3 approval path (closed on `writ-ledger-6`; still observes trusted time first).
fn approve<L, A>(b: &Broker<'_, L, A>, req: &Value, label: &str) -> Result<(), Refusal>
where
    L: ExecuteLedger + ?Sized,
    for<'x> &'x L: LedgerStore,
    A: writ_broker::effects::EffectAdapter + ?Sized,
{
    let verified = verify(req);
    let label = GrantLabel::new(label).unwrap();
    b.grants()
        .approve(
            &Approval {
                request: &verified,
                state_revision: revision(),
                label: &label,
                verdict: Verdict::Approved,
                ttl_seconds: None,
            },
            &ApprovalPresentation {
                channel: "approval-ui",
                approver: "human:alice",
                credential: b"cred-alice",
            },
            &Alice,
        )
        .map(|_| ())
}

// ---------------------------------------------------------------------------
// Read-only ledger file

#[test]
fn a_read_only_ledger_file_is_refused_and_unchanged() {
    let _serial = serial();
    let d = tmp("ro-file");
    let db = d.join("ledger.db");
    let mut l = SqliteLedger::create(&db).unwrap();
    l.record_refusal("BEFORE").unwrap();
    drop(l);
    let epoch = epoch_in(&db);
    let rows = dump(&db);
    chmod(&db, 0o444);
    prove_file_unwritable(&db);
    let before = files(&d);

    let refused = SqliteLedger::open(&db).err();
    assert_eq!(refused, Some(unavailable("initialize", "SQLITE_READONLY")));
    assert_eq!(
        files(&d),
        before,
        "a refused read-only ledger must not change"
    );
    // An explicit upgrade is refused too (the file is not an older schema) and changes nothing.
    assert!(SqliteLedger::upgrade(&db).is_err());
    assert_eq!(files(&d), before);

    chmod(&db, 0o644);
    assert_eq!(dump(&db), rows, "no row changed");
    assert_eq!(epoch_in(&db), epoch, "the refused open took no owner epoch");
    let l = SqliteLedger::open(&db).unwrap();
    assert_eq!(
        (l.owner_epoch(), l.refusal_count().unwrap()),
        (epoch + 1, 1)
    );
    drop(l);
    let _ = fs::remove_dir_all(&d);
}

// ---------------------------------------------------------------------------
// Read-only ledger directory

#[test]
fn creating_a_ledger_in_a_read_only_directory_fails_and_leaves_it_empty() {
    let _serial = serial();
    let d = tmp("ro-dir-create");
    let ro = d.join("ro");
    fs::create_dir(&ro).unwrap();
    chmod(&ro, 0o555);
    prove_dir_unwritable(&ro);

    assert_eq!(
        SqliteLedger::create(&ro.join("ledger.db")).err(),
        Some(unavailable("ownership", "io"))
    );
    assert!(
        fs::read_dir(&ro).unwrap().next().is_none(),
        "nothing created"
    );
    chmod(&ro, 0o755);
    let _ = fs::remove_dir_all(&d);
}

#[test]
fn an_existing_ledger_in_a_read_only_directory_is_refused_and_unchanged() {
    let _serial = serial();
    let d = tmp("ro-dir-open");
    let db = d.join("ledger.db");
    let mut l = SqliteLedger::create(&db).unwrap();
    l.record_refusal("BEFORE").unwrap();
    // Inspect while the owner is idle, then close it last: its close checkpoints and removes
    // the write-ahead log and shared-memory files, so opening must create them again.
    let epoch = epoch_in(&db);
    let rows = dump(&db);
    drop(l);
    assert_eq!(
        files(&d).keys().map(String::as_str).collect::<Vec<_>>(),
        ["ledger.db", "ledger.db.owner"],
        "a cleanly closed ledger has no -wal or -shm file"
    );
    chmod(&d, 0o555);
    prove_dir_unwritable(&d);
    let before = files(&d);

    let refused = SqliteLedger::open(&db).err();
    assert_eq!(refused, Some(unavailable("initialize", "SQLITE_READONLY")));
    assert_eq!(files(&d), before, "no file created, removed or changed");

    chmod(&d, 0o755);
    assert_eq!(dump(&db), rows, "no row changed");
    assert_eq!(epoch_in(&db), epoch, "the refused open took no owner epoch");
    let l = SqliteLedger::open(&db).unwrap();
    assert_eq!(
        (l.owner_epoch(), l.refusal_count().unwrap()),
        (epoch + 1, 1)
    );
    drop(l);
    let _ = fs::remove_dir_all(&d);
}

// ---------------------------------------------------------------------------
// Unwritable ledger after the broker process was killed

/// xorshift64* (deterministic, seeded).
struct Rng(u64);

impl Rng {
    fn new(seed: u64) -> Rng {
        Rng(seed.wrapping_mul(0x9E37_79B9_7F4A_7C15) | 1)
    }

    fn below(&mut self, n: u64) -> u64 {
        self.0 ^= self.0 >> 12;
        self.0 ^= self.0 << 25;
        self.0 ^= self.0 >> 27;
        self.0.wrapping_mul(0x2545_F491_4F6C_DD1D) % n
    }
}

/// Child entry point: inert unless the parent set the environment variable. Runs a seeded
/// broker workload (allowed writes, approved publishes, failing effects, reconciliation)
/// on the parent's ledger until killed, announcing each completed operation.
#[test]
fn storage_failure_child() {
    let Ok(dir) = std::env::var(CHILD) else {
        return;
    };
    let dir = PathBuf::from(dir);
    let ledger = SqliteLedger::open(&dir.join("ledger.db"))
        .unwrap()
        .with_approval_channel(test_channel())
        .unwrap();
    let effects = SqliteEffectStore::open(&dir.join("effects.db"), ledger.ledger_id()).unwrap();
    let (active, clock, key) = (policy(), clock(), permit_key());
    let b = Broker::new(&active, &clock, &ledger, &effects, &key);
    let mut rng = Rng::new(SEED);
    let out = std::io::stdout();
    writeln!(out.lock(), "START").unwrap();
    for op in 0..1_000_000_u64 {
        let outcome = match rng.below(10) {
            0 => format!("{:?}", b.reconcile()),
            1..=3 => {
                let k = format!("p-{}", rng.below(400));
                match grant_for(&ledger, &active, &clock, &publish_distinct(&k)) {
                    Ok(g) => format!(
                        "{:?}",
                        b.execute(&verify(&publish_distinct(&k)), revision(), Some(&g))
                            .result
                    ),
                    Err(r) => format!("{r:?}"),
                }
            }
            _ => {
                effects.set_effects_fail(rng.below(6) == 0);
                let k = format!("a-{}", rng.below(2000));
                format!(
                    "{:?}",
                    b.execute(&verify(&file_write(&k)), revision(), None).result
                )
            }
        };
        let mut o = out.lock();
        writeln!(o, "OP {op} {outcome}").unwrap();
        o.flush().unwrap();
    }
}

#[test]
fn an_unwritable_ledger_after_sigkill_fails_closed_without_change() {
    let _serial = serial();
    let d = tmp("after-kill");
    let db = d.join("ledger.db");
    let ledger = SqliteLedger::create(&db).unwrap();
    drop(SqliteEffectStore::create(&d.join("effects.db"), ledger.ledger_id()).unwrap());
    drop(ledger);
    enroll_storage_approver(&db);

    let mut child = Command::new(std::env::current_exe().unwrap())
        .args([
            "storage_failure_child",
            "--exact",
            "--nocapture",
            "--test-threads=1",
            "--quiet",
        ])
        .env(CHILD, &d)
        .stdin(Stdio::null())
        .stdout(Stdio::piped())
        .stderr(Stdio::null())
        .spawn()
        .unwrap();
    let out = BufReader::new(child.stdout.take().unwrap());
    let (tx, rx) = mpsc::channel();
    std::thread::spawn(move || {
        for line in out.lines() {
            let Ok(line) = line else { break };
            if tx.send(line).is_err() {
                break;
            }
        }
    });
    // Kill after a seeded number of completed operations, while the workload runs.
    let target = 200 + Rng::new(SEED ^ 0x5eed).below(300);
    let mut done = 0;
    while done < target {
        let line = rx
            .recv_timeout(Duration::from_secs(60))
            .expect("the child stopped reporting operations");
        if line.starts_with("OP ") {
            done += 1;
        }
    }
    assert!(child.try_wait().unwrap().is_none(), "the child is running");
    child.kill().unwrap();
    let status = child.wait().unwrap();
    assert_eq!(status.signal(), Some(9), "{status:?}");

    // The killed process left its write-ahead log behind.
    let wal = d.join("ledger.db-wal");
    let shm = d.join("ledger.db-shm");
    assert!(wal.exists() && shm.exists(), "{:?}", files(&d).keys());
    for f in [&db, &wal, &shm] {
        chmod(f, 0o444);
        prove_file_unwritable(f);
    }
    let before = files(&d);

    let refused = SqliteLedger::open(&db).err();
    assert_eq!(refused, Some(unavailable("initialize", "SQLITE_READONLY")));
    assert_eq!(files(&d), before, "the unwritable ledger changed");
    // The broker cannot be started on it: no ledger, so no operation, audit or effect.
    let refused_again = SqliteLedger::open(&db).err();
    assert_eq!(refused_again, refused, "deterministic refusal");
    assert_eq!(files(&d), before);

    // With write access restored the ledger opens and recovers from durable state.
    for f in [&db, &wal, &shm] {
        chmod(f, 0o644);
    }
    let ledger = SqliteLedger::open(&db).unwrap();
    let effects = SqliteEffectStore::open(&d.join("effects.db"), ledger.ledger_id()).unwrap();
    let (active, clock, key) = (policy(), clock(), permit_key());
    let b = Broker::new(&active, &clock, &ledger, &effects, &key);
    assert!(b.reconcile().is_ok(), "recovery");
    assert_eq!(
        b.reconcile(),
        Ok(Reconciled::default()),
        "repeated recovery"
    );
    assert_eq!(
        b.execute(&verify(&file_write("after-restore")), revision(), None)
            .result,
        Ok(Executed::EffectCompleted)
    );
    drop(b);
    drop(effects);
    drop(ledger);
    let _ = fs::remove_dir_all(&d);
}

// ---------------------------------------------------------------------------
// Fully unavailable storage (total outage at the storage boundary)

/// Every operation of this ledger fails at begin: lifecycle transactions, the refusal
/// audit, failure records, the trusted-time floor (read and raise) and clock events. The
/// real ledger behind it is never reached.
struct Outage<'a> {
    real: &'a SqliteLedger,
}

fn down(name: &str) -> StoreError {
    unavailable(&format!("transaction {name}"), "SQLITE_IOERR")
}

fn down_tx(name: &str) -> TxFailure {
    TxFailure::new(TxStage::Begin, down(name))
}

impl GrantLedger for Outage<'_> {
    fn insert_grant(&self, _grant: &NewGrant) -> Result<GrantInsert, StoreError> {
        Err(down("approve"))
    }

    fn reserve(
        &self,
        _request: &ReserveRequest<'_>,
    ) -> Result<Result<ReserveOutcome, Refusal>, TxFailure> {
        Err(down_tx("reserve"))
    }
}

impl ExecuteLedger for Outage<'_> {
    fn dispatch(
        &self,
        _intent: &DispatchIntent<'_>,
    ) -> Result<Result<Dispatched, Refusal>, TxFailure> {
        Err(down_tx("dispatch"))
    }

    fn observe_effect(&self, _observation: &Observation<'_>) -> Result<(), TxFailure> {
        Err(down_tx("observe"))
    }

    fn pending_receipts(
        &self,
        _gate: &ExclusiveHold<'_>,
    ) -> Result<Vec<PendingReceipt>, TxFailure> {
        Err(down_tx("reconcile_scan"))
    }

    fn reconcile(
        &self,
        _gate: &ExclusiveHold<'_>,
        _resolutions: &[Resolution],
    ) -> Result<Reconciled, TxFailure> {
        Err(down_tx("reconcile"))
    }

    fn record_failure(&self, _code: &str, _record: &FailureRecord) -> Result<(), StoreError> {
        Err(down("audit"))
    }
}

impl LedgerStore for &Outage<'_> {
    fn owner_epoch(&self) -> u64 {
        self.real.owner_epoch()
    }

    fn record_refusal(&mut self, _code: &str) -> Result<(), StoreError> {
        Err(down("audit"))
    }

    fn time_floor(&self) -> Result<Option<Timestamp>, StoreError> {
        Err(down("read"))
    }

    fn raise_time_floor(&mut self, _at: Timestamp) -> Result<Timestamp, StoreError> {
        Err(down("time_floor"))
    }

    fn record_clock_event(&mut self, _event: &ClockEvent) -> Result<(), StoreError> {
        Err(down("clock_event"))
    }
}

#[test]
fn fully_unavailable_storage_fails_closed_without_audit_effect_or_state_change() {
    let _serial = serial();
    let d = tmp("outage");
    let (db, edb) = (d.join("ledger.db"), d.join("effects.db"));
    drop(SqliteLedger::create(&db).unwrap());
    enroll_storage_approver(&db);
    let ledger = SqliteLedger::open(&db).unwrap();
    let effects = SqliteEffectStore::create(&edb, ledger.ledger_id()).unwrap();
    let (active, clock, key) = (policy(), clock(), permit_key());

    // State before the outage: a completed write (its permit is presented later), an
    // approved but unused grant, an audited refusal and a persisted time floor.
    let b = Broker::new(&active, &clock, &ledger, &effects, &key);
    let done = b.execute(&verify(&file_write("k1")), revision(), None);
    assert_eq!(done.result, Ok(Executed::EffectCompleted));
    let permit = done.permit.unwrap();
    let g2 = grant_for(&ledger, &active, &clock, &publish("k2")).unwrap();
    assert!(b
        .execute(&verify(&publish("k3")), revision(), None)
        .result
        .is_err());
    drop(b);
    let floor = LedgerStore::time_floor(&&ledger).unwrap();
    assert!(floor.is_some());
    let refusals = ledger.refusal_count().unwrap();
    let (rows, erows) = (dump(&db), dump(&edb));

    let outage = Outage { real: &ledger };
    let b = Broker::new(&active, &clock, &outage, &effects, &key);
    let answers = [
        (
            "execute new",
            b.execute(&verify(&file_write("k4")), revision(), None)
                .result
                .map(|_| ()),
        ),
        (
            "execute retry",
            b.execute(&verify(&file_write("k1")), revision(), None)
                .result
                .map(|_| ()),
        ),
        (
            "execute with grant",
            b.execute(&verify(&publish("k2")), revision(), Some(&g2))
                .result
                .map(|_| ()),
        ),
        (
            "deny-class execute",
            b.execute(
                &verify(&request(
                    "network.egress",
                    "https://example.org",
                    "network:fixture-tenant/fixture-project/example.org",
                    "k5",
                )),
                revision(),
                None,
            )
            .result
            .map(|_| ()),
        ),
        ("approve", approve(&b, &publish("k6"), "g6")),
        (
            "present permit",
            Err(b.present_permit(&permit, &file_write("k1"))),
        ),
        ("reconcile", b.reconcile().map(|_| ())),
    ];
    let got: Vec<(&str, Result<(), String>)> = answers
        .iter()
        .map(|(n, r)| (*n, r.clone().map_err(|e| e.code().to_string())))
        .collect();
    let want = |code: &str| Err(code.to_string());
    assert_eq!(
        got,
        [
            ("execute new", want("TIME_UNAVAILABLE")),
            ("execute retry", want("TIME_UNAVAILABLE")),
            ("execute with grant", want("TIME_UNAVAILABLE")),
            ("deny-class execute", want("TIME_UNAVAILABLE")),
            ("approve", want("TIME_UNAVAILABLE")),
            ("present permit", want("TIME_UNAVAILABLE")),
            ("reconcile", want("LEDGER_UNAVAILABLE")),
        ]
    );
    drop(b);

    // Nothing was written anywhere: no audit, no grant, no receipt, no permit, no effect,
    // and the trusted-time floor is unchanged.
    assert_eq!(ledger.refusal_count().unwrap(), refusals, "no audit");
    assert_eq!(dump(&db), rows, "ledger unchanged");
    assert_eq!(dump(&edb), erows, "effect store unchanged");
    assert_eq!(LedgerStore::time_floor(&&ledger).unwrap(), floor);

    // The same broker inputs on the available ledger work again (the outage was the cause).
    let b = Broker::new(&active, &clock, &ledger, &effects, &key);
    assert_eq!(
        b.execute(&verify(&file_write("k4")), revision(), None)
            .result,
        Ok(Executed::EffectCompleted)
    );
    drop(b);
    drop(effects);
    drop(ledger);
    let _ = fs::remove_dir_all(&d);
}
