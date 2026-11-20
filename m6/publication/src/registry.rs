use std::path::Path;

use writ_broker::{
    clock::{ObservedTime, TrustedClock},
    human::SubjectId,
    refusal::Refusal,
    waiver::{WaiverPolicyGuard, WaiverPolicyIdentity, WaiverPolicyView},
};
use writ_kernel::{
    contracts::AcceptanceRequirements, json::Value, policy::LoadedBundle, time::Timestamp,
};

use crate::{
    policy::{self, Policies},
    signature::verified_until,
    store::{Audit, Commit, Store},
    wire, Approval, Error, Intent, Operation, Publication, Purpose, Scope, VerificationSource,
};

/// The trusted, pinned view used for one ordered registry operation.
pub trait CurrentTrust: VerificationSource + TrustedClock + Sync {
    /// Resolve the current disposable broker registration under this same trust guard.
    fn enforcement_evidence(&self) -> Option<crate::FixtureEvidence> {
        None
    }

    /// Authenticate a fresh, explicitly scoped human publisher for this exact intent.
    fn approve(
        &self,
        intent: &Intent,
        presentation: &[u8],
        now: Timestamp,
    ) -> Result<Approval, Error>;

    /// Check the complete owning dependency view for this immutable target.
    /// Implementations must pin that view until this trust guard drops.
    fn has_active_dependents(
        &self,
        _scope: &Scope,
        _publication_version: i64,
        _publication_digest: &str,
    ) -> Result<bool, Error> {
        Err(Error::DependencyUnavailable)
    }

    /// Resolve the D-051 publisher's stable audit identity to its qualified
    /// M4 subject. Deployments must explicitly configure this bridge for waivers.
    fn waiver_publisher_subject(&self, _publisher: &str) -> Result<SubjectId, Error> {
        Err(Error::Authority)
    }
}

/// Authority configured by a trusted caller when opening the registry.
///
/// The returned guard pins key status and publisher authorization until drop. Updates
/// must be ordered against the entire guard lifetime. Time is read from the underlying
/// trusted clock at each check. Authority implementations must not re-enter the registry.
pub trait TrustAuthority {
    /// Acquire the current authority state or refuse.
    fn current(&self) -> Result<impl CurrentTrust + '_, Error>;
}

/// A trusted host-configured witness for an active management dispatch.
pub trait ManagedIntentSource: Send + Sync {
    /// Require the exact registered management intent under its live draft fence.
    fn verify(&self, intent: &Intent, presentation: &[u8]) -> Result<(), Error>;
}

/// Validated authorization, acceptance and proof at one durable activation.
pub struct Selection<'a> {
    revision: i64,
    version: i64,
    publication: &'a Publication,
    policies: &'a Policies,
    checked_at: Timestamp,
    trust: &'a dyn CurrentTrust,
    scope: &'a Scope,
    audit: &'a [Audit],
}

impl Selection<'_> {
    /// Monotonic activation revision within the registry scope.
    pub fn revision(&self) -> i64 {
        self.revision
    }

    /// Immutable publication version.
    pub fn publication_version(&self) -> i64 {
        self.version
    }

    /// The exact D-051 publication.
    pub fn publication(&self) -> &Publication {
        self.publication
    }

    /// The validated bundle consumed by the existing broker.
    pub fn authorization(&self) -> &LoadedBundle {
        &self.policies.authorization
    }

    /// Return ordinary kernel requirements only when they express the complete signed policy.
    pub fn acceptance(&self) -> Result<&AcceptanceRequirements, Error> {
        self.policies
            .acceptance
            .ordinary_requirements()
            .ok_or(Error::Unsupported)
    }

    /// Recheck the selected signatures and validity at the current trusted time.
    pub fn recheck(&self) -> Result<Timestamp, Error> {
        current_signatures(self.publication, self.scope, self.trust)
    }

    /// Trusted time sampled at selection load.
    pub fn checked_at(&self) -> Timestamp {
        self.checked_at
    }
}

