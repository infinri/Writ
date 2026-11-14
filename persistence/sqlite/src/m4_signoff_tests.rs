//! M4-06 signoff tests on the SQLite ledger and the production broker (D-040 O-4, O-5, O-7;
//! D-042 item 2; M4-INTERFACES I-5, I-8, X6, X7): signoff exclusions read from authoritative
//! ledger state (stored promotion contexts, enrollment links, administrative changers) and
//! fingerprinted with the displayed context; caller input only adds; the use-time checks of
//! `check_use` inside the promotion transaction; atomic single-use consumption with the
//! promotion; identical retries replay their receipt; and the exact ledger order of
//! concurrent revocation (signoff, credential, subject, role) against promotion consumption
//! (M4-12, CO-PROP-02). Ceremonies run through a test verifier (test code only;
//! `VerifierCapability` is allowed here); the records, attestations and keys are test
//! evidence.

#![allow(clippy::unwrap_used, clippy::expect_used, clippy::panic)]

use std::path::PathBuf;
use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::{Arc, Barrier};
use std::thread;

use writ_broker::acceptance::{MAC_ALG, RECORD_CONTRACT, RECORD_CONTRACT_V2, RECORD_PURPOSE};
use writ_broker::approval::{ApprovalCode, ChallengeRefusal, NewSignoff, RevocationTarget};
use writ_broker::approval_store::{
    ApprovalLedger, CeremonyBegin, CeremonyFinish, CeremonyOp, CeremonyOutcome, CeremonyResult,
    IssuedChallenge, NewEnrollment, NewRole, SignoffParties, UseState,
};
use writ_broker::clock::{observe, ObservedTime};
use writ_broker::execute::Broker;
use writ_broker::human::{
    ActiveCredentials, ApprovalChannel, CeremonyStart, CeremonyState, HumanVerifier, Permission,
    PrincipalKind, SubjectId, VerifiedAssertion, VerifierCapability, VerifierError,
    WEBAUTHN_PASSKEY,
};
use writ_broker::keys::{AcceptanceRecordKey, MemoryKeys, PermitKey, RunnerAttestationKey};
use writ_broker::policy::{ActivePolicy, PolicyState};
use writ_broker::refusal::Refusal;
use writ_broker::workflow::{AdminRequest, PromotionOutcome, Trust};
use writ_broker::writ_kernel::json::{self, Digest, Value};
use writ_broker::writ_kernel::policy::{load_bundle, LoadedBundle};
use writ_broker::writ_kernel::time::Timestamp;

use super::grant_tests::{set, verify, TestClock};
use super::m4_support::{bootstrap_root_at, registered, TestVerifier as RegistrationFixture};
use super::transition_tests::{
    alice, committed, parse, sha, sign, transition, wf, Docs, Operators, Snapshots, RECORD_KEY,
};
use super::{SqliteEffectStore, SqliteLedger};

const T: &str = "fixture-tenant";
const P: &str = "fixture-project";
const ADAPTER: &str = "writ-host-adapter:fixture";
const RUNNER_KEY: [u8; 32] = [0x6b; 32];

fn now() -> Timestamp {
    Timestamp::parse_strict("2026-11-07T11:00:00Z").unwrap()
}

// ---------------------------------------------------------------------------
// Test verifier (test code only): responses are `ok:<credential id>`; anything else is a
// forged signature.

#[derive(Default)]
struct TestVerifier {
    next: AtomicU64,
}

