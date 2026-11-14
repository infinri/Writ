//! The loopback approval listener (M4-11; D-040 O-2, O-3, O-11, O-12; D-041 listener).
//!
//! A Hyper HTTP/1 server (no HTTP/2, no client networking) on a Tokio current-thread
//! runtime, bound only to `127.0.0.1` (the configuration has no address to change), with a
//! fixed origin `http://localhost:<port>` and WebAuthn RP id `localhost`:
//!
//! - **Transport bounds:** at most `max_connections` open connections (further ones are
//!   closed at accept); one request per connection (no keep-alive); header bytes bounded
//!   by Hyper's read buffer, a header count and a header read timeout; bodies bounded by
//!   `Content-Length` and while reading; every connection ends at `connection_timeout`.
//! - **Request checks:** the `Host` header must equal `localhost:<port>` exactly (DNS
//!   rebinding and other names refused); an absolute-form target is refused; `Origin`,
//!   when present, must equal the origin exactly and is required on every `POST`;
//!   `POST` bodies must be `application/x-www-form-urlencoded`.
//! - **Sessions** (at most 15 minutes, [`MAX_SESSION_SECONDS`]): viewing a stored request
//!   creates a server-side page session (random token in an `HttpOnly`, `SameSite=Strict`
//!   cookie) holding a CSRF token, the viewed request and the fingerprint displayed. A
//!   session is read-only: it never authorizes a state change. Approve and deny each need
//!   a fresh user-verified ceremony through the ledger (`begin_ceremony` then
//!   `finish_ceremony`); the session only carries the server-side operation, target and
//!   idempotency key between the two, so the client never chooses them.
//! - **Routes** (W-F, D-043; documented in M4-INTERFACES "Pages and registration binding"):
//!   `GET /request/<id>` (approve or deny), `GET /signoff/<id>` (a pending signoff of a
//!   stored promotion context), the operator forms `GET /revoke`, `GET /role` and
//!   `GET /enroll`, `POST /prepare` (a form's operation becomes the session's displayed
//!   target with the ledger's context for it; a read) and `GET /view` (that context),
//!   `POST /registration/begin` and `POST /registration/finish` (the new person's passkey
//!   registration, bound to the operator and this page session, D-043 Y2), and
//!   `POST /ceremony/begin` and `POST /ceremony/finish` for every operation. Every page
//!   displays only the ledger's context for the ceremony, which is what its fingerprint
//!   covers (R6); the operation, target, idempotency key, registration result and
//!   page-session id are server-side session state, never request fields once displayed.
//! - **Stale display:** a ceremony starts only if the fingerprint the page displayed is
//!   the current fingerprint of the stored request or of the ledger's recomputed context
//!   (and the ledger binds the challenge to it).
//! - **Channel identity**, verifier and relying party are the ledger's configured
//!   approval channel (`writ_broker::human::ApprovalChannel`, M1), never a request field.
//! - **Outstanding ceremonies** (L2): at most `max_pending` per session and
//!   `max_outstanding` issued, unexpired challenges and registration rows across all
//!   sessions (a challenge counted for at most the 120 s challenge lifetime, a registration
//!   row for its 600 s lifetime); further begins are refused. When `max_sessions` is
//!   reached, only a session without a pending ceremony or registration is evicted; if
//!   every session has one, the new session is refused (fail closed). Repeated hostile
//!   loopback requests can sustain this denial of service: 120 s bounds one challenge, not
//!   the overall outage (N2).
//! - **Responses** carry a strict Content-Security-Policy (no inline script or style),
//!   `nosniff`, `frame-ancestors 'none'`, `no-referrer` and `no-store`. A refused `begin`
//!   answers one generic `refused` (L1: the page session is not an authentication, so it
//!   never learns whether the subject is unknown, a service, revoked, without permission
//!   or without a credential; the specific code stays in the ledger's refusal audit);
//!   `/prepare` and `/registration/begin` answer the same generic `refused`, and a
//!   signoff id the ledger does not offer is one generic not-found page.
//!   After a begin, the page receives the verifier's options including the subject's
//!   allowed credential ids (WebAuthn `allowCredentials`; documented exposure). A refused
//!   `finish` returns the internal refusal code (O-6); a verifier that reports itself
//!   unavailable answers `unavailable` and keeps the pending ceremony (L6).

use std::collections::HashMap;
use std::convert::Infallible;
use std::future::{poll_fn, Future};
use std::io;
use std::net::{Ipv4Addr, SocketAddr, TcpListener as StdListener};
use std::pin::Pin;
use std::sync::atomic::{AtomicBool, AtomicUsize, Ordering};
use std::sync::{Arc, Mutex, PoisonError};
use std::task::{Context, Poll};
use std::time::{Duration, Instant};

use hyper::body::{Body, Bytes, Frame, Incoming, SizeHint};
use hyper::header::{HeaderMap, HeaderValue};
use hyper::server::conn::http1;
use hyper::{Method, Request, Response, StatusCode};
use hyper_util::rt::{TokioIo, TokioTimer};
use writ_broker::approval::{ApprovalCode, RevocationTarget};
use writ_broker::approval_store::{
    parse_permission, CeremonyOp, CeremonyOutcome, CeremonyResult, NewEnrollment, NewRole,
    OperatorSession, RoleChange, SignoffParties, REGISTRATION_TTL_SECONDS,
};
use writ_broker::human::{PrincipalKind, SubjectId, VerifiedRegistration, VerifierError};
use writ_broker::refusal::Refusal;
use writ_broker::writ_kernel::json::{self, Value};
use writ_broker::writ_kernel::time::Timestamp;

use crate::backend::{Backend, Unavailable};
use crate::page;

/// The WebAuthn relying-party id of the local profile.
pub const RP_ID: &str = "localhost";
/// The longest page session (O-3: read-only inbox sessions at most 15 minutes).
pub const MAX_SESSION_SECONDS: u64 = 900;
const SESSION_COOKIE: &str = "writ_session";
/// Hyper's smallest read buffer.
const MIN_HEADER_BUFFER: usize = 8192;

