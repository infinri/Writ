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
//! - [`clock`]: the [`clock::TrustedClock`] interface (injected clock only).
//! - [`authorize`]: use-time re-decision through `writ-kernel`.
//! - [`policy`]: the active authorization policy behind a shared/exclusive coordination gate.
//! - [`permit`]: permit issue and verification (fingerprint, MAC, expiry, active bundle).
//! - [`refusal`]: refusal codes and their agent-facing mapping.

pub mod authorize;
pub mod clock;
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
