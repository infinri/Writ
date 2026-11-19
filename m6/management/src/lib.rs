//! Disposable policy draft operations at the typed /manage/v1 boundary.

mod evaluation;
mod store;
mod wire;

#[cfg(feature = "fixtures")]
pub mod fixture;

pub use store::Store;
pub use wire::{error_response, execute_v1, RequestV1, ResponseV1};

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
    Agent,
    Adapter,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Role {
    Viewer,
    PolicyAuthor,
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
}

impl<'a> Service<'a> {
    pub fn new(store: &'a Store, authority: Option<&'a dyn Authenticator>) -> Self {
        Self { store, authority }
    }

    fn authenticate(&self, credential: &[u8]) -> Result<Principal> {
        if credential.is_empty() || credential.len() > MAX_CREDENTIAL {
            return Err(Error::Unauthorized);
        }
        let principal = self
            .authority
            .and_then(|authority| authority.authenticate(credential))
            .ok_or(Error::Unauthorized)?;
        if principal.realm != Realm::Management || !valid_label(&principal.subject) {
            return Err(Error::Unauthorized);
        }
        if principal.grants.len() > 128 {
            return Err(Error::Unauthorized);
        }
        Ok(principal)
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
        let write_role = !matches!(&operation, Operation::Get(_) | Operation::History { .. });
        authorize(&principal, &scope, write_role)?;
        Ok(match operation {
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
