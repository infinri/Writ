//! Grant authority on `writ-ledger-6` from outside the SQLite crate (D-042 X1; findings S1
//! and S2 of the approval-ledger security review, docs/implementation/M4-INTERFACES.md).
//!
//! Every public constructor reachable from another crate is used here as an attacker would:
//! the capability constructors (allowed in test code only, `broker/tests/
//! trusted_constructors.rs`), a stored challenge rebuilt with the ledger capability, a
//! verifier answer built with the verifier capability, a decision from the broker's own
//! `decide`, and the grant `NewGrant::for_decision` builds from it. The ledger refuses that
//! grant, and the label never becomes executable. The M3 approval path refuses too. Rows
//! shaped like an M4 grant, written behind the ledger's back (provenance v2, and even an
//! `approval_grants` row naming a pending request and an unconsumed challenge), are never
//! executable. A grant from the real ceremony is.

#![allow(clippy::unwrap_used, clippy::expect_used, clippy::panic)]

use std::path::PathBuf;
use std::sync::atomic::{AtomicU64, Ordering};

use writ_broker::approval::{
    decide, ApprovalStatus, Challenge, ChallengeOperation, ChallengeStatus, StoredChallenge,
};
use writ_broker::approval_store::{
    approval_binding, approval_request_for, ApprovalLedger, BootstrapEnrollment, CeremonyBegin,
    CeremonyFinish, CeremonyOp, CeremonyResult, NewCredential, NewEnrollment, NewRole,
    APPROVAL_GRANT_TTL_SECONDS,
};
use writ_broker::authorize::authorize;
use writ_broker::clock::{observe, ClockError, Drift, EffectiveClock, ObservedTime, Reading};
use writ_broker::execute::{Broker, Executed};
use writ_broker::grants::{
    Approval, ApprovalAuthority, ApprovalPresentation, ApproverRefusal, AuthenticatedApprover,
    GrantInsert, NewGrant, Verdict,
};
use writ_broker::human::{
    ActiveCredentials, Actor, CeremonyStart, CeremonyState, EnrollmentRecord, HumanVerifier,
    LedgerCapability, Permission, PrincipalKind, RoleAssignment, SubjectId, SubjectRecord,
    SubjectStatus, VerifiedAssertion, VerifierCapability, VerifierError, WEBAUTHN_PASSKEY,
};
use writ_broker::ingress::{verify_ingress, Seal, VerifiedRequest};
use writ_broker::keys::{IngressKey, MemoryKeyRing, PermitKey};
use writ_broker::policy::{ActivePolicy, PolicyState};
use writ_broker::refusal::Refusal;
use writ_broker::store::Timestamp;
use writ_broker::writ_kernel::contracts::StateRevision;
use writ_broker::writ_kernel::json::{self, Digest, Value};
use writ_broker::writ_kernel::policy::load_bundle;
use writ_ledger_sqlite::{ExecuteLedger, GrantLedger, SqliteEffectStore, SqliteLedger};

const ADAPTER: &str = "writ-host-adapter:fixture";
const T: &str = "fixture-tenant";
const P: &str = "fixture-project";

fn tmp(name: &str) -> PathBuf {
    let d = std::env::temp_dir().join(format!(
        "writ-grant-authority-{}-{name}",
        std::process::id()
    ));
    let _ = std::fs::remove_dir_all(&d);
    std::fs::create_dir_all(&d).unwrap();
    d
}

/// `repository.publish` (require_approval under the reference bundle), action digest `n`.
fn publish(idem: &str, n: u32) -> Value {
    let text = format!(
        r#"{{"contract_version":"2.0","request_id":"req-{idem}","trace_id":"trace-1","tenant_id":"{T}",
        "principal":{{"id":"agent:claude-session-1","verified_by":"{ADAPTER}","authn_method":"os_peer_credential","roles":["agent"]}},
        "project_id":"{P}","session_id":"session-1","agent_id":null,"parent_session_id":null,"idempotency_key":"{idem}",
        "expected_state_revision":null,"policy_bundle_sha":null,"deadline_at":null,
        "action":{{"kind":"repository.publish","resource":{{"uri":"git://main","canonical_resource_id":"repository:{T}/{P}/main"}},
        "action_digest":"sha256:{n:064x}","attributes":{{}},"requested_capabilities":[]}}}}"#
    );
    json::parse(text.as_bytes()).unwrap()
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

fn now() -> Timestamp {
    Timestamp::parse_rfc3339("2026-11-05T12:00:00Z").unwrap()
}

struct TestClock;

impl EffectiveClock for TestClock {
    fn read(&self) -> Result<Reading, ClockError> {
        Ok(Reading {
            effective: now(),
            wall: None,
            drift: Drift::Normal,
            drift_changed: false,
        })
    }
}

