//! M3-03 tests on the SQLite ledger: approval issuance, atomic single-use grant
//! consumption in T-reserve, idempotency outcomes, C-4 stored-grant integrity,
//! concurrency (64 threads), policy activation racing consumption, storage fault
//! injection, trusted-time expiry and restart persistence.

#![allow(clippy::unwrap_used, clippy::expect_used, clippy::panic)]

use std::path::PathBuf;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{mpsc, Barrier, Mutex};
use std::thread;
use std::time::Duration;

use writ_broker::clock::{ClockError, Drift, EffectiveClock, Reading};
use writ_broker::grants::{
    Approval, ApprovalAuthority, ApprovalPresentation, ApproverRefusal, AuthenticatedApprover,
    GrantInsert, GrantIssued, GrantLabel, GrantService, NewGrant, PrincipalKind, Replay,
    ReserveOutcome, ReserveRequest, Verdict,
};
use writ_broker::ingress::{verify_ingress, Seal, VerifiedRequest};
use writ_broker::keys::{IngressKey, MemoryKeyRing};
use writ_broker::policy::{ActivePolicy, PolicyState};
use writ_broker::refusal::Refusal;
use writ_broker::store::{GrantLedger, LedgerStore, StoreError};
use writ_broker::writ_kernel::contracts::StateRevision;
use writ_broker::writ_kernel::json::{self, Value};
use writ_broker::writ_kernel::policy::{load_bundle, LoadedBundle};
use writ_broker::writ_kernel::time::Timestamp;

use super::{fault, SqliteLedger};

const ADAPTER: &str = "writ-host-adapter:fixture";
const UI: &str = "approval-ui";
const ALICE: &str = "human:alice";

pub(crate) fn ts(s: &str) -> Timestamp {
    Timestamp::parse_rfc3339(s).unwrap()
}

pub(crate) fn now() -> Timestamp {
    ts("2026-11-05T12:00:00Z")
}

pub(crate) fn revision() -> StateRevision {
    StateRevision::new(5).unwrap()
}

pub(crate) fn label(s: &str) -> GrantLabel {
    GrantLabel::new(s).unwrap()
}

fn tmp(name: &str) -> PathBuf {
    let d = std::env::temp_dir().join(format!("writ-grants-{}-{name}", std::process::id()));
    let _ = std::fs::remove_dir_all(&d);
    std::fs::create_dir_all(&d).unwrap();
    d.join("ledger.db")
}

// ---------------------------------------------------------------------------
// Requests, seals and bundles.

/// The M0 golden base request with `kind` on `resource` and idempotency key `idem`.
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

/// `repository.publish` to main: `require_approval` under the reference bundle.
pub(crate) fn publish(idem: &str) -> Value {
    request(
        "repository.publish",
        "git://main",
        "repository:fixture-tenant/fixture-project/main",
        idem,
    )
}

/// `file.write` in the workspace: `allow` under the reference bundle.
pub(crate) fn file_write(idem: &str) -> Value {
    request(
        "file.write",
        "file:///work/src/app.py",
        "workspace:fixture-tenant/fixture-project/src/app.py",
        idem,
    )
}

pub(crate) fn set(v: &Value, path: &[&str], value: Value) -> Value {
    let mut out = v.clone();
    let mut cur = &mut out;
    for (i, key) in path.iter().enumerate() {
        let Value::Obj(m) = cur else {
            panic!("not an object")
        };
        if i + 1 == path.len() {
            m.insert((*key).to_string(), value);
            return out;
        }
        cur = m.get_mut(*key).unwrap();
    }
    out
}

pub(crate) fn verify(req: &Value) -> VerifiedRequest {
    let key = || IngressKey::from_bytes(vec![0x11; 32]).unwrap();
    let mut ring = MemoryKeyRing::new();
    ring.register(ADAPTER, key());
    let seal = Seal::create(req, ADAPTER, &key()).unwrap().to_value();
    verify_ingress(req, Some(&seal), &ring).unwrap()
}

fn reference_value() -> Value {
    json::parse(include_bytes!(
        "../../../docs/contracts/fixtures/policy/m0-reference.bundle.json"
    ))
    .unwrap()
}

pub(crate) fn reference() -> LoadedBundle {
    load_bundle(&reference_value()).unwrap()
}

/// The reference bundle under another version: same statements, another digest.
pub(crate) fn reference_v2() -> LoadedBundle {
    load_bundle(&set(&reference_value(), &["version"], json::s("0.9.0"))).unwrap()
}

/// Publishing forbidden.
pub(crate) fn publish_forbidden() -> LoadedBundle {
    let statements = json::parse(
        br#"[{"statement_id":"freeze","effect":"forbid","action_kinds":["repository.publish"],"reason_code":"PUBLISH_FROZEN"}]"#,
    )
    .unwrap();
    let doc = set(&reference_value(), &["version"], json::s("0.8.0"));
    load_bundle(&set(&doc, &["statements"], statements)).unwrap()
}

// ---------------------------------------------------------------------------
// Trusted clock and approval authority.

/// An injected effective clock whose time the test moves.
pub(crate) struct TestClock(Mutex<Timestamp>);

impl TestClock {
    pub(crate) fn at(t: Timestamp) -> TestClock {
        TestClock(Mutex::new(t))
    }

    pub(crate) fn set(&self, t: Timestamp) {
        *self.0.lock().unwrap() = t;
    }
}

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