/// Transport and session bounds.
#[derive(Clone, Debug)]
pub struct Limits {
    /// Open connections at once.
    pub max_connections: usize,
    /// Request body bytes.
    pub max_body_bytes: usize,
    /// Header read buffer bytes (at least 8192).
    pub max_header_bytes: usize,
    /// Header count.
    pub max_headers: usize,
    /// Time to receive the complete header.
    pub header_timeout: Duration,
    /// Lifetime of one connection.
    pub connection_timeout: Duration,
    /// Page session lifetime (clamped to [`MAX_SESSION_SECONDS`]).
    pub session_ttl: Duration,
    /// Live page sessions at once (the oldest without a pending ceremony is evicted
    /// first; a new session is refused when every session has one).
    pub max_sessions: usize,
    /// Ceremonies pending per session.
    pub max_pending: usize,
    /// Issued, unexpired challenges begun through this listener, across all sessions.
    pub max_outstanding: usize,
}

impl Default for Limits {
    fn default() -> Limits {
        Limits {
            max_connections: 16,
            max_body_bytes: 64 * 1024,
            max_header_bytes: MIN_HEADER_BUFFER,
            max_headers: 32,
            header_timeout: Duration::from_secs(5),
            connection_timeout: Duration::from_secs(30),
            session_ttl: Duration::from_secs(MAX_SESSION_SECONDS),
            max_sessions: 64,
            max_pending: 4,
            max_outstanding: 32,
        }
    }
}

/// The lifetime for which a begun challenge is counted as outstanding (the ledger's
/// challenge lifetime is at most this, I-2).
const OUTSTANDING_SECONDS: u64 = 120;

/// The lifetime for which a begun registration row is counted as outstanding (the
/// ledger's row lifetime, D-043 Y2).
const REGISTRATION_SECONDS: u64 = REGISTRATION_TTL_SECONDS as u64;

/// The listener's trusted configuration. The channel identity recorded on decisions is the
/// ledger's configured approval channel (O-2, M1).
#[derive(Clone, Debug)]
pub struct ListenerConfig {
    /// Port on `127.0.0.1` (0 picks a free one; the origin uses the bound port).
    pub port: u16,
    /// Issuer (human realm) of the subjects who sign in on this page.
    pub subject_issuer: String,
    /// Bounds.
    pub limits: Limits,
}

/// A bound, not yet serving, approval listener.
pub struct ApprovalListener {
    listener: StdListener,
    state: Arc<State>,
}

struct Pending {
    op: CeremonyOp,
    idempotency_key: String,
    /// Counted as outstanding until then (L2).
    expires: Instant,
}

/// What a page session displayed: its server-held ceremony target (the client never
/// chooses or changes it after the display).
enum Shown {
    /// A stored approval request: approve or deny (the verdict chooses the operation).
    Request {
        request_id: String,
        fingerprint: String,
    },
    /// A signoff, revocation, enrollment or role change: one fixed operation with the
    /// ledger's context for it as displayed (R6).
    Op {
        title: &'static str,
        op: Box<CeremonyOp>,
        context: Value,
        fingerprint: String,
        /// For an enrollment: the operator who began the registration row (D-043 Y2); the
        /// ceremony is begun only for that subject.
        actor: Option<SubjectId>,
    },
}

/// A registration row this page session began (D-043 Y2).
struct Registration {
    subject: SubjectId,
    operator: SubjectId,
    /// Counted as outstanding until then (the row's lifetime).
    expires: Instant,
    /// The verified registration once finished (held server-side for the enrollment).
    verified: Option<VerifiedRegistration>,
}

struct Session {
    csrf: String,
    /// The page-session id (random, not secret, distinct from the cookie token) that
    /// registration rows and enrollments of this session are bound to (D-043 Y2).
    id: String,
    shown: Option<Shown>,
    expires: Instant,
    pending: HashMap<String, Pending>,
    registrations: HashMap<String, Registration>,
}

impl Session {
    fn busy(&self) -> bool {
        !self.pending.is_empty() || !self.registrations.is_empty()
    }

    fn outstanding(&self) -> usize {
        self.pending.len() + self.registrations.len()
    }
}

struct State {
    config: ListenerConfig,
    host: String,
    origin: String,
    backend: Arc<dyn Backend>,
    sessions: Mutex<HashMap<String, Session>>,
    active: AtomicUsize,
}

impl ApprovalListener {
    /// Bind `127.0.0.1:<port>` (loopback only).
    pub fn bind(config: ListenerConfig, backend: Arc<dyn Backend>) -> io::Result<ApprovalListener> {
        let listener = StdListener::bind((Ipv4Addr::LOCALHOST, config.port))?;
        listener.set_nonblocking(true)?;
        let port = listener.local_addr()?.port();
        let mut config = config;
        config.limits.session_ttl = config
            .limits
            .session_ttl
            .min(Duration::from_secs(MAX_SESSION_SECONDS));
        config.limits.max_header_bytes = config.limits.max_header_bytes.max(MIN_HEADER_BUFFER);
        Ok(ApprovalListener {
            listener,
            state: Arc::new(State {
                host: format!("localhost:{port}"),
                origin: format!("http://localhost:{port}"),
                config,
                backend,
                sessions: Mutex::new(HashMap::new()),
                active: AtomicUsize::new(0),
            }),
        })
    }

    /// The bound loopback address.
    pub fn local_addr(&self) -> io::Result<SocketAddr> {
        self.listener.local_addr()
    }

    /// The fixed origin (`http://localhost:<port>`) a verifier must be configured with.
    pub fn origin(&self) -> &str {
        &self.state.origin
    }

