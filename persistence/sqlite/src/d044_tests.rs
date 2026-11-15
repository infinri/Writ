//! D-044 item 3 (M4-INTERFACES Z1..Z4): authenticated, scoped record reads on the SQLite
//! ledger. A read-session login is a purpose-separated ceremony (`ReadSession`, verdict
//! `read`) bound to the subject, its active credentials, the page session and the configured
//! channel; every protected read goes through `ApprovalLedger::read_record`, which rechecks
//! the session, the subject, the credential and the scoped permission inside its read
//! transaction and refuses every failure with the one `ReadRefused`. Test verifier only.

#![allow(clippy::unwrap_used, clippy::expect_used, clippy::panic)]

use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::Arc;

use writ_broker::approval::{ApprovalCode, ApprovalStatus, ChallengeRefusal, RevocationTarget};
use writ_broker::approval_store::{
    ApprovalLedger, CeremonyOp, CeremonyResult, ReadRefused, ReadSession, ReadTarget, RecordRead,
    RecordView, RoleChange, READ_SESSION_MAX_SECONDS,
};
use writ_broker::human::{LedgerCapability, Permission, SubjectId, VerifierError};
use writ_broker::refusal::Refusal;

use super::grant_tests::now;
use super::m4_approval_tests::{ap, challenge, human, ok, person, root, M4};
use super::m4_support::{start_op, TestVerifier};

static KEYS: AtomicU64 = AtomicU64::new(0);

fn key(what: &str) -> String {
    format!("{what}-{}", KEYS.fetch_add(1, Ordering::SeqCst))
}

fn login_op(who: &SubjectId, page: &str) -> CeremonyOp {
    CeremonyOp::ReadSession {
        subject: who.clone(),
        page_session: page.into(),
    }
}

/// `who` logs in on page session `page` (a full login ceremony).
fn login(m: &M4, who: &SubjectId, page: &str) -> ReadSession {
    match m.ceremony(who, &login_op(who, page), &key("login")) {
        Ok(out) => match out.result {
            CeremonyResult::ReadSessionOpened { session } => session,
            other => panic!("a login answers ReadSessionOpened, got {other:?}"),
        },
        Err(r) => panic!("login of {} refused: {r:?}", who.subject),
    }
}

fn read(m: &M4, s: &ReadSession, target: ReadTarget) -> Result<RecordView, ReadRefused> {
    m.l()
        .read_record(&RecordRead {
            session: s,
            target: &target,
            now: m.now(),
        })
        .unwrap()
}

fn req(id: &str) -> ReadTarget {
    ReadTarget::ApprovalRequest {
        request_id: id.into(),
    }
}

fn ceremony(op: CeremonyOp) -> ReadTarget {
    ReadTarget::Ceremony(Box::new(op))
}

fn revoke(target: RevocationTarget) -> ReadTarget {
    ceremony(CeremonyOp::Revoke {
        target,
        reason: "read test".into(),
    })
}

