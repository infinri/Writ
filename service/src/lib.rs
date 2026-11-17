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
//! **Sockets (J-8; M5a-11).** With `sockets.activation = "systemd"` every socket (`host`,
//! `admin` and, when configured, `approval`) is received from systemd and taken over through
//! the service's own pidfd ([`writ_hostsys::sock::take_inherited`]); each is verified (family,
//! type, listening, exact address, and for the Unix sockets owner and mode: `host.sock` 0660,
//! `admin.sock` 0600 in group `writ`) and the service refuses to start unless it receives
//! exactly those. It never binds in that mode. Otherwise (development) it binds them itself.
//!
//! **Graceful shutdown (J-8; [`Running::stop_gracefully`], [`daemon`]).** Stop accepting work
//! (listening sockets stay open; admitted connections and new requests on open connections
//! are answered `SERVICE_STOPPING`, the page `503`), drain, terminate owned jobs through the
//! launcher (the [`Running::job_termination`] token), join every task holding a store handle,
//! then drop the stores and the ledger last. A second signal or the hard deadline exits at
//! once ([`shutdown`]).
//!
//! **Start-time sweep ([`sweep`]).** Leftover job state through the launcher contract.
//!
//! **Not in this crate yet (recorded):** the runner supervisor composed over the launcher
//! client (M5a-07 client exists; composition with the execute path is M5b-03) and the broker
//! execute path over an effect adapter (M5a-09, M5b-03).

pub mod config;
pub mod daemon;
pub mod seal;
pub mod shutdown;
pub mod sweep;

use std::io::{self, Write};
use std::net::{Shutdown, TcpListener};
use std::os::unix::fs::{FileTypeExt, MetadataExt};
use std::os::unix::net::{UnixListener, UnixStream};
use std::path::Path;
use std::sync::atomic::{AtomicBool, AtomicUsize, Ordering};
use std::sync::{Arc, Mutex, PoisonError};
use std::thread::{self, JoinHandle};
use std::time::{Duration, Instant};

use writ_approval::backend::LedgerBackend;
use writ_approval::listener::{
    ApprovalListener, Limits as PageLimits, ListenerConfig, ServeControl, RP_ID,
};
use writ_approval::verifier::PasskeyVerifier;
use writ_broker::clock::{observe, Boottime, HostClock, SystemWall};
use writ_broker::human::ApprovalChannel;
use writ_broker::keys::{KeyId, KeyPurpose, MaterialDigest};
use writ_broker::store::StoreError;
use writ_broker::writ_kernel::json::{self, Value};
use writ_broker::writ_kernel::time::Timestamp;
use writ_hostsys::deployment::{DeploymentRefused, VerifiedDeployment};
use writ_hostsys::sock::{
    check_listening, peer_credentials, take_inherited, ActivationRefused, ListenEnv, ListenKind,
    PlainReader,
};
use writ_keys::{DirRefusal, FileKeySource, KeyDirConfig};
use writ_ledger_sqlite::{
    OsActor, RevocationRow, RevocationScope, RevokeRefusal, SnapshotLimits, SqliteLedger,
    SqliteSnapshotStore,
};
use writ_rpc::admission::{AdmissionTable, Endpoint, PeerCred};
use writ_rpc::frame::{read_frame_timed, write_control, Frame, FrameError, FrameLimits, TimedRead};
use writ_rpc::ConnectionCaps;

use crate::config::{HostBinding, ServiceConfig};
use crate::sweep::SweepReport;

