//! `writ-serviced`: the trusted Writ service (M5a-04; D-045 M5-D3, M5-D7; D-046;
//! [M5-INTERFACES](../../docs/implementation/M5-INTERFACES.md) J-1, J-2, J-3, J-6, J-9,
//! J-11.8).
//!
//! **Single owner (J-1).** One process owns the ledger through the `OwnerLock` path and every
//! store bound to it, and composes: the snapshot store (`writ-snapshots-1`, bound to the
//! ledger, `immutable()` only with a [`VerifiedDeployment`]), the key source
//! (`FileKeySource`, admitted through the ledger's key registry), the loopback approval
//! listener (when configured), service-side sealing ([`seal`]) and the local RPC endpoints.
//! `writ-approval-server` stays a standalone development entry point; it and this service
//! cannot own one ledger at once (`OWNED_ELSEWHERE`).
//!
//! **Order of [`Service::open`]** (each step fails closed before the next): the deployment
//! proof (only when configured; on this workstation there is none); the ledger
//! ([`SqliteLedger::open`], with the approval channel when configured); trusted time
//! against the ledger floor (a blocked startup rollback refuses); the snapshot store bound to
//! the ledger identity, with the proof; the key directory, each key admitted through the
//! ledger registry.
//!
//! **Local RPC (J-2)** ([`Running`]): `host.sock` and `admin.sock`. On accept the service
//! reads `SO_PEERCRED` and decides admission before reading a byte (unlisted uid, uid or gid
//! 65534, uid 0 on `host.sock` refused); then the connection caps; a refused or capped
//! connection is closed unread. Frames are read through a receive path that refuses
//! ancillary data. One request in flight per connection; any transport or protocol
//! violation answers the generic error and closes.
//!
//! - `host.sock`: `snapshot.ingest.begin`, `snapshot.ingest.file` (followed by data frames),
//!   `snapshot.ingest.commit` (scope from the connection binding, J-4); `status.receipt`
//!   (the key is scoped by OS principal and binding, J-2). `host.event` and
//!   `effect.request` are M5b (sealing exists in [`seal`]) and refuse here.
//! - `admin.sock` (uids `admin_uids`, default `[0]`): `readiness`; `keys.revoke`
//!   (J-11.8: explicit scope, registered purpose and digest, OS-peer provenance, ordering
//!   row, idempotent). `keys.reload`, `registry.*` refuse here (later tasks).
//!
//! **Not in this crate yet (recorded):** systemd socket activation (J-8; receiving
//! inherited descriptors needs either `unsafe` or a reviewed safe path, M5a-11), graceful
//! shutdown on signals (M5a-11), the runner supervisor and job launcher client (M5a-07,
//! J-11), the broker execute path over an effect adapter (M5a-09, M5b-03).

pub mod config;
pub mod seal;

use std::io::{self, Write};
use std::os::unix::net::{UnixListener, UnixStream};
use std::path::Path;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Mutex, PoisonError};
use std::thread::{self, JoinHandle};
use std::time::Duration;

use writ_approval::backend::LedgerBackend;
use writ_approval::listener::{ApprovalListener, Limits as PageLimits, ListenerConfig, RP_ID};
use writ_approval::verifier::PasskeyVerifier;
use writ_broker::clock::{observe, Boottime, HostClock, SystemWall};
use writ_broker::human::ApprovalChannel;
use writ_broker::keys::{KeyId, KeyPurpose, MaterialDigest};
use writ_broker::store::StoreError;
use writ_broker::writ_kernel::json::{self, Value};
use writ_broker::writ_kernel::time::Timestamp;
use writ_hostsys::deployment::{DeploymentRefused, VerifiedDeployment};
use writ_hostsys::sock::{peer_credentials, PlainReader};
use writ_keys::{DirRefusal, FileKeySource, KeyDirConfig};
use writ_ledger_sqlite::{
    OsActor, RevocationRow, RevocationScope, RevokeRefusal, SnapshotLimits, SqliteLedger,
    SqliteSnapshotStore,
};
use writ_rpc::admission::{AdmissionTable, Endpoint, PeerCred};
use writ_rpc::frame::{read_frame, write_control, Frame, FrameError, FrameLimits};
use writ_rpc::ConnectionCaps;