/// Trusted approval channels: (channel, principal, credential, kind).
pub(crate) struct Channels(Vec<(&'static str, &'static str, &'static [u8], PrincipalKind)>);

pub(crate) fn channels() -> Channels {
    Channels(vec![
        (UI, ALICE, b"cred-alice", PrincipalKind::Human),
        // The M1 fixture channel lists the requesting agent's id as a human
        // (b-26): the self-approval rule must still refuse it.
        (
            UI,
            "agent:claude-session-1",
            b"cred-agent",
            PrincipalKind::Human,
        ),
        (UI, "agent:sub-1", b"cred-sub", PrincipalKind::Human),
        (UI, "agent:helper", b"cred-helper", PrincipalKind::Agent),
        (UI, "model:assistant", b"cred-model", PrincipalKind::Model),
        (UI, "svc:ci", b"cred-ci", PrincipalKind::Service),
        (
            "ops-console",
            "human:bob",
            b"cred-bob",
            PrincipalKind::Human,
        ),
    ])
}

impl ApprovalAuthority for Channels {
    fn authenticate(
        &self,
        p: &ApprovalPresentation<'_>,
    ) -> Result<AuthenticatedApprover, ApproverRefusal> {
        if !self.0.iter().any(|e| e.0 == p.channel) {
            return Err(ApproverRefusal::UntrustedChannel);
        }
        self.0
            .iter()
            .find(|e| e.0 == p.channel && e.1 == p.approver && e.2 == p.credential)
            .map(|e| AuthenticatedApprover {
                id: e.1.to_string(),
                kind: e.3,
                channel: e.0.to_string(),
                issuer: "realm:fixture".to_string(),
                evidence_ref: format!("authn-event:{}:{}", e.0, e.1),
            })
            .ok_or(ApproverRefusal::Unauthenticated)
    }
}

/// An authority that answers for another channel than the one presented.
struct Confused;

impl ApprovalAuthority for Confused {
    fn authenticate(
        &self,
        _: &ApprovalPresentation<'_>,
    ) -> Result<AuthenticatedApprover, ApproverRefusal> {
        Ok(AuthenticatedApprover {
            id: ALICE.into(),
            kind: PrincipalKind::Human,
            channel: "ops-console".into(),
            issuer: "realm:fixture".into(),
            evidence_ref: "authn-event:confused".into(),
        })
    }
}

pub(crate) fn alice() -> ApprovalPresentation<'static> {
    ApprovalPresentation {
        channel: UI,
        approver: ALICE,
        credential: b"cred-alice",
    }
}

// ---------------------------------------------------------------------------
// Ledger inspection.

pub(crate) fn count(l: &SqliteLedger, sql: &str) -> i64 {
    l.db().conn.query_row(sql, [], |r| r.get(0)).unwrap()
}

pub(crate) fn uses(l: &SqliteLedger, g: &str) -> Option<i64> {
    l.db()
        .conn
        .query_row("SELECT uses FROM grants WHERE label = ?1", [g], |r| {
            r.get(0)
        })
        .ok()
}

fn grant_rows(l: &SqliteLedger) -> i64 {
    count(l, "SELECT count(*) FROM grants")
}

pub(crate) fn use_rows(l: &SqliteLedger) -> i64 {
    count(l, "SELECT count(*) FROM grant_uses")
}

pub(crate) fn receipt_rows(l: &SqliteLedger) -> i64 {
    count(l, "SELECT count(*) FROM receipts")
}

pub(crate) fn event_rows(l: &SqliteLedger) -> i64 {
    count(l, "SELECT count(*) FROM receipt_events")
}

pub(crate) fn audited(l: &SqliteLedger, code: &str) -> i64 {
    l.db()
        .conn
        .query_row(
            "SELECT count(*) FROM audit_refusals WHERE code = ?1",
            [code],
            |r| r.get(0),
        )
        .unwrap()
}

fn set_status(l: &SqliteLedger, idem: &str, status: &str) {
    l.db()
        .conn
        .execute(
            "UPDATE receipts SET status = ?1 WHERE idempotency_key = ?2",
            [status, idem],
        )
        .unwrap();
}

/// Nothing durable from a refused or failed consumption.
fn assert_untouched(l: &SqliteLedger, g: &str) {
    assert_eq!(uses(l, g), Some(0), "no grant use");
    assert_eq!(use_rows(l), 0, "no grant_uses row");
    assert_eq!(receipt_rows(l), 0, "no receipt");
    assert_eq!(event_rows(l), 0, "no receipt event");
}

// ---------------------------------------------------------------------------
// Service helpers.

pub(crate) type Svc<'a> = GrantService<'a, SqliteLedger>;

pub(crate) fn approve_as(
    svc: &Svc<'_>,
    req: &Value,
    g: &str,
    verdict: Verdict,
    ttl: Option<u32>,
    who: &ApprovalPresentation<'_>,
    authority: &dyn ApprovalAuthority,
) -> Result<GrantIssued, Refusal> {
    let verified = verify(req);
    let g = label(g);
    svc.approve(
        &Approval {
            request: &verified,
            state_revision: revision(),
            label: &g,
            verdict,
            ttl_seconds: ttl,
        },
        who,
        authority,
    )
}

pub(crate) fn approve(svc: &Svc<'_>, req: &Value, g: &str) -> Result<GrantIssued, Refusal> {
    approve_as(svc, req, g, Verdict::Approved, None, &alice(), &channels())
}

pub(crate) fn reserve(
    svc: &Svc<'_>,
    req: &Value,
    g: Option<&str>,
) -> Result<ReserveOutcome, Refusal> {
    let g = g.map(label);
    svc.reserve(&verify(req), revision(), g.as_ref())
}

/// A fresh ledger, the reference bundle active, the clock at `now()`.
struct World {
    ledger: SqliteLedger,
    active: ActivePolicy,
    clock: TestClock,
}

impl World {
    fn new(name: &str) -> World {
        World {
            ledger: SqliteLedger::create(&tmp(name)).unwrap(),
            active: ActivePolicy::new(PolicyState::loaded(reference())),
            clock: TestClock::at(now()),
        }
    }

    fn svc(&self) -> Svc<'_> {
        GrantService::new(&self.active, &self.clock, &self.ledger)
    }
}

fn reserve_unavailable(code: &str) -> Refusal {
    Refusal::LedgerUnavailable(StoreError::Unavailable {
        category: "transaction reserve".into(),
        code: code.into(),
    })
}

// ---------------------------------------------------------------------------
// Approval issuance.

#[test]
fn approval_issues_a_single_use_grant_bound_to_the_full_request_with_trusted_expiry() {
    let w = World::new("issue");
    let svc = w.svc();
    let issued = approve(&svc, &publish("k1"), "g1").unwrap();
    assert_eq!(issued.label, label("g1"));
    assert_eq!(issued.expires_at, now().plus_seconds(600), "default 600 s");
    // The stored binding is the canonical full binding with verdict `approved`.
    let row: (String, i64, i64, i64, i64) = w
        .ledger
        .db()
        .conn
        .query_row(
            "SELECT binding, expires_s, expires_ns, max_uses, uses FROM grants WHERE label = 'g1'",
            [],
            |r| Ok((r.get(0)?, r.get(1)?, r.get(2)?, r.get(3)?, r.get(4)?)),
        )
        .unwrap();
    assert_eq!(row.0, issued.binding);
    assert!(writ_broker::grants::binding_intact(&row.0));
    let binding = json::parse(row.0.as_bytes()).unwrap();
    for (member, want) in [
        ("verdict", "approved"),
        ("tenant_id", "fixture-tenant"),
        ("principal_id", "agent:claude-session-1"),
        ("action_kind", "repository.publish"),
        (
            "canonical_resource_id",
            "repository:fixture-tenant/fixture-project/main",
        ),
        ("policy_bundle_sha", reference().sha.as_str()),
    ] {
        assert_eq!(
            binding.get(member).and_then(Value::as_str),
            Some(want),
            "{member}"
        );
    }
    assert!(binding
        .get("approval_request_id")
        .and_then(Value::as_str)
        .is_some_and(|a| a.starts_with("apr:")));
    assert_eq!(
        (row.1, row.2, row.3, row.4),
        (now().plus_seconds(600).unix_seconds(), 0, 1, 0)
    );
    // The decision time was observed and persisted as the floor.
    assert_eq!(w.ledger.time_floor().unwrap(), Some(now()));
    // A given lifetime.
    let short = approve_as(
        &svc,
        &publish("k2"),
        "g2",
        Verdict::Approved,
        Some(30),
        &alice(),
        &channels(),
    )
    .unwrap();
    assert_eq!(short.expires_at, now().plus_seconds(30));
    assert_eq!(count(&w.ledger, "SELECT count(*) FROM audit_refusals"), 0);
}

