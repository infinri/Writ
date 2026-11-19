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
//! - **Fetch Metadata (LOW-1):** cookies are not port-scoped and another localhost port is
//!   same-site, so every page that creates or uses a session requires `Sec-Fetch-Site:
//!   same-origin` or `none`, while every `POST` and `GET /view` require exactly
//!   `same-origin`; `same-site`, `cross-site` and a missing header are refused (fail closed).
//! - **Read sessions** (D-044 item 3, Z1..Z4; at most 15 minutes, [`MAX_SESSION_SECONDS`]):
//!   the landing page `GET /` offers the login form and static assets only, with no record
//!   or session data. `POST /login/begin` (the subject identifier; no prior session) opens a
//!   pre-authentication page session (random token in an `HttpOnly`, `SameSite=Strict`
//!   cookie, a CSRF token and a page-session id) and asks the ledger for a purpose-separated
//!   `ReadSession` challenge bound to the subject, its active credentials, the page session
//!   and the configured channel; `POST /login/finish` completes it with a fresh
//!   user-verified assertion, binds the authenticated subject and credential (the ledger's
//!   read session) to the page session and rotates the session token and CSRF token. The
//!   session expires absolutely with the read session (never extended). Every page and every
//!   other `POST` needs an authenticated session.
//! - **Protected reads** (Z3): every lookup or display of stored record context (an approval
//!   request, a pending signoff, the context `/prepare` and `/view` show, the stale-display
//!   check of a begin) is the ledger's one protected read ([`Backend::read`]), which
//!   rechecks the read session, the subject, the credential and the scoped permission for
//!   the target inside its read transaction. No session, an unauthenticated or expired
//!   session, a revoked subject, credential or role, an out-of-scope target and an unknown
//!   or guessed id all receive one identical answer (404 "Not found", no session created;
//!   `POST /prepare`: 403 `refused`).
//! - **Mutations:** a read session never authorizes a state change. Approve, deny, signoff,
//!   revocation, enrollment and role change each need a fresh user-verified,
//!   operation-bound ceremony through the ledger (`begin_ceremony` then `finish_ceremony`)
//!   whose actor is always the read session's subject (a page naming another subject is
//!   refused); the session only carries the server-side operation, target and idempotency
//!   key between the two, so the client never chooses them.
//! - **Routes** (W-F, D-043; D-044): `GET /` (landing, login), `POST /login/begin`,
//!   `POST /login/finish`, `GET /request/<id>` (approve or deny), `GET /signoff/<id>` (a
//!   pending signoff of a stored promotion context), the operator forms `GET /revoke`,
//!   `GET /role` and `GET /enroll`, `POST /prepare` (a form's operation becomes the
//!   session's displayed target with the ledger's context for it; a protected read) and
//!   `GET /view` (that context, read again), `POST /registration/finish` (the new person's
//!   passkey registration for a row the operator's authenticated registration-start
//!   ceremony created, bound to the operator and this page session, D-043 Y2; there is no
//!   registration begin route), and `POST /ceremony/begin` and `POST /ceremony/finish` for
//!   every operation. Every page displays only the ledger's context for the ceremony, which
//!   is what its fingerprint covers (R6); the operation, target, idempotency key,
//!   registration result and page-session id are server-side session state, never request
//!   fields once displayed.
//! - **Stale display:** a ceremony starts only if the fingerprint the page displayed is
//!   the current fingerprint of the stored request or of the ledger's recomputed context
//!   (and the ledger binds the challenge to it).
//! - **Channel identity**, verifier and relying party are the ledger's configured
//!   approval channel (`writ_broker::human::ApprovalChannel`, M1), never a request field.
//! - **Outstanding ceremonies** (L2): at most `max_pending` per session and
//!   `max_outstanding` issued, unexpired challenges (login challenges included) and
//!   registration rows across all sessions (a challenge counted for at most the 120 s
//!   challenge lifetime, a registration row for its 600 s lifetime); further begins are
//!   refused. When `max_sessions` is reached, only a session without a pending ceremony or
//!   registration is evicted; if every session has one, the new session is refused (fail
//!   closed). Repeated hostile loopback requests can sustain this denial of service: 120 s
//!   bounds one challenge, not the overall outage (N2).
//! - **Responses** carry a strict Content-Security-Policy (no inline script or style),
//!   `nosniff`, `frame-ancestors 'none'`, `no-referrer` and `no-store`. A refused login or
//!   ceremony `begin` answers one generic `refused` (L1, Z1: it never tells whether the
//!   subject is unknown, a service, revoked, without permission or without a credential;
//!   the specific code stays in the ledger's refusal audit). After a successful begin, the
//!   page receives the verifier's options including the subject's allowed credential ids
//!   (WebAuthn `allowCredentials`; documented L1 exposure). A refused `finish` returns the
//!   internal refusal code (O-6); a verifier that reports itself unavailable answers
//!   `unavailable` and keeps the pending ceremony (L6).

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
    OperatorSession, ReadSession, ReadTarget, RecordView, RoleChange, READ_SESSION_MAX_SECONDS,
    REGISTRATION_TTL_SECONDS,
};
use writ_broker::human::{
    Permission, PrincipalKind, SubjectId, VerifiedRegistration, VerifierError,
};
use writ_broker::refusal::Refusal;
use writ_broker::writ_kernel::json::{self, Value};

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
    /// Live authenticated page sessions at once (the oldest without a pending ceremony is
    /// evicted first; a new session is refused when every session has one). Login
    /// (pre-authentication) sessions are bounded separately by `max_logins` and never
    /// evict an authenticated session.
    pub max_sessions: usize,
    /// Ceremonies pending per session.
    pub max_pending: usize,
    /// Issued, unexpired mutation challenges (and registration rows) begun through this
    /// listener, across all sessions. Login challenges never count here (M2 of the D-044
    /// review: a login flood never takes the slots an approver needs).
    pub max_outstanding: usize,
    /// Live login (pre-authentication) sessions at once, each holding at most one login
    /// challenge for at most 120 s ([`DEFAULT_MAX_LOGINS`]).
    pub max_logins: usize,
    /// Process-wide login-begin token bucket: its capacity ([`DEFAULT_LOGIN_BURST`]).
    pub login_burst: u32,
    /// Process-wide login-begin token bucket: tokens added per second
    /// ([`DEFAULT_LOGIN_PER_SECOND`]).
    pub login_per_second: u32,
    /// The fixed minimum latency of every `POST /login/begin` and `POST /login/finish`
    /// answer, success and refusal, real and decoy alike ([`DEFAULT_LOGIN_FLOOR_MS`]; D-044
    /// re-review M1): well above the real path, so timing does not tell a decoy.
    pub login_floor: Duration,
}

