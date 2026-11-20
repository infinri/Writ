#![allow(clippy::unwrap_used)]
use rusqlite::Connection;
use writ_kernel::json::Digest;
use writ_policy_management_lab::{
    identity::{CatalogAuthority, CATALOG_SCHEMA},
    Authenticator,
};

#[test]
fn credential_rotation_revocation_expiry_scope_and_pin_order() {
    let lab = std::env::temp_dir().join(format!("writ-m6-catalog-{}", std::process::id()));
    std::fs::create_dir(&lab).unwrap();
    let path = lab.join("identity.db");
    let db = Connection::open(&path).unwrap();
    db.execute_batch(CATALOG_SCHEMA).unwrap();
    db.execute_batch(
        "INSERT INTO principals VALUES('alice','management',1);
        INSERT INTO scoped_grants VALUES('alice','team','project','viewer');",
    )
    .unwrap();
    let token = b"01234567890123456789012345678901";
    let next = b"12345678901234567890123456789012";
    let issue = |credential: &[u8], expiry: i64| {
        db.execute(
            "INSERT INTO credentials VALUES(?,'alice',0,?,0)",
            rusqlite::params![Digest::of_bytes(credential).as_str(), expiry],
        )
        .unwrap();
    };
    issue(token, 4102444800);
    let owner = CatalogAuthority(path);
    assert!(owner.authenticate(b"fixture-viewer").is_none());
    let guard = owner.pin(token).unwrap();
    assert_eq!(guard.principal().grants[0].scope.tenant, "team");
    db.busy_timeout(std::time::Duration::ZERO).unwrap();
    assert!(db.execute("UPDATE credentials SET revoked=1", []).is_err());
    drop(guard);
    db.execute_batch("BEGIN; UPDATE credentials SET revoked=1;")
        .unwrap();
    issue(next, 4102444800);
    db.execute_batch("COMMIT").unwrap();
    assert!(owner.authenticate(token).is_none());
    assert!(owner.authenticate(next).is_some());
    db.execute("DELETE FROM scoped_grants", []).unwrap();
    assert!(owner.authenticate(next).unwrap().grants.is_empty());
    db.execute("UPDATE principals SET active=0", []).unwrap();
    assert!(owner.authenticate(next).is_none());
    db.execute("UPDATE principals SET active=1", []).unwrap();
    let expired = b"22222222222222222222222222222222";
    issue(expired, 1);
    assert!(owner.authenticate(expired).is_none());
    db.execute_batch("PRAGMA journal_mode=WAL").unwrap();
    assert!(owner.authenticate(next).is_none());
    assert_eq!(
        db.query_row(
            "SELECT count(*) FROM credential_audit WHERE change='credential.revoked'",
            [],
            |r| r.get::<_, i64>(0)
        )
        .unwrap(),
        1
    );
    drop(db);
    std::fs::remove_dir_all(lab).unwrap();
}
