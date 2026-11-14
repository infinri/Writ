//! D-042 remediation regressions on the SQLite ledger (findings S4, S6, S7, S8; remediation
//! design X2..X6 in docs/implementation/M4-INTERFACES.md): grants bound to the exact approved
//! agent, replay identity, refusals burning their challenge (fail closed when the burn cannot
//! commit), wildcard role scopes and the authenticated role change, identity revocation
//! against issuance, T-reserve and T-dispatch (O-5), and ordering rows for expiry and role
//! changes. Finding S1/S2 (grant minting) is covered by `grant_tests` and the external-crate
//! test `tests/grant_authority.rs`.

#![allow(clippy::unwrap_used, clippy::expect_used, clippy::panic)]

use writ_broker::approval::{ApprovalCode, ApprovalStatus, ChallengeRefusal, RevocationTarget};
use writ_broker::approval_store::{
    ApprovalLedger, CeremonyFinish, CeremonyOp, CeremonyResult, NewEnrollment, RoleChange, UseState,
};
use writ_broker::execute::Executed;
use writ_broker::grants::{GrantLabel, Replay, ReserveOutcome};
use writ_broker::human::{Permission, PrincipalKind, SubjectId, VerifierError};
use writ_broker::refusal::Refusal;
use writ_broker::writ_kernel::json::{self, Value};

use super::fault;
use super::grant_tests::{now, revision, set, uses, verify};
use super::m4_approval_tests::{action, ap, challenge, digest_text, human, ok, person, root, M4};

const T: &str = "fixture-tenant";
const P: &str = "fixture-project";
const ADAPTER: &str = "writ-host-adapter:fixture";

/// Action `n` asked by agent `agent` (or the principal itself) under key `idem`.
fn by_agent(idem: &str, n: u32, agent: Option<&str>) -> Value {
    set(
        &action(idem, n),
        &["agent_id"],
        agent.map_or(Value::Null, json::s),
    )
}

fn approve_by(m: &M4, who: &SubjectId, request_id: &str, key: &str) -> GrantLabel {
    let out = m
        .ceremony(
            who,
            &CeremonyOp::Approve {
                request_id: request_id.to_string(),
            },
            key,
        )
        .unwrap();
    match out.result {
        CeremonyResult::Approved { grant } => grant,
        other => panic!("{other:?}"),
    }
}

fn role_of(m: &M4, name: &str, permission: &str) -> String {
    m.l()
        .db()
        .conn
        .query_row(
            "SELECT assignment_id FROM role_assignments WHERE subject = ?1 AND permission = ?2 \
             AND revoked_at IS NULL",
            [name, permission],
            |r| r.get(0),
        )
        .unwrap()
}

// ---------------------------------------------------------------------------
// X2 (S8): grant bound to the exact agent.

#[test]
fn x2_an_approved_grant_is_usable_only_by_the_exact_agent_of_its_request() {
    let m = M4::new("x2-agent");
    // Agent a of the principal asks; the request records agent a.
    let e = m.exec(&by_agent("k1", 1, Some("agent-a")), None);
    assert_eq!(e.result, Err(Refusal::ApprovalRequired));
    let id = m
        .text(
            "SELECT request_id FROM approval_requests WHERE instr(context, ?1) > 0",
            &digest_text(1),
        )
        .unwrap();
    let (request, _) = m.l().approval_request(&id).unwrap().unwrap();
    assert_eq!(
        request.fields().agent,
        Some(SubjectId::new(ADAPTER, "agent-a"))
    );
    let grant = approve_by(&m, &human("alice"), &id, "a1");
    // Another agent of the same principal, and the principal without an agent, share the
    // request id (the kernel's id omits the agent) and the binding (principal only), but
    // never the grant: refused before any use, at T-reserve.
    for (idem, agent) in [("k2", Some("agent-b")), ("k3", None)] {
        let e = m.exec(&by_agent(idem, 1, agent), Some(&grant));
        assert_eq!(e.result, Err(Refusal::GrantAgentMismatch), "{agent:?}");
        assert_eq!(e.result.unwrap_err().code(), "GRANT_MISMATCH");
        assert_eq!(uses(m.l(), grant.as_str()), Some(0), "{agent:?}");
        assert_eq!(m.receipt(idem), None, "{agent:?}");
    }
    assert_eq!(
        m.count("SELECT count(*) FROM audit_refusals WHERE code = 'GRANT_MISMATCH'"),
        2
    );
    // The approved agent uses it, once.
    assert_eq!(
        m.exec(&by_agent("k4", 1, Some("agent-a")), Some(&grant))
            .result,
        Ok(Executed::EffectCompleted)
    );
    assert_eq!(uses(m.l(), grant.as_str()), Some(1));
    // A grant approved for the principal itself (no agent) is not usable by its agents.
    let e = m.exec(&by_agent("k5", 2, None), None);
    assert_eq!(e.result, Err(Refusal::ApprovalRequired));
    let id2 = m
        .text(
            "SELECT request_id FROM approval_requests WHERE instr(context, ?1) > 0",
            &digest_text(2),
        )
        .unwrap();
    let grant2 = approve_by(&m, &human("alice"), &id2, "a2");
    assert_eq!(
        m.exec(&by_agent("k6", 2, Some("agent-a")), Some(&grant2))
            .result,
        Err(Refusal::GrantAgentMismatch)
    );
    assert_eq!(
        m.exec(&by_agent("k7", 2, None), Some(&grant2)).result,
        Ok(Executed::EffectCompleted)
    );
}

