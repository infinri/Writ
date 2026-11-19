//! Writ V2 authorization broker (M3): the only path to governed effects.
//!
//! The broker verifies sealed ingress, re-decides at use time through the pure
//! `writ-kernel`, consumes approval grants, issues and verifies permits, records
//! dispatch intent and receipts, and reconciles uncertain outcomes. Persistence
//! is behind [`store::LedgerStore`]; the SQLite local profile lives in
//! `writ-ledger-sqlite`. Plan: docs/implementation/M3-START-PLAN.md.
//!
//! Decision-free core (M3-02):
//! - [`keys`]: ingress keys per adapter ([`keys::KeyRing`]) and the permit key, supplied explicitly.
//! - [`ingress`]: seal verification (D-010) and the D-012 request fingerprint.
//! - [`clock`]: trusted time: the fallible [`clock::TrustedClock`] interface, the
//!   local-profile [`clock::HostClock`] (base plus `CLOCK_BOOTTIME` elapsed time),
//!   the persisted high-water mark ([`clock::observe`]) and operator acknowledgement.
//! - [`authorize`]: use-time re-decision through `writ-kernel`, bound to a
//!   shared hold of the policy gate.
//! - [`policy`]: the active authorization policy behind a shared/exclusive coordination gate.
//! - [`permit`]: permit issue and verification (fingerprint, MAC, expiry, active bundle).
//! - [`refusal`]: refusal codes and their agent-facing mapping.
//! - [`contracts`]: semantic validation of the proposed M3-06 acceptance
//!   contracts (registry entry, runner attestation, evaluation record scope).
//!
//! Approvals and grants (M3-03):
//! - [`grants`]: approval issuance through an [`grants::ApprovalAuthority`],
//!   the grant binding and its C-4 integrity check, per-key locks, and atomic
//!   single-use consumption in the T-reserve transaction of a
//!   [`store::GrantLedger`] ([`grants::GrantService`]).
//!
//! Execute lifecycle (M3-04):
//! - [`execute`]: T-dispatch, permit issue, T-adapter, T-observe,
//!   `present_permit`, reconciliation under the exclusive gate, D-026
//!   transaction evidence ([`execute::Broker`]); at most one dispatch per
//!   idempotency key, never a redispatch after uncertainty.
//! - [`effects`]: the controlled effect adapter interface (its own store of
//!   consumed permits and effects, reached only through the adapter).
//!
//! Mechanical runner (M3-07):
//! - [`runner`]: shared runner interfaces (command registry, snapshot content, sandbox).
//! - [`process`]: the process lifecycle gate between child spawning and ownership-lock
//!   changes ([`process::spawn`], [`process::OwnerLock`]) and the uncontained
//!   [`process::LocalProcessSandbox`].
//!
//! Authenticated approval and signoff contracts (M4-01, storage-independent):
//! - [`human`]: subjects, roles, the verifier boundary ([`human::HumanVerifier`]),
//!   separation and the service-permission rule.
//! - [`approval`]: approval requests and their state machine, decisions,
//!   challenges, revocations, signoffs, context fingerprints and I-6 codes.
//!   Decisions, signoffs and revocations decide eligibility themselves
//!   (human subject, permission in scope, separation; [`human::Actor`]).
//! - [`approval_store`]: the approval ledger interface ([`approval_store::ApprovalLedger`]):
//!   stored approval requests (created by [`execute::Broker::execute`] on
//!   `require_approval` without a grant), ceremonies with single-use
//!   challenges, decisions, revocations, signoffs, enrollment and expiry.
//! - Trusted constructors (verifier answers, enrollment records, stored rows)
//!   need a [`human::VerifierCapability`] or [`human::LedgerCapability`];
//!   `tests/trusted_constructors.rs` confines their constructors to the
//!   `writ-approval` crate, the SQLite ledger and test code.

pub mod acceptance;
pub mod approval;
pub mod approval_store;
pub mod authorize;
pub mod clock;
pub mod contracts;
pub mod effects;
pub mod evaluator_publication;
pub mod evaluator_rubric;
pub mod execute;
pub mod grants;
pub mod human;
pub mod ingress;
pub mod keys;
pub mod lifecycle;
mod mac;
pub mod permit;
pub mod policy;
pub mod process;
pub mod refusal;
pub mod runner;
pub mod store;
pub mod workflow;

/// The kernel this broker decides with, re-exported so that store
/// implementations and harnesses use exactly the same kernel types (requests,
/// revisions, timestamps, JSON) without a separate dependency.
pub use writ_kernel;

#[cfg(test)]
mod scenario_tests;
#[cfg(test)]
mod test_support;