/// The generic refusal answered while stopping (J-8; external mapping: the request was not
/// started and may be retried after the restart).
pub const SERVICE_STOPPING: &str = "SERVICE_STOPPING";

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
    /// Socket activation was configured and the inherited sockets were refused (J-8).
    Activation(String),
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
            StartError::Activation(why) => write!(f, "socket activation: {why}"),
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
    sweep: Option<SweepReport>,
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
            sweep: None,
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

    /// Run the start-time sweep when configured ([`sweep`]) and keep its report for
    /// `readiness`; `None` when not configured.
    pub fn sweep(&mut self) -> Option<&SweepReport> {
        let cfg = self.config.sweep.clone()?;
        self.sweep = Some(sweep::sweep(&cfg, self.config.service_uid));
        self.sweep.as_ref()
    }

    /// `true` while the deployment proof exists, holds (rechecked here) and is current.
    pub fn deployment_verified(&self) -> bool {
        self.deployment
            .as_ref()
            .is_some_and(|d| d.recheck() && d.is_current())
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
            (
                "sweep",
                self.sweep
                    .as_ref()
                    .map_or(Value::Null, SweepReport::to_value),
            ),
            ("keys", Value::Arr(keys)),
            ("key_diagnostics", Value::Arr(diagnostics)),
        ])
    }

    fn snapshots_immutable(&self) -> bool {
        use writ_broker::runner::SnapshotContent;
        self.snapshots.immutable()
    }

    /// Bind both sockets (umask 0077 applies; the paths must not exist) and serve them, and
    /// the approval listener when configured, until [`Running::shutdown`] or
    /// [`Running::stop_gracefully`]. Refused when socket activation is configured.
    pub fn serve(self) -> Result<Running, StartError> {
        self.serve_with(Sockets::Bind)
    }

    /// Serve `sockets` (module documentation, "Sockets").
    pub fn serve_with(self, sockets: Sockets) -> Result<Running, StartError> {
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
        let caps = ConnectionCaps::new(
            config.limits.max_connections,
            config.limits.max_connections_per_uid,
        )
        .ok_or(StartError::Socket("caps", io::ErrorKind::InvalidInput))?;
        let (host, admin, approval_socket, bound_self) = match sockets {
            Sockets::Bind if config.socket_activation => {
                return Err(StartError::Activation(
                    "configured for systemd sockets; the service never binds them".into(),
                ))
            }
            Sockets::Bind => {
                let host = bind(&config.host_socket, "host.sock")?;
                let admin = match bind(&config.admin_socket, "admin.sock") {
                    Ok(a) => a,
                    Err(e) => {
                        let _ = std::fs::remove_file(&config.host_socket);
                        return Err(e);
                    }
                };
                (host, admin, None, true)
            }
            Sockets::Inherited(i) => {
                let Inherited {
                    host,
                    admin,
                    approval,
                } = i;
                host.set_nonblocking(true)
                    .and_then(|()| admin.set_nonblocking(true))
                    .map_err(|e| StartError::Socket("inherited", e.kind()))?;
                (host, admin, approval, false)
            }
        };
        let stop = Arc::new(AtomicBool::new(false));
        let lifecycle = Arc::new(Lifecycle::default());
        let connections: Arc<Mutex<Vec<Open>>> = Arc::new(Mutex::new(Vec::new()));
        let mut threads = Vec::new();
        let mut approval = None;
        if let Some(a) = &config.approval {
            let backend = Arc::new(LedgerBackend::new(
                Arc::clone(&service.ledger),
                Arc::clone(&service.clock),
            ));
            let page = ListenerConfig {
                port: a.port,
                subject_issuer: a.subject_issuer.clone(),
                limits: PageLimits::default(),
            };
            let listener = match approval_socket {
                Some(l) => ApprovalListener::from_std(l, page, backend),
                None => ApprovalListener::bind(page, backend),
            }
            .map_err(|e| StartError::Approval(format!("bind: {:?}", e.kind())))?;
            if listener.origin() != format!("http://localhost:{}", a.port) {
                return Err(StartError::Approval(
                    "origin differs from the configuration".into(),
                ));
            }
            let control = listener.control();
            let stop = Arc::clone(&stop);
            approval = Some((
                control,
                thread::spawn(move || {
                    let _ = listener.serve(stop);
                }),
            ));
        } else if approval_socket.is_some() {
            return Err(StartError::Activation(
                "an approval socket was received but no approval listener is configured".into(),
            ));
        }
        if let Some(proof) = &service.deployment {
            threads.push(recheck_timer(Arc::clone(proof), Arc::clone(&stop)));
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
                lifecycle: Arc::clone(&lifecycle),
                connections: Arc::clone(&connections),
            };
            threads.push(thread::spawn(move || ctx.run(listener)));
        }
        Ok(Running {
            service,
            stop,
            lifecycle,
            jobs: Arc::new(AtomicBool::new(false)),
            approval,
            threads,
            connections,
            bound_self,
        })
    }
}

