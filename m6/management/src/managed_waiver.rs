use writ_broker::{
    approval::ApprovalCode,
    approval_store::CeremonyResult,
    effects::EffectAdapter,
    execute::Broker,
    human::SubjectId,
    ingress::WaiverTransition,
    refusal::Refusal,
    store::TxFailure,
    waiver::{WaiverBinding, WaiverRequest},
    waiver_lifecycle::WaiverReceipt,
    workflow::{WaiverInvocation, WaiverManagement, WorkflowKey},
};
use writ_kernel::{
    ids,
    json::{self, Digest, Map, Value},
};
use writ_ledger_sqlite::SqliteLedger;

use crate::{
    lifecycle::{LifecycleCall, LifecycleKind, WaiverBoundary, WaiverOperation},
    valid_label, Error, Result, Scope,
};

/// The configured ingress adapter resolves a verified transition. A request creation also
/// names the authenticated management caller, which the adapter must bind to its requester.
pub trait WaiverTransitions {
    fn resolve(
        &self,
        scope: &Scope,
        transition_ref: &str,
        management_requester: Option<&str>,
    ) -> Result<WaiverTransition>;
}

pub struct ManagedWaiver<'a, A: EffectAdapter + ?Sized> {
    ledger: &'a SqliteLedger,
    broker: &'a Broker<'a, SqliteLedger, A>,
    transitions: &'a dyn WaiverTransitions,
}

impl<'a, A: EffectAdapter + ?Sized> ManagedWaiver<'a, A> {
    pub fn new(
        ledger: &'a SqliteLedger,
        broker: &'a Broker<'a, SqliteLedger, A>,
        transitions: &'a dyn WaiverTransitions,
    ) -> Result<Self> {
        if !broker.uses_waiver_ledger(ledger) {
            return Err(Error::Unavailable);
        }
        Ok(Self {
            ledger,
            broker,
            transitions,
        })
    }
}

