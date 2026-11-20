//! Disposable policy management at the typed /manage/v1 boundary.

mod boundary;
#[cfg(feature = "fixtures")]
pub mod contract_fixture;
pub mod contract_registry;
mod contract_validation;
pub mod contracts;
mod evaluation;
#[cfg(feature = "fixtures")]
pub mod evaluator_fixture;
mod lifecycle;
pub mod managed_evaluator;
pub mod managed_waiver;
#[cfg(feature = "fixtures")]
pub mod pack_fixture;
pub mod pack_registry;
pub mod packs;
mod publication;
mod store;
#[cfg(feature = "fixtures")]
pub mod waiver_fixture;
mod wire;

#[cfg(feature = "fixtures")]
pub mod fixture;

pub use boundary::PublicationBoundary;
pub use contracts::{ContractBoundary, ContractOperation};
pub use lifecycle::{
    EvaluatorBoundary, EvaluatorOperation, LifecycleCall, LifecycleKind, WaiverBoundary,
    WaiverOperation,
};
pub use packs::{PackBoundary, PackOperation};
pub use publication::PublicationRequest;
pub use store::Store;
pub use wire::{
    error_response, execute_v1, execute_v1_with_publication, execute_v1_with_service, preflight_v1,
    RequestV1, ResponseV1,
};

use writ_kernel::json::{self, Digest, Value};

