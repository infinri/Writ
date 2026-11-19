use writ_broker::{
    acceptance::AcceptanceCode,
    effects::EffectAdapter,
    evaluator_publication::{
        EvaluatorDependencies, EvaluatorIntent, EvaluatorPublisher, ManagedEvaluatorAuthorization,
    },
    execute::Broker,
    refusal::Refusal,
    store::StoreError,
    workflow::{AdminPresentation, AdminRequest},
};
use writ_kernel::{
    ids,
    json::{self, Digest, Map, Value},
};
#[cfg(feature = "fixtures")]
use writ_ledger_sqlite::EvaluatorRegistryReader;
use writ_ledger_sqlite::{EvaluatorRegistryRecord, EvaluatorRegistrySnapshot, SqliteLedger};

use crate::{
    lifecycle::{EvaluatorBoundary, EvaluatorOperation, LifecycleCall, LifecycleKind},
    valid_label, Error, Result, Scope,
};

const MAX_HISTORY: i64 = 50;
const MAX_PRESENTATION: usize = 512;

pub struct ManagedEvaluator<'a, A: ?Sized, H> {
    scope: &'a Scope,
    ledger: &'a SqliteLedger,
    broker: &'a Broker<'a, SqliteLedger, A>,
    authority: &'a H,
    dependencies: EvaluatorDependencies<'a>,
    publisher_channel: &'a str,
}

impl<'a, A: EffectAdapter + ?Sized, H: EvaluatorPublisher> ManagedEvaluator<'a, A, H> {
    pub fn new(
        scope: &'a Scope,
        ledger: &'a SqliteLedger,
        broker: &'a Broker<'a, SqliteLedger, A>,
        authority: &'a H,
        dependencies: EvaluatorDependencies<'a>,
        publisher_channel: &'a str,
    ) -> Result<Self> {
        if !broker.uses_evaluator_ledger(ledger) {
            return Err(Error::Unavailable);
        }
        Ok(Self {
            scope,
            ledger,
            broker,
            authority,
            dependencies,
            publisher_channel,
        })
    }
}

impl<A: EffectAdapter + ?Sized, H: EvaluatorPublisher> EvaluatorBoundary
    for ManagedEvaluator<'_, A, H>
{
    fn execute(
        &self,
        call: &LifecycleCall<'_>,
        operation: EvaluatorOperation,
        mut fields: Map,
    ) -> Result<Value> {
        use EvaluatorOperation::*;
        if call.scope != self.scope {
            return Err(Error::NotFound);
        }
        match operation {
            Publish | Revoke => {
                let intent = exact_intent(call, &mut fields, operation == Publish)?;
                let intent_digest = string(&mut fields, "intent_digest")?;
                let key = key(&mut fields)?;
                let presentation = string(&mut fields, "presentation")?;
                complete(&fields)?;
                if presentation.is_empty() || presentation.len() > MAX_PRESENTATION {
                    return Err(Error::InvalidRequest);
                }
                if intent_digest != intent.fingerprint() {
                    return Err(Error::RevisionConflict);
                }
                self.change(call, &intent, &key, presentation.as_bytes())
            }
            _ => read_operation(
                RegistryRead::Owned(self.ledger),
                &self.dependencies,
                call,
                operation,
                fields,
            ),
        }
    }
}

pub(crate) enum RegistryRead<'a> {
    Owned(&'a SqliteLedger),
    #[cfg(feature = "fixtures")]
    ReadOnly(&'a EvaluatorRegistryReader),
}

impl RegistryRead<'_> {
    fn history(
        &self,
        scope: &Scope,
        evaluator: &str,
        after: i64,
        limit: usize,
    ) -> std::result::Result<EvaluatorRegistrySnapshot, StoreError> {
        let (tenant, project) = (&scope.tenant, &scope.project);
        match self {
            Self::Owned(ledger) => {
                ledger.evaluator_registry_history(tenant, project, evaluator, after, limit)
            }
            #[cfg(feature = "fixtures")]
            Self::ReadOnly(ledger) => ledger.history(tenant, project, evaluator, after, limit),
        }
    }

    fn receipt(
        &self,
        scope: &Scope,
        intent: &str,
        correlation: &str,
    ) -> std::result::Result<Option<EvaluatorRegistryRecord>, StoreError> {
        let (tenant, project) = (&scope.tenant, &scope.project);
        match self {
            Self::Owned(ledger) => {
                ledger.evaluator_management_receipt(tenant, project, intent, correlation)
            }
            #[cfg(feature = "fixtures")]
            Self::ReadOnly(ledger) => ledger.receipt(tenant, project, intent, correlation),
        }
    }
}