use crate::config::{HostBinding, ServiceConfig};

/// The host clock the service runs on.
pub type Clock = HostClock<SystemWall, Boottime>;

/// Why the service did not start.
#[derive(Debug)]
pub enum StartError {
    /// The deployment proof was configured and refused.
    Deployment(DeploymentRefused),
    /// The ledger could not be created, opened or owned.
    Ledger(StoreError),
    /// Trusted time could not start, or startup rollback is unacknowledged.
    Clock,
    /// The snapshot store could not be created, opened or owned.
    Snapshots(StoreError),
    /// The key directory refused.
    Keys(DirRefusal),
    /// The approval verifier, channel or listener refused.
    Approval(String),
    /// A socket path exists already or could not be bound.
    Socket(&'static str, io::ErrorKind),
}

impl std::fmt::Display for StartError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            StartError::Deployment(r) => write!(f, "deployment proof refused: {r:?}"),
            StartError::Ledger(e) => write!(f, "ledger: {e}"),
            StartError::Clock => f.write_str("trusted clock unavailable or blocked"),
            StartError::Snapshots(e) => write!(f, "snapshot store: {e}"),
            StartError::Keys(r) => write!(f, "key directory: {r}"),
            StartError::Approval(e) => write!(f, "approval listener: {e}"),
            StartError::Socket(which, k) => write!(f, "socket {which}: {k:?}"),
        }
    }
}

/// The composed single-owner service (module documentation).
pub struct Service {
    config: ServiceConfig,
    // Field order: stores and keys drop before the ledger (whose lock is released last).
    keys: FileKeySource,
    snapshots: Arc<SqliteSnapshotStore>,
    deployment: Option<Arc<VerifiedDeployment>>,
    clock: Arc<Clock>,
    ledger: Arc<SqliteLedger>,
}

impl Service {
    /// Operator initialization (before the first start, as the service user): create a new
    /// ledger and a new snapshot store bound to it. Both paths must not exist.
    pub fn initialize(config: &ServiceConfig) -> Result<(), StartError> {
        let ledger = SqliteLedger::create(&config.ledger).map_err(StartError::Ledger)?;
        SqliteSnapshotStore::create(
            &config.snapshots,
            ledger.ledger_id(),
            SnapshotLimits::REFERENCE,
        )
        .map_err(StartError::Snapshots)?;
        Ok(())
    }

    /// Open and own everything (module documentation for the order).
    pub fn open(config: ServiceConfig) -> Result<Service, StartError> {
        let deployment = match &config.deployment {
            None => None,
            Some(d) => Some(VerifiedDeployment::issue(d.clone()).map_err(StartError::Deployment)?),
        };
        let mut ledger = SqliteLedger::open(&config.ledger).map_err(StartError::Ledger)?;
        if let Some(a) = &config.approval {
            let origin = format!("http://localhost:{}", a.port);
            let verifier = Arc::new(
                PasskeyVerifier::new(RP_ID, &origin)
                    .map_err(|e| StartError::Approval(format!("verifier: {e:?}")))?,
            );
            let channel = ApprovalChannel::new(verifier.clone(), verifier, &a.channel)
                .ok_or_else(|| StartError::Approval("channel refused".into()))?;
            ledger = ledger
                .with_approval_channel(channel)
                .map_err(StartError::Ledger)?;
        }
        let clock = {
            let mut store: &SqliteLedger = &ledger;
            HostClock::start(SystemWall, Boottime, &mut store).map_err(|_| StartError::Clock)?
        };
        if clock.is_blocked() {
            return Err(StartError::Clock);
        }
        let mut snapshots = SqliteSnapshotStore::open(
            &config.snapshots,
            ledger.ledger_id(),
            SnapshotLimits::REFERENCE,
        )
        .map_err(StartError::Snapshots)?;
        if let Some(proof) = &deployment {
            snapshots = snapshots.with_deployment(proof.clone());
        }
        let keys = {
            let mut dir = KeyDirConfig::new(config.key_dir.clone(), config.service_uid);
            dir.margin_seconds = config.key_margin_seconds;
            let mut registry: &SqliteLedger = &ledger;
            FileKeySource::load(&dir, &mut registry).map_err(StartError::Keys)?
        };
        Ok(Service {
            config,
            keys,
            snapshots: Arc::new(snapshots),
            deployment,
            clock: Arc::new(clock),
            ledger: Arc::new(ledger),
        })
    }

