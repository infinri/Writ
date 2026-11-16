//! M4 approval lifecycle tests on the SQLite ledger (M4-03, M4-04, M4-05, M4-08 storage,
//! M4-09, M4-10; D-040 O-3..O-9, O-14; M4-INTERFACES I-2..I-8, C1, C2): stored approval
//! requests, ceremonies through a test verifier (test code only; `VerifierCapability` is
//! allowed here), terminal denial, swapped and replayed decisions, expiry, separation and
//! eligibility, enrollment, revocation against T-reserve and T-dispatch, 16-way races,
//! migration from `writ-ledger-5` with legacy grants, unavailable storage and unknown
//! commits.

#![allow(clippy::unwrap_used, clippy::expect_used, clippy::panic)]

use std::sync::atomic::Ordering;
use std::sync::{Arc, Barrier};
use std::thread;

use writ_broker::approval::{ApprovalCode, ApprovalStatus, ChallengeRefusal, RevocationTarget};
use writ_broker::approval_store::{
    ApprovalLedger, BootstrapEnrollment, CeremonyBegin, CeremonyFinish, CeremonyOp,
    CeremonyOutcome, CeremonyResult, IssuedChallenge, NewEnrollment, NewRole, OperatorSession,
    RegistrationBegin, RegistrationFinish, RegistrationLedger, UseState, REGISTRATION_TTL_SECONDS,
};
use writ_broker::clock::{observe, ObservedTime};
use writ_broker::execute::{Executed, Execution};
use writ_broker::grants::{approval_grant_label, GrantLabel, Replay, ReserveOutcome};
use writ_broker::human::{
    Permission, PrincipalKind, RegistrationBinding, RegistrationVerifier, SubjectId,
    VerifiedRegistration, VerifierError, ES256,
};
use writ_broker::refusal::Refusal;
use writ_broker::store::{ExecuteLedger, StoreError, TxStage};
use writ_broker::writ_kernel::json::{self, Value};

use super::execute_tests::World;
use super::grant_tests::{approve, label, now, publish, revision, set, uses, verify};
use super::m4_support::{
    bootstrap_root_at, channel_of, enroll_op, forged_registration, operator_register,
    operator_session, registered, session_register, start_op, start_registration, TestVerifier,
    ORIGIN, TEST_SESSION,
};
use super::{fault, BootstrapSession, SqliteLedger, SCHEMA_V5, SCHEMA_VERSION_V5};

const T: &str = "fixture-tenant";
const P: &str = "fixture-project";
const ADAPTER: &str = "writ-host-adapter:fixture";
const REQUESTER: &str = "agent:claude-session-1";

// ---------------------------------------------------------------------------
// World.

pub(crate) fn human(s: &str) -> SubjectId {
    SubjectId::new("writ-local", s)
}

pub(crate) fn root() -> SubjectId {
    human("root")
}

fn requester() -> SubjectId {
    SubjectId::new(ADAPTER, REQUESTER)
}

pub(crate) fn cred(s: &SubjectId) -> String {
    format!("cred-{}", s.subject)
}

pub(crate) fn ok(s: &SubjectId) -> String {
    format!("ok:{}", cred(s))
}