    /// The fixed RP id ([`RP_ID`]).
    pub fn rp_id(&self) -> &'static str {
        RP_ID
    }

    /// Serve until `stop` is set (checked at least every 50 ms), on a current-thread
    /// Tokio runtime owned by this call.
    pub fn serve(self, stop: Arc<AtomicBool>) -> io::Result<()> {
        let runtime = tokio::runtime::Builder::new_current_thread()
            .enable_io()
            .enable_time()
            .build()?;
        runtime.block_on(async move {
            let listener = tokio::net::TcpListener::from_std(self.listener)?;
            loop {
                let mut tick = Box::pin(tokio::time::sleep(Duration::from_millis(50)));
                let accepted = poll_fn(|cx| {
                    if stop.load(Ordering::SeqCst) {
                        return Poll::Ready(None);
                    }
                    if let Poll::Ready(r) = listener.poll_accept(cx) {
                        return Poll::Ready(Some(Some(r)));
                    }
                    match tick.as_mut().poll(cx) {
                        Poll::Ready(()) => Poll::Ready(Some(None)),
                        Poll::Pending => Poll::Pending,
                    }
                })
                .await;
                match accepted {
                    None => return Ok(()),
                    Some(None) | Some(Some(Err(_))) => continue,
                    Some(Some(Ok((stream, peer)))) => {
                        if !peer.ip().is_loopback() {
                            continue;
                        }
                        let Some(guard) = Slot::take(&self.state) else {
                            // Over the connection bound: closed unanswered.
                            continue;
                        };
                        let state = Arc::clone(&self.state);
                        tokio::spawn(connection(state, stream, guard));
                    }
                }
            }
        })
    }
}

/// One counted connection slot.
struct Slot(Arc<State>);

impl Slot {
    fn take(state: &Arc<State>) -> Option<Slot> {
        let max = state.config.limits.max_connections;
        state
            .active
            .try_update(Ordering::SeqCst, Ordering::SeqCst, |n| {
                (n < max).then_some(n + 1)
            })
            .ok()
            .map(|_| Slot(Arc::clone(state)))
    }
}

impl Drop for Slot {
    fn drop(&mut self) {
        self.0.active.fetch_sub(1, Ordering::SeqCst);
    }
}

async fn connection(state: Arc<State>, stream: tokio::net::TcpStream, slot: Slot) {
    let limits = state.config.limits.clone();
    let mut builder = http1::Builder::new();
    builder
        .timer(TokioTimer::new())
        .header_read_timeout(limits.header_timeout)
        .max_buf_size(limits.max_header_bytes)
        .max_headers(limits.max_headers)
        .keep_alive(false)
        .half_close(false);
    let service = hyper::service::service_fn(move |req| {
        let state = Arc::clone(&state);
        async move { Ok::<_, Infallible>(handle(&state, req).await) }
    });
    let conn = builder.serve_connection(TokioIo::new(stream), service);
    let _ = tokio::time::timeout(limits.connection_timeout, conn).await;
    drop(slot);
}

/// A complete response body.
pub struct Full(Option<Bytes>);

impl Body for Full {
    type Data = Bytes;
    type Error = Infallible;

    fn poll_frame(
        mut self: Pin<&mut Self>,
        _cx: &mut Context<'_>,
    ) -> Poll<Option<Result<Frame<Bytes>, Infallible>>> {
        Poll::Ready(self.0.take().map(|b| Ok(Frame::data(b))))
    }

    fn is_end_stream(&self) -> bool {
        self.0.is_none()
    }

    fn size_hint(&self) -> SizeHint {
        SizeHint::with_exact(self.0.as_ref().map_or(0, |b| b.len() as u64))
    }
}

const CSP: &str = "default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; \
img-src 'none'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'";

fn respond(status: StatusCode, content_type: &'static str, body: String) -> Response<Full> {
    let mut response = Response::new(Full(Some(Bytes::from(body))));
    *response.status_mut() = status;
    let h = response.headers_mut();
    for (name, value) in [
        ("content-type", content_type),
        ("content-security-policy", CSP),
        ("x-content-type-options", "nosniff"),
        ("x-frame-options", "DENY"),
        ("referrer-policy", "no-referrer"),
        ("cache-control", "no-store"),
        ("cross-origin-opener-policy", "same-origin"),
        ("cross-origin-resource-policy", "same-origin"),
        ("connection", "close"),
    ] {
        h.insert(name, HeaderValue::from_static(value));
    }
    response
}

fn html(status: StatusCode, body: String) -> Response<Full> {
    respond(status, "text/html; charset=utf-8", body)
}

fn refused(status: StatusCode, what: &str) -> Response<Full> {
    respond(
        status,
        "application/json",
        format!("{{\"error\":\"{}\"}}", page::json_string(what)),
    )
}

fn header<'a>(headers: &'a HeaderMap, name: &str) -> Option<&'a str> {
    let mut values = headers.get_all(name).iter();
    let first = values.next()?;
    if values.next().is_some() {
        return None;
    }
    first.to_str().ok()
}

/// Read the whole body within `max` bytes; `None` when larger or failed.
async fn read_body(mut body: Incoming, max: usize) -> Option<Vec<u8>> {
    let mut out = Vec::new();
    loop {
        let frame = poll_fn(|cx| Pin::new(&mut body).poll_frame(cx)).await;
        match frame {
            None => return Some(out),
            Some(Err(_)) => return None,
            Some(Ok(frame)) => {
                if let Ok(data) = frame.into_data() {
                    if out.len() + data.len() > max {
                        return None;
                    }
                    out.extend_from_slice(&data);
                }
            }
        }
    }
}

fn random_token() -> Option<String> {
    use std::io::Read;
    let mut bytes = [0u8; 32];
    std::fs::File::open("/dev/urandom")
        .and_then(|mut f| f.read_exact(&mut bytes))
        .ok()?;
    Some(bytes.iter().map(|b| format!("{b:02x}")).collect())
}

/// Constant-time equality of two tokens.
fn same_token(a: &str, b: &str) -> bool {
    a.len() == b.len()
        && a.bytes()
            .zip(b.bytes())
            .fold(0u8, |acc, (x, y)| acc | (x ^ y))
            == 0
}

fn cookie_token(headers: &HeaderMap) -> Option<String> {
    let mut found = None;
    for value in headers.get_all("cookie") {
        for part in value.to_str().ok()?.split(';') {
            if let Some(v) = part.trim().strip_prefix("writ_session=") {
                if found.is_some() {
                    return None;
                }
                found = Some(v.to_string());
            }
        }
    }
    found
}