#[test]
fn approval_without_an_authenticated_human_on_a_trusted_channel_is_refused() {
    let w = World::new("unauthenticated");
    let svc = w.svc();
    let trusted = channels();
    let cases: Vec<(
        ApprovalPresentation<'_>,
        &dyn ApprovalAuthority,
        ApproverRefusal,
    )> = vec![
        // Wrong credential.
        (
            ApprovalPresentation {
                channel: UI,
                approver: ALICE,
                credential: b"cred-bob",
            },
            &trusted,
            ApproverRefusal::Unauthenticated,
        ),
        // No credential.
        (
            ApprovalPresentation {
                channel: UI,
                approver: ALICE,
                credential: b"",
            },
            &trusted,
            ApproverRefusal::Unauthenticated,
        ),
        // Wrong channel: a credential valid on one trusted channel, presented on another.
        (
            ApprovalPresentation {
                channel: "ops-console",
                approver: ALICE,
                credential: b"cred-alice",
            },
            &trusted,
            ApproverRefusal::Unauthenticated,
        ),
        // A channel that is not a trusted approval channel (for example a transcript).
        (
            ApprovalPresentation {
                channel: "agent-transcript",
                approver: ALICE,
                credential: b"cred-alice",
            },
            &trusted,
            ApproverRefusal::UntrustedChannel,
        ),
        // An authority answering for another channel than the one presented.
        (alice(), &Confused, ApproverRefusal::ChannelMismatch),
    ];
    for (who, authority, why) in cases {
        let got = approve_as(
            &svc,
            &publish("k1"),
            "g1",
            Verdict::Approved,
            None,
            &who,
            authority,
        );
        assert_eq!(got, Err(Refusal::HumanUnauthenticated(why)), "{who:?}");
        assert_eq!(got.unwrap_err().external_code(), "PROMOTION_REFUSED");
    }
    assert_eq!(grant_rows(&w.ledger), 0);
    // Refused before any ledger access other than the audit: no time was observed.
    assert_eq!(w.ledger.time_floor().unwrap(), None);
    assert_eq!(audited(&w.ledger, "HUMAN_UNAUTHENTICATED"), 5);
}

#[test]
fn approval_by_an_agent_model_or_service_identity_is_never_accepted() {
    let w = World::new("not-human");
    let svc = w.svc();
    for (who, cred) in [
        ("agent:helper", &b"cred-helper"[..]),
        ("model:assistant", &b"cred-model"[..]),
        ("svc:ci", &b"cred-ci"[..]),
    ] {
        let p = ApprovalPresentation {
            channel: UI,
            approver: who,
            credential: cred,
        };
        assert_eq!(
            approve_as(
                &svc,
                &publish("k1"),
                "g1",
                Verdict::Approved,
                None,
                &p,
                &channels()
            ),
            Err(Refusal::HumanUnauthenticated(ApproverRefusal::NotHuman)),
            "{who}"
        );
    }
    assert_eq!(grant_rows(&w.ledger), 0);
}

#[test]
fn approval_of_a_request_that_does_not_require_approval_is_not_approvable() {
    let w = World::new("not-approvable");
    let svc = w.svc();
    // allow
    assert_eq!(
        approve(&svc, &file_write("k1"), "g1"),
        Err(Refusal::NotApprovable)
    );
    // forbid (hard deny never becomes approvable)
    let secrets = request(
        "file.write",
        "file:///work/secrets/k",
        "workspace:fixture-tenant/fixture-project/secrets/k",
        "k2",
    );
    assert_eq!(approve(&svc, &secrets, "g2"), Err(Refusal::NotApprovable));
    // invalid request
    let stale = set(&publish("k3"), &["expected_state_revision"], Value::Int(4));
    assert_eq!(approve(&svc, &stale, "g3"), Err(Refusal::NotApprovable));
    // policy unavailable or rejected
    for state in [PolicyState::Absent, PolicyState::Rejected] {
        w.active.activate(state);
        assert_eq!(
            approve(&svc, &publish("k4"), "g4"),
            Err(Refusal::NotApprovable)
        );
    }
    assert_eq!(grant_rows(&w.ledger), 0);
    assert_eq!(audited(&w.ledger, "NOT_APPROVABLE"), 5);
}

#[test]
fn self_approval_is_forbidden() {
    let w = World::new("self");
    let svc = w.svc();
    // The approver is the request's principal.
    let principal = ApprovalPresentation {
        channel: UI,
        approver: "agent:claude-session-1",
        credential: b"cred-agent",
    };
    assert_eq!(
        approve_as(
            &svc,
            &publish("k1"),
            "g1",
            Verdict::Approved,
            None,
            &principal,
            &channels()
        ),
        Err(Refusal::SelfApprovalForbidden)
    );
    // The approver is the request's agent.
    let sub = ApprovalPresentation {
        channel: UI,
        approver: "agent:sub-1",
        credential: b"cred-sub",
    };
    let by_sub = set(&publish("k2"), &["agent_id"], json::s("agent:sub-1"));
    assert_eq!(
        approve_as(
            &svc,
            &by_sub,
            "g2",
            Verdict::Approved,
            None,
            &sub,
            &channels()
        ),
        Err(Refusal::SelfApprovalForbidden)
    );
    // NOT_APPROVABLE is decided first.
    assert_eq!(
        approve_as(
            &svc,
            &file_write("k3"),
            "g3",
            Verdict::Approved,
            None,
            &principal,
            &channels()
        ),
        Err(Refusal::NotApprovable)
    );
    assert_eq!(grant_rows(&w.ledger), 0);
}

#[test]
fn a_reused_label_is_refused_and_never_resets_or_replenishes_the_grant() {
    let w = World::new("label-reuse");
    let svc = w.svc();
    let first = approve(&svc, &publish("k1"), "g1").unwrap();
    assert_eq!(
        reserve(&svc, &publish("k1"), Some("g1")),
        Ok(ReserveOutcome::Reserved)
    );
    assert_eq!(uses(&w.ledger, "g1"), Some(1));
    // Replayed approval, another request, another verdict, another lifetime: all refused.
    assert_eq!(
        approve(&svc, &publish("k1"), "g1"),
        Err(Refusal::GrantRefInUse)
    );
    assert_eq!(
        approve(&svc, &publish("k9"), "g1"),
        Err(Refusal::GrantRefInUse)
    );
    assert_eq!(
        approve_as(
            &svc,
            &publish("k1"),
            "g1",
            Verdict::Denied,
            Some(9),
            &alice(),
            &channels()
        ),
        Err(Refusal::GrantRefInUse)
    );
    let stored: (String, i64, i64) = w
        .ledger
        .db()
        .conn
        .query_row(
            "SELECT binding, expires_s, uses FROM grants WHERE label = 'g1'",
            [],
            |r| Ok((r.get(0)?, r.get(1)?, r.get(2)?)),
        )
        .unwrap();
    assert_eq!(
        stored,
        (first.binding, first.expires_at.unix_seconds(), 1),
        "never reset"
    );
    assert_eq!(
        reserve(&svc, &publish("k2"), Some("g1")),
        Err(Refusal::GrantExhausted)
    );
    assert_eq!(uses(&w.ledger, "g1"), Some(1));
    assert_eq!(use_rows(&w.ledger), 1);
    assert_eq!(grant_rows(&w.ledger), 1);
    assert_eq!(audited(&w.ledger, "GRANT_REF_IN_USE"), 3);
}

