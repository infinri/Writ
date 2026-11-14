//! Trusted time (risk 14, D-017, D-018 A5, D-033 item 3;
//! docs/implementation/M3-TRUSTED-TIME.md revision 2).
//!
//! The broker reads time only through [`TrustedClock`]; the agent never
//! supplies authoritative time. Reading time is fallible: a clock-source
//! failure, an arithmetic overflow, an unacknowledged startup rollback or a
//! persisted-floor failure is a [`ClockError`], which the broker refuses as
//! `TIME_UNAVAILABLE` (externally the kernel's generic `PROMOTION_REFUSED`).
//! There is no fallback to zero, to a cached value or to the plain wall clock.
//!
//! **Local profile ([`HostClock`]).** Effective time is `base + elapsed`, where
//! `elapsed` comes from an [`ElapsedSource`] that keeps counting through system
//! suspend ([`Boottime`], Linux `CLOCK_BOOTTIME`). The wall clock
//! ([`WallSource`], [`SystemWall`]) sets the base only at startup and is
//! otherwise a drift detector: rollback below `-120 s` and forward jumps above
//! `+24 h` against effective time are reported as [`Drift`] diagnostics and
//! recorded as clock events, but never change effective time.
//!
//! **Startup** ([`HostClock::start`]) compares the wall clock `W0` with the
//! persisted high-water mark `H`: `W0 >= H` (or no `H`) starts from `W0`;
//! `H - 120 s <= W0 < H` starts from `H`; `W0 < H - 120 s` blocks every read
//! with [`ClockError::RollbackUnacknowledged`] until an operator
//! acknowledgement ([`HostClock::acknowledge`]) that an [`OperatorAuthority`]
//! authenticates and authorizes. The acknowledgement is recorded in the ledger,
//! starts the clock from `H` (never lower) and therefore never revives anything
//! that had expired at `H`. There is no agent path and no silent reset.
//!
//! **Persisted floor.** A time-sensitive decision takes its time from
//! [`observe`], which raises the ledger's high-water mark to the effective time
//! in its own fenced transaction and only then returns it as an
//! [`ObservedTime`] (a [`TrustedClock`] fixed at that instant). The floor is
//! therefore durable before the decision's result is final, even when the
//! governed operation is refused, rolled back or fails afterwards.
//!
//! **Residual risk.** While the broker runs, no wall-clock change can extend a
//! lifetime. Across downtime there is no elapsed baseline: a host clock moved
//! back during downtime by less than the downtime is not detected, so security
//! across restarts still depends on host-clock integrity (M5).
//!
//! [`FixedClock`] is an injected clock for tests and harnesses; it can inject
//! failures.

use std::sync::atomic::{AtomicU8, Ordering};
use std::time::{SystemTime, UNIX_EPOCH};

use crate::refusal::Refusal;
use crate::store::{LedgerStore, StoreError};
use std::fmt;
use writ_kernel::time::Timestamp;

const NANOS_PER_SECOND: i128 = 1_000_000_000;

/// At startup, a wall clock at most this far below the high-water mark starts
/// the broker from the mark; further below fails closed until acknowledged.
pub const STARTUP_ROLLBACK_TOLERANCE_SECONDS: i64 = 120;
/// Diagnostic threshold (never an allowance): wall clock this far behind
/// effective time while running is reported as a rollback.
pub const ROLLBACK_DIAGNOSTIC_SECONDS: i64 = 120;
/// Diagnostic threshold (never an allowance): wall clock this far ahead of
/// effective time while running is reported as a forward jump.
pub const FORWARD_JUMP_DIAGNOSTIC_SECONDS: i64 = 86_400;

/// Why trusted time is unavailable. Every variant is refused as
/// [`Refusal::TimeUnavailable`].
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum ClockError {
    /// The wall or elapsed clock source could not be read.
    Source,
    /// The elapsed clock went backwards (a source fault).
    ElapsedRegressed,
    /// Effective time is not representable (arithmetic overflow or outside
    /// the kernel's timestamp range).
    Overflow,
    /// Startup found the wall clock more than 120 s below the high-water mark
    /// and no operator has acknowledged it.
    RollbackUnacknowledged,
    /// The persisted high-water mark is later than the effective time read.
    BehindFloor,
    /// The persisted floor could not be read, raised or audited.
    Floor(StoreError),
}