/// The periodic deployment recheck (J-9; M5a-13 M5): every configured interval the proof is
/// rechecked; a difference withdraws it for good. The proof is current only while its last
/// passed check is no older than the interval ([`VerifiedDeployment::is_current`]), so the
/// timer runs at half the interval and a stalled timer fails closed. The thread holds the
/// proof only, never a store handle, and ends at the stop flag.
fn recheck_timer(proof: Arc<VerifiedDeployment>, stop: Arc<AtomicBool>) -> JoinHandle<()> {
    thread::spawn(move || {
        let period = (proof.recheck_interval() / 2).max(Duration::from_millis(10));
        let mut next = Instant::now() + period;
        while !stop.load(Ordering::SeqCst) {
            if Instant::now() >= next {
                if !proof.recheck() {
                    eprintln!(
                        "writ-serviced: deployment proof withdrawn: {:?}",
                        proof.withdrawn_because()
                    );
                    return;
                }
                next = Instant::now() + period;
            }
            thread::sleep(Duration::from_millis(50).min(period));
        }
    })
}

/// Where the service's sockets come from (J-8).
pub enum Sockets {
    /// The service binds them (development; refused with socket activation configured).
    Bind,
    /// Received from systemd and verified ([`Inherited::take`]).
    Inherited(Inherited),
}

/// The verified sockets received from systemd (J-8).
pub struct Inherited {
    host: UnixListener,
    admin: UnixListener,
    approval: Option<TcpListener>,
}

/// The `FileDescriptorName=` of each socket unit.
pub const SOCKET_NAMES: [&str; 3] = ["host", "admin", "approval"];

impl Inherited {
    /// Take over and verify the sockets systemd passed (module documentation): exactly
    /// `host`, `admin` and, iff the approval listener is configured, `approval`.
    pub fn take(config: &ServiceConfig, env: &ListenEnv) -> Result<Inherited, StartError> {
        let refused = |why: String| StartError::Activation(why);
        let got = take_inherited(env).map_err(|r| match r {
            ActivationRefused::NotActivated => refused("no sockets were passed".into()),
            other => refused(format!("{other:?}")),
        })?;
        let mut want: Vec<&str> = vec!["host", "admin"];
        if config.approval.is_some() {
            want.push("approval");
        }
        let mut names: Vec<&str> = got.iter().map(|(n, _)| n.as_str()).collect();
        names.sort_unstable();
        want.sort_unstable();
        if names != want {
            return Err(refused(format!(
                "received sockets {names:?}, configured {want:?}"
            )));
        }
        let mut host = None;
        let mut admin = None;
        let mut approval = None;
        for (name, fd) in got {
            match name.as_str() {
                "approval" => {
                    check_listening(&fd, ListenKind::TcpV4)
                        .map_err(|e| refused(format!("approval: {:?}", e.kind())))?;
                    approval = Some(TcpListener::from(fd));
                }
                "host" | "admin" => {
                    check_listening(&fd, ListenKind::UnixStream)
                        .map_err(|e| refused(format!("{name}: {:?}", e.kind())))?;
                    let l = UnixListener::from(fd);
                    // M5a-13 L10: the received host.sock's group is verified too (J-1:
                    // `writ:writ-agents` 0660; `sockets.host_gid` is required with activation).
                    let (path, mode, gid) = if name == "host" {
                        (&config.host_socket, 0o660, config.host_socket_gid)
                    } else {
                        (&config.admin_socket, 0o600, Some(config.service_gid))
                    };
                    verify_unix(&l, path, mode, config.service_uid, gid)
                        .map_err(|why| refused(format!("{name}: {why}")))?;
                    if name == "host" {
                        host = Some(l);
                    } else {
                        admin = Some(l);
                    }
                }
                _ => return Err(refused(format!("unexpected socket {name}"))),
            }
        }
        match (host, admin) {
            (Some(host), Some(admin)) => Ok(Inherited {
                host,
                admin,
                approval,
            }),
            _ => Err(refused("host or admin socket missing".into())),
        }
    }
}