impl writ_broker::quorum_policy::QuorumPolicyGuard for Selection<'_> {
    fn current(
        &self,
        tenant: &str,
        project: &str,
        at: Timestamp,
    ) -> Result<&writ_broker::quorum_policy::QuorumRequirements, Refusal> {
        if (tenant, project) != (self.scope.tenant(), self.scope.project()) {
            return Err(Refusal::AcceptancePolicyUnavailable);
        }
        let (now, expires) = signed_window(self.publication, self.scope, self.trust)
            .map_err(|_| Refusal::AcceptancePolicyUnavailable)?;
        if now < self.checked_at || at < self.checked_at {
            return Err(Refusal::TimeUnavailable);
        }
        if at >= expires {
            return Err(Refusal::AcceptancePolicyUnavailable);
        }
        Ok(&self.policies.acceptance)
    }
}

impl WaiverPolicyGuard for Selection<'_> {
    fn current(&self, now: ObservedTime) -> Result<WaiverPolicyView, Refusal> {
        let unavailable = |_| Refusal::AcceptancePolicyUnavailable;
        let requirements = self.acceptance().map_err(unavailable)?;
        let checked_at = now.at();
        if checked_at < self.checked_at {
            return Err(Refusal::TimeUnavailable);
        }
        let expires_at = signed_window_at(self.publication, self.scope, self.trust, checked_at)
            .map_err(unavailable)?;
        let publishers = self
            .audit
            .iter()
            .filter(|event| {
                (event.operation != Operation::Retire && event.revision == self.revision)
                    || (event.operation == Operation::Publish
                        && event.publication_version == self.version)
            })
            .map(|event| self.trust.waiver_publisher_subject(&event.human))
            .collect::<Result<Vec<_>, _>>()
            .map_err(unavailable)?;
        let digest = wire::digest(&self.publication.to_value()).map_err(unavailable)?;
        WaiverPolicyView::new(
            WaiverPolicyIdentity {
                tenant_id: self.scope.tenant().to_owned(),
                project_id: self.scope.project().to_owned(),
                authorization_digest: self.policies.authorization.sha.to_string(),
                acceptance_digest: requirements.policy.acceptance_policy_bundle_sha.clone(),
                activation_revision: self.revision,
                publication_digest: digest.to_string(),
            },
            self.policies.waivers.clone(),
            publishers,
            checked_at,
            expires_at,
            requirements.clone(),
        )
    }
}

/// Disposable registry bound to one trusted authority and scope.
pub struct Registry<T> {
    store: Store,
    authority: T,
    management: Option<Box<dyn ManagedIntentSource>>,
}

impl<T: TrustAuthority> Registry<T> {
    /// Create an empty registry file. Existing state is never replaced.
    pub fn create(path: &Path, scope: Scope, authority: T) -> Result<Self, Error> {
        let store = Store::create(path, scope)?;
        Ok(Self {
            store,
            authority,
            management: None,
        })
    }

    /// Open existing state and verify any selected publication under current trust.
    pub fn open(path: &Path, scope: Scope, authority: T) -> Result<Self, Error> {
        let registry = Self::open_for_administration(path, scope, authority)?;
        if !registry.store.audit()?.is_empty() {
            registry.with_selection(|_| Ok(()))?;
        }
        Ok(registry)
    }

    /// Open coherent history for inspection and rollback when the current policy is revoked.
    /// Policy consumers still require current verification through `open` or `with_selection`.
    pub fn open_for_administration(path: &Path, scope: Scope, authority: T) -> Result<Self, Error> {
        let store = Store::open(path, scope)?;
        Ok(Self {
            store,
            authority,
            management: None,
        })
    }

    /// Open a read-only inspection handle with a bounded integrity check and history replay.
    pub fn open_for_inspection(path: &Path, scope: Scope, authority: T) -> Result<Self, Error> {
        Ok(Self {
            store: Store::open_for_inspection(path, scope)?,
            authority,
            management: None,
        })
    }

    /// Configure the same trusted management store handle used for coordinated dispatch.
    pub fn with_management_source(mut self, source: impl ManagedIntentSource + 'static) -> Self {
        self.management = Some(Box::new(source));
        self
    }

    /// Validate a proposed publish or rollback under current trust without requesting approval.
    pub fn preview(
        &self,
        expected_revision: i64,
        publication_version: i64,
        candidate: Option<&Publication>,
    ) -> Result<Publication, Error> {
        let trust = self.authority.current()?;
        let operation = if candidate.is_some() {
            Operation::Publish
        } else {
            Operation::Rollback
        };
        self.store.preview(
            operation,
            expected_revision,
            publication_version,
            candidate,
            |publication, selected| {
                let (policies, _) =
                    checked(publication, self.scope(), publication_version, &trust)?;
                policy::preserve_required_criteria(&policies, selected)?;
                if policy::check_selection(operation, publication, selected)?.is_some() {
                    let (version, current) = selected.ok_or(Error::InvalidPolicy)?;
                    checked(current, self.scope(), version, &trust)?;
                }
                Ok(())
            },
        )
    }

