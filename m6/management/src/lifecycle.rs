use writ_kernel::json::{Map, Value};

use crate::{Result, Scope, Store};

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum EvaluatorOperation {
    Inspect,
    PreviewPublish,
    PreviewRevoke,
    Publish,
    Revoke,
    Recover,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum WaiverOperation {
    Request,
    Begin,
    Finish,
    Inspect,
    Validate,
    Recover,
}

pub trait EvaluatorBoundary {
    fn execute(
        &self,
        call: &LifecycleCall<'_>,
        operation: EvaluatorOperation,
        fields: Map,
    ) -> Result<Value>;
}

pub trait WaiverBoundary {
    fn execute(
        &self,
        call: &LifecycleCall<'_>,
        operation: WaiverOperation,
        fields: Map,
    ) -> Result<Value>;
}

pub struct LifecycleCall<'a> {
    pub store: &'a Store,
    pub scope: &'a Scope,
    pub subject: &'a str,
    pub reauthorize: &'a dyn Fn() -> Result<()>,
}

#[derive(Clone, Copy)]
pub enum LifecycleKind {
    Evaluator,
    Waiver,
}

impl LifecycleKind {
    fn name(self) -> &'static str {
        match self {
            Self::Evaluator => "evaluator",
            Self::Waiver => "waiver",
        }
    }
}

const INTENTS: &str = "
CREATE TABLE IF NOT EXISTS lifecycle_intents (
    tenant TEXT NOT NULL,
    project TEXT NOT NULL,
    subject TEXT NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN ('evaluator', 'waiver')),
    key_digest TEXT NOT NULL,
    request_digest TEXT NOT NULL,
    correlation TEXT NOT NULL,
    PRIMARY KEY (tenant, project, subject, kind, key_digest)
) STRICT, WITHOUT ROWID";

impl LifecycleCall<'_> {
    /// Record a dispatch before crossing stores. An existing intent may only be recovered.
    pub fn reserve(
        &self,
        kind: LifecycleKind,
        key: &str,
        binding: &Value,
        correlation: &Value,
    ) -> Result<bool> {
        use crate::{valid_label, Error};
        use rusqlite::{params, OptionalExtension, TransactionBehavior};
        use writ_kernel::json::{self, Digest};

        if !valid_label(key) {
            return Err(Error::InvalidRequest);
        }
        let key_digest = Digest::of_bytes(key.as_bytes());
        let request_digest = json::digest(binding).map_err(|_| Error::InvalidRequest)?;
        let correlation = json::canonical(correlation).map_err(|_| Error::InvalidRequest)?;
        if correlation.len() > 4096 {
            return Err(Error::LimitExceeded);
        }

        let mut db = self.store.connection(true)?;
        let tx = db
            .transaction_with_behavior(TransactionBehavior::Immediate)
            .map_err(|_| Error::Unavailable)?;
        (self.reauthorize)()?;
        tx.execute_batch(INTENTS).map_err(|_| Error::Unavailable)?;
        let identity = params![
            self.scope.tenant,
            self.scope.project,
            self.subject,
            kind.name(),
            key_digest.as_str()
        ];
        let previous = tx
            .query_row(
                "SELECT request_digest, correlation FROM lifecycle_intents
                 WHERE tenant=?1 AND project=?2 AND subject=?3 AND kind=?4 AND key_digest=?5",
                identity,
                |row| Ok((row.get::<_, String>(0)?, row.get::<_, String>(1)?)),
            )
            .optional()
            .map_err(|_| Error::Unavailable)?;
        if let Some((digest, existing)) = previous {
            return if digest == request_digest.as_str() && existing == correlation {
                Ok(false)
            } else {
                Err(Error::IdempotencyConflict)
            };
        }
        let count: i64 = tx
            .query_row("SELECT count(*) FROM lifecycle_intents", [], |row| {
                row.get(0)
            })
            .map_err(|_| Error::Unavailable)?;
        if count >= 8192 {
            return Err(Error::LimitExceeded);
        }
        tx.execute(
            "INSERT INTO lifecycle_intents VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7)",
            params![
                self.scope.tenant,
                self.scope.project,
                self.subject,
                kind.name(),
                key_digest.as_str(),
                request_digest.as_str(),
                correlation
            ],
        )
        .map_err(|_| Error::Unavailable)?;
        tx.commit().map_err(|_| Error::Unavailable)?;
        Ok(true)
    }

    /// Resolve only the original caller's scoped correlation. This is not an owning receipt.
    pub fn correlation(&self, kind: LifecycleKind, key: &str) -> Result<Value> {
        use crate::{valid_label, Error};
        use rusqlite::{params, OptionalExtension};
        use writ_kernel::json::{self, Digest};

        if !valid_label(key) {
            return Err(Error::InvalidRequest);
        }
        let db = self.store.connection(false)?;
        let exists: bool = db
            .query_row(
                "SELECT EXISTS (SELECT 1 FROM sqlite_schema
                 WHERE type='table' AND name='lifecycle_intents')",
                [],
                |row| row.get(0),
            )
            .map_err(|_| Error::Unavailable)?;
        if !exists {
            return Err(Error::NotFound);
        }
        let key = Digest::of_bytes(key.as_bytes());
        let correlation: String = db
            .query_row(
                "SELECT correlation FROM lifecycle_intents
                 WHERE tenant=?1 AND project=?2 AND subject=?3 AND kind=?4 AND key_digest=?5",
                params![
                    self.scope.tenant,
                    self.scope.project,
                    self.subject,
                    kind.name(),
                    key.as_str()
                ],
                |row| row.get(0),
            )
            .optional()
            .map_err(|_| Error::Unavailable)?
            .ok_or(Error::NotFound)?;
        json::parse(correlation.as_bytes()).map_err(|_| Error::Unavailable)
    }
}
