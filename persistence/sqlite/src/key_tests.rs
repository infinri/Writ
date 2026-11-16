//! M5a-04 (with M5a-05) tests of the ledger key registry and revocations (J-3, J-11.8):
//! registry admission rules, ids never reused, revoked material refused under any id,
//! `keys.revoke` validation, provenance, ordering (`ledger_sequence`), idempotence and
//! append-only rows; and revocation looked up inside T-reserve, T-dispatch and the permit
//! verification callback of the keyed broker path (permit/2, seal/2).

#![allow(clippy::unwrap_used, clippy::expect_used, clippy::panic)]

use writ_broker::clock::ClockEvent;
use writ_broker::execute::{
    Broker, DispatchIntent, Dispatched, Executed, FailureRecord, Observation, PendingReceipt,
    PermitKeys, Reconciled, Resolution,
};
use writ_broker::grants::{GrantInsert, NewGrant, ReserveOutcome, ReserveRequest};
use writ_broker::ingress::{verify_ingress_keyed, IngressBindings, Seal, VerifiedRequest};
use writ_broker::keys::{
    Admission, KeyId, KeyPurpose, KeyRegistry, KeyRevocation, KeyRevocations, KeyValidity,
    Material, MaterialDigest, MemoryKeySource,
};
use writ_broker::policy::ExclusiveHold;
use writ_broker::policy::{ActivePolicy, PolicyState};
use writ_broker::refusal::Refusal;
use writ_broker::store::{ExecuteLedger, GrantLedger, LedgerStore, StoreError, TxFailure};
use writ_broker::writ_kernel::json::Value;
use writ_broker::writ_kernel::time::Timestamp;

use super::grant_tests::{file_write, now, reference, revision, ts, TestClock};
use super::{
    OsActor, RevocationScope, RevokeRefusal, SqliteEffectStore, SqliteLedger,
    MAX_REVOCATION_REASON_BYTES,
};

const ADAPTER: &str = "writ-host-adapter:fixture";

fn tmp(name: &str) -> std::path::PathBuf {
    let d = std::env::temp_dir().join(format!("writ-keys-{}-{name}", std::process::id()));
    let _ = std::fs::remove_dir_all(&d);
    std::fs::create_dir_all(&d).unwrap();
    d
}

fn id(s: &str) -> KeyId {
    KeyId::parse(s).unwrap()
}

fn digest(b: u8) -> MaterialDigest {
    MaterialDigest::of(&[b; 32])
}

fn actor() -> OsActor {
    OsActor { uid: 0, pid: 4242 }
}

fn count(l: &SqliteLedger, sql: &str) -> i64 {
    l.db().conn.query_row(sql, [], |r| r.get(0)).unwrap()
}

fn admit(l: &SqliteLedger, k: &str, p: KeyPurpose, b: u8) -> Admission {
    let mut reg: &SqliteLedger = l;
    reg.admit(&id(k), p, &digest(b)).unwrap()
}

#[test]
fn registry_admits_once_and_never_reuses_ids_or_material() {
    let dir = tmp("registry");
    let l = SqliteLedger::create(&dir.join("ledger.db")).unwrap();
    assert_eq!(
        admit(&l, "ingress/a", KeyPurpose::Ingress, 1),
        Admission::Admitted
    );
    // Same id, purpose and material again: admitted, recorded once.
    assert_eq!(
        admit(&l, "ingress/a", KeyPurpose::Ingress, 1),
        Admission::Admitted
    );
    assert_eq!(count(&l, "SELECT count(*) FROM key_registry"), 1);
    // The id with other material: refused (ids are never reused).
    assert_eq!(
        admit(&l, "ingress/a", KeyPurpose::Ingress, 2),
        Admission::IdConflict
    );
    // The material under another id or another purpose: refused.
    assert_eq!(
        admit(&l, "ingress/b", KeyPurpose::Ingress, 1),
        Admission::MaterialConflict
    );
    assert_eq!(
        admit(&l, "permit/a", KeyPurpose::Permit, 1),
        Admission::MaterialConflict
    );
    // An id naming another purpose than the one presented is never recorded.
    assert_eq!(
        admit(&l, "permit/x", KeyPurpose::Ingress, 9),
        Admission::IdConflict
    );
    assert_eq!(count(&l, "SELECT count(*) FROM key_registry"), 1);
    // The registry survives a restart.
    drop(l);
    let l = SqliteLedger::open(&dir.join("ledger.db")).unwrap();
    assert_eq!(
        admit(&l, "ingress/a", KeyPurpose::Ingress, 2),
        Admission::IdConflict
    );
}