    /// The configuration.
    pub fn config(&self) -> &ServiceConfig {
        &self.config
    }

    /// The owned ledger.
    pub fn ledger(&self) -> &Arc<SqliteLedger> {
        &self.ledger
    }

    /// The snapshot store (the single instance for broker and runner, J-4).
    pub fn snapshots(&self) -> &Arc<SqliteSnapshotStore> {
        &self.snapshots
    }

    /// The loaded key source.
    pub fn keys(&self) -> &FileKeySource {
        &self.keys
    }

    /// `true` while the deployment proof exists and holds (rechecked here).
    pub fn deployment_verified(&self) -> bool {
        self.deployment.as_ref().is_some_and(|d| d.recheck())
    }

    /// Trusted time now (raises the ledger floor; D-021).
    pub fn now(&self) -> Option<Timestamp> {
        let mut store: &SqliteLedger = &self.ledger;
        observe(self.clock.as_ref(), &mut store)
            .ok()
            .map(|o| o.at())
    }

    /// The `readiness` answer: label, deployment, loaded key ids and diagnostics, never
    /// material.
    pub fn readiness(&self) -> Value {
        let verified = self.deployment_verified();
        let withdrawn = self
            .deployment
            .as_ref()
            .and_then(|d| d.withdrawn_because())
            .map(|r| json::s(&format!("{r:?}")))
            .unwrap_or(Value::Null);
        let keys = self
            .keys
            .loaded()
            .map(|(id, v)| {
                json::obj([
                    ("key_id", json::s(id.as_str())),
                    ("not_before", json::s(&v.not_before().to_string())),
                    ("not_after", json::s(&v.not_after().to_string())),
                ])
            })
            .collect();
        let diagnostics = self
            .keys
            .diagnostics()
            .iter()
            .map(|d| {
                json::obj([
                    ("entry", json::s(&d.entry)),
                    ("refused", json::s(&format!("{:?}", d.refused))),
                ])
            })
            .collect();
        json::obj([
            ("ok", Value::Bool(true)),
            ("label", json::s(self.config.label(verified))),
            ("deployment_verified", Value::Bool(verified)),
            ("deployment_withdrawn", withdrawn),
            (
                "snapshot_store_immutable",
                Value::Bool(self.snapshots_immutable()),
            ),
            ("ledger_id", json::s(self.ledger.ledger_id().as_str())),
            ("keys", Value::Arr(keys)),
            ("key_diagnostics", Value::Arr(diagnostics)),
        ])
    }

    fn snapshots_immutable(&self) -> bool {
        use writ_broker::runner::SnapshotContent;
        self.snapshots.immutable()
    }