/// The received Unix socket is bound at exactly `path`, a socket owned by `uid` (and `gid`
/// when given) with permission bits exactly `mode`.
fn verify_unix(
    l: &UnixListener,
    path: &Path,
    mode: u32,
    uid: u32,
    gid: Option<u32>,
) -> Result<(), String> {
    let addr = l.local_addr().map_err(|e| format!("{:?}", e.kind()))?;
    if addr.as_pathname() != Some(path) {
        return Err("bound to another address".into());
    }
    let m = std::fs::symlink_metadata(path).map_err(|e| format!("{:?}", e.kind()))?;
    if !m.file_type().is_socket() {
        return Err("path is not a socket".into());
    }
    if m.mode() & 0o7777 != mode || m.uid() != uid || gid.is_some_and(|g| m.gid() != g) {
        return Err(format!(
            "owner {}:{} mode {:o}, required uid {uid} gid {gid:?} mode {mode:o}",
            m.uid(),
            m.gid(),
            m.mode() & 0o7777
        ));
    }
    Ok(())
}

fn bind(path: &Path, which: &'static str) -> Result<UnixListener, StartError> {
    let listener = UnixListener::bind(path).map_err(|e| StartError::Socket(which, e.kind()))?;
    listener
        .set_nonblocking(true)
        .map_err(|e| StartError::Socket(which, e.kind()))?;
    Ok(listener)
}

/// Requests in progress and the `stopping` state of the Unix endpoints (J-8). A request
/// counts itself in flight **before** it checks `stopping` (both sequentially consistent),
/// so a drain that observes zero after setting `stopping` cannot miss a request that will
/// still run.
#[derive(Debug, Default)]
pub struct Lifecycle {
    stopping: AtomicBool,
    in_flight: AtomicUsize,
}

impl Lifecycle {
    /// Answer every new connection and request `SERVICE_STOPPING` from now on.
    pub fn stop(&self) {
        self.stopping.store(true, Ordering::SeqCst);
    }

    /// Whether [`Lifecycle::stop`] was called.
    pub fn is_stopping(&self) -> bool {
        self.stopping.load(Ordering::SeqCst)
    }

    /// Requests in progress now.
    pub fn in_flight(&self) -> usize {
        self.in_flight.load(Ordering::SeqCst)
    }

    /// Count one request in flight, or `None` while stopping. Public so that a test or a
    /// later component (the runner supervisor) can hold a request open.
    pub fn enter(self: &Arc<Self>) -> Option<InFlight> {
        self.in_flight.fetch_add(1, Ordering::SeqCst);
        let guard = InFlight(Arc::clone(self));
        (!self.is_stopping()).then_some(guard)
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
            thread::sleep(Duration::from_millis(5));
        }
    }
}

/// One counted request in flight ([`Lifecycle::enter`]).
pub struct InFlight(Arc<Lifecycle>);

impl Drop for InFlight {
    fn drop(&mut self) {
        self.0.in_flight.fetch_sub(1, Ordering::SeqCst);
    }
}

/// One open connection: its thread and a handle to shut its socket down at release.
struct Open {
    thread: JoinHandle<()>,
    stream: Option<UnixStream>,
}

/// A serving service. Dropping it without [`Running::shutdown`] or
/// [`Running::stop_gracefully`] leaves the threads running.
pub struct Running {
    service: Arc<Service>,
    stop: Arc<AtomicBool>,
    lifecycle: Arc<Lifecycle>,
    jobs: Arc<AtomicBool>,
    approval: Option<(Arc<ServeControl>, JoinHandle<()>)>,
    threads: Vec<JoinHandle<()>>,
    connections: Arc<Mutex<Vec<Open>>>,
    bound_self: bool,
}