impl ClockError {
    /// The refusal for this error (always `TIME_UNAVAILABLE`).
    pub fn refusal(&self) -> Refusal {
        Refusal::TimeUnavailable
    }
}

impl From<ClockError> for Refusal {
    fn from(e: ClockError) -> Refusal {
        e.refusal()
    }
}

impl fmt::Display for ClockError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            ClockError::Source => f.write_str("clock SOURCE"),
            ClockError::ElapsedRegressed => f.write_str("clock ELAPSED_REGRESSED"),
            ClockError::Overflow => f.write_str("clock OVERFLOW"),
            ClockError::RollbackUnacknowledged => f.write_str("clock ROLLBACK_UNACKNOWLEDGED"),
            ClockError::BehindFloor => f.write_str("clock BEHIND_FLOOR"),
            ClockError::Floor(e) => write!(f, "clock FLOOR {e}"),
        }
    }
}

impl std::error::Error for ClockError {}

/// The broker's only source of time for decisions.
pub trait TrustedClock {
    /// The current trusted time, or why it is unavailable.
    fn now(&self) -> Result<Timestamp, ClockError>;
}

/// An injected clock that returns exactly the time (or the failure) it was given.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct FixedClock {
    now: Result<Timestamp, ClockError>,
}

impl FixedClock {
    /// A clock fixed at `now`.
    pub fn new(now: Timestamp) -> FixedClock {
        FixedClock { now: Ok(now) }
    }

    /// A clock whose every read fails with `error`.
    pub fn failing(error: ClockError) -> FixedClock {
        FixedClock { now: Err(error) }
    }

    /// Set the time (clears an injected failure).
    pub fn set(&mut self, now: Timestamp) {
        self.now = Ok(now);
    }

    /// Make every following read fail with `error`.
    pub fn fail(&mut self, error: ClockError) {
        self.now = Err(error);
    }

    /// Move the time by `seconds` (negative values move it back; saturating).
    /// No effect while a failure is injected.
    pub fn advance(&mut self, seconds: i64) {
        if let Ok(now) = &mut self.now {
            *now = now.plus_seconds(seconds);
        }
    }
}

impl TrustedClock for FixedClock {
    fn now(&self) -> Result<Timestamp, ClockError> {
        self.now.clone()
    }
}

impl EffectiveClock for FixedClock {
    fn read(&self) -> Result<Reading, ClockError> {
        Ok(Reading {
            effective: self.now.clone()?,
            wall: None,
            drift: Drift::Normal,
            drift_changed: false,
        })
    }
}

/// The instant `secs` seconds and `nanos` nanoseconds after 1970-01-01T00:00:00Z,
/// if the kernel can represent it (years 0000 to 9999).
pub fn timestamp_from_parts(secs: i64, nanos: u32) -> Option<Timestamp> {
    if i128::from(nanos) >= NANOS_PER_SECOND {
        return None;
    }
    let whole = Timestamp::parse_strict("1970-01-01T00:00:00Z")?.plus_seconds(secs);
    if whole.unix_seconds() != secs {
        return None;
    }
    let text = whole.to_string();
    if Timestamp::parse_strict(&text) != Some(whole) {
        return None;
    }
    if nanos == 0 {
        return Some(whole);
    }
    let stem = text.strip_suffix('Z')?;
    Timestamp::parse_rfc3339(&format!("{stem}.{nanos:09}Z"))
}

fn to_nanos(t: Timestamp) -> i128 {
    i128::from(t.unix_seconds()) * NANOS_PER_SECOND + i128::from(t.subsec_nanos())
}

fn from_nanos(ns: i128) -> Result<Timestamp, ClockError> {
    let secs = i64::try_from(ns.div_euclid(NANOS_PER_SECOND)).map_err(|_| ClockError::Overflow)?;
    let nanos = u32::try_from(ns.rem_euclid(NANOS_PER_SECOND)).map_err(|_| ClockError::Overflow)?;
    timestamp_from_parts(secs, nanos).ok_or(ClockError::Overflow)
}

