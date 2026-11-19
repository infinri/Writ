use rusqlite::{params, Connection, OptionalExtension, TransactionBehavior};
use writ_kernel::json::{self, Digest, Value};
use writ_policy_publication_lab::{
    Audit, Commit, Error as RegistryError, Intent, Operation, Publication,
};

use crate::{
    store::{authors, current_revision, load},
    DraftRef, Error, PublicationBoundary, Result, Scope, Store, Version,
};

const SCHEMA: &str = include_str!("publication.sql");
const CONTEXT_VERSION: &str = "writ-managed-publication-context/1";

pub enum PublicationRequest {
    PreviewPublish {
        draft: DraftRef,
        expected_revision: i64,
        publication_version: i64,
        candidate: Publication,
        acknowledge_self: bool,
        key: String,
    },
    PreviewRollback {
        expected_revision: i64,
        publication_version: i64,
        acknowledge_self: bool,
        key: String,
    },
    Publish {
        draft: DraftRef,
        expected_revision: i64,
        publication_version: i64,
        candidate: Publication,
        acknowledge_self: bool,
        intent_digest: String,
        key: String,
        presentation: Vec<u8>,
    },
    Rollback {
        expected_revision: i64,
        publication_version: i64,
        acknowledge_self: bool,
        intent_digest: String,
        key: String,
        presentation: Vec<u8>,
    },
    History {
        after: i64,
        limit: usize,
    },
}

enum Target {
    Publish(DraftRef, Publication),
    Rollback,
}

impl Target {
    fn operation(&self) -> Operation {
        match self {
            Self::Publish(..) => Operation::Publish,
            Self::Rollback => Operation::Rollback,
        }
    }

    fn candidate(&self) -> Option<&Publication> {
        match self {
            Self::Publish(_, candidate) => Some(candidate),
            Self::Rollback => None,
        }
    }
}

struct Plan {
    target: Target,
    expected_revision: i64,
    version: i64,
    acknowledge_self: bool,
    key: Digest,
}

struct Submission {
    intent_digest: String,
    presentation: Vec<u8>,
}

struct Context {
    document: Value,
    digest: Digest,
    draft: DraftRef,
    draft_head: i64,
}

struct Proposed {
    intent: Intent,
    intent_digest: Digest,
    context: Context,
}

pub(crate) fn execute(
    store: &Store,
    registry: &dyn PublicationBoundary,
    scope: &Scope,
    subject: &str,
    request: PublicationRequest,
    allow_self_publication: bool,
    reauthorize: impl Fn() -> Result<()>,
) -> Result<Value> {
    if registry.scope().tenant() != scope.tenant || registry.scope().project() != scope.project {
        return Err(Error::NotFound);
    }

    use PublicationRequest::*;
    let (target, expected_revision, version, acknowledge_self, key, submission) = match request {
        History { after, limit } => return history(store, registry, scope, after, limit),
        PreviewPublish {
            draft,
            expected_revision,
            publication_version,
            candidate,
            acknowledge_self,
            key,
        } => (
            Target::Publish(draft, candidate),
            expected_revision,
            publication_version,
            acknowledge_self,
            key,
            None,
        ),
        PreviewRollback {
            expected_revision,
            publication_version,
            acknowledge_self,
            key,
        } => (
            Target::Rollback,
            expected_revision,
            publication_version,
            acknowledge_self,
            key,
            None,
        ),
        Publish {
            draft,
            expected_revision,
            publication_version,
            candidate,
            acknowledge_self,
            intent_digest,
            key,
            presentation,
        } => (
            Target::Publish(draft, candidate),
            expected_revision,
            publication_version,
            acknowledge_self,
            key,
            Some(Submission {
                intent_digest,
                presentation,
            }),
        ),
        Rollback {
            expected_revision,
            publication_version,
            acknowledge_self,
            intent_digest,
            key,
            presentation,
        } => (
            Target::Rollback,
            expected_revision,
            publication_version,
            acknowledge_self,
            key,
            Some(Submission {
                intent_digest,
                presentation,
            }),
        ),
    };
    let plan = Plan {
        target,
        expected_revision,
        version,
        acknowledge_self,
        key: Digest::of_bytes(key.as_bytes()),
    };
    let coordinator = Coordinator {
        store,
        registry,
        scope,
        subject,
        allow_self_publication,
    };
    if let Some(submission) = submission {
        return coordinator.change(plan, submission, reauthorize);
    }

    let mut db = store.connection(false)?;
    let tx = db.transaction().map_err(unavailable)?;
    let proposed = coordinator.propose(&tx, &plan)?;
    reauthorize()?;
    Ok(json::obj([
        ("intent", proposed.intent.to_value()),
        ("intent_digest", json::s(proposed.intent_digest.as_str())),
        ("context", proposed.context.document),
        ("revision", Value::Int(plan.expected_revision + 1)),
        ("publication_version", Value::Int(plan.version)),
    ]))
}