#[test]
fn revoked_ids_and_material_are_refused_at_load_under_any_id() {
    let dir = tmp("revoked-load");
    let l = SqliteLedger::create(&dir.join("ledger.db")).unwrap();
    admit(&l, "permit/a", KeyPurpose::Permit, 3);
    let row = l
        .revoke_key(
            &RevocationScope::KeyId {
                purpose: KeyPurpose::Permit,
                key_id: id("permit/a"),
            },
            "rotated out",
            &actor(),
            now(),
        )
        .unwrap()
        .unwrap();
    assert_eq!(row.material_digest, digest(3).as_str());
    assert_eq!(
        admit(&l, "permit/a", KeyPurpose::Permit, 3),
        Admission::Revoked
    );
    // The revoked material under a fresh id is still refused.
    assert_eq!(
        admit(&l, "permit/b", KeyPurpose::Permit, 3),
        Admission::Revoked
    );
    // A pre-emptive material revocation refuses that material before it is ever loaded.
    l.revoke_key(
        &RevocationScope::Material {
            purpose: KeyPurpose::Ingress,
            digest: digest(7),
        },
        "leaked before provisioning",
        &actor(),
        now(),
    )
    .unwrap()
    .unwrap();
    assert_eq!(
        admit(&l, "ingress/new", KeyPurpose::Ingress, 7),
        Admission::Revoked
    );
    assert!(l.is_revoked(&id("ingress/other"), &digest(7)).unwrap());
    assert!(l.is_revoked(&id("permit/a"), &digest(99)).unwrap());
    assert!(!l.is_revoked(&id("permit/c"), &digest(98)).unwrap());
}

#[test]
fn keys_revoke_validates_scope_purpose_and_reason() {
    let dir = tmp("revoke-validate");
    let l = SqliteLedger::create(&dir.join("ledger.db")).unwrap();
    admit(&l, "ingress/a", KeyPurpose::Ingress, 1);
    let by_id = |p: KeyPurpose, k: &str| RevocationScope::KeyId {
        purpose: p,
        key_id: id(k),
    };
    let revoke =
        |s: &RevocationScope, reason: &str| l.revoke_key(s, reason, &actor(), now()).unwrap();
    assert_eq!(
        revoke(&by_id(KeyPurpose::Ingress, "ingress/unknown"), "x"),
        Err(RevokeRefusal::Unregistered)
    );
    // A registered digest under another purpose than the stated one.
    assert_eq!(
        revoke(
            &RevocationScope::Material {
                purpose: KeyPurpose::Permit,
                digest: digest(1),
            },
            "x"
        ),
        Err(RevokeRefusal::PurposeMismatch)
    );
    let long = "r".repeat(MAX_REVOCATION_REASON_BYTES + 1);
    for bad in ["", long.as_str(), "nul\0inside"] {
        assert_eq!(
            revoke(&by_id(KeyPurpose::Ingress, "ingress/a"), bad),
            Err(RevokeRefusal::Reason)
        );
    }
    assert_eq!(count(&l, "SELECT count(*) FROM key_revocations"), 0);
    assert_eq!(count(&l, "SELECT count(*) FROM ledger_sequence"), 0);
    // A reason at the bound is accepted.
    let ok = revoke(
        &by_id(KeyPurpose::Ingress, "ingress/a"),
        &"r".repeat(MAX_REVOCATION_REASON_BYTES),
    );
    assert!(ok.is_ok());
}