    /// Bind both sockets (umask 0077 applies; the paths must not exist) and serve them, and
    /// the approval listener when configured, until [`Running::shutdown`].
    pub fn serve(self) -> Result<Running, StartError> {
        let service = Arc::new(self);
        let config = &service.config;
        let host_table = AdmissionTable::new(
            Endpoint::Host,
            config
                .host_bindings
                .iter()
                .map(|b| (b.uid, Some(b.clone())))
                .collect(),
        )
        .map_err(|_| StartError::Socket("host.sock", io::ErrorKind::InvalidInput))?;
        let admin_table = AdmissionTable::new(
            Endpoint::Admin,
            config
                .admin_uids
                .iter()
                .map(|u| (*u, None::<HostBinding>))
                .collect(),
        )
        .map_err(|_| StartError::Socket("admin.sock", io::ErrorKind::InvalidInput))?;
        let host = bind(&config.host_socket, "host.sock")?;
        let admin = match bind(&config.admin_socket, "admin.sock") {
            Ok(a) => a,
            Err(e) => {
                let _ = std::fs::remove_file(&config.host_socket);
                return Err(e);
            }
        };
        let caps = ConnectionCaps::new(
            config.limits.max_connections,
            config.limits.max_connections_per_uid,
        )
        .ok_or(StartError::Socket("caps", io::ErrorKind::InvalidInput))?;
        let stop = Arc::new(AtomicBool::new(false));
        let connections: Arc<Mutex<Vec<JoinHandle<()>>>> = Arc::new(Mutex::new(Vec::new()));
        let mut threads = Vec::new();
        if let Some(a) = &config.approval {
            let backend = Arc::new(LedgerBackend::new(
                Arc::clone(&service.ledger),
                Arc::clone(&service.clock),
            ));
            let listener = ApprovalListener::bind(
                ListenerConfig {
                    port: a.port,
                    subject_issuer: a.subject_issuer.clone(),
                    limits: PageLimits::default(),
                },
                backend,
            )
            .map_err(|e| StartError::Approval(format!("bind: {:?}", e.kind())))?;
            if listener.origin() != format!("http://localhost:{}", a.port) {
                return Err(StartError::Approval(
                    "origin differs from the configuration".into(),
                ));
            }
            let stop = Arc::clone(&stop);
            threads.push(thread::spawn(move || {
                let _ = listener.serve(stop);
            }));
        }
        for (listener, table, endpoint) in [
            (host, host_table, Endpoint::Host),
            (admin, admin_table, Endpoint::Admin),
        ] {
            let ctx = Acceptor {
                service: Arc::clone(&service),
                table: Arc::new(table),
                endpoint,
                caps: caps.clone(),
                stop: Arc::clone(&stop),
                connections: Arc::clone(&connections),
            };
            threads.push(thread::spawn(move || ctx.run(listener)));
        }
        Ok(Running {
            service,
            stop,
            threads,
            connections,
        })
    }
}

fn bind(path: &Path, which: &'static str) -> Result<UnixListener, StartError> {
    let listener = UnixListener::bind(path).map_err(|e| StartError::Socket(which, e.kind()))?;
    listener
        .set_nonblocking(true)
        .map_err(|e| StartError::Socket(which, e.kind()))?;
    Ok(listener)
}

/// A serving service. Dropping it without [`Running::shutdown`] leaves the threads running.
pub struct Running {
    service: Arc<Service>,
    stop: Arc<AtomicBool>,
    threads: Vec<JoinHandle<()>>,
    connections: Arc<Mutex<Vec<JoinHandle<()>>>>,
}

impl Running {
    /// The served service.
    pub fn service(&self) -> &Arc<Service> {
        &self.service
    }

    /// Stop accepting, wait for the open connections (bounded by their idle deadline), remove
    /// the socket files and release the stores and the ledger (this is not the J-8 signal
    /// path, which is M5a-11). Returns the service if no other reference remains.
    pub fn shutdown(self) -> Option<Service> {
        self.stop.store(true, Ordering::SeqCst);
        for t in self.threads {
            let _ = t.join();
        }
        let open: Vec<JoinHandle<()>> = std::mem::take(
            &mut *self
                .connections
                .lock()
                .unwrap_or_else(PoisonError::into_inner),
        );
        for t in open {
            let _ = t.join();
        }
        let _ = std::fs::remove_file(&self.service.config.host_socket);
        let _ = std::fs::remove_file(&self.service.config.admin_socket);
        Arc::into_inner(self.service)
    }
}