struct Coordinator<'a> {
    store: &'a Store,
    registry: &'a dyn PublicationBoundary,
    scope: &'a Scope,
    subject: &'a str,
    allow_self_publication: bool,
}

impl Coordinator<'_> {
    fn propose(&self, db: &Connection, plan: &Plan) -> Result<Proposed> {
        let target = self
            .registry
            .preview(
                plan.expected_revision,
                plan.version,
                plan.target.candidate(),
            )
            .map_err(registry_error)?;
        let draft = match &plan.target {
            Target::Publish(draft, _) => draft.clone(),
            Target::Rollback => {
                let audit = self.registry.audit().map_err(registry_error)?;
                let source = audit
                    .iter()
                    .find(|event| {
                        event.operation == Operation::Publish
                            && event.publication_version == plan.version
                    })
                    .ok_or(Error::NotFound)?;
                let context = event_context(db, self.scope, source)?;
                draft_ref(&context).ok_or(Error::NotFound)?
            }
        };
        let draft_head = current_revision(db, self.scope, &draft.id)?;
        if matches!(plan.target, Target::Publish(..)) && draft_head != draft.revision {
            return Err(Error::RevisionConflict);
        }
        let row = load(db, self.scope, &draft)?;
        if target.prerequisites.get("draft") != Some(&row.candidate) {
            return Err(Error::InvalidPolicy);
        }
        let proof = target
            .prerequisites
            .get("proof_digest")
            .and_then(Value::as_str)
            .ok_or(Error::InvalidPolicy)?;
        let version: Version = row.try_into()?;
        let self_publication = authors(db, self.scope, &draft)?
            .iter()
            .any(|author| author == self.subject);
        if self_publication && (!plan.acknowledge_self || !self.allow_self_publication) {
            return Err(Error::Unauthorized);
        }
        if plan.acknowledge_self && !self_publication {
            return Err(Error::InvalidRequest);
        }
        let document = json::obj([
            ("version", json::s(CONTEXT_VERSION)),
            ("scope", self.registry.scope().to_value()),
            ("publisher", json::s(self.subject)),
            ("draft", draft_value(&draft)),
            ("draft_digest", json::s(&version.digest)),
            ("draft_head_revision", Value::Int(draft_head)),
            ("proof_digest", json::s(proof)),
            ("idempotency_key_digest", json::s(plan.key.as_str())),
            (
                "self_publication_acknowledged",
                Value::Bool(self_publication),
            ),
        ]);
        let digest = value_digest(&document)?;
        let intent = Intent::new(
            self.registry.scope().clone(),
            plan.target.operation(),
            plan.expected_revision,
            plan.version,
            &target,
        )
        .and_then(|intent| intent.with_management(digest.clone(), self.subject))
        .map_err(registry_error)?;
        let intent_digest = intent.fingerprint().map_err(registry_error)?;
        Ok(Proposed {
            intent,
            intent_digest,
            context: Context {
                document,
                digest,
                draft,
                draft_head,
            },
        })
    }

    fn change(
        &self,
        plan: Plan,
        submission: Submission,
        reauthorize: impl Fn() -> Result<()>,
    ) -> Result<Value> {
        let request = request_digest(&plan, &submission)?;
        let mut db = self.store.connection(true)?;
        let tx = db
            .transaction_with_behavior(TransactionBehavior::Immediate)
            .map_err(unavailable)?;
        if let Some(attempt) = self.attempt(&tx, &plan.key)? {
            reauthorize()?;
            if attempt.request != request.as_str() {
                return Err(Error::IdempotencyConflict);
            }
            let commit = self.recover(&tx, &attempt)?;
            return Ok(receipt(&commit, &attempt.intent));
        }
        let used_by_draft: bool = tx
            .query_row(
                "SELECT EXISTS (SELECT 1 FROM draft_audit
                 WHERE tenant = ?1 AND project = ?2 AND subject = ?3 AND key_digest = ?4)",
                params![
                    self.scope.tenant,
                    self.scope.project,
                    self.subject,
                    plan.key.as_str()
                ],
                |row| row.get(0),
            )
            .map_err(unavailable)?;
        if used_by_draft {
            return Err(Error::IdempotencyConflict);
        }
        let proposed = self.propose(&tx, &plan)?;
        if proposed.intent_digest.as_str() != submission.intent_digest {
            return Err(Error::RevisionConflict);
        }
        reauthorize()?;
        tx.execute_batch(SCHEMA).map_err(unavailable)?;
        let context = json::canonical(&proposed.context.document).map_err(unavailable)?;
        tx.execute(
            "INSERT INTO publication_intents (tenant, project, subject, key_digest, request_digest,
             intent_digest, context_digest, context) VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8)",
            params![
                self.scope.tenant,
                self.scope.project,
                self.subject,
                plan.key.as_str(),
                request.as_str(),
                proposed.intent_digest.as_str(),
                proposed.context.digest.as_str(),
                context
            ],
        )
        .map_err(unavailable)?;
        tx.commit().map_err(unavailable)?;

        // Keep draft writes ordered against registry commit, without making the two
        // database transactions a single atomic domain.
        let tx = db
            .transaction_with_behavior(TransactionBehavior::Immediate)
            .map_err(unavailable)?;
        let outcome = (|| {
            let actual = current_revision(&tx, self.scope, &proposed.context.draft.id)?;
            if actual != proposed.context.draft_head {
                return Err(Error::RevisionConflict);
            }
            reauthorize()?;
            self.store
                .with_publication_dispatch(&proposed.intent, &submission.presentation, || {
                    self.registry
                        .change(
                            &proposed.intent,
                            plan.target.candidate(),
                            &submission.presentation,
                            &reauthorize,
                        )
                        .map_err(registry_error)
                })
        })();
        if let Err(error) = outcome {
            if let Some(code) = refusal_code(error) {
                tx.execute(
                    "UPDATE publication_intents SET refusal = ?1
                     WHERE tenant = ?2 AND project = ?3 AND subject = ?4 AND key_digest = ?5",
                    params![
                        code,
                        self.scope.tenant,
                        self.scope.project,
                        self.subject,
                        plan.key.as_str()
                    ],
                )
                .map_err(unavailable)?;
            }
        }
        tx.commit().map_err(unavailable)?;
        let reported = outcome?;
        let attempt = Attempt {
            request: request.to_string(),
            intent: proposed.intent_digest.to_string(),
            context: proposed.context.digest.to_string(),
            refusal: None,
        };
        let commit = self.recover(&db, &attempt)?;
        if reported != commit {
            return Err(Error::Unavailable);
        }
        Ok(receipt(&commit, &attempt.intent))
    }

    fn attempt(&self, db: &Connection, key: &Digest) -> Result<Option<Attempt>> {
        if !has_table(db)? {
            return Ok(None);
        }
        db.query_row(
            "SELECT request_digest, intent_digest, context_digest, refusal
             FROM publication_intents WHERE tenant = ?1 AND project = ?2 AND subject = ?3 AND key_digest = ?4",
            params![self.scope.tenant, self.scope.project, self.subject, key.as_str()],
            |row| Ok(Attempt {request: row.get(0)?, intent: row.get(1)?, context: row.get(2)?, refusal: row.get(3)? }),
        ).optional().map_err(unavailable)
    }

    fn recover(&self, db: &Connection, attempt: &Attempt) -> Result<Commit> {
        let audit = self.registry.audit().map_err(registry_error)?;
        if let Some(event) = audit
            .iter()
            .find(|event| event.intent_digest == attempt.intent)
        {
            if attempt.refusal.is_some()
                || event.human != self.subject
                || event.management_digest.as_deref() != Some(attempt.context.as_str())
            {
                return Err(Error::Unavailable);
            }
            event_context(db, self.scope, event)?;
            return Ok(event_commit(event));
        }
        Err(match attempt.refusal.as_deref() {
            Some("conflict") => Error::RevisionConflict,
            Some("authority") => Error::Unauthorized,
            Some("policy") => Error::InvalidPolicy,
            Some("request") => Error::InvalidRequest,
            Some("not_found") => Error::NotFound,
            _ => Error::Unavailable,
        })
    }
}