/// What [`Running::stop_gracefully`] did (J-8).
pub struct Stopped {
    /// Every request in progress finished within the drain period.
    pub drained: bool,
    /// The job-termination token was set (step 4); requests that held jobs then ended
    /// before the hard deadline.
    pub jobs_ended: bool,
    /// The service, when every task holding a store handle was joined before the hard
    /// deadline; dropping it drops the stores, then the ledger (its lock last). `None`: a
    /// handle is still held; the caller exits non-zero and the operating system releases
    /// the locks (uncertain outcomes stay uncertain).
    pub service: Option<Service>,
}

impl Running {
    /// The served service.
    pub fn service(&self) -> &Arc<Service> {
        &self.service
    }

    /// The request lifecycle of the Unix endpoints.
    pub fn lifecycle(&self) -> &Arc<Lifecycle> {
        &self.lifecycle
    }

    /// The job-termination token (J-8 step 4): set after the drain period; a runner job in
    /// progress is then cancelled through the launcher (`writ-containment`
    /// `LauncherSandbox::with_termination`), killed and verified empty by the launcher, and
    /// finished `failed` with `terminated_shutdown`, or left `started` when that cannot be
    /// recorded.
    pub fn job_termination(&self) -> &Arc<AtomicBool> {
        &self.jobs
    }

    /// Stop at once: no drain period (tests and the development path). Returns the service
    /// if no other reference remains.
    pub fn shutdown(self) -> Option<Service> {
        let hard = Instant::now() + Duration::from_secs(60);
        self.stop_gracefully(Duration::ZERO, hard).service
    }

    /// The J-8 sequence (module documentation): stop accepting work, drain for `drain`,
    /// terminate owned jobs, release ownership; every wait bounded by `hard`.
    pub fn stop_gracefully(self, drain: Duration, hard: Instant) -> Stopped {
        // 1. Stop accepting work; the listening sockets stay open.
        self.lifecycle.stop();
        if let Some((control, _)) = &self.approval {
            control.stop_accepting_work();
        }
        // 2. Drain.
        let drain_end = (Instant::now() + drain).min(hard);
        let unix_idle = self.lifecycle.wait_idle(drain_end);
        let page_idle = self
            .approval
            .as_ref()
            .is_none_or(|(c, _)| c.wait_idle(drain_end));
        // 4. Terminate owned jobs through the launcher; their requests then end.
        self.jobs.store(true, Ordering::SeqCst);
        let jobs_ended = self.lifecycle.wait_idle(hard);
        // 5. Release: end the accept loops (closing the listening sockets), shut down every
        //    open connection, join every task holding a store handle.
        self.stop.store(true, Ordering::SeqCst);
        if let Some((control, _)) = &self.approval {
            control.exit();
        }
        for t in self.threads {
            let _ = t.join();
        }
        if let Some((_, t)) = self.approval {
            let _ = t.join();
        }
        let open: Vec<Open> = std::mem::take(
            &mut *self
                .connections
                .lock()
                .unwrap_or_else(PoisonError::into_inner),
        );
        for o in &open {
            if let Some(s) = &o.stream {
                let _ = s.shutdown(Shutdown::Both);
            }
        }
        for o in open {
            let _ = o.thread.join();
        }
        if self.bound_self {
            let _ = std::fs::remove_file(&self.service.config.host_socket);
            let _ = std::fs::remove_file(&self.service.config.admin_socket);
        }
        let mut service = self.service;
        let service = loop {
            match Arc::try_unwrap(service) {
                Ok(s) => break Some(s),
                Err(still) if Instant::now() < hard => {
                    service = still;
                    thread::sleep(Duration::from_millis(10));
                }
                Err(_) => break None,
            }
        };
        Stopped {
            drained: unix_idle && page_idle,
            jobs_ended,
            service,
        }
    }
}