    /// Verify an exact signed replacement and preview its complete offline Decisions.
    pub fn preview_optional_removal(
        &self,
        expected_revision: i64,
        replacement_version: i64,
        candidate: &Publication,
    ) -> Result<Value, Error> {
        let trust = self.authority.current()?;
        let mut difference = None;
        self.store.preview(
            Operation::Publish,
            expected_revision,
            replacement_version,
            Some(candidate),
            |publication, selected| {
                checked(publication, self.scope(), replacement_version, &trust)?;
                let (version, current) = selected.ok_or(Error::InvalidPolicy)?;
                checked(current, self.scope(), version, &trust)?;
                difference = policy::check_selection(Operation::Publish, publication, selected)?;
                difference.as_ref().ok_or(Error::InvalidPolicy)?;
                Ok(())
            },
        )?;
        difference.ok_or(Error::InvalidPolicy)
    }

    /// Check retirement eligibility without requesting approval or changing state.
    pub fn preview_retire(
        &self,
        expected_revision: i64,
        publication_version: i64,
        confirmation: &str,
    ) -> Result<Publication, Error> {
        wire::number(expected_revision, 0)?;
        wire::number(publication_version, 1)?;
        if confirmation != Intent::retirement_phrase(publication_version) {
            return Err(Error::Format);
        }
        let trust = self.authority.current()?;
        self.store.preview(
            Operation::Retire,
            expected_revision,
            publication_version,
            None,
            |publication, _| {
                checked(publication, self.scope(), publication_version, &trust)?;
                require_no_dependents(&trust, self.scope(), publication_version, publication)
            },
        )
    }

    /// Refuse mandatory removal and unsupported synthetic rule removal in the current profile.
    pub fn refuse_deactivation(
        &self,
        expected_revision: i64,
        publication_version: i64,
        policy_id: &str,
        confirmation: &str,
    ) -> Result<(), Error> {
        wire::number(expected_revision, 0)?;
        wire::number(publication_version, 1)?;
        if policy_id.len() > 64
            || !writ_kernel::ids::is_kind(policy_id)
            || confirmation != Intent::deactivation_phrase(policy_id)
        {
            return Err(Error::Format);
        }
        self.with_selection(|selected| {
            if selected.revision() != expected_revision {
                return Err(Error::Conflict);
            }
            if selected.publication_version() != publication_version {
                return Err(Error::Replay);
            }
            policy::refuse_deactivation(
                selected.publication(),
                &selected.policies.acceptance,
                policy_id,
            )
        })
    }

    /// Read the current selection after verification, or report that the registry is empty.
    pub fn current_selection(&self) -> Result<Option<Commit>, Error> {
        let trust = self.authority.current()?;
        self.store
            .with_selected(|revision, version, publication, audit| {
                checked(publication, self.scope(), version, &trust)?;
                let event = audit
                    .iter()
                    .rev()
                    .find(|event| event.operation != Operation::Retire)
                    .ok_or(Error::Recovery)?;
                Ok(Commit {
                    revision,
                    publication_version: version,
                    audit_sequence: event.sequence,
                })
            })
    }

    /// Reopen coherent durable history to perform an explicit rollback when the
    /// selected publication cannot currently be verified. Revalidates the rollback target.
    pub fn recover(
        path: &Path,
        scope: Scope,
        authority: T,
        intent: &Intent,
        presentation: &[u8],
    ) -> Result<Commit, Error> {
        if intent.operation() != Operation::Rollback {
            return Err(Error::Authority);
        }
        let store = Store::open(path, scope)?;
        Self {
            store,
            authority,
            management: None,
        }
        .change(intent, None, presentation)
    }

    /// Trusted scope configured when creating or opening this registry.
    pub fn scope(&self) -> &Scope {
        self.store.scope()
    }

    /// Validate, publish and select a fresh, exactly authorized candidate.
    pub fn publish(
        &self,
        intent: &Intent,
        candidate: &Publication,
        presentation: &[u8],
    ) -> Result<Commit, Error> {
        self.change(intent, Some(candidate), presentation)
    }