async fn handle(state: &State, req: Request<Incoming>) -> Response<Full> {
    let (parts, body) = req.into_parts();
    // Exact Host; no absolute-form target (DNS rebinding and other names refused).
    if parts.uri.authority().is_some() || header(&parts.headers, "host") != Some(&state.host) {
        return refused(StatusCode::MISDIRECTED_REQUEST, "host");
    }
    let origin = header(&parts.headers, "origin");
    if parts.headers.contains_key("origin") && origin != Some(&state.origin) {
        return refused(StatusCode::FORBIDDEN, "origin");
    }
    let path = parts.uri.path();
    if parts.method == Method::GET {
        if parts.uri.query().is_some() {
            return refused(StatusCode::BAD_REQUEST, "query");
        }
        return match path {
            "/static/approval.js" => respond(
                StatusCode::OK,
                "text/javascript; charset=utf-8",
                page::SCRIPT.into(),
            ),
            "/static/approval.css" => respond(
                StatusCode::OK,
                "text/css; charset=utf-8",
                page::STYLE.into(),
            ),
            "/revoke" => form(state, page::FormKind::Revoke),
            "/role" => form(state, page::FormKind::Role),
            "/enroll" => form(state, page::FormKind::Enroll),
            "/view" => shown_view(state, cookie_token(&parts.headers).as_deref()),
            _ => {
                if let Some(id) = path.strip_prefix("/request/") {
                    view(state, id)
                } else if let Some(id) = path.strip_prefix("/signoff/") {
                    signoff_view(state, id)
                } else {
                    not_found()
                }
            }
        };
    }
    if parts.method != Method::POST {
        return refused(StatusCode::METHOD_NOT_ALLOWED, "method");
    }
    if origin != Some(&state.origin) {
        return refused(StatusCode::FORBIDDEN, "origin");
    }
    if header(&parts.headers, "content-type") != Some("application/x-www-form-urlencoded") {
        return refused(StatusCode::UNSUPPORTED_MEDIA_TYPE, "content type");
    }
    let max = state.config.limits.max_body_bytes;
    let declared = header(&parts.headers, "content-length").and_then(|v| v.parse::<usize>().ok());
    if declared.is_none_or(|n| n > max) {
        return refused(StatusCode::PAYLOAD_TOO_LARGE, "body");
    }
    let Some(bytes) = read_body(body, max).await else {
        return refused(StatusCode::PAYLOAD_TOO_LARGE, "body");
    };
    let Some(fields) = page::form(&bytes) else {
        return refused(StatusCode::BAD_REQUEST, "form");
    };
    let field = |name: &str| {
        fields
            .iter()
            .find(|(k, _)| k == name)
            .map(|(_, v)| v.as_str())
    };
    let Some(token) = cookie_token(&parts.headers) else {
        return refused(StatusCode::FORBIDDEN, "session");
    };
    match path {
        "/ceremony/begin" => begin(state, &token, &field),
        "/ceremony/finish" => finish(state, &token, &field),
        "/prepare" => prepare(state, &token, &field),
        "/registration/finish" => registration_finish(state, &token, &field),
        _ => refused(StatusCode::NOT_FOUND, "path"),
    }
}

fn not_found() -> Response<Full> {
    html(
        StatusCode::NOT_FOUND,
        page::message_page("Not found", "No such page."),
    )
}

fn sessions(state: &State) -> std::sync::MutexGuard<'_, HashMap<String, Session>> {
    state
        .sessions
        .lock()
        .unwrap_or_else(PoisonError::into_inner)
}

/// Drop expired sessions and, in the others, pending ceremonies past their challenge
/// lifetime and registration rows past theirs.
fn prune(sessions: &mut HashMap<String, Session>, now: Instant) {
    sessions.retain(|_, s| s.expires > now);
    for s in sessions.values_mut() {
        s.pending.retain(|_, p| p.expires > now);
        s.registrations.retain(|_, r| r.expires > now);
    }
}

/// Issued, unexpired challenges and registration rows begun through this listener (after
/// [`prune`]).
fn outstanding(sessions: &HashMap<String, Session>) -> usize {
    sessions.values().map(Session::outstanding).sum()
}

/// A new page session displaying `shown`: (cookie token, CSRF token). L2: the oldest
/// session without a pending ceremony or registration is evicted at the bound; when every
/// session has one, the new session is refused (fail closed).
fn new_session(
    state: &State,
    shown: Option<Shown>,
) -> Result<(String, String), Box<Response<Full>>> {
    let (Some(token), Some(csrf), Some(id)) = (random_token(), random_token(), random_token())
    else {
        return Err(Box::new(unavailable()));
    };
    let limits = &state.config.limits;
    let mut sessions = sessions(state);
    let now = Instant::now();
    prune(&mut sessions, now);
    while sessions.len() >= limits.max_sessions {
        let Some(oldest) = sessions
            .iter()
            .filter(|(_, s)| !s.busy())
            .min_by_key(|(_, s)| s.expires)
            .map(|(k, _)| k.clone())
        else {
            return Err(Box::new(refused(
                StatusCode::SERVICE_UNAVAILABLE,
                "sessions",
            )));
        };
        sessions.remove(&oldest);
    }
    sessions.insert(
        token.clone(),
        Session {
            csrf: csrf.clone(),
            id: format!("page-session-{id}"),
            shown,
            expires: now + limits.session_ttl,
            pending: HashMap::new(),
            registrations: HashMap::new(),
        },
    );
    Ok((token, csrf))
}

/// `body` with the new session's cookie.
fn with_cookie(state: &State, mut response: Response<Full>, token: &str) -> Response<Full> {
    let cookie = format!(
        "{SESSION_COOKIE}={token}; HttpOnly; SameSite=Strict; Path=/; Max-Age={}",
        state.config.limits.session_ttl.as_secs()
    );
    if let Ok(v) = HeaderValue::from_str(&cookie) {
        response.headers_mut().insert("set-cookie", v);
    }
    response
}