struct Attempt {
    request: String,
    intent: String,
    context: String,
    refusal: Option<String>,
}

pub(crate) fn key_used(
    db: &Connection,
    scope: &Scope,
    subject: &str,
    key_digest: &str,
) -> Result<bool> {
    if !has_table(db)? {
        return Ok(false);
    }
    db.query_row(
        "SELECT EXISTS (SELECT 1 FROM publication_intents
         WHERE tenant = ?1 AND project = ?2 AND subject = ?3 AND key_digest = ?4)",
        params![scope.tenant, scope.project, subject, key_digest],
        |row| row.get(0),
    )
    .map_err(unavailable)
}

fn has_table(db: &Connection) -> Result<bool> {
    db.query_row(
        "SELECT EXISTS (SELECT 1 FROM sqlite_schema WHERE type = 'table' AND name = 'publication_intents')",
        [],
        |row| row.get(0),
    ).map_err(unavailable)
}

fn draft_value(draft: &DraftRef) -> Value {
    json::obj([
        ("id", json::s(&draft.id)),
        ("revision", Value::Int(draft.revision)),
    ])
}

fn draft_ref(context: &Value) -> Option<DraftRef> {
    let draft = context.get("draft")?;
    Some(DraftRef {
        id: draft.get("id")?.as_str()?.into(),
        revision: draft.get("revision")?.as_int()?,
    })
}