fn seconds(s: i64) -> i128 {
    i128::from(s) * NANOS_PER_SECOND
}

/// The wall clock: nanoseconds since 1970-01-01T00:00:00Z (negative before).
/// Used only for the startup base and for drift diagnostics.
pub trait WallSource {
    /// The current wall-clock reading.
    fn wall_nanos(&self) -> Result<i128, ClockError>;
}

/// A monotonic elapsed-time clock that keeps counting during system suspend.
pub trait ElapsedSource {
    /// The current reading in nanoseconds from an arbitrary fixed origin.
    fn elapsed_nanos(&self) -> Result<i128, ClockError>;
}

/// The host wall clock (`std::time::SystemTime`).
#[derive(Clone, Copy, Debug, Default)]
pub struct SystemWall;

impl WallSource for SystemWall {
    fn wall_nanos(&self) -> Result<i128, ClockError> {
        match SystemTime::now().duration_since(UNIX_EPOCH) {
            Ok(d) => i128::try_from(d.as_nanos()).map_err(|_| ClockError::Overflow),
            Err(e) => i128::try_from(e.duration().as_nanos())
                .map(|n| -n)
                .map_err(|_| ClockError::Overflow),
        }
    }
}

/// Linux `CLOCK_BOOTTIME` through `rustix` (monotonic, counts suspend).
#[derive(Clone, Copy, Debug, Default)]
pub struct Boottime;

impl ElapsedSource for Boottime {
    fn elapsed_nanos(&self) -> Result<i128, ClockError> {
        let ts = rustix::time::clock_gettime_dynamic(rustix::time::DynamicClockId::Boottime)
            .map_err(|_| ClockError::Source)?;
        let nsec = i128::from(ts.tv_nsec);
        if !(0..NANOS_PER_SECOND).contains(&nsec) || ts.tv_sec < 0 {
            return Err(ClockError::Source);
        }
        Ok(i128::from(ts.tv_sec) * NANOS_PER_SECOND + nsec)
    }
}

/// Wall clock relative to effective time while running (a diagnostic only).
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Drift {
    /// Within the diagnostic thresholds.
    Normal,
    /// Wall clock more than 120 s behind effective time.
    Rollback,
    /// Wall clock more than 24 h ahead of effective time.
    ForwardJump,
}

impl Drift {
    fn code(self) -> u8 {
        match self {
            Drift::Normal => 0,
            Drift::Rollback => 1,
            Drift::ForwardJump => 2,
        }
    }

    fn event_kind(self) -> ClockEventKind {
        match self {
            Drift::Normal => ClockEventKind::DriftNormal,
            Drift::Rollback => ClockEventKind::RollbackDetected,
            Drift::ForwardJump => ClockEventKind::ForwardJumpDetected,
        }
    }
}

/// One reading of an [`EffectiveClock`].
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Reading {
    /// Effective (trusted) time.
    pub effective: Timestamp,
    /// The wall clock at the same reading, when known and representable.
    pub wall: Option<Timestamp>,
    /// Drift diagnostic.
    pub drift: Drift,
    /// `true` when `drift` differs from the last drift recorded in the ledger.
    pub drift_changed: bool,
}

/// A clock whose readings [`observe`] persists before they are used.
pub trait EffectiveClock {
    /// Read effective time and its drift diagnostic.
    fn read(&self) -> Result<Reading, ClockError>;

    /// Called after a changed drift was recorded in the ledger.
    fn drift_recorded(&self, _drift: Drift) {}
}

/// An effective time already persisted as the ledger's floor: the only
/// production [`TrustedClock`]; built only by [`observe`].
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct ObservedTime(Timestamp);

impl ObservedTime {
    /// The observed instant.
    pub fn at(&self) -> Timestamp {
        self.0
    }
}

impl TrustedClock for ObservedTime {
    fn now(&self) -> Result<Timestamp, ClockError> {
        Ok(self.0)
    }
}