/// GET `/request/<id>`: the stored, fingerprint-covered context and a new page session.
fn view(state: &State, raw_id: &str) -> Response<Full> {
    let Some(id) = page::path_segment(raw_id) else {
        return refused(StatusCode::BAD_REQUEST, "request id");
    };
    let (request, status) = match state.backend.request(&id) {
        Ok(Some(found)) => found,
        Ok(None) => {
            return html(
                StatusCode::NOT_FOUND,
                page::message_page("Unknown request", "No stored approval request has this id."),
            )
        }
        Err(Unavailable) => return unavailable(),
    };
    let fingerprint = request.fingerprint().as_str().to_string();
    let shown = Shown::Request {
        request_id: id.clone(),
        fingerprint: fingerprint.clone(),
    };
    let (token, csrf) = match new_session(state, Some(shown)) {
        Ok(t) => t,
        Err(response) => return *response,
    };
    let expires = request.expires_at().to_string();
    let body = page::request_page(&page::Displayed {
        request_id: &id,
        status: status.as_str(),
        expires_at: &expires,
        fingerprint: &fingerprint,
        context: request.context(),
        csrf: &csrf,
        decidable: status == writ_broker::approval::ApprovalStatus::Pending,
    });
    with_cookie(state, html(StatusCode::OK, body), &token)
}

/// The fixed page title of a ceremony operation.
fn title(op: &CeremonyOp) -> &'static str {
    match op {
        CeremonyOp::Approve { .. } | CeremonyOp::Deny { .. } => "Approval request",
        CeremonyOp::Revoke { .. } => "Revocation",
        CeremonyOp::Signoff { .. } => "Signoff",
        CeremonyOp::Enroll { .. } => "Enrollment",
        CeremonyOp::RoleChange { .. } => "Role change",
        CeremonyOp::RegistrationStart { .. } => "Registration start",
    }
}

/// GET `/signoff/<id>`: the ledger's pending signoff binding of a stored promotion context
/// and the complete context a signoff ceremony binds (its reviewed parties and exclusions,
/// `delegation_chain: unavailable` and the expiry the ledger set), with a new page session.
fn signoff_view(state: &State, raw_id: &str) -> Response<Full> {
    let Some(id) = page::path_segment(raw_id) else {
        return refused(StatusCode::BAD_REQUEST, "signoff id");
    };
    let none = || {
        html(
            StatusCode::NOT_FOUND,
            page::message_page("No pending signoff", "No pending signoff has this id."),
        )
    };
    let binding = match state.backend.pending_signoff(&id) {
        Ok(Some(b)) => b,
        Ok(None) => return none(),
        Err(Unavailable) => return unavailable(),
    };
    // D-043 Y1: the ledger computes the effective expiry (as a begin at this trusted time
    // would); the page pins exactly that value as the requested expiry, re-reads the
    // context with it (unchanged unless a deadline moved, then refused) and displays it.
    // The begin computes min(its own cap, trusted deadline, this value) = this value, or a
    // different context, which the fingerprint comparison refuses as a stale display: the
    // page never shows one expiry and signs another.
    let mut op = CeremonyOp::Signoff {
        binding: Box::new(binding),
        parties: SignoffParties::default(),
        requested_expires_at: None,
    };
    let first = match state.backend.context(&op) {
        Ok(Ok(c)) => c,
        Ok(Err(_)) => return none(),
        Err(Unavailable) => return unavailable(),
    };
    let Some(effective) = first
        .context
        .get("expires_at")
        .and_then(Value::as_str)
        .and_then(Timestamp::parse_strict)
    else {
        return none();
    };
    if let CeremonyOp::Signoff {
        requested_expires_at,
        ..
    } = &mut op
    {
        *requested_expires_at = Some(effective);
    }
    let context = match state.backend.context(&op) {
        Ok(Ok(c)) if c.binding.context_fingerprint == first.binding.context_fingerprint => c,
        Ok(_) => return none(),
        Err(Unavailable) => return unavailable(),
    };
    let fingerprint = context.binding.context_fingerprint.as_str().to_string();
    let shown = Shown::Op {
        title: title(&op),
        fingerprint: fingerprint.clone(),
        context: context.context.clone(),
        op: Box::new(op),
        actor: None,
    };
    let (token, csrf) = match new_session(state, Some(shown)) {
        Ok(t) => t,
        Err(response) => return *response,
    };
    let body = page::target_page(&page::Target {
        then_enroll: false,
        title: "Signoff",
        fingerprint: &fingerprint,
        context: &context.context,
        csrf: &csrf,
    });
    with_cookie(state, html(StatusCode::OK, body), &token)
}

/// GET `/revoke`, `/role`, `/enroll`: an operator form with a new page session (nothing
/// displayed yet; the form goes to `/prepare`).
fn form(state: &State, kind: page::FormKind) -> Response<Full> {
    let (token, csrf) = match new_session(state, None) {
        Ok(t) => t,
        Err(response) => return *response,
    };
    with_cookie(
        state,
        html(StatusCode::OK, page::form_page(kind, &csrf)),
        &token,
    )
}

/// GET `/view`: the context this session prepared (as the ledger returned it, R6).
fn shown_view(state: &State, token: Option<&str>) -> Response<Full> {
    let Some(token) = token else {
        return not_found();
    };
    let mut sessions = sessions(state);
    let Some(session) = sessions.get(token) else {
        return not_found();
    };
    if session.expires <= Instant::now() {
        sessions.remove(token);
        return not_found();
    }
    match &session.shown {
        Some(Shown::Op {
            title,
            context,
            fingerprint,
            op,
            ..
        }) => html(
            StatusCode::OK,
            page::target_page(&page::Target {
                title,
                fingerprint,
                context,
                csrf: &session.csrf,
                // D-043 Y2: after the operator's start ceremony, the new person's passkey
                // is registered on this page and the enrollment is prepared from it.
                then_enroll: matches!(**op, CeremonyOp::RegistrationStart { .. }),
            }),
        ),
        _ => not_found(),
    }
}

fn unavailable() -> Response<Full> {
    refused(StatusCode::SERVICE_UNAVAILABLE, "unavailable")
}

/// The live session for `token` whose CSRF token is `csrf`.
fn checked<'a>(
    sessions: &'a mut HashMap<String, Session>,
    token: &str,
    csrf: Option<&str>,
) -> Option<&'a mut Session> {
    if sessions.get(token)?.expires <= Instant::now() {
        sessions.remove(token);
        return None;
    }
    let session = sessions.get_mut(token)?;
    (csrf.is_some_and(|c| same_token(c, &session.csrf))).then_some(session)
}