pub const MAX_CREDENTIAL: usize = 512;
pub const MAX_REQUEST: usize = 40 * 1024;
pub const MAX_CANDIDATE: usize = 32 * 1024;

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Scope {
    pub tenant: String,
    pub project: String,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Realm {
    Management,
    ManagementService,
    Agent,
    Adapter,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Role {
    Viewer,
    PolicyAuthor,
    Publisher,
    WaiverOperator,
}

pub struct Grant {
    pub scope: Scope,
    pub role: Role,
}

/// Only the configured authenticator can supply this data to the application service.
pub struct Principal {
    pub realm: Realm,
    pub subject: String,
    pub grants: Vec<Grant>,
}

/// Trusted host-side boundary for current identity and grants. No wire fields supply either.
pub trait Authenticator {
    fn authenticate(&self, credential: &[u8]) -> Option<Principal>;

    /// Pin current credential, identity and grant state until the returned guard is dropped.
    /// Lifecycle writes require this capability. Authority changes must order against a live
    /// guard. A guard must not re-enter the management service or the owning stores.
    fn pin(&self, _credential: &[u8]) -> Option<Box<dyn PinnedPrincipal + '_>> {
        None
    }
}

pub trait PinnedPrincipal {
    fn principal(&self) -> &Principal;

    /// Recheck time-dependent credential validity without releasing the pinned state.
    fn still_valid(&self) -> bool {
        true
    }
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct DraftRef {
    pub id: String,
    pub revision: i64,
}

pub(crate) struct Draft {
    pub id: String,
    pub revision: i64,
    pub candidate: Value,
    pub source: Option<DraftRef>,
}

pub(crate) struct Mutation {
    pub id: String,
    pub expected_revision: i64,
    pub key: String,
    pub preview: bool,
    pub change: Change,
}

pub(crate) enum Change {
    Create(Value),
    Edit(Value),
    Clone(DraftRef),
}

#[derive(Debug, PartialEq, Eq)]
pub struct Receipt {
    pub revision: i64,
    pub audit_id: Option<i64>,
}

pub(crate) struct History {
    pub versions: Vec<Version>,
    pub next: Option<i64>,
}

#[derive(Debug, PartialEq, Eq)]
pub struct Version {
    pub draft: DraftRef,
    pub digest: String,
    pub source: Option<DraftRef>,
}

impl TryFrom<Draft> for Version {
    type Error = Error;

    fn try_from(draft: Draft) -> Result<Self> {
        let candidate = json::canonical(&draft.candidate).map_err(|_| Error::Unavailable)?;
        Ok(Self {
            draft: DraftRef {
                id: draft.id,
                revision: draft.revision,
            },
            digest: Digest::of_bytes(candidate.as_bytes()).to_string(),
            source: draft.source,
        })
    }
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Error {
    InvalidRequest,
    Unauthorized,
    NotFound,
    RevisionConflict,
    IdempotencyConflict,
    LimitExceeded,
    Unavailable,
    InvalidPolicy,
    ProtectedContract,
    InvalidContract,
    InvalidPack,
    PackConflict,
    IncompatibleContract,
    ActiveDependents,
    Unsupported,
}

pub type Result<T> = std::result::Result<T, Error>;

fn valid_label(label: &str) -> bool {
    !label.is_empty()
        && label.len() <= 64
        && label
            .bytes()
            .all(|byte| byte.is_ascii_alphanumeric() || matches!(byte, b'.' | b'_' | b'-'))
}

pub struct Service<'a> {
    store: &'a Store,
    authority: Option<&'a dyn Authenticator>,
    publication: Option<&'a dyn PublicationBoundary>,
    evaluator: Option<&'a dyn EvaluatorBoundary>,
    contracts: Option<&'a dyn ContractBoundary>,
    packs: Option<&'a dyn PackBoundary>,
    waiver: Option<&'a dyn WaiverBoundary>,
    allow_self_publication: bool,
}

impl<'a> Service<'a> {
    pub fn new(store: &'a Store, authority: Option<&'a dyn Authenticator>) -> Self {
        Self {
            store,
            authority,
            publication: None,
            evaluator: None,
            contracts: None,
            packs: None,
            waiver: None,
            allow_self_publication: false,
        }
    }

    /// Connect to the separately configured publication registry.
    pub fn with_publication(mut self, boundary: &'a dyn PublicationBoundary) -> Self {
        self.publication = Some(boundary);
        self
    }

    pub fn with_evaluator(mut self, boundary: &'a dyn EvaluatorBoundary) -> Self {
        self.evaluator = Some(boundary);
        self
    }

    pub fn with_packs(mut self, boundary: &'a dyn PackBoundary) -> Self {
        self.packs = Some(boundary);
        self
    }

    pub fn with_contracts(mut self, boundary: &'a dyn ContractBoundary) -> Self {
        self.contracts = Some(boundary);
        self
    }

    pub fn with_waiver(mut self, boundary: &'a dyn WaiverBoundary) -> Self {
        self.waiver = Some(boundary);
        self
    }

    /// Enable explicit, audited author and publisher overlap for the local fixture.
    pub fn allow_self_publication(mut self) -> Self {
        self.allow_self_publication = true;
        self
    }

    fn authenticate(&self, credential: &[u8]) -> Result<Principal> {
        authenticate(self.authority, credential)
    }

    pub fn execute(&self, credential: &[u8], request: RequestV1) -> Result<ResponseV1> {
        self.dispatch(credential, self.authenticate(credential)?, request)
    }

    fn dispatch(
        &self,
        credential: &[u8],
        principal: Principal,
        request: RequestV1,
    ) -> Result<ResponseV1> {
        use wire::Operation;
        let RequestV1 {
            scope,
            operation,
            binding,
        } = request;
        authorize_resource(&principal, &scope, &operation, self.allow_self_publication)?;
        if let Operation::Publication(request) = operation {
            let check = publication_reader_or_publisher(&request);
            let pinned = if matches!(
                &request,
                PublicationRequest::Retire { .. }
                    | PublicationRequest::OptionalRemoval {
                        approval: Some(_),
                        ..
                    }
            ) {
                Some(
                    self.authority
                        .and_then(|authority| authority.pin(credential))
                        .ok_or(Error::Unauthorized)?,
                )
            } else {
                None
            };
            let check_identity = |current: &Principal| {
                if current.subject != principal.subject
                    || current.realm != principal.realm
                    || current.grants.len() > 128
                {
                    return Err(Error::Unauthorized);
                }
                check(current, &scope)
            };
            let reauthorize = || {
                if let Some(guard) = &pinned {
                    let current = guard
                        .still_valid()
                        .then(|| guard.principal())
                        .ok_or(Error::Unauthorized)?;
                    check_identity(current)
                } else {
                    check_identity(&self.authenticate(credential)?)
                }
            };
            if pinned.is_some() {
                reauthorize()?;
            }
            let boundary = self.publication.ok_or(Error::Unavailable)?;
            return Ok(ResponseV1::Publication(publication::execute(
                self.store,
                boundary,
                &scope,
                &principal.subject,
                request,
                self.allow_self_publication,
                reauthorize,
            )?));
        }
        if matches!(
            &operation,
            Operation::Evaluator(..)
                | Operation::Waiver(..)
                | Operation::Contract(..)
                | Operation::Pack(..)
        ) {
            let pin_required = !matches!(&operation, Operation::Pack(kind, _) if kind.read_only())
                && !matches!(
                    &operation,
                    Operation::Evaluator(EvaluatorOperation::Inspect, _)
                        | Operation::Waiver(
                            WaiverOperation::Inspect | WaiverOperation::Validate,
                            _
                        )
                        | Operation::Contract(
                            ContractOperation::Browse
                                | ContractOperation::Inspect
                                | ContractOperation::ValidateSchema
                                | ContractOperation::ValidateExample
                                | ContractOperation::Preview
                                | ContractOperation::Recover,
                            _,
                        )
                );
            let pinned = if pin_required {
                Some(
                    self.authority
                        .and_then(|authority| authority.pin(credential))
                        .ok_or(Error::Unauthorized)?,
                )
            } else {
                None
            };
            let check_identity = |current: &Principal| {
                if current.subject != principal.subject
                    || current.realm != principal.realm
                    || current.grants.len() > 128
                {
                    return Err(Error::Unauthorized);
                }
                authorize_resource(current, &scope, &operation, self.allow_self_publication)
            };
            let reauthorize = || {
                if let Some(guard) = &pinned {
                    let current = guard
                        .still_valid()
                        .then(|| guard.principal())
                        .ok_or(Error::Unauthorized)?;
                    check_identity(current)
                } else {
                    check_identity(&self.authenticate(credential)?)
                }
            };
            if pin_required {
                reauthorize()?;
            }
            let call = LifecycleCall {
                store: self.store,
                scope: &scope,
                subject: &principal.subject,
                reauthorize: &reauthorize,
            };
            return match &operation {
                Operation::Evaluator(kind, fields) => self
                    .evaluator
                    .ok_or(Error::Unavailable)?
                    .execute(&call, *kind, fields.clone())
                    .map(ResponseV1::Evaluator),
                Operation::Pack(kind, fields) => self
                    .packs
                    .ok_or(Error::Unavailable)?
                    .execute(&call, *kind, fields.clone())
                    .map(ResponseV1::Pack),
                Operation::Contract(kind, fields) => self
                    .contracts
                    .ok_or(Error::Unavailable)?
                    .execute(
                        &contracts::ContractCall {
                            operation: *kind,
                            scope: &scope,
                            subject: &principal.subject,
                            reauthorize: &reauthorize,
                        },
                        *kind,
                        fields.clone(),
                    )
                    .map(ResponseV1::Contract),
                Operation::Waiver(kind, fields) => self
                    .waiver
                    .ok_or(Error::Unavailable)?
                    .execute(&call, *kind, fields.clone())
                    .map(ResponseV1::Waiver),
                _ => Err(Error::InvalidRequest),
            };
        }
        Ok(match operation {
            Operation::Publication(_)
            | Operation::Evaluator(..)
            | Operation::Waiver(..)
            | Operation::Contract(..)
            | Operation::Pack(..) => {
                return Err(Error::Unavailable);
            }
            Operation::Mutation(mutation) => {
                let reauthorize = || {
                    let current = self.authenticate(credential)?;
                    if current.subject != principal.subject {
                        return Err(Error::Unauthorized);
                    }
                    authorize(&current, &scope, true)
                };
                ResponseV1::Mutation(self.store.mutate(
                    &scope,
                    &principal.subject,
                    mutation,
                    &binding,
                    reauthorize,
                )?)
            }
            Operation::Get(draft) => {
                ResponseV1::Version(self.store.get(&scope, &draft)?.try_into()?)
            }
            Operation::History { id, after, limit } => {
                let history = self.store.history(&scope, &id, after, limit)?;
                ResponseV1::History {
                    versions: history.versions,
                    next: history.next,
                }
            }
            Operation::Validate(draft) => ResponseV1::Validation(evaluation::validate(
                &scope,
                &self.store.get(&scope, &draft)?.candidate,
            )?),
            Operation::Simulate { draft, corpus } => ResponseV1::Simulation(evaluation::simulate(
                &scope,
                &self.store.get(&scope, &draft)?.candidate,
                &corpus,
            )?),
            Operation::Compare {
                before,
                after,
                corpus,
            } => ResponseV1::Comparison(evaluation::compare(
                &scope,
                &self.store.get(&scope, &before)?.candidate,
                &self.store.get(&scope, &after)?.candidate,
                &corpus,
            )?),
        })
    }
}

fn authenticate(authority: Option<&dyn Authenticator>, credential: &[u8]) -> Result<Principal> {
    if credential.is_empty() || credential.len() > MAX_CREDENTIAL {
        return Err(Error::Unauthorized);
    }
    let principal = authority
        .and_then(|authority| authority.authenticate(credential))
        .ok_or(Error::Unauthorized)?;
    if !matches!(
        principal.realm,
        Realm::Management | Realm::ManagementService
    ) || !valid_label(&principal.subject)
    {
        return Err(Error::Unauthorized);
    }
    if principal.grants.len() > 128 {
        return Err(Error::Unauthorized);
    }
    Ok(principal)
}

fn policy_reader(principal: &Principal, scope: &Scope) -> Result<()> {
    if principal.realm == Realm::Management
        && principal.grants.iter().any(|grant| {
            grant.scope == *scope
                && matches!(
                    grant.role,
                    Role::Viewer | Role::PolicyAuthor | Role::Publisher
                )
        })
    {
        Ok(())
    } else {
        Err(Error::NotFound)
    }
}

fn publication_reader_or_publisher(
    request: &PublicationRequest,
) -> fn(&Principal, &Scope) -> Result<()> {
    match request {
        PublicationRequest::Lifecycle { .. } => policy_reader,
        PublicationRequest::History { .. } => |principal, scope| authorize(principal, scope, false),
        _ => publisher,
    }
}

fn authorize_resource(
    principal: &Principal,
    scope: &Scope,
    operation: &wire::Operation,
    allow_self_publication: bool,
) -> Result<()> {
    use wire::Operation;
    match operation {
        Operation::Publication(request) => {
            publication_reader_or_publisher(request)(principal, scope)
        }
        Operation::Evaluator(EvaluatorOperation::Inspect, _)
        | Operation::Waiver(WaiverOperation::Inspect | WaiverOperation::Validate, _) => {
            authorize(principal, scope, false)
        }
        Operation::Evaluator(..) => publisher(principal, scope),
        Operation::Pack(
            PackOperation::Verify | PackOperation::Inspect | PackOperation::History,
            _,
        ) if principal.realm == Realm::Management => authorize(principal, scope, false),
        Operation::Pack(..) => publisher(principal, scope),
        Operation::Contract(
            ContractOperation::Browse
            | ContractOperation::Inspect
            | ContractOperation::ValidateSchema
            | ContractOperation::ValidateExample,
            _,
        ) => authorize(principal, scope, false),
        Operation::Contract(ContractOperation::Create, _) => contract_author(principal, scope),
        Operation::Contract(ContractOperation::Recover, _) => {
            contract_author(principal, scope).or_else(|_| publisher(principal, scope))
        }
        Operation::Contract(_, fields) => {
            publisher(principal, scope)?;
            if fields
                .get("acknowledge_self_publication")
                .and_then(Value::as_bool)
                == Some(true)
                && !allow_self_publication
            {
                return Err(Error::Unauthorized);
            }
            Ok(())
        }
        Operation::Waiver(..) => {
            if principal.realm == Realm::Management
                && principal
                    .grants
                    .iter()
                    .any(|grant| grant.scope == *scope && grant.role == Role::WaiverOperator)
            {
                Ok(())
            } else {
                Err(Error::NotFound)
            }
        }
        Operation::Get(_) | Operation::History { .. } => authorize(principal, scope, false),
        Operation::Mutation(_)
        | Operation::Validate(_)
        | Operation::Simulate { .. }
        | Operation::Compare { .. } => authorize(principal, scope, true),
    }
}

fn authorize(principal: &Principal, scope: &Scope, author: bool) -> Result<()> {
    if principal
        .grants
        .iter()
        .any(|grant| grant.scope == *scope && (!author || grant.role == Role::PolicyAuthor))
    {
        Ok(())
    } else {
        Err(Error::NotFound)
    }
}

fn contract_author(principal: &Principal, scope: &Scope) -> Result<()> {
    if principal.realm == Realm::Management && writ_kernel::ids::is_id(&principal.subject) {
        authorize(principal, scope, true)
    } else {
        Err(Error::NotFound)
    }
}

fn publisher(principal: &Principal, scope: &Scope) -> Result<()> {
    if principal.realm == Realm::Management
        && writ_kernel::ids::is_id(&principal.subject)
        && principal
            .grants
            .iter()
            .any(|grant| grant.scope == *scope && grant.role == Role::Publisher)
    {
        Ok(())
    } else {
        Err(Error::NotFound)
    }
}