fn stored_context(
    db: &Connection,
    scope: &Scope,
    subject: &str,
    intent_digest: &str,
    digest: &str,
) -> Result<Value> {
    if !has_table(db)? {
        return Err(Error::Unavailable);
    }
    let (stored_digest, stored_context, key): (String, String, String) = db.query_row(
        "SELECT context_digest, context, key_digest FROM publication_intents
         WHERE tenant = ?1 AND project = ?2 AND subject = ?3 AND intent_digest = ?4 AND refusal IS NULL",
        params![scope.tenant, scope.project, subject, intent_digest],
        |row| Ok((row.get(0)?, row.get(1)?, row.get(2)?)),
    ).map_err(unavailable)?;
    if stored_digest != digest || Digest::of_bytes(stored_context.as_bytes()).as_str() != digest {
        return Err(Error::Unavailable);
    }
    let context = json::parse(stored_context.as_bytes()).map_err(unavailable)?;
    if context.get("version") != Some(&json::s(CONTEXT_VERSION))
        || context.get("publisher") != Some(&json::s(subject))
        || context.get("scope")
            != Some(&json::obj([
                ("tenant", json::s(&scope.tenant)),
                ("project", json::s(&scope.project)),
            ]))
        || context.get("idempotency_key_digest") != Some(&json::s(&key))
    {
        return Err(Error::Unavailable);
    }
    Ok(context)
}

pub(crate) fn verify_registered_intent(store: &Store, intent: &Intent) -> Result<()> {
    let scope = Scope {
        tenant: intent.scope().tenant().to_owned(),
        project: intent.scope().project().to_owned(),
    };
    let command = intent.to_value();
    let subject = command
        .get("management")
        .and_then(|management| management.get("publisher"))
        .and_then(Value::as_str)
        .ok_or(Error::Unauthorized)?;
    let digest = intent.management_digest().ok_or(Error::Unauthorized)?;
    let intent_digest = intent.fingerprint().map_err(registry_error)?;
    let db = store.connection(false)?;
    let context = stored_context(&db, &scope, subject, intent_digest.as_str(), digest)?;
    let draft = draft_ref(&context).ok_or(Error::Unavailable)?;
    let current = current_revision(&db, &scope, &draft.id)?;
    if context.get("draft_head_revision") != Some(&Value::Int(current))
        || (intent.operation() == Operation::Publish && current != draft.revision)
    {
        return Err(Error::RevisionConflict);
    }
    let version: Version = load(&db, &scope, &draft)?.try_into()?;
    if context.get("draft_digest") != Some(&json::s(&version.digest)) {
        return Err(Error::Unavailable);
    }
    let self_publication = authors(&db, &scope, &draft)?
        .iter()
        .any(|author| author == subject);
    if context.get("self_publication_acknowledged") != Some(&Value::Bool(self_publication)) {
        return Err(Error::Unauthorized);
    }
    Ok(())
}

fn event_context(db: &Connection, scope: &Scope, event: &Audit) -> Result<Value> {
    let Some(digest) = &event.management_digest else {
        return Ok(Value::Null);
    };
    stored_context(db, scope, &event.human, &event.intent_digest, digest)
}

