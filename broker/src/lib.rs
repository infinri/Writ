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

pub mod authorize;
pub mod clock;
pub mod contracts;
pub mod ingress;
pub mod keys;
mod mac;
pub mod permit;
pub mod policy;
pub mod refusal;
pub mod store;

#[cfg(test)]
mod scenario_tests;
#[cfg(test)]
mod test_support;
