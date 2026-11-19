//! Exact, scoped human authorization for the development evaluator registry.

use writ_kernel::{
    contracts::CriterionClass,
    ids,
    json::{self, Value},
    time::Timestamp,
};

use crate::{
    acceptance::{class_label, AcceptanceCode},
    clock::EffectiveClock,
    contracts::RegistryEntry,
    keys::{KeyStatus, MaterialDigest, RunnerKeyRing},
    refusal::Refusal,
    runner::CommandRegistry,
    workflow::{
        AdminPresentation, AdminRefusal, AdminRequest, AuthorizedOperator, SourceUnavailable,
    },
};

/// The immutable, purpose-bound operation the human authorizes.
pub struct EvaluatorIntent {
    request: AdminRequest,
    expected_revision: i64,
    fingerprint: String,
    value: Value,
}

impl EvaluatorIntent {
    /// Bind an exact registry mutation to its required prior scoped revision.
    pub fn new(expected_revision: i64, request: AdminRequest) -> Result<Self, Refusal> {
        use Refusal::AdminRequestInvalid;
        if !(0..json::MAX_SAFE_INTEGER).contains(&expected_revision) {
            return Err(AdminRequestInvalid);
        }
        let (operation, body) = match &request {
            AdminRequest::RegisterEvaluator { entry } => {
                RegistryEntry::validate(entry).map_err(|_| AdminRequestInvalid)?;
                if entry.get("status").and_then(Value::as_str) != Some("active")
                    || entry.get("registry_revision").and_then(Value::as_int)
                        != Some(expected_revision + 1)
                {
                    return Err(AdminRequestInvalid);
                }
                ("publish", entry.clone())
            }
            AdminRequest::RevokeEvaluator {
                tenant_id,
                project_id,
                evaluator_id,
            } if ids::is_id(tenant_id) && ids::is_id(project_id) && ids::is_name(evaluator_id) => (
                "revoke",
                json::obj([
                    ("tenant_id", json::s(tenant_id)),
                    ("project_id", json::s(project_id)),
                    ("evaluator_id", json::s(evaluator_id)),
                ]),
            ),
            _ => return Err(AdminRequestInvalid),
        };
        let value = json::obj([
            ("purpose", json::s("writ-human-evaluator-publisher/1")),
            ("operation", json::s(operation)),
            ("expected_revision", Value::Int(expected_revision)),
            ("mutation", body),
        ]);
        let fingerprint = json::digest(&value)
            .map_err(|_| AdminRequestInvalid)?
            .as_str()
            .to_owned();
        Ok(Self {
            request,
            expected_revision,
            fingerprint,
            value,
        })
    }

    /// The exact scoped operation.
    pub fn request(&self) -> &AdminRequest {
        &self.request
    }

    /// The canonicalizable request the publisher challenge must bind.
    pub fn as_value(&self) -> &Value {
        &self.value
    }

    /// Digest of the exact purpose-bound request.
    pub fn fingerprint(&self) -> &str {
        &self.fingerprint
    }
}

/// Provenance from the configured human publisher authority.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct PublisherApproval {
    /// Authenticated publisher and evidence reference.
    pub operator: AuthorizedOperator,
    /// Globally unique single-use ceremony identifier.
    pub ceremony: String,
    /// Trusted issuance time.
    pub issued_at: Timestamp,
    /// Exclusive trusted expiry, at most 120 seconds after issuance.
    pub expires_at: Timestamp,
}

impl PublisherApproval {
    fn valid(&self, presentation: &AdminPresentation<'_>, now: Timestamp) -> bool {
        self.operator.id == presentation.operator
            && self.operator.channel == presentation.channel
            && ids::is_name(&self.operator.id)
            && [
                &self.operator.issuer,
                &self.operator.channel,
                &self.operator.evidence_ref,
                &self.ceremony,
            ]
            .iter()
            .all(|s| !s.is_empty() && s.chars().count() <= 256)
            && self.issued_at <= now
            && now < self.expires_at
            && self.expires_at <= self.issued_at.plus_seconds(120)
    }
}

/// Configured authority for the disposable test publisher, separate from workflow administration.
pub trait EvaluatorPublisher {
    /// Pin the publisher's identity, role and authorization state until this guard is dropped.
    /// Implementations must order changes to that state against the guard and must not re-enter
    /// the broker or ledger from the guard.
    fn current(&self) -> Result<impl CurrentEvaluatorPublisher + '_, AdminRefusal>;
}

