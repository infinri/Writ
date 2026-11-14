//! M4 approval lifecycle tests on the SQLite ledger (M4-03, M4-04, M4-05, M4-08 storage,
//! M4-09, M4-10; D-040 O-3..O-9, O-14; M4-INTERFACES I-2..I-8, C1, C2): stored approval
//! requests, ceremonies through a test verifier (test code only; `VerifierCapability` is
//! allowed here), terminal denial, swapped and replayed decisions, expiry, separation and
//! eligibility, enrollment, revocation against T-reserve and T-dispatch, 16-way races,
//! migration from `writ-ledger-5` with legacy grants, unavailable storage and unknown
//! commits.

#![allow(clippy::unwrap_used, clippy::expect_used, clippy::panic)]

use std::sync::atomic::{AtomicBool, AtomicU64, Ordering};
use std::sync::Barrier;
use std::thread;

use writ_broker::approval::{ApprovalCode, ApprovalStatus, ChallengeRefusal, RevocationTarget};
use writ_broker::approval_store::{
    ApprovalLedger, BootstrapEnrollment, CeremonyBegin, CeremonyFinish, CeremonyOp,
    CeremonyOutcome, CeremonyResult, IssuedChallenge, NewCredential, NewEnrollment, NewRole,
    UseState,
};
use writ_broker::clock::{observe, ObservedTime};
use writ_broker::execute::{Executed, Execution};
use writ_broker::grants::{approval_grant_label, GrantLabel, Replay, ReserveOutcome};
use writ_broker::human::{
    ActiveCredentials, CeremonyStart, CeremonyState, CredentialUpdate, HumanVerifier, Permission,
    PrincipalKind, SubjectId, VerifiedAssertion, VerifierCapability, VerifierError,
    WEBAUTHN_PASSKEY,
};
use writ_broker::refusal::Refusal;
use writ_broker::store::{ExecuteLedger, StoreError, TxStage};
use writ_broker::writ_kernel::json::{self, Digest, Value};

use super::execute_tests::World;
use super::grant_tests::{approve, now, publish, revision, set, uses, verify};
use super::{fault, SqliteLedger, SCHEMA_V5, SCHEMA_VERSION_V5};

const T: &str = "fixture-tenant";
const P: &str = "fixture-project";
const ADAPTER: &str = "writ-host-adapter:fixture";
const REQUESTER: &str = "agent:claude-session-1";

// ---------------------------------------------------------------------------
// Test verifier (test code only).

/// Responses are `<mode>:<credential id>`: `ok`, `nouv` (no user verification), `other`
/// (an assertion from another ceremony), `update` (with a credential update); anything else
/// is a forged signature. `fixed` makes every ceremony reuse one challenge (C1 test).
#[derive(Default)]
struct TestVerifier {
    next: AtomicU64,
    fixed: AtomicBool,
}

impl HumanVerifier for TestVerifier {
    fn begin(
        &self,
        _subject: &SubjectId,
        _credentials: &ActiveCredentials,
    ) -> Result<CeremonyStart, VerifierError> {
        let n = self.next.fetch_add(1, Ordering::SeqCst);
        let state = if self.fixed.load(Ordering::SeqCst) {
            b"fixed-nonce".to_vec()
        } else {
            format!("nonce-{n}").into_bytes()
        };
        Ok(CeremonyStart {
            client_options: b"options".to_vec(),
            challenge_digest: Digest::of_bytes(&state),
            state: CeremonyState::from_bytes(state),
        })
    }

    fn finish(
        &self,
        state: &CeremonyState,
        response: &[u8],
        credentials: &ActiveCredentials,
    ) -> Result<VerifiedAssertion, VerifierError> {
        let text = std::str::from_utf8(response).map_err(|_| VerifierError::Malformed)?;
        let (mode, credential) = text.split_once(':').ok_or(VerifierError::Malformed)?;
        if !credentials.ids().iter().any(|c| c == credential) {
            return Err(VerifierError::CredentialUnknown);
        }
        let cap = VerifierCapability::for_verifier_implementation();
        let digest = Digest::of_bytes(state.as_bytes());
        let make = |uv: bool, digest: Digest, update: Option<CredentialUpdate>| {
            VerifiedAssertion::new(
                &cap,
                credentials.subject().clone(),
                credential,
                uv,
                WEBAUTHN_PASSKEY,
                digest,
                update,
            )
        };
        match mode {
            "ok" => Ok(make(true, digest, None)),
            "nouv" => Ok(make(false, digest, None)),
            "other" => Ok(make(true, Digest::of_bytes(b"another ceremony"), None)),
            "update" => Ok(make(
                true,
                digest,
                Some(CredentialUpdate {
                    credential_id: credential.to_string(),
                    public_material: b"material-2".to_vec(),
                }),
            )),
            _ => Err(VerifierError::SignatureInvalid),
        }
    }
}