pub(crate) fn read_operation(
    registry: RegistryRead<'_>,
    dependencies: &EvaluatorDependencies<'_>,
    call: &LifecycleCall<'_>,
    operation: EvaluatorOperation,
    mut fields: Map,
) -> Result<Value> {
    use EvaluatorOperation::*;
    match operation {
        Inspect => {
            let evaluator = evaluator_id(&mut fields)?;
            let after = number(&mut fields, "after", 0, json::MAX_SAFE_INTEGER)?;
            let limit = number(&mut fields, "limit", 1, MAX_HISTORY)? as usize;
            complete(&fields)?;
            let EvaluatorRegistrySnapshot {
                revision,
                current,
                mut records,
            } = registry
                .history(call.scope, &evaluator, after, limit + 1)
                .map_err(|_| Error::Unavailable)?;
            let has_next = records.len() > limit;
            records.truncate(limit);
            let next = if has_next {
                records
                    .last()
                    .map_or(Value::Null, |row| Value::Int(row.revision))
            } else {
                Value::Null
            };
            let (entry, key_revoked) = current
                .map_or((Value::Null, Value::Null), |(entry, revoked)| {
                    (entry.as_value().clone(), Value::Bool(revoked))
                });
            Ok(json::obj([
                ("revision", Value::Int(revision)),
                ("entry", entry),
                ("runner_key_revoked", key_revoked),
                (
                    "history",
                    Value::Arr(records.into_iter().map(receipt).collect()),
                ),
                ("next", next),
            ]))
        }
        Recover => {
            let key = key(&mut fields)?;
            complete(&fields)?;
            let record = call.correlation(LifecycleKind::Evaluator, &key)?;
            recover(&registry, call, &record)
        }
        PreviewPublish | PreviewRevoke => {
            let intent = exact_intent(call, &mut fields, operation == PreviewPublish)?;
            complete(&fields)?;
            preview(&registry, dependencies, &intent, call)
        }
        _ => Err(Error::InvalidRequest),
    }
}

fn preview(
    registry: &RegistryRead<'_>,
    dependencies: &EvaluatorDependencies<'_>,
    intent: &EvaluatorIntent,
    call: &LifecycleCall<'_>,
) -> Result<Value> {
    let evaluator = match intent.request() {
        AdminRequest::RegisterEvaluator { entry } => entry
            .get("evaluator_id")
            .and_then(Value::as_str)
            .ok_or(Error::InvalidRequest)?,
        AdminRequest::RevokeEvaluator { evaluator_id, .. } => evaluator_id,
        _ => return Err(Error::InvalidRequest),
    };
    let snapshot = registry
        .history(call.scope, evaluator, json::MAX_SAFE_INTEGER, 1)
        .map_err(|_| Error::Unavailable)?;
    if snapshot.revision != intent.expected_revision() {
        return Err(Error::RevisionConflict);
    }
    if matches!(intent.request(), AdminRequest::RevokeEvaluator { .. })
        && !snapshot.current.as_ref().is_some_and(|(entry, _)| {
            entry.as_value().get("status").and_then(Value::as_str) == Some("active")
        })
    {
        return Err(Error::NotFound);
    }
    dependencies.preview(intent).map_err(refusal)?;
    (call.reauthorize)()?;
    Ok(json::obj([
        ("intent", intent.as_value().clone()),
        ("intent_digest", json::s(intent.fingerprint())),
    ]))
}

impl<A: EffectAdapter + ?Sized, H: EvaluatorPublisher> ManagedEvaluator<'_, A, H> {
    fn change(
        &self,
        call: &LifecycleCall<'_>,
        intent: &EvaluatorIntent,
        key: &str,
        presentation: &[u8],
    ) -> Result<Value> {
        let binding = json::obj([
            ("version", json::s("writ-managed-evaluator-attempt/1")),
            ("ledger", json::s(self.ledger.ledger_id().as_str())),
            ("tenant", json::s(&call.scope.tenant)),
            ("project", json::s(&call.scope.project)),
            ("subject", json::s(call.subject)),
            ("key", json::s(key)),
            ("intent_digest", json::s(intent.fingerprint())),
            (
                "presentation_digest",
                json::s(Digest::of_bytes(presentation).as_str()),
            ),
        ]);
        let correlation = json::digest(&binding).map_err(|_| Error::InvalidRequest)?;
        let owner_identity = json::obj([
            ("correlation", json::s(correlation.as_str())),
            ("intent_digest", json::s(intent.fingerprint())),
        ]);
        (call.reauthorize)()?;
        if !call.reserve(LifecycleKind::Evaluator, key, &binding, &owner_identity)? {
            return recover(&RegistryRead::Owned(self.ledger), call, &owner_identity);
        }
        let reauthorize = || {
            (call.reauthorize)().map_err(|_| Refusal::OperatorUnauthorized)?;
            let current = call
                .correlation(LifecycleKind::Evaluator, key)
                .map_err(|_| Refusal::OperatorUnauthorized)?;
            if current != owner_identity {
                return Err(Refusal::OperatorUnauthorized);
            }
            Ok(())
        };
        let management = ManagedEvaluatorAuthorization {
            correlation: &correlation,
            reauthorize: &reauthorize,
        };
        let presentation = AdminPresentation {
            channel: self.publisher_channel,
            operator: call.subject,
            credential: presentation,
        };
        match self.broker.publish_evaluator_managed(
            self.authority,
            &presentation,
            &self.dependencies,
            intent,
            &management,
        ) {
            Ok(Err(error)) => Err(refusal(error)),
            Ok(Ok(())) => recover(&RegistryRead::Owned(self.ledger), call, &owner_identity),
            Err(_) => Err(Error::Unavailable),
        }
    }
}