    /// Select or retire an existing immutable publication under fresh authorization.
    pub fn change(
        &self,
        intent: &Intent,
        candidate: Option<&Publication>,
        presentation: &[u8],
    ) -> Result<Commit, Error> {
        if intent.management().is_some() {
            return Err(Error::Authority);
        }
        self.change_inner(intent, candidate, presentation, || Ok(()))
    }

    /// Submit a managed intent under the configured live draft fence and recheck the
    /// scoped management role before both registry validations. The trusted callback
    /// must avoid registry reentry.
    pub fn change_managed(
        &self,
        intent: &Intent,
        candidate: Option<&Publication>,
        presentation: &[u8],
        reauthorize: impl Fn() -> Result<(), Error>,
    ) -> Result<Commit, Error> {
        if intent.management().is_none() {
            return Err(Error::Authority);
        }
        let witness = self.management.as_deref().ok_or(Error::Authority)?;
        self.change_inner(intent, candidate, presentation, || {
            witness.verify(intent, presentation)?;
            reauthorize()
        })
    }

    fn change_inner(
        &self,
        intent: &Intent,
        candidate: Option<&Publication>,
        presentation: &[u8],
        before_commit: impl Fn() -> Result<(), Error>,
    ) -> Result<Commit, Error> {
        if intent.scope() != self.store.scope() {
            return Err(Error::Scope);
        }
        let trust = self.authority.current()?;
        self.store
            .change(intent, candidate, |publication, selected| {
                let (policies, before) = checked(
                    publication,
                    intent.scope(),
                    intent.publication_version(),
                    &trust,
                )?;
                let source = if intent.operation() == Operation::Retire {
                    None
                } else {
                    policy::preserve_required_criteria(&policies, selected)?;
                    if policy::check_selection(intent.operation(), publication, selected)?.is_some()
                    {
                        let (version, current) = selected.ok_or(Error::InvalidPolicy)?;
                        policy::load(current, intent.scope(), version)?;
                        signed_window_at(current, intent.scope(), &trust, before)?;
                        Some(current)
                    } else {
                        None
                    }
                };
                let approval = trust.approve(intent, presentation, before)?;
                if !intent.matches_approval(&approval) {
                    return Err(Error::Authority);
                }
                before_commit()?;
                if intent.operation() == Operation::Retire {
                    require_no_dependents(
                        &trust,
                        intent.scope(),
                        intent.publication_version(),
                        publication,
                    )?;
                }
                let now = current_signatures(publication, intent.scope(), &trust)?;
                if now < before {
                    return Err(Error::Time);
                }
                if let Some(source) = source {
                    signed_window_at(source, intent.scope(), &trust, now)?;
                }
                require_enforcement(&trust, intent.scope(), now)?;
                approval.check(now)?;
                Ok((approval, now))
            })
    }