#[test]
fn z1_a_login_opens_one_bound_read_session_with_provenance_and_order() {
    let m = M4::new("z1-login");
    let alice = human("alice");
    let op = login_op(&alice, "page-1");
    let k = key("login");
    let c = m.begin(&alice, &op, &k).unwrap();
    // The challenge binds the purpose, the subject's credentials, the page session and the
    // configured channel, at most 120 s.
    assert_eq!(
        m.text(
            "SELECT operation || ' ' || verdict || ' ' || tenant_id || ' ' || project_id \
             || ' ' || rp_id || ' ' || origin || ' ' || allowed_credentials \
             FROM challenges WHERE challenge_id = ?1",
            &c.challenge_id
        )
        .unwrap(),
        "read_session read * * localhost http://localhost:8443 [\"cred-alice\"]"
    );
    assert!(c.expires_at <= now().plus_seconds(120));
    let seq_before = m.count("SELECT count(*) FROM ledger_sequence");
    let out = m.finish(&op, &c.challenge_id, &ok(&alice), &k).unwrap();
    let CeremonyResult::ReadSessionOpened { session } = &out.result else {
        panic!("{out:?}")
    };
    assert!(!out.replayed);
    assert_eq!(session.session_id(), c.challenge_id);
    assert_eq!(session.subject(), &alice);
    assert_eq!(session.credential_id(), "cred-alice");
    assert_eq!(session.page_session(), "page-1");
    assert_eq!(
        session.expires_at(),
        session.issued_at().plus_seconds(READ_SESSION_MAX_SECONDS)
    );
    assert_eq!(READ_SESSION_MAX_SECONDS, 900);
    // Consumed with an approval event and one I-8 row, in the same transaction.
    assert_eq!(m.challenge_status(&c.challenge_id), "consumed ");
    assert_eq!(
        m.count("SELECT count(*) FROM ledger_sequence"),
        seq_before + 1
    );
    assert_eq!(
        m.text(
            "SELECT kind || ' ' || reference FROM ledger_sequence WHERE seq = \
             (SELECT max(seq) FROM ledger_sequence) AND ?1 = ?1",
            ""
        ),
        Some(format!("read_session {}", c.challenge_id))
    );
    assert_eq!(
        m.text(
            "SELECT actor_subject || ' ' || credential_id || ' ' || channel FROM approval_events \
             WHERE kind = 'read_session' AND challenge_id = ?1",
            &c.challenge_id
        ),
        Some("alice cred-alice approval-listener".into())
    );
    // An identical retry after an unknown commit replays the same session; another key or
    // another page session never yields one.
    let again = m.finish(&op, &c.challenge_id, &ok(&alice), &k).unwrap();
    assert!(again.replayed);
    assert_eq!(again.result, out.result);
    assert_eq!(
        m.finish(&op, &c.challenge_id, &ok(&alice), "another-key"),
        Err(challenge(ChallengeRefusal::BindingMismatch))
    );
    assert!(m
        .finish(
            &login_op(&alice, "page-2"),
            &c.challenge_id,
            &ok(&alice),
            &k
        )
        .is_err());
    assert_eq!(
        m.count("SELECT count(*) FROM approval_events WHERE kind = 'read_session'"),
        1
    );
}

#[test]
fn z1_login_refusals_burn_or_keep_the_challenge_as_every_ceremony() {
    let m = M4::new("z1-refusals");
    let alice = human("alice");
    // Another acting subject than the one named, an unknown subject, an empty page session.
    assert_eq!(
        m.begin(&root(), &login_op(&alice, "p"), &key("k")),
        Err(challenge(ChallengeRefusal::SubjectMismatch))
    );
    let nobody = human("nobody");
    assert_eq!(
        m.begin(&nobody, &login_op(&nobody, "p"), &key("k")),
        Err(ap(ApprovalCode::PermissionMissing))
    );
    assert_eq!(
        m.begin(&alice, &login_op(&alice, ""), &key("k")),
        Err(challenge(ChallengeRefusal::Malformed))
    );
    // A forged assertion is a terminal refusal: the challenge is burnt (X4).
    let op = login_op(&alice, "p");
    let k = key("k");
    let c = m.begin(&alice, &op, &k).unwrap();
    assert!(matches!(
        m.finish(&op, &c.challenge_id, "forged:cred-alice", &k),
        Err(Refusal::Approval(ApprovalCode::AssertionInvalid(_)))
    ));
    assert!(m.challenge_status(&c.challenge_id).starts_with("refused "));
    assert_eq!(
        m.finish(&op, &c.challenge_id, &ok(&alice), &k),
        Err(challenge(ChallengeRefusal::Consumed))
    );
    // Without user verification: refused.
    let c = m.begin(&alice, &op, &k).unwrap();
    assert!(m
        .finish(&op, &c.challenge_id, "nouv:cred-alice", &k)
        .is_err());
    // L6: an unavailable verifier burns nothing; the same challenge then succeeds once.
    let c = m.begin(&alice, &op, &k).unwrap();
    assert_eq!(
        m.finish(&op, &c.challenge_id, "down:cred-alice", &k),
        Err(ap(ApprovalCode::AssertionInvalid(
            VerifierError::Unavailable
        )))
    );
    assert_eq!(m.challenge_status(&c.challenge_id), "issued ");
    assert!(m.finish(&op, &c.challenge_id, &ok(&alice), &k).is_ok());
    // A revoked subject cannot log in.
    m.enroll(person("rita", &[(Permission::Approver, "*", "*")], vec![]))
        .unwrap();
    m.revoke(
        &root(),
        RevocationTarget::Subject(human("rita")),
        &key("revoke"),
    )
    .unwrap();
    assert_eq!(
        m.begin(&human("rita"), &login_op(&human("rita"), "p"), &key("k")),
        Err(ap(ApprovalCode::SubjectRevoked))
    );
}