/// A pinned view of the current human publisher authority.
pub trait CurrentEvaluatorPublisher {
    /// Authenticate human publisher permission in the exact intent's scope and verify the
    /// fresh approval binds the intent fingerprint. Rechecking must return the same ceremony
    /// without consuming it. The ledger commits its single use.
    fn approve(
        &self,
        intent: &EvaluatorIntent,
        presentation: &AdminPresentation<'_>,
        now: Timestamp,
    ) -> Result<PublisherApproval, AdminRefusal>;
}

/// Trusted implementation and environment identity for a scoped evaluator version.
pub struct EvaluatorImplementation {
    /// The trusted class. A model-assisted implementation cannot be reclassified by publication.
    pub class: CriterionClass,
    /// The assigned command id and exact digest, when the implementation uses a command.
    pub command: Option<(String, String)>,
    /// The approved implementation digest.
    pub implementation_digest: String,
    /// The approved runner environment digest.
    pub environment_digest: String,
    /// The runner-purpose key assigned to this evaluator.
    pub attestation_key_id: String,
}

/// Trusted implementation bindings supplied independently of the publication or pack.
pub trait EvaluatorImplementations {
    /// Resolve the exact scoped evaluator and version.
    fn get(
        &self,
        tenant_id: &str,
        project_id: &str,
        evaluator_id: &str,
        evaluator_version: &str,
    ) -> Result<Option<EvaluatorImplementation>, SourceUnavailable>;
}

/// Trusted, scoped implementation and command bindings and the purpose-typed key view.
/// The caller pins these sources for the operation. The SQLite transaction separately checks
/// durable key revocation. These sources must not re-enter the ledger transaction.
pub struct EvaluatorDependencies<'a> {
    /// Trusted registered implementation and environment bindings.
    pub implementations: &'a dyn EvaluatorImplementations,
    /// Existing scoped command registry.
    pub commands: &'a dyn CommandRegistry,
    /// Existing runner-purpose key verification source.
    pub runner_keys: &'a dyn RunnerKeyRing,
}

fn text<'a>(value: &'a Value, name: &str) -> Result<&'a str, Refusal> {
    value
        .get(name)
        .and_then(Value::as_str)
        .ok_or(Refusal::AdminRequestInvalid)
}

impl EvaluatorDependencies<'_> {
    fn check(&self, intent: &EvaluatorIntent) -> Result<Option<(String, MaterialDigest)>, Refusal> {
        let AdminRequest::RegisterEvaluator { entry } = intent.request() else {
            return Ok(None);
        };
        let binding = Refusal::Acceptance(AcceptanceCode::EvaluatorBindingMismatch);
        let unavailable = Refusal::Acceptance(AcceptanceCode::RegistryUnavailable);
        let scope = entry.get("scope").ok_or(Refusal::AdminRequestInvalid)?;
        let tenant = text(scope, "tenant_id")?;
        let project = text(scope, "project_id")?;
        let implementation = self
            .implementations
            .get(
                tenant,
                project,
                text(entry, "evaluator_id")?,
                text(entry, "evaluator_version")?,
            )
            .map_err(|_| unavailable.clone())?
            .ok_or_else(|| binding.clone())?;
        let key_id = text(
            entry
                .get("attestation")
                .ok_or(Refusal::AdminRequestInvalid)?,
            "key_id",
        )?;
        if class_label(implementation.class) != text(entry, "class")?
            || implementation.implementation_digest != text(entry, "implementation_digest")?
            || implementation.environment_digest != text(entry, "environment_digest")?
            || implementation.attestation_key_id != key_id
        {
            return Err(binding);
        }
        match (entry.get("command"), implementation.command) {
            (Some(spec), Some((id, digest)))
                if text(spec, "command_id")? == id && text(spec, "command_digest")? == digest =>
            {
                let command = self
                    .commands
                    .command(tenant, project, &id)
                    .map_err(|_| unavailable)?
                    .ok_or_else(|| binding.clone())?;
                if command.command_id != id || command.digest().as_deref() != Some(digest.as_str())
                {
                    return Err(binding);
                }
            }
            (None, None) => {}
            _ => return Err(binding),
        }
        match self
            .runner_keys
            .runner_key(key_id)
            .map_err(|_| Refusal::Acceptance(AcceptanceCode::AttestationKeyUnavailable))?
        {
            KeyStatus::Active(key) => Ok(Some((key_id.into(), MaterialDigest::of(key.bytes())))),
            _ => Err(Refusal::Acceptance(
                AcceptanceCode::AttestationUnauthenticated,
            )),
        }
    }
}