/// A human enrollment without its credential yet: [`M4::registered`] (and [`M4::enroll`])
/// add one through the ledger's registration ceremony (S5, X8).
pub(crate) fn person(
    name: &str,
    roles: &[(Permission, &str, &str)],
    linked: Vec<SubjectId>,
) -> NewEnrollment {
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

/// `repository.publish` (require_approval) with a distinct action digest per `n`.
pub(crate) fn action(idem: &str, n: u32) -> Value {
    set(
        &publish(idem),
        &["action", "action_digest"],
        json::s(&digest_text(n)),
    )
}

pub(crate) fn digest_text(n: u32) -> String {
    format!("sha256:{n:064x}")
}

pub(crate) fn ap(code: ApprovalCode) -> Refusal {
    Refusal::Approval(code)
}

pub(crate) fn challenge(r: ChallengeRefusal) -> Refusal {
    ap(ApprovalCode::ChallengeInvalid(r))
}

static REOPENS: std::sync::atomic::AtomicU64 = std::sync::atomic::AtomicU64::new(0);

pub(crate) struct M4 {
    pub(crate) w: World,
    pub(crate) v: Arc<TestVerifier>,
}

impl M4 {
    /// A fresh ledger with the bootstrap administrator `root`, approver `alice` and
    /// revoker `carol` in (fixture-tenant, fixture-project).
    pub(crate) fn new(name: &str) -> M4 {
        let mut w = World::new(&format!("m4-{name}"));
        let v = Arc::new(TestVerifier::default());
        // The bootstrap runs through its own entry point, which owns the ledger file
        // exclusively: release the world's ownership for it, then take it back.
        let path = w.dir.join("ledger.db");
        let held = std::mem::replace(
            &mut w.ledger,
            SqliteLedger::create(&w.dir.join("placeholder.db")).unwrap(),
        );
        drop(held);
        assert_eq!(bootstrap_root_at(&path, &w.clock, &v, &root()), Ok(1));
        drop(std::mem::replace(
            &mut w.ledger,
            SqliteLedger::open(&path)
                .unwrap()
                .with_approval_channel(channel_of(&v))
                .unwrap(),
        ));
        let m = M4 { w, v };
        m.enroll(person("alice", &[(Permission::Approver, T, P)], vec![]))
            .unwrap();
        m.enroll(person("carol", &[(Permission::Revoker, T, P)], vec![]))
            .unwrap();
        m
    }

    pub(crate) fn l(&self) -> &SqliteLedger {
        &self.w.ledger
    }

    /// Close the ledger and open it again with `v` as its approval channel (a restart
    /// with another configured channel, M1). Returns the previous verifier.
    pub(crate) fn reopen_with(&mut self, v: &Arc<TestVerifier>) -> Arc<TestVerifier> {
        let path = self.w.dir.join("ledger.db");
        let n = REOPENS.fetch_add(1, Ordering::SeqCst);
        drop(std::mem::replace(
            &mut self.w.ledger,
            SqliteLedger::create(&self.w.dir.join(format!("placeholder-{n}.db"))).unwrap(),
        ));
        drop(std::mem::replace(
            &mut self.w.ledger,
            SqliteLedger::open(&path)
                .unwrap()
                .with_approval_channel(channel_of(v))
                .unwrap(),
        ));
        std::mem::replace(&mut self.v, v.clone())
    }

    /// Finish the registration row `id` with `response`.
    pub(crate) fn finish_registration(
        &self,
        id: &str,
        response: &str,
    ) -> Result<VerifiedRegistration, Refusal> {
        self.l()
            .finish_registration(&RegistrationFinish {
                registration_id: id,
                response: response.as_bytes(),
                operator: Some(&operator_session(&root())),
                now: self.now(),
            })
            .expect("store")
    }

    pub(crate) fn now(&self) -> ObservedTime {
        let mut l: &SqliteLedger = &self.w.ledger;
        observe(&self.w.clock, &mut l).unwrap()
    }

    pub(crate) fn begin(
        &self,
        who: &SubjectId,
        op: &CeremonyOp,
        key: &str,
    ) -> Result<IssuedChallenge, Refusal> {
        self.l()
            .begin_ceremony(&CeremonyBegin {
                op,
                subject: who,
                idempotency_key: key,
                now: self.now(),
            })
            .expect("store")
    }

    pub(crate) fn finish(
        &self,
        op: &CeremonyOp,
        challenge_id: &str,
        response: &str,
        key: &str,
    ) -> Result<CeremonyOutcome, Refusal> {
        self.l()
            .finish_ceremony(&CeremonyFinish {
                op,
                challenge_id,
                response: response.as_bytes(),
                idempotency_key: key,
                now: self.now(),
            })
            .expect("store")
    }

    pub(crate) fn ceremony(
        &self,
        who: &SubjectId,
        op: &CeremonyOp,
        key: &str,
    ) -> Result<CeremonyOutcome, Refusal> {
        let c = self.begin(who, op, key)?;
        self.finish(op, &c.challenge_id, &ok(who), key)
    }

    /// A credential for `subject` from the serving ledger's `enroll` registration ceremony.
    pub(crate) fn register(
        &self,
        subject: &SubjectId,
        response: &str,
    ) -> Result<VerifiedRegistration, Refusal> {
        operator_register(self.l(), &|| self.now(), subject, response, &root())
    }

    /// `e` with a registered credential `cred-<name>` when it is a human without one.
    pub(crate) fn registered(&self, e: NewEnrollment) -> NewEnrollment {
        registered(self.l(), &|| self.now(), &root(), e)
    }

    pub(crate) fn enroll(&self, e: NewEnrollment) -> Result<CeremonyOutcome, Refusal> {
        let e = self.registered(e);
        let key = format!("enroll-{}", e.subject.subject);
        self.ceremony(&root(), &enroll_op(e), &key)
    }

    pub(crate) fn exec(&self, req: &Value, grant: Option<&GrantLabel>) -> Execution {
        self.w.broker().execute(&verify(req), revision(), grant)
    }

    /// Ask for action `n` (APPROVAL_REQUIRED) and return the stored request id.
    pub(crate) fn ask(&self, idem: &str, n: u32) -> String {
        let e = self.exec(&action(idem, n), None);
        assert_eq!(e.result, Err(Refusal::ApprovalRequired));
        assert!(
            e.evidence.transactions.is_empty(),
            "no lifecycle transaction"
        );
        self.text(
            "SELECT request_id FROM approval_requests WHERE instr(context, ?1) > 0",
            &digest_text(n),
        )
        .expect("stored request")
    }

    /// Ask for action `n` and have `alice` approve it; returns (request id, grant).
    pub(crate) fn approved(&self, idem: &str, n: u32) -> (String, GrantLabel) {
        let id = self.ask(idem, n);
        let op = CeremonyOp::Approve {
            request_id: id.clone(),
        };
        let out = self
            .ceremony(&human("alice"), &op, &format!("approve-{n}"))
            .unwrap();
        let grant = approval_grant_label(&id).unwrap();
        assert_eq!(
            out.result,
            CeremonyResult::Approved {
                grant: grant.clone()
            }
        );
        (id, grant)
    }

    pub(crate) fn revoke(
        &self,
        who: &SubjectId,
        target: RevocationTarget,
        key: &str,
    ) -> Result<CeremonyOutcome, Refusal> {
        let op = CeremonyOp::Revoke {
            target,
            reason: "test".into(),
        };
        self.ceremony(who, &op, key)
    }

    pub(crate) fn count(&self, sql: &str) -> i64 {
        self.l().db().conn.query_row(sql, [], |r| r.get(0)).unwrap()
    }

    pub(crate) fn text(&self, sql: &str, arg: &str) -> Option<String> {
        self.l().db().conn.query_row(sql, [arg], |r| r.get(0)).ok()
    }

    pub(crate) fn status(&self, request_id: &str) -> ApprovalStatus {
        self.l().approval_request(request_id).unwrap().unwrap().1
    }

    /// `<status> <refusal code>` of a stored challenge (the code empty unless refused).
    pub(crate) fn challenge_status(&self, challenge_id: &str) -> String {
        self.text(
            "SELECT status || ' ' || coalesce(refusal_code, '') FROM challenges \
             WHERE challenge_id = ?1",
            challenge_id,
        )
        .unwrap()
    }

    pub(crate) fn receipt(&self, idem: &str) -> Option<String> {
        self.text(
            "SELECT status FROM receipts WHERE idempotency_key = ?1",
            idem,
        )
    }
}

// ---------------------------------------------------------------------------
// M4-03: stored approval requests.

#[test]
fn require_approval_without_a_grant_stores_one_immutable_pending_request() {
    let m = M4::new("request");
    let id = m.ask("k1", 1);
    let (request, status) = m.l().approval_request(&id).unwrap().unwrap();
    assert_eq!(status, ApprovalStatus::Pending);
    let r = request.fields();
    assert_eq!((r.tenant_id.as_str(), r.project_id.as_str()), (T, P));
    assert_eq!(r.requester, requester());
    assert_eq!(r.agent, None);
    assert_eq!(r.action_digest, digest_text(1));
    assert_eq!(
        r.action.get("kind").and_then(Value::as_str),
        Some("repository.publish")
    );
    assert_eq!(r.expires_at, now().plus_seconds(600));
    // The agent's answer is unchanged and generic.
    let e = m.exec(&action("k2", 1), None);
    assert_eq!(e.result, Err(Refusal::ApprovalRequired));
    // The same action under another key reuses the one request; nothing is replaced.
    assert_eq!(m.count("SELECT count(*) FROM approval_requests"), 1);
    assert_eq!(
        m.count("SELECT count(*) FROM approval_events WHERE kind = 'request_created'"),
        1
    );
    assert_eq!(m.l().approval_request(&id).unwrap().unwrap().0, request);
    // An allowed request never creates one.
    let allowed = super::grant_tests::file_write("k3");
    assert_eq!(m.exec(&allowed, None).result, Ok(Executed::EffectCompleted));
    assert_eq!(m.count("SELECT count(*) FROM approval_requests"), 1);
}

#[test]
fn an_approval_mints_one_provenance_v2_grant_the_agent_can_use_once() {
    let m = M4::new("approve");
    let (id, grant) = m.approved("k1", 1);
    assert_eq!(m.status(&id), ApprovalStatus::Approved);
    assert_eq!(m.count("SELECT count(*) FROM grants"), 1);
    assert_eq!(
        m.text(
            "SELECT version || ' ' || approver_issuer || ' ' || approver_id || ' ' || channel \
             FROM grant_provenance WHERE label = ?1",
            grant.as_str()
        )
        .as_deref(),
        Some("writ-provenance-2 writ-local alice approval-listener")
    );
    assert!(m
        .text(
            "SELECT auth_evidence_ref FROM grant_provenance WHERE label = ?1",
            grant.as_str()
        )
        .unwrap()
        .starts_with("challenge:chl-"));
    // Provenance v2 events, no credential material anywhere.
    assert_eq!(
        m.count(
            "SELECT count(*) FROM approval_events WHERE kind = 'approved' \
             AND provenance_version = 'writ-provenance-2' AND credential_id = 'cred-alice' \
             AND ledger_seq IS NOT NULL"
        ),
        1
    );
    assert_eq!(
        m.count("SELECT count(*) FROM approval_events WHERE instr(coalesce(credential_id, ''), 'pk-') > 0"),
        0
    );
    assert_eq!(
        m.count("SELECT count(*) FROM ledger_sequence WHERE kind = 'decision'"),
        1
    );
    let e = m.exec(&action("k1", 1), Some(&grant));
    assert_eq!(e.result, Ok(Executed::EffectCompleted));
    assert_eq!(uses(m.l(), grant.as_str()), Some(1));
    // Single use: another key for the same action cannot reuse it.
    assert_eq!(
        m.exec(&action("k9", 1), Some(&grant)).result,
        Err(Refusal::GrantExhausted)
    );
    // The retry of the first key replays.
    assert_eq!(
        m.exec(&action("k1", 1), Some(&grant)).result,
        Ok(Executed::Replayed(Replay::EffectCompleted))
    );
}

#[test]
fn a_denied_request_is_terminal_never_reopened_or_replaced() {
    let m = M4::new("deny");
    let id = m.ask("k1", 1);
    let deny = CeremonyOp::Deny {
        request_id: id.clone(),
    };
    let out = m.ceremony(&human("alice"), &deny, "d1").unwrap();
    assert_eq!(out.result, CeremonyResult::Denied);
    assert_eq!(m.status(&id), ApprovalStatus::Denied);
    assert_eq!(m.count("SELECT count(*) FROM grants"), 0);
    // No reopening: neither approval nor another denial.
    let approve = CeremonyOp::Approve {
        request_id: id.clone(),
    };
    assert_eq!(
        m.begin(&human("alice"), &approve, "a1").err(),
        Some(ap(ApprovalCode::RequestClosed))
    );
    assert_eq!(
        m.begin(&human("alice"), &deny, "d2").err(),
        Some(ap(ApprovalCode::RequestClosed))
    );
    // No replacement: the agent asking again keeps the denied request.
    assert_eq!(
        m.exec(&action("k2", 1), None).result,
        Err(Refusal::ApprovalRequired)
    );
    assert_eq!(m.count("SELECT count(*) FROM approval_requests"), 1);
    assert_eq!(m.status(&id), ApprovalStatus::Denied);
    let label = approval_grant_label(&id).unwrap();
    assert_eq!(
        m.exec(&action("k2", 1), Some(&label)).result,
        Err(Refusal::GrantUnknown)
    );
    // A challenge issued before the denial cannot approve afterwards.
    let id2 = m.ask("k3", 2);
    let approve2 = CeremonyOp::Approve {
        request_id: id2.clone(),
    };
    let early = m.begin(&human("alice"), &approve2, "a2").unwrap();
    m.ceremony(
        &human("alice"),
        &CeremonyOp::Deny {
            request_id: id2.clone(),
        },
        "d3",
    )
    .unwrap();
    assert_eq!(
        m.finish(&approve2, &early.challenge_id, &ok(&human("alice")), "a2"),
        Err(ap(ApprovalCode::RequestClosed))
    );
    assert_eq!(m.status(&id2), ApprovalStatus::Denied);
    assert_eq!(m.count("SELECT count(*) FROM grants"), 0);
}

#[test]
fn swapped_decisions_and_swapped_targets_are_refused() {
    let m = M4::new("swap");
    let alice = human("alice");
    let a = m.ask("k1", 1);
    let b = m.ask("k2", 2);
    let approve_a = CeremonyOp::Approve {
        request_id: a.clone(),
    };
    // The verdict comes from the challenge: a deny presented on an approve challenge.
    let deny_a = CeremonyOp::Deny {
        request_id: a.clone(),
    };
    let c = m.begin(&alice, &approve_a, "x1").unwrap();
    assert_eq!(
        m.finish(&deny_a, &c.challenge_id, &ok(&alice), "x1"),
        Err(challenge(ChallengeRefusal::OperationMismatch))
    );
    // X4: that refusal burned the challenge; its own decision cannot use it any more.
    assert_eq!(
        m.challenge_status(&c.challenge_id),
        "refused CHALLENGE_INVALID"
    );
    assert_eq!(
        m.finish(&approve_a, &c.challenge_id, &ok(&alice), "x1"),
        Err(challenge(ChallengeRefusal::Consumed))
    );
    // Another request presented on request a's challenge.
    let approve_b = CeremonyOp::Approve {
        request_id: b.clone(),
    };
    let c = m.begin(&alice, &approve_a, "x2").unwrap();
    assert_eq!(
        m.finish(&approve_b, &c.challenge_id, &ok(&alice), "x2"),
        Err(challenge(ChallengeRefusal::TargetMismatch))
    );
    assert_eq!(
        m.challenge_status(&c.challenge_id),
        "refused CHALLENGE_INVALID"
    );
    // Nothing changed; a fresh challenge decides exactly its own request.
    assert_eq!(m.status(&a), ApprovalStatus::Pending);
    assert_eq!(m.status(&b), ApprovalStatus::Pending);
    let c = m.begin(&alice, &approve_a, "x3").unwrap();
    assert!(m
        .finish(&approve_a, &c.challenge_id, &ok(&alice), "x3")
        .is_ok());
    assert_eq!(m.status(&b), ApprovalStatus::Pending);
    assert_eq!(
        m.count("SELECT count(*) FROM audit_refusals WHERE code = 'CHALLENGE_INVALID'"),
        3
    );
}

#[test]
fn replayed_consumed_forged_and_mismatched_assertions_never_decide_twice() {
    let m = M4::new("replay");
    let alice = human("alice");
    let id = m.ask("k1", 1);
    let op = CeremonyOp::Approve {
        request_id: id.clone(),
    };
    // Before the decision: verifier refusals and binding mismatches, each on its own
    // challenge (X4: every one of these terminal refusals burns the challenge it named).
    m.enroll(person("dave", &[(Permission::Approver, T, P)], vec![]))
        .unwrap();
    let cases: [(&str, &str, Refusal); 6] = [
        (
            "forged",
            "key-1",
            ap(ApprovalCode::AssertionInvalid(VerifierError::Malformed)),
        ),
        (
            "bad:cred-alice",
            "key-1",
            ap(ApprovalCode::AssertionInvalid(
                VerifierError::SignatureInvalid,
            )),
        ),
        (
            "nouv:cred-alice",
            "key-1",
            ap(ApprovalCode::AssertionInvalid(
                VerifierError::UserNotVerified,
            )),
        ),
        (
            "other:cred-alice",
            "key-1",
            challenge(ChallengeRefusal::CeremonyMismatch),
        ),
        (
            "ok:cred-alice",
            "key-2",
            challenge(ChallengeRefusal::IdempotencyMismatch),
        ),
        // Another approver cannot answer alice's challenge.
        (
            "ok:cred-dave",
            "key-1",
            ap(ApprovalCode::AssertionInvalid(
                VerifierError::CredentialUnknown,
            )),
        ),
    ];
    for (response, key, want) in cases {
        let c = m.begin(&alice, &op, "key-1").unwrap();
        assert_eq!(
            m.finish(&op, &c.challenge_id, response, key),
            Err(want.clone())
        );
        assert_eq!(
            m.challenge_status(&c.challenge_id),
            format!("refused {}", want.code()),
            "{response}"
        );
        // A retry on the burned challenge, even a valid one, is consumed.
        assert_eq!(
            m.finish(&op, &c.challenge_id, &ok(&alice), "key-1"),
            Err(challenge(ChallengeRefusal::Consumed)),
            "{response}"
        );
    }
    assert_eq!(
        m.finish(&op, "chl-unknown", &ok(&alice), "key-1"),
        Err(challenge(ChallengeRefusal::Unknown))
    );
    assert_eq!(m.status(&id), ApprovalStatus::Pending);
    // The decision, with a credential update persisted atomically.
    let c = m.begin(&alice, &op, "key-1").unwrap();
    let first = m
        .finish(&op, &c.challenge_id, "update:cred-alice", "key-1")
        .unwrap();
    assert!(!first.replayed);
    assert_eq!(
        m.l()
            .db()
            .conn
            .query_row(
                "SELECT public_material FROM credentials WHERE credential_id = 'cred-alice'",
                [],
                |r| r.get::<_, Vec<u8>>(0)
            )
            .unwrap(),
        b"material-2".to_vec()
    );
    // An identical retry replays without a second grant or decision.
    let again = m
        .finish(&op, &c.challenge_id, &ok(&alice), "key-1")
        .unwrap();
    assert!(again.replayed);
    assert_eq!(
        (again.result.clone(), again.ledger_seq),
        (first.result.clone(), first.ledger_seq)
    );
    // X3 (S4): anything else on the consumed challenge is a binding mismatch, never the
    // earlier result, and never burns or changes the committed decision.
    assert_eq!(
        m.finish(&op, &c.challenge_id, &ok(&alice), "key-3"),
        Err(challenge(ChallengeRefusal::BindingMismatch))
    );
    let deny = CeremonyOp::Deny {
        request_id: id.clone(),
    };
    assert_eq!(
        m.finish(&deny, &c.challenge_id, &ok(&alice), "key-1"),
        Err(challenge(ChallengeRefusal::BindingMismatch))
    );
    assert_eq!(m.challenge_status(&c.challenge_id), "consumed ");
    assert_eq!(m.count("SELECT count(*) FROM grants"), 1);
    assert_eq!(m.count("SELECT count(*) FROM approval_decisions"), 1);
    assert_eq!(
        m.count("SELECT count(*) FROM ledger_sequence WHERE kind = 'decision'"),
        1
    );
}

#[test]
fn challenge_ids_and_digests_are_single_use_ledger_wide() {
    let m = M4::new("c1");
    let alice = human("alice");
    let a = m.ask("k1", 1);
    let b = m.ask("k2", 2);
    m.v.fixed.store(true, Ordering::SeqCst);
    let op_a = CeremonyOp::Approve { request_id: a };
    let op_b = CeremonyOp::Approve { request_id: b };
    assert!(m.begin(&alice, &op_a, "x1").is_ok());
    // The same ceremony challenge cannot be stored twice (C1).
    assert_eq!(
        m.begin(&alice, &op_b, "x2").err(),
        Some(challenge(ChallengeRefusal::Malformed))
    );
    assert_eq!(
        m.count("SELECT count(*) FROM challenges WHERE operation = 'approve'"),
        1
    );
}

#[test]
fn expired_challenges_and_requests_are_refused_and_expiry_is_never_early() {
    let m = M4::new("expiry");
    let alice = human("alice");
    let id = m.ask("k1", 1);
    let op = CeremonyOp::Approve {
        request_id: id.clone(),
    };
    let c = m.begin(&alice, &op, "x1").unwrap();
    assert_eq!(c.expires_at, now().plus_seconds(120));
    m.w.clock.set(now().plus_seconds(120));
    assert_eq!(
        m.finish(&op, &c.challenge_id, &ok(&alice), "x1"),
        Err(challenge(ChallengeRefusal::Expired))
    );
    // The expiry step: nothing before the request expires, then exactly the due rows.
    m.w.clock.set(now().plus_seconds(599));
    let e = m.l().expire(m.now()).unwrap();
    assert_eq!((e.requests, e.challenges), (0, 1));
    assert_eq!(m.status(&id), ApprovalStatus::Pending);
    // A challenge near the request's end is bounded by it.
    let late = m.begin(&alice, &op, "x2").unwrap();
    assert_eq!(late.expires_at, now().plus_seconds(600));
    m.w.clock.set(now().plus_seconds(600));
    assert_eq!(
        m.finish(&op, &late.challenge_id, &ok(&alice), "x2"),
        Err(challenge(ChallengeRefusal::Expired))
    );
    assert_eq!(
        m.begin(&alice, &op, "x3").err(),
        Some(ap(ApprovalCode::RequestExpired))
    );
    let e = m.l().expire(m.now()).unwrap();
    assert_eq!((e.requests, e.challenges), (1, 1));
    assert_eq!(m.status(&id), ApprovalStatus::Expired);
    assert_eq!(m.l().expire(m.now()).unwrap().requests, 0, "terminal");
    assert_eq!(
        m.begin(
            &alice,
            &CeremonyOp::Deny {
                request_id: id.clone()
            },
            "x4"
        )
        .err(),
        Some(ap(ApprovalCode::RequestExpired))
    );
    // Asking again never replaces the expired request.
    assert_eq!(
        m.exec(&action("k2", 1), None).result,
        Err(Refusal::ApprovalRequired)
    );
    assert_eq!(m.status(&id), ApprovalStatus::Expired);
    assert_eq!(m.count("SELECT count(*) FROM grants"), 0);
}

// ---------------------------------------------------------------------------
// Separation and eligibility (O-7, O-8, C2).

#[test]
fn approval_by_the_requester_agent_unenrolled_service_or_unscoped_subject_is_refused() {
    let m = M4::new("separation");
    let id = m.ask("k1", 1);
    let op = CeremonyOp::Approve {
        request_id: id.clone(),
    };
    // The requester's identity under the human realm (fail-closed text match).
    m.enroll(person(REQUESTER, &[(Permission::Approver, T, P)], vec![]))
        .unwrap();
    assert_eq!(
        m.ceremony(&human(REQUESTER), &op, "s1"),
        Err(ap(ApprovalCode::SeparationViolated))
    );
    // A human whose enrollment links the requester (R1).
    m.enroll(person(
        "bob",
        &[(Permission::Approver, T, P)],
        vec![requester()],
    ))
    .unwrap();
    assert_eq!(
        m.ceremony(&human("bob"), &op, "s2"),
        Err(ap(ApprovalCode::SeparationViolated))
    );
    // The agent of the request.
    let with_agent = set(&action("k2", 2), &["agent_id"], json::s("agent:helper"));
    assert_eq!(
        m.exec(&with_agent, None).result,
        Err(Refusal::ApprovalRequired)
    );
    let agent_req = m
        .text(
            "SELECT request_id FROM approval_requests WHERE instr(context, ?1) > 0",
            &digest_text(2),
        )
        .unwrap();
    m.enroll(person(
        "agent:helper",
        &[(Permission::Approver, T, P)],
        vec![],
    ))
    .unwrap();
    assert_eq!(
        m.ceremony(
            &human("agent:helper"),
            &CeremonyOp::Approve {
                request_id: agent_req
            },
            "s3"
        ),
        Err(ap(ApprovalCode::SeparationViolated))
    );
    // A subject with credentials and a role but no enrollment record (written behind the
    // ceremony's back) is refused by separation.
    m.l()
        .db()
        .conn
        .execute_batch(
            "INSERT INTO subjects (issuer, subject, kind, created_at) VALUES ('writ-local', 'uma', 'human', '2026-11-05T12:00:00Z');
             INSERT INTO credentials (credential_id, issuer, subject, public_material, status, enrolled_at, enrolled_by_issuer, enrolled_by)
               VALUES ('cred-uma', 'writ-local', 'uma', x'01', 'active', '2026-11-05T12:00:00Z', 'writ-local', 'root');
             INSERT INTO role_assignments (assignment_id, issuer, subject, kind, permission, tenant_id, project_id, granted_at)
               VALUES ('role-uma', 'writ-local', 'uma', 'human', 'approver', 'fixture-tenant', 'fixture-project', '2026-11-05T12:00:00Z');",
        )
        .unwrap();
    assert_eq!(
        m.ceremony(&human("uma"), &op, "s4"),
        Err(ap(ApprovalCode::SeparationViolated))
    );
    // A service never approves (O-8), and holds no human role.
    let service = NewEnrollment {
        subject: SubjectId::new("writ-local", "svc-ci"),
        kind: PrincipalKind::Service,
        credential: None,
        linked_identities: vec![],
        roles: vec![NewRole {
            permission: Permission::Operator,
            tenant_id: T.into(),
            project_id: P.into(),
        }],
    };
    m.enroll(service).unwrap();
    assert_eq!(
        m.begin(&SubjectId::new("writ-local", "svc-ci"), &op, "s5")
            .err(),
        Some(ap(ApprovalCode::ServiceNotPermitted))
    );
    // No approver role, or one in another project.
    assert_eq!(
        m.begin(&human("carol"), &op, "s6").err(),
        Some(ap(ApprovalCode::PermissionMissing))
    );
    m.enroll(person(
        "erin",
        &[(Permission::Approver, T, "other-project")],
        vec![],
    ))
    .unwrap();
    assert_eq!(
        m.begin(&human("erin"), &op, "s7").err(),
        Some(ap(ApprovalCode::PermissionMissing))
    );
    // The bootstrap administrator holds no approver role.
    assert_eq!(
        m.begin(&root(), &op, "s8").err(),
        Some(ap(ApprovalCode::PermissionMissing))
    );
    // Revoked between challenge and decision: the deciding transaction reads it (C2).
    let c = m.begin(&human("alice"), &op, "s9").unwrap();
    m.revoke(&root(), RevocationTarget::Subject(human("alice")), "r1")
        .unwrap();
    assert_eq!(
        m.finish(&op, &c.challenge_id, &ok(&human("alice")), "s9"),
        Err(ap(ApprovalCode::SubjectRevoked))
    );
    assert_eq!(m.status(&id), ApprovalStatus::Pending);
    assert_eq!(m.count("SELECT count(*) FROM grants"), 0);
}

#[test]
fn enrollment_happens_only_through_the_documented_ceremony() {
    let m = M4::new("enroll");
    let unsupported = Some(ap(ApprovalCode::EnrollmentUnsupported));
    let enroll = enroll_op;
    // Existing subject (no recovery, no additional credentials): no registration at all.
    assert_eq!(
        m.register(&human("alice"), "ok|cred-alice-2|pk").err(),
        unsupported
    );
    // A human without a credential; a service with a human-only role.
    assert_eq!(
        m.begin(&root(), &enroll(person("frank", &[], vec![])), "e2")
            .err(),
        unsupported
    );
    let mut svc = person("svc-x", &[(Permission::Approver, T, P)], vec![]);
    svc.kind = PrincipalKind::Service;
    assert_eq!(m.begin(&root(), &enroll(svc), "e3").err(), unsupported);
    // A service with a credential.
    let mut svc = m.registered(person("svc-y", &[(Permission::Operator, T, P)], vec![]));
    svc.kind = PrincipalKind::Service;
    assert_eq!(m.begin(&root(), &enroll(svc), "e3b").err(), unsupported);
    // A credential id already stored never registers (and the row is burnt).
    assert_eq!(
        m.register(&human("gina"), "ok|cred-alice|pk-gina").err(),
        unsupported
    );
    // Only an operator in the global scope enrolls; nobody enrolls itself.
    assert_eq!(
        m.begin(
            &human("alice"),
            &enroll(m.registered(person("hank", &[], vec![]))),
            "e5"
        )
        .err(),
        Some(ap(ApprovalCode::PermissionMissing))
    );
    let mut me = person("root", &[(Permission::Approver, T, P)], vec![]);
    me.subject = SubjectId::new("other-realm", "ROOT");
    // D-043 Y2: an operator never begins a registration row for itself.
    assert_eq!(
        m.register(&me.subject, "ok|cred-me|pk-me").err(),
        Some(ap(ApprovalCode::SeparationViolated))
    );
    // A linked identity equal to the operator is refused at the ceremony.
    let mut me = m.registered(person(
        "root-alias",
        &[(Permission::Approver, T, P)],
        vec![],
    ));
    me.linked_identities = vec![SubjectId::new("other-realm", "ROOT")];
    assert_eq!(
        m.ceremony(&root(), &enroll(me), "e6"),
        Err(ap(ApprovalCode::SeparationViolated))
    );
    // Enrollment escalation: a changed enrollment cannot reuse the displayed one's challenge.
    let ivy = m.registered(person("ivy", &[(Permission::Approver, T, P)], vec![]));
    let shown = enroll(ivy.clone());
    let mut more = ivy.clone();
    more.roles = vec![NewRole {
        permission: Permission::Operator,
        tenant_id: "*".into(),
        project_id: "*".into(),
    }];
    let mut linked = ivy.clone();
    linked.linked_identities = vec![SubjectId::new("adapter", "someone")];
    for (i, escalated) in [more, linked].into_iter().enumerate() {
        let key = format!("e7-{i}");
        let c = m.begin(&root(), &shown, &key).unwrap();
        assert_eq!(
            m.finish(&enroll(escalated), &c.challenge_id, &ok(&root()), &key),
            Err(challenge(ChallengeRefusal::ContextMismatch))
        );
        // X4: the refusal burned that challenge.
        assert_eq!(
            m.finish(&shown, &c.challenge_id, &ok(&root()), &key),
            Err(challenge(ChallengeRefusal::Consumed))
        );
    }
    let c = m.begin(&root(), &shown, "e7").unwrap();
    let out = m
        .finish(&shown, &c.challenge_id, &ok(&root()), "e7")
        .unwrap();
    assert_eq!(
        out.result,
        CeremonyResult::Enrolled {
            subject: human("ivy")
        }
    );
    assert_eq!(
        m.count("SELECT count(*) FROM role_assignments WHERE subject = 'ivy' AND permission = 'approver'"),
        1
    );
    assert_eq!(
        m.count("SELECT count(*) FROM enrollments WHERE subject = 'ivy' AND enrolled_by = 'root' AND challenge_id IS NOT NULL"),
        1
    );
    assert_eq!(
        m.count("SELECT count(*) FROM enrollments WHERE enrolled_by_issuer = 'writ-local' AND enrolled_by = 'bootstrap'"),
        1
    );
    // The credential is the registration row's, which is consumed (single use).
    assert_eq!(
        m.count("SELECT count(*) FROM credentials c JOIN registrations r ON r.credential_id = c.credential_id WHERE c.subject = 'ivy' AND c.public_material = r.public_material AND r.status = 'enrolled' AND r.consumed_by IS NOT NULL"),
        1
    );
    assert_eq!(
        m.count("SELECT count(*) FROM registrations WHERE status = 'enrolled' AND operation = 'bootstrap' AND consumed_by = 'bootstrap'"),
        1
    );
}

#[test]
fn enrollment_refuses_raw_or_foreign_public_material() {
    let m = M4::new("enroll-raw");
    let unsupported = Some(ap(ApprovalCode::EnrollmentUnsupported));
    let enroll = enroll_op;
    let binding = |id: &str, subject: &SubjectId, operation| RegistrationBinding {
        registration_id: id.into(),
        operation,
        subject: subject.clone(),
        relying_party: m.v.relying_party(),
    };
    let with = |name: &str, reg: VerifiedRegistration| {
        let mut e = person(name, &[(Permission::Approver, T, P)], vec![]);
        e.credential = Some(reg);
        enroll(e)
    };
    let enroll_op = writ_broker::human::RegistrationOperation::Enroll;
    // Raw public material: a registration result that no ledger row recorded.
    let raw = forged_registration(
        &binding("reg-made-up", &human("kim"), enroll_op),
        "cred-kim",
        b"pk-kim".to_vec(),
        ES256,
        true,
    );
    assert_eq!(m.begin(&root(), &with("kim", raw), "r1").err(), unsupported);
    // A real row, but other material, another credential id, algorithm or UV flag.
    let real = m.register(&human("kim"), "ok|cred-kim|pk-kim").unwrap();
    let b = real.binding().clone();
    for (cred, material, alg, uv) in [
        ("cred-kim", b"pk-other".to_vec(), ES256, true),
        ("cred-kim-2", b"pk-kim".to_vec(), ES256, true),
        ("cred-kim", b"pk-kim".to_vec(), "RS256", true),
        ("cred-kim", b"pk-kim".to_vec(), ES256, false),
    ] {
        let forged = forged_registration(&b, cred, material, alg, uv);
        assert_eq!(
            m.begin(&root(), &with("kim", forged), "r2").err(),
            unsupported
        );
    }
    // The real row for another subject, or claimed as a bootstrap registration.
    assert_eq!(
        m.begin(&root(), &with("lea", real.clone()), "r3").err(),
        unsupported
    );
    let as_bootstrap = forged_registration(
        &RegistrationBinding {
            operation: writ_broker::human::RegistrationOperation::Bootstrap,
            ..b.clone()
        },
        "cred-kim",
        b"pk-kim".to_vec(),
        ES256,
        true,
    );
    assert_eq!(
        m.begin(&root(), &with("kim", as_bootstrap), "r4").err(),
        unsupported
    );
    // The real one enrolls once; its row is then used up.
    assert!(m
        .ceremony(&root(), &with("kim", real.clone()), "r5")
        .is_ok());
    let mut again = person("kim", &[], vec![]);
    again.subject = human("kim2");
    again.credential = Some(real);
    assert_eq!(m.begin(&root(), &enroll(again), "r6").err(), unsupported);
    // An expired registration never enrolls.
    let late = m.register(&human("max"), "ok|cred-max|pk-max").unwrap();
    let op = with("max", late);
    m.w.clock.set(now().plus_seconds(590));
    let c = m.begin(&root(), &op, "r7").unwrap();
    m.w.clock.set(now().plus_seconds(600));
    assert_eq!(
        m.finish(&op, &c.challenge_id, &ok(&root()), "r7"),
        Err(ap(ApprovalCode::EnrollmentUnsupported))
    );
    assert_eq!(
        m.count("SELECT count(*) FROM subjects WHERE subject IN ('lea', 'kim2', 'max')"),
        0
    );
}

#[test]
fn registration_is_verified_bound_and_single_use() {
    let mut m = M4::new("registration");
    let assertion = |e| Err(ap(ApprovalCode::AssertionInvalid(e)));
    let unsupported = Err(ap(ApprovalCode::EnrollmentUnsupported));
    // Verifier refusals and unaccepted answers burn the row.
    for (name, response, expected) in [
        (
            "n1",
            "forged|cred-n1|pk",
            assertion(VerifierError::SignatureInvalid),
        ),
        (
            "n2",
            "nouv|cred-n2|pk",
            assertion(VerifierError::UserNotVerified),
        ),
        ("n3", "other|cred-n3|pk", assertion(VerifierError::Rejected)),
        ("n4", "rs256|cred-n4|pk", unsupported.clone()),
        ("n5", "garbage", assertion(VerifierError::Malformed)),
    ] {
        let issued =
            start_registration(m.l(), &|| m.now(), &human(name), &root(), TEST_SESSION).unwrap();
        let finish = |response: &str| {
            m.l()
                .finish_registration(&RegistrationFinish {
                    registration_id: &issued.registration_id,
                    response: response.as_bytes(),
                    operator: Some(&operator_session(&root())),
                    now: m.now(),
                })
                .unwrap()
        };
        assert_eq!(finish(response), expected, "{name}");
        // Burnt: even a valid answer is refused afterwards.
        let valid = format!("ok|cred-{name}|pk");
        assert_eq!(
            finish(&valid),
            Err(challenge(ChallengeRefusal::Consumed)),
            "{name}"
        );
    }
    assert_eq!(
        m.count("SELECT count(*) FROM registrations WHERE status = 'refused' AND length(ceremony_state) = 0 AND credential_id IS NULL"),
        5
    );
    // L6: a verifier that reports itself unavailable does not burn the row; it stays
    // issued and a later valid answer registers.
    let issued =
        start_registration(m.l(), &|| m.now(), &human("d1"), &root(), TEST_SESSION).unwrap();
    let finish = |response: &str| {
        m.l()
            .finish_registration(&RegistrationFinish {
                registration_id: &issued.registration_id,
                response: response.as_bytes(),
                operator: Some(&operator_session(&root())),
                now: m.now(),
            })
            .unwrap()
    };
    assert_eq!(
        finish("down|cred-d1|pk"),
        assertion(VerifierError::Unavailable)
    );
    assert_eq!(
        m.text(
            "SELECT status FROM registrations WHERE registration_id = ?1",
            &issued.registration_id
        )
        .as_deref(),
        Some("issued")
    );
    assert!(finish("ok|cred-d1|pk").is_ok());
    // M1: a row issued under one approval channel cannot be finished by a ledger
    // configured with another relying party (origin); the row is burnt.
    let issued =
        start_registration(m.l(), &|| m.now(), &human("o1"), &root(), TEST_SESSION).unwrap();
    let other = Arc::new(TestVerifier::default());
    other.other_origin.store(true, Ordering::SeqCst);
    let original = m.reopen_with(&other);
    assert_eq!(
        m.finish_registration(&issued.registration_id, "ok|cred-o1|pk"),
        unsupported
    );
    m.reopen_with(&original);
    assert_eq!(
        m.finish_registration(&issued.registration_id, "ok|cred-o1|pk"),
        Err(challenge(ChallengeRefusal::Consumed))
    );
    // A finished row is single use; an unknown row is refused.
    let reg = m.register(&human("p1"), "ok|cred-p1|pk").unwrap();
    assert_eq!(reg.subject(), &human("p1"));
    assert_eq!(reg.binding().relying_party.origin, ORIGIN);
    let again = m
        .l()
        .finish_registration(&RegistrationFinish {
            registration_id: reg.registration_id(),
            response: b"ok|cred-p1b|pk",
            operator: Some(&operator_session(&root())),
            now: m.now(),
        })
        .unwrap();
    assert_eq!(again, Err(challenge(ChallengeRefusal::Consumed)));
    let unknown = m
        .l()
        .finish_registration(&RegistrationFinish {
            registration_id: "reg-unknown",
            response: b"ok|cred-x|pk",
            operator: Some(&operator_session(&root())),
            now: m.now(),
        })
        .unwrap();
    assert_eq!(unknown, Err(challenge(ChallengeRefusal::Unknown)));
    // The registered credential id is taken for every later registration.
    assert_eq!(
        m.register(&human("p2"), "ok|cred-p1|pk").err(),
        unsupported.err()
    );
    // An expired row cannot finish.
    let issued =
        start_registration(m.l(), &|| m.now(), &human("q1"), &root(), TEST_SESSION).unwrap();
    m.w.clock.set(now().plus_seconds(600));
    let expired = m
        .l()
        .finish_registration(&RegistrationFinish {
            registration_id: &issued.registration_id,
            response: b"ok|cred-q1|pk",
            operator: Some(&operator_session(&root())),
            now: m.now(),
        })
        .unwrap();
    assert_eq!(expired, Err(challenge(ChallengeRefusal::Expired)));
    // The serving ledger never issues bootstrap registrations: its rows are all `enroll`
    // except the bootstrap's own.
    assert_eq!(
        m.count("SELECT count(*) FROM registrations WHERE operation = 'bootstrap'"),
        1
    );
}

#[test]
fn bootstrap_is_exclusive_against_service_and_happens_once() {
    let dir = std::env::temp_dir().join(format!(
        "writ-m4-bootstrap-{}-{}",
        std::process::id(),
        now().to_string().replace(':', "")
    ));
    let _ = std::fs::remove_dir_all(&dir);
    std::fs::create_dir_all(&dir).unwrap();
    let path = dir.join("ledger.db");
    let clock = super::grant_tests::TestClock::at(now());
    let v = Arc::new(TestVerifier::default());
    // While the service owns the ledger, no bootstrap session opens (no separate lock, no
    // fencing bypass): the same sidecar ownership lock refuses it.
    let service = SqliteLedger::create(&path)
        .unwrap()
        .with_approval_channel(channel_of(&v))
        .unwrap();
    assert!(matches!(
        BootstrapSession::open(&path),
        Err(StoreError::OwnedElsewhere)
    ));
    // The serving ledger never registers for a bootstrap or before one.
    let early = session_register(
        &service,
        &|| {
            let mut l: &SqliteLedger = &service;
            observe(&clock, &mut l).unwrap()
        },
        &root(),
        "ok|cred-root|pk-root",
    );
    assert_eq!(early, Err(ap(ApprovalCode::EnrollmentUnsupported)));
    drop(service);
    // While a bootstrap session owns it, the service cannot open it.
    let session = match BootstrapSession::open(&path) {
        Ok(Ok(s)) => s.with_approval_channel(channel_of(&v)).unwrap(),
        _ => panic!("bootstrap session"),
    };
    assert!(matches!(
        SqliteLedger::open(&path),
        Err(StoreError::OwnedElsewhere)
    ));
    // A bootstrap enrollment needs this session's registration of the same subject.
    let reg = session_register(
        &session,
        &|| session.observe(&clock).unwrap(),
        &human("someone-else"),
        "ok|cred-else|pk",
    )
    .unwrap();
    let now1 = session.observe(&clock).unwrap();
    let wrong = BootstrapEnrollment {
        subject: root(),
        credential: reg,
        linked_identities: vec![],
    };
    assert_eq!(
        session.bootstrap(&wrong, now1).unwrap(),
        Err(ap(ApprovalCode::EnrollmentUnsupported))
    );
    // (The refused bootstrap consumed the session; ownership is released.)
    assert_eq!(bootstrap_root_at(&path, &clock, &v, &root()), Ok(1));
    // Bootstrap twice: refused at open, whatever happens to the first administrator.
    assert!(matches!(
        BootstrapSession::open(&path),
        Ok(Err(Refusal::Approval(ApprovalCode::EnrollmentUnsupported)))
    ));
    let l = SqliteLedger::open(&path).unwrap();
    let count = |sql: &str| -> i64 { l.db().conn.query_row(sql, [], |r| r.get(0)).unwrap() };
    assert_eq!(count("SELECT count(*) FROM subjects"), 1);
    assert_eq!(
        count("SELECT count(*) FROM role_assignments WHERE permission IN ('operator', 'revoker') AND tenant_id = '*' AND project_id = '*'"),
        2
    );
    assert_eq!(
        count("SELECT count(*) FROM approval_events WHERE kind = 'bootstrap'"),
        1
    );
    drop(l);
    let _ = std::fs::remove_dir_all(&dir);
}

// ---------------------------------------------------------------------------
// M4-05: revocation, serialized with T-reserve, T-dispatch and promotion (O-5).

#[test]
fn a_grant_revoked_before_reservation_is_never_used() {
    let m = M4::new("revoke-before");
    let (_, grant) = m.approved("k1", 1);
    let out = m
        .revoke(
            &human("carol"),
            RevocationTarget::Grant(grant.as_str().into()),
            "r1",
        )
        .unwrap();
    assert_eq!(
        out.result,
        CeremonyResult::Revoked {
            use_state: UseState::Unused
        }
    );
    let e = m.exec(&action("k1", 1), Some(&grant));
    assert_eq!(e.result, Err(ap(ApprovalCode::GrantRevoked)));
    assert_eq!(uses(m.l(), grant.as_str()), Some(0));
    assert_eq!(m.receipt("k1"), None);
    assert_eq!(
        m.count("SELECT count(*) FROM audit_refusals WHERE code = 'GRANT_REVOKED'"),
        1
    );
    // Revoking again never revives or refunds anything.
    assert_eq!(
        m.revoke(
            &human("carol"),
            RevocationTarget::Grant(grant.as_str().into()),
            "r2"
        ),
        Err(ap(ApprovalCode::GrantRevoked))
    );
    assert_eq!(m.count("SELECT count(*) FROM revocations"), 1);
}

#[test]
fn a_grant_revoked_between_reserve_and_dispatch_keeps_the_reservation_and_its_use() {
    let m = M4::new("revoke-reserved");
    let (_, grant) = m.approved("k1", 1);
    let b = m.w.broker();
    assert_eq!(
        b.grants()
            .reserve(&verify(&action("k1", 1)), revision(), Some(&grant)),
        Ok(ReserveOutcome::Reserved)
    );
    let out = m
        .revoke(
            &human("carol"),
            RevocationTarget::Grant(grant.as_str().into()),
            "r1",
        )
        .unwrap();
    assert_eq!(
        out.result,
        CeremonyResult::Revoked {
            use_state: UseState::Reserved
        }
    );
    for _ in 0..2 {
        let e = m.exec(&action("k1", 1), Some(&grant));
        assert_eq!(e.result, Err(ap(ApprovalCode::GrantRevoked)));
        assert_eq!(e.permit, None);
        assert_eq!(m.receipt("k1").as_deref(), Some("reserved"));
        assert_eq!(uses(m.l(), grant.as_str()), Some(1), "no refund");
        assert_eq!(m.count("SELECT count(*) FROM receipt_permits"), 0);
    }
}

#[test]
fn a_revocation_after_dispatch_undoes_nothing_and_is_recorded_as_such() {
    let m = M4::new("revoke-after");
    let (_, grant) = m.approved("k1", 1);
    assert_eq!(
        m.exec(&action("k1", 1), Some(&grant)).result,
        Ok(Executed::EffectCompleted)
    );
    let out = m
        .revoke(
            &human("carol"),
            RevocationTarget::Grant(grant.as_str().into()),
            "r1",
        )
        .unwrap();
    assert_eq!(
        out.result,
        CeremonyResult::Revoked {
            use_state: UseState::Dispatched
        }
    );
    assert_eq!(m.receipt("k1").as_deref(), Some("observed_success"));
    assert_eq!(
        m.exec(&action("k1", 1), Some(&grant)).result,
        Ok(Executed::Replayed(Replay::EffectCompleted)),
        "durable replay is kept"
    );
}

#[test]
fn revoking_the_approver_credential_subject_or_role_invalidates_unused_grants() {
    let m = M4::new("revoke-identity");
    let (_, g1) = m.approved("k1", 1);
    let refused = |g: &GrantLabel, idem: &str, n: u32| {
        assert_eq!(
            m.exec(&action(idem, n), Some(g)).result,
            Err(ap(ApprovalCode::GrantRevoked))
        );
    };
    m.revoke(
        &root(),
        RevocationTarget::Credential("cred-alice".into()),
        "r1",
    )
    .unwrap();
    refused(&g1, "k1", 1);
    // A revoked credential is never offered to the verifier again.
    let id = m.ask("k2", 2);
    assert_eq!(
        m.begin(
            &human("alice"),
            &CeremonyOp::Approve { request_id: id },
            "x"
        )
        .err(),
        Some(ap(ApprovalCode::CredentialRevoked))
    );
    // Role revocation.
    m.enroll(person("dave", &[(Permission::Approver, T, P)], vec![]))
        .unwrap();
    let id3 = m.ask("k3", 3);
    m.ceremony(
        &human("dave"),
        &CeremonyOp::Approve {
            request_id: id3.clone(),
        },
        "a3",
    )
    .unwrap();
    let g3 = approval_grant_label(&id3).unwrap();
    let role = m
        .text(
            "SELECT assignment_id FROM role_assignments WHERE subject = ?1 AND permission = 'approver'",
            "dave",
        )
        .unwrap();
    m.revoke(&root(), RevocationTarget::Role(role), "r2")
        .unwrap();
    refused(&g3, "k3", 3);
    // Subject revocation.
    m.enroll(person("erin", &[(Permission::Approver, T, P)], vec![]))
        .unwrap();
    let id4 = m.ask("k4", 4);
    m.ceremony(
        &human("erin"),
        &CeremonyOp::Approve {
            request_id: id4.clone(),
        },
        "a4",
    )
    .unwrap();
    let g4 = approval_grant_label(&id4).unwrap();
    m.revoke(&root(), RevocationTarget::Subject(human("erin")), "r3")
        .unwrap();
    refused(&g4, "k4", 4);
    assert_eq!(
        m.count("SELECT count(*) FROM revocations WHERE use_state = 'not_applicable'"),
        3
    );
    assert_eq!(
        m.count("SELECT count(*) FROM ledger_sequence WHERE kind = 'revocation'"),
        3
    );
}

#[test]
fn only_a_scoped_revoker_or_the_approver_of_an_unused_grant_revokes_it() {
    let m = M4::new("revoke-who");
    let (_, g1) = m.approved("k1", 1);
    let target = || RevocationTarget::Grant(g1.as_str().into());
    // X5 (S7): the bootstrap administrator's explicitly granted revoker scope (*, *) contains
    // the request's scope, so a challenge is issued; a revoker of another tenant is not.
    assert!(m
        .begin(
            &root(),
            &CeremonyOp::Revoke {
                target: target(),
                reason: "x".into()
            },
            "r0"
        )
        .is_ok());
    m.enroll(person(
        "olga",
        &[(Permission::Revoker, "other-tenant", "*")],
        vec![],
    ))
    .unwrap();
    assert_eq!(
        m.begin(
            &human("olga"),
            &CeremonyOp::Revoke {
                target: target(),
                reason: "x".into()
            },
            "r0b"
        )
        .err(),
        Some(ap(ApprovalCode::PermissionMissing))
    );
    // Another approver without the revoker permission.
    m.enroll(person("dave", &[(Permission::Approver, T, P)], vec![]))
        .unwrap();
    assert_eq!(
        m.begin(
            &human("dave"),
            &CeremonyOp::Revoke {
                target: target(),
                reason: "x".into()
            },
            "r1"
        )
        .err(),
        Some(ap(ApprovalCode::PermissionMissing))
    );
    // The approver of the unused grant.
    let out = m.revoke(&human("alice"), target(), "r2").unwrap();
    assert_eq!(
        out.result,
        CeremonyResult::Revoked {
            use_state: UseState::Unused
        }
    );
    // Not once it is used.
    let (_, g2) = m.approved("k2", 2);
    assert_eq!(
        m.exec(&action("k2", 2), Some(&g2)).result,
        Ok(Executed::EffectCompleted)
    );
    assert_eq!(
        m.begin(
            &human("alice"),
            &CeremonyOp::Revoke {
                target: RevocationTarget::Grant(g2.as_str().into()),
                reason: "x".into()
            },
            "r3"
        )
        .err(),
        Some(ap(ApprovalCode::PermissionMissing))
    );
}

// ---------------------------------------------------------------------------
// Races.

#[test]
fn sixteen_concurrent_decisions_on_one_request_commit_exactly_one() {
    let m = M4::new("race-decide");
    let alice = human("alice");
    let id = m.ask("k1", 1);
    let ops: Vec<CeremonyOp> = (0..16)
        .map(|i| {
            if i % 2 == 0 {
                CeremonyOp::Approve {
                    request_id: id.clone(),
                }
            } else {
                CeremonyOp::Deny {
                    request_id: id.clone(),
                }
            }
        })
        .collect();
    let challenges: Vec<IssuedChallenge> = ops
        .iter()
        .enumerate()
        .map(|(i, op)| m.begin(&alice, op, &format!("key-{i}")).unwrap())
        .collect();
    let barrier = Barrier::new(16);
    let results: Vec<Result<CeremonyOutcome, Refusal>> = thread::scope(|s| {
        let handles: Vec<_> = ops
            .iter()
            .zip(&challenges)
            .enumerate()
            .map(|(i, (op, c))| {
                let (m, barrier, alice) = (&m, &barrier, &alice);
                s.spawn(move || {
                    barrier.wait();
                    m.finish(op, &c.challenge_id, &ok(alice), &format!("key-{i}"))
                })
            })
            .collect();
        handles.into_iter().map(|h| h.join().unwrap()).collect()
    });
    let committed: Vec<_> = results.iter().filter(|r| r.is_ok()).collect();
    assert_eq!(committed.len(), 1, "{results:?}");
    assert!(results
        .iter()
        .filter(|r| r.is_err())
        .all(|r| r == &Err(ap(ApprovalCode::RequestClosed))));
    assert_eq!(m.count("SELECT count(*) FROM approval_decisions"), 1);
    assert_eq!(
        m.count("SELECT count(*) FROM challenges WHERE status = 'consumed' AND operation IN ('approve', 'deny')"),
        1
    );
    let approved =
        matches!(committed[0], Ok(o) if matches!(o.result, CeremonyResult::Approved { .. }));
    assert_eq!(m.count("SELECT count(*) FROM grants"), i64::from(approved));
    assert_eq!(
        m.count("SELECT count(*) FROM ledger_sequence WHERE kind = 'decision'"),
        1
    );
}

#[test]
fn sixteen_concurrent_finishes_of_one_challenge_decide_once_and_replay() {
    let m = M4::new("race-replay");
    let alice = human("alice");
    let id = m.ask("k1", 1);
    let op = CeremonyOp::Approve {
        request_id: id.clone(),
    };
    let c = m.begin(&alice, &op, "key").unwrap();
    let barrier = Barrier::new(16);
    let results: Vec<CeremonyOutcome> = thread::scope(|s| {
        let handles: Vec<_> = (0..16)
            .map(|_| {
                let (m, barrier, alice, op, c) = (&m, &barrier, &alice, &op, &c);
                s.spawn(move || {
                    barrier.wait();
                    m.finish(op, &c.challenge_id, &ok(alice), "key").unwrap()
                })
            })
            .collect();
        handles.into_iter().map(|h| h.join().unwrap()).collect()
    });
    assert_eq!(results.iter().filter(|o| !o.replayed).count(), 1);
    assert!(results
        .windows(2)
        .all(|w| w[0].result == w[1].result && w[0].ledger_seq == w[1].ledger_seq));
    assert_eq!(m.count("SELECT count(*) FROM grants"), 1);
    assert_eq!(m.count("SELECT count(*) FROM approval_decisions"), 1);
}

#[test]
fn revocation_racing_execution_is_serialized_with_the_recorded_order() {
    let m = M4::new("race-revoke");
    let carol = human("carol");
    let mut seen = std::collections::BTreeSet::new();
    for n in 1..=12u32 {
        let idem = format!("k{n}");
        let (_, grant) = m.approved(&idem, n);
        let op = CeremonyOp::Revoke {
            target: RevocationTarget::Grant(grant.as_str().into()),
            reason: "race".into(),
        };
        let c = m.begin(&carol, &op, &format!("rk{n}")).unwrap();
        let barrier = Barrier::new(2);
        let (executed, revoked) = thread::scope(|s| {
            let e = s.spawn(|| {
                barrier.wait();
                m.exec(&action(&idem, n), Some(&grant)).result
            });
            let r = s.spawn(|| {
                barrier.wait();
                m.finish(&op, &c.challenge_id, &ok(&carol), &format!("rk{n}"))
            });
            (e.join().unwrap(), r.join().unwrap())
        });
        let CeremonyResult::Revoked { use_state } = revoked.unwrap().result else {
            panic!("not revoked");
        };
        seen.insert(use_state.as_str());
        let grant_uses = uses(m.l(), grant.as_str());
        match use_state {
            UseState::Unused => {
                assert_eq!(executed, Err(ap(ApprovalCode::GrantRevoked)));
                assert_eq!((grant_uses, m.receipt(&idem)), (Some(0), None));
            }
            UseState::Reserved => {
                assert_eq!(executed, Err(ap(ApprovalCode::GrantRevoked)));
                assert_eq!(
                    (grant_uses, m.receipt(&idem).as_deref()),
                    (Some(1), Some("reserved"))
                );
            }
            UseState::Dispatched => {
                assert_eq!(executed, Ok(Executed::EffectCompleted));
                assert_eq!(grant_uses, Some(1));
            }
            other => panic!("{other:?}"),
        }
        // At most one dispatch per key, whatever the order.
        assert!(m.count("SELECT count(*) FROM receipt_permits") <= i64::from(n));
    }
    assert!(!seen.is_empty());
}

#[test]
fn sixteen_concurrent_revocations_of_one_grant_commit_exactly_one() {
    let m = M4::new("race-revocations");
    let carol = human("carol");
    let (_, grant) = m.approved("k1", 1);
    let op = CeremonyOp::Revoke {
        target: RevocationTarget::Grant(grant.as_str().into()),
        reason: "race".into(),
    };
    let challenges: Vec<IssuedChallenge> = (0..16)
        .map(|i| m.begin(&carol, &op, &format!("rk{i}")).unwrap())
        .collect();
    let barrier = Barrier::new(16);
    let results: Vec<_> = thread::scope(|s| {
        let handles: Vec<_> = challenges
            .iter()
            .enumerate()
            .map(|(i, c)| {
                let (m, barrier, carol, op) = (&m, &barrier, &carol, &op);
                s.spawn(move || {
                    barrier.wait();
                    m.finish(op, &c.challenge_id, &ok(carol), &format!("rk{i}"))
                })
            })
            .collect();
        handles.into_iter().map(|h| h.join().unwrap()).collect()
    });
    assert_eq!(results.iter().filter(|r| r.is_ok()).count(), 1);
    assert!(results
        .iter()
        .filter(|r| r.is_err())
        .all(|r| r == &Err(ap(ApprovalCode::GrantRevoked))));
    assert_eq!(m.count("SELECT count(*) FROM revocations"), 1);
}

// ---------------------------------------------------------------------------
// M4-10: unavailable storage and unknown commits.

#[test]
fn an_unknown_commit_is_recovered_by_an_identical_retry_without_a_second_grant() {
    let m = M4::new("unknown-commit");
    let alice = human("alice");
    let id = m.ask("k1", 1);
    let op = CeremonyOp::Approve {
        request_id: id.clone(),
    };
    let c = m.begin(&alice, &op, "key").unwrap();
    let finish = || {
        m.l().finish_ceremony(&CeremonyFinish {
            op: &op,
            challenge_id: &c.challenge_id,
            response: ok(&alice).as_bytes(),
            idempotency_key: "key",
            now: m.now(),
        })
    };
    m.l().inject(
        "ceremony",
        fault::Stage::Commit,
        rusqlite::ffi::SQLITE_IOERR,
    );
    let failed = finish().unwrap_err();
    assert_eq!(failed.stage, TxStage::Commit);
    assert!(failed.outcome_unknown);
    // The retry recovers from durable state: here the commit did not persist, so it decides.
    let first = finish().unwrap().unwrap();
    assert!(!first.replayed);
    // Had it persisted, the identical retry replays it: never a second grant or decision.
    let again = finish().unwrap().unwrap();
    assert!(again.replayed);
    assert_eq!(again.result, first.result);
    assert_eq!(m.count("SELECT count(*) FROM grants"), 1);
    assert_eq!(m.count("SELECT count(*) FROM approval_decisions"), 1);
    // The same for a revocation and an enrollment.
    let grant = approval_grant_label(&id).unwrap();
    let revoke = CeremonyOp::Revoke {
        target: RevocationTarget::Grant(grant.as_str().into()),
        reason: "x".into(),
    };
    let rc = m.begin(&human("carol"), &revoke, "rkey").unwrap();
    m.l().inject(
        "ceremony",
        fault::Stage::Commit,
        rusqlite::ffi::SQLITE_IOERR,
    );
    assert!(
        m.l()
            .finish_ceremony(&CeremonyFinish {
                op: &revoke,
                challenge_id: &rc.challenge_id,
                response: ok(&human("carol")).as_bytes(),
                idempotency_key: "rkey",
                now: m.now(),
            })
            .unwrap_err()
            .outcome_unknown
    );
    let r1 = m
        .finish(&revoke, &rc.challenge_id, &ok(&human("carol")), "rkey")
        .unwrap();
    let r2 = m
        .finish(&revoke, &rc.challenge_id, &ok(&human("carol")), "rkey")
        .unwrap();
    assert!(!r1.replayed && r2.replayed);
    assert_eq!(m.count("SELECT count(*) FROM revocations"), 1);
    let enroll = enroll_op(m.registered(person("zed", &[(Permission::Approver, T, P)], vec![])));
    let ec = m.begin(&root(), &enroll, "ekey").unwrap();
    let e1 = m
        .finish(&enroll, &ec.challenge_id, &ok(&root()), "ekey")
        .unwrap();
    let e2 = m
        .finish(&enroll, &ec.challenge_id, &ok(&root()), "ekey")
        .unwrap();
    assert!(!e1.replayed && e2.replayed);
    assert_eq!(
        m.count("SELECT count(*) FROM subjects WHERE subject = 'zed'"),
        1
    );
}

#[test]
fn unavailable_storage_fails_closed_without_partial_state() {
    let m = M4::new("unavailable");
    let alice = human("alice");
    // Recording the request fails: the agent's answer is unchanged; nothing is stored.
    m.l().inject(
        "approval_request",
        fault::Stage::Begin,
        rusqlite::ffi::SQLITE_BUSY,
    );
    assert_eq!(
        m.exec(&action("k1", 1), None).result,
        Err(Refusal::ApprovalRequired)
    );
    assert_eq!(m.count("SELECT count(*) FROM approval_requests"), 0);
    let id = m.ask("k2", 1);
    let op = CeremonyOp::Approve {
        request_id: id.clone(),
    };
    for (name, stage) in [
        ("challenge_issue", fault::Stage::Begin),
        ("challenge_issue", fault::Stage::Body),
    ] {
        m.l().inject(name, stage, rusqlite::ffi::SQLITE_IOERR);
        let begin = CeremonyBegin {
            op: &op,
            subject: &alice,
            idempotency_key: "key",
            now: m.now(),
        };
        let f = m.l().begin_ceremony(&begin).unwrap_err();
        assert!(!f.outcome_unknown);
        assert_eq!(f.error.code(), "SQLITE_IOERR");
    }
    assert_eq!(
        m.count("SELECT count(*) FROM challenges WHERE operation = 'approve'"),
        0
    );
    let c = m.begin(&alice, &op, "key").unwrap();
    for stage in [fault::Stage::Begin, fault::Stage::Body] {
        m.l().inject("ceremony", stage, rusqlite::ffi::SQLITE_IOERR);
        let f = m
            .l()
            .finish_ceremony(&CeremonyFinish {
                op: &op,
                challenge_id: &c.challenge_id,
                response: ok(&alice).as_bytes(),
                idempotency_key: "key",
                now: m.now(),
            })
            .unwrap_err();
        assert!(!f.outcome_unknown);
        assert_eq!(m.status(&id), ApprovalStatus::Pending);
        assert_eq!(
            m.count(
                "SELECT count(*) FROM challenges WHERE status = 'issued' AND operation = 'approve'"
            ),
            1
        );
        assert_eq!(m.count("SELECT count(*) FROM grants"), 0);
    }
    assert!(m.finish(&op, &c.challenge_id, &ok(&alice), "key").is_ok());
    // A ledger without approval storage records nothing and says so.
    struct Bare;
    impl writ_broker::store::GrantLedger for Bare {
        fn insert_grant(
            &self,
            _: &writ_broker::grants::NewGrant,
        ) -> Result<writ_broker::grants::GrantInsert, StoreError> {
            unreachable!()
        }
        fn reserve(
            &self,
            _: &writ_broker::grants::ReserveRequest<'_>,
        ) -> Result<Result<ReserveOutcome, Refusal>, writ_broker::store::TxFailure> {
            unreachable!()
        }
    }
    impl ExecuteLedger for Bare {
        fn dispatch(
            &self,
            _: &writ_broker::execute::DispatchIntent<'_>,
        ) -> Result<Result<writ_broker::execute::Dispatched, Refusal>, writ_broker::store::TxFailure>
        {
            unreachable!()
        }
        fn observe_effect(
            &self,
            _: &writ_broker::execute::Observation<'_>,
        ) -> Result<(), writ_broker::store::TxFailure> {
            unreachable!()
        }
        fn pending_receipts(
            &self,
            _: &writ_broker::policy::ExclusiveHold<'_>,
        ) -> Result<Vec<writ_broker::execute::PendingReceipt>, writ_broker::store::TxFailure>
        {
            unreachable!()
        }
        fn reconcile(
            &self,
            _: &writ_broker::policy::ExclusiveHold<'_>,
            _: &[writ_broker::execute::Resolution],
        ) -> Result<writ_broker::execute::Reconciled, writ_broker::store::TxFailure> {
            unreachable!()
        }
        fn record_failure(
            &self,
            _: &str,
            _: &writ_broker::execute::FailureRecord,
        ) -> Result<(), StoreError> {
            unreachable!()
        }
    }
    let (request, _) = m.l().approval_request(&id).unwrap().unwrap();
    let f = Bare.record_approval_request(&request).unwrap_err();
    assert_eq!(f.error.code(), "unsupported");
}

// ---------------------------------------------------------------------------
// M4-10 / O-14: migration from writ-ledger-5.

#[test]
fn the_upgrade_from_writ_ledger_5_marks_every_pre_m4_grant_and_keeps_all_history() {
    let w = World::new("m4-upgrade-v5");
    let path = w.dir.join("ledger.db");
    let b = w.broker();
    // Grants in every receipt state. writ-ledger-6 mints grants only in its ceremony (D-042
    // X1), so they are issued there (distinct actions, one approval request each) and then
    // rewritten below, behind the ledger's back, into a writ-ledger-5 ledger whose grants
    // carry M3 `writ-provenance-1` rows.
    let grants = [
        ("g-unused", "k1", 1),
        ("g-reserved", "k2", 2),
        ("g-done", "k3", 3),
        ("g-dispatched", "k4", 4),
    ];
    for (g, idem, n) in grants {
        approve(b.grants(), &action(idem, n), g).unwrap();
    }
    assert_eq!(
        b.grants().reserve(
            &verify(&action("k2", 2)),
            revision(),
            Some(&label("g-reserved"))
        ),
        Ok(ReserveOutcome::Reserved)
    );
    assert_eq!(
        b.execute(
            &verify(&action("k3", 3)),
            revision(),
            Some(&label("g-done"))
        )
        .result,
        Ok(Executed::EffectCompleted)
    );
    w.ledger
        .inject("observe", fault::Stage::Begin, rusqlite::ffi::SQLITE_IOERR);
    assert!(b
        .execute(
            &verify(&action("k4", 4)),
            revision(),
            Some(&label("g-dispatched"))
        )
        .result
        .is_err());
    drop(b);
    w.ledger
        .db()
        .conn
        .execute(
            "UPDATE grant_provenance SET version = 'writ-provenance-1', \
             auth_evidence_ref = 'authn-event:pre-m4'",
            [],
        )
        .unwrap();
    let tables = [
        "grants",
        "grant_provenance",
        "grant_uses",
        "receipts",
        "receipt_events",
        "receipt_permits",
        "audit_refusals",
        "time_floor",
        "clock_events",
    ];
    let counts = |l: &SqliteLedger| -> Vec<i64> {
        tables
            .iter()
            .map(|t| {
                l.db()
                    .conn
                    .query_row(&format!("SELECT count(*) FROM {t}"), [], |r| r.get(0))
                    .unwrap()
            })
            .collect()
    };
    let before = counts(&w.ledger);
    let World {
        ledger,
        effects,
        active,
        clock,
        key,
        dir,
    } = w;
    let id = ledger.ledger_id().clone();
    drop(effects);
    drop(ledger);
    // Turn it into an exact writ-ledger-5 ledger (drop the writ-ledger-6 tables).
    let c = rusqlite::Connection::open(&path).unwrap();
    for t in [
        "key_revocations",
        "key_registry",
        "approval_events",
        "consumed_signoffs",
        "signoff_requests",
        "signoffs",
        "revocations",
        "legacy_grants",
        "approval_grants",
        "approval_decisions",
        "challenges",
        "approval_requests",
        "role_assignments",
        "registrations",
        "enrollment_links",
        "enrollments",
        "credentials",
        "subjects",
        "ledger_sequence",
    ] {
        c.execute_batch(&format!("DROP TABLE {t};")).unwrap();
    }
    c.execute(
        "UPDATE meta SET value = ?1 WHERE key = 'schema_version'",
        [SCHEMA_VERSION_V5],
    )
    .unwrap();
    drop(c);
    let objects = |p: &std::path::Path| -> i64 {
        rusqlite::Connection::open(p)
            .unwrap()
            .query_row(
                "SELECT count(*) FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'",
                [],
                |r| r.get(0),
            )
            .unwrap()
    };
    let expected_v5 = {
        let mem = rusqlite::Connection::open_in_memory().unwrap();
        mem.execute_batch(SCHEMA_V5).unwrap();
        mem.query_row(
            "SELECT count(*) FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'",
            [],
            |r| r.get::<_, i64>(0),
        )
        .unwrap()
    };
    assert_eq!(objects(&path), expected_v5);
    // open never upgrades; upgrade is explicit and keeps every row and the identity.
    let raw = std::fs::read(&path).unwrap();
    assert!(SqliteLedger::open(&path).is_err());
    assert_eq!(std::fs::read(&path).unwrap(), raw, "refused unchanged");
    let ledger = SqliteLedger::upgrade(&path).unwrap();
    assert_eq!(ledger.ledger_id(), &id);
    assert_eq!(counts(&ledger), before, "no row rewritten or deleted");
    let mut marked: Vec<String> = grants
        .iter()
        .map(|(g, _, _)| format!("{}/writ-ledger-5", label(g).as_str()))
        .collect();
    marked.sort();
    assert_eq!(
        ledger
            .db()
            .conn
            .query_row(
                "SELECT group_concat(label || '/' || upgraded_from, ',') FROM (SELECT * FROM legacy_grants ORDER BY label)",
                [],
                |r| r.get::<_, String>(0)
            )
            .unwrap(),
        marked.join(",")
    );
    let provenance_v1: i64 = ledger
        .db()
        .conn
        .query_row(
            "SELECT count(*) FROM grant_provenance WHERE version = 'writ-provenance-1'",
            [],
            |r| r.get(0),
        )
        .unwrap();
    assert_eq!(provenance_v1, 4, "original provenance kept");
    let effects =
        super::SqliteEffectStore::open(&dir.join("effects.db"), ledger.ledger_id()).unwrap();
    let b = writ_broker::execute::Broker::new(&active, &clock, &ledger, &effects, &key);
    let unavailable = |category: &str| {
        Refusal::LedgerUnavailable(StoreError::Unavailable {
            category: category.into(),
            code: "provenance".into(),
        })
    };
    let run = |idem: &str, g: &str| {
        let n = grants.iter().find(|x| x.1 == idem).unwrap().2;
        b.execute(&verify(&action(idem, n)), revision(), Some(&label(g)))
            .result
    };
    // Unused: never consumed.
    assert_eq!(
        run("k1", "g-unused"),
        Err(unavailable("transaction reserve"))
    );
    assert_eq!(uses(&ledger, "g-unused"), Some(0));
    // Reserved: never dispatched, reservation and use kept.
    assert_eq!(
        run("k2", "g-reserved"),
        Err(unavailable("transaction dispatch"))
    );
    assert_eq!(uses(&ledger, "g-reserved"), Some(1));
    // Completed: durable replay kept.
    assert_eq!(
        run("k3", "g-done"),
        Ok(Executed::Replayed(Replay::EffectCompleted))
    );
    // Dispatched: reconciliation kept, never redispatched.
    assert_eq!(
        run("k4", "g-dispatched"),
        Err(Refusal::ReconciliationRequired)
    );
    assert_eq!(b.reconcile().map(|r| r.changed()), Ok(1));
    let status = |idem: &str| -> String {
        ledger
            .db()
            .conn
            .query_row(
                "SELECT status FROM receipts WHERE idempotency_key = ?1",
                [idem],
                |r| r.get(0),
            )
            .unwrap()
    };
    assert_eq!(status("k2"), "reserved");
    assert_eq!(status("k4"), "observed_success");
    drop(b);
    drop(effects);
    drop(ledger);
    assert!(SqliteLedger::upgrade(&path).is_err(), "one-way");
    assert!(SqliteLedger::open(&path).is_ok());
}

// ---------------------------------------------------------------------------
// D-043 Y2: registration rows bound to the initiating operator session.

/// A registration row for `subject` begun in `session` (no finish).
fn begin_row(
    m: &M4,
    subject: &SubjectId,
    session: Option<&OperatorSession>,
) -> Result<String, Refusal> {
    match session {
        // D-043 Y2: the only way the serving ledger creates a row.
        Some(o) => start_registration(m.l(), &|| m.now(), subject, &o.operator, &o.session_id)
            .map(|i| i.registration_id),
        // Without a start ceremony (refused).
        None => m
            .l()
            .begin_registration(&RegistrationBegin {
                subject,
                operator: None,
                now: m.now(),
            })
            .expect("store")
            .map(|i| i.registration_id),
    }
}

/// Finish row `id` for `subject` (credential `cred-<subject>`) in `session`.
fn finish_row(
    m: &M4,
    id: &str,
    subject: &SubjectId,
    session: Option<&OperatorSession>,
) -> Result<VerifiedRegistration, Refusal> {
    let response = format!("ok|cred-{0}|pk-{0}", subject.subject);
    m.l()
        .finish_registration(&RegistrationFinish {
            registration_id: id,
            response: response.as_bytes(),
            operator: session,
            now: m.now(),
        })
        .expect("store")
}

fn in_session(operator: &SubjectId, id: &str) -> OperatorSession {
    OperatorSession {
        operator: operator.clone(),
        session_id: id.into(),
    }
}

fn enroll_in(e: NewEnrollment, session: &str) -> CeremonyOp {
    CeremonyOp::Enroll {
        enrollment: e,
        operator_session: session.into(),
    }
}

/// `name` (approver in the fixture scope) with `reg` as its credential.
fn with_credential(name: &str, reg: VerifiedRegistration) -> NewEnrollment {
    let mut e = person(name, &[(Permission::Approver, T, P)], vec![]);
    e.credential = Some(reg);
    e
}

#[test]
fn y2_registration_rows_are_bound_to_the_initiating_operator_session() {
    let mut m = M4::new("y2-binding");
    let unsupported = Some(ap(ApprovalCode::EnrollmentUnsupported));
    // A second global operator.
    m.enroll(person("olga", &[(Permission::Operator, "*", "*")], vec![]))
        .unwrap();
    let olga = human("olga");
    let s1 = in_session(&root(), "s1");

    // Happy path: begun, finished and enrolled by root in s1; the row records the operator,
    // the page session and the relying party, and is consumed by the enrollment.
    let id = begin_row(&m, &human("kay"), Some(&s1)).unwrap();
    assert_eq!(
        m.count(&format!(
            "SELECT count(*) FROM registrations WHERE registration_id = '{id}' \
             AND operation = 'enroll' AND operator_issuer = 'writ-local' \
             AND operator_subject = 'root' AND operator_session = 's1' \
             AND rp_id = 'localhost' AND origin = '{ORIGIN}'"
        )),
        1
    );
    let reg = finish_row(&m, &id, &human("kay"), Some(&s1)).unwrap();
    let op = enroll_in(with_credential("kay", reg), "s1");
    let out = m.ceremony(&root(), &op, "y2-ok").unwrap();
    assert_eq!(
        out.result,
        CeremonyResult::Enrolled {
            subject: human("kay")
        }
    );
    assert_eq!(
        m.text(
            "SELECT status FROM registrations WHERE registration_id = ?1",
            &id
        )
        .as_deref(),
        Some("enrolled")
    );

    // No operator session: the serving ledger never creates an unbound row.
    assert_eq!(begin_row(&m, &human("lou"), None).err(), unsupported);
    // An operator session of someone who is not an active global operator.
    assert_eq!(
        begin_row(&m, &human("lou"), Some(&in_session(&human("alice"), "s1"))).err(),
        Some(ap(ApprovalCode::PermissionMissing))
    );
    assert_eq!(
        begin_row(&m, &human("lou"), Some(&in_session(&human("nobody"), "s1"))).err(),
        Some(ap(ApprovalCode::PermissionMissing))
    );
    // An empty session id.
    assert_eq!(
        begin_row(&m, &human("lou"), Some(&in_session(&root(), ""))).err(),
        unsupported
    );
    assert_eq!(
        m.count("SELECT count(*) FROM registrations WHERE subject = 'lou'"),
        0
    );

    // A finish by another operator, in another session or without one burns the row.
    for (n, wrong) in [
        Some(in_session(&olga, "s1")),
        Some(in_session(&root(), "s2")),
        None,
    ]
    .into_iter()
    .enumerate()
    {
        let who = human(&format!("fin-{n}"));
        let id = begin_row(&m, &who, Some(&s1)).unwrap();
        assert_eq!(
            finish_row(&m, &id, &who, wrong.as_ref()).err(),
            unsupported,
            "{n}"
        );
        assert_eq!(
            finish_row(&m, &id, &who, Some(&s1)).err(),
            Some(challenge(ChallengeRefusal::Consumed)),
            "{n}"
        );
    }

    // The Enroll ceremony by another operator, or in another session, is refused at begin.
    let id = begin_row(&m, &human("mia"), Some(&s1)).unwrap();
    let reg = finish_row(&m, &id, &human("mia"), Some(&s1)).unwrap();
    let mia = with_credential("mia", reg);
    assert_eq!(
        m.begin(&olga, &enroll_in(mia.clone(), "s1"), "y2-o").err(),
        unsupported
    );
    assert_eq!(
        m.begin(&root(), &enroll_in(mia.clone(), "s2"), "y2-s")
            .err(),
        unsupported
    );
    // ... and at finish (a session changed after the begin burns the challenge).
    let c = m
        .begin(&root(), &enroll_in(mia.clone(), "s1"), "y2-f")
        .unwrap();
    assert_eq!(
        m.finish(
            &enroll_in(mia.clone(), "s2"),
            &c.challenge_id,
            &ok(&root()),
            "y2-f"
        )
        .err(),
        unsupported
    );
    assert_eq!(
        m.finish(
            &enroll_in(mia.clone(), "s1"),
            &c.challenge_id,
            &ok(&root()),
            "y2-f"
        )
        .err(),
        Some(challenge(ChallengeRefusal::Consumed))
    );
    // Another subject's registration never enrolls a different subject.
    let mut other = mia.clone();
    other.subject = human("ned");
    assert_eq!(
        m.begin(&root(), &enroll_in(other, "s1"), "y2-n").err(),
        unsupported
    );
    // The right operator, session and subject still enroll it.
    assert!(m.ceremony(&root(), &enroll_in(mia, "s1"), "y2-m").is_ok());

    // A row of olga's session enrolls only through olga.
    let so = in_session(&olga, "so");
    let id = begin_row(&m, &human("pia"), Some(&so)).unwrap();
    let reg = finish_row(&m, &id, &human("pia"), Some(&so)).unwrap();
    let pia = enroll_in(with_credential("pia", reg), "so");
    assert_eq!(m.begin(&root(), &pia, "y2-p1").err(), unsupported);
    let c = m.begin(&olga, &pia, "y2-p2").unwrap();
    assert!(m.finish(&pia, &c.challenge_id, &ok(&olga), "y2-p2").is_ok());

    // Another relying party (origin): a ledger reopened with another channel does not
    // enroll a row registered under the original one.
    let id = begin_row(&m, &human("quinn"), Some(&s1)).unwrap();
    let reg = finish_row(&m, &id, &human("quinn"), Some(&s1)).unwrap();
    let quinn = enroll_in(with_credential("quinn", reg), "s1");
    let other_rp = Arc::new(TestVerifier::default());
    other_rp.other_origin.store(true, Ordering::SeqCst);
    let original = m.reopen_with(&other_rp);
    assert_eq!(m.begin(&root(), &quinn, "y2-rp").err(), unsupported);
    m.reopen_with(&original);

    // Expired: the enrollment must commit within the row's lifetime.
    let c = m.begin(&root(), &quinn, "y2-exp").unwrap();
    m.w.clock.set(now().plus_seconds(REGISTRATION_TTL_SECONDS));
    assert!(m
        .finish(&quinn, &c.challenge_id, &ok(&root()), "y2-exp")
        .is_err());
    assert_eq!(
        m.count("SELECT count(*) FROM subjects WHERE subject = 'quinn'"),
        0
    );
}

#[test]
fn y2_the_bootstrap_registration_stays_one_shot_and_unbound() {
    let m = M4::new("y2-bootstrap");
    // The bootstrap row carries no operator session.
    assert_eq!(
        m.count(
            "SELECT count(*) FROM registrations WHERE operation = 'bootstrap' \
             AND operator_issuer IS NULL AND operator_subject IS NULL \
             AND operator_session IS NULL"
        ),
        1
    );
    // The schema refuses an `enroll` row without an operator session and a `bootstrap` row
    // with one.
    for sql in [
        "INSERT INTO registrations (registration_id, operation, issuer, subject, rp_id, origin, \
         ceremony_state, issued_at, expires_at, status) VALUES ('r-x', 'enroll', 'i', 's', \
         'localhost', 'o', x'00', 't', 't', 'issued')",
        "INSERT INTO registrations (registration_id, operation, issuer, subject, rp_id, origin, \
         ceremony_state, issued_at, expires_at, status, operator_issuer, operator_subject, \
         operator_session) VALUES ('r-y', 'bootstrap', 'i', 's', 'localhost', 'o', x'00', 't', \
         't', 'issued', 'i', 'root', 's1')",
    ] {
        assert!(m.l().db().conn.execute(sql, []).is_err(), "{sql}");
    }
}

#[test]
fn y2_the_page_context_is_the_bound_context() {
    // R6: what `ceremony_context` returns for each operation is exactly the context whose
    // fingerprint a begin binds.
    let m = M4::new("y2-context");
    let id = begin_row(&m, &human("ray"), Some(&in_session(&root(), "s1"))).unwrap();
    let reg = finish_row(&m, &id, &human("ray"), Some(&in_session(&root(), "s1"))).unwrap();
    let ops = [
        enroll_in(with_credential("ray", reg), "s1"),
        CeremonyOp::Revoke {
            target: RevocationTarget::Subject(human("alice")),
            reason: "<left>".into(),
        },
        CeremonyOp::RoleChange {
            change: writ_broker::approval_store::RoleChange::Grant {
                subject: human("alice"),
                permission: Permission::SignoffAuthority,
                tenant_id: T.into(),
                project_id: P.into(),
            },
        },
    ];
    for (n, op) in ops.iter().enumerate() {
        let shown = m
            .l()
            .ceremony_context_for_tests(op, m.now())
            .unwrap()
            .unwrap();
        assert_eq!(
            writ_broker::approval::context_fingerprint(&shown.context).unwrap(),
            shown.binding.context_fingerprint,
            "{n}"
        );
        let c = m.begin(&root(), op, &format!("ctx-{n}")).unwrap();
        let stored = m
            .text(
                "SELECT context_fingerprint FROM challenges WHERE challenge_id = ?1",
                &c.challenge_id,
            )
            .unwrap();
        assert_eq!(stored, shown.binding.context_fingerprint.as_str(), "{n}");
    }
    // An unknown target is refused, as at begin.
    let unknown = CeremonyOp::Revoke {
        target: RevocationTarget::Credential("cred-none".into()),
        reason: "x".into(),
    };
    assert!(m
        .l()
        .ceremony_context_for_tests(&unknown, m.now())
        .unwrap()
        .is_err());
    // No stored promotion context names this signoff id.
    assert_eq!(m.l().pending_signoff_for_tests("so-none").unwrap(), None);
}

#[test]
fn y2_a_registration_row_is_created_only_by_an_authenticated_operator_start_ceremony() {
    let m = M4::new("y2-start");
    let unsupported = Some(ap(ApprovalCode::EnrollmentUnsupported));
    let rows = |sql: &str| m.count(&format!("SELECT count(*) FROM registrations WHERE {sql}"));
    // Unauthenticated row creation: the serving ledger's registration begin refuses with or
    // without an operator session.
    for session in [None, Some(operator_session(&root()))] {
        let r = m
            .l()
            .begin_registration(&RegistrationBegin {
                subject: &human("una"),
                operator: session.as_ref(),
                now: m.now(),
            })
            .unwrap();
        assert_eq!(r.err(), unsupported);
    }
    assert_eq!(rows("subject = 'una'"), 0);

    // Refused starts: a non-operator, an unknown subject, a service operator, self
    // registration, an actor other than the operator the operation names.
    m.enroll(person("olga", &[(Permission::Operator, "*", "*")], vec![]))
        .unwrap();
    let mut svc = person("svc-op", &[(Permission::Operator, "*", "*")], vec![]);
    svc.kind = PrincipalKind::Service;
    m.enroll(svc).unwrap();
    let start = |who: &str, op_by: &str, subject: &str| {
        m.begin(
            &human(who),
            &start_op(&human(subject), &human(op_by), "s1"),
            &format!("st-{who}-{subject}"),
        )
        .err()
    };
    assert_eq!(
        start("alice", "alice", "una"),
        Some(ap(ApprovalCode::PermissionMissing))
    );
    assert_eq!(
        start("nobody", "nobody", "una"),
        Some(ap(ApprovalCode::PermissionMissing))
    );
    assert!(start("svc-op", "svc-op", "una").is_some());
    assert_eq!(
        start("root", "root", "root"),
        unsupported,
        "self: an existing subject"
    );
    assert_eq!(
        m.begin(
            &root(),
            &start_op(&SubjectId::new("other-realm", "ROOT"), &root(), "s1"),
            "st-alias"
        )
        .err(),
        Some(ap(ApprovalCode::SeparationViolated))
    );
    assert_eq!(start("olga", "root", "una"), unsupported);
    assert_eq!(
        start("root", "root", "alice"),
        unsupported,
        "existing subject"
    );
    assert_eq!(
        m.begin(&root(), &start_op(&human("una"), &root(), ""), "st-empty")
            .err(),
        unsupported
    );
    assert_eq!(rows("subject = 'una'"), 0);

    // The displayed context names the operator, the subject, the operation, the relying
    // party and the row lifetime; it is what the challenge binds.
    let op = start_op(&human("una"), &root(), "s1");
    let shown = m
        .l()
        .ceremony_context_for_tests(&op, m.now())
        .unwrap()
        .unwrap();
    for member in [
        "operator",
        "subject",
        "rp_id",
        "origin",
        "registration_lifetime_seconds",
    ] {
        assert!(shown.context.get(member).is_some(), "{member}");
    }
    assert_eq!(
        shown.context.get("origin").and_then(Value::as_str),
        Some(ORIGIN)
    );

    // A forged assertion is a terminal refusal: the challenge is burnt, no row.
    let c = m.begin(&root(), &op, "st-burn").unwrap();
    assert_eq!(
        m.finish(&op, &c.challenge_id, "bad:cred-root", "st-burn")
            .err(),
        Some(ap(ApprovalCode::AssertionInvalid(
            VerifierError::SignatureInvalid
        )))
    );
    assert_eq!(
        m.finish(&op, &c.challenge_id, &ok(&root()), "st-burn")
            .err(),
        Some(challenge(ChallengeRefusal::Consumed))
    );
    // L6: an unavailable verifier burns nothing; the same challenge then succeeds.
    let c = m.begin(&root(), &op, "st-ok").unwrap();
    assert_eq!(
        m.finish(&op, &c.challenge_id, "down:cred-root", "st-ok")
            .err(),
        Some(ap(ApprovalCode::AssertionInvalid(
            VerifierError::Unavailable
        )))
    );
    let first = m
        .finish(&op, &c.challenge_id, &ok(&root()), "st-ok")
        .unwrap();
    let CeremonyResult::RegistrationStarted { registration } = &first.result else {
        panic!("{first:?}");
    };
    assert!(!first.replayed);
    assert_eq!(
        rows(&format!(
            "registration_id = '{}' AND started_by = '{}' AND operator_subject = 'root' \
             AND operator_session = 's1' AND subject = 'una' AND status = 'issued'",
            registration.registration_id, c.challenge_id
        )),
        1
    );
    assert_eq!(
        m.count(&format!(
            "SELECT count(*) FROM approval_events e JOIN ledger_sequence q ON q.seq = e.ledger_seq \
             WHERE e.kind = 'registration_started' AND e.challenge_id = '{}' \
             AND q.reference = '{}'",
            c.challenge_id, c.challenge_id
        )),
        1
    );
    // X3: an identical retry replays the same row; a changed operation does not.
    let again = m
        .finish(&op, &c.challenge_id, &ok(&root()), "st-ok")
        .unwrap();
    assert!(again.replayed);
    assert_eq!(again.result, first.result);
    assert_eq!(again.ledger_seq, first.ledger_seq);
    let other = start_op(&human("una"), &root(), "s2");
    assert_eq!(
        m.finish(&other, &c.challenge_id, &ok(&root()), "st-ok")
            .err(),
        Some(challenge(ChallengeRefusal::BindingMismatch)),
        "a retry from another page session never receives the row"
    );
    let changed = start_op(&human("vic"), &root(), "s1");
    assert_eq!(
        m.finish(&changed, &c.challenge_id, &ok(&root()), "st-ok")
            .err(),
        Some(challenge(ChallengeRefusal::BindingMismatch))
    );
    assert_eq!(rows("subject = 'una'"), 1);
    // The started row then registers and enrolls through the same operator session.
    let s1 = in_session(&root(), "s1");
    let reg = finish_row(&m, &registration.registration_id, &human("una"), Some(&s1)).unwrap();
    assert!(m
        .ceremony(
            &root(),
            &enroll_in(with_credential("una", reg), "s1"),
            "st-en"
        )
        .is_ok());
}