// ---------------------------------------------------------------------------
// World.

fn human(s: &str) -> SubjectId {
    SubjectId::new("writ-local", s)
}

fn root() -> SubjectId {
    human("root")
}

fn requester() -> SubjectId {
    SubjectId::new(ADAPTER, REQUESTER)
}

fn cred(s: &SubjectId) -> String {
    format!("cred-{}", s.subject)
}

fn ok(s: &SubjectId) -> String {
    format!("ok:{}", cred(s))
}

fn person(name: &str, roles: &[(Permission, &str, &str)], linked: Vec<SubjectId>) -> NewEnrollment {
    NewEnrollment {
        subject: human(name),
        kind: PrincipalKind::Human,
        credential: Some(NewCredential {
            credential_id: format!("cred-{name}"),
            public_material: format!("pk-{name}").into_bytes(),
        }),
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
fn action(idem: &str, n: u32) -> Value {
    set(
        &publish(idem),
        &["action", "action_digest"],
        json::s(&digest_text(n)),
    )
}

fn digest_text(n: u32) -> String {
    format!("sha256:{n:064x}")
}

fn ap(code: ApprovalCode) -> Refusal {
    Refusal::Approval(code)
}

fn challenge(r: ChallengeRefusal) -> Refusal {
    ap(ApprovalCode::ChallengeInvalid(r))
}

struct M4 {
    w: World,
    v: TestVerifier,
}

impl M4 {
    /// A fresh ledger with the bootstrap administrator `root`, approver `alice` and
    /// revoker `carol` in (fixture-tenant, fixture-project).
    fn new(name: &str) -> M4 {
        let m = M4 {
            w: World::new(&format!("m4-{name}")),
            v: TestVerifier::default(),
        };
        let seq =
            m.w.ledger
                .bootstrap_administrator(
                    &BootstrapEnrollment {
                        subject: root(),
                        credential: NewCredential {
                            credential_id: "cred-root".into(),
                            public_material: b"pk-root".to_vec(),
                        },
                        linked_identities: vec![],
                    },
                    m.now(),
                )
                .unwrap();
        assert_eq!(seq, Ok(1));
        m.enroll(person("alice", &[(Permission::Approver, T, P)], vec![]))
            .unwrap();
        m.enroll(person("carol", &[(Permission::Revoker, T, P)], vec![]))
            .unwrap();
        m
    }

    fn l(&self) -> &SqliteLedger {
        &self.w.ledger
    }

    fn now(&self) -> ObservedTime {
        let mut l: &SqliteLedger = &self.w.ledger;
        observe(&self.w.clock, &mut l).unwrap()
    }

    fn begin(
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
                verifier: &self.v,
                now: self.now(),
            })
            .expect("store")
    }

    fn finish(
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
                channel: "approval-listener",
                verifier: &self.v,
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
        self.finish(op, &c.challenge_id, &ok(who), key)
    }

    fn enroll(&self, e: NewEnrollment) -> Result<CeremonyOutcome, Refusal> {
        let key = format!("enroll-{}", e.subject.subject);
        self.ceremony(&root(), &CeremonyOp::Enroll { enrollment: e }, &key)
    }

    fn exec(&self, req: &Value, grant: Option<&GrantLabel>) -> Execution {
        self.w.broker().execute(&verify(req), revision(), grant)
    }

    /// Ask for action `n` (APPROVAL_REQUIRED) and return the stored request id.
    fn ask(&self, idem: &str, n: u32) -> String {
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
    fn approved(&self, idem: &str, n: u32) -> (String, GrantLabel) {
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

    fn revoke(
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

    fn count(&self, sql: &str) -> i64 {
        self.l().db().conn.query_row(sql, [], |r| r.get(0)).unwrap()
    }

    fn text(&self, sql: &str, arg: &str) -> Option<String> {
        self.l().db().conn.query_row(sql, [arg], |r| r.get(0)).ok()
    }

    fn status(&self, request_id: &str) -> ApprovalStatus {
        self.l().approval_request(request_id).unwrap().unwrap().1
    }

    fn receipt(&self, idem: &str) -> Option<String> {
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
    let c = m.begin(&alice, &approve_a, "x1").unwrap();
    // The verdict comes from the challenge: a deny presented on an approve challenge.
    let deny_a = CeremonyOp::Deny {
        request_id: a.clone(),
    };
    assert_eq!(
        m.finish(&deny_a, &c.challenge_id, &ok(&alice), "x1"),
        Err(challenge(ChallengeRefusal::OperationMismatch))
    );
    // Another request presented on request a's challenge.
    let approve_b = CeremonyOp::Approve {
        request_id: b.clone(),
    };
    assert_eq!(
        m.finish(&approve_b, &c.challenge_id, &ok(&alice), "x1"),
        Err(challenge(ChallengeRefusal::TargetMismatch))
    );
    // Nothing changed and the challenge is still usable for exactly its own decision.
    assert_eq!(m.status(&a), ApprovalStatus::Pending);
    assert_eq!(m.status(&b), ApprovalStatus::Pending);
    assert!(m
        .finish(&approve_a, &c.challenge_id, &ok(&alice), "x1")
        .is_ok());
    assert_eq!(m.status(&b), ApprovalStatus::Pending);
    assert_eq!(
        m.count("SELECT count(*) FROM audit_refusals WHERE code = 'CHALLENGE_INVALID'"),
        2
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
    let c = m.begin(&alice, &op, "key-1").unwrap();
    // Before the decision: verifier refusals and binding mismatches.
    assert_eq!(
        m.finish(&op, &c.challenge_id, "forged", "key-1"),
        Err(ap(ApprovalCode::AssertionInvalid(VerifierError::Malformed)))
    );
    assert_eq!(
        m.finish(&op, &c.challenge_id, "bad:cred-alice", "key-1"),
        Err(ap(ApprovalCode::AssertionInvalid(
            VerifierError::SignatureInvalid
        )))
    );
    assert_eq!(
        m.finish(&op, &c.challenge_id, "nouv:cred-alice", "key-1"),
        Err(ap(ApprovalCode::AssertionInvalid(
            VerifierError::UserNotVerified
        )))
    );
    assert_eq!(
        m.finish(&op, &c.challenge_id, "other:cred-alice", "key-1"),
        Err(challenge(ChallengeRefusal::CeremonyMismatch))
    );
    assert_eq!(
        m.finish(&op, &c.challenge_id, &ok(&alice), "key-2"),
        Err(challenge(ChallengeRefusal::IdempotencyMismatch))
    );
    assert_eq!(
        m.finish(&op, "chl-unknown", &ok(&alice), "key-1"),
        Err(challenge(ChallengeRefusal::Unknown))
    );
    // Another approver cannot answer alice's challenge.
    m.enroll(person("dave", &[(Permission::Approver, T, P)], vec![]))
        .unwrap();
    assert_eq!(
        m.finish(&op, &c.challenge_id, &ok(&human("dave")), "key-1"),
        Err(ap(ApprovalCode::AssertionInvalid(
            VerifierError::CredentialUnknown
        )))
    );
    assert_eq!(m.status(&id), ApprovalStatus::Pending);
    // The decision, with a credential update persisted atomically.
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
    // Anything else on the consumed challenge is refused.
    assert_eq!(
        m.finish(&op, &c.challenge_id, &ok(&alice), "key-3"),
        Err(challenge(ChallengeRefusal::Consumed))
    );
    let deny = CeremonyOp::Deny {
        request_id: id.clone(),
    };
    assert_eq!(
        m.finish(&deny, &c.challenge_id, &ok(&alice), "key-1"),
        Err(challenge(ChallengeRefusal::Consumed))
    );
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
    let now = m.now();
    // One bootstrap per ledger.
    let again = BootstrapEnrollment {
        subject: human("mallory"),
        credential: NewCredential {
            credential_id: "cred-mallory".into(),
            public_material: b"pk".to_vec(),
        },
        linked_identities: vec![],
    };
    assert_eq!(
        m.l().bootstrap_administrator(&again, now).unwrap(),
        Err(ap(ApprovalCode::EnrollmentUnsupported))
    );
    let unsupported = Some(ap(ApprovalCode::EnrollmentUnsupported));
    let enroll = |e: NewEnrollment| CeremonyOp::Enroll { enrollment: e };
    // Existing subject (no recovery, no additional credentials).
    let mut again = person("alice", &[], vec![]);
    again.credential.as_mut().unwrap().credential_id = "cred-alice-2".into();
    assert_eq!(m.begin(&root(), &enroll(again), "e1").err(), unsupported);
    // A human without a credential; a service with a credential or a human-only role.
    let mut bare = person("frank", &[], vec![]);
    bare.credential = None;
    assert_eq!(m.begin(&root(), &enroll(bare), "e2").err(), unsupported);
    let mut svc = person("svc-x", &[(Permission::Approver, T, P)], vec![]);
    svc.kind = PrincipalKind::Service;
    svc.credential = None;
    assert_eq!(m.begin(&root(), &enroll(svc), "e3").err(), unsupported);
    // A credential id already stored.
    let mut dup = person("gina", &[], vec![]);
    dup.credential.as_mut().unwrap().credential_id = "cred-alice".into();
    assert_eq!(m.begin(&root(), &enroll(dup), "e4").err(), unsupported);
    // Only an operator in the global scope enrolls; nobody enrolls itself.
    assert_eq!(
        m.begin(&human("alice"), &enroll(person("hank", &[], vec![])), "e5")
            .err(),
        Some(ap(ApprovalCode::PermissionMissing))
    );
    let mut me = person("root", &[(Permission::Approver, T, P)], vec![]);
    me.subject = SubjectId::new("other-realm", "ROOT");
    me.credential.as_mut().unwrap().credential_id = "cred-root-2".into();
    assert_eq!(
        m.ceremony(&root(), &enroll(me), "e6"),
        Err(ap(ApprovalCode::SeparationViolated))
    );
    // A changed enrollment cannot reuse the displayed one's challenge.
    let shown = enroll(person("ivy", &[(Permission::Approver, T, P)], vec![]));
    let c = m.begin(&root(), &shown, "e7").unwrap();
    let escalated = enroll(person("ivy", &[(Permission::Operator, "*", "*")], vec![]));
    assert_eq!(
        m.finish(&escalated, &c.challenge_id, &ok(&root()), "e7"),
        Err(challenge(ChallengeRefusal::ContextMismatch))
    );
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
    // The global administrator is not a revoker for the request's scope.
    assert_eq!(
        m.begin(
            &root(),
            &CeremonyOp::Revoke {
                target: target(),
                reason: "x".into()
            },
            "r0"
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
            channel: "approval-listener",
            verifier: &m.v,
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
                channel: "approval-listener",
                verifier: &m.v,
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
    let enroll = CeremonyOp::Enroll {
        enrollment: person("zed", &[(Permission::Approver, T, P)], vec![]),
    };
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
            verifier: &m.v,
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
                channel: "approval-listener",
                verifier: &m.v,
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
    // Pre-M4 grants (writ-provenance-1) in every receipt state.
    for (g, idem) in [
        ("g-unused", "k1"),
        ("g-reserved", "k2"),
        ("g-done", "k3"),
        ("g-dispatched", "k4"),
    ] {
        approve(b.grants(), &publish(idem), g).unwrap();
    }
    assert_eq!(
        b.grants().reserve(
            &verify(&publish("k2")),
            revision(),
            Some(&GrantLabel::new("g-reserved").unwrap())
        ),
        Ok(ReserveOutcome::Reserved)
    );
    assert_eq!(
        b.execute(
            &verify(&publish("k3")),
            revision(),
            Some(&GrantLabel::new("g-done").unwrap())
        )
        .result,
        Ok(Executed::EffectCompleted)
    );
    w.ledger
        .inject("observe", fault::Stage::Begin, rusqlite::ffi::SQLITE_IOERR);
    assert!(b
        .execute(
            &verify(&publish("k4")),
            revision(),
            Some(&GrantLabel::new("g-dispatched").unwrap())
        )
        .result
        .is_err());
    drop(b);
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
        "g-dispatched/writ-ledger-5,g-done/writ-ledger-5,g-reserved/writ-ledger-5,g-unused/writ-ledger-5"
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
        b.execute(
            &verify(&publish(idem)),
            revision(),
            Some(&GrantLabel::new(g).unwrap()),
        )
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