#[test]
fn z1_purpose_separation_between_login_and_mutating_challenges() {
    let m = M4::new("z1-purpose");
    let alice = human("alice");
    let id = m.ask("purpose", 1);
    let approve = CeremonyOp::Approve {
        request_id: id.clone(),
    };
    // A login challenge is never accepted for a mutation: refused and burnt; nothing
    // decided.
    let login_challenge = login_op(&alice, "p");
    let k = key("k");
    let c = m.begin(&alice, &login_challenge, &k).unwrap();
    assert!(matches!(
        m.finish(&approve, &c.challenge_id, &ok(&alice), &k),
        Err(Refusal::Approval(ApprovalCode::ChallengeInvalid(_)))
    ));
    assert!(m.challenge_status(&c.challenge_id).starts_with("refused "));
    assert_eq!(m.status(&id), ApprovalStatus::Pending);
    // A mutating challenge is never accepted for a login: refused and burnt; no session.
    let k = key("k");
    let c = m.begin(&alice, &approve, &k).unwrap();
    assert!(matches!(
        m.finish(&login_challenge, &c.challenge_id, &ok(&alice), &k),
        Err(Refusal::Approval(ApprovalCode::ChallengeInvalid(_)))
    ));
    assert!(m.challenge_status(&c.challenge_id).starts_with("refused "));
    assert_eq!(
        m.count("SELECT count(*) FROM approval_events WHERE kind = 'read_session'"),
        0
    );
    // A consumed login challenge is never a challenge for a mutation either.
    let s = login(&m, &alice, "p");
    assert!(matches!(
        m.finish(&approve, s.session_id(), &ok(&alice), "x"),
        Err(Refusal::Approval(ApprovalCode::ChallengeInvalid(_)))
    ));
    assert_eq!(m.status(&id), ApprovalStatus::Pending);
    // A login is never a readable target.
    assert_eq!(
        read(&m, &s, ceremony(login_op(&alice, "p"))),
        Err(ReadRefused)
    );
    // The approval still needs its own fresh ceremony, which succeeds.
    assert!(m.ceremony(&alice, &approve, &key("approve")).is_ok());
    assert_eq!(m.status(&id), ApprovalStatus::Approved);
}

#[test]
fn z3_reads_are_scoped_to_the_permission_of_the_target() {
    let m = M4::new("z3-scope");
    let id = m.ask("scope", 1);
    m.enroll(person(
        "pat",
        &[(Permission::Approver, "fixture-tenant", "other-project")],
        vec![],
    ))
    .unwrap();
    m.enroll(person(
        "tess",
        &[(Permission::Approver, "fixture-tenant", "*")],
        vec![],
    ))
    .unwrap();
    let alice = login(&m, &human("alice"), "a");
    let tess = login(&m, &human("tess"), "t");
    let pat = login(&m, &human("pat"), "p");
    let admin = login(&m, &root(), "r");
    let carol = login(&m, &human("carol"), "c");
    // Approvers read approval requests in scope (exact or containing).
    let Ok(RecordView::ApprovalRequest { request, status }) = read(&m, &alice, req(&id)) else {
        panic!("alice reads the request in her scope")
    };
    assert_eq!(
        (request.request_id(), status),
        (id.as_str(), ApprovalStatus::Pending)
    );
    assert!(read(&m, &tess, req(&id)).is_ok());
    // Another project, an operator, a revoker: the one refusal.
    for s in [&pat, &admin, &carol] {
        assert_eq!(read(&m, s, req(&id)), Err(ReadRefused));
    }
    // Guessed and unknown ids: the same refusal.
    for guess in ["", "req-1", "approval/x", &format!("{id}x")] {
        assert_eq!(read(&m, &alice, req(guess)), Err(ReadRefused), "{guess}");
    }
    // The approve context: approvers in scope only.
    let approve = ceremony(CeremonyOp::Approve {
        request_id: id.clone(),
    });
    assert!(read(&m, &alice, approve.clone()).is_ok());
    assert_eq!(read(&m, &pat, approve), Err(ReadRefused));
    // Revoke targets: revokers over a containing scope. A grant in (T, P): carol and root.
    let (_, grant) = m.approved("scope-grant", 2);
    let grant = revoke(RevocationTarget::Grant(grant.as_str().to_string()));
    assert!(read(&m, &carol, grant.clone()).is_ok());
    assert!(read(&m, &admin, grant.clone()).is_ok());
    assert_eq!(read(&m, &pat, grant), Err(ReadRefused));
    // A subject or credential target is global: only the global revoker.
    let subject = revoke(RevocationTarget::Subject(human("pat")));
    assert!(read(&m, &admin, subject.clone()).is_ok());
    assert_eq!(read(&m, &carol, subject), Err(ReadRefused));
    let credential = revoke(RevocationTarget::Credential("cred-pat".into()));
    assert!(read(&m, &admin, credential.clone()).is_ok());
    assert_eq!(read(&m, &alice, credential), Err(ReadRefused));
    // An unknown revocation target: the same refusal.
    assert_eq!(
        read(
            &m,
            &admin,
            revoke(RevocationTarget::Grant("approval/none".into()))
        ),
        Err(ReadRefused)
    );
    // Role and enrollment targets: operators only.
    let role = ceremony(CeremonyOp::RoleChange {
        change: RoleChange::Grant {
            subject: human("pat"),
            permission: Permission::Revoker,
            tenant_id: "fixture-tenant".into(),
            project_id: "fixture-project".into(),
        },
    });
    assert!(read(&m, &admin, role.clone()).is_ok());
    for s in [&alice, &carol, &tess] {
        assert_eq!(read(&m, s, role.clone()), Err(ReadRefused));
    }
    let start = ceremony(start_op(&human("newbie"), &root(), "page-r"));
    assert!(read(&m, &admin, start.clone()).is_ok());
    assert_eq!(read(&m, &alice, start), Err(ReadRefused));
    // A registration start naming another operator than the reader: refused.
    let other = ceremony(start_op(&human("newbie"), &human("alice"), "page-a"));
    assert_eq!(read(&m, &admin, other), Err(ReadRefused));
    // R6: the context read is exactly the bound one.
    let Ok(RecordView::Ceremony { context, .. }) = read(&m, &admin, role) else {
        panic!("operator reads the role context")
    };
    assert_eq!(
        writ_broker::approval::context_fingerprint(&context.context).unwrap(),
        context.binding.context_fingerprint
    );
    // Nothing a read did is written.
    assert_eq!(
        m.count("SELECT count(*) FROM approval_events WHERE kind = 'read_session'"),
        5
    );
}