#[test]
fn keys_revoke_records_provenance_and_ordering_and_is_idempotent() {
    let dir = tmp("revoke-record");
    let l = SqliteLedger::create(&dir.join("ledger.db")).unwrap();
    admit(&l, "ingress/a", KeyPurpose::Ingress, 1);
    let scope = RevocationScope::KeyId {
        purpose: KeyPurpose::Ingress,
        key_id: id("ingress/a"),
    };
    let reason = "suspected <script>\"leak\"";
    let first = l
        .revoke_key(&scope, reason, &actor(), ts("2026-11-05T12:00:01Z"))
        .unwrap()
        .unwrap();
    assert!(first.created);
    assert_eq!(first.scope, "key_id");
    assert_eq!(first.key_id.as_deref(), Some("ingress/a"));
    assert_eq!(first.purpose, "ingress");
    assert_eq!(first.revoked_at, "2026-11-05T12:00:01Z");
    assert_eq!(first.reason, reason);
    assert_eq!(
        first.actor,
        r#"{"endpoint":"admin.sock","os_principal":"uid:0","pid":4242,"principal_kind":"os","verified_by":"so_peercred"}"#
    );
    let (kind, reference): (String, String) = l
        .db()
        .conn
        .query_row(
            "SELECT kind, reference FROM ledger_sequence WHERE seq = ?1",
            [first.ledger_seq],
            |r| Ok((r.get(0)?, r.get(1)?)),
        )
        .unwrap();
    assert_eq!(kind, "key_revocation");
    assert_eq!(reference, format!("key_revocation/{}", first.revocation_id));
    // Repeated identical revocation: the existing row, no new row, no new sequence entry.
    let again = l
        .revoke_key(
            &scope,
            "another reason",
            &OsActor { uid: 0, pid: 1 },
            ts("2026-11-05T12:00:09Z"),
        )
        .unwrap()
        .unwrap();
    assert!(!again.created);
    assert_eq!(again.revocation_id, first.revocation_id);
    assert_eq!(again.reason, reason);
    assert_eq!(count(&l, "SELECT count(*) FROM key_revocations"), 1);
    assert_eq!(count(&l, "SELECT count(*) FROM ledger_sequence"), 1);
    // Material scope of the same digest is a separate explicit scope, ordered after.
    let material = l
        .revoke_key(
            &RevocationScope::Material {
                purpose: KeyPurpose::Ingress,
                digest: digest(1),
            },
            "material",
            &actor(),
            ts("2026-11-05T12:00:10Z"),
        )
        .unwrap()
        .unwrap();
    assert_eq!(material.key_id.as_deref(), Some("ingress/a"));
    assert!(material.ledger_seq > first.ledger_seq);
    let listed = l.key_revocations().unwrap();
    assert_eq!(listed.len(), 2);
    assert_eq!(listed[0].revocation_id, first.revocation_id);
}

#[test]
fn registry_and_revocations_are_append_only() {
    let dir = tmp("append-only");
    let l = SqliteLedger::create(&dir.join("ledger.db")).unwrap();
    admit(&l, "ingress/a", KeyPurpose::Ingress, 1);
    l.revoke_key(
        &RevocationScope::KeyId {
            purpose: KeyPurpose::Ingress,
            key_id: id("ingress/a"),
        },
        "x",
        &actor(),
        now(),
    )
    .unwrap()
    .unwrap();
    let db = l.db();
    for sql in [
        "UPDATE key_registry SET material_digest = material_digest",
        "DELETE FROM key_registry",
        "UPDATE key_revocations SET reason = 'y'",
        "DELETE FROM key_revocations",
    ] {
        assert!(db.conn.execute(sql, []).is_err(), "{sql}");
    }
}

#[test]
fn trait_revoke_requires_the_registered_binding() {
    let dir = tmp("trait-revoke");
    let l = SqliteLedger::create(&dir.join("ledger.db")).unwrap();
    admit(&l, "permit/a", KeyPurpose::Permit, 5);
    let mut reg: &SqliteLedger = &l;
    let revocation = |b: u8| KeyRevocation {
        key_id: id("permit/a"),
        material: digest(b),
        purpose: KeyPurpose::Permit,
        revoked_at: now(),
        actor: actor().document().unwrap(),
        reason: "r".into(),
    };
    // M5a-13 L9: free-text provenance is refused; only the structured OS actor is recorded.
    let free_text = KeyRevocation {
        actor: "operator".into(),
        ..revocation(5)
    };
    assert!(reg.revoke(free_text).is_err());
    assert!(reg.revoke(revocation(6)).is_err());
    assert!(reg.revoke(revocation(5)).is_ok());
    assert!(reg.revoke(revocation(5)).is_ok());
    assert_eq!(count(&l, "SELECT count(*) FROM key_revocations"), 1);
    assert!(l.is_revoked(&id("permit/a"), &digest(5)).unwrap());
}

// ---------------------------------------------------------------------------
// Revocation inside the consuming transactions (keyed broker path).