fn recover(registry: &RegistryRead<'_>, call: &LifecycleCall<'_>, record: &Value) -> Result<Value> {
    let digest = |name| {
        record
            .get(name)
            .and_then(Value::as_str)
            .filter(|value| ids::is_digest(value))
            .ok_or(Error::Unavailable)
    };
    let committed = registry
        .receipt(call.scope, digest("intent_digest")?, digest("correlation")?)
        .map_err(|_| Error::Unavailable)?
        .ok_or(Error::Unavailable)?;
    Ok(receipt(committed))
}

fn exact_intent(
    call: &LifecycleCall<'_>,
    fields: &mut Map,
    publish: bool,
) -> Result<EvaluatorIntent> {
    let expected = number(fields, "expected_revision", 0, json::MAX_SAFE_INTEGER - 1)?;
    let request = if publish {
        let entry = fields.remove("entry").ok_or(Error::InvalidRequest)?;
        let scope = entry.get("scope").ok_or(Error::InvalidRequest)?;
        if scope.get("tenant_id").and_then(Value::as_str) != Some(call.scope.tenant.as_str())
            || scope.get("project_id").and_then(Value::as_str) != Some(call.scope.project.as_str())
        {
            return Err(Error::NotFound);
        }
        if entry.get("publisher").and_then(Value::as_str) != Some(call.subject) {
            return Err(Error::Unauthorized);
        }
        AdminRequest::RegisterEvaluator { entry }
    } else {
        AdminRequest::RevokeEvaluator {
            tenant_id: call.scope.tenant.clone(),
            project_id: call.scope.project.clone(),
            evaluator_id: evaluator_id(fields)?,
        }
    };
    EvaluatorIntent::new(expected, request).map_err(refusal)
}

fn complete(fields: &Map) -> Result<()> {
    if fields.is_empty() {
        Ok(())
    } else {
        Err(Error::InvalidRequest)
    }
}

fn evaluator_id(fields: &mut Map) -> Result<String> {
    let id = string(fields, "evaluator_id")?;
    ids::is_name(&id).then_some(id).ok_or(Error::InvalidRequest)
}

fn key(fields: &mut Map) -> Result<String> {
    let key = string(fields, "idempotency_key")?;
    valid_label(&key)
        .then_some(key)
        .ok_or(Error::InvalidRequest)
}

fn string(fields: &mut Map, name: &str) -> Result<String> {
    match fields.remove(name) {
        Some(Value::Str(value)) => Ok(value),
        _ => Err(Error::InvalidRequest),
    }
}

fn number(fields: &mut Map, name: &str, min: i64, max: i64) -> Result<i64> {
    match fields.remove(name) {
        Some(Value::Int(value)) if (min..=max).contains(&value) => Ok(value),
        _ => Err(Error::InvalidRequest),
    }
}

fn receipt(record: EvaluatorRegistryRecord) -> Value {
    json::obj([
        ("revision", Value::Int(record.revision)),
        ("sequence", Value::Int(record.sequence)),
        ("evaluator_id", json::s(&record.evaluator_id)),
        ("status", json::s(record.status)),
        ("intent_digest", json::s(&record.intent_digest)),
        ("entry_digest", json::s(&record.entry_digest)),
        (
            "management_correlation",
            record
                .management_correlation
                .map_or(Value::Null, Value::Str),
        ),
    ])
}

fn refusal(error: Refusal) -> Error {
    match error {
        Refusal::AdminRequestInvalid => Error::InvalidRequest,
        Refusal::OperatorUnauthorized | Refusal::OperatorUnauthenticated => Error::Unauthorized,
        Refusal::RegistryRevisionStale => Error::RevisionConflict,
        Refusal::Acceptance(AcceptanceCode::EvaluatorBindingMismatch) => Error::InvalidPolicy,
        Refusal::Acceptance(
            AcceptanceCode::EvaluatorUnregistered | AcceptanceCode::EvaluatorRevoked,
        ) => Error::NotFound,
        _ => Error::Unavailable,
    }
}
