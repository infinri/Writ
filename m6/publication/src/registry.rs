use std::path::Path;

use writ_broker::{
    clock::{ObservedTime, TrustedClock},
    human::SubjectId,
    refusal::Refusal,
    waiver::{WaiverPolicyGuard, WaiverPolicyIdentity, WaiverPolicyView},
};
use writ_kernel::{contracts::AcceptanceRequirements, policy::LoadedBundle, time::Timestamp};

use crate::{
    policy::{self, Policies},
    signature::verified_until,
    store::{Audit, Commit, Store},
    wire, Approval, Error, Intent, Operation, Publication, Purpose, Scope, VerificationSource,
};

/// The trusted, pinned view used for one ordered registry operation.
pub trait CurrentTrust: VerificationSource + TrustedClock + Sync {
    /// Authenticate a fresh, explicitly scoped human publisher for this exact intent.
    fn approve(
        &self,
        intent: &Intent,
        presentation: &[u8],
        now: Timestamp,
    ) -> Result<Approval, Error>;

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

    /// The requirements derived from the verified acceptance payload.
    pub fn acceptance(&self) -> &AcceptanceRequirements {
        &self.policies.acceptance
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

impl WaiverPolicyGuard for Selection<'_> {
    fn current(&self, now: ObservedTime) -> Result<WaiverPolicyView, Refusal> {
        let unavailable = |_| Refusal::AcceptancePolicyUnavailable;
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
                acceptance_digest: self
                    .policies
                    .acceptance
                    .policy
                    .acceptance_policy_bundle_sha
                    .clone(),
                activation_revision: self.revision,
                publication_digest: digest.to_string(),
            },
            self.policies.waivers.clone(),
            publishers,
            checked_at,
            expires_at,
            self.policies.acceptance.clone(),
        )
    }
}

/// Disposable registry bound to one trusted authority and scope.
pub struct Registry<T> {
    store: Store,
    authority: T,
}

impl<T: TrustAuthority> Registry<T> {
    /// Create an empty registry file. Existing state is never replaced.
    pub fn create(path: &Path, scope: Scope, authority: T) -> Result<Self, Error> {
        let store = Store::create(path, scope)?;
        Ok(Self { store, authority })
    }

    /// Open existing state and verify any selected publication under current trust.
    pub fn open(path: &Path, scope: Scope, authority: T) -> Result<Self, Error> {
        let store = Store::open(path, scope)?;
        let registry = Self { store, authority };
        if !registry.store.audit()?.is_empty() {
            registry.with_selection(|_| Ok(()))?;
        }
        Ok(registry)
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
        Self { store, authority }.change(intent, None, presentation)
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
        if intent.scope() != self.store.scope() {
            return Err(Error::Scope);
        }
        let trust = self.authority.current()?;
        self.store.change(intent, candidate, |publication| {
            let (_, before) = checked(
                publication,
                intent.scope(),
                intent.publication_version(),
                &trust,
            )?;
            let approval = trust.approve(intent, presentation, before)?;
            let now = current_signatures(publication, intent.scope(), &trust)?;
            if now < before {
                return Err(Error::Time);
            }
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
            })
    }

    /// Read the append-only, structurally verified audit history.
    pub fn audit(&self) -> Result<Vec<Audit>, Error> {
        self.store.audit()
    }
}

fn checked(
    publication: &Publication,
    scope: &Scope,
    version: i64,
    trust: &impl CurrentTrust,
) -> Result<(Policies, Timestamp), Error> {
    let policies = policy::load(publication, scope, version)?;
    let now = current_signatures(publication, scope, trust)?;
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