struct Acceptor {
    service: Arc<Service>,
    table: Arc<AdmissionTable<Option<HostBinding>>>,
    endpoint: Endpoint,
    caps: ConnectionCaps,
    stop: Arc<AtomicBool>,
    lifecycle: Arc<Lifecycle>,
    connections: Arc<Mutex<Vec<Open>>>,
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
            if self.lifecycle.is_stopping() {
                // J-8: answered without reading, then closed.
                let _ = stream
                    .set_nonblocking(false)
                    .and_then(|()| stream.set_write_timeout(Some(Duration::from_secs(1))));
                let mut w = &stream;
                let _ = write_control(&mut w, &refused(SERVICE_STOPPING));
                continue;
            }
            let Some(slot) = self.caps.try_acquire(peer.uid) else {
                eprintln!(
                    "writ-serviced: {:?} over the connection cap for uid {}",
                    self.endpoint, peer.uid
                );
                continue;
            };
            let service = Arc::clone(&self.service);
            let lifecycle = Arc::clone(&self.lifecycle);
            let endpoint = self.endpoint;
            // One clone lets the release step shut the socket down; another shuts it down
            // when the connection ends, so the registry's clone never keeps it open.
            let handle_stream = stream.try_clone().ok();
            let closer = stream.try_clone().ok();
            let handle = thread::spawn(move || {
                let _slot = slot;
                serve_connection(&service, &lifecycle, stream, endpoint, peer, binding);
                if let Some(c) = closer {
                    let _ = c.shutdown(Shutdown::Both);
                }
            });
            let mut open = self
                .connections
                .lock()
                .unwrap_or_else(PoisonError::into_inner);
            open.retain(|o| !o.thread.is_finished());
            open.push(Open {
                thread: handle,
                stream: handle_stream,
            });
        }
    }
}

/// The receive side of an admitted connection: no ancillary data ([`PlainReader`]) and a
/// settable read timeout ([`TimedRead`]).
struct Peer<'a>(&'a UnixStream);

impl io::Read for Peer<'_> {
    fn read(&mut self, buf: &mut [u8]) -> io::Result<usize> {
        PlainReader(self.0).read(buf)
    }
}