struct Acceptor {
    service: Arc<Service>,
    table: Arc<AdmissionTable<Option<HostBinding>>>,
    endpoint: Endpoint,
    caps: ConnectionCaps,
    stop: Arc<AtomicBool>,
    connections: Arc<Mutex<Vec<JoinHandle<()>>>>,
}

impl Acceptor {
    fn run(self, listener: UnixListener) {
        while !self.stop.load(Ordering::SeqCst) {
            let stream = match listener.accept() {
                Ok((s, _)) => s,
                Err(e) if e.kind() == io::ErrorKind::WouldBlock => {
                    thread::sleep(Duration::from_millis(20));
                    continue;
                }
                Err(_) => {
                    thread::sleep(Duration::from_millis(20));
                    continue;
                }
            };
            // Admission from peer credentials before any read (J-2).
            let Ok(peer) = peer_credentials(&stream) else {
                continue;
            };
            let peer = PeerCred {
                uid: peer.uid,
                gid: peer.gid,
                pid: peer.pid,
            };
            let binding = match self.table.admit(&peer) {
                Ok(b) => b.clone(),
                Err(why) => {
                    eprintln!(
                        "writ-serviced: {:?} refused uid {} pid {}: {why:?}",
                        self.endpoint, peer.uid, peer.pid
                    );
                    continue;
                }
            };
            let Some(slot) = self.caps.try_acquire(peer.uid) else {
                eprintln!(
                    "writ-serviced: {:?} over the connection cap for uid {}",
                    self.endpoint, peer.uid
                );
                continue;
            };
            let service = Arc::clone(&self.service);
            let endpoint = self.endpoint;
            let handle = thread::spawn(move || {
                let _slot = slot;
                serve_connection(&service, stream, endpoint, peer, binding);
            });
            let mut open = self
                .connections
                .lock()
                .unwrap_or_else(PoisonError::into_inner);
            open.retain(|h| !h.is_finished());
            open.push(handle);
        }
    }
}

/// What a handled request does to its connection.
enum Next {
    Continue(Option<Value>),
    Close(Value),
}

fn refused(code: &str) -> Value {
    json::obj([("ok", Value::Bool(false)), ("error", json::s(code))])
}

fn generic() -> Value {
    refused(writ_rpc::GENERIC_ERROR)
}

fn op_members<'a>(v: &'a Value, allowed: &[&str]) -> Option<&'a json::Map> {
    let m = v.as_obj()?;
    m.keys()
        .all(|k| k == "op" || allowed.contains(&k.as_str()))
        .then_some(m)
}

struct Ingest {
    session: writ_ledger_sqlite::IngestSession,
}

struct Conn<'a> {
    service: &'a Service,
    peer: PeerCred,
    binding: Option<HostBinding>,
    ingest: Option<Ingest>,
}

fn serve_connection(
    service: &Service,
    stream: UnixStream,
    endpoint: Endpoint,
    peer: PeerCred,
    binding: Option<HostBinding>,
) {
    let limits = service.config.limits;
    if stream.set_nonblocking(false).is_err()
        || stream
            .set_read_timeout(Some(Duration::from_secs(limits.idle_seconds)))
            .is_err()
        || stream
            .set_write_timeout(Some(Duration::from_secs(limits.request_seconds)))
            .is_err()
    {
        return;
    }
    let frame_limits = match endpoint {
        Endpoint::Host => FrameLimits::host(),
        Endpoint::Admin => FrameLimits::admin(),
    };
    let mut conn = Conn {
        service,
        peer,
        binding,
        ingest: None,
    };
    let mut writer = &stream;
    loop {
        let next = match read_frame(&mut PlainReader(&stream), &frame_limits) {
            Err(FrameError::Closed) => return,
            Err(_) => Next::Close(generic()),
            Ok(Frame::Data(bytes)) => conn.data(&bytes),
            Ok(Frame::Control(v)) => match endpoint {
                Endpoint::Host => conn.host(&v),
                Endpoint::Admin => conn.admin(&v),
            },
        };
        match next {
            Next::Continue(None) => {}
            Next::Continue(Some(reply)) => {
                if write_control(&mut writer, &reply).is_err() {
                    return;
                }
            }
            Next::Close(reply) => {
                let _ = write_control(&mut writer, &reply);
                let _ = writer.flush();
                return;
            }
        }
    }
}

