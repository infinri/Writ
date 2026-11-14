//! Writ V2 human approval channel (M4-02, M4-11; D-040 O-1, O-2, O-3, O-11, O-12; D-041).
//!
//! - [`verifier`]: the production [`writ_broker::human::HumanVerifier`] and
//!   [`writ_broker::human::RegistrationVerifier`] over `webauthn-rs`'s safe passkey APIs
//!   (user verification required; ceremony state serialized only into the ledger; ES256
//!   reported for the ledger's DR-3 restriction).
//! - [`listener`]: the loopback-only Hyper HTTP/1 approval listener and page, with a fixed
//!   origin `http://localhost:<port>` and RP id `localhost`, exact Host and Origin checks,
//!   CSRF tokens, a strict Content-Security-Policy, escaped untrusted text, bounded
//!   connections, headers, bodies and time, and read-only page sessions of at most 15
//!   minutes that never authorize a state change.
//! - [`backend`]: the broker operations the page uses ([`backend::Backend`]), with
//!   [`backend::LedgerBackend`] over any `ApprovalLedger`; production wiring opens the
//!   ledger with its approval channel configured once (M1), for example
//!   `SqliteLedger::open(path)?.with_approval_channel(ApprovalChannel::new(v.clone(), v,
//!   channel)?)` with `v = Arc::new(PasskeyVerifier::for_listener(&listener)?)`, then
//!   `LedgerBackend::new(Arc::new(ledger), clock)`.
//! - [`page`]: escaped rendering of the broker's stored, fingerprint-covered context (R6).

pub mod backend;
pub mod listener;
pub mod page;
pub mod verifier;