    /// Verify and use one coherent selection under the store order lock and current trust.
    /// The callback must complete the guarded operation before returning.
    pub fn with_selection<R>(
        &self,
        operation: impl FnOnce(Selection<'_>) -> Result<R, Error>,
    ) -> Result<R, Error> {
        let trust = self.authority.current()?;
        self.store
            .with_selected(|revision, version, publication, audit| {
                let (policies, checked_at) =
                    checked(publication, self.store.scope(), version, &trust)?;
                operation(Selection {
                    revision,
                    version,
                    publication,
                    policies: &policies,
                    checked_at,
                    trust: &trust,
                    scope: self.store.scope(),
                    audit,
                })
            })?
            .ok_or(Error::Recovery)
    }

    /// Read one bounded lifecycle page and derive evidence for its exact selected publication.
    pub fn enforcement_labels(
        &self,
        scope: &Scope,
        after: i64,
        limit: usize,
    ) -> Result<Value, Error> {
        if scope != self.scope() {
            return Err(Error::Scope);
        }
        let trust = self.authority.current();
        self.store
            .lifecycle_page(after, limit, |selection, events, next| {
                let mut rows = Vec::new();
                let mut selected = None;
                if let Some((event, publication)) = selection {
                    let evidence = trust
                        .as_ref()
                        .ok()
                        .and_then(|guard| guard.enforcement_evidence());
                    let status =
                        trust
                            .as_ref()
                            .map_err(|_| "TRUST_UNAVAILABLE")
                            .and_then(|guard| {
                                let now = guard.now().map_err(|_| "CLOCK_UNAVAILABLE")?;
                                policy::load(publication, scope, event.publication_version)
                                    .map_err(|_| "POLICY_MISMATCH")?;
                                signed_window_at(publication, scope, guard, now).map_err(
                                    |error| match error {
                                        Error::Time => "POLICY_EXPIRED",
                                        Error::Trust
                                            if [
                                                &publication.authorization,
                                                &publication.acceptance,
                                            ]
                                            .iter()
                                            .any(|policy| {
                                                policy
                                                    .envelope
                                                    .get("protected")
                                                    .and_then(|header| {
                                                        header.get("publisher_key_id")
                                                    })
                                                    .and_then(Value::as_str)
                                                    .and_then(|id| guard.resolve(id).ok())
                                                    .is_some_and(|key| key.revoked)
                                            }) =>
                                        {
                                            "POLICY_KEY_REVOKED"
                                        }
                                        Error::Trust => "POLICY_KEY_UNTRUSTED",
                                        _ => "POLICY_MISMATCH",
                                    },
                                )?;
                                evidence
                                    .as_ref()
                                    .ok_or("EVIDENCE_UNAVAILABLE")?
                                    .check(scope, now)
                            });
                    selected = Some((event, status.is_ok()));
                    rows =
                        crate::evidence::rows(scope, publication, evidence.as_ref(), status.err())?;
                }
                Ok(writ_kernel::json::obj([
                    (
                        "selection_revision",
                        selected.map_or(Value::Null, |(event, _)| Value::Int(event.revision)),
                    ),
                    (
                        "publication_version",
                        selected.map_or(Value::Null, |(event, _)| {
                            Value::Int(event.publication_version)
                        }),
                    ),
                    ("history", crate::evidence::history(&events, next, selected)),
                    ("enforcement_labels", Value::Arr(rows)),
                ]))
            })
    }

    /// Read the append-only, structurally verified audit history.
    pub fn audit(&self) -> Result<Vec<Audit>, Error> {
        self.store.audit()
    }
}

fn require_no_dependents(
    trust: &impl CurrentTrust,
    scope: &Scope,
    version: i64,
    publication: &Publication,
) -> Result<(), Error> {
    let digest = publication.fingerprint()?;
    if trust.has_active_dependents(scope, version, digest.as_str())? {
        return Err(Error::ActiveDependents);
    }
    Ok(())
}

fn require_enforcement(
    trust: &impl CurrentTrust,
    scope: &Scope,
    now: Timestamp,
) -> Result<(), Error> {
    trust
        .enforcement_evidence()
        .ok_or(Error::DependencyUnavailable)?
        .check(scope, now)
        .map_err(|_| Error::InvalidPolicy)
}

fn checked(
    publication: &Publication,
    scope: &Scope,
    version: i64,
    trust: &impl CurrentTrust,
) -> Result<(Policies, Timestamp), Error> {
    let policies = policy::load(publication, scope, version)?;
    let now = current_signatures(publication, scope, trust)?;
    require_enforcement(trust, scope, now)?;
    Ok((policies, now))
}

fn current_signatures(
    publication: &Publication,
    scope: &Scope,
    trust: &dyn CurrentTrust,
) -> Result<Timestamp, Error> {
    signed_window(publication, scope, trust).map(|(now, _)| now)
}

fn signed_window(
    publication: &Publication,
    scope: &Scope,
    trust: &dyn CurrentTrust,
) -> Result<(Timestamp, Timestamp), Error> {
    let now = trust.now().map_err(|_| Error::Time)?;
    let expires_at = signed_window_at(publication, scope, trust, now)?;
    Ok((now, expires_at))
}

fn signed_window_at(
    publication: &Publication,
    scope: &Scope,
    trust: &dyn CurrentTrust,
    now: Timestamp,
) -> Result<Timestamp, Error> {
    let authorization = verified_until(
        &publication.authorization,
        Purpose::Authorization,
        scope,
        now,
        trust,
    )?;
    let acceptance = verified_until(
        &publication.acceptance,
        Purpose::Acceptance,
        scope,
        now,
        trust,
    )?;
    Ok(authorization.min(acceptance))
}
