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
//! - **Stale display:** a ceremony starts only if the fingerprint the page displayed is
//!   the stored request's current fingerprint (and the ledger binds the challenge to it).
//! - **Channel identity** is [`ListenerConfig::channel`], never a request field.
//! - **Responses** carry a strict Content-Security-Policy (no inline script or style),
//!   `nosniff`, `frame-ancestors 'none'`, `no-referrer` and `no-store`; refusals return
//!   the internal refusal code to the authenticated human interface only (O-6).

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
use writ_broker::approval_store::{CeremonyOp, CeremonyResult};
use writ_broker::human::SubjectId;
use writ_broker::writ_kernel::json;

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
    /// Live page sessions at once (the oldest expire first).
    pub max_sessions: usize,
    /// Ceremonies pending per session.
    pub max_pending: usize,
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
        }
    }
}

/// The listener's trusted configuration (O-2: the channel identity comes from here).
#[derive(Clone, Debug)]
pub struct ListenerConfig {
    /// Port on `127.0.0.1` (0 picks a free one; the origin uses the bound port).
    pub port: u16,
    /// Channel identity recorded on every decision.
    pub channel: String,
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
}

struct Session {
    csrf: String,
    request_id: String,
    fingerprint: String,
    expires: Instant,
    pending: HashMap<String, Pending>,
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
            _ => match path.strip_prefix("/request/") {
                Some(id) => view(state, id),
                None => html(
                    StatusCode::NOT_FOUND,
                    page::message_page("Not found", "No such page."),
                ),
            },
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
        _ => refused(StatusCode::NOT_FOUND, "path"),
    }
}

fn sessions(state: &State) -> std::sync::MutexGuard<'_, HashMap<String, Session>> {
    state
        .sessions
        .lock()
        .unwrap_or_else(PoisonError::into_inner)
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
    let (Some(token), Some(csrf)) = (random_token(), random_token()) else {
        return unavailable();
    };
    let fingerprint = request.fingerprint().as_str().to_string();
    let limits = &state.config.limits;
    {
        let mut sessions = sessions(state);
        let now = Instant::now();
        sessions.retain(|_, s| s.expires > now);
        while sessions.len() >= limits.max_sessions {
            let Some(oldest) = sessions
                .iter()
                .min_by_key(|(_, s)| s.expires)
                .map(|(k, _)| k.clone())
            else {
                break;
            };
            sessions.remove(&oldest);
        }
        sessions.insert(
            token.clone(),
            Session {
                csrf: csrf.clone(),
                request_id: id.clone(),
                fingerprint: fingerprint.clone(),
                expires: now + limits.session_ttl,
                pending: HashMap::new(),
            },
        );
    }
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
    let mut response = html(StatusCode::OK, body);
    let cookie = format!(
        "{SESSION_COOKIE}={token}; HttpOnly; SameSite=Strict; Path=/; Max-Age={}",
        limits.session_ttl.as_secs()
    );
    if let Ok(v) = HeaderValue::from_str(&cookie) {
        response.headers_mut().insert("set-cookie", v);
    }
    response
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

/// POST `/ceremony/begin`: a challenge for approving or denying the session's request.
fn begin<'a>(
    state: &State,
    token: &str,
    field: &dyn Fn(&str) -> Option<&'a str>,
) -> Response<Full> {
    let (request_id, op, idempotency_key) = {
        let mut sessions = sessions(state);
        let Some(session) = checked(&mut sessions, token, field("csrf")) else {
            return refused(StatusCode::FORBIDDEN, "session");
        };
        if field("request_id") != Some(session.request_id.as_str()) {
            return refused(StatusCode::FORBIDDEN, "target");
        }
        if field("fingerprint") != Some(session.fingerprint.as_str()) {
            return refused(StatusCode::CONFLICT, "stale display");
        }
        if session.pending.len() >= state.config.limits.max_pending {
            return refused(StatusCode::TOO_MANY_REQUESTS, "pending");
        }
        let request_id = session.request_id.clone();
        let op = match field("verdict") {
            Some("approve") => CeremonyOp::Approve {
                request_id: request_id.clone(),
            },
            Some("deny") => CeremonyOp::Deny {
                request_id: request_id.clone(),
            },
            _ => return refused(StatusCode::BAD_REQUEST, "verdict"),
        };
        let Some(key) = random_token() else {
            return unavailable();
        };
        (request_id, op, format!("page-{key}"))
    };
    // The displayed context must still be the stored one (stale display).
    match state.backend.request(&request_id) {
        Ok(Some((request, _))) => {
            if field("fingerprint") != Some(request.fingerprint().as_str()) {
                return refused(StatusCode::CONFLICT, "stale display");
            }
        }
        Ok(None) => return refused(StatusCode::NOT_FOUND, "APPROVAL_REQUEST_UNKNOWN"),
        Err(Unavailable) => return unavailable(),
    }
    let subject = match field("subject") {
        Some(s) if !s.is_empty() => SubjectId::new(&state.config.subject_issuer, s),
        _ => return refused(StatusCode::BAD_REQUEST, "subject"),
    };
    let issued = match state.backend.begin(&op, &subject, &idempotency_key) {
        Ok(Ok(issued)) => issued,
        Ok(Err(refusal)) => return refused(StatusCode::FORBIDDEN, refusal.code()),
        Err(Unavailable) => return unavailable(),
    };
    {
        let mut sessions = sessions(state);
        let Some(session) = checked(&mut sessions, token, field("csrf")) else {
            return refused(StatusCode::FORBIDDEN, "session");
        };
        session.pending.insert(
            issued.challenge_id.clone(),
            Pending {
                op,
                idempotency_key,
            },
        );
    }
    // The verifier's options are JSON; anything else is sent as a string.
    let options = std::str::from_utf8(&issued.client_options)
        .ok()
        .filter(|t| json::parse(t.as_bytes()).is_ok())
        .map(str::to_string)
        .unwrap_or_else(|| {
            format!(
                "\"{}\"",
                page::json_string(&String::from_utf8_lossy(&issued.client_options))
            )
        });
    respond(
        StatusCode::OK,
        "application/json",
        format!(
            "{{\"challenge_id\":\"{}\",\"expires_at\":\"{}\",\"options\":{options}}}",
            page::json_string(&issued.challenge_id),
            page::json_string(&issued.expires_at.to_string()),
        ),
    )
}

/// POST `/ceremony/finish`: the client's assertion for a challenge this session began.
/// The operation, target and idempotency key are the session's server-side record of the
/// begin; the channel is the configuration's.
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
    let outcome = state.backend.finish(
        &op,
        challenge_id,
        response.as_bytes(),
        &key,
        &state.config.channel,
    );
    let answer = match outcome {
        // Unknown outcome: the pending entry stays so an identical retry replays.
        Err(Unavailable) => return unavailable(),
        Ok(answer) => answer,
    };
    if let Some(session) = sessions(state).get_mut(token) {
        session.pending.remove(challenge_id);
    }
    match answer {
        Ok(outcome) => {
            let result = match outcome.result {
                CeremonyResult::Approved { .. } => "approved",
                CeremonyResult::Denied => "denied",
                CeremonyResult::Revoked { .. } => "revoked",
                CeremonyResult::SignedOff { .. } => "signed off",
                CeremonyResult::Enrolled { .. } => "enrolled",
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
