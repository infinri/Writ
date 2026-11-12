//! Writ V2 trusted kernel (M2).
//!
//! Deterministic and key-free: no I/O, no ambient clock, no secrets, no model SDK.
//! Trusted time and state are supplied explicitly by the caller.

/// Version of the kernel contract implemented by this crate.
pub const KERNEL_CONTRACT_VERSION: &str = "m2-0";