fn history(
    store: &Store,
    registry: &dyn PublicationBoundary,
    scope: &Scope,
    after: i64,
    limit: usize,
) -> Result<Value> {
    if after < 0 || !(1..=50).contains(&limit) {
        return Err(Error::InvalidRequest);
    }
    let audit = registry.audit().map_err(registry_error)?;
    let audited_selection = audit
        .iter()
        .rev()
        .find(|event| event.operation != Operation::Retire)
        .map(event_commit);
    let verified = match registry.current_selection() {
        Ok(selected) if selected == audited_selection => true,
        Err(
            RegistryError::Trust
            | RegistryError::Time
            | RegistryError::Signature
            | RegistryError::InvalidPolicy,
        ) if audited_selection.is_some() => false,
        _ => return Err(Error::Unavailable),
    };
    let selected = audited_selection.map_or(Value::Null, |commit| {
        json::obj([
            ("revision", Value::Int(commit.revision)),
            (
                "publication_version",
                Value::Int(commit.publication_version),
            ),
            ("audit_id", Value::Int(commit.audit_sequence)),
            ("verified", Value::Bool(verified)),
        ])
    });
    let db = store.connection(false)?;
    let mut remaining = audit.iter().filter(|event| event.sequence > after);
    let mut events = Vec::with_capacity(limit);
    let mut last = after;
    for event in remaining.by_ref().take(limit) {
        last = event.sequence;
        events.push(json::obj([
            ("audit_id", Value::Int(event.sequence)),
            ("revision", Value::Int(event.revision)),
            ("operation", json::s(event.operation.as_str())),
            ("publication_version", Value::Int(event.publication_version)),
            ("publication_digest", json::s(&event.publication_digest)),
            ("intent_digest", json::s(&event.intent_digest)),
            ("human", json::s(&event.human)),
            ("at", json::s(&event.at.to_string())),
            ("context", event_context(&db, scope, event)?),
        ]));
    }
    let next = if remaining.next().is_some() {
        Value::Int(last)
    } else {
        Value::Null
    };
    Ok(json::obj([
        ("events", Value::Arr(events)),
        ("next", next),
        ("selected", selected),
    ]))
}

fn event_commit(event: &Audit) -> Commit {
    Commit {
        revision: event.revision,
        publication_version: event.publication_version,
        audit_sequence: event.sequence,
    }
}

fn receipt(commit: &Commit, intent: &str) -> Value {
    json::obj([
        ("revision", Value::Int(commit.revision)),
        (
            "publication_version",
            Value::Int(commit.publication_version),
        ),
        ("audit_id", Value::Int(commit.audit_sequence)),
        ("intent_digest", json::s(intent)),
    ])
}

fn request_digest(plan: &Plan, submission: &Submission) -> Result<Digest> {
    let draft = match &plan.target {
        Target::Publish(draft, _) => draft_value(draft),
        Target::Rollback => Value::Null,
    };
    let candidate = plan
        .target
        .candidate()
        .map(|candidate| {
            candidate
                .fingerprint()
                .map(|digest| json::s(digest.as_str()))
        })
        .transpose()
        .map_err(registry_error)?
        .unwrap_or(Value::Null);
    value_digest(&json::obj([
        ("operation", json::s(plan.target.operation().as_str())),
        ("draft", draft),
        ("candidate", candidate),
        ("expected_revision", Value::Int(plan.expected_revision)),
        ("publication_version", Value::Int(plan.version)),
        ("acknowledge_self", Value::Bool(plan.acknowledge_self)),
        ("intent_digest", json::s(&submission.intent_digest)),
        (
            "presentation_digest",
            json::s(Digest::of_bytes(&submission.presentation).as_str()),
        ),
    ]))
}

fn value_digest(value: &Value) -> Result<Digest> {
    let canonical = json::canonical(value).map_err(unavailable)?;
    Ok(Digest::of_bytes(canonical.as_bytes()))
}

fn unavailable<E>(_: E) -> Error {
    Error::Unavailable
}

fn refusal_code(error: Error) -> Option<&'static str> {
    match error {
        Error::RevisionConflict => Some("conflict"),
        Error::Unauthorized => Some("authority"),
        Error::InvalidPolicy => Some("policy"),
        Error::InvalidRequest => Some("request"),
        Error::NotFound => Some("not_found"),
        _ => None,
    }
}

fn registry_error(error: RegistryError) -> Error {
    match error {
        RegistryError::Format => Error::InvalidRequest,
        RegistryError::Conflict | RegistryError::Replay => Error::RevisionConflict,
        RegistryError::Scope | RegistryError::Retired => Error::NotFound,
        RegistryError::InvalidPolicy | RegistryError::Signature => Error::InvalidPolicy,
        RegistryError::Authority | RegistryError::Trust | RegistryError::Time => {
            Error::Unauthorized
        }
        RegistryError::Recovery | RegistryError::Storage | RegistryError::StalePermit => {
            Error::Unavailable
        }
    }
}

#[cfg(test)]
#[path = "publication-tests.rs"]
mod tests;