impl Conn<'_> {
    fn data(&mut self, bytes: &[u8]) -> Next {
        let Some(ingest) = self.ingest.as_mut() else {
            return Next::Close(generic());
        };
        match ingest.session.data(bytes) {
            Ok(()) => Next::Continue(None),
            Err(e) => {
                self.ingest = None;
                Next::Close(refused(e.external_code()))
            }
        }
    }

    fn host(&mut self, v: &Value) -> Next {
        let Some(binding) = self.binding.clone() else {
            return Next::Close(generic());
        };
        match v.as_obj().and_then(|m| m.get("op")).and_then(Value::as_str) {
            Some("snapshot.ingest.begin") => {
                if op_members(v, &[]).is_none() || self.ingest.is_some() {
                    return Next::Close(generic());
                }
                match self
                    .service
                    .snapshots
                    .begin(&binding.tenant_id, &binding.project_id)
                {
                    Ok(session) => {
                        self.ingest = Some(Ingest { session });
                        Next::Continue(Some(json::obj([("ok", Value::Bool(true))])))
                    }
                    Err(e) => Next::Continue(Some(refused(e.external_code()))),
                }
            }
            Some("snapshot.ingest.file") => {
                let Some(m) = op_members(v, &["path", "size"]) else {
                    return Next::Close(generic());
                };
                let (Some(path), Some(Value::Int(size))) =
                    (m.get("path").and_then(Value::as_str), m.get("size"))
                else {
                    return Next::Close(generic());
                };
                let (Some(ingest), Ok(size)) = (self.ingest.as_mut(), u64::try_from(*size)) else {
                    return Next::Close(generic());
                };
                match ingest.session.file(path, size) {
                    Ok(()) => Next::Continue(Some(json::obj([("ok", Value::Bool(true))]))),
                    Err(e) => {
                        self.ingest = None;
                        Next::Continue(Some(refused(e.external_code())))
                    }
                }
            }
            Some("snapshot.ingest.commit") => {
                let Some(m) = op_members(v, &["claimed_manifest_digest"]) else {
                    return Next::Close(generic());
                };
                let claimed = match m.get("claimed_manifest_digest") {
                    None => None,
                    Some(Value::Str(s)) => Some(s.as_str()),
                    Some(_) => return Next::Close(generic()),
                };
                let Some(ingest) = self.ingest.take() else {
                    return Next::Close(generic());
                };
                match ingest.session.commit(claimed) {
                    Ok(c) => Next::Continue(Some(json::obj([
                        ("ok", Value::Bool(true)),
                        ("snapshot_id", json::s(&c.snapshot_id)),
                        ("manifest_digest", json::s(&c.manifest_digest)),
                    ]))),
                    Err(e) => Next::Continue(Some(refused(e.external_code()))),
                }
            }
            Some("status.receipt") => {
                let Some(m) = op_members(v, &["idempotency_key"]) else {
                    return Next::Close(generic());
                };
                let Some(client_key) = m
                    .get("idempotency_key")
                    .and_then(Value::as_str)
                    .filter(|k| !k.is_empty() && k.len() <= 200)
                else {
                    return Next::Close(generic());
                };
                let Some(key) = seal::scoped_idempotency_key(
                    &writ_rpc::os_principal(self.peer.uid),
                    &binding.tenant_id,
                    &binding.project_id,
                    client_key,
                ) else {
                    return Next::Close(generic());
                };
                match self.service.ledger.receipt_status(&binding.tenant_id, &key) {
                    Ok(status) => Next::Continue(Some(json::obj([
                        ("ok", Value::Bool(true)),
                        ("status", status.map_or(Value::Null, |s| json::s(&s))),
                    ]))),
                    Err(_) => Next::Continue(Some(refused("LEDGER_UNAVAILABLE"))),
                }
            }
            _ => Next::Close(generic()),
        }
    }

    fn admin(&mut self, v: &Value) -> Next {
        match v.as_obj().and_then(|m| m.get("op")).and_then(Value::as_str) {
            Some("readiness") if op_members(v, &[]).is_some() => {
                Next::Continue(Some(self.service.readiness()))
            }
            Some("keys.revoke") => {
                let Some(m) = op_members(v, &["scope", "reason"]) else {
                    return Next::Close(generic());
                };
                let (Some(scope), Some(reason)) = (
                    m.get("scope").and_then(parse_scope),
                    m.get("reason").and_then(Value::as_str),
                ) else {
                    return Next::Continue(Some(refused("KEYS_REVOKE_INVALID")));
                };
                let Some(at) = self.service.now() else {
                    return Next::Continue(Some(refused("TIME_UNAVAILABLE")));
                };
                let actor = OsActor {
                    uid: self.peer.uid,
                    pid: self.peer.pid,
                };
                match self.service.ledger.revoke_key(&scope, reason, &actor, at) {
                    Ok(Ok(row)) => {
                        eprintln!(
                            "writ-serviced: key revocation {} by uid {} reason {:?}",
                            row.revocation_id, actor.uid, row.reason
                        );
                        Next::Continue(Some(revocation_reply(&row)))
                    }
                    Ok(Err(r)) => Next::Continue(Some(refused(match r {
                        RevokeRefusal::Unregistered => "KEY_UNREGISTERED",
                        RevokeRefusal::PurposeMismatch => "KEY_PURPOSE_MISMATCH",
                        RevokeRefusal::Reason => "KEYS_REVOKE_INVALID",
                    }))),
                    Err(_) => Next::Continue(Some(refused("LEDGER_UNAVAILABLE"))),
                }
            }
            _ => Next::Close(generic()),
        }
    }
}