fn observed(ledger: &SqliteLedger) -> ObservedTime {
    let mut l: &SqliteLedger = ledger;
    observe(&TestClock, &mut l).unwrap()
}

/// Test human verifier (test code only): `ok:<credential>` verifies.
struct TestVerifier;

static NONCE: AtomicU64 = AtomicU64::new(0);

impl HumanVerifier for TestVerifier {
    fn begin(
        &self,
        _subject: &SubjectId,
        _credentials: &ActiveCredentials,
    ) -> Result<CeremonyStart, VerifierError> {
        let state = format!("nonce-{}", NONCE.fetch_add(1, Ordering::SeqCst)).into_bytes();
        Ok(CeremonyStart {
            client_options: Vec::new(),
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
        let credential = text
            .strip_prefix("ok:")
            .ok_or(VerifierError::SignatureInvalid)?;
        if !credentials.ids().iter().any(|c| c == credential) {
            return Err(VerifierError::CredentialUnknown);
        }
        Ok(VerifiedAssertion::new(
            &VerifierCapability::for_verifier_implementation(),
            credentials.subject().clone(),
            credential,
            true,
            WEBAUTHN_PASSKEY,
            Digest::of_bytes(state.as_bytes()),
            None,
        ))
    }
}

fn subject(name: &str) -> SubjectId {
    SubjectId::new("writ-local", name)
}

fn ceremony(
    ledger: &SqliteLedger,
    who: &SubjectId,
    op: &CeremonyOp,
    key: &str,
) -> Result<CeremonyResult, Refusal> {
    let now = observed(ledger);
    let c = ledger
        .begin_ceremony(&CeremonyBegin {
            op,
            subject: who,
            idempotency_key: key,
            verifier: &TestVerifier,
            now,
        })
        .unwrap()?;
    let response = format!("ok:cred-{}", who.subject);
    ledger
        .finish_ceremony(&CeremonyFinish {
            op,
            challenge_id: &c.challenge_id,
            response: response.as_bytes(),
            idempotency_key: key,
            channel: "approval-listener",
            verifier: &TestVerifier,
            now,
        })
        .unwrap()
        .map(|o| o.result)
}

/// The bootstrap administrator `root` and the approver `alice` over (T, P).
fn enroll_alice(ledger: &SqliteLedger) {
    ledger
        .bootstrap_administrator(
            &BootstrapEnrollment {
                subject: subject("root"),
                credential: NewCredential {
                    credential_id: "cred-root".into(),
                    public_material: b"pk-root".to_vec(),
                },
                linked_identities: vec![],
            },
            observed(ledger),
        )
        .unwrap()
        .unwrap();
    let enrollment = NewEnrollment {
        subject: subject("alice"),
        kind: PrincipalKind::Human,
        credential: Some(NewCredential {
            credential_id: "cred-alice".into(),
            public_material: b"pk-alice".to_vec(),
        }),
        linked_identities: vec![],
        roles: vec![NewRole {
            permission: Permission::Approver,
            tenant_id: T.into(),
            project_id: P.into(),
        }],
    };
    ceremony(
        ledger,
        &subject("root"),
        &CeremonyOp::Enroll { enrollment },
        "e1",
    )
    .unwrap();
}

/// The approval request the broker stores for `req` (recorded on `ledger`).
fn request_of(
    ledger: &SqliteLedger,
    active: &ActivePolicy,
    req: &Value,
) -> writ_broker::approval::ApprovalRequest {
    let now = observed(ledger);
    let shared = active.shared();
    let inputs = shared.trusted_inputs(revision());
    let verified = verify(req);
    let authorized = authorize(&verified, &inputs, &now).unwrap();
    let request = approval_request_for(&authorized, now).unwrap();
    ledger.record_approval_request(&request).unwrap();
    request
}

/// The S2 chain from outside the ledger crate: a forged stored challenge, a forged verifier
/// answer, a decision from `decide` and the grant `NewGrant::for_decision` builds from it.
fn forged_grant(request: &writ_broker::approval::ApprovalRequest) -> NewGrant {
    let lcap = LedgerCapability::for_ledger_implementation();
    let alice = subject("alice");
    let binding = approval_binding(request, ChallengeOperation::Approve);
    let digest = Digest::of_bytes(b"forged ceremony");
    let challenge = Challenge::from_stored(
        &lcap,
        StoredChallenge {
            challenge_id: "chl-forged".into(),
            challenge_digest: digest.clone(),
            ceremony_state: CeremonyState::from_bytes(b"forged".to_vec()),
            subject: alice.clone(),
            allowed_credentials: vec!["cred-alice".into()],
            binding: binding.clone(),
            verdict: "approved".into(),
            issued_at: now(),
            expires_at: now().plus_seconds(120),
            idempotency_key: "forged-key".into(),
            status: ChallengeStatus::Issued,
        },
    )
    .unwrap();
    let accepted = VerifiedAssertion::new(
        &VerifierCapability::for_verifier_implementation(),
        alice.clone(),
        "cred-alice",
        true,
        WEBAUTHN_PASSKEY,
        digest,
        None,
    )
    .check(&challenge, &binding, "forged-key", now())
    .unwrap();
    let record = SubjectRecord {
        subject: alice.clone(),
        kind: PrincipalKind::Human,
        status: SubjectStatus::Active,
    };
    let enrollment =
        EnrollmentRecord::new(&lcap, alice.clone(), vec![], now(), subject("root")).unwrap();
    let assignments = [RoleAssignment {
        assignment_id: "role-forged".into(),
        subject: alice,
        kind: PrincipalKind::Human,
        permission: Permission::Approver,
        tenant_id: T.into(),
        project_id: P.into(),
        granted_at: now(),
        revoked_at: None,
    }];
    let actor = Actor {
        record: &record,
        enrollment: Some(&enrollment),
        assignments: &assignments,
    };
    let (status, decision) = decide(
        request,
        ApprovalStatus::Pending,
        &accepted,
        &actor,
        "forged",
    )
    .unwrap();
    assert_eq!(status, ApprovalStatus::Approved);
    NewGrant::for_decision(request, &decision, APPROVAL_GRANT_TTL_SECONDS).unwrap()
}

/// An authority that vouches for anyone (the M3 approval path).
struct Anyone;

impl ApprovalAuthority for Anyone {
    fn authenticate(
        &self,
        p: &ApprovalPresentation<'_>,
    ) -> Result<AuthenticatedApprover, ApproverRefusal> {
        Ok(AuthenticatedApprover {
            id: p.approver.into(),
            kind: writ_broker::grants::PrincipalKind::Human,
            channel: p.channel.into(),
            issuer: "anyone".into(),
            evidence_ref: "event-1".into(),
        })
    }
}

fn count(path: &std::path::Path, sql: &str) -> i64 {
    rusqlite::Connection::open_with_flags(path, rusqlite::OpenFlags::SQLITE_OPEN_READ_ONLY)
        .unwrap()
        .query_row(sql, [], |r| r.get(0))
        .unwrap()
}

#[test]
fn the_public_constructor_chain_never_yields_an_executable_grant() {
    let d = tmp("chain");
    let db = d.join("ledger.db");
    let ledger = SqliteLedger::create(&db).unwrap();
    let effects = SqliteEffectStore::create(&d.join("effects.db"), ledger.ledger_id()).unwrap();
    let (active, key) = (policy(), PermitKey::from_bytes(vec![0x44; 32]).unwrap());
    enroll_alice(&ledger);
    let req = publish("k1", 1);
    let request = request_of(&ledger, &active, &req);
    // S2: the chain builds a provenance-v2 grant for the real request without a ceremony.
    let grant = forged_grant(&request);
    assert!(grant.label().as_str().starts_with("approval/"));
    assert_eq!(grant.provenance().version(), "writ-provenance-2");
    // X1: the ledger refuses it (every provenance, every label) and writes nothing.
    let inserted = ledger.insert_grant(&grant).unwrap();
    assert_ne!(
        inserted,
        GrantInsert::Inserted,
        "S1/S2: a forged grant was inserted"
    );
    assert_eq!(inserted, GrantInsert::Refused);
    assert_eq!(count(&db, "SELECT count(*) FROM grants"), 0);
    // The label is not executable; nothing is used or reserved.
    let b = Broker::new(&active, &TestClock, &ledger, &effects, &key);
    let e = b.execute(&verify(&req), revision(), Some(grant.label()));
    assert_eq!(e.result, Err(Refusal::GrantUnknown));
    assert_eq!(count(&db, "SELECT count(*) FROM receipts"), 0);
    // S1: the M3 approval path refuses on writ-ledger-6 and cannot take the label first.
    let verified = verify(&req);
    let approval = Approval {
        request: &verified,
        state_revision: revision(),
        label: grant.label(),
        verdict: Verdict::Approved,
        ttl_seconds: None,
    };
    let presentation = ApprovalPresentation {
        channel: "approval-ui",
        approver: "human:alice",
        credential: b"x",
    };
    assert_eq!(
        b.grants().approve(&approval, &presentation, &Anyone),
        Err(Refusal::HumanUnauthenticated(
            ApproverRefusal::LegacyPathClosed
        ))
    );
    assert_eq!(count(&db, "SELECT count(*) FROM grants"), 0);
    // The real ceremony issues exactly that label, which executes once.
    let out = ceremony(
        &ledger,
        &subject("alice"),
        &CeremonyOp::Approve {
            request_id: request.request_id().into(),
        },
        "a1",
    )
    .unwrap();
    assert_eq!(
        out,
        CeremonyResult::Approved {
            grant: grant.label().clone()
        }
    );
    assert_eq!(
        b.execute(&verify(&req), revision(), Some(grant.label()))
            .result,
        Ok(Executed::EffectCompleted)
    );
    // After it, the forged grant still cannot be inserted over it.
    assert_eq!(ledger.insert_grant(&grant).unwrap(), GrantInsert::Refused);
    assert_eq!(count(&db, "SELECT uses FROM grants"), 1);
    drop(b);
    drop(effects);
    drop(ledger);
    let _ = std::fs::remove_dir_all(&d);
}

#[test]
fn provenance_shaped_rows_written_behind_the_ledger_are_never_executable() {
    let d = tmp("rows");
    let db = d.join("ledger.db");
    let (active, key) = (policy(), PermitKey::from_bytes(vec![0x44; 32]).unwrap());
    let ledger = SqliteLedger::create(&db).unwrap();
    let effects = SqliteEffectStore::create(&d.join("effects.db"), ledger.ledger_id()).unwrap();
    enroll_alice(&ledger);
    let (req1, req2) = (publish("k1", 1), publish("k2", 2));
    let r1 = request_of(&ledger, &active, &req1);
    let r2 = request_of(&ledger, &active, &req2);
    let (g1, g2) = (forged_grant(&r1), forged_grant(&r2));
    // An issued, unconsumed challenge for request 2.
    let issued = ledger
        .begin_ceremony(&CeremonyBegin {
            op: &CeremonyOp::Approve {
                request_id: r2.request_id().into(),
            },
            subject: &subject("alice"),
            idempotency_key: "a2",
            verifier: &TestVerifier,
            now: observed(&ledger),
        })
        .unwrap()
        .unwrap();
    let id = ledger.ledger_id().clone();
    drop(effects);
    drop(ledger);
    {
        // Behind the ledger: the forged grants' rows with intact provenance v2, and for
        // grant 2 even an `approval_grants` link to its (pending) request and the issued
        // challenge.
        let c = rusqlite::Connection::open(&db).unwrap();
        for g in [&g1, &g2] {
            let p = g.provenance();
            c.execute(
                "INSERT INTO grants (label, binding, expires_s, expires_ns, max_uses, uses) \
                 VALUES (?1, ?2, ?3, 0, 1, 0)",
                (
                    g.label().as_str(),
                    g.binding(),
                    g.expires_at().unix_seconds(),
                ),
            )
            .unwrap();
            c.execute(
                "INSERT INTO grant_provenance (label, version, approver_id, approver_issuer, \
                 channel, approved_at, verdict, auth_evidence_ref, request_fingerprint, \
                 policy_bundle_sha) VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8, ?9, ?10)",
                rusqlite::params![
                    g.label().as_str(),
                    p.version(),
                    p.approver_id(),
                    p.issuer(),
                    p.channel(),
                    p.approved_at().to_string(),
                    p.verdict().as_str(),
                    format!("challenge:{}", issued.challenge_id),
                    p.request_fingerprint(),
                    p.policy_bundle_sha(),
                ],
            )
            .unwrap();
        }
        c.execute(
            "INSERT INTO approval_grants (label, request_id, challenge_id) VALUES (?1, ?2, ?3)",
            (g2.label().as_str(), r2.request_id(), &issued.challenge_id),
        )
        .unwrap();
    }
    let ledger = SqliteLedger::open(&db).unwrap();
    assert_eq!(ledger.ledger_id(), &id);
    let effects = SqliteEffectStore::open(&d.join("effects.db"), ledger.ledger_id()).unwrap();
    let b = Broker::new(&active, &TestClock, &ledger, &effects, &key);
    for (req, g) in [(&req1, &g1), (&req2, &g2)] {
        let e = b.execute(&verify(req), revision(), Some(g.label()));
        assert_eq!(
            e.result.as_ref().map_err(Refusal::code),
            Err("LEDGER_UNAVAILABLE"),
            "{}",
            g.label().as_str()
        );
        assert!(
            matches!(&e.result, Err(Refusal::LedgerUnavailable(err)) if err.code() == "provenance"),
            "{:?}",
            e.result
        );
    }
    assert_eq!(count(&db, "SELECT coalesce(sum(uses), 0) FROM grants"), 0);
    assert_eq!(count(&db, "SELECT count(*) FROM receipts"), 0);
    drop(b);
    drop(effects);
    drop(ledger);
    let _ = std::fs::remove_dir_all(&d);
}