// ---------------------------------------------------------------------------
// X3 (S4): replay identity.

#[test]
fn x3_a_retry_for_another_target_on_a_consumed_challenge_never_replays() {
    let m = M4::new("x3-target");
    let alice = human("alice");
    let a = m.ask("k1", 1);
    let b = m.ask("k2", 2);
    let op_a = CeremonyOp::Approve {
        request_id: a.clone(),
    };
    let op_b = CeremonyOp::Approve {
        request_id: b.clone(),
    };
    let c = m.begin(&alice, &op_a, "x1").unwrap();
    let first = m.finish(&op_a, &c.challenge_id, &ok(&alice), "x1").unwrap();
    // Same operation, verdict, key, subject, credential and ceremony, another target: before
    // D-042 this replayed request a's result for request b.
    assert_eq!(
        m.finish(&op_b, &c.challenge_id, &ok(&alice), "x1"),
        Err(challenge(ChallengeRefusal::BindingMismatch))
    );
    assert_eq!(m.status(&b), ApprovalStatus::Pending);
    // A denial (another operation and verdict) on it is also a mismatch.
    assert_eq!(
        m.finish(
            &CeremonyOp::Deny {
                request_id: a.clone()
            },
            &c.challenge_id,
            &ok(&alice),
            "x1"
        ),
        Err(challenge(ChallengeRefusal::BindingMismatch))
    );
    // The identical retry still replays the committed decision, with no second grant.
    let again = m.finish(&op_a, &c.challenge_id, &ok(&alice), "x1").unwrap();
    assert!(again.replayed);
    assert_eq!(again.result, first.result);
    assert_eq!(m.count("SELECT count(*) FROM grants"), 1);
    assert_eq!(m.challenge_status(&c.challenge_id), "consumed ");
}

// ---------------------------------------------------------------------------
// X4 (S6): a refusal burns the challenge.

#[test]
fn x4_terminal_refusals_burn_the_challenge_and_a_lost_burn_fails_closed() {
    let m = M4::new("x4-burn");
    let alice = human("alice");
    let id = m.ask("k1", 1);
    let op = CeremonyOp::Approve {
        request_id: id.clone(),
    };
    // The burn cannot commit: the answer is the storage failure, never the refusal or a
    // decision, and nothing is decided.
    let c = m.begin(&alice, &op, "x1").unwrap();
    m.l().inject(
        "ceremony",
        fault::Stage::Commit,
        rusqlite::ffi::SQLITE_IOERR,
    );
    let lost = m.l().finish_ceremony(&CeremonyFinish {
        op: &op,
        challenge_id: &c.challenge_id,
        response: b"bad:cred-alice",
        idempotency_key: "x1",
        channel: "approval-listener",
        verifier: &m.v,
        now: m.now(),
    });
    let failure = lost.unwrap_err();
    assert!(failure.outcome_unknown, "{failure:?}");
    assert_eq!(m.status(&id), ApprovalStatus::Pending);
    // The next transaction reads the stored status: here the failed commit stored nothing,
    // so the challenge is still issued; the repeated refusal burns it.
    assert_eq!(m.challenge_status(&c.challenge_id), "issued ");
    assert_eq!(
        m.finish(&op, &c.challenge_id, "bad:cred-alice", "x1"),
        Err(ap(ApprovalCode::AssertionInvalid(
            VerifierError::SignatureInvalid
        )))
    );
    assert_eq!(
        m.challenge_status(&c.challenge_id),
        "refused ASSERTION_INVALID"
    );
    assert_eq!(
        m.finish(&op, &c.challenge_id, &ok(&alice), "x1"),
        Err(challenge(ChallengeRefusal::Consumed))
    );
    // A closed target burns the challenge that named it.
    let early = m.begin(&alice, &op, "x2").unwrap();
    let c = m.begin(&alice, &op, "x3").unwrap();
    m.finish(&op, &c.challenge_id, &ok(&alice), "x3").unwrap();
    assert_eq!(
        m.finish(&op, &early.challenge_id, &ok(&alice), "x2"),
        Err(ap(ApprovalCode::RequestClosed))
    );
    assert_eq!(
        m.challenge_status(&early.challenge_id),
        "refused APPROVAL_REQUEST_CLOSED"
    );
    // A permission refusal (the role revoked in between) burns it as well.
    let id2 = m.ask("k2", 2);
    let op2 = CeremonyOp::Approve {
        request_id: id2.clone(),
    };
    let c2 = m.begin(&alice, &op2, "x4").unwrap();
    let role = role_of(&m, "alice", "approver");
    m.revoke(&root(), RevocationTarget::Role(role), "r1")
        .unwrap();
    assert_eq!(
        m.finish(&op2, &c2.challenge_id, &ok(&alice), "x4"),
        Err(ap(ApprovalCode::PermissionMissing))
    );
    assert_eq!(
        m.challenge_status(&c2.challenge_id),
        "refused PERMISSION_MISSING"
    );
    assert_eq!(m.status(&id2), ApprovalStatus::Pending);
    assert_eq!(m.count("SELECT count(*) FROM grants"), 1);
}