const INGRESS: [u8; 32] = [0x11; 32];
const PERMIT: [u8; 32] = [0x44; 32];

fn keyed_source() -> MemoryKeySource {
    let window = KeyValidity::new(ts("2026-01-01T00:00:00Z"), ts("2027-01-01T00:00:00Z")).unwrap();
    let mut s = MemoryKeySource::new(60);
    assert!(s.insert(
        id("ingress/k1"),
        Material::new(KeyPurpose::Ingress, INGRESS.to_vec()).unwrap(),
        window
    ));
    assert!(s.insert(
        id("permit/k1"),
        Material::new(KeyPurpose::Permit, PERMIT.to_vec()).unwrap(),
        window
    ));
    s
}

fn keyed_verify(req: &Value, keys: &MemoryKeySource, at: Timestamp) -> VerifiedRequest {
    let material = Material::new(KeyPurpose::Ingress, INGRESS.to_vec()).unwrap();
    let seal = Seal::create_keyed(req, ADAPTER, &id("ingress/k1"), material.ingress().unwrap())
        .unwrap()
        .to_value();
    let mut bindings = IngressBindings::new();
    assert!(bindings.bind(ADAPTER, id("ingress/k1")));
    verify_ingress_keyed(req, Some(&seal), keys, &bindings, at).unwrap()
}

struct KeyedWorld {
    ledger: SqliteLedger,
    effects: SqliteEffectStore,
    active: ActivePolicy,
    clock: TestClock,
    keys: MemoryKeySource,
}

impl KeyedWorld {
    fn new(name: &str) -> KeyedWorld {
        let dir = tmp(name);
        let ledger = SqliteLedger::create(&dir.join("ledger.db")).unwrap();
        let keys = keyed_source();
        {
            let mut reg: &SqliteLedger = &ledger;
            for (k, p, m) in [
                ("ingress/k1", KeyPurpose::Ingress, INGRESS),
                ("permit/k1", KeyPurpose::Permit, PERMIT),
            ] {
                assert_eq!(
                    reg.admit(&id(k), p, &MaterialDigest::of(&m)).unwrap(),
                    Admission::Admitted
                );
            }
        }
        KeyedWorld {
            effects: SqliteEffectStore::create(&dir.join("effects.db"), ledger.ledger_id())
                .unwrap(),
            ledger,
            active: ActivePolicy::new(PolicyState::loaded(reference())),
            clock: TestClock::at(now()),
            keys,
        }
    }

    fn broker(&self) -> Broker<'_, SqliteLedger, SqliteEffectStore> {
        Broker::with_permit_keys(
            &self.active,
            &self.clock,
            &self.ledger,
            &self.effects,
            PermitKeys::Keyed {
                source: &self.keys,
                revocations: &self.ledger,
            },
        )
    }

    fn revoke(&self, k: &str, p: KeyPurpose) {
        self.ledger
            .revoke_key(
                &RevocationScope::KeyId {
                    purpose: p,
                    key_id: id(k),
                },
                "test",
                &actor(),
                now(),
            )
            .unwrap()
            .unwrap();
    }

    fn status(&self, idem: &str) -> Option<String> {
        self.ledger
            .db()
            .conn
            .query_row(
                "SELECT status FROM receipts WHERE idempotency_key = ?1",
                [idem],
                |r| r.get(0),
            )
            .ok()
    }
}

#[test]
fn keyed_execution_issues_permit_2_and_completes() {
    let w = KeyedWorld::new("keyed-ok");
    let req = file_write("k1");
    let e = w
        .broker()
        .execute(&keyed_verify(&req, &w.keys, now()), revision(), None);
    assert_eq!(e.result, Ok(Executed::EffectCompleted));
    let permit = e.permit.unwrap().to_value();
    let m = permit.as_obj().unwrap();
    assert_eq!(
        m.get("contract_version").and_then(Value::as_str),
        Some("permit/2")
    );
    assert_eq!(m.get("key_id").and_then(Value::as_str), Some("permit/k1"));
}

#[test]
fn a_seal_key_revoked_before_reserve_is_refused_in_t_reserve() {
    let w = KeyedWorld::new("seal-before-reserve");
    let req = file_write("k1");
    let verified = keyed_verify(&req, &w.keys, now());
    w.revoke("ingress/k1", KeyPurpose::Ingress);
    let e = w.broker().execute(&verified, revision(), None);
    assert_eq!(e.result, Err(Refusal::IngressUnauthenticated));
    assert_eq!(w.status("k1"), None);
}