#[test]
fn z3_a_session_is_only_what_the_ledger_recorded() {
    let m = M4::new("z3-forged");
    let id = m.ask("forged", 1);
    let alice = login(&m, &human("alice"), "a");
    assert!(read(&m, &alice, req(&id)).is_ok());
    let cap = LedgerCapability::for_ledger_implementation();
    let forge = |sid: &str, who: &SubjectId, cred: &str, page: &str, at| {
        ReadSession::from_ledger(&cap, sid, who.clone(), cred, page, at)
    };
    let at = alice.issued_at();
    // Every field must equal the recorded login.
    for s in [
        forge(
            alice.session_id(),
            &human("alice"),
            "cred-alice",
            "other-page",
            at,
        ),
        forge(alice.session_id(), &human("alice"), "cred-carol", "a", at),
        forge(alice.session_id(), &human("carol"), "cred-alice", "a", at),
        forge(
            alice.session_id(),
            &human("alice"),
            "cred-alice",
            "a",
            at.plus_seconds(1),
        ),
        forge("chl-unknown", &human("alice"), "cred-alice", "a", at),
    ] {
        assert_eq!(read(&m, &s, req(&id)), Err(ReadRefused), "{s:?}");
    }
    // The id of a consumed mutating challenge is never a session.
    let k = key("approve");
    let op = CeremonyOp::Approve {
        request_id: m.ask("forged-2", 2),
    };
    let c = m.begin(&human("alice"), &op, &k).unwrap();
    m.finish(&op, &c.challenge_id, &ok(&human("alice")), &k)
        .unwrap();
    let s = forge(&c.challenge_id, &human("alice"), "cred-alice", "a", at);
    assert_eq!(read(&m, &s, req(&id)), Err(ReadRefused));
    // An issued, unfinished login is not a session.
    let k = key("login");
    let pending = m
        .begin(&human("alice"), &login_op(&human("alice"), "z"), &k)
        .unwrap();
    let s = forge(
        &pending.challenge_id,
        &human("alice"),
        "cred-alice",
        "z",
        at,
    );
    assert_eq!(read(&m, &s, req(&id)), Err(ReadRefused));
}

