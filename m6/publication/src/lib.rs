//! Disposable development policy publication.

use std::fmt;

/// A publication, trust, or storage refusal.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Error {
    /// The input is outside the closed contract.
    Format,
    /// A payload did not pass proof or omitted a mandatory protection.
    InvalidPolicy,
    /// A document belongs to another scope.
    Scope,
    /// No current verification key is eligible.
    Trust,
    /// Invalid detached signature.
    Signature,
    /// Trusted time was unavailable or outside a validity window.
    Time,
    /// No fresh human publisher approval covers this exact intent.
    Authority,
    /// The expected selection revision changed.
    Conflict,
    /// Publication or selection repeats or downgrades without a valid rollback.
    Replay,
    /// The target has been retired and cannot be selected.
    Retired,
    /// An active dependent still references the retirement target.
    ActiveDependents,
    /// The owning service cannot establish a complete current dependency view.
    DependencyUnavailable,
    /// This compiler profile has no supported removal operation for the target.
    Unsupported,
    /// Durable state is absent or incoherent.
    Recovery,
    /// The store or audit transaction failed.
    Storage,
    /// A permit came from a different activation.
    StalePermit,
}

impl fmt::Display for Error {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(f, "{self:?}")
    }
}

impl std::error::Error for Error {}

mod wire;
pub use wire::{Approval, Intent, Operation, Publication, Scope, SignedPolicy};

mod signature;
pub use signature::{
    sign, verify, Protected, Purpose, Signer, VerificationKey, VerificationSource,
};

mod policy;
mod registry;
mod store;
pub use registry::{CurrentTrust, ManagedIntentSource, Registry, Selection, TrustAuthority};
pub use store::{Audit, Commit};
