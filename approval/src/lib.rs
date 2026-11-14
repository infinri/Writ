//! Writ V2 human approval channel (M4-11; D-040 O-1, O-2, O-3, O-11, O-12; D-041).
//!
//! - [`listener`]: the loopback-only Hyper HTTP/1 approval listener and page, with a fixed
//!   origin `http://localhost:<port>` and RP id `localhost`, exact Host and Origin checks,
//!   CSRF tokens, a strict Content-Security-Policy, escaped untrusted text, bounded
//!   connections, headers, bodies and time, and read-only page sessions of at most 15
//!   minutes that never authorize a state change.
//! - [`backend`]: the broker operations the page uses ([`backend::Backend`]), with
//!   [`backend::LedgerBackend`] over any `ApprovalLedger`.
//! - [`page`]: escaped rendering of the broker's stored, fingerprint-covered context (R6).
//!
//! **Not yet here: the WebAuthn verifier.** The production `HumanVerifier` and
//! `RegistrationVerifier` over `webauthn-rs` need to persist the library's ceremony state
//! (`PasskeyAuthentication`, `PasskeyRegistration`) as bytes and to parse the browser's
//! JSON responses; both are `serde` types, and the approved dependency set of this crate
//! has no direct `serde` or `serde_json` dependency (both are in the lock through
//! `webauthn-rs-core`). Adding one is a dependency decision for the lead; until then the
//! listener runs only with the test verifiers of the ledger tests.

pub mod backend;
pub mod listener;
pub mod page;

/// The reviewed dependency types this crate builds on (closure anchors).
pub mod placeholder {
    /// WebAuthn relying-party verifier type from webauthn-rs.
    pub type Verifier = webauthn_rs::Webauthn;
    /// Serializable server-side passkey authentication ceremony state.
    pub type AuthenticationState = webauthn_rs::prelude::PasskeyAuthentication;
}