// ---------------------------------------------------------------------------
// X5 (S7): wildcard scopes and the authenticated role change.

fn change(m: &M4, who: &SubjectId, change: RoleChange, key: &str) -> Result<String, Refusal> {
    match m
        .ceremony(who, &CeremonyOp::RoleChange { change }, key)?
        .result
    {
        CeremonyResult::RoleChanged { assignment_id } => Ok(assignment_id),
        other => panic!("{other:?}"),
    }
}

fn grant_role(name: &str, permission: Permission, tenant: &str, project: &str) -> RoleChange {
    RoleChange::Grant {
        subject: human(name),
        permission,
        tenant_id: tenant.into(),
        project_id: project.into(),
    }
}

#[test]
fn x5_role_changes_need_an_operator_over_a_containing_scope_and_never_self() {
    let m = M4::new("x5-roles");
    m.enroll(person("dave", &[], vec![])).unwrap();
    // The bootstrap operator holds (*, *): it grants dave approver in (T, P).
    let op = CeremonyOp::RoleChange {
        change: grant_role("dave", Permission::Approver, T, P),
    };
    let c = m.begin(&root(), &op, "rc1").unwrap();
    let out = m.finish(&op, &c.challenge_id, &ok(&root()), "rc1").unwrap();
    let CeremonyResult::RoleChanged { assignment_id } = out.result.clone() else {
        panic!()
    };
    assert_eq!(assignment_id, format!("role:{}", c.challenge_id));
    // Provenance and ordering in the same transaction; an identical retry replays.
    assert_eq!(
        m.count(
            "SELECT count(*) FROM approval_events e JOIN ledger_sequence s \
             ON s.seq = e.ledger_seq WHERE e.kind = 'role_changed' \
             AND s.kind = 'administration' AND e.actor_subject = 'root'"
        ),
        1
    );
    let again = m.finish(&op, &c.challenge_id, &ok(&root()), "rc1").unwrap();
    assert!(again.replayed);
    assert_eq!(
        (again.result, again.ledger_seq),
        (out.result, out.ledger_seq)
    );
    assert_eq!(
        m.count("SELECT count(*) FROM role_assignments WHERE subject = 'dave'"),
        1
    );
    // Dave now approves in (T, P).
    let id = m.ask("k1", 1);
    let g = approve_by(&m, &human("dave"), &id, "a1");
    // An operator over (T, *) changes roles inside tenant T only; never its own.
    m.enroll(person("opal", &[(Permission::Operator, T, "*")], vec![]))
        .unwrap();
    m.enroll(person("erin", &[], vec![])).unwrap();
    assert!(change(
        &m,
        &human("opal"),
        grant_role("erin", Permission::Approver, T, P),
        "rc2"
    )
    .is_ok());
    assert_eq!(
        m.begin(
            &human("opal"),
            &CeremonyOp::RoleChange {
                change: grant_role("erin", Permission::Approver, "other-tenant", P)
            },
            "rc3"
        )
        .err(),
        Some(ap(ApprovalCode::PermissionMissing))
    );
    assert_eq!(
        m.begin(
            &human("opal"),
            &CeremonyOp::RoleChange {
                change: grant_role("erin", Permission::Approver, "*", "*")
            },
            "rc4"
        )
        .err(),
        Some(ap(ApprovalCode::PermissionMissing)),
        "a narrower assignment never matches a wildcard target"
    );
    assert_eq!(
        change(
            &m,
            &human("opal"),
            grant_role("opal", Permission::Approver, T, P),
            "rc5"
        ),
        Err(ap(ApprovalCode::SeparationViolated))
    );
    // A service never receives approver or signoff authority (O-8).
    m.enroll(NewEnrollment {
        subject: human("svc-ci"),
        kind: PrincipalKind::Service,
        credential: None,
        linked_identities: vec![],
        roles: vec![],
    })
    .unwrap();
    for p in [Permission::Approver, Permission::SignoffAuthority] {
        assert_eq!(
            m.begin(
                &root(),
                &CeremonyOp::RoleChange {
                    change: grant_role("svc-ci", p, T, P)
                },
                "rc6"
            )
            .err(),
            Some(ap(ApprovalCode::ServiceNotPermitted)),
            "{p:?}"
        );
    }
    assert!(change(
        &m,
        &root(),
        grant_role("svc-ci", Permission::Operator, T, P),
        "rc7"
    )
    .is_ok());
    // Revoking dave's approver assignment by role change invalidates his unused grant.
    let dave_role = role_of(&m, "dave", "approver");
    change(
        &m,
        &human("opal"),
        RoleChange::Revoke {
            assignment_id: dave_role.clone(),
        },
        "rc8",
    )
    .unwrap();
    assert_eq!(
        m.exec(&action("k1", 1), Some(&g)).result,
        Err(ap(ApprovalCode::GrantRevoked))
    );
    assert_eq!(
        m.count(
            "SELECT count(*) FROM revocations WHERE target_kind = 'role' \
             AND use_state = 'not_applicable' AND reason = 'role change'"
        ),
        1
    );
    assert_eq!(
        m.begin(
            &root(),
            &CeremonyOp::RoleChange {
                change: RoleChange::Revoke {
                    assignment_id: dave_role
                }
            },
            "rc9"
        )
        .err(),
        Some(ap(ApprovalCode::PermissionMissing)),
        "already revoked"
    );
    assert_eq!(
        m.count("SELECT count(*) FROM ledger_sequence WHERE kind = 'administration'"),
        4
    );
}