/// The verifier's options as JSON (anything else is sent as a string).
fn options_json(options: &[u8]) -> String {
    std::str::from_utf8(options)
        .ok()
        .filter(|t| json::parse(t.as_bytes()).is_ok())
        .map(str::to_string)
        .unwrap_or_else(|| {
            format!(
                "\"{}\"",
                page::json_string(&String::from_utf8_lossy(options))
            )
        })
}

/// A subject of this page's realm named by a form field.
fn subject_field(state: &State, value: Option<&str>) -> Option<SubjectId> {
    match value {
        Some(s) if !s.is_empty() => Some(SubjectId::new(&state.config.subject_issuer, s)),
        _ => None,
    }
}

/// POST `/ceremony/begin`: a challenge for the session's displayed target (approve or deny
/// a request; confirm a signoff, revocation, enrollment or role change).
///
/// A slot is reserved (per session and across all sessions, L2) under the session lock
/// before the ledger is asked, so concurrent begins never exceed the bounds; the
/// reservation becomes the pending ceremony or is released.
fn begin<'a>(
    state: &State,
    token: &str,
    field: &dyn Fn(&str) -> Option<&'a str>,
) -> Response<Full> {
    let limits = &state.config.limits;
    let (op, actor, idempotency_key, reservation) = {
        let mut sessions = sessions(state);
        let now = Instant::now();
        prune(&mut sessions, now);
        let total = outstanding(&sessions);
        let Some(session) = checked(&mut sessions, token, field("csrf")) else {
            return refused(StatusCode::FORBIDDEN, "session");
        };
        let (op, actor) = match &session.shown {
            Some(Shown::Request {
                request_id,
                fingerprint,
            }) => {
                if field("request_id") != Some(request_id.as_str()) {
                    return refused(StatusCode::FORBIDDEN, "target");
                }
                if field("fingerprint") != Some(fingerprint.as_str()) {
                    return refused(StatusCode::CONFLICT, "stale display");
                }
                let request_id = request_id.clone();
                let op = match field("verdict") {
                    Some("approve") => CeremonyOp::Approve { request_id },
                    Some("deny") => CeremonyOp::Deny { request_id },
                    _ => return refused(StatusCode::BAD_REQUEST, "verdict"),
                };
                (op, None)
            }
            Some(Shown::Op {
                op,
                fingerprint,
                actor,
                ..
            }) => {
                if field("request_id").is_some() {
                    return refused(StatusCode::FORBIDDEN, "target");
                }
                if field("fingerprint") != Some(fingerprint.as_str()) {
                    return refused(StatusCode::CONFLICT, "stale display");
                }
                if field("verdict") != Some("confirm") {
                    return refused(StatusCode::BAD_REQUEST, "verdict");
                }
                ((**op).clone(), actor.clone())
            }
            None => return refused(StatusCode::FORBIDDEN, "target"),
        };
        if session.outstanding() >= limits.max_pending || total >= limits.max_outstanding {
            return refused(StatusCode::TOO_MANY_REQUESTS, "pending");
        }
        let (Some(key), Some(slot)) = (random_token(), random_token()) else {
            return unavailable();
        };
        let idempotency_key = format!("page-{key}");
        let reservation = format!("reservation:{slot}");
        session.pending.insert(
            reservation.clone(),
            Pending {
                op: op.clone(),
                idempotency_key: idempotency_key.clone(),
                expires: now + Duration::from_secs(OUTSTANDING_SECONDS),
            },
        );
        (op, actor, idempotency_key, reservation)
    };
    let release = |response: Response<Full>| {
        if let Some(session) = sessions(state).get_mut(token) {
            session.pending.remove(&reservation);
        }
        response
    };
    // The displayed context must still be the stored one (stale display).
    let current = match &op {
        CeremonyOp::Approve { request_id } | CeremonyOp::Deny { request_id } => {
            match state.backend.request(request_id) {
                Ok(Some((request, _))) => request.fingerprint().as_str().to_string(),
                Ok(None) => {
                    return release(refused(StatusCode::NOT_FOUND, "APPROVAL_REQUEST_UNKNOWN"))
                }
                Err(Unavailable) => return release(unavailable()),
            }
        }
        _ => match state.backend.context(&op) {
            Ok(Ok(c)) => c.binding.context_fingerprint.as_str().to_string(),
            // L1: one generic refusal.
            Ok(Err(_)) => return release(refused(StatusCode::FORBIDDEN, "refused")),
            Err(Unavailable) => return release(unavailable()),
        },
    };
    if field("fingerprint") != Some(current.as_str()) {
        return release(refused(StatusCode::CONFLICT, "stale display"));
    }
    let Some(subject) = subject_field(state, field("subject")) else {
        return release(refused(StatusCode::BAD_REQUEST, "subject"));
    };
    // D-043 Y2: an enrollment is begun only by the operator who registered the credential
    // in this session (the ledger checks it again).
    if actor.as_ref().is_some_and(|a| a != &subject) {
        return release(refused(StatusCode::FORBIDDEN, "refused"));
    }
    let issued = match state.backend.begin(&op, &subject, &idempotency_key) {
        Ok(Ok(issued)) => issued,
        // L1: one generic refusal; the specific code is in the ledger's refusal audit.
        Ok(Err(_)) => return release(refused(StatusCode::FORBIDDEN, "refused")),
        Err(Unavailable) => return release(unavailable()),
    };
    {
        let mut sessions = sessions(state);
        let Some(session) = sessions.get_mut(token) else {
            return refused(StatusCode::FORBIDDEN, "session");
        };
        let Some(pending) = session.pending.remove(&reservation) else {
            return refused(StatusCode::FORBIDDEN, "session");
        };
        session.pending.insert(issued.challenge_id.clone(), pending);
    }
    respond(
        StatusCode::OK,
        "application/json",
        format!(
            "{{\"challenge_id\":\"{}\",\"expires_at\":\"{}\",\"options\":{}}}",
            page::json_string(&issued.challenge_id),
            page::json_string(&issued.expires_at.to_string()),
            options_json(&issued.client_options),
        ),
    )
}