#[test]
fn a_seal_key_revoked_while_a_receipt_is_reserved_is_refused_at_resume() {
    let w = KeyedWorld::new("seal-before-resume");
    let req = file_write("k1");
    let verified = keyed_verify(&req, &w.keys, now());
    // T-dispatch cannot begin once: the receipt stays reserved.
    w.ledger.inject(
        "dispatch",
        super::fault::Stage::Begin,
        rusqlite::ffi::SQLITE_IOERR,
    );
    let first = w.broker().execute(&verified, revision(), None);
    assert!(first.result.is_err());
    assert_eq!(w.status("k1").as_deref(), Some("reserved"));
    w.revoke("ingress/k1", KeyPurpose::Ingress);
    let again = w.broker().execute(&verified, revision(), None);
    assert_eq!(again.result, Err(Refusal::IngressUnauthenticated));
    assert_eq!(w.status("k1").as_deref(), Some("reserved"));
}

/// The ledger with a hook run just before T-dispatch begins, so a revocation commits
/// between T-reserve and T-dispatch of one execution.
struct RevokeBeforeDispatch<'a> {
    inner: &'a SqliteLedger,
    hook: &'a (dyn Fn() + Sync),
}

impl LedgerStore for &RevokeBeforeDispatch<'_> {
    fn owner_epoch(&self) -> u64 {
        LedgerStore::owner_epoch(&self.inner)
    }
    fn record_refusal(&mut self, code: &str) -> Result<(), StoreError> {
        let mut l = self.inner;
        l.record_refusal(code)
    }
    fn time_floor(&self) -> Result<Option<Timestamp>, StoreError> {
        LedgerStore::time_floor(&self.inner)
    }
    fn raise_time_floor(&mut self, at: Timestamp) -> Result<Timestamp, StoreError> {
        let mut l = self.inner;
        l.raise_time_floor(at)
    }
    fn record_clock_event(&mut self, event: &ClockEvent) -> Result<(), StoreError> {
        let mut l = self.inner;
        l.record_clock_event(event)
    }
}

impl GrantLedger for RevokeBeforeDispatch<'_> {
    fn insert_grant(&self, grant: &NewGrant) -> Result<GrantInsert, StoreError> {
        self.inner.insert_grant(grant)
    }
    fn reserve(
        &self,
        request: &ReserveRequest<'_>,
    ) -> Result<Result<ReserveOutcome, Refusal>, TxFailure> {
        self.inner.reserve(request)
    }
}

impl ExecuteLedger for RevokeBeforeDispatch<'_> {
    fn dispatch(
        &self,
        intent: &DispatchIntent<'_>,
    ) -> Result<Result<Dispatched, Refusal>, TxFailure> {
        (self.hook)();
        self.inner.dispatch(intent)
    }
    fn observe_effect(&self, observation: &Observation<'_>) -> Result<(), TxFailure> {
        self.inner.observe_effect(observation)
    }
    fn pending_receipts(&self, gate: &ExclusiveHold<'_>) -> Result<Vec<PendingReceipt>, TxFailure> {
        self.inner.pending_receipts(gate)
    }
    fn reconcile(
        &self,
        gate: &ExclusiveHold<'_>,
        resolutions: &[Resolution],
    ) -> Result<Reconciled, TxFailure> {
        self.inner.reconcile(gate, resolutions)
    }
    fn record_failure(&self, code: &str, record: &FailureRecord) -> Result<(), StoreError> {
        self.inner.record_failure(code, record)
    }
}