#[test]
fn injected_approve_transaction_faults_issue_no_grant() {
    for (i, stage) in [
        fault::Stage::Begin,
        fault::Stage::Body,
        fault::Stage::Commit,
    ]
    .into_iter()
    .enumerate()
    {
        let w = World::new(&format!("approve-fault-{i}"));
        let svc = w.svc();
        w.ledger
            .inject("approve", stage, rusqlite::ffi::SQLITE_IOERR);
        assert_eq!(
            approve(&svc, &publish("k1"), "g1"),
            Err(Refusal::LedgerUnavailable(StoreError::Unavailable {
                category: "transaction approve".into(),
                code: "SQLITE_IOERR".into(),
            })),
            "{stage:?}"
        );
        assert_eq!(grant_rows(&w.ledger), 0, "{stage:?}");
        assert!(w.ledger.db().conn.is_autocommit());
        assert!(
            approve(&svc, &publish("k1"), "g1").is_ok(),
            "{stage:?}: label reusable"
        );
    }
}

// ---------------------------------------------------------------------------
// Consumption refusals.

#[test]
fn require_approval_without_a_grant_is_approval_required() {
    let w = World::new("approval-required");
    let svc = w.svc();
    approve(&svc, &publish("k1"), "g1").unwrap();
    let refusal = reserve(&svc, &publish("k1"), None).unwrap_err();
    assert_eq!(refusal, Refusal::ApprovalRequired);
    assert_eq!(refusal.external_code(), "APPROVAL_REQUIRED");
    assert_untouched(&w.ledger, "g1");
    // The key stays usable (D-018 A6).
    assert_eq!(
        reserve(&svc, &publish("k1"), Some("g1")),
        Ok(ReserveOutcome::Reserved)
    );
}

#[test]
fn unknown_grant_is_refused() {
    let w = World::new("unknown");
    let svc = w.svc();
    approve(&svc, &publish("k1"), "g1").unwrap();
    assert_eq!(
        reserve(&svc, &publish("k1"), Some("forged")),
        Err(Refusal::GrantUnknown)
    );
    assert_untouched(&w.ledger, "g1");
    assert_eq!(audited(&w.ledger, "GRANT_UNKNOWN"), 1);
}

#[test]
fn a_grant_for_another_binding_is_a_mismatch() {
    let w = World::new("mismatch");
    let svc = w.svc();
    approve(&svc, &publish("k1"), "g1").unwrap();
    let mismatches = [
        // Another resource (b-16).
        request(
            "repository.publish",
            "git://release",
            "repository:fixture-tenant/fixture-project/release",
            "k1",
        ),
        // Another requester principal (no subagent inheritance, D-009).
        set(
            &publish("k1"),
            &["principal", "id"],
            json::s("agent:claude-session-2"),
        ),
        // Another action digest.
        set(
            &publish("k1"),
            &["action", "action_digest"],
            json::s("sha256:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"),
        ),
        // Another kind needing approval.
        request(
            "process.execute",
            "proc://make",
            "process:fixture-tenant/fixture-project/make",
            "k1",
        ),
    ];
    for req in &mismatches {
        assert_eq!(
            reserve(&svc, req, Some("g1")),
            Err(Refusal::GrantMismatch),
            "{req:?}"
        );
    }
    // A denial is recorded as a grant that never matches (b-21).
    approve_as(
        &svc,
        &publish("k2"),
        "denied",
        Verdict::Denied,
        None,
        &alice(),
        &channels(),
    )
    .unwrap();
    assert_eq!(
        reserve(&svc, &publish("k2"), Some("denied")),
        Err(Refusal::GrantMismatch)
    );
    assert_untouched(&w.ledger, "g1");
    assert_eq!(uses(&w.ledger, "denied"), Some(0));
    assert_eq!(audited(&w.ledger, "GRANT_MISMATCH"), 5);
}

#[test]
fn grant_expiry_is_decided_on_trusted_time_and_the_floor_persists_although_refused() {
    let w = World::new("expiry");
    let svc = w.svc();
    approve_as(
        &svc,
        &publish("k1"),
        "g1",
        Verdict::Approved,
        Some(30),
        &alice(),
        &channels(),
    )
    .unwrap();
    approve_as(
        &svc,
        &publish("k2"),
        "g2",
        Verdict::Approved,
        Some(30),
        &alice(),
        &channels(),
    )
    .unwrap();
    // Just before expiry: consumed.
    w.clock.set(ts("2026-11-05T12:00:29.999999999Z"));
    assert_eq!(
        reserve(&svc, &publish("k1"), Some("g1")),
        Ok(ReserveOutcome::Reserved)
    );
    // At expiry (exclusive): refused, and the time that decided it is the persisted floor.
    let expiry = ts("2026-11-05T12:00:30Z");
    w.clock.set(expiry);
    // The refusal audit fails as well: the floor is durable regardless.
    w.ledger
        .inject("audit", fault::Stage::Commit, rusqlite::ffi::SQLITE_FULL);
    assert_eq!(
        reserve(&svc, &publish("k2"), Some("g2")),
        Err(Refusal::GrantExpired)
    );
    assert_eq!(audited(&w.ledger, "GRANT_EXPIRED"), 0, "audit rolled back");
    assert_eq!(w.ledger.time_floor().unwrap(), Some(expiry));
    assert_eq!(uses(&w.ledger, "g2"), Some(0));
    assert_eq!(receipt_rows(&w.ledger), 1, "only k1");
    // A clock set back afterwards never revives the grant: time is unavailable.
    w.clock.set(now());
    assert_eq!(
        reserve(&svc, &publish("k2"), Some("g2")),
        Err(Refusal::TimeUnavailable)
    );
    assert_eq!(
        w.ledger.time_floor().unwrap(),
        Some(expiry),
        "never lowered"
    );
    assert_eq!(uses(&w.ledger, "g2"), Some(0));
    // The floor survives a restart.
    drop(svc);
    let World {
        ledger,
        active,
        clock,
    } = w;
    let path = ledger_path(&ledger);
    drop(ledger);
    let ledger = SqliteLedger::open(&path).unwrap();
    assert_eq!(ledger.time_floor().unwrap(), Some(expiry));
    clock.set(expiry.plus_seconds(1));
    let svc = GrantService::new(&active, &clock, &ledger);
    assert_eq!(
        reserve(&svc, &publish("k2"), Some("g2")),
        Err(Refusal::GrantExpired)
    );
}

fn ledger_path(l: &SqliteLedger) -> PathBuf {
    let db = l.db();
    PathBuf::from(db.conn.path().unwrap())
}