/// Read `clock` and persist the effective time as the ledger's floor (raised,
/// never lowered) in its own fenced transaction, then return it for one
/// time-sensitive decision. A changed drift diagnostic is recorded as a clock
/// event. Any failure is a [`ClockError`] (fail closed); a stored floor later
/// than the reading is [`ClockError::BehindFloor`].
pub fn observe(
    clock: &dyn EffectiveClock,
    store: &mut dyn LedgerStore,
) -> Result<ObservedTime, ClockError> {
    let reading = clock.read()?;
    let stored = store
        .raise_time_floor(reading.effective)
        .map_err(ClockError::Floor)?;
    if stored > reading.effective {
        return Err(ClockError::BehindFloor);
    }
    if reading.drift_changed {
        store
            .record_clock_event(&ClockEvent {
                kind: reading.drift.event_kind(),
                effective: Some(reading.effective),
                wall: reading.wall,
                floor: Some(stored),
                operator: None,
            })
            .map_err(ClockError::Floor)?;
        clock.drift_recorded(reading.drift);
    }
    Ok(ObservedTime(reading.effective))
}

/// Kinds of clock audit events.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum ClockEventKind {
    /// Started from the wall clock (`W0 >= H` or no `H`).
    Startup,
    /// Started from `H` (`H - 120 s <= W0 < H`).
    StartupRollbackAbsorbed,
    /// `W0 < H - 120 s`: time-sensitive operations blocked until acknowledged.
    StartupRollbackBlocked,
    /// Wall clock more than 120 s behind effective time while running.
    RollbackDetected,
    /// Wall clock more than 24 h ahead of effective time while running.
    ForwardJumpDetected,
    /// Drift back within the thresholds.
    DriftNormal,
    /// An authorized operator acknowledged a blocked startup; started from `H`.
    Acknowledged,
    /// An acknowledgement was refused: not authenticated.
    AcknowledgementUnauthenticated,
    /// An acknowledgement was refused: authenticated but not authorized.
    AcknowledgementUnauthorized,
    /// An acknowledgement named a floor other than the persisted one.
    AcknowledgementStale,
}

impl ClockEventKind {
    /// Stable code stored in the ledger.
    pub fn as_str(self) -> &'static str {
        match self {
            ClockEventKind::Startup => "startup",
            ClockEventKind::StartupRollbackAbsorbed => "startup_rollback_absorbed",
            ClockEventKind::StartupRollbackBlocked => "startup_rollback_blocked",
            ClockEventKind::RollbackDetected => "rollback_detected",
            ClockEventKind::ForwardJumpDetected => "forward_jump_detected",
            ClockEventKind::DriftNormal => "drift_normal",
            ClockEventKind::Acknowledged => "acknowledged",
            ClockEventKind::AcknowledgementUnauthenticated => "acknowledgement_unauthenticated",
            ClockEventKind::AcknowledgementUnauthorized => "acknowledgement_unauthorized",
            ClockEventKind::AcknowledgementStale => "acknowledgement_stale",
        }
    }
}

/// A clock audit record (operator record; never agent-facing).
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct ClockEvent {
    /// What happened.
    pub kind: ClockEventKind,
    /// Effective time, when established.
    pub effective: Option<Timestamp>,
    /// Wall clock, when read and representable.
    pub wall: Option<Timestamp>,
    /// High-water mark at the event.
    pub floor: Option<Timestamp>,
    /// Operator identity (acknowledgements only).
    pub operator: Option<String>,
}

/// How [`HostClock::start`] set the base.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum StartOutcome {
    /// From the wall clock.
    Wall,
    /// From the high-water mark (small rollback across downtime absorbed).
    AbsorbedRollback,
    /// Blocked until an operator acknowledgement.
    Blocked,
}

/// Maximum length of an operator identity.
pub const MAX_OPERATOR_ID_LEN: usize = 128;

/// An authenticated operator identity, as established by an [`OperatorAuthority`].
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct OperatorId(String);

impl OperatorId {
    /// `Some` for 1 to [`MAX_OPERATOR_ID_LEN`] characters.
    pub fn new(id: &str) -> Option<OperatorId> {
        (!id.is_empty() && id.chars().count() <= MAX_OPERATOR_ID_LEN)
            .then(|| OperatorId(id.to_string()))
    }

    /// The identity text.
    pub fn as_str(&self) -> &str {
        &self.0
    }
}