impl<A: EffectAdapter + ?Sized> WaiverBoundary for ManagedWaiver<'_, A> {
    fn execute(
        &self,
        call: &LifecycleCall<'_>,
        operation: WaiverOperation,
        mut fields: Map,
    ) -> Result<Value> {
        if let Some(history) = execute_history(self.ledger, call, operation, &fields) {
            return history;
        }
        let transition = take_text(&mut fields, "transition_ref", ids::is_id)?;
        let reauthorize = || (call.reauthorize)().map_err(|_| Refusal::OperatorUnauthorized);
        let management = WaiverManagement {
            reauthorize: &reauthorize,
            dispatch: None,
        };
        if operation == WaiverOperation::Request {
            let request = WaiverRequest {
                workflow: WorkflowKey {
                    tenant_id: call.scope.tenant.clone(),
                    project_id: call.scope.project.clone(),
                    workflow_id: take_text(&mut fields, "workflow_id", ids::is_name)?,
                },
                from_state: take_text(&mut fields, "from_state", ids::is_name)?,
                to_state: take_text(&mut fields, "to_state", ids::is_name)?,
                expected_revision: fields
                    .remove("expected_revision")
                    .and_then(|revision| revision.as_int())
                    .ok_or(Error::InvalidRequest)?,
                criterion_id: take_text(&mut fields, "criterion_id", ids::is_name)?,
                reason: take_text(&mut fields, "reason", |_| true)?,
            };
            if !fields.is_empty() || !request.well_formed() {
                return Err(Error::InvalidRequest);
            }
            let verified = self
                .transitions
                .resolve(call.scope, &transition, Some(call.subject))?;
            let invocation = WaiverInvocation::from_transition(&verified, &request);
            return context_value(owner(
                self.broker
                    .request_waiver_guarded(&invocation, Some(&management)),
            )?);
        }
        let waiver_id = take_text(&mut fields, "waiver_id", ids::is_digest)?;
        let context = request_in_scope(self.ledger, call.scope, &waiver_id)?;
        let verified = self.transitions.resolve(call.scope, &transition, None)?;
        let invocation = WaiverInvocation::from_transition(&verified, &context.request);
        match operation {
            WaiverOperation::Begin => {
                let approver = SubjectId::new(
                    &take_text(&mut fields, "approver_issuer", valid_label)?,
                    &take_text(&mut fields, "approver_subject", valid_label)?,
                );
                let key = take_text(&mut fields, "idempotency_key", valid_label)?;
                if !fields.is_empty() || !approver.is_well_formed() {
                    return Err(Error::InvalidRequest);
                }
                let challenge = owner(self.broker.begin_waiver_guarded(
                    &invocation,
                    &waiver_id,
                    &approver,
                    &key,
                    Some(&management),
                ))?;
                let options: String = challenge
                    .issued
                    .client_options
                    .iter()
                    .map(|byte| format!("{byte:02x}"))
                    .collect();
                Ok(json::obj([
                    ("waiver_id", json::s(&waiver_id)),
                    ("context", challenge.context.value()),
                    ("challenge_id", json::s(&challenge.issued.challenge_id)),
                    (
                        "challenge_expires_at",
                        json::s(&challenge.issued.expires_at.to_string()),
                    ),
                    ("client_options_hex", json::s(&options)),
                ]))
            }
            WaiverOperation::Finish => {
                let challenge = take_text(&mut fields, "challenge_id", ids::is_id)?;
                let owner_key = take_text(&mut fields, "idempotency_key", valid_label)?;
                let dispatch_key = take_text(&mut fields, "dispatch_key", valid_label)?;
                let presentation = take_text(&mut fields, "presentation", |value| {
                    !value.is_empty() && value.len() <= 16 * 1024
                })?;
                if !fields.is_empty() {
                    return Err(Error::InvalidRequest);
                }
                let binding = json::obj([
                    ("waiver_id", json::s(&waiver_id)),
                    ("challenge_id", json::s(&challenge)),
                    ("idempotency_key", json::s(&owner_key)),
                    ("transition_ref", json::s(&transition)),
                    (
                        "transition_digest",
                        json::s(verified.fingerprint().as_str()),
                    ),
                    (
                        "assertion_digest",
                        json::s(Digest::of_bytes(presentation.as_bytes()).as_str()),
                    ),
                ]);
                let dispatch = json::digest(&json::obj([
                    ("purpose", json::s("writ-managed-waiver/1")),
                    ("ledger", json::s(self.ledger.ledger_id().as_str())),
                    ("tenant", json::s(&call.scope.tenant)),
                    ("project", json::s(&call.scope.project)),
                    ("subject", json::s(call.subject)),
                    ("dispatch_key", json::s(&dispatch_key)),
                    ("binding", binding.clone()),
                ]))
                .map_err(|_| Error::InvalidRequest)?;
                // Only historical identity crosses stores. Never persist the assertion in management.
                let correlation = json::obj([
                    ("waiver_id", json::s(&waiver_id)),
                    ("challenge_id", json::s(&challenge)),
                    ("dispatch_digest", json::s(dispatch.as_str())),
                ]);
                if !call.reserve(LifecycleKind::Waiver, &dispatch_key, &binding, &correlation)? {
                    return recover(self.ledger, call.scope, correlation);
                }
                let management = WaiverManagement {
                    reauthorize: &reauthorize,
                    dispatch: Some(&dispatch),
                };
                let outcome = self.broker.finish_waiver_guarded(
                    &invocation,
                    &waiver_id,
                    &challenge,
                    presentation.as_bytes(),
                    &owner_key,
                    Some(&management),
                );
                let outcome = match outcome {
                    Err(failure) if failure.outcome_unknown => {
                        return recover(self.ledger, call.scope, correlation);
                    }
                    other => owner(other)?,
                };
                if outcome.result != (CeremonyResult::Waived { waiver_id }) {
                    return Err(Error::Unavailable);
                }
                let receipt = correlated_receipt(self.ledger, call.scope, correlation)?;
                if receipt.ledger_seq != outcome.ledger_seq {
                    return Err(Error::Unavailable);
                }
                Ok(receipt_value(receipt))
            }
            WaiverOperation::Validate => {
                if !fields.is_empty() {
                    return Err(Error::InvalidRequest);
                }
                let binding = owner(self.broker.validate_waiver(&invocation, &waiver_id))?;
                Ok(json::obj([
                    ("waiver_id", json::s(&waiver_id)),
                    ("result", json::s("waived")),
                    ("currently_valid", Value::Bool(true)),
                    ("context", binding.value()),
                ]))
            }
            _ => Err(Error::InvalidRequest),
        }
    }
}