#[test]
fn exhausted_grant_is_refused_without_a_second_use() {
    let w = World::new("exhausted");
    let svc = w.svc();
    approve(&svc, &publish("k1"), "g1").unwrap();
    assert_eq!(
        reserve(&svc, &publish("k1"), Some("g1")),
        Ok(ReserveOutcome::Reserved)
    );
    assert_eq!(
        reserve(&svc, &publish("k2"), Some("g1")),
        Err(Refusal::GrantExhausted)
    );
    assert_eq!(uses(&w.ledger, "g1"), Some(1));
    assert_eq!(use_rows(&w.ledger), 1);
    assert_eq!(receipt_rows(&w.ledger), 1);
    let row: (String, String, String) = w
        .ledger
        .db()
        .conn
        .query_row(
            "SELECT label, tenant_id, idempotency_key FROM grant_uses",
            [],
            |r| Ok((r.get(0)?, r.get(1)?, r.get(2)?)),
        )
        .unwrap();
    assert_eq!(row, ("g1".into(), "fixture-tenant".into(), "k1".into()));
    let receipt: (Option<String>, String) = w
        .ledger
        .db()
        .conn
        .query_row(
            "SELECT grant_label, status FROM receipts WHERE idempotency_key = 'k1'",
            [],
            |r| Ok((r.get(0)?, r.get(1)?)),
        )
        .unwrap();
    assert_eq!(receipt, (Some("g1".into()), "reserved".into()));
    let event: (String, String) = w
        .ledger
        .db()
        .conn
        .query_row("SELECT status, cause FROM receipt_events", [], |r| {
            Ok((r.get(0)?, r.get(1)?))
        })
        .unwrap();
    assert_eq!(event, ("reserved".into(), "reserve".into()));
}

#[test]
fn an_allowed_request_reserves_without_a_grant() {
    let w = World::new("allow");
    let svc = w.svc();
    approve(&svc, &publish("k0"), "g1").unwrap();
    // A grant presented with an `allow` decision is not effective and not used.
    assert_eq!(
        reserve(&svc, &file_write("k1"), Some("g1")),
        Ok(ReserveOutcome::Reserved)
    );
    assert_eq!(uses(&w.ledger, "g1"), Some(0));
    let grant: Option<String> = w
        .ledger
        .db()
        .conn
        .query_row(
            "SELECT grant_label FROM receipts WHERE idempotency_key = 'k1'",
            [],
            |r| r.get(0),
        )
        .unwrap();
    assert_eq!(grant, None);
    // A decision refusal never reaches the ledger transaction.
    let secrets = request(
        "file.write",
        "file:///work/secrets/k",
        "workspace:fixture-tenant/fixture-project/secrets/k",
        "k2",
    );
    assert_eq!(
        reserve(&svc, &secrets, None).unwrap_err().code(),
        "SECRETS_PATH_FORBIDDEN"
    );
    assert_eq!(receipt_rows(&w.ledger), 1);
}

// ---------------------------------------------------------------------------
// Idempotency outcomes of an existing receipt.

#[test]
fn an_existing_receipt_gives_conflict_replay_reconciliation_or_resume() {
    let w = World::new("idempotency");
    let svc = w.svc();
    approve(&svc, &publish("k1"), "g1").unwrap();
    approve(&svc, &publish("k1"), "g2").unwrap();
    assert_eq!(
        reserve(&svc, &publish("k1"), Some("g1")),
        Ok(ReserveOutcome::Reserved)
    );
    // Resume from `reserved` without a second grant use (C-3).
    assert_eq!(
        reserve(&svc, &publish("k1"), Some("g1")),
        Ok(ReserveOutcome::Resumed)
    );
    assert_eq!(uses(&w.ledger, "g1"), Some(1));
    assert_eq!(use_rows(&w.ledger), 1);
    assert_eq!(event_rows(&w.ledger), 1, "a resume writes nothing");
    // Same key, another grant (SR-11): conflict; the other grant is not used.
    assert_eq!(
        reserve(&svc, &publish("k1"), Some("g2")),
        Err(Refusal::IdempotencyConflict)
    );
    assert_eq!(uses(&w.ledger, "g2"), Some(0));
    // Same key, another request (fingerprint): conflict.
    let other = set(&publish("k1"), &["trace_id"], json::s("trace-2"));
    assert_eq!(
        reserve(&svc, &other, Some("g1")),
        Err(Refusal::IdempotencyConflict)
    );
    // Same key, an allowed request (no effective grant): conflict.
    let allowed = file_write("k1");
    assert_eq!(
        reserve(&svc, &allowed, None),
        Err(Refusal::IdempotencyConflict)
    );
    // By stored status.
    for (status, want) in [
        ("dispatched", Err(Refusal::ReconciliationRequired)),
        ("unknown_reconcile", Err(Refusal::ReconciliationRequired)),
        (
            "observed_success",
            Ok(ReserveOutcome::Replayed(Replay::EffectCompleted)),
        ),
        (
            "observed_failure",
            Ok(ReserveOutcome::Replayed(Replay::EffectFailed)),
        ),
    ] {
        set_status(&w.ledger, "k1", status);
        assert_eq!(reserve(&svc, &publish("k1"), Some("g1")), want, "{status}");
        // A conflicting retry is still a conflict, whatever the status.
        assert_eq!(
            reserve(&svc, &publish("k1"), Some("g2")),
            Err(Refusal::IdempotencyConflict),
            "{status}"
        );
    }
    assert_eq!(uses(&w.ledger, "g1"), Some(1));
    assert_eq!(uses(&w.ledger, "g2"), Some(0));
    assert_eq!(use_rows(&w.ledger), 1);
    assert_eq!(receipt_rows(&w.ledger), 1);
    assert_eq!(event_rows(&w.ledger), 1);
    assert_eq!(
        Refusal::ReconciliationRequired.external_code(),
        "RECONCILIATION_REQUIRED"
    );
}

#[test]
fn an_unknown_stored_receipt_status_fails_closed_as_integrity() {
    let w = World::new("receipt-status");
    let svc = w.svc();
    assert_eq!(
        reserve(&svc, &file_write("k1"), None),
        Ok(ReserveOutcome::Reserved)
    );
    // The schema's CHECK forbids other statuses; bypass it to prove the read side.
    {
        let db = w.ledger.db();
        db.conn
            .execute_batch("PRAGMA ignore_check_constraints = ON")
            .unwrap();
        db.conn
            .execute("UPDATE receipts SET status = 'bogus'", [])
            .unwrap();
        db.conn
            .execute_batch("PRAGMA ignore_check_constraints = OFF")
            .unwrap();
    }
    assert_eq!(
        reserve(&svc, &file_write("k1"), None),
        Err(reserve_unavailable("integrity"))
    );
}

// ---------------------------------------------------------------------------
// C-4 stored-grant integrity, injected into the ledger file.

