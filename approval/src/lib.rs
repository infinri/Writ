//! Writ V2 human approval channel (M4, D-041): candidate skeleton for dependency review only.
//! No verifier or listener logic exists yet.

/// Placeholder that names the reviewed dependency types so the dependency closure resolves.
pub mod placeholder {
    /// WebAuthn relying-party verifier type from webauthn-rs.
    pub type Verifier = webauthn_rs::Webauthn;
    /// Serializable server-side passkey authentication ceremony state.
    pub type AuthenticationState = webauthn_rs::prelude::PasskeyAuthentication;
    /// Hyper HTTP/1 server connection builder.
    pub type Http1Builder = hyper::server::conn::http1::Builder;
    /// Tokio TCP stream adapted to Hyper's IO traits.
    pub type Connection = hyper_util::rt::TokioIo<tokio::net::TcpStream>;
    /// Broker crate the approval channel builds on.
    pub use writ_broker as broker;
}