/// Default bound on live login sessions (and so on outstanding login challenges).
pub const DEFAULT_MAX_LOGINS: usize = 16;
/// Default login-begin burst (token bucket capacity).
pub const DEFAULT_LOGIN_BURST: u32 = 10;
/// Default login-begin refill rate (tokens per second).
pub const DEFAULT_LOGIN_PER_SECOND: u32 = 2;
/// Default latency floor of the login routes, in milliseconds (the real refused finish
/// measured 2 to 7 ms locally).
pub const DEFAULT_LOGIN_FLOOR_MS: u64 = 100;
/// The byte length of a decoy credential id when the ledger has no active credential.
const DEFAULT_DECOY_ID_BYTES: usize = 16;
/// How long a verifier outage seen at a login begin makes every login begin answer 503
/// (a global condition, never subject-specific; D-044 re-review item 5).
const VERIFIER_DOWN_SECONDS: u64 = 10;
/// The number of `allowCredentials` entries of every login begin (D-044 review M1): real
/// credential ids padded with decoys, or decoys only for a subject the ledger refuses.
pub const LOGIN_ALLOW_CREDENTIALS: usize = 4;
/// The `timeout` (milliseconds) of every login begin's options (the challenge lifetime).
const LOGIN_TIMEOUT_MS: u64 = 120_000;

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
            max_logins: DEFAULT_MAX_LOGINS,
            login_burst: DEFAULT_LOGIN_BURST,
            login_per_second: DEFAULT_LOGIN_PER_SECOND,
            login_floor: Duration::from_millis(DEFAULT_LOGIN_FLOOR_MS),
        }
    }
}

/// The lifetime for which a begun challenge is counted as outstanding (the ledger's
/// challenge lifetime is at most this, I-2).
const OUTSTANDING_SECONDS: u64 = 120;

/// The lifetime for which a begun registration row is counted as outstanding (the
/// ledger's row lifetime, D-043 Y2).
const REGISTRATION_SECONDS: u64 = REGISTRATION_TTL_SECONDS as u64;

/// The read session's lifetime (D-044 Z1, Z2): an authenticated page session never
/// outlives it.
const READ_SESSION_SECONDS: u64 = READ_SESSION_MAX_SECONDS as u64;

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

/// Lifecycle of a serving listener for the bounded graceful shutdown (M5-INTERFACES J-8;
/// M5a-11). The listener never handles signals itself: the process entry point does, and
/// drives this control.
///
/// - [`ServeControl::stop_accepting_work`]: the listener keeps accepting connections and
///   answers every request that arrives from now on with `503` `{"error":"stopping"}`; no new
///   ceremony, login or decision begins. A request already in progress finishes normally.
///   Ceremonies not finished are left (their challenges stay issued and expire).
/// - [`ServeControl::in_flight`] / [`ServeControl::wait_idle`]: the requests in progress (the
///   drain).
/// - [`ServeControl::exit`]: the accept loop ends and [`ApprovalListener::serve`] returns;
///   connections still open are dropped with the runtime.
///
/// A request counts itself in flight **before** it checks `stopping` (both sequentially
/// consistent), so a drain that observes zero after setting `stopping` cannot miss a request
/// that will still run.
#[derive(Debug, Default)]
pub struct ServeControl {
    stopping: AtomicBool,
    exit: AtomicBool,
    in_flight: AtomicUsize,
}

impl ServeControl {
    /// Answer every new request `503` from now on (J-8 step 1).
    pub fn stop_accepting_work(&self) {
        self.stopping.store(true, Ordering::SeqCst);
    }

    /// Whether [`ServeControl::stop_accepting_work`] was called.
    pub fn is_stopping(&self) -> bool {
        self.stopping.load(Ordering::SeqCst)
    }

    /// Requests in progress now.
    pub fn in_flight(&self) -> usize {
        self.in_flight.load(Ordering::SeqCst)
    }

    /// Wait until no request is in progress, or `deadline`; `true` when idle.
    pub fn wait_idle(&self, deadline: Instant) -> bool {
        loop {
            if self.in_flight() == 0 {
                return true;
            }
            if Instant::now() >= deadline {
                return false;
            }
            std::thread::sleep(Duration::from_millis(5));
        }
    }

    /// End the accept loop (checked at least every 50 ms).
    pub fn exit(&self) {
        self.exit.store(true, Ordering::SeqCst);
    }

    fn exiting(&self) -> bool {
        self.exit.load(Ordering::SeqCst)
    }

    /// Count a request in flight, or `None` while stopping.
    fn enter(self: &Arc<Self>) -> Option<InFlight> {
        self.in_flight.fetch_add(1, Ordering::SeqCst);
        let guard = InFlight(Arc::clone(self));
        (!self.is_stopping()).then_some(guard)
    }
}

/// One counted request in flight.
struct InFlight(Arc<ServeControl>);

impl Drop for InFlight {
    fn drop(&mut self) {
        self.0.in_flight.fetch_sub(1, Ordering::SeqCst);
    }
}

/// A bound, not yet serving, approval listener.
pub struct ApprovalListener {
    listener: StdListener,
    state: Arc<State>,
}

struct Pending {
    op: CeremonyOp,
    /// A decoy login (the ledger refused the begin; D-044 review M1): its finish is refused
    /// exactly like a bad assertion, without asking the ledger.
    decoy: bool,
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
    /// The authenticated read session bound to this page session (D-044 Z2); `None` for
    /// a login (pre-authentication) session, which reads and changes nothing.
    auth: Option<ReadSession>,
}

impl Session {
    fn busy(&self) -> bool {
        !self.pending.is_empty() || !self.registrations.is_empty()
    }

    /// Mutation challenges and registration rows held (L2). A login session's challenge is
    /// bounded by `max_logins` instead and never counts here (D-044 review M2).
    fn outstanding(&self) -> usize {
        if self.auth.is_none() {
            return 0;
        }
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
    /// Shutdown control (J-8).
    control: Arc<ServeControl>,
    /// Per-process random key of the login decoys (D-044 review M1): decoy credential ids
    /// are derived from it and the subject, so repeated probes see stable ids.
    decoy_key: Vec<u8>,
    /// Process-wide login-begin token bucket: (tokens, last refill).
    login_bucket: Mutex<(f64, Instant)>,
    /// Until when every login begin answers 503 after a verifier outage (item 5).
    verifier_down_until: Mutex<Option<Instant>>,
}

impl ApprovalListener {
    /// Bind `127.0.0.1:<port>` (loopback only).
    pub fn bind(config: ListenerConfig, backend: Arc<dyn Backend>) -> io::Result<ApprovalListener> {
        let listener = StdListener::bind((Ipv4Addr::LOCALHOST, config.port))?;
        ApprovalListener::new(listener, config, backend)
    }