fn publisher_refusal(refusal: AdminRefusal) -> Refusal {
    match refusal {
        AdminRefusal::Unauthenticated => Refusal::OperatorUnauthenticated,
        AdminRefusal::Unauthorized => Refusal::OperatorUnauthorized,
    }
}

/// Verified publisher context accessible to the ledger only through the broker's change.
pub struct EvaluatorPublication<'a> {
    intent: &'a EvaluatorIntent,
    presentation: &'a AdminPresentation<'a>,
    authority: &'a dyn CurrentEvaluatorPublisher,
    dependencies: &'a EvaluatorDependencies<'a>,
    clock: &'a dyn EffectiveClock,
    initial_time: Timestamp,
    approval: PublisherApproval,
    runner_key: Option<(String, MaterialDigest)>,
}

impl<'a> EvaluatorPublication<'a> {
    pub(crate) fn authorize(
        intent: &'a EvaluatorIntent,
        presentation: &'a AdminPresentation<'a>,
        authority: &'a dyn CurrentEvaluatorPublisher,
        dependencies: &'a EvaluatorDependencies<'a>,
        clock: &'a dyn EffectiveClock,
        now: Timestamp,
    ) -> Result<Self, Refusal> {
        let approval = authority
            .approve(intent, presentation, now)
            .map_err(publisher_refusal)?;
        if !approval.valid(presentation, now) {
            return Err(Refusal::OperatorUnauthorized);
        }
        if let AdminRequest::RegisterEvaluator { entry } = intent.request() {
            let published_at = entry
                .get("published_at")
                .and_then(Value::as_str)
                .and_then(Timestamp::parse_strict);
            if entry.get("publisher").and_then(Value::as_str) != Some(approval.operator.id.as_str())
                || !published_at.is_some_and(|t| approval.issued_at <= t && t <= now)
            {
                return Err(Refusal::OperatorUnauthorized);
            }
        }
        let runner_key = dependencies.check(intent)?;
        Ok(Self {
            intent,
            presentation,
            authority,
            dependencies,
            clock,
            initial_time: now,
            approval,
            runner_key,
        })
    }

    pub(crate) fn operator(&self) -> &AuthorizedOperator {
        &self.approval.operator
    }

    /// The required prior scoped revision.
    pub fn expected_revision(&self) -> i64 {
        self.intent.expected_revision
    }

    /// The exact authorized intent digest.
    pub fn intent_sha(&self) -> &str {
        self.intent.fingerprint()
    }

    /// The single-use human ceremony.
    pub fn ceremony(&self) -> &str {
        &self.approval.ceremony
    }

    /// Runner-purpose key id and material digest for the in-transaction revocation check.
    pub fn runner_key(&self) -> Option<(&str, &MaterialDigest)> {
        self.runner_key
            .as_ref()
            .map(|(id, digest)| (id.as_str(), digest))
    }

    /// Revalidate dependencies, publisher authority and freshness immediately before commit.
    pub fn recheck(&self) -> Result<(), Refusal> {
        if self.dependencies.check(self.intent)? != self.runner_key {
            return Err(Refusal::Acceptance(
                AcceptanceCode::EvaluatorBindingMismatch,
            ));
        }
        let now = self.clock.read().map_err(|e| e.refusal())?.effective;
        if now < self.initial_time {
            return Err(Refusal::TimeUnavailable);
        }
        let approval = self
            .authority
            .approve(self.intent, self.presentation, now)
            .map_err(publisher_refusal)?;
        let finished_at = self.clock.read().map_err(|e| e.refusal())?.effective;
        if finished_at < now {
            return Err(Refusal::TimeUnavailable);
        }
        if approval == self.approval && approval.valid(self.presentation, finished_at) {
            Ok(())
        } else {
            Err(Refusal::OperatorUnauthorized)
        }
    }
}