#[test]
fn x5_a_global_revoker_revokes_a_scoped_grant() {
    let m = M4::new("x5-global-revoker");
    let (_, g) = m.approved("k1", 1);
    // S7: the bootstrap administrator's (*, *) revoker scope contains the request's scope.
    let out = m
        .revoke(&root(), RevocationTarget::Grant(g.as_str().into()), "r1")
        .unwrap();
    assert_eq!(
        out.result,
        CeremonyResult::Revoked {
            use_state: UseState::Unused
        }
    );
    assert_eq!(
        m.exec(&action("k1", 1), Some(&g)).result,
        Err(ap(ApprovalCode::GrantRevoked))
    );
}

// ---------------------------------------------------------------------------
// O-5: credential, subject and role revocation against issuance, T-reserve and T-dispatch.

#[derive(Clone, Copy, Debug)]
enum Identity {
    Credential,
    Subject,
    Role,
}

fn revoke_identity(m: &M4, what: Identity, name: &str, key: &str) -> CeremonyResult {
    match what {
        Identity::Credential => {
            m.revoke(
                &root(),
                RevocationTarget::Credential(format!("cred-{name}")),
                key,
            )
            .unwrap()
            .result
        }
        Identity::Subject => {
            m.revoke(&root(), RevocationTarget::Subject(human(name)), key)
                .unwrap()
                .result
        }
        Identity::Role => {
            let id = role_of(m, name, "approver");
            m.ceremony(
                &root(),
                &CeremonyOp::RoleChange {
                    change: RoleChange::Revoke { assignment_id: id },
                },
                key,
            )
            .unwrap()
            .result
        }
    }
}