    /// Serve an already bound listening socket (systemd socket activation, J-8). It must be
    /// bound to `127.0.0.1` on exactly the configured, nonzero port; anything else is refused
    /// with `InvalidInput`.
    pub fn from_std(
        listener: StdListener,
        config: ListenerConfig,
        backend: Arc<dyn Backend>,
    ) -> io::Result<ApprovalListener> {
        let addr = listener.local_addr()?;
        if config.port == 0
            || addr.port() != config.port
            || addr.ip() != std::net::IpAddr::V4(Ipv4Addr::LOCALHOST)
        {
            return Err(io::Error::from(io::ErrorKind::InvalidInput));
        }
        ApprovalListener::new(listener, config, backend)
    }

    fn new(
        listener: StdListener,
        config: ListenerConfig,
        backend: Arc<dyn Backend>,
    ) -> io::Result<ApprovalListener> {
        listener.set_nonblocking(true)?;
        let port = listener.local_addr()?.port();
        let mut config = config;
        config.limits.session_ttl = config
            .limits
            .session_ttl
            .min(Duration::from_secs(MAX_SESSION_SECONDS));
        config.limits.max_header_bytes = config.limits.max_header_bytes.max(MIN_HEADER_BUFFER);
        let burst = config.limits.login_burst;
        Ok(ApprovalListener {
            listener,
            state: Arc::new(State {
                host: format!("localhost:{port}"),
                origin: format!("http://localhost:{port}"),
                config,
                backend,
                sessions: Mutex::new(HashMap::new()),
                active: AtomicUsize::new(0),
                control: Arc::new(ServeControl::default()),
                decoy_key: random_bytes(32)
                    .ok_or_else(|| io::Error::other("random source unavailable"))?,
                login_bucket: Mutex::new((f64::from(burst), Instant::now())),
                verifier_down_until: Mutex::new(None),
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

    /// The shutdown control of this listener (J-8).
    pub fn control(&self) -> Arc<ServeControl> {
        Arc::clone(&self.state.control)
    }

    /// Serve until `stop` is set or [`ServeControl::exit`] is called (checked at least every
    /// 50 ms), on a current-thread Tokio runtime owned by this call.
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
                    if stop.load(Ordering::SeqCst) || self.state.control.exiting() {
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
        async move {
            // J-8: counted in flight before `stopping` is checked (ServeControl).
            let Some(_in_flight) = state.control.enter() else {
                return Ok::<_, Infallible>(refused(StatusCode::SERVICE_UNAVAILABLE, "stopping"));
            };
            Ok::<_, Infallible>(handle(&state, req).await)
        }
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

/// One request. The login routes are held to the fixed latency floor `login_floor`, counted
/// from the request's arrival, whatever their answer (D-044 re-review M1: a decoy refusal,
/// a real refusal and a success are not told apart by time while the real path stays below
/// the floor).
async fn handle(state: &State, req: Request<Incoming>) -> Response<Full> {
    let started = tokio::time::Instant::now();
    let login = req.method() == Method::POST && req.uri().path().starts_with("/login/");
    let response = route(state, req).await;
    if login {
        tokio::time::sleep_until(started + state.config.limits.login_floor).await;
    }
    response
}

async fn route(state: &State, req: Request<Incoming>) -> Response<Full> {
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
        let token = cookie_token(&parts.headers);
        let token = token.as_deref();
        return match path {
            // Z4: the landing page (login) and the static assets need no session and show
            // no record.
            "/" => html(StatusCode::OK, page::landing_page()),
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
            // LOW-1: every other page uses a session; it is served only to a navigation
            // from this origin or a direct one (address bar, bookmark).
            _ if !page_fetch_site(&parts.headers) => refused(StatusCode::FORBIDDEN, "fetch site"),
            "/revoke" => form(state, token, page::FormKind::Revoke),
            "/role" => form(state, token, page::FormKind::Role),
            "/enroll" => form(state, token, page::FormKind::Enroll),
            // `/view` shows a staged operation from an existing session; it is reached only by
            // the page script's same-origin navigation, never directly (re-review residual of
            // LOW-1: a reload after a cookie overwrite from another port would send `none`).
            "/view" if !same_origin(&parts.headers) => refused(StatusCode::FORBIDDEN, "fetch site"),
            "/view" => shown_view(state, token),
            _ => {
                if let Some(id) = path.strip_prefix("/request/") {
                    view(state, token, id)
                } else if let Some(id) = path.strip_prefix("/signoff/") {
                    signoff_view(state, token, id)
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
    // LOW-1: a state-changing or session-using POST comes only from this origin's own page
    // (a request from another localhost port is `same-site`, and cookies are not scoped by
    // port, so a cookie planted there is refused here); missing is refused (fail closed).
    // The pages send every POST with `fetch` from this origin, which is always `same-origin`.
    if !same_origin(&parts.headers) {
        return refused(StatusCode::FORBIDDEN, "fetch site");
    }
    let token = cookie_token(&parts.headers);
    // Z1: a login needs no prior session (a presented one is ended: every login rotates).
    if path == "/login/begin" {
        return login_begin(state, token.as_deref(), &field);
    }
    let Some(token) = token else {
        return match path {
            // Z3: an unauthenticated prepare is the one generic refusal of a read.
            "/prepare" => refused(StatusCode::FORBIDDEN, "refused"),
            _ => refused(StatusCode::FORBIDDEN, "session"),
        };
    };
    match path {
        "/login/finish" => login_finish(state, &token, &field),
        "/ceremony/begin" => begin(state, &token, &field),
        "/ceremony/finish" => finish(state, &token, &field),
        "/prepare" => prepare(state, &token, &field),
        "/registration/finish" => registration_finish(state, &token, &field),
        _ => refused(StatusCode::NOT_FOUND, "path"),
    }
}

/// LOW-1: `Sec-Fetch-Site` of a GET that uses a page session: `same-origin` (a navigation
/// from this listener's own pages) or `none` (a user-initiated navigation: address bar,
/// bookmark, reload). `same-site` (another localhost port, whose cookies the browser shares
/// because cookies are not port-scoped), `cross-site` and a missing header (a non-browser
/// client, or a browser without Fetch Metadata) are refused: fail closed.
fn page_fetch_site(headers: &HeaderMap) -> bool {
    matches!(
        header(headers, "sec-fetch-site"),
        Some("same-origin" | "none")
    )
}

/// `Sec-Fetch-Site: same-origin` exactly (every POST, and `GET /view`).
fn same_origin(headers: &HeaderMap) -> bool {
    header(headers, "sec-fetch-site") == Some("same-origin")
}

/// The one answer of every page that is not available to the request (D-044 Z3): an
/// unknown path, no or an expired session, a session that is not authenticated, an
/// unknown, guessed or out-of-scope record and a session whose subject, credential or
/// permission was revoked receive the same status and body, and no session is created.
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

/// Make room for one more authenticated session (L2): at `max_sessions` authenticated
/// sessions the oldest without a pending ceremony or registration is evicted; when every
/// one has one, the new session is refused (fail closed). Login sessions are bounded by
/// `max_logins` instead and are never evicted here, nor counted (D-044 review M2).
fn make_room(
    sessions: &mut HashMap<String, Session>,
    limits: &Limits,
) -> Result<(), Box<Response<Full>>> {
    while sessions.values().filter(|s| s.auth.is_some()).count() >= limits.max_sessions {
        let Some(oldest) = sessions
            .iter()
            .filter(|(_, s)| s.auth.is_some() && !s.busy())
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
    Ok(())
}

/// `response` with the session cookie `token` (`max_age` seconds).
fn with_cookie(mut response: Response<Full>, token: &str, max_age: Duration) -> Response<Full> {
    let cookie = format!(
        "{SESSION_COOKIE}={token}; HttpOnly; SameSite=Strict; Path=/; Max-Age={}",
        max_age.as_secs()
    );
    if let Ok(v) = HeaderValue::from_str(&cookie) {
        response.headers_mut().insert("set-cookie", v);
    }
    response
}

/// The lifetime of a login (pre-authentication) session: the challenge lifetime, bounded
/// by the configured session lifetime.
fn login_ttl(state: &State) -> Duration {
    state
        .config
        .limits
        .session_ttl
        .min(Duration::from_secs(OUTSTANDING_SECONDS))
}

/// The lifetime of an authenticated session: the configured session lifetime, bounded by
/// the read session's (Z2: absolute, at most 900 s, never extended).
fn session_ttl(state: &State) -> Duration {
    state
        .config
        .limits
        .session_ttl
        .min(Duration::from_secs(READ_SESSION_SECONDS))
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

/// The live, authenticated session for `token` whose CSRF token is `csrf` (Z2).
fn authed<'a>(
    sessions: &'a mut HashMap<String, Session>,
    token: &str,
    csrf: Option<&str>,
) -> Option<&'a mut Session> {
    checked(sessions, token, csrf).filter(|s| s.auth.is_some())
}

/// The live, authenticated session for a GET (cookie only): its read session and CSRF token.
fn authed_get(state: &State, token: Option<&str>) -> Option<(ReadSession, String)> {
    let token = token?;
    let mut sessions = sessions(state);
    if sessions.get(token)?.expires <= Instant::now() {
        sessions.remove(token);
        return None;
    }
    let session = sessions.get(token)?;
    Some((session.auth.clone()?, session.csrf.clone()))
}

/// Set what the authenticated session `token` displays.
fn show(state: &State, token: &str, shown: Shown) -> bool {
    match sessions(state).get_mut(token) {
        Some(s) if s.auth.is_some() && s.expires > Instant::now() => {
            s.shown = Some(shown);
            true
        }
        _ => false,
    }
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

/// `n` random bytes from the operating system's random source.
fn random_bytes(n: usize) -> Option<Vec<u8>> {
    use std::io::Read;
    let mut bytes = vec![0u8; n];
    std::fs::File::open("/dev/urandom")
        .and_then(|mut f| f.read_exact(&mut bytes))
        .ok()?;
    Some(bytes)
}

/// Unpadded base64url (the WebAuthn options' encoding).
fn base64url(bytes: &[u8]) -> String {
    const A: &[u8; 64] = b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_";
    let mut out = String::with_capacity(bytes.len().div_ceil(3) * 4);
    for chunk in bytes.chunks(3) {
        let n = chunk
            .iter()
            .enumerate()
            .fold(0u32, |acc, (i, b)| acc | (u32::from(*b) << (16 - 8 * i)));
        for i in 0..=chunk.len() {
            out.push(char::from(A[((n >> (18 - 6 * i)) & 63) as usize]));
        }
    }
    out
}

/// The bytes of a lowercase hex text (`None` when malformed).
fn unhex(text: &str) -> Option<Vec<u8>> {
    text.len()
        .is_multiple_of(2)
        .then(|| {
            (0..text.len())
                .step_by(2)
                .map(|i| u8::from_str_radix(text.get(i..i + 2)?, 16).ok())
                .collect()
        })
        .flatten()
}

/// Decoy credential id `index` of `subject` (D-044 review M1): bytes derived from the
/// per-process key and the subject, so repeated probes of one subject see the same ids and
/// different subjects different ones; its length is drawn (by the same derivation) from
/// `lengths`, the byte lengths of the credentials actually enrolled (re-review item 4;
/// [`DEFAULT_DECOY_ID_BYTES`] when none is), so a length never marks a decoy. A keyed
/// SHA-256 (the kernel's digest; no new dependency) is enough: the ids authorize nothing and
/// only need to be unpredictable without the key.
fn decoy_id(key: &[u8], subject: &SubjectId, index: usize, lengths: &[usize]) -> Option<String> {
    let block = |j: u32| {
        let mut input = key.to_vec();
        input.extend_from_slice(b"\0writ-login-decoy-v1\0");
        input.extend_from_slice(subject.issuer.as_bytes());
        input.push(0);
        input.extend_from_slice(subject.subject.as_bytes());
        input.push(0);
        input.extend_from_slice(&(index as u64).to_be_bytes());
        input.extend_from_slice(&j.to_be_bytes());
        unhex(json::Digest::of_bytes(&input).hex())
    };
    let pick = block(0)?;
    let choice = u64::from_be_bytes(pick.get(..8)?.try_into().ok()?);
    let len = match lengths {
        [] => DEFAULT_DECOY_ID_BYTES,
        some => some[usize::try_from(choice % some.len() as u64).ok()?],
    }
    .clamp(1, 1023);
    let mut bytes = Vec::with_capacity(len + 32);
    let mut j = 1;
    while bytes.len() < len {
        bytes.extend(block(j)?);
        j += 1;
    }
    Some(base64url(&bytes[..len]))
}

/// The options of a login begin, in one fixed shape whether the subject is real or not
/// (D-044 review M1): `{"publicKey":{"allowCredentials":[{"id":..,"type":"public-key"}
/// x LOGIN_ALLOW_CREDENTIALS],"challenge":..,"rpId":"localhost","timeout":120000,
/// "userVerification":"required"}}`, the ids sorted. `real` is the verifier's options (its
/// challenge and credential ids are kept, padded with the subject's decoys); `None` builds a
/// decoy with a random challenge. Passkey authentication options carry no hints or
/// extensions; transports and the verifier's timeout are dropped (client hints only). A
/// subject holds at most one active credential (the ledger supports no additional
/// credential, M4-ENROLLMENT-CEREMONY section 4); options listing more than
/// [`LOGIN_ALLOW_CREDENTIALS`] fail closed (`None`, the begin answers 503).
fn login_options(
    key: &[u8],
    subject: &SubjectId,
    real: Option<&[u8]>,
    lengths: &[usize],
) -> Option<String> {
    let (challenge, mut ids) = match real {
        Some(options) => {
            let v = json::parse(options).ok()?;
            let pk = v.get("publicKey")?;
            if pk.get("userVerification")?.as_str()? != "required" {
                return None;
            }
            let ids = pk
                .get("allowCredentials")?
                .as_arr()?
                .iter()
                .map(|c| c.get("id")?.as_str().map(str::to_string))
                .collect::<Option<Vec<_>>>()?;
            (pk.get("challenge")?.as_str()?.to_string(), ids)
        }
        None => (base64url(&random_bytes(32)?), Vec::new()),
    };
    if ids.len() > LOGIN_ALLOW_CREDENTIALS {
        return None;
    }
    let mut index = 0;
    while ids.len() < LOGIN_ALLOW_CREDENTIALS {
        ids.push(decoy_id(key, subject, index, lengths)?);
        index += 1;
    }
    ids.sort();
    let allow: Vec<String> = ids
        .iter()
        .map(|id| {
            format!(
                "{{\"id\":\"{}\",\"type\":\"public-key\"}}",
                page::json_string(id)
            )
        })
        .collect();
    Some(format!(
        "{{\"publicKey\":{{\"allowCredentials\":[{}],\"challenge\":\"{}\",\"rpId\":\"{RP_ID}\",\
         \"timeout\":{LOGIN_TIMEOUT_MS},\"userVerification\":\"required\"}}}}",
        allow.join(","),
        page::json_string(&challenge)
    ))
}

/// The expiry text of a decoy login challenge: now plus the challenge lifetime, in the
/// ledger's timestamp format.
fn decoy_expiry() -> Option<String> {
    let d = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .ok()?;
    let at = writ_broker::clock::timestamp_from_parts(
        i64::try_from(d.as_secs()).ok()?,
        d.subsec_nanos(),
    )?;
    Some(at.plus_seconds(OUTSTANDING_SECONDS as i64).to_string())
}

/// Take one token from the process-wide login-begin bucket (D-044 review M2).
fn login_token(state: &State) -> bool {
    let limits = &state.config.limits;
    let mut bucket = state
        .login_bucket
        .lock()
        .unwrap_or_else(PoisonError::into_inner);
    let now = Instant::now();
    let refill = now.duration_since(bucket.1).as_secs_f64() * f64::from(limits.login_per_second);
    bucket.0 = (bucket.0 + refill).min(f64::from(limits.login_burst));
    bucket.1 = now;
    if bucket.0 >= 1.0 {
        bucket.0 -= 1.0;
        true
    } else {
        false
    }
}

/// The one refusal of `/login/finish` (D-044 review LOW-6): every failure (no or another
/// session, a wrong CSRF token, an unknown, decoy or replayed challenge, a missing field,
/// every ledger refusal) answers it; the specific code stays in the ledger's audit.
fn login_refused() -> Response<Full> {
    refused(StatusCode::FORBIDDEN, "refused")
}

/// A login finish refused: the login (pre-authentication) session named by `token` is
/// removed, so a failed or decoy login never holds a login slot (re-review M2); an
/// authenticated session is never touched here.
fn end_login(state: &State, token: &str) -> Response<Full> {
    let mut sessions = sessions(state);
    if sessions.get(token).is_some_and(|s| s.auth.is_none()) {
        sessions.remove(token);
    }
    login_refused()
}

/// A verifier outage was seen at a login begin within [`VERIFIER_DOWN_SECONDS`].
fn verifier_down(state: &State) -> bool {
    let mut down = state
        .verifier_down_until
        .lock()
        .unwrap_or_else(PoisonError::into_inner);
    match *down {
        Some(until) if until > Instant::now() => true,
        _ => {
            *down = None;
            false
        }
    }
}

/// POST `/login/begin` (Z1): a read-session login challenge for the subject named by the
/// page, in a new pre-authentication session (cookie, CSRF token, page-session id); a
/// session the request presents is ended first (every login rotates the token).
///
/// D-044 review M1: the answer never tells whether the subject is enrolled. A begin the
/// ledger refuses (unknown, revoked or service subject, no active credential) answers a
/// decoy of exactly the same shape: status, headers, a session, a CSRF token, a challenge
/// id in the ledger's format, an expiry and options with [`LOGIN_ALLOW_CREDENTIALS`]
/// credential ids derived from the per-process key and the subject (stable across probes);
/// a real begin's ids are padded with the subject's decoys to the same count. The decoy's
/// pending entry makes `/login/finish` fail exactly like a bad assertion.
///
/// D-044 review M2: login begins are bounded by their own `max_logins` live login sessions
/// and a process-wide token bucket (`login_burst`, `login_per_second`), and never take the
/// mutation slots (`max_outstanding`) or evict an authenticated session.
fn login_begin<'a>(
    state: &State,
    presented: Option<&str>,
    field: &dyn Fn(&str) -> Option<&'a str>,
) -> Response<Full> {
    let Some(subject) = subject_field(state, field("subject")) else {
        return refused(StatusCode::BAD_REQUEST, "subject");
    };
    if !login_token(state) {
        return refused(StatusCode::TOO_MANY_REQUESTS, "rate");
    }
    // Re-review item 5: a verifier outage is global; while one was seen recently every
    // login begin answers 503, whatever the subject.
    if verifier_down(state) {
        return unavailable();
    }
    let Ok(lengths) = state.backend.credential_id_lengths() else {
        return unavailable();
    };
    let (Some(token), Some(csrf), Some(id), Some(key), Some(slot)) = (
        random_token(),
        random_token(),
        random_token(),
        random_token(),
        random_token(),
    ) else {
        return unavailable();
    };
    let page_session = format!("page-session-{id}");
    let idempotency_key = format!("login-{key}");
    let reservation = format!("reservation:{slot}");
    let op = CeremonyOp::ReadSession {
        subject: subject.clone(),
        page_session: page_session.clone(),
    };
    let ttl = login_ttl(state);
    {
        let mut sessions = sessions(state);
        if let Some(old) = presented {
            sessions.remove(old);
        }
        let now = Instant::now();
        prune(&mut sessions, now);
        // Re-review M2: at the bound the oldest login session is evicted (never refused), so
        // a lockout needs the full sustained rate; authenticated sessions are never touched.
        while sessions.values().filter(|s| s.auth.is_none()).count()
            >= state.config.limits.max_logins.max(1)
        {
            let Some(oldest) = sessions
                .iter()
                .filter(|(_, s)| s.auth.is_none())
                .min_by_key(|(_, s)| s.expires)
                .map(|(k, _)| k.clone())
            else {
                break;
            };
            sessions.remove(&oldest);
        }
        let mut pending = HashMap::new();
        pending.insert(
            reservation.clone(),
            Pending {
                op: op.clone(),
                decoy: false,
                idempotency_key: idempotency_key.clone(),
                expires: now + Duration::from_secs(OUTSTANDING_SECONDS),
            },
        );
        sessions.insert(
            token.clone(),
            Session {
                csrf: csrf.clone(),
                id: page_session,
                shown: None,
                expires: now + ttl,
                pending,
                registrations: HashMap::new(),
                auth: None,
            },
        );
    }
    let drop_session = |response: Response<Full>| {
        sessions(state).remove(&token);
        response
    };
    let (challenge_id, expires_at, options, decoy) =
        match state.backend.begin(&op, &subject, &idempotency_key) {
            Ok(Ok(issued)) => {
                let Some(options) = login_options(
                    &state.decoy_key,
                    &subject,
                    Some(&issued.client_options),
                    &lengths,
                ) else {
                    return drop_session(unavailable());
                };
                (
                    issued.challenge_id,
                    issued.expires_at.to_string(),
                    options,
                    false,
                )
            }
            // Item 5: no channel or a verifier outage is global: 503 for every subject, and
            // for the next VERIFIER_DOWN_SECONDS for every login begin (L6 unchanged).
            Ok(Err(Refusal::Approval(ApprovalCode::AssertionInvalid(
                VerifierError::Unavailable,
            )))) => {
                *state
                    .verifier_down_until
                    .lock()
                    .unwrap_or_else(PoisonError::into_inner) =
                    Some(Instant::now() + Duration::from_secs(VERIFIER_DOWN_SECONDS));
                return drop_session(unavailable());
            }
            // M1: a decoy of the same shape (the specific code is in the ledger's audit).
            Ok(Err(_)) => {
                let (Some(options), Some(hex), Some(expires_at)) = (
                    login_options(&state.decoy_key, &subject, None, &lengths),
                    random_bytes(16),
                    decoy_expiry(),
                ) else {
                    return drop_session(unavailable());
                };
                let id: String = hex.iter().map(|b| format!("{b:02x}")).collect();
                (format!("chl-{id}"), expires_at, options, true)
            }
            Err(Unavailable) => return drop_session(unavailable()),
        };
    {
        let mut sessions = sessions(state);
        let Some(session) = sessions.get_mut(&token) else {
            return login_refused();
        };
        let Some(mut pending) = session.pending.remove(&reservation) else {
            return login_refused();
        };
        pending.decoy = decoy;
        session.pending.insert(challenge_id.clone(), pending);
    }
    with_cookie(
        respond(
            StatusCode::OK,
            "application/json",
            format!(
                "{{\"challenge_id\":\"{}\",\"expires_at\":\"{}\",\"csrf\":\"{}\",\"options\":{}}}",
                page::json_string(&challenge_id),
                page::json_string(&expires_at),
                page::json_string(&csrf),
                options,
            ),
        ),
        &token,
        ttl,
    )
}

/// POST `/login/finish` (Z1, Z2): the assertion for this session's login challenge. On
/// success the authenticated subject and credential are bound to the page session and the
/// session token and CSRF token are rotated (the pre-authentication token stops working);
/// the session expires absolutely with the read session. Every failure is the one
/// [`login_refused`] (LOW-6), a decoy challenge's included (M1), and leaves the session
/// unauthenticated. A new authenticated session evicts only an idle authenticated one (L2).
fn login_finish<'a>(
    state: &State,
    token: &str,
    field: &dyn Fn(&str) -> Option<&'a str>,
) -> Response<Full> {
    let (Some(challenge_id), Some(response)) = (field("challenge_id"), field("response")) else {
        return end_login(state, token);
    };
    let found = {
        let mut sessions = sessions(state);
        match checked(&mut sessions, token, field("csrf")) {
            None => None,
            Some(session) => match session.pending.get(challenge_id) {
                // M1: a decoy is refused exactly like a bad assertion (the session ends).
                Some(p) if p.decoy => None,
                Some(p) if matches!(p.op, CeremonyOp::ReadSession { .. }) => {
                    Some((p.op.clone(), p.idempotency_key.clone(), session.id.clone()))
                }
                _ => None,
            },
        }
    };
    let Some((op, key, page_session)) = found else {
        return end_login(state, token);
    };
    let answer = match state
        .backend
        .finish(&op, challenge_id, response.as_bytes(), &key)
    {
        // Unknown outcome or L6: the pending entry stays so an identical retry replays.
        Err(Unavailable)
        | Ok(Err(Refusal::Approval(ApprovalCode::AssertionInvalid(VerifierError::Unavailable)))) => {
            return unavailable()
        }
        Ok(answer) => answer,
    };
    let (Some(new_token), Some(new_csrf)) = (random_token(), random_token()) else {
        return unavailable();
    };
    let ttl = session_ttl(state);
    let mut sessions = sessions(state);
    // The login session ends here whatever the answer (its token is rotated on success).
    if sessions.remove(token).is_none() {
        return login_refused();
    }
    let read = match answer {
        Ok(CeremonyOutcome {
            result: CeremonyResult::ReadSessionOpened { session: read },
            ..
        }) if read.page_session() == page_session
            && matches!(&op, CeremonyOp::ReadSession { subject, .. } if subject == read.subject()) =>
        {
            read
        }
        // Re-review M2: the refused login's session is gone (it already left the map).
        _ => return login_refused(),
    };
    if let Err(response) = make_room(&mut sessions, &state.config.limits) {
        return *response;
    }
    let subject = read.subject().subject.clone();
    // Z2: a new token and CSRF token; the old token is gone. Nothing displayed carries over.
    sessions.insert(
        new_token.clone(),
        Session {
            csrf: new_csrf.clone(),
            id: page_session,
            shown: None,
            expires: Instant::now() + ttl,
            pending: HashMap::new(),
            registrations: HashMap::new(),
            auth: Some(read),
        },
    );
    with_cookie(
        respond(
            StatusCode::OK,
            "application/json",
            format!(
                "{{\"result\":\"signed in\",\"subject\":\"{}\",\"csrf\":\"{}\"}}",
                page::json_string(&subject),
                page::json_string(&new_csrf)
            ),
        ),
        &new_token,
        ttl,
    )
}

/// GET `/request/<id>`: the stored, fingerprint-covered context of an approval request the
/// session's subject may decide (Z3: the ledger's protected read), displayed in the
/// session.
fn view(state: &State, token: Option<&str>, raw_id: &str) -> Response<Full> {
    let Some((read, csrf)) = authed_get(state, token) else {
        return not_found();
    };
    let Some(id) = page::path_segment(raw_id) else {
        return refused(StatusCode::BAD_REQUEST, "request id");
    };
    let target = ReadTarget::ApprovalRequest {
        request_id: id.clone(),
    };
    let (request, status) = match state.backend.read(&read, &target) {
        Ok(Ok(RecordView::ApprovalRequest { request, status })) => (request, status),
        Ok(_) => return not_found(),
        Err(Unavailable) => return unavailable(),
    };
    let fingerprint = request.fingerprint().as_str().to_string();
    let shown = Shown::Request {
        request_id: id.clone(),
        fingerprint: fingerprint.clone(),
    };
    if !show(state, token.unwrap_or(""), shown) {
        return not_found();
    }
    let expires = request.expires_at().to_string();
    let body = page::request_page(&page::Displayed {
        request_id: &id,
        status: status.as_str(),
        expires_at: &expires,
        fingerprint: &fingerprint,
        context: request.context(),
        csrf: &csrf,
        subject: &read.subject().subject,
        decidable: status == writ_broker::approval::ApprovalStatus::Pending,
    });
    html(StatusCode::OK, body)
}

/// The fixed page title of a ceremony operation.
fn title(op: &CeremonyOp) -> &'static str {
    match op {
        CeremonyOp::Approve { .. } | CeremonyOp::Deny { .. } => "Approval request",
        CeremonyOp::Revoke { .. } => "Revocation",
        CeremonyOp::Signoff { .. } => "Signoff",
        CeremonyOp::Waive { .. } => "Waiver",
        CeremonyOp::Enroll { .. } => "Enrollment",
        CeremonyOp::RoleChange { .. } => "Role change",
        CeremonyOp::RegistrationStart { .. } => "Registration start",
        CeremonyOp::ReadSession { .. } => "Sign in",
    }
}

/// GET `/signoff/<id>`: the pending signoff of a stored promotion context the session's
/// subject may sign (Z3: the ledger's protected read), with the complete context a signoff
/// ceremony binds (its reviewed parties and exclusions, `delegation_chain: unavailable` and
/// the effective expiry the ledger computed and pinned, D-043 Y1).
fn signoff_view(state: &State, token: Option<&str>, raw_id: &str) -> Response<Full> {
    let Some((read, csrf)) = authed_get(state, token) else {
        return not_found();
    };
    let Some(id) = page::path_segment(raw_id) else {
        return refused(StatusCode::BAD_REQUEST, "signoff id");
    };
    let target = ReadTarget::Signoff { signoff_id: id };
    let (op, context) = match state.backend.read(&read, &target) {
        Ok(Ok(RecordView::Ceremony { op, context })) => (op, context),
        Ok(_) => return not_found(),
        Err(Unavailable) => return unavailable(),
    };
    let fingerprint = context.binding.context_fingerprint.as_str().to_string();
    let shown = Shown::Op {
        title: title(&op),
        fingerprint: fingerprint.clone(),
        context: context.context.clone(),
        op,
        actor: None,
    };
    if !show(state, token.unwrap_or(""), shown) {
        return not_found();
    }
    let body = page::target_page(&page::Target {
        then_enroll: false,
        title: "Signoff",
        fingerprint: &fingerprint,
        context: &context.context,
        csrf: &csrf,
        subject: &read.subject().subject,
    });
    html(StatusCode::OK, body)
}

/// GET `/revoke`, `/role`, `/enroll`: an operator form in the authenticated session
/// (nothing displayed yet; the form goes to `/prepare`, whose read checks the target's
/// scoped permission). D-044 review LOW-3: the form itself is served only while the ledger
/// confirms the live subject, credential and permission over some scope (`revoker` for
/// `/revoke`, `operator` for `/role` and `/enroll`); otherwise the one 404.
fn form(state: &State, token: Option<&str>, kind: page::FormKind) -> Response<Full> {
    let Some((read, csrf)) = authed_get(state, token) else {
        return not_found();
    };
    let permission = match kind {
        page::FormKind::Revoke => Permission::Revoker,
        page::FormKind::Role | page::FormKind::Enroll => Permission::Operator,
    };
    match state.backend.read(&read, &ReadTarget::Form { permission }) {
        Ok(Ok(RecordView::Permitted)) => {}
        Ok(_) => return not_found(),
        Err(Unavailable) => return unavailable(),
    }
    html(
        StatusCode::OK,
        page::form_page(kind, &csrf, &read.subject().subject),
    )
}

/// GET `/view`: the context this session prepared, read again through the ledger's
/// protected read (Z3: the session, subject, credential and permission are rechecked); the
/// session then displays exactly what was read.
fn shown_view(state: &State, token: Option<&str>) -> Response<Full> {
    let Some((read, csrf)) = authed_get(state, token) else {
        return not_found();
    };
    let token = token.unwrap_or("");
    let op = match sessions(state).get(token).and_then(|s| s.shown.as_ref()) {
        Some(Shown::Op { op, .. }) => op.clone(),
        _ => return not_found(),
    };
    let context = match state.backend.read(&read, &ReadTarget::Ceremony(op.clone())) {
        Ok(Ok(RecordView::Ceremony { context, .. })) => context,
        Ok(_) => return not_found(),
        Err(Unavailable) => return unavailable(),
    };
    let fingerprint = context.binding.context_fingerprint.as_str().to_string();
    let mut sessions = sessions(state);
    let Some(Shown::Op {
        op: shown_op,
        context: shown_context,
        fingerprint: shown_fingerprint,
        title,
        ..
    }) = sessions.get_mut(token).and_then(|s| s.shown.as_mut())
    else {
        return not_found();
    };
    if **shown_op != *op {
        return not_found();
    }
    *shown_context = context.context.clone();
    *shown_fingerprint = fingerprint.clone();
    html(
        StatusCode::OK,
        page::target_page(&page::Target {
            title,
            fingerprint: &fingerprint,
            context: &context.context,
            csrf: &csrf,
            subject: &read.subject().subject,
            // D-043 Y2: after the operator's start ceremony, the new person's passkey
            // is registered on this page and the enrollment is prepared from it.
            then_enroll: matches!(*op, CeremonyOp::RegistrationStart { .. }),
        }),
    )
}

/// POST `/ceremony/begin`: a challenge for the session's displayed target (approve or deny
/// a request; confirm a signoff, revocation, enrollment or role change), always for the
/// session's authenticated subject (Z3: the actor of every mutation is the read session's
/// subject; a different `subject` field is refused). The read session authorizes nothing
/// here: the ledger issues a fresh operation-bound challenge.
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
    let (op, actor, read, idempotency_key, reservation) = {
        let mut sessions = sessions(state);
        let now = Instant::now();
        prune(&mut sessions, now);
        let total = outstanding(&sessions);
        let Some(session) = authed(&mut sessions, token, field("csrf")) else {
            return refused(StatusCode::FORBIDDEN, "session");
        };
        let Some(read) = session.auth.clone() else {
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
                decoy: false,
                idempotency_key: idempotency_key.clone(),
                expires: now + Duration::from_secs(OUTSTANDING_SECONDS),
            },
        );
        (op, actor, read, idempotency_key, reservation)
    };
    let release = |response: Response<Full>| {
        if let Some(session) = sessions(state).get_mut(token) {
            session.pending.remove(&reservation);
        }
        response
    };
    // Z3: the actor is the read session's subject; a page naming another is refused.
    let subject = read.subject().clone();
    if field("subject").is_some_and(|s| subject_field(state, Some(s)).as_ref() != Some(&subject)) {
        return release(refused(StatusCode::FORBIDDEN, "refused"));
    }
    // The displayed context must still be the stored one (stale display), read through the
    // protected read (the session, credential and permission rechecked).
    let target = match &op {
        CeremonyOp::Approve { request_id } | CeremonyOp::Deny { request_id } => {
            ReadTarget::ApprovalRequest {
                request_id: request_id.clone(),
            }
        }
        other => ReadTarget::Ceremony(Box::new(other.clone())),
    };
    let current = match state.backend.read(&read, &target) {
        Ok(Ok(RecordView::ApprovalRequest { request, .. })) => {
            request.fingerprint().as_str().to_string()
        }
        Ok(Ok(RecordView::Ceremony { context, .. })) => {
            context.binding.context_fingerprint.as_str().to_string()
        }
        // Z3, L1: one generic refusal.
        Ok(Err(_) | Ok(RecordView::Permitted)) => {
            return release(refused(StatusCode::FORBIDDEN, "refused"))
        }
        Err(Unavailable) => return release(unavailable()),
    };
    if field("fingerprint") != Some(current.as_str()) {
        return release(refused(StatusCode::CONFLICT, "stale display"));
    }
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

/// POST `/ceremony/finish`: the client's assertion for a challenge this authenticated
/// session began. The operation, target and idempotency key are the session's server-side
/// record of the begin; the channel is the ledger's configured approval channel.
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
        let Some(session) = authed(&mut sessions, token, field("csrf")) else {
            return refused(StatusCode::FORBIDDEN, "session");
        };
        match session.pending.get(challenge_id) {
            Some(p) if !matches!(p.op, CeremonyOp::ReadSession { .. }) => {
                (p.op.clone(), p.idempotency_key.clone())
            }
            _ => return refused(StatusCode::FORBIDDEN, "challenge"),
        }
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
                CeremonyResult::Waived { .. } => "waived",
                CeremonyResult::Enrolled { .. } => "enrolled",
                CeremonyResult::RoleChanged { .. } => "role changed",
                CeremonyResult::RegistrationStarted { .. } => "registration started",
                // Never for a mutation's challenge (purpose separation, Z1).
                CeremonyResult::ReadSessionOpened { .. } => {
                    return refused(StatusCode::FORBIDDEN, "refused")
                }
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
/// request's page). `Err` names the malformed field. The operator of a registration start
/// is the session's authenticated subject (Z3; an `operator` field naming another is
/// refused).
fn form_op<'a>(
    state: &State,
    session: &Session,
    operator: &SubjectId,
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
            if let Some(named) = nonempty(field("operator")) {
                if subject_field(state, Some(named)).as_ref() != Some(operator) {
                    return Err("operator");
                }
            }
            let subject = subject_field(state, field("subject")).ok_or("subject")?;
            Ok((
                CeremonyOp::RegistrationStart {
                    subject,
                    operator: operator.clone(),
                    operator_session: session.id.clone(),
                },
                Some(operator.clone()),
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

/// POST `/prepare`: an operator form's operation becomes this authenticated session's
/// displayed target with the ledger's context for it (Z3: the protected read; nothing is
/// begun). Answers `{"view":"/view"}`; no or an unauthenticated session, a wrong CSRF
/// token and every ledger refusal are the one generic `refused` (L1, Z3).
fn prepare<'a>(
    state: &State,
    token: &str,
    field: &dyn Fn(&str) -> Option<&'a str>,
) -> Response<Full> {
    let (op, actor, read) = {
        let mut sessions = sessions(state);
        let Some(session) = authed(&mut sessions, token, field("csrf")) else {
            return refused(StatusCode::FORBIDDEN, "refused");
        };
        let Some(read) = session.auth.clone() else {
            return refused(StatusCode::FORBIDDEN, "refused");
        };
        // A displayed target never changes under a pending ceremony.
        if !session.pending.is_empty() {
            return refused(StatusCode::CONFLICT, "pending");
        }
        match form_op(state, session, read.subject(), field) {
            Ok((op, actor)) => (op, actor, read),
            Err(what) => return refused(StatusCode::BAD_REQUEST, what),
        }
    };
    let context = match state
        .backend
        .read(&read, &ReadTarget::Ceremony(Box::new(op.clone())))
    {
        Ok(Ok(RecordView::Ceremony { context, .. })) => context,
        Ok(_) => return refused(StatusCode::FORBIDDEN, "refused"),
        Err(Unavailable) => return unavailable(),
    };
    let mut sessions = sessions(state);
    let Some(session) = authed(&mut sessions, token, field("csrf")) else {
        return refused(StatusCode::FORBIDDEN, "refused");
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
/// authenticated operator session began, finished in this operator session (D-043 Y2). The
/// verified result stays in the session for `/prepare` (`operation=enroll`); it is never
/// sent to the client.
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
        let Some(session) = authed(&mut sessions, token, field("csrf")) else {
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
