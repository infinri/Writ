//! Writ V2 trusted kernel (M2).
//!
//! Deterministic and key-free: no I/O, no ambient clock, no secrets, no model SDK.
//! Trusted time and state are supplied explicitly by the caller.
//!
//! Modules:
//! - [`json`]: strict JSON parsing (D-017, D-018) and the D-006 canonical form and digest.
//! - [`ids`] and [`time`]: identifier patterns and the timestamp forms used by the contracts.
//! - [`contracts`]: typed ActionRequest, PolicyBundle, Decision and acceptance contracts.
//! - [`policy`]: fail-closed policy bundle loading.
//! - [`decision`]: the pure authorization decision (`decide`).
//! - [`transition`]: workflow transition structure and protection classification.
//! - [`acceptance`]: the pure acceptance-record check (`check-acceptance`).
//! - [`refusal`]: the agent-facing external refusal mapping.
//! - [`commands`]: JSON-text-in, JSON-text-out commands used by fixtures and harnesses.

pub mod acceptance;
pub mod commands;
pub mod contracts;
pub mod decision;
pub mod ids;
pub mod json;
pub mod policy;
pub mod refusal;
pub mod time;
pub mod transition;

/// Version of the kernel contract implemented by this crate.
pub const KERNEL_CONTRACT_VERSION: &str = "m2-0";