#[test]
fn z2_sessions_expire_absolutely_and_revocation_invalidates_open_sessions() {
    let mut m = M4::new("z2-expiry");
    let id = m.ask("expiry", 1);
    for name in ["cred-holder", "subject-holder", "role-holder"] {
        m.enroll(person(name, &[(Permission::Approver, "*", "*")], vec![]))
            .unwrap();
    }
    let alice = login(&m, &human("alice"), "a");
    let cred = login(&m, &human("cred-holder"), "c");
    let subject = login(&m, &human("subject-holder"), "s");
    let role = login(&m, &human("role-holder"), "r");
    for s in [&alice, &cred, &subject, &role] {
        assert!(read(&m, s, req(&id)).is_ok());
    }
    // Revoking the credential, the subject or the role of an open session invalidates it
    // at the next read; the others are unaffected.
    m.revoke(
        &root(),
        RevocationTarget::Credential("cred-cred-holder".into()),
        &key("r"),
    )
    .unwrap();
    m.revoke(
        &root(),
        RevocationTarget::Subject(human("subject-holder")),
        &key("r"),
    )
    .unwrap();
    let assignment = m
        .text(
            "SELECT assignment_id FROM role_assignments WHERE subject = ?1",
            "role-holder",
        )
        .unwrap();
    m.ceremony(
        &root(),
        &CeremonyOp::RoleChange {
            change: RoleChange::Revoke {
                assignment_id: assignment,
            },
        },
        &key("role"),
    )
    .unwrap();
    for s in [&cred, &subject, &role] {
        assert_eq!(read(&m, s, req(&id)), Err(ReadRefused), "{s:?}");
    }
    assert!(read(&m, &alice, req(&id)).is_ok());
    // Absolute expiry at issued_at + 900 s on trusted time: one second before is valid, the
    // boundary refuses, and a later read never revives it.
    m.w.clock.set(alice.expires_at().plus_seconds(-1));
    assert!(read(&m, &alice, req(&id)).is_ok());
    m.w.clock.set(alice.expires_at());
    assert_eq!(read(&m, &alice, req(&id)), Err(ReadRefused));
    // A new login opens a new session (the request itself expired meanwhile, but stays
    // readable as a stored record).
    let fresh = login(&m, &human("alice"), "a2");
    assert_ne!(fresh.session_id(), alice.session_id());
    assert!(read(&m, &fresh, req(&id)).is_ok());
    // A session opened under one channel is not valid under another relying party.
    let other = Arc::new(TestVerifier::default());
    other.other_origin.store(true, Ordering::SeqCst);
    m.reopen_with(&other);
    assert_eq!(read(&m, &fresh, req(&id)), Err(ReadRefused));
}

#[test]
fn low3_operator_forms_need_the_live_permission_over_some_scope() {
    let m = M4::new("low3-forms");
    let form = |p| ReadTarget::Form { permission: p };
    let root_s = login(&m, &root(), "r");
    let carol = login(&m, &human("carol"), "c");
    let alice = login(&m, &human("alice"), "a");
    // root: operator and revoker (*, *); carol: revoker (T, P); alice: approver only.
    assert_eq!(
        read(&m, &root_s, form(Permission::Operator)),
        Ok(RecordView::Permitted)
    );
    assert_eq!(
        read(&m, &root_s, form(Permission::Revoker)),
        Ok(RecordView::Permitted)
    );
    assert_eq!(
        read(&m, &carol, form(Permission::Revoker)),
        Ok(RecordView::Permitted)
    );
    assert_eq!(
        read(&m, &carol, form(Permission::Operator)),
        Err(ReadRefused)
    );
    for p in [Permission::Operator, Permission::Revoker] {
        assert_eq!(read(&m, &alice, form(p)), Err(ReadRefused));
    }
    // Revoking carol's role, then an open session of a revoked subject: refused.
    let assignment = m
        .text(
            "SELECT assignment_id FROM role_assignments WHERE subject = ?1",
            "carol",
        )
        .unwrap();
    m.ceremony(
        &root(),
        &CeremonyOp::RoleChange {
            change: RoleChange::Revoke {
                assignment_id: assignment,
            },
        },
        &key("role"),
    )
    .unwrap();
    assert_eq!(
        read(&m, &carol, form(Permission::Revoker)),
        Err(ReadRefused)
    );
    m.enroll(person("otto", &[(Permission::Operator, "*", "*")], vec![]))
        .unwrap();
    let otto = login(&m, &human("otto"), "o");
    assert_eq!(
        read(&m, &otto, form(Permission::Operator)),
        Ok(RecordView::Permitted)
    );
    m.revoke(&root(), RevocationTarget::Subject(human("otto")), &key("r"))
        .unwrap();
    assert_eq!(
        read(&m, &otto, form(Permission::Operator)),
        Err(ReadRefused)
    );
}