/// POST `/ceremony/finish`: the client's assertion for a challenge this session began.
/// The operation, target and idempotency key are the session's server-side record of the
/// begin; the channel is the ledger's configured approval channel.
fn finish<'a>(
    state: &State,
    token: &str,
    field: &dyn Fn(&str) -> Option<&'a str>,
) -> Response<Full> {
    let Some(challenge_id) = field("challenge_id") else {
        return refused(StatusCode::BAD_REQUEST, "challenge");
    };
    let Some(response) = field("response") else {
        return refused(StatusCode::BAD_REQUEST, "response");
    };
    let (op, key) = {
        let mut sessions = sessions(state);
        let Some(session) = checked(&mut sessions, token, field("csrf")) else {
            return refused(StatusCode::FORBIDDEN, "session");
        };
        let Some(p) = session.pending.get(challenge_id) else {
            return refused(StatusCode::FORBIDDEN, "challenge");
        };
        (p.op.clone(), p.idempotency_key.clone())
    };
    let outcome = state
        .backend
        .finish(&op, challenge_id, response.as_bytes(), &key);
    let answer = match outcome {
        // Unknown outcome: the pending entry stays so an identical retry replays.
        Err(Unavailable) => return unavailable(),
        // L6: the verifier (or the channel) is unavailable; the ledger kept the challenge
        // issued, so the pending entry stays for a retry within its lifetime.
        Ok(Err(Refusal::Approval(ApprovalCode::AssertionInvalid(VerifierError::Unavailable)))) => {
            return unavailable()
        }
        Ok(answer) => answer,
    };
    if let Some(session) = sessions(state).get_mut(token) {
        session.pending.remove(challenge_id);
        // D-043 Y2: the row the operator's start ceremony created belongs to this session.
        if let (
            CeremonyOp::RegistrationStart {
                subject, operator, ..
            },
            Ok(CeremonyOutcome {
                result: CeremonyResult::RegistrationStarted { registration },
                ..
            }),
        ) = (&op, &answer)
        {
            session.registrations.insert(
                registration.registration_id.clone(),
                Registration {
                    subject: subject.clone(),
                    operator: operator.clone(),
                    expires: Instant::now() + Duration::from_secs(REGISTRATION_SECONDS),
                    verified: None,
                },
            );
        }
        // An enrollment consumed its registration row (or burnt the challenge); either way
        // the row is no longer this session's to use.
        if let CeremonyOp::Enroll {
            enrollment:
                NewEnrollment {
                    credential: Some(c),
                    ..
                },
            ..
        } = &op
        {
            session.registrations.remove(c.registration_id());
        }
    }
    match answer {
        Ok(CeremonyOutcome {
            result: CeremonyResult::RegistrationStarted { registration },
            replayed,
            ..
        }) => respond(
            StatusCode::OK,
            "application/json",
            format!(
                "{{\"result\":\"registration started\",\"replayed\":{replayed},\
                 \"registration_id\":\"{}\",\"expires_at\":\"{}\",\"options\":{}}}",
                page::json_string(&registration.registration_id),
                page::json_string(&registration.expires_at.to_string()),
                options_json(&registration.client_options),
            ),
        ),
        Ok(outcome) => {
            let result = match outcome.result {
                CeremonyResult::Approved { .. } => "approved",
                CeremonyResult::Denied => "denied",
                CeremonyResult::Revoked { .. } => "revoked",
                CeremonyResult::SignedOff { .. } => "signed off",
                CeremonyResult::Enrolled { .. } => "enrolled",
                CeremonyResult::RoleChanged { .. } => "role changed",
                CeremonyResult::RegistrationStarted { .. } => "registration started",
            };
            respond(
                StatusCode::OK,
                "application/json",
                format!(
                    "{{\"result\":\"{result}\",\"replayed\":{}}}",
                    outcome.replayed
                ),
            )
        }
        Err(refusal) => refused(StatusCode::FORBIDDEN, refusal.code()),
    }
}

/// Bound on linked identities and roles in one enrollment form.
const MAX_FORM_LINES: usize = 32;

/// `issuer subject` per line.
fn linked_lines(text: &str) -> Option<Vec<SubjectId>> {
    let lines: Vec<&str> = text
        .lines()
        .map(str::trim)
        .filter(|l| !l.is_empty())
        .collect();
    if lines.len() > MAX_FORM_LINES {
        return None;
    }
    lines
        .into_iter()
        .map(|l| {
            let (issuer, subject) = l.split_once(char::is_whitespace)?;
            let subject = subject.trim();
            (!issuer.is_empty() && !subject.is_empty()).then(|| SubjectId::new(issuer, subject))
        })
        .collect()
}

/// `permission tenant project` per line.
fn role_lines(text: &str) -> Option<Vec<NewRole>> {
    let lines: Vec<&str> = text
        .lines()
        .map(str::trim)
        .filter(|l| !l.is_empty())
        .collect();
    if lines.len() > MAX_FORM_LINES {
        return None;
    }
    lines
        .into_iter()
        .map(|l| {
            let words: Vec<&str> = l.split_whitespace().collect();
            let [permission, tenant, project] = words.as_slice() else {
                return None;
            };
            Some(NewRole {
                permission: parse_permission(permission)?,
                tenant_id: tenant.to_string(),
                project_id: project.to_string(),
            })
        })
        .collect()
}

/// A non-empty field.
fn nonempty(value: Option<&str>) -> Option<&str> {
    value.filter(|v| !v.is_empty())
}