#[test]
fn o5_identity_revocation_is_rechecked_at_issuance_reserve_and_dispatch() {
    for what in [Identity::Credential, Identity::Subject, Identity::Role] {
        let m = M4::new(&format!("o5-{what:?}"));
        for name in ["d1", "d2", "d3", "d4"] {
            m.enroll(person(name, &[(Permission::Approver, T, P)], vec![]))
                .unwrap();
        }
        // Issuance: revoked between challenge and decision; the challenge is burned.
        let id1 = m.ask("k1", 1);
        let op1 = CeremonyOp::Approve {
            request_id: id1.clone(),
        };
        let c = m.begin(&human("d1"), &op1, "a1").unwrap();
        revoke_identity(&m, what, "d1", "r1");
        let refused = m.finish(&op1, &c.challenge_id, &ok(&human("d1")), "a1");
        let want = match what {
            Identity::Credential => ap(ApprovalCode::CredentialRevoked),
            Identity::Subject => ap(ApprovalCode::SubjectRevoked),
            Identity::Role => ap(ApprovalCode::PermissionMissing),
        };
        assert_eq!(refused, Err(want.clone()), "{what:?}");
        assert_eq!(
            m.challenge_status(&c.challenge_id),
            format!("refused {}", want.code())
        );
        assert_eq!(m.status(&id1), ApprovalStatus::Pending, "{what:?}");
        // Before T-reserve: refused, nothing used.
        let id2 = m.ask("k2", 2);
        let g2 = approve_by(&m, &human("d2"), &id2, "a2");
        revoke_identity(&m, what, "d2", "r2");
        assert_eq!(
            m.exec(&action("k2", 2), Some(&g2)).result,
            Err(ap(ApprovalCode::GrantRevoked)),
            "{what:?}"
        );
        assert_eq!(uses(m.l(), g2.as_str()), Some(0));
        assert_eq!(m.receipt("k2"), None);
        // After T-reserve, before T-dispatch: refused at T-dispatch, the reservation and its
        // grant use kept, no refund, no permit.
        let id3 = m.ask("k3", 3);
        let g3 = approve_by(&m, &human("d3"), &id3, "a3");
        assert_eq!(
            m.w.broker()
                .grants()
                .reserve(&verify(&action("k3", 3)), revision(), Some(&g3)),
            Ok(ReserveOutcome::Reserved)
        );
        revoke_identity(&m, what, "d3", "r3");
        let e = m.exec(&action("k3", 3), Some(&g3));
        assert_eq!(e.result, Err(ap(ApprovalCode::GrantRevoked)), "{what:?}");
        assert_eq!(e.permit, None);
        assert_eq!(m.receipt("k3").as_deref(), Some("reserved"));
        assert_eq!(uses(m.l(), g3.as_str()), Some(1), "no refund");
        // After T-dispatch: nothing undone, recorded accurately, durable replay kept.
        let id4 = m.ask("k4", 4);
        let g4 = approve_by(&m, &human("d4"), &id4, "a4");
        assert_eq!(
            m.exec(&action("k4", 4), Some(&g4)).result,
            Ok(Executed::EffectCompleted)
        );
        let recorded = revoke_identity(&m, what, "d4", "r4");
        match what {
            Identity::Role => assert!(matches!(recorded, CeremonyResult::RoleChanged { .. })),
            _ => assert_eq!(
                recorded,
                CeremonyResult::Revoked {
                    use_state: UseState::NotApplicable
                }
            ),
        }
        assert_eq!(m.receipt("k4").as_deref(), Some("observed_success"));
        assert_eq!(
            m.exec(&action("k4", 4), Some(&g4)).result,
            Ok(Executed::Replayed(Replay::EffectCompleted))
        );
        assert_eq!(uses(m.l(), g4.as_str()), Some(1));
        assert_eq!(
            m.count("SELECT count(*) FROM receipt_permits"),
            1,
            "{what:?}: only k4 dispatched"
        );
    }
}

// ---------------------------------------------------------------------------
// X6 (S8): ordering rows for expiry.

#[test]
fn x6_an_expiry_step_that_changes_anything_commits_one_ordering_row() {
    let m = M4::new("x6-expiry");
    let id = m.ask("k1", 1);
    let rows = |m: &M4| m.count("SELECT count(*) FROM ledger_sequence WHERE kind = 'expiry'");
    // Nothing due: no row.
    let e = m.l().expire(m.now()).unwrap();
    assert_eq!((e.requests, e.challenges), (0, 0));
    assert_eq!(rows(&m), 0);
    // A due request and a due challenge: one row for the step, referenced by the event.
    let _c = m
        .begin(
            &human("alice"),
            &CeremonyOp::Approve {
                request_id: id.clone(),
            },
            "x1",
        )
        .unwrap();
    m.w.clock.set(now().plus_seconds(600));
    let e = m.l().expire(m.now()).unwrap();
    assert_eq!((e.requests, e.challenges), (1, 1));
    assert_eq!(rows(&m), 1);
    assert_eq!(
        m.count(
            "SELECT count(*) FROM approval_events e JOIN ledger_sequence s \
             ON s.seq = e.ledger_seq WHERE e.kind = 'expired' AND s.kind = 'expiry'"
        ),
        1
    );
    assert_eq!(m.status(&id), ApprovalStatus::Expired);
}