/// An operator's acknowledgement of a blocked startup. It is presented through
/// the operator channel only (never an agent request field), names the
/// high-water mark it acknowledges, and carries a credential that only the
/// [`OperatorAuthority`] interprets.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Acknowledgement {
    /// The persisted high-water mark the operator acknowledges starting from.
    pub floor: Timestamp,
    /// Authority-specific proof of the operator's identity and intent.
    pub credential: Vec<u8>,
}

/// Why an [`OperatorAuthority`] refused an acknowledgement.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum OperatorRefusal {
    /// The credential does not authenticate an operator.
    Unauthenticated,
    /// The operator is authenticated but may not acknowledge clock rollbacks.
    Unauthorized,
}

/// Authenticates and authorizes clock acknowledgements. There is no production
/// implementation yet (the operator channel is later work); without one, a
/// blocked startup stays blocked.
pub trait OperatorAuthority {
    /// The operator who may acknowledge `ack`, or why not.
    fn authorize_clock_acknowledgement(
        &self,
        ack: &Acknowledgement,
    ) -> Result<OperatorId, OperatorRefusal>;
}

/// Why an acknowledgement did not unblock the clock.
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum AckError {
    /// The clock is not blocked; there is nothing to acknowledge (no silent reset).
    NotBlocked,
    /// The authority refused the acknowledgement (recorded in the ledger).
    Refused(OperatorRefusal),
    /// The acknowledgement names a floor other than the persisted one (recorded).
    Stale,
    /// Time or the ledger was unavailable; the clock stays blocked.
    Clock(ClockError),
}

/// The local-profile clock: effective time is `base + elapsed`.
#[derive(Debug)]
pub struct HostClock<W: WallSource = SystemWall, E: ElapsedSource = Boottime> {
    wall: W,
    elapsed: E,
    base: i128,
    anchor: i128,
    blocked: bool,
    outcome: StartOutcome,
    last_drift: AtomicU8,
}

impl<W: WallSource, E: ElapsedSource> HostClock<W, E> {
    /// Start against the persisted high-water mark of `store` (rule 3) and
    /// record the startup as a clock event. A running start persists its base
    /// as the floor. A blocked start returns a clock whose every read fails
    /// with [`ClockError::RollbackUnacknowledged`].
    pub fn start(
        wall: W,
        elapsed: E,
        store: &mut dyn LedgerStore,
    ) -> Result<HostClock<W, E>, ClockError> {
        let floor = store.time_floor().map_err(ClockError::Floor)?;
        let w0 = wall.wall_nanos()?;
        let anchor = elapsed.elapsed_nanos()?;
        let tolerance = seconds(STARTUP_ROLLBACK_TOLERANCE_SECONDS);
        let (base, outcome) = match floor.map(to_nanos) {
            None => (w0, StartOutcome::Wall),
            Some(h) if w0 >= h => (w0, StartOutcome::Wall),
            Some(h) if w0 >= h.checked_sub(tolerance).ok_or(ClockError::Overflow)? => {
                (h, StartOutcome::AbsorbedRollback)
            }
            Some(h) => (h, StartOutcome::Blocked),
        };
        let blocked = outcome == StartOutcome::Blocked;
        let effective = if blocked {
            None
        } else {
            Some(from_nanos(base)?)
        };
        let kind = match outcome {
            StartOutcome::Wall => ClockEventKind::Startup,
            StartOutcome::AbsorbedRollback => ClockEventKind::StartupRollbackAbsorbed,
            StartOutcome::Blocked => ClockEventKind::StartupRollbackBlocked,
        };
        store
            .record_clock_event(&ClockEvent {
                kind,
                effective,
                wall: from_nanos(w0).ok(),
                floor,
                operator: None,
            })
            .map_err(ClockError::Floor)?;
        if let Some(at) = effective {
            let stored = store.raise_time_floor(at).map_err(ClockError::Floor)?;
            if stored > at {
                return Err(ClockError::BehindFloor);
            }
        }
        Ok(HostClock {
            wall,
            elapsed,
            base,
            anchor,
            blocked,
            outcome,
            last_drift: AtomicU8::new(Drift::Normal.code()),
        })
    }

    /// How the base was chosen at startup.
    pub fn outcome(&self) -> StartOutcome {
        self.outcome
    }