#[test]
fn a_seal_key_revoked_between_reserve_and_dispatch_is_refused_in_t_dispatch() {
    let w = KeyedWorld::new("seal-in-dispatch");
    let req = file_write("k1");
    let verified = keyed_verify(&req, &w.keys, now());
    let hook = || w.revoke("ingress/k1", KeyPurpose::Ingress);
    let ledger = RevokeBeforeDispatch {
        inner: &w.ledger,
        hook: &hook,
    };
    let b = Broker::with_permit_keys(
        &w.active,
        &w.clock,
        &ledger,
        &w.effects,
        PermitKeys::Keyed {
            source: &w.keys,
            revocations: &w.ledger,
        },
    );
    let e = b.execute(&verified, revision(), None);
    assert_eq!(e.result, Err(Refusal::IngressUnauthenticated));
    // T-reserve committed before the revocation; T-dispatch refused after it: the receipt
    // stays reserved, no permit was issued and nothing was applied.
    assert_eq!(w.status("k1").as_deref(), Some("reserved"));
    assert!(e.permit.is_none());
    let seq: Vec<String> = {
        let db = w.ledger.db();
        let mut st = db
            .conn
            .prepare("SELECT kind FROM ledger_sequence ORDER BY seq")
            .unwrap();
        st.query_map([], |r| r.get(0))
            .unwrap()
            .collect::<rusqlite::Result<_>>()
            .unwrap()
    };
    assert_eq!(seq, vec!["key_revocation".to_string()]);
}

#[test]
fn a_permit_key_revoked_after_issue_is_refused_inside_the_adapter_transaction() {
    let w = KeyedWorld::new("permit-revoked");
    let req = file_write("k1");
    let verified = keyed_verify(&req, &w.keys, now());
    // The adapter transaction cannot begin: the permit is issued and unconsumed.
    w.effects
        .inject(super::fault::Stage::Begin, rusqlite::ffi::SQLITE_IOERR);
    let e = w.broker().execute(&verified, revision(), None);
    let permit = e.permit.unwrap();
    w.revoke("permit/k1", KeyPurpose::Permit);
    // Presenting it now: the verification callback reads the revocation (PERMIT_INVALID).
    assert_eq!(
        w.broker().present_permit(&permit, &req),
        Refusal::PermitInvalid
    );
    // And issuing refuses (no qualifying permit key): fail closed.
    let other = file_write("k2");
    let e = w
        .broker()
        .execute(&keyed_verify(&other, &w.keys, now()), revision(), None);
    assert_eq!(e.result, Err(Refusal::PermitInvalid));
    assert_eq!(w.status("k2").as_deref(), Some("dispatched"));
}

#[test]
fn the_keyed_path_refuses_permit_1() {
    let w = KeyedWorld::new("permit-1");
    let req = file_write("k1");
    let verified = keyed_verify(&req, &w.keys, now());
    w.effects
        .inject(super::fault::Stage::Begin, rusqlite::ffi::SQLITE_IOERR);
    let e = w.broker().execute(&verified, revision(), None);
    let keyed = e.permit.unwrap();
    // The same permit without its version and key id is not a valid permit/1 under the
    // keyed broker (and the MAC would not match anyway).
    let mut v = keyed.to_value();
    if let Value::Obj(m) = &mut v {
        m.remove("contract_version");
        m.remove("key_id");
    }
    let stripped = writ_broker::permit::Permit::from_value(&v).unwrap();
    assert_eq!(
        w.broker().present_permit(&stripped, &req),
        Refusal::PermitInvalid
    );
}

/// M5a-13 L9: a material-scope revocation naming another purpose than an existing
/// revocation of the same digest refuses (it never "succeeds" by returning the other row).
#[test]
fn a_material_revocation_naming_another_purpose_refuses() {
    let dir = tmp("material-purpose");
    let l = SqliteLedger::create(&dir.join("ledger.db")).unwrap();
    let scope = |purpose| RevocationScope::Material {
        purpose,
        digest: digest(7),
    };
    // Pre-emptive revocation of an unregistered digest as a permit key.
    let first = l
        .revoke_key(&scope(KeyPurpose::Permit), "leaked", &actor(), now())
        .unwrap()
        .unwrap();
    assert!(first.created);
    // The same digest named as an ingress key: refused, nothing recorded.
    assert_eq!(
        l.revoke_key(&scope(KeyPurpose::Ingress), "leaked", &actor(), now())
            .unwrap(),
        Err(RevokeRefusal::PurposeMismatch)
    );
    assert_eq!(count(&l, "SELECT count(*) FROM key_revocations"), 1);
    // The identical revocation still returns the existing row.
    let again = l
        .revoke_key(&scope(KeyPurpose::Permit), "again", &actor(), now())
        .unwrap()
        .unwrap();
    assert!(!again.created);
    assert_eq!(again.revocation_id, first.revocation_id);
    assert!(writ_broker::keys::OsActor::from_document(&again.actor).is_some());
}