impl HumanVerifier for TestVerifier {
    fn begin(
        &self,
        _subject: &SubjectId,
        _credentials: &ActiveCredentials,
    ) -> Result<CeremonyStart, VerifierError> {
        let n = self.next.fetch_add(1, Ordering::SeqCst);
        let state = format!("signoff-nonce-{n}").into_bytes();
        Ok(CeremonyStart {
            client_options: b"options".to_vec(),
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
        let (mode, credential) = text.split_once(':').ok_or(VerifierError::Malformed)?;
        if !credentials.ids().iter().any(|c| c == credential) {
            return Err(VerifierError::CredentialUnknown);
        }
        if mode != "ok" {
            return Err(VerifierError::SignatureInvalid);
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

// ---------------------------------------------------------------------------
// Trusted test documents: a requirement set with the human criterion `signoff`.

fn requirements() -> Value {
    parse(&format!(
        r#"{{"acceptance_policy_id":"accept-code-review","acceptance_policy_version":"3","acceptance_policy_bundle_sha":"{}","criteria":[{{"criterion_id":"build","required_class":"mechanical"}},{{"criterion_id":"docs","required_class":"evidence"}},{{"criterion_id":"signoff","required_class":"human"}}]}}"#,
        sha('c')
    ))
}

/// The fixture bundle plus an unprotected `accepted` to `verifying` reopening.
fn bundle() -> LoadedBundle {
    let mut b = parse(include_str!(
        "../../../m1/fixtures/transition/m1-transition.bundle.json"
    ));
    let mut statements = b.get("statements").unwrap().as_arr().unwrap().to_vec();
    statements.push(parse(
        r#"{"statement_id":"permit-reopen","effect":"permit","action_kinds":["workflow.transition"],"resource_prefixes":["workflow:fixture-tenant/fixture-project/"],"from_states":["accepted"],"to_states":["verifying"],"reason_code":"REOPEN_PERMITTED"}"#,
    ));
    b = set(&b, &["statements"], Value::Arr(statements));
    load_bundle(&b).unwrap()
}

/// Authenticated record `id` (verifying to accepted at `revision`) with the human criterion;
/// `/2` naming `signoff` for it, or `/1` when `signoff` is `None`.
fn record(id: &str, revision: i64, att: &str, signoff: Option<&str>) -> Value {
    let mut r = parse(&format!(
        r#"{{"contract_version":"m1-draft-3","acceptance_record_id":"{id}","tenant_id":"{T}","project_id":"{P}","task_id":"task-1","workflow_id":"wf-1","transition":{{"from_state":"verifying","to_state":"accepted"}},"artifact_snapshot_id":"snap-1","artifact_digest":"{}","acceptance_policy_id":"accept-code-review","acceptance_policy_version":"3","acceptance_policy_bundle_sha":"{}","state_revision":{revision},"status":"accepted","issued_at":"2026-11-07T10:00:00Z","expires_at":"2026-11-07T12:00:00Z","criteria":[{{"criterion_id":"build","mandatory":true,"required_class":"mechanical","status":"pass","evaluator_id":"ci-runner","evaluator_trust":"M","fresh_until":"2026-11-07T11:30:00Z"}},{{"criterion_id":"docs","mandatory":true,"required_class":"evidence","status":"pass","evaluator_id":"doc-check","evaluator_trust":"E","fresh_until":"2026-11-07T11:30:00Z"}},{{"criterion_id":"signoff","mandatory":true,"required_class":"human","status":"pass","evaluator_id":"approval-service","evaluator_trust":"H","fresh_until":"2026-11-07T11:30:00Z"}}],"record_digest":"{}"}}"#,
        sha('b'),
        sha('c'),
        sha('0')
    ));
    let d = json::digest(&r.without("record_digest")).unwrap();
    r = set(&r, &["record_digest"], json::s(d.as_str()));
    let v2 = signoff.is_some();
    let eval = |criterion: &str, evaluator: &str, attestation: Value, so: Value| {
        let mut e = json::obj([
            ("attestation_id", attestation),
            ("criterion_id", json::s(criterion)),
            ("environment_digest", json::s(&sha('2'))),
            ("evaluation_id", json::s(&format!("eval-{criterion}"))),
            ("evaluator_id", json::s(evaluator)),
            ("evaluator_version", json::s("1.4.0")),
            ("evidence_digests", Value::Arr(vec![])),
            ("implementation_digest", json::s(&sha('1'))),
            ("registry_revision", Value::Int(12)),
        ]);
        if v2 {
            e = set(&e, &["signoff_id"], so);
        }
        e
    };
    let doc = json::obj([
        (
            "auth",
            json::obj([
                ("alg", json::s(MAC_ALG)),
                ("key_id", json::s("acc-key-1")),
                ("mac", json::s(&"0".repeat(64))),
            ]),
        ),
        (
            "contract_version",
            json::s(if v2 {
                RECORD_CONTRACT_V2
            } else {
                RECORD_CONTRACT
            }),
        ),
        (
            "evaluations",
            Value::Arr(vec![
                eval("build", "ci-runner", json::s(att), Value::Null),
                eval("docs", "doc-check", Value::Null, Value::Null),
                eval(
                    "signoff",
                    "approval-service",
                    Value::Null,
                    signoff.map_or(Value::Null, json::s),
                ),
            ]),
        ),
        ("issuer", json::s("acceptance-service")),
        ("mode", json::s("governed")),
        ("purpose", json::s(RECORD_PURPOSE)),
        ("record", r),
    ]);
    sign(&doc, &RECORD_KEY)
}

fn attestation(id: &str, record_id: &str, revision: i64) -> Value {
    let mut a = parse(include_str!(
        "../../../docs/contracts/proposed/examples/runner-attestation.valid.json"
    ));
    for (k, v) in [
        ("attestation_id", json::s(id)),
        ("tenant_id", json::s(T)),
        ("project_id", json::s(P)),
        (
            "canonical_target",
            json::s(&format!("workflow:{T}/{P}/wf-1")),
        ),
        ("acceptance_record_id", json::s(record_id)),
        ("state_revision", Value::Int(revision)),
    ] {
        a = set(&a, &[k], v);
    }
    sign(&a, &RUNNER_KEY)
}

fn registry_entry(evaluator: &str) -> Value {
    let mut v = parse(include_str!(
        "../../../docs/contracts/proposed/examples/evaluator-registry-entry.valid.json"
    ));
    v = set(&v, &["scope", "tenant_id"], json::s(T));
    v = set(&v, &["scope", "project_id"], json::s(P));
    let (class, trust, criterion) = match evaluator {
        "doc-check" => ("evidence", "E", "docs"),
        "approval-service" => ("human", "H", "signoff"),
        _ => return v,
    };
    v = set(&v, &["evaluator_id"], json::s(evaluator));
    v = set(&v, &["class"], json::s(class));
    v = set(&v, &["trust"], json::s(trust));
    v = set(&v, &["criteria"], Value::Arr(vec![json::s(criterion)]));
    if let Value::Obj(m) = &mut v {
        m.remove("command");
    }
    v
}

/// verifying to accepted at `expected`, bound to record `id`, as `principal`.
fn accept_as(key: &str, expected: i64, id: &str, principal: &str) -> Value {
    let r = transition(
        key,
        "verifying",
        "accepted",
        expected,
        Some((id, "snap-1", &sha('b'))),
    );
    set(&r, &["principal", "id"], json::s(principal))
}

fn accept(key: &str, expected: i64, id: &str) -> Value {
    accept_as(key, expected, id, "agent:session-1")
}

fn human(s: &str) -> SubjectId {
    SubjectId::new("writ-local", s)
}

fn requester() -> SubjectId {
    SubjectId::new(ADAPTER, "agent:session-1")
}

fn ap(code: ApprovalCode) -> Refusal {
    Refusal::Approval(code)
}

fn person(name: &str, roles: &[(Permission, &str, &str)], linked: Vec<SubjectId>) -> NewEnrollment {
    NewEnrollment {
        subject: human(name),
        kind: PrincipalKind::Human,
        credential: None,
        linked_identities: linked,
        roles: roles
            .iter()
            .map(|(p, t, q)| NewRole {
                permission: *p,
                tenant_id: t.to_string(),
                project_id: q.to_string(),
            })
            .collect(),
    }
}

// ---------------------------------------------------------------------------
// The world.

struct W {
    dir: PathBuf,
    ledger: SqliteLedger,
    effects: SqliteEffectStore,
    active: ActivePolicy,
    clock: TestClock,
    key: PermitKey,
    docs: Docs,
    snapshots: Snapshots,
    record_keys: MemoryKeys<AcceptanceRecordKey>,
    runner_keys: MemoryKeys<RunnerAttestationKey>,
}

impl Drop for W {
    fn drop(&mut self) {
        let _ = std::fs::remove_dir_all(&self.dir);
    }
}

impl W {
    /// Workflow wf-1 in `verifying` at revision 1 under the human requirement set, the
    /// administrator `root`, the signoff authority `sara` (linked to `extra_link`, if any)
    /// and the revoker `carol` in (fixture-tenant, fixture-project); record `acc-1` (`/2`,
    /// signoff `so-1`) and attestation `att-1` published.
    fn new(name: &str, sara_links: Vec<SubjectId>) -> W {
        let dir = std::env::temp_dir().join(format!("writ-m406-{}-{name}", std::process::id()));
        let _ = std::fs::remove_dir_all(&dir);
        std::fs::create_dir_all(&dir).unwrap();
        // The administrator `root` is bootstrapped through the separate entry point (S5, X8)
        // before the world takes ownership of the ledger.
        drop(SqliteLedger::create(&dir.join("ledger.db")).unwrap());
        assert_eq!(
            bootstrap_root_at(
                &dir.join("ledger.db"),
                &TestClock::at(now()),
                &Arc::new(RegistrationFixture::default()),
                &human("root"),
            ),
            Ok(1)
        );
        let channel = ApprovalChannel::new(
            Arc::new(TestVerifier::default()),
            Arc::new(RegistrationFixture::default()),
            "approval-listener",
        )
        .unwrap();
        let ledger = SqliteLedger::open(&dir.join("ledger.db"))
            .unwrap()
            .with_approval_channel(channel)
            .unwrap();
        let effects =
            SqliteEffectStore::create(&dir.join("effects.db"), ledger.ledger_id()).unwrap();
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
            active: ActivePolicy::new(PolicyState::loaded(bundle())),
            clock: TestClock::at(now()),
            key: PermitKey::from_bytes(vec![0x44; 32]).unwrap(),
            docs: Docs::default(),
            snapshots: Snapshots::default(),
            record_keys,
            runner_keys,
        };
        w.snapshots
            .content
            .lock()
            .unwrap()
            .insert("snap-1".into(), sha('b'));
        w.enroll(person(
            "sara",
            &[(Permission::SignoffAuthority, T, P)],
            sara_links,
        ))
        .unwrap();
        w.enroll(person("carol", &[(Permission::Revoker, T, P)], vec![]))
            .unwrap();
        w.admin(AdminRequest::CreateWorkflow {
            key: wf(),
            task_id: "task-1".into(),
            initial_state: "planning".into(),
            artifact_snapshot_id: "snap-1".into(),
            artifact_digest: sha('b'),
        })
        .unwrap();
        w.admin(AdminRequest::ActivateAcceptancePolicy {
            tenant_id: T.into(),
            project_id: P.into(),
            requirements: requirements(),
            effect_prerequisites: vec![],
        })
        .unwrap();
        for e in ["ci-runner", "doc-check", "approval-service"] {
            w.admin(AdminRequest::RegisterEvaluator {
                entry: registry_entry(e),
            })
            .unwrap();
        }
        assert_eq!(
            w.promote(&transition("k0", "planning", "verifying", 0, None)),
            Ok(committed(1))
        );
        w.publish("acc-1", 1, "att-1", Some("so-1"));
        w
    }

    fn publish(&self, id: &str, revision: i64, att: &str, signoff: Option<&str>) {
        self.docs.put(id, record(id, revision, att, signoff));
        self.docs.put(att, attestation(att, id, revision));
    }

    fn now(&self) -> ObservedTime {
        let mut l: &SqliteLedger = &self.ledger;
        observe(&self.clock, &mut l).unwrap()
    }

    fn broker(&self) -> Broker<'_, SqliteLedger, SqliteEffectStore> {
        Broker::new(
            &self.active,
            &self.clock,
            &self.ledger,
            &self.effects,
            &self.key,
        )
    }

    fn trust(&self) -> Trust<'_> {
        Trust {
            records: &self.docs,
            attestations: &self.docs,
            record_keys: &self.record_keys,
            runner_keys: &self.runner_keys,
            snapshots: &self.snapshots,
        }
    }

    fn admin(&self, request: AdminRequest) -> Result<(), Refusal> {
        self.broker()
            .administer(&Operators, &alice(), &self.snapshots, request)
    }

    fn promote(&self, request: &Value) -> Result<PromotionOutcome, Refusal> {
        self.broker()
            .promote(&self.trust(), &verify(request), None)
            .result
    }

    fn request_signoff(&self, request: &Value) -> Result<Vec<String>, Refusal> {
        self.broker()
            .request_signoff(&self.trust(), &verify(request))
    }

    fn begin(
        &self,
        who: &SubjectId,
        op: &CeremonyOp,
        key: &str,
    ) -> Result<IssuedChallenge, Refusal> {
        self.ledger
            .begin_ceremony(&CeremonyBegin {
                op,
                subject: who,
                idempotency_key: key,
                now: self.now(),
            })
            .expect("store")
    }

    fn finish(
        &self,
        who: &SubjectId,
        op: &CeremonyOp,
        challenge_id: &str,
        key: &str,
    ) -> Result<CeremonyOutcome, Refusal> {
        self.ledger
            .finish_ceremony(&CeremonyFinish {
                op,
                challenge_id,
                response: format!("ok:cred-{}", who.subject).as_bytes(),
                idempotency_key: key,
                now: self.now(),
            })
            .expect("store")
    }

    fn ceremony(
        &self,
        who: &SubjectId,
        op: &CeremonyOp,
        key: &str,
    ) -> Result<CeremonyOutcome, Refusal> {
        let c = self.begin(who, op, key)?;
        self.finish(who, op, &c.challenge_id, key)
    }

    fn enroll(&self, e: NewEnrollment) -> Result<CeremonyOutcome, Refusal> {
        // The credential comes from the ledger's registration ceremony (S5, X8).
        let e = registered(&self.ledger, &|| self.now(), e);
        let key = format!("enroll-{}", e.subject.subject);
        self.ceremony(&human("root"), &CeremonyOp::Enroll { enrollment: e }, &key)
    }

    fn revoke(
        &self,
        who: &str,
        target: RevocationTarget,
        key: &str,
    ) -> Result<CeremonyOutcome, Refusal> {
        let op = CeremonyOp::Revoke {
            target,
            reason: "test".into(),
        };
        self.ceremony(&human(who), &op, key)
    }

    fn count(&self, sql: &str) -> i64 {
        self.ledger
            .db()
            .conn
            .query_row(sql, [], |r| r.get(0))
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

    /// The ledger-wide order (I-8): (seq, kind, reference).
    fn sequence(&self) -> Vec<(i64, String, String)> {
        let db = self.ledger.db();
        let mut st = db
            .conn
            .prepare("SELECT seq, kind, reference FROM ledger_sequence ORDER BY seq")
            .unwrap();
        st.query_map([], |r| Ok((r.get(0)?, r.get(1)?, r.get(2)?)))
            .unwrap()
            .collect::<rusqlite::Result<Vec<_>>>()
            .unwrap()
    }

    fn role_of(&self, subject: &str) -> String {
        self.ledger
            .db()
            .conn
            .query_row(
                "SELECT assignment_id FROM role_assignments WHERE subject = ?1 \
                 AND permission = 'signoff_authority'",
                [subject],
                |r| r.get(0),
            )
            .unwrap()
    }
}

/// The binding of signoff `id` for record `record_id` at `revision`.
fn binding(id: &str, record_id: &str, revision: i64) -> NewSignoff {
    NewSignoff {
        signoff_id: id.into(),
        tenant_id: T.into(),
        project_id: P.into(),
        task_id: "task-1".into(),
        workflow_id: "wf-1".into(),
        from_state: "verifying".into(),
        to_state: "accepted".into(),
        acceptance_record_id: record_id.into(),
        criterion_id: "signoff".into(),
        artifact_snapshot_id: "snap-1".into(),
        artifact_digest: sha('b'),
        acceptance_policy_id: "accept-code-review".into(),
        acceptance_policy_version: "3".into(),
        acceptance_policy_bundle_sha: sha('c'),
        state_revision: revision,
        expires_at: Timestamp::parse_strict("2026-11-07T11:20:00Z").unwrap(),
    }
}

fn signoff_op(b: NewSignoff, added: Vec<SubjectId>) -> CeremonyOp {
    CeremonyOp::Signoff {
        binding: Box::new(b),
        parties: SignoffParties { added },
    }
}

/// Request and sign `so-1` for `acc-1` at revision 1 as `who`.
fn signed(w: &W, who: &str) -> Result<CeremonyOutcome, Refusal> {
    assert_eq!(
        w.request_signoff(&accept("k1", 1, "acc-1")),
        Ok(vec!["so-1".to_string()])
    );
    w.ceremony(
        &human(who),
        &signoff_op(binding("so-1", "acc-1", 1), vec![]),
        "so-k1",
    )
}

// ---------------------------------------------------------------------------
// X7 / S3: exclusions from authoritative state.

#[test]
fn signoff_exclusions_come_from_the_ledger_and_caller_input_only_adds() {
    // sara controls the requester identity (an explicit enrollment link, R1).
    let w = W::new("x7-linked", vec![requester()]);
    // No stored promotion context: nothing to sign.
    assert_eq!(
        w.begin(
            &human("sara"),
            &signoff_op(binding("so-1", "acc-1", 1), vec![]),
            "s0"
        )
        .err(),
        Some(ap(ApprovalCode::SignoffUnknown))
    );
    assert_eq!(
        w.request_signoff(&accept("k1", 1, "acc-1")),
        Ok(vec!["so-1".to_string()])
    );
    // S3 regression: before X7 the parties came from the caller, so a caller naming
    // anyone else as requester let sara (linked to the real requester) sign. Now the
    // stored requester is excluded whatever the caller passes.
    let op = signoff_op(
        binding("so-1", "acc-1", 1),
        vec![SubjectId::new("elsewhere", "someone-else")],
    );
    assert_eq!(
        w.ceremony(&human("sara"), &op, "s1"),
        Err(ap(ApprovalCode::SeparationViolated))
    );
    assert_eq!(w.count("SELECT count(*) FROM signoffs"), 0);
    // The displayed context names the exclusions; the stored signoff keeps them.
    w.enroll(person(
        "sid",
        &[(Permission::SignoffAuthority, T, P)],
        vec![],
    ))
    .unwrap();
    let out = w.ceremony(&human("sid"), &op, "s2").unwrap();
    assert_eq!(
        out.result,
        CeremonyResult::SignedOff {
            signoff_id: "so-1".into()
        }
    );
    let state = w.ledger.signoff("so-1").unwrap().unwrap();
    let ex = &state.signoff.fields().exclusions;
    assert_eq!(ex.requesters, [requester()]);
    assert_eq!(ex.linked_subjects, [human("sara")]);
    assert_eq!(
        ex.artifact_changers,
        [SubjectId::new("realm:ops", "ops:alice")]
    );
    assert_eq!(
        ex.policy_changers,
        [SubjectId::new("realm:ops", "ops:alice")]
    );
    assert_eq!(ex.added, [SubjectId::new("elsewhere", "someone-else")]);
    let shown = state.signoff.context();
    assert_eq!(
        shown
            .get("excluded")
            .and_then(|e| e.get("delegation_chain"))
            .and_then(Value::as_str),
        Some("unavailable")
    );
    // The challenge bound the fingerprint of exactly that context.
    let fingerprint: String = w
        .ledger
        .db()
        .conn
        .query_row(
            "SELECT context_fingerprint FROM challenges WHERE challenge_id = \
             (SELECT challenge_id FROM signoffs WHERE signoff_id = 'so-1')",
            [],
            |r| r.get(0),
        )
        .unwrap();
    assert_eq!(fingerprint, state.signoff.fingerprint().unwrap().as_str());
}

#[test]
fn an_exclusion_committed_between_begin_and_finish_refuses_the_signoff() {
    let w = W::new("x7-between", vec![]);
    assert_eq!(
        w.request_signoff(&accept("k1", 1, "acc-1")),
        Ok(vec!["so-1".to_string()])
    );
    let op = signoff_op(binding("so-1", "acc-1", 1), vec![]);
    let c = w.begin(&human("sara"), &op, "s1").unwrap();
    // Another requester asks for the same signoff before the finish.
    w.request_signoff(&accept_as("k2", 1, "acc-1", "agent:session-2"))
        .unwrap();
    assert_eq!(
        w.finish(&human("sara"), &op, &c.challenge_id, "s1"),
        Err(ap(ApprovalCode::ChallengeInvalid(
            ChallengeRefusal::ContextMismatch
        )))
    );
    // The artifact and policy changer (durable admin events) never signs off.
    w.enroll(person(
        "ops:alice",
        &[(Permission::SignoffAuthority, T, P)],
        vec![],
    ))
    .unwrap();
    assert_eq!(
        w.ceremony(&human("ops:alice"), &op, "s2"),
        Err(ap(ApprovalCode::SeparationViolated))
    );
    // A context that disagrees with the binding refuses.
    let mut other = binding("so-1", "acc-1", 1);
    other.criterion_id = "review".into();
    assert_eq!(
        w.begin(&human("sara"), &signoff_op(other, vec![]), "s3")
            .err(),
        Some(ap(ApprovalCode::SignoffMismatch))
    );
    assert!(w.ceremony(&human("sara"), &op, "s4").is_ok());
}

#[test]
fn a_signoff_request_needs_a_v2_record_naming_a_signoff_and_writes_no_authority() {
    let w = W::new("x7-request", vec![]);
    w.publish("acc-v1", 1, "att-v1", None);
    assert_eq!(
        w.request_signoff(&accept("k1", 1, "acc-v1")),
        Err(Refusal::Acceptance(
            writ_broker::acceptance::AcceptanceCode::HumanUnavailable
        ))
    );
    assert_eq!(
        w.request_signoff(&accept("k1", 1, "acc-none")),
        Err(Refusal::Acceptance(
            writ_broker::acceptance::AcceptanceCode::RecordUnknown
        ))
    );
    let mut swapped = accept("k1", 1, "acc-1");
    swapped = set(
        &swapped,
        &["action", "attributes", "artifact_snapshot_id"],
        json::s("snap-2"),
    );
    assert_eq!(
        w.request_signoff(&swapped),
        Err(Refusal::Acceptance(
            writ_broker::acceptance::AcceptanceCode::BindingMismatch
        ))
    );
    assert_eq!(w.count("SELECT count(*) FROM signoff_requests"), 0);
    // Identical requests are stored once.
    w.request_signoff(&accept("k1", 1, "acc-1")).unwrap();
    w.request_signoff(&accept("k1", 1, "acc-1")).unwrap();
    assert_eq!(w.count("SELECT count(*) FROM signoff_requests"), 1);
    // A /1 record with a human criterion keeps being refused at promotion.
    assert_eq!(
        w.promote(&accept("k9", 1, "acc-v1")),
        Err(Refusal::Acceptance(
            writ_broker::acceptance::AcceptanceCode::HumanUnavailable
        ))
    );
}

// ---------------------------------------------------------------------------
// M4-06: use-time verification and atomic consumption.

#[test]
fn a_valid_signoff_is_consumed_atomically_with_the_promotion_and_retries_replay() {
    let w = W::new("m406-commit", vec![]);
    signed(&w, "sara").unwrap();
    let before = w.sequence();
    assert_eq!(w.promote(&accept("k1", 1, "acc-1")), Ok(committed(2)));
    assert_eq!(w.state(), ("accepted".into(), 2));
    assert_eq!(
        w.count("SELECT count(*) FROM consumed_signoffs WHERE signoff_id = 'so-1' AND idempotency_key = 'k1'"),
        1
    );
    assert_eq!(
        w.count("SELECT count(*) FROM consumed_records WHERE acceptance_record_id = 'acc-1'"),
        1
    );
    assert!(w.ledger.signoff("so-1").unwrap().unwrap().consumed);
    // I-8: the signoff precedes the promotion that consumed it, each in its own row.
    let after = w.sequence();
    assert_eq!(after.len(), before.len() + 1);
    let last = after.last().unwrap();
    assert_eq!(
        (last.1.as_str(), last.2.as_str()),
        ("promotion", "promotion:wf-1:2")
    );
    assert!(before.iter().any(|(_, k, _)| k == "signoff"));
    // An identical retry replays the receipt; nothing is consumed again.
    assert_eq!(
        w.promote(&accept("k1", 1, "acc-1")),
        Ok(PromotionOutcome::Replayed {
            revision: 2,
            receipt_id: "promotion:wf-1:2".into()
        })
    );
    assert_eq!(w.count("SELECT count(*) FROM consumed_signoffs"), 1);
    assert_eq!(w.sequence().len(), after.len());
}

#[test]
fn a_consumed_signoff_never_authorizes_another_promotion() {
    let w = W::new("m406-consumed", vec![]);
    signed(&w, "sara").unwrap();
    assert_eq!(w.promote(&accept("k1", 1, "acc-1")), Ok(committed(2)));
    // Back to verifying (unprotected), then a fresh record naming the consumed signoff.
    assert_eq!(
        w.promote(&transition("k2", "accepted", "verifying", 2, None)),
        Ok(committed(3))
    );
    w.publish("acc-2", 3, "att-2", Some("so-1"));
    let refusals = w.count("SELECT count(*) FROM audit_refusals");
    // The signoff is bound to acc-1 at revision 1: a mismatch before it is ever reused.
    assert_eq!(
        w.promote(&accept("k3", 3, "acc-2")),
        Err(ap(ApprovalCode::SignoffMismatch))
    );
    assert_eq!(w.state(), ("verifying".into(), 3));
    assert_eq!(w.count("SELECT count(*) FROM consumed_records"), 1);
    assert_eq!(w.count("SELECT count(*) FROM audit_refusals"), refusals + 1);
}

#[test]
fn an_unknown_or_unsigned_signoff_refuses_and_changes_nothing() {
    let w = W::new("m406-unknown", vec![]);
    // Forged reference: the record names a signoff nobody signed.
    assert_eq!(
        w.promote(&accept("k1", 1, "acc-1")),
        Err(ap(ApprovalCode::SignoffUnknown))
    );
    assert_eq!(w.state(), ("verifying".into(), 1));
    assert_eq!(w.count("SELECT count(*) FROM consumed_records"), 0);
    assert_eq!(w.count("SELECT count(*) FROM consumed_signoffs"), 0);
    // The key stays usable: sign, then the same key commits.
    signed(&w, "sara").unwrap();
    assert_eq!(w.promote(&accept("k1", 1, "acc-1")), Ok(committed(2)));
}

#[test]
fn an_expired_signoff_refuses_at_trusted_time() {
    let w = W::new("m406-expired", vec![]);
    signed(&w, "sara").unwrap();
    w.clock
        .set(Timestamp::parse_strict("2026-11-07T11:25:00Z").unwrap());
    assert_eq!(
        w.promote(&accept("k1", 1, "acc-1")),
        Err(ap(ApprovalCode::SignoffExpired))
    );
    assert_eq!(w.count("SELECT count(*) FROM consumed_signoffs"), 0);
}

#[test]
fn separation_is_rechecked_against_the_promoting_requester() {
    // sara controls agent:session-2, which never asked for the signoff.
    let w = W::new(
        "m406-separation",
        vec![SubjectId::new(ADAPTER, "agent:session-2")],
    );
    signed(&w, "sara").unwrap();
    assert_eq!(
        w.promote(&accept_as("k1", 1, "acc-1", "agent:session-2")),
        Err(ap(ApprovalCode::SeparationViolated))
    );
    assert_eq!(w.state(), ("verifying".into(), 1));
    // Another requester commits.
    assert_eq!(w.promote(&accept("k2", 1, "acc-1")), Ok(committed(2)));
}

#[test]
fn revoking_the_signoff_credential_subject_or_role_before_promotion_refuses() {
    for (n, target) in ["signoff", "credential", "subject", "role"]
        .iter()
        .enumerate()
    {
        let w = W::new(&format!("m406-revoked-{target}"), vec![]);
        signed(&w, "sara").unwrap();
        let (who, t) = revocation(&w, target);
        w.revoke(who, t, &format!("r{n}")).unwrap();
        assert_eq!(
            w.promote(&accept("k1", 1, "acc-1")),
            Err(ap(ApprovalCode::SignoffRevoked)),
            "{target}"
        );
        assert_eq!(w.state(), ("verifying".into(), 1), "{target}");
        assert_eq!(
            w.count("SELECT count(*) FROM consumed_signoffs"),
            0,
            "{target}"
        );
    }
}

fn revocation(w: &W, target: &str) -> (&'static str, RevocationTarget) {
    match target {
        "signoff" => ("carol", RevocationTarget::Signoff("so-1".into())),
        "credential" => ("root", RevocationTarget::Credential("cred-sara".into())),
        "subject" => ("root", RevocationTarget::Subject(human("sara"))),
        _ => ("root", RevocationTarget::Role(w.role_of("sara"))),
    }
}

#[test]
fn sixteen_concurrent_promotions_consume_the_signoff_once() {
    let w = W::new("m406-race", vec![]);
    signed(&w, "sara").unwrap();
    let barrier = Barrier::new(16);
    let results: Vec<Result<PromotionOutcome, Refusal>> = thread::scope(|s| {
        let handles: Vec<_> = (0..16)
            .map(|i| {
                let (w, barrier) = (&w, &barrier);
                s.spawn(move || {
                    barrier.wait();
                    w.promote(&accept(&format!("race-{i}"), 1, "acc-1"))
                })
            })
            .collect();
        handles.into_iter().map(|h| h.join().unwrap()).collect()
    });
    assert_eq!(results.iter().filter(|r| r.is_ok()).count(), 1);
    assert!(results
        .iter()
        .filter(|r| r.is_err())
        .all(|r| r.as_ref().unwrap_err().code() == "REVISION_CONFLICT"));
    assert_eq!(w.count("SELECT count(*) FROM consumed_signoffs"), 1);
    assert_eq!(
        w.count("SELECT count(*) FROM ledger_sequence WHERE kind = 'promotion'"),
        2
    );
}

// ---------------------------------------------------------------------------
// M4-12 / CO-PROP-02: the actual order of concurrent revocation and promotion.

/// What one race committed: the promotion's answer, the revocation's answer, the
/// ledger-wide order and the revocation's recorded use state.
struct Race {
    promotion: Result<PromotionOutcome, Refusal>,
    revocation: Result<CeremonyOutcome, Refusal>,
    /// Sequence number of the promotion row, if one committed.
    promotion_seq: Option<i64>,
    /// Sequence number of the revocation row, if one committed.
    revocation_seq: Option<i64>,
    use_state: Option<String>,
    consumed: i64,
}

/// The exact ordering rule (checked from durable evidence, not a set of allowed outcomes):
/// the revocation always commits; the promotion commits if and only if its sequence row
/// precedes the revocation's, and then the revocation records the signoff `consumed` (for
/// a signoff target) and the signoff is consumed once; otherwise the promotion wrote no
/// row, consumed nothing and refused `SIGNOFF_REVOKED`, and a signoff revocation recorded
/// `unused`.
fn check_order(target: &str, r: &Race) -> Result<(), String> {
    let rev = r
        .revocation_seq
        .ok_or("the revocation did not commit a sequence row")?;
    if r.revocation.is_err() {
        return Err("the revocation was refused".into());
    }
    let signoff_target = target == "signoff";
    match (&r.promotion, r.promotion_seq) {
        (Ok(PromotionOutcome::Committed { .. }), Some(p)) => {
            if p >= rev {
                return Err(format!(
                    "promotion committed at {p} after the revocation at {rev}"
                ));
            }
            if r.consumed != 1 {
                return Err("committed promotion did not consume the signoff once".into());
            }
            let want = if signoff_target {
                "consumed"
            } else {
                "not_applicable"
            };
            if r.use_state.as_deref() != Some(want) {
                return Err(format!("use state {:?}, want {want}", r.use_state));
            }
        }
        (Err(e), None) => {
            if e.code() != "SIGNOFF_REVOKED" {
                return Err(format!(
                    "promotion refused {}, want SIGNOFF_REVOKED",
                    e.code()
                ));
            }
            if r.consumed != 0 {
                return Err("a refused promotion consumed the signoff".into());
            }
            let want = if signoff_target {
                "unused"
            } else {
                "not_applicable"
            };
            if r.use_state.as_deref() != Some(want) {
                return Err(format!("use state {:?}, want {want}", r.use_state));
            }
        }
        (p, s) => return Err(format!("promotion {p:?} with sequence row {s:?}")),
    }
    Ok(())
}

fn race(n: usize, target: &str) -> Race {
    let w = W::new(&format!("m412-{target}-{n}"), vec![]);
    signed(&w, "sara").unwrap();
    let (who, t) = revocation(&w, target);
    let op = CeremonyOp::Revoke {
        target: t,
        reason: "race".into(),
    };
    let c = w.begin(&human(who), &op, "race-r").unwrap();
    let barrier = Barrier::new(2);
    let (promotion, revocation) = thread::scope(|s| {
        let p = s.spawn(|| {
            barrier.wait();
            w.promote(&accept("race-p", 1, "acc-1"))
        });
        let r = s.spawn(|| {
            barrier.wait();
            // Stagger the revocation (0 to 33 ms) so both commit orders occur; the rule is
            // checked from the recorded order whichever commits first.
            thread::sleep(std::time::Duration::from_micros(n as u64 * 3_000));
            w.finish(&human(who), &op, &c.challenge_id, "race-r")
        });
        (p.join().unwrap(), r.join().unwrap())
    });
    let seq = |kind: &str| {
        w.sequence()
            .into_iter()
            .filter(|(_, k, r)| k == kind && (kind != "promotion" || r == "promotion:wf-1:2"))
            .map(|(s, _, _)| s)
            .next_back()
    };
    let use_state = w
        .ledger
        .db()
        .conn
        .query_row(
            "SELECT use_state FROM revocations ORDER BY seq DESC LIMIT 1",
            [],
            |r| r.get(0),
        )
        .ok();
    Race {
        promotion_seq: seq("promotion"),
        revocation_seq: seq("revocation"),
        use_state,
        consumed: w.count("SELECT count(*) FROM consumed_signoffs"),
        promotion,
        revocation,
    }
}

#[test]
fn concurrent_revocation_and_promotion_follow_the_recorded_ledger_order() {
    let mut seen = std::collections::BTreeSet::new();
    for target in ["signoff", "credential", "subject", "role"] {
        for n in 0..12 {
            let r = race(n, target);
            if let Err(e) = check_order(target, &r) {
                panic!("{target} trial {n}: {e}");
            }
            seen.insert((target, r.promotion.is_ok()));
        }
    }
    // Both commit orders occurred for every target (the staggering makes the late
    // revocation lose to the promotion; the rule above holds for whichever committed).
    for target in ["signoff", "credential", "subject", "role"] {
        assert!(
            seen.contains(&(target, true)),
            "{target}: no promotion-first trial"
        );
        assert!(
            seen.contains(&(target, false)),
            "{target}: no revocation-first trial"
        );
    }
}

#[test]
fn the_ordering_rule_rejects_mutant_histories() {
    let committed_race = || Race {
        promotion: Ok(committed(2)),
        revocation: Err(ap(ApprovalCode::SignoffRevoked)),
        promotion_seq: Some(10),
        revocation_seq: Some(11),
        use_state: Some("consumed".into()),
        consumed: 1,
    };
    let ok_revocation = || {
        Ok(CeremonyOutcome {
            result: CeremonyResult::Revoked {
                use_state: UseState::Consumed,
            },
            ledger_seq: 11,
            replayed: false,
        })
    };
    let good = Race {
        revocation: ok_revocation(),
        ..committed_race()
    };
    assert_eq!(check_order("signoff", &good), Ok(()));
    // Mutants: wrong revocation precedence (promotion after the revocation).
    let late = Race {
        promotion_seq: Some(12),
        revocation: ok_revocation(),
        ..committed_race()
    };
    assert!(check_order("signoff", &late).is_err());
    // Hidden consumption: a refused promotion that consumed the signoff.
    let hidden = Race {
        promotion: Err(ap(ApprovalCode::SignoffRevoked)),
        promotion_seq: None,
        use_state: Some("unused".into()),
        consumed: 1,
        revocation: ok_revocation(),
        ..committed_race()
    };
    assert!(check_order("signoff", &hidden).is_err());
    // A committed promotion without its sequence row.
    let unordered = Race {
        promotion_seq: None,
        revocation: ok_revocation(),
        ..committed_race()
    };
    assert!(check_order("signoff", &unordered).is_err());
    // A revocation recorded as unused although the promotion consumed first.
    let misrecorded = Race {
        use_state: Some("unused".into()),
        revocation: ok_revocation(),
        ..committed_race()
    };
    assert!(check_order("signoff", &misrecorded).is_err());
    // A refused promotion with another code than SIGNOFF_REVOKED.
    let other = Race {
        promotion: Err(ap(ApprovalCode::SignoffUnknown)),
        promotion_seq: None,
        use_state: Some("unused".into()),
        consumed: 0,
        revocation: ok_revocation(),
        ..committed_race()
    };
    assert!(check_order("signoff", &other).is_err());
    // A refused revocation.
    assert!(check_order("signoff", &committed_race()).is_err());
}