    /// `true` while a startup rollback is unacknowledged.
    pub fn is_blocked(&self) -> bool {
        self.blocked
    }

    /// Acknowledge a blocked startup (rule 3 and 6). The authority must
    /// authenticate and authorize `ack`, and `ack` must name the persisted
    /// high-water mark `H`. Every attempt on a blocked clock is recorded in the
    /// ledger; on success the clock starts from `H` (the floor is not lowered
    /// and nothing that had expired at `H` becomes valid again).
    pub fn acknowledge(
        &mut self,
        ack: &Acknowledgement,
        authority: &dyn OperatorAuthority,
        store: &mut dyn LedgerStore,
    ) -> Result<(), AckError> {
        if !self.blocked {
            return Err(AckError::NotBlocked);
        }
        let floor = store
            .time_floor()
            .map_err(|e| AckError::Clock(ClockError::Floor(e)))?;
        let wall = self.wall.wall_nanos().ok().and_then(|w| from_nanos(w).ok());
        let mut audit = |kind, operator: Option<&OperatorId>| {
            store
                .record_clock_event(&ClockEvent {
                    kind,
                    effective: None,
                    wall,
                    floor,
                    operator: operator.map(|o| o.as_str().to_string()),
                })
                .map_err(|e| AckError::Clock(ClockError::Floor(e)))
        };
        let operator = match authority.authorize_clock_acknowledgement(ack) {
            Ok(operator) => operator,
            Err(refusal) => {
                let kind = match refusal {
                    OperatorRefusal::Unauthenticated => {
                        ClockEventKind::AcknowledgementUnauthenticated
                    }
                    OperatorRefusal::Unauthorized => ClockEventKind::AcknowledgementUnauthorized,
                };
                audit(kind, None)?;
                return Err(AckError::Refused(refusal));
            }
        };
        let Some(h) = floor.filter(|h| *h == ack.floor) else {
            audit(ClockEventKind::AcknowledgementStale, Some(&operator))?;
            return Err(AckError::Stale);
        };
        let anchor = self.elapsed.elapsed_nanos().map_err(AckError::Clock)?;
        audit(ClockEventKind::Acknowledged, Some(&operator))?;
        let stored = store
            .raise_time_floor(h)
            .map_err(|e| AckError::Clock(ClockError::Floor(e)))?;
        if stored > h {
            return Err(AckError::Clock(ClockError::BehindFloor));
        }
        self.base = to_nanos(h);
        self.anchor = anchor;
        self.blocked = false;
        self.last_drift
            .store(Drift::Normal.code(), Ordering::Relaxed);
        Ok(())
    }
}

impl<W: WallSource, E: ElapsedSource> EffectiveClock for HostClock<W, E> {
    fn read(&self) -> Result<Reading, ClockError> {
        if self.blocked {
            return Err(ClockError::RollbackUnacknowledged);
        }
        let now = self.elapsed.elapsed_nanos()?;
        let elapsed = now.checked_sub(self.anchor).ok_or(ClockError::Overflow)?;
        if elapsed < 0 {
            return Err(ClockError::ElapsedRegressed);
        }
        let effective_ns = self.base.checked_add(elapsed).ok_or(ClockError::Overflow)?;
        let effective = from_nanos(effective_ns)?;
        let wall_ns = self.wall.wall_nanos()?;
        let drift_ns = wall_ns
            .checked_sub(effective_ns)
            .ok_or(ClockError::Overflow)?;
        let drift = if drift_ns < -seconds(ROLLBACK_DIAGNOSTIC_SECONDS) {
            Drift::Rollback
        } else if drift_ns > seconds(FORWARD_JUMP_DIAGNOSTIC_SECONDS) {
            Drift::ForwardJump
        } else {
            Drift::Normal
        };
        Ok(Reading {
            effective,
            wall: from_nanos(wall_ns).ok(),
            drift,
            drift_changed: self.last_drift.load(Ordering::Relaxed) != drift.code(),
        })
    }

    fn drift_recorded(&self, drift: Drift) {
        self.last_drift.store(drift.code(), Ordering::Relaxed);
    }
}

#[cfg(test)]
#[allow(clippy::unwrap_used, clippy::expect_used, clippy::panic)]
mod tests;