/// Parse a J-11.8 scope: `{"kind":"key_id","purpose","key_id"}` or
/// `{"kind":"material","purpose","material_digest"}`, exactly those members.
pub fn parse_scope(v: &Value) -> Option<RevocationScope> {
    let m = v.as_obj()?;
    let text = |k: &str| m.get(k).and_then(Value::as_str);
    let purpose = KeyPurpose::parse(text("purpose")?)?;
    match text("kind")? {
        "key_id" if m.len() == 3 => {
            let key_id = KeyId::parse(text("key_id")?)?;
            (key_id.purpose() == purpose).then_some(RevocationScope::KeyId { purpose, key_id })
        }
        "material" if m.len() == 3 => Some(RevocationScope::Material {
            purpose,
            digest: MaterialDigest::parse(text("material_digest")?)?,
        }),
        _ => None,
    }
}

fn revocation_reply(row: &RevocationRow) -> Value {
    json::obj([
        ("ok", Value::Bool(true)),
        (
            "revocation",
            json::obj([
                ("revocation_id", Value::Int(row.revocation_id)),
                ("scope", json::s(&row.scope)),
                ("key_id", row.key_id.as_deref().map_or(Value::Null, json::s)),
                ("material_digest", json::s(&row.material_digest)),
                ("purpose", json::s(&row.purpose)),
                ("revoked_at", json::s(&row.revoked_at)),
                ("ledger_seq", Value::Int(row.ledger_seq)),
                ("created", Value::Bool(row.created)),
            ]),
        ),
    ])
}