/// The operation an operator form names (never an approval: those come only from a stored
/// request's page). `Err` names the malformed field.
fn form_op<'a>(
    state: &State,
    session: &Session,
    field: &dyn Fn(&str) -> Option<&'a str>,
) -> Result<(CeremonyOp, Option<SubjectId>), &'static str> {
    let target = || nonempty(field("target")).ok_or("target");
    let issuer = || nonempty(field("target_issuer")).unwrap_or(&state.config.subject_issuer);
    match field("operation") {
        Some("revoke") => {
            let target = match field("target_kind") {
                Some("grant") => RevocationTarget::Grant(target()?.to_string()),
                Some("signoff") => RevocationTarget::Signoff(target()?.to_string()),
                Some("credential") => RevocationTarget::Credential(target()?.to_string()),
                Some("subject") => RevocationTarget::Subject(SubjectId::new(issuer(), target()?)),
                Some("role") => RevocationTarget::Role(target()?.to_string()),
                _ => return Err("target kind"),
            };
            let reason = nonempty(field("reason")).ok_or("reason")?;
            Ok((
                CeremonyOp::Revoke {
                    target,
                    reason: reason.to_string(),
                },
                None,
            ))
        }
        Some("role-grant") => {
            let permission = field("permission")
                .and_then(parse_permission)
                .ok_or("permission")?;
            Ok((
                CeremonyOp::RoleChange {
                    change: RoleChange::Grant {
                        subject: SubjectId::new(issuer(), target()?),
                        permission,
                        tenant_id: nonempty(field("tenant_id")).ok_or("tenant")?.to_string(),
                        project_id: nonempty(field("project_id")).ok_or("project")?.to_string(),
                    },
                },
                None,
            ))
        }
        Some("role-revoke") => Ok((
            CeremonyOp::RoleChange {
                change: RoleChange::Revoke {
                    assignment_id: target()?.to_string(),
                },
            },
            None,
        )),
        Some("registration-start") => {
            let operator = subject_field(state, field("operator")).ok_or("operator")?;
            let subject = subject_field(state, field("subject")).ok_or("subject")?;
            Ok((
                CeremonyOp::RegistrationStart {
                    subject,
                    operator: operator.clone(),
                    operator_session: session.id.clone(),
                },
                Some(operator),
            ))
        }
        Some("enroll") => {
            let id = nonempty(field("registration_id")).ok_or("registration")?;
            let registration = session.registrations.get(id).ok_or("registration")?;
            let credential = registration.verified.clone().ok_or("registration")?;
            let linked = linked_lines(field("linked").unwrap_or("")).ok_or("linked")?;
            let roles = role_lines(field("roles").unwrap_or("")).ok_or("roles")?;
            Ok((
                CeremonyOp::Enroll {
                    enrollment: NewEnrollment {
                        subject: registration.subject.clone(),
                        kind: PrincipalKind::Human,
                        credential: Some(credential),
                        linked_identities: linked,
                        roles,
                    },
                    operator_session: session.id.clone(),
                },
                Some(registration.operator.clone()),
            ))
        }
        _ => Err("operation"),
    }
}

/// POST `/prepare`: an operator form's operation becomes this session's displayed target
/// with the ledger's context for it (a read; nothing is begun). Answers `{"view":"/view"}`;
/// a ledger refusal is one generic `refused` (L1).
fn prepare<'a>(
    state: &State,
    token: &str,
    field: &dyn Fn(&str) -> Option<&'a str>,
) -> Response<Full> {
    let (op, actor) = {
        let mut sessions = sessions(state);
        let Some(session) = checked(&mut sessions, token, field("csrf")) else {
            return refused(StatusCode::FORBIDDEN, "session");
        };
        // A displayed target never changes under a pending ceremony.
        if !session.pending.is_empty() {
            return refused(StatusCode::CONFLICT, "pending");
        }
        match form_op(state, session, field) {
            Ok(found) => found,
            Err(what) => return refused(StatusCode::BAD_REQUEST, what),
        }
    };
    let context = match state.backend.context(&op) {
        Ok(Ok(c)) => c,
        Ok(Err(_)) => return refused(StatusCode::FORBIDDEN, "refused"),
        Err(Unavailable) => return unavailable(),
    };
    let mut sessions = sessions(state);
    let Some(session) = checked(&mut sessions, token, field("csrf")) else {
        return refused(StatusCode::FORBIDDEN, "session");
    };
    if !session.pending.is_empty() {
        return refused(StatusCode::CONFLICT, "pending");
    }
    session.shown = Some(Shown::Op {
        title: title(&op),
        fingerprint: context.binding.context_fingerprint.as_str().to_string(),
        context: context.context,
        op: Box::new(op),
        actor,
    });
    respond(
        StatusCode::OK,
        "application/json",
        "{\"view\":\"/view\"}".into(),
    )
}

/// POST `/registration/finish`: the new person's registration response for a row this
/// session began, finished in this operator session (D-043 Y2). The verified result stays
/// in the session for `/prepare` (`operation=enroll`); it is never sent to the client.
fn registration_finish<'a>(
    state: &State,
    token: &str,
    field: &dyn Fn(&str) -> Option<&'a str>,
) -> Response<Full> {
    let Some(registration_id) = field("registration_id") else {
        return refused(StatusCode::BAD_REQUEST, "registration");
    };
    let Some(response) = field("response") else {
        return refused(StatusCode::BAD_REQUEST, "response");
    };
    let operator_session = {
        let mut sessions = sessions(state);
        let Some(session) = checked(&mut sessions, token, field("csrf")) else {
            return refused(StatusCode::FORBIDDEN, "session");
        };
        match session.registrations.get(registration_id) {
            Some(r) if r.verified.is_none() => OperatorSession {
                operator: r.operator.clone(),
                session_id: session.id.clone(),
            },
            _ => return refused(StatusCode::FORBIDDEN, "registration"),
        }
    };
    let answer = match state.backend.finish_registration(
        registration_id,
        response.as_bytes(),
        &operator_session,
    ) {
        Err(Unavailable) => return unavailable(),
        Ok(Err(Refusal::Approval(ApprovalCode::AssertionInvalid(VerifierError::Unavailable)))) => {
            return unavailable()
        }
        Ok(answer) => answer,
    };
    let mut sessions = sessions(state);
    let Some(session) = sessions.get_mut(token) else {
        return refused(StatusCode::FORBIDDEN, "session");
    };
    match answer {
        Ok(verified) => {
            let credential_id = verified.credential_id().to_string();
            if let Some(r) = session.registrations.get_mut(registration_id) {
                r.verified = Some(verified);
            }
            respond(
                StatusCode::OK,
                "application/json",
                format!(
                    "{{\"result\":\"registered\",\"credential_id\":\"{}\"}}",
                    page::json_string(&credential_id)
                ),
            )
        }
        Err(refusal) => {
            session.registrations.remove(registration_id);
            refused(StatusCode::FORBIDDEN, refusal.code())
        }
    }
}