impl TimedRead for Peer<'_> {
    fn set_read_timeout(&mut self, timeout: Duration) -> io::Result<()> {
        self.0.set_read_timeout(Some(timeout))
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
    lifecycle: &Arc<Lifecycle>,
    stream: UnixStream,
    endpoint: Endpoint,
    peer: PeerCred,
    binding: Option<HostBinding>,
) {
    let limits = service.config.limits;
    if stream.set_nonblocking(false).is_err()
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
    let idle = Duration::from_secs(limits.idle_seconds);
    let request = Duration::from_secs(limits.request_seconds);
    loop {
        // J-2 (M5a-13 L10): the idle deadline between frames and a per-request deadline
        // for the whole frame, so a slow-drip peer cannot hold a cap slot.
        let frame = read_frame_timed(&mut Peer(&stream), &frame_limits, idle, request);
        if matches!(frame, Err(FrameError::Closed)) {
            return;
        }
        // J-8: a request that arrives while stopping is answered SERVICE_STOPPING and the
        // connection closed (an uncommitted ingest session is abandoned with it).
        let Some(_in_flight) = lifecycle.enter() else {
            let _ = write_control(&mut writer, &refused(SERVICE_STOPPING));
            let _ = writer.flush();
            return;
        };
        let next = match frame {
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
                        // M5a-13 L9: the loaded key source stops using the revoked key, so
                        // issuing falls back to a valid older key.
                        if self
                            .service
                            .keys
                            .sync_revocations(&*self.service.ledger)
                            .is_err()
                        {
                            eprintln!(
                                "writ-serviced: key source not synchronized with the revocation"
                            );
                        }
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

#[cfg(test)]
#[allow(clippy::unwrap_used, clippy::expect_used, clippy::panic)]
mod tests {
    use super::*;
    use std::os::unix::fs::PermissionsExt;
    use writ_broker::writ_kernel::json::Digest;
    use writ_hostsys::deployment::{
        observe, static_facts_value, DeploymentConfig, FactPlan, IdentityPlan, REQUIRED_SYSCTLS,
    };

    /// M5a-13 L10: a received Unix socket is verified for its group too.
    #[test]
    fn a_received_socket_with_another_group_is_refused() {
        let d = std::env::temp_dir().join(format!("writ-service-gid-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&d);
        std::fs::create_dir_all(&d).unwrap();
        let path = d.join("host.sock");
        let l = UnixListener::bind(&path).unwrap();
        std::fs::set_permissions(&path, std::fs::Permissions::from_mode(0o660)).unwrap();
        let m = std::fs::symlink_metadata(&path).unwrap();
        assert_eq!(
            verify_unix(&l, &path, 0o660, m.uid(), Some(m.gid())),
            Ok(())
        );
        let wrong = verify_unix(&l, &path, 0o660, m.uid(), Some(m.gid().wrapping_add(1)));
        assert!(wrong.unwrap_err().contains("owner"));
        drop(l);
        let _ = std::fs::remove_dir_all(&d);
    }

    /// M5a-13 M5: the recheck timer keeps a holding proof current past its interval, and a
    /// changed fact is withdrawn by the timer without any request.
    #[test]
    fn the_recheck_timer_keeps_the_proof_current_and_withdraws_on_change() {
        let d = std::env::temp_dir().join(format!("writ-service-timer-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&d);
        std::fs::create_dir_all(d.join("state")).unwrap();
        let state = d.join("state");
        std::fs::set_permissions(&state, std::fs::Permissions::from_mode(0o700)).unwrap();
        std::fs::write(d.join("helper"), b"x").unwrap();
        std::fs::write(
            d.join("passwd"),
            "human:x:1000:1000::/:/bin/sh\nwrit:x:990:990::/:/bin/false\n\
             agent:x:1001:1001::/:/bin/sh\nwrit-job:x:992:992::/:/bin/false\n",
        )
        .unwrap();
        std::fs::write(
            d.join("group"),
            "human:x:1000:\nwrit:x:990:\nagent:x:1001:\nwrit-agents:x:991:agent\nwrit-job:x:992:\n",
        )
        .unwrap();
        let plan = FactPlan {
            paths: vec![state.clone()],
            sysctls: REQUIRED_SYSCTLS.iter().map(|s| s.to_string()).collect(),
            binaries: vec![d.join("helper")],
            groups: vec!["writ".into(), "writ-agents".into(), "writ-job".into()],
            own_identity: true,
            group_file: Some(d.join("group")),
            identity: Some(IdentityPlan {
                service_uid: 990,
                service_gid: 990,
                agent_uids: vec![1001],
                job_uid: 992,
                desktop_uids: vec![1000],
                passwd_file: Some(d.join("passwd")),
            }),
            required_paths: vec![state.clone()],
        };
        let mut m = json::Map::new();
        m.insert("contract".into(), json::s("writ-readiness/1"));
        m.insert("governed_ready".into(), Value::Bool(true));
        m.insert("blocking".into(), Value::Arr(vec![]));
        m.insert(
            "probes".into(),
            Value::Arr(
                writ_hostsys::readiness::ProbeId::ALL
                    .into_iter()
                    .filter(|id| id.required())
                    .map(|id| id.name())
                    .chain(["P-AGENT-DESKTOP"])
                    .map(|id| json::obj([("id", json::s(id)), ("status", json::s("ok"))]))
                    .collect(),
            ),
        );
        m.insert("static_facts".into(), static_facts_value(&observe(&plan)));
        let bytes = json::canonical(&Value::Obj(m)).unwrap().into_bytes();
        std::fs::write(d.join("readiness.json"), &bytes).unwrap();
        let proof = VerifiedDeployment::issue(DeploymentConfig {
            report: d.join("readiness.json"),
            report_digest: Digest::of_bytes(&bytes).to_string(),
            plan,
            recheck_interval: Duration::from_millis(200),
        })
        .unwrap();
        let stop = Arc::new(AtomicBool::new(false));
        let timer = recheck_timer(Arc::clone(&proof), Arc::clone(&stop));
        thread::sleep(Duration::from_millis(700));
        assert!(proof.is_current(), "rechecked by the timer");
        std::fs::set_permissions(&state, std::fs::Permissions::from_mode(0o755)).unwrap();
        thread::sleep(Duration::from_millis(400));
        assert!(!proof.is_verified(), "withdrawn by the timer");
        stop.store(true, Ordering::SeqCst);
        timer.join().unwrap();
        let _ = std::fs::remove_dir_all(&d);
    }
}