#[test]
fn a_malformed_stored_binding_is_a_ledger_integrity_failure_without_a_grant_use() {
    let path = tmp("c4");
    let active = ActivePolicy::new(PolicyState::loaded(reference()));
    let clock = TestClock::at(now());
    let good = {
        let ledger = SqliteLedger::create(&path).unwrap();
        let svc = GrantService::new(&active, &clock, &ledger);
        approve(&svc, &publish("k0"), "g1").unwrap().binding
    };
    let value = json::parse(good.as_bytes()).unwrap();
    let canonical = |v: &Value| json::canonical(v).unwrap();
    let reordered = {
        // Member order reversed: valid JSON, not its own canonical text.
        let Value::Obj(m) = &value else { panic!() };
        let members: Vec<String> = m
            .iter()
            .rev()
            .map(|(k, v)| format!("{}:{}", json::s(k).to_json_text(), v.to_json_text()))
            .collect();
        format!("{{{}}}", members.join(","))
    };
    let malformed: Vec<(&str, String)> = vec![
        ("not json", "not json".into()),
        ("empty", String::new()),
        ("truncated", good[..good.len() - 1].to_string()),
        ("trailing space", format!("{good} ")),
        ("inner whitespace", good.replacen(':', ": ", 1)),
        ("member order", reordered),
        (
            "duplicate key",
            good.replacen('{', r#"{"verdict":"approved","#, 1),
        ),
        (
            "missing member",
            canonical(&value.without("approval_request_id")),
        ),
        (
            "extra member",
            canonical(&set(&value, &["grant_id"], json::s("x"))),
        ),
        (
            "number",
            canonical(&set(&value, &["tenant_id"], Value::Int(7))),
        ),
        ("null", canonical(&set(&value, &["verdict"], Value::Null))),
        (
            "capability not a string",
            canonical(&set(
                &value,
                &["requested_capabilities"],
                Value::Arr(vec![Value::Bool(true)]),
            )),
        ),
        (
            "capabilities not an array",
            canonical(&set(&value, &["requested_capabilities"], json::s("x"))),
        ),
        ("array", "[]".into()),
        ("float", good.replacen("[]", "[1.5]", 1)),
    ];
    for (i, (what, text)) in malformed.iter().enumerate() {
        {
            let raw = rusqlite::Connection::open(&path).unwrap();
            raw.execute("UPDATE grants SET binding = ?1 WHERE label = 'g1'", [text])
                .unwrap();
        }
        let ledger = SqliteLedger::open(&path).unwrap();
        let svc = GrantService::new(&active, &clock, &ledger);
        let idem = format!("k{i}");
        let refusal = reserve(&svc, &publish(&idem), Some("g1")).unwrap_err();
        assert_eq!(refusal, reserve_unavailable("integrity"), "{what}");
        assert_eq!(refusal.external_code(), "PROMOTION_REFUSED");
        assert_untouched(&ledger, "g1");
        assert_eq!(
            audited(&ledger, "LEDGER_UNAVAILABLE"),
            i64::try_from(i).unwrap() + 1
        );
        assert!(ledger.db().conn.is_autocommit(), "{what}");
    }
    // A well-formed binding that differs from the request stays a mismatch.
    {
        let raw = rusqlite::Connection::open(&path).unwrap();
        let other = canonical(&set(&value, &["tenant_id"], json::s("other-tenant")));
        raw.execute("UPDATE grants SET binding = ?1 WHERE label = 'g1'", [other])
            .unwrap();
    }
    let ledger = SqliteLedger::open(&path).unwrap();
    let svc = GrantService::new(&active, &clock, &ledger);
    assert_eq!(
        reserve(&svc, &publish("k-last"), Some("g1")),
        Err(Refusal::GrantMismatch)
    );
    // The original binding consumes normally.
    ledger
        .db()
        .conn
        .execute("UPDATE grants SET binding = ?1 WHERE label = 'g1'", [&good])
        .unwrap();
    assert_eq!(
        reserve(&svc, &publish("k-good"), Some("g1")),
        Ok(ReserveOutcome::Reserved)
    );
}

#[test]
fn an_unrepresentable_stored_expiry_is_an_integrity_failure() {
    let w = World::new("c4-expiry");
    let svc = w.svc();
    approve(&svc, &publish("k0"), "g1").unwrap();
    w.ledger
        .db()
        .conn
        .execute("UPDATE grants SET expires_s = ?1", [i64::MAX])
        .unwrap();
    assert_eq!(
        reserve(&svc, &publish("k1"), Some("g1")),
        Err(reserve_unavailable("integrity"))
    );
    assert_untouched(&w.ledger, "g1");
}

// ---------------------------------------------------------------------------
// Storage faults in T-reserve.

#[test]
fn injected_reserve_faults_at_begin_body_and_commit_leave_no_grant_use_and_no_receipt() {
    for (i, (stage, code, name)) in [
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
    ]
    .into_iter()
    .enumerate()
    {
        let w = World::new(&format!("reserve-fault-{i}"));
        let svc = w.svc();
        approve(&svc, &publish("k0"), "g1").unwrap();
        w.ledger.inject("reserve", stage, code);
        let refusal = reserve(&svc, &publish("k1"), Some("g1")).unwrap_err();
        assert_eq!(refusal, reserve_unavailable(name), "{stage:?}");
        assert_eq!(refusal.external_code(), "PROMOTION_REFUSED");
        assert_untouched(&w.ledger, "g1");
        assert!(
            w.ledger.db().conn.is_autocommit(),
            "{stage:?}: transaction left open"
        );
        assert_eq!(audited(&w.ledger, "LEDGER_UNAVAILABLE"), 1, "{stage:?}");
        assert_eq!(svc.key_locks().held(), 0, "{stage:?}: key lock released");
        // Nothing partial: the same key and grant reserve normally afterwards.
        assert_eq!(
            reserve(&svc, &publish("k1"), Some("g1")),
            Ok(ReserveOutcome::Reserved),
            "{stage:?}"
        );
        assert_eq!(uses(&w.ledger, "g1"), Some(1));
        assert_eq!(use_rows(&w.ledger), 1);
    }
}

#[test]
fn injected_floor_fault_refuses_before_any_consumption() {
    let w = World::new("floor-fault");
    let svc = w.svc();
    approve(&svc, &publish("k0"), "g1").unwrap();
    w.ledger.inject(
        "time_floor",
        fault::Stage::Commit,
        rusqlite::ffi::SQLITE_IOERR,
    );
    assert_eq!(
        reserve(&svc, &publish("k1"), Some("g1")),
        Err(Refusal::TimeUnavailable)
    );
    assert_untouched(&w.ledger, "g1");
}

// ---------------------------------------------------------------------------
// A delegating ledger: trusted time on one ledger, grant transactions on another,
// with a hook around the T-reserve transaction.

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
enum Phase {
    Before,
    After,
}

struct Wrap<'a> {
    time: &'a SqliteLedger,
    grants: &'a SqliteLedger,
    hook: &'a (dyn Fn(Phase) + Sync),
}

impl GrantLedger for Wrap<'_> {
    fn insert_grant(&self, grant: &NewGrant) -> Result<GrantInsert, StoreError> {
        self.grants.insert_grant(grant)
    }

    fn reserve(
        &self,
        request: &ReserveRequest<'_>,
    ) -> Result<Result<ReserveOutcome, Refusal>, writ_broker::store::TxFailure> {
        (self.hook)(Phase::Before);
        let out = self.grants.reserve(request);
        (self.hook)(Phase::After);
        out
    }
}

impl LedgerStore for &Wrap<'_> {
    fn owner_epoch(&self) -> u64 {
        self.time.epoch
    }

    fn record_refusal(&mut self, code: &str) -> Result<(), StoreError> {
        self.time.audit(code)
    }

    fn time_floor(&self) -> Result<Option<Timestamp>, StoreError> {
        self.time.floor()
    }

    fn raise_time_floor(&mut self, at: Timestamp) -> Result<Timestamp, StoreError> {
        self.time.raise_floor(at)
    }

    fn record_clock_event(
        &mut self,
        event: &writ_broker::clock::ClockEvent,
    ) -> Result<(), StoreError> {
        self.time.clock_event(event)
    }
}

fn no_hook(_: Phase) {}

// ---------------------------------------------------------------------------
// Fencing.