pub(crate) fn execute_history(
    ledger: &SqliteLedger,
    call: &LifecycleCall<'_>,
    operation: WaiverOperation,
    fields: &Map,
) -> Option<Result<Value>> {
    match operation {
        WaiverOperation::Inspect => Some((|| {
            let waiver_id = one_field(fields, "waiver_id", ids::is_digest)?;
            let request = request_in_scope(ledger, call.scope, waiver_id)?;
            let receipt = ledger
                .waiver_receipt_in_scope(&call.scope.tenant, &call.scope.project, waiver_id, None)
                .map_err(|_| Error::Unavailable)?;
            Ok(json::obj([
                ("waiver_id", json::s(waiver_id)),
                ("context", request.value()),
                (
                    "historical_receipt",
                    receipt.map_or(Value::Null, receipt_value),
                ),
            ]))
        })()),
        WaiverOperation::Recover => Some((|| {
            let key = one_field(fields, "dispatch_key", valid_label)?;
            (call.reauthorize)()?;
            recover(
                ledger,
                call.scope,
                call.correlation(LifecycleKind::Waiver, key)?,
            )
        })()),
        _ => None,
    }
}

fn take_text(fields: &mut Map, key: &str, valid: impl FnOnce(&str) -> bool) -> Result<String> {
    match fields.remove(key) {
        Some(Value::Str(value)) if valid(&value) => Ok(value),
        _ => Err(Error::InvalidRequest),
    }
}

fn one_field<'a>(fields: &'a Map, key: &str, valid: impl FnOnce(&str) -> bool) -> Result<&'a str> {
    if fields.len() != 1 {
        return Err(Error::InvalidRequest);
    }
    fields
        .get(key)
        .and_then(Value::as_str)
        .filter(|value| valid(value))
        .ok_or(Error::InvalidRequest)
}

fn request_in_scope(
    ledger: &SqliteLedger,
    scope: &Scope,
    waiver_id: &str,
) -> Result<WaiverBinding> {
    ledger
        .waiver_request_in_scope(&scope.tenant, &scope.project, waiver_id)
        .map_err(|_| Error::Unavailable)?
        .ok_or(Error::NotFound)
}

fn correlated_receipt(
    ledger: &SqliteLedger,
    scope: &Scope,
    correlation: Value,
) -> Result<WaiverReceipt> {
    let field = |key| {
        correlation
            .get(key)
            .and_then(Value::as_str)
            .ok_or(Error::Unavailable)
    };
    ledger
        .waiver_receipt_in_scope(
            &scope.tenant,
            &scope.project,
            field("waiver_id")?,
            Some(field("dispatch_digest")?),
        )
        .map_err(|_| Error::Unavailable)?
        .filter(|receipt| field("challenge_id").is_ok_and(|id| receipt.challenge_id == id))
        .ok_or(Error::Unavailable)
}

fn recover(ledger: &SqliteLedger, scope: &Scope, correlation: Value) -> Result<Value> {
    correlated_receipt(ledger, scope, correlation).map(receipt_value)
}

fn context_value(binding: WaiverBinding) -> Result<Value> {
    let id = binding.fingerprint().map_err(|_| Error::Unavailable)?;
    Ok(json::obj([
        ("waiver_id", json::s(id.as_str())),
        ("context", binding.value()),
    ]))
}

fn receipt_value(receipt: WaiverReceipt) -> Value {
    json::obj([
        ("disposition", json::s("historical_receipt")),
        ("result", json::s("waived")),
        ("context", receipt.binding.value()),
        ("approver_issuer", json::s(&receipt.approver.issuer)),
        ("approver_subject", json::s(&receipt.approver.subject)),
        ("challenge_id", json::s(&receipt.challenge_id)),
        ("recorded_at", json::s(&receipt.recorded_at.to_string())),
        ("ledger_sequence", Value::Int(receipt.ledger_seq)),
    ])
}

fn owner<T>(outcome: std::result::Result<std::result::Result<T, Refusal>, TxFailure>) -> Result<T> {
    outcome
        .map_err(|_| Error::Unavailable)?
        .map_err(|refusal| match refusal {
            Refusal::WorkflowUnknown | Refusal::Approval(ApprovalCode::WaiverUnknown) => {
                Error::NotFound
            }
            Refusal::RevisionConflict(_) => Error::RevisionConflict,
            Refusal::IdempotencyConflict => Error::IdempotencyConflict,
            Refusal::OperatorUnauthorized
            | Refusal::RequestScopeMismatch
            | Refusal::Approval(_)
            | Refusal::Decision { .. } => Error::Unauthorized,
            _ => Error::Unavailable,
        })
}
