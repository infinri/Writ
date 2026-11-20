//! Read-only adapter for a host-owned management credential and grant catalog.
use crate::{valid_label, Authenticator, Grant, PinnedPrincipal, Principal, Realm, Role, Scope};
use rusqlite::{Connection, OpenFlags};
use std::path::PathBuf;
use std::time::{SystemTime, UNIX_EPOCH};
use writ_kernel::json::Digest;

pub const CATALOG_SCHEMA: &str = include_str!("identity.sql");

pub struct CatalogAuthority(pub PathBuf);

struct Session {
    _transaction: Connection,
    principal: Principal,
    issued: i64,
    expires: i64,
}

impl PinnedPrincipal for Session {
    fn principal(&self) -> &Principal {
        &self.principal
    }

    fn still_valid(&self) -> bool {
        SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .ok()
            .and_then(|elapsed| i64::try_from(elapsed.as_secs()).ok())
            .is_some_and(|now| (self.issued..self.expires).contains(&now))
    }
}

impl CatalogAuthority {
    fn session(&self, token: &[u8]) -> Option<Session> {
        if !(32..=crate::MAX_CREDENTIAL).contains(&token.len()) {
            return None;
        }
        if !std::fs::symlink_metadata(&self.0).ok()?.is_file() {
            return None;
        }
        let db = Connection::open_with_flags(
            &self.0,
            OpenFlags::SQLITE_OPEN_READ_ONLY | OpenFlags::SQLITE_OPEN_NOFOLLOW,
        )
        .ok()?;
        db.busy_timeout(std::time::Duration::from_millis(200))
            .ok()?;
        db.execute_batch("PRAGMA trusted_schema=OFF; BEGIN").ok()?;
        if db
            .query_row("PRAGMA application_id", [], |row| row.get::<_, i64>(0))
            .ok()?
            != 1464421681
            || db
                .query_row("PRAGMA user_version", [], |row| row.get::<_, i64>(0))
                .ok()?
                != 1
        {
            return None;
        }
        let fingerprint = Digest::of_bytes(token);
        let (subject, realm, issued, expires): (String,String,i64,i64) = db.query_row(
            "SELECT p.subject,p.realm,c.issued,c.expires FROM credentials c JOIN principals p USING(subject)
             WHERE c.fingerprint=?1 AND p.active=1 AND c.revoked=0",
            [fingerprint.as_str()], |r| Ok((r.get(0)?,r.get(1)?,r.get(2)?,r.get(3)?))
        ).ok()?;
        // Only rollback journaling orders a credential writer's commit against this read guard.
        let mode: String = db
            .query_row("PRAGMA journal_mode", [], |row| row.get(0))
            .ok()?;
        if !matches!(mode.as_str(), "delete" | "persist" | "truncate") || !valid_label(&subject) {
            return None;
        }
        let realm = match realm.as_str() {
            "management" => Realm::Management,
            "management-service" => Realm::ManagementService,
            _ => return None,
        };
        let mut grants = Vec::new();
        {
            let mut query = db
                .prepare("SELECT tenant,project,role FROM scoped_grants WHERE subject=? LIMIT 129")
                .ok()?;
            let mut rows = query.query([&subject]).ok()?;
            while let Some(row) = rows.next().ok()? {
                let scope = Scope {
                    tenant: row.get(0).ok()?,
                    project: row.get(1).ok()?,
                };
                if !valid_label(&scope.tenant)
                    || !valid_label(&scope.project)
                    || grants.len() >= 128
                {
                    return None;
                }
                let role = match row.get::<_, String>(2).ok()?.as_str() {
                    "viewer" => Role::Viewer,
                    "policy-author" => Role::PolicyAuthor,
                    "publisher" => Role::Publisher,
                    "waiver-operator" => Role::WaiverOperator,
                    "security-auditor" => Role::SecurityAuditor,
                    _ => return None,
                };
                grants.push(Grant { scope, role });
            }
        }
        let session = Session {
            _transaction: db,
            principal: Principal {
                subject,
                realm,
                grants,
            },
            issued,
            expires,
        };
        session.still_valid().then_some(session)
    }
}

impl Authenticator for CatalogAuthority {
    fn pin_requests(&self) -> bool {
        true
    }

    fn credential_metadata(&self, credential: &[u8]) -> Option<writ_kernel::json::Value> {
        use writ_kernel::json::{self, Value};
        let session = self.session(credential)?;
        Some(json::obj([
            (
                "fingerprint",
                json::s(Digest::of_bytes(credential).as_str()),
            ),
            ("subject", json::s(&session.principal.subject)),
            ("purpose", json::s("management-authentication")),
            ("status", json::s("active")),
            ("created", Value::Int(session.issued)),
            ("expires", Value::Int(session.expires)),
            (
                "last_used",
                Value::Int(
                    i64::try_from(SystemTime::now().duration_since(UNIX_EPOCH).ok()?.as_secs())
                        .ok()?,
                ),
            ),
        ]))
    }

    fn authenticate(&self, credential: &[u8]) -> Option<Principal> {
        self.session(credential).map(|session| session.principal)
    }

    fn pin(&self, credential: &[u8]) -> Option<Box<dyn PinnedPrincipal + '_>> {
        Some(Box::new(self.session(credential)?))
    }
}