#[test]
fn a_stale_owner_is_fenced_on_the_approve_and_reserve_transactions() {
    let path = tmp("fence");
    let active = ActivePolicy::new(PolicyState::loaded(reference()));
    let clock = TestClock::at(now());
    let mut stale = SqliteLedger::create(&path).unwrap();
    approve(
        &GrantService::new(&active, &clock, &stale),
        &publish("k0"),
        "g1",
    )
    .unwrap();
    // Simulate a lost ownership lock; a new owner starts.
    stale._lock = None;
    let owner = SqliteLedger::open(&path).unwrap();
    let fenced = Refusal::LedgerUnavailable(StoreError::Fenced { mine: 1, stored: 2 });
    // Through the stale ledger alone, the floor transaction is the first fenced write.
    let svc = GrantService::new(&active, &clock, &stale);
    assert_eq!(
        approve(&svc, &publish("k1"), "g2"),
        Err(Refusal::TimeUnavailable)
    );
    assert_eq!(
        reserve(&svc, &publish("k1"), Some("g1")),
        Err(Refusal::TimeUnavailable)
    );
    // With time from the live owner, the grant transactions of the stale one are fenced.
    let split = Wrap {
        time: &owner,
        grants: &stale,
        hook: &no_hook,
    };
    let svc = GrantService::new(&active, &clock, &split);
    let g2 = label("g2");
    let verified = verify(&publish("k1"));
    let approval = Approval {
        request: &verified,
        state_revision: revision(),
        label: &g2,
        verdict: Verdict::Approved,
        ttl_seconds: None,
    };
    assert_eq!(
        svc.approve(&approval, &alice(), &channels()),
        Err(fenced.clone())
    );
    assert_eq!(
        svc.reserve(&verified, revision(), Some(&label("g1"))),
        Err(fenced)
    );
    assert_eq!(grant_rows(&owner), 1);
    assert_eq!(uses(&owner, "g1"), Some(0));
    assert_eq!(receipt_rows(&owner), 0);
}

// ---------------------------------------------------------------------------
// Concurrency.

const WORKERS: usize = 64;
const ROUNDS: usize = 20;

#[test]
fn sixty_four_threads_with_distinct_keys_race_for_one_single_use_grant() {
    let w = World::new("race-distinct");
    let svc = w.svc();
    for round in 0..ROUNDS {
        let g = format!("g{round}");
        approve(&svc, &publish(&format!("approve-{round}")), &g).unwrap();
        let requests: Vec<VerifiedRequest> = (0..WORKERS)
            .map(|i| verify(&publish(&format!("r{round}-k{i}"))))
            .collect();
        let start = Barrier::new(WORKERS);
        let grant = label(&g);
        let outcomes: Vec<Result<ReserveOutcome, Refusal>> = thread::scope(|s| {
            let workers: Vec<_> = requests
                .iter()
                .map(|v| {
                    let (svc, start, grant) = (&svc, &start, &grant);
                    s.spawn(move || {
                        start.wait();
                        svc.reserve(v, revision(), Some(grant))
                    })
                })
                .collect();
            workers.into_iter().map(|h| h.join().unwrap()).collect()
        });
        let reserved = outcomes
            .iter()
            .filter(|o| **o == Ok(ReserveOutcome::Reserved))
            .count();
        let exhausted = outcomes
            .iter()
            .filter(|o| **o == Err(Refusal::GrantExhausted))
            .count();
        assert_eq!(
            (reserved, exhausted),
            (1, WORKERS - 1),
            "round {round}: {outcomes:?}"
        );
        assert_eq!(uses(&w.ledger, &g), Some(1), "round {round}");
        let round_uses: i64 = w
            .ledger
            .db()
            .conn
            .query_row(
                "SELECT count(*) FROM grant_uses WHERE label = ?1",
                [&g],
                |r| r.get(0),
            )
            .unwrap();
        assert_eq!(round_uses, 1, "round {round}");
        let round_receipts: i64 = w
            .ledger
            .db()
            .conn
            .query_row(
                "SELECT count(*) FROM receipts WHERE grant_label = ?1",
                [&g],
                |r| r.get(0),
            )
            .unwrap();
        assert_eq!(round_receipts, 1, "round {round}");
        assert_eq!(svc.key_locks().held(), 0);
    }
    let rounds = i64::try_from(ROUNDS).unwrap();
    assert_eq!(use_rows(&w.ledger), rounds);
    assert_eq!(receipt_rows(&w.ledger), rounds);
    assert_eq!(event_rows(&w.ledger), rounds);
    assert_eq!(
        audited(&w.ledger, "GRANT_EXHAUSTED"),
        rounds * i64::try_from(WORKERS - 1).unwrap()
    );
}

#[test]
fn sixty_four_threads_on_one_idempotency_key_give_exactly_one_reservation() {
    for (round, with_grant) in [(0, true), (1, false)] {
        let w = World::new(&format!("race-one-key-{round}"));
        let svc = w.svc();
        approve(&svc, &publish("k1"), "g1").unwrap();
        let req = if with_grant {
            publish("k1")
        } else {
            file_write("k1")
        };
        let verified = verify(&req);
        let grant = with_grant.then(|| label("g1"));
        let start = Barrier::new(WORKERS);
        let outcomes: Vec<Result<ReserveOutcome, Refusal>> = thread::scope(|s| {
            let workers: Vec<_> = (0..WORKERS)
                .map(|_| {
                    let (svc, start, verified, grant) = (&svc, &start, &verified, &grant);
                    s.spawn(move || {
                        start.wait();
                        svc.reserve(verified, revision(), grant.as_ref())
                    })
                })
                .collect();
            workers.into_iter().map(|h| h.join().unwrap()).collect()
        });
        let reserved = outcomes
            .iter()
            .filter(|o| **o == Ok(ReserveOutcome::Reserved))
            .count();
        let resumed = outcomes
            .iter()
            .filter(|o| **o == Ok(ReserveOutcome::Resumed))
            .count();
        assert_eq!((reserved, resumed), (1, WORKERS - 1), "{outcomes:?}");
        assert_eq!(receipt_rows(&w.ledger), 1);
        assert_eq!(event_rows(&w.ledger), 1);
        assert_eq!(use_rows(&w.ledger), i64::from(with_grant));
        assert_eq!(uses(&w.ledger, "g1"), Some(i64::from(with_grant)));
        assert_eq!(count(&w.ledger, "SELECT count(*) FROM audit_refusals"), 0);
    }
}

// ---------------------------------------------------------------------------
// Policy activation racing grant consumption.

#[test]
fn activation_is_never_observed_inside_a_consumption_and_a_later_consumption_redecides() {
    let path = tmp("activation");
    let ledger = SqliteLedger::create(&path).unwrap();
    let active = ActivePolicy::new(PolicyState::loaded(reference()));
    let clock = TestClock::at(now());
    let attempting = AtomicBool::new(false);
    let activated = AtomicBool::new(false);
    let seen_inside = AtomicBool::new(false);
    let (entered_tx, entered_rx) = mpsc::channel::<()>();
    let entered_tx = Mutex::new(Some(entered_tx));
    let hook = |phase: Phase| match phase {
        Phase::Before => {
            if let Some(tx) = entered_tx.lock().unwrap().take() {
                tx.send(()).unwrap();
                // Run T-reserve only once the activation is pending: it must wait.
                let waited = std::time::Instant::now();
                while !attempting.load(Ordering::SeqCst) {
                    assert!(waited.elapsed() < Duration::from_secs(10));
                    thread::sleep(Duration::from_millis(1));
                }
                thread::sleep(Duration::from_millis(200));
            }
            if activated.load(Ordering::SeqCst) {
                seen_inside.store(true, Ordering::SeqCst);
            }
        }
        Phase::After => {
            if activated.load(Ordering::SeqCst) {
                seen_inside.store(true, Ordering::SeqCst);
            }
        }
    };
    let wrapped = Wrap {
        time: &ledger,
        grants: &ledger,
        hook: &hook,
    };
    let svc = GrantService::new(&active, &clock, &wrapped);
    for (k, g) in [("k1", "g1"), ("k2", "g2"), ("k3", "g3")] {
        let verified = verify(&publish(k));
        let g = label(g);
        svc.approve(
            &Approval {
                request: &verified,
                state_revision: revision(),
                label: &g,
                verdict: Verdict::Approved,
                ttl_seconds: None,
            },
            &alice(),
            &channels(),
        )
        .unwrap();
    }
    let v1 = verify(&publish("k1"));
    let g1 = label("g1");
    let first = thread::scope(|s| {
        let consumer = s.spawn(|| svc.reserve(&v1, revision(), Some(&g1)));
        entered_rx.recv_timeout(Duration::from_secs(10)).unwrap();
        let activator = s.spawn(|| {
            attempting.store(true, Ordering::SeqCst);
            active.activate(PolicyState::loaded(reference_v2()));
            activated.store(true, Ordering::SeqCst);
        });
        activator.join().unwrap();
        consumer.join().unwrap()
    });
    assert_eq!(
        first,
        Ok(ReserveOutcome::Reserved),
        "decided under the old policy"
    );
    assert!(
        !seen_inside.load(Ordering::SeqCst),
        "the activation completed inside a consumption"
    );
    assert!(activated.load(Ordering::SeqCst));
    // After the activation the consumption re-decides under the new bundle: the grant
    // was bound to the old digest.
    assert_eq!(
        svc.reserve(&verify(&publish("k2")), revision(), Some(&label("g2"))),
        Err(Refusal::GrantMismatch)
    );
    // Under a bundle that forbids the action, the decision refuses before the ledger.
    active.activate(PolicyState::loaded(publish_forbidden()));
    assert_eq!(
        svc.reserve(&verify(&publish("k3")), revision(), Some(&label("g3")))
            .unwrap_err()
            .code(),
        "PUBLISH_FROZEN"
    );
    active.activate(PolicyState::Absent);
    assert_eq!(
        svc.reserve(&verify(&publish("k3")), revision(), Some(&label("g3")))
            .unwrap_err()
            .code(),
        "POLICY_UNAVAILABLE"
    );
    assert_eq!(uses(&ledger, "g1"), Some(1));
    assert_eq!(uses(&ledger, "g2"), Some(0));
    assert_eq!(uses(&ledger, "g3"), Some(0));
    assert_eq!(use_rows(&ledger), 1);
    assert_eq!(receipt_rows(&ledger), 1);
}

#[test]
fn concurrent_consumptions_racing_an_activation_are_each_decided_under_one_policy() {
    const CONSUMERS: usize = 32;
    let w = World::new("activation-stress");
    let svc = w.svc();
    for i in 0..CONSUMERS {
        approve(&svc, &publish(&format!("k{i}")), &format!("g{i}")).unwrap();
    }
    let requests: Vec<(VerifiedRequest, GrantLabel)> = (0..CONSUMERS)
        .map(|i| (verify(&publish(&format!("k{i}"))), label(&format!("g{i}"))))
        .collect();
    let start = Barrier::new(CONSUMERS + 1);
    let outcomes: Vec<Result<ReserveOutcome, Refusal>> = thread::scope(|s| {
        let workers: Vec<_> = requests
            .iter()
            .map(|(v, g)| {
                let (svc, start) = (&svc, &start);
                s.spawn(move || {
                    start.wait();
                    svc.reserve(v, revision(), Some(g))
                })
            })
            .collect();
        start.wait();
        w.active.activate(PolicyState::loaded(reference_v2()));
        workers.into_iter().map(|h| h.join().unwrap()).collect()
    });
    let reserved = outcomes
        .iter()
        .filter(|o| **o == Ok(ReserveOutcome::Reserved))
        .count();
    for o in &outcomes {
        assert!(
            *o == Ok(ReserveOutcome::Reserved) || *o == Err(Refusal::GrantMismatch),
            "{o:?}"
        );
    }
    let reserved_i64 = i64::try_from(reserved).unwrap();
    assert_eq!(use_rows(&w.ledger), reserved_i64);
    assert_eq!(receipt_rows(&w.ledger), reserved_i64);
    assert_eq!(
        count(&w.ledger, "SELECT coalesce(sum(uses), 0) FROM grants"),
        reserved_i64
    );
}

// ---------------------------------------------------------------------------
// Restart persistence.

#[test]
fn grants_and_their_uses_persist_across_a_restart() {
    let path = tmp("restart");
    let active = ActivePolicy::new(PolicyState::loaded(reference()));
    let clock = TestClock::at(now());
    {
        let ledger = SqliteLedger::create(&path).unwrap();
        let svc = GrantService::new(&active, &clock, &ledger);
        approve(&svc, &publish("k1"), "g1").unwrap();
        approve(&svc, &publish("k2"), "g2").unwrap();
        assert_eq!(
            reserve(&svc, &publish("k1"), Some("g1")),
            Ok(ReserveOutcome::Reserved)
        );
    }
    clock.set(now().plus_seconds(60));
    let ledger = SqliteLedger::open(&path).unwrap();
    assert_eq!(LedgerStore::owner_epoch(&ledger), 2);
    let svc = GrantService::new(&active, &clock, &ledger);
    assert_eq!(uses(&ledger, "g1"), Some(1));
    assert_eq!(use_rows(&ledger), 1);
    // The reserved receipt resumes without a second use; the grant stays exhausted.
    assert_eq!(
        reserve(&svc, &publish("k1"), Some("g1")),
        Ok(ReserveOutcome::Resumed)
    );
    assert_eq!(
        reserve(&svc, &publish("k3"), Some("g1")),
        Err(Refusal::GrantExhausted)
    );
    // A replayed approval after the restart does not reset the grant.
    assert_eq!(
        approve(&svc, &publish("k1"), "g1"),
        Err(Refusal::GrantRefInUse)
    );
    // The unused grant survived and is consumed exactly once.
    assert_eq!(
        reserve(&svc, &publish("k2"), Some("g2")),
        Ok(ReserveOutcome::Reserved)
    );
    assert_eq!(uses(&ledger, "g1"), Some(1));
    assert_eq!(uses(&ledger, "g2"), Some(1));
    assert_eq!(use_rows(&ledger), 2);
    assert_eq!(receipt_rows(&ledger), 2);
    drop(svc);
    drop(ledger);
    let ledger = SqliteLedger::open(&path).unwrap();
    assert_eq!(use_rows(&ledger), 2);
    assert_eq!(ledger.time_floor().unwrap(), Some(now().plus_seconds(60)));
}
