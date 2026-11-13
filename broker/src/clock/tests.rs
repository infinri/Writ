//! Trusted-time tests (M3-TRUSTED-TIME revision 2). Wall and elapsed sources
//! are injected; nothing here changes the host clock or suspends the machine.

use super::*;
use crate::authorize::authorize;
use crate::permit::{Permit, PermitId, PermitIssueError, VerifiedPermit};
use crate::policy::{ActivePolicy, PolicyState};
use crate::test_support::*;
use std::cell::Cell;
use std::rc::Rc;
use writ_kernel::json::Value;

fn ts(s: &str) -> Timestamp {
    Timestamp::parse_rfc3339(s).unwrap()
}

fn ns(s: &str) -> i128 {
    to_nanos(ts(s))
}

/// A shared, settable source reading; `None` makes the read fail.
#[derive(Clone, Debug)]
struct Manual(Rc<Cell<Option<i128>>>);

impl WallSource for Manual {
    fn wall_nanos(&self) -> Result<i128, ClockError> {
        self.0.get().ok_or(ClockError::Source)
    }
}

impl ElapsedSource for Manual {
    fn elapsed_nanos(&self) -> Result<i128, ClockError> {
        self.0.get().ok_or(ClockError::Source)
    }
}

/// An injected host: a wall clock and a suspend-counting elapsed clock.
struct Host {
    wall: Manual,
    elapsed: Manual,
}

impl Host {
    /// Wall clock at `wall`; elapsed clock at an arbitrary origin (1000 s after boot).
    fn new(wall: &str) -> Host {
        Host {
            wall: Manual(Rc::new(Cell::new(Some(ns(wall))))),
            elapsed: Manual(Rc::new(Cell::new(Some(1_000 * NANOS_PER_SECOND)))),
        }
    }

    fn start(&self, store: &mut MemoryLedger) -> Result<HostClock<Manual, Manual>, ClockError> {
        HostClock::start(self.wall.clone(), self.elapsed.clone(), store)
    }

    /// Move the wall clock by `wall` seconds and the elapsed clock by `elapsed` seconds.
    fn step(&self, wall: i64, elapsed: i64) {
        self.wall
            .0
            .set(self.wall.0.get().map(|w| w + seconds(wall)));
        self.elapsed
            .0
            .set(self.elapsed.0.get().map(|e| e + seconds(elapsed)));
    }

    fn set_wall(&self, s: &str) {
        self.wall.0.set(Some(ns(s)));
    }
}

const T0: &str = "2026-11-05T12:00:00Z";

/// A ledger whose high-water mark is `h` (as left by an earlier run).
fn ledger_at(h: &str) -> MemoryLedger {
    MemoryLedger {
        floor: Some(ts(h)),
        floor_history: vec![ts(h)],
        ..MemoryLedger::default()
    }
}

/// Issue a permit for the base request at `clock` (expires 30 s later).
fn issue_at(clock: &dyn TrustedClock) -> Permit {
    let active = ActivePolicy::new(PolicyState::loaded(reference_bundle()));
    let shared = active.shared();
    let authorized = authorize(
        &verify(&base_request()),
        &shared.trusted_inputs(revision(5)),
        clock,
    )
    .unwrap();
    Permit::issue(
        &PermitId::new("permit-t").unwrap(),
        &authorized,
        clock,
        &permit_key(PERMIT_KEY),
    )
    .unwrap()
}

fn verify_at(permit: &Permit, clock: &dyn TrustedClock) -> Result<VerifiedPermit, Refusal> {
    let active = ActivePolicy::new(PolicyState::loaded(reference_bundle()));
    let req: Value = base_request();
    let shared = active.shared();
    let result = permit.verify(&req, &permit_key(PERMIT_KEY), clock, &shared);
    drop(shared);
    result
}

/// Observe the clock and verify `permit` at that time, as the broker does.
fn observe_and_verify(
    clock: &dyn EffectiveClock,
    store: &mut MemoryLedger,
    permit: &Permit,
) -> Result<VerifiedPermit, Refusal> {
    let at = observe(clock, store)?;
    verify_at(permit, &at)
}

struct Authority;

const ADMIN: &[u8] = b"operator-admin";
const VIEWER: &[u8] = b"operator-viewer";

impl OperatorAuthority for Authority {
    fn authorize_clock_acknowledgement(
        &self,
        ack: &Acknowledgement,
    ) -> Result<OperatorId, OperatorRefusal> {
        match ack.credential.as_slice() {
            ADMIN => Ok(OperatorId::new("operator:admin").unwrap()),
            VIEWER => Err(OperatorRefusal::Unauthorized),
            _ => Err(OperatorRefusal::Unauthenticated),
        }
    }
}

fn ack(floor: &str, credential: &[u8]) -> Acknowledgement {
    Acknowledgement {
        floor: ts(floor),
        credential: credential.to_vec(),
    }
}

#[test]
fn fixed_clock_returns_the_injected_time_or_failure() {
    let t = ts(T0);
    let mut clock = FixedClock::new(t);
    assert_eq!(clock.now(), Ok(t));
    assert_eq!(clock.now(), Ok(t), "reading does not move time");
    clock.advance(31);
    assert_eq!(clock.now().unwrap().to_string(), "2026-11-05T12:00:31Z");
    clock.fail(ClockError::Source);
    assert_eq!(clock.now(), Err(ClockError::Source));
    clock.advance(5);
    assert_eq!(
        clock.now(),
        Err(ClockError::Source),
        "advance keeps the failure"
    );
    clock.set(t);
    assert_eq!(clock.now(), Ok(t));
    assert_eq!(
        FixedClock::failing(ClockError::Overflow).read(),
        Err(ClockError::Overflow)
    );
}

#[test]
fn timestamp_parts_round_trip_within_the_kernel_range() {
    for s in [
        "1970-01-01T00:00:00Z",
        "2026-11-05T12:00:00.000000001Z",
        "2026-11-05T12:00:00.5Z",
        "1969-12-31T23:59:59.999999999Z",
        "0000-01-01T00:00:00Z",
        "9999-12-31T23:59:59.999999999Z",
    ] {
        let t = ts(s);
        assert_eq!(
            timestamp_from_parts(t.unix_seconds(), t.subsec_nanos()),
            Some(t),
            "{s}"
        );
        assert_eq!(from_nanos(to_nanos(t)), Ok(t), "{s}");
    }
    let last = ts("9999-12-31T23:59:59Z").unix_seconds();
    assert_eq!(timestamp_from_parts(last + 1, 0), None);
    assert_eq!(timestamp_from_parts(i64::MAX, 0), None);
    assert_eq!(timestamp_from_parts(0, 1_000_000_000), None);
    assert_eq!(from_nanos(i128::MAX), Err(ClockError::Overflow));
}

#[test]
fn first_start_uses_the_wall_clock_and_persists_the_floor() {
    let host = Host::new(T0);
    let mut store = MemoryLedger::default();
    let clock = host.start(&mut store).unwrap();
    assert_eq!(clock.outcome(), StartOutcome::Wall);
    assert_eq!(store.floor, Some(ts(T0)));
    assert_eq!(store.kinds(), vec![ClockEventKind::Startup]);
    host.step(7, 7);
    let at = observe(&clock, &mut store).unwrap();
    assert_eq!(at.at(), ts("2026-11-05T12:00:07Z"));
    assert_eq!(at.now(), Ok(at.at()));
    assert_eq!(store.floor, Some(at.at()));
}

#[test]
fn elapsed_time_advances_through_suspend() {
    let host = Host::new(T0);
    let mut store = MemoryLedger::default();
    let clock = host.start(&mut store).unwrap();
    let permit = issue_at(&observe(&clock, &mut store).unwrap());
    assert_eq!(permit.expires_at(), "2026-11-05T12:00:30Z");
    host.step(10, 10);
    assert!(observe_and_verify(&clock, &mut store, &permit).is_ok());
    // A one-hour suspend: CLOCK_BOOTTIME counts it, so wall and elapsed both jump.
    host.step(3_600, 3_600);
    let reading = clock.read().unwrap();
    assert_eq!(reading.effective, ts("2026-11-05T13:00:10Z"));
    assert_eq!(
        reading.drift,
        Drift::Normal,
        "a suspend is not a clock anomaly"
    );
    assert_eq!(
        observe_and_verify(&clock, &mut store, &permit),
        Err(Refusal::PermitExpired),
        "the lifetime kept running through the suspend"
    );
    assert_eq!(store.floor, Some(ts("2026-11-05T13:00:10Z")));
    assert_eq!(store.kinds(), vec![ClockEventKind::Startup]);
}

#[test]
fn rollback_while_running_cannot_pause_a_running_lifetime() {
    let host = Host::new(T0);
    let mut store = MemoryLedger::default();
    let clock = host.start(&mut store).unwrap();
    let permit = issue_at(&observe(&clock, &mut store).unwrap());
    // The wall clock is set back one hour while 29 s elapse.
    host.step(-3_600, 29);
    let at = observe(&clock, &mut store).unwrap();
    assert_eq!(
        at.at(),
        ts("2026-11-05T12:00:29Z"),
        "effective time kept running"
    );
    assert!(verify_at(&permit, &at).is_ok());
    assert_eq!(
        store.kinds(),
        vec![ClockEventKind::Startup, ClockEventKind::RollbackDetected]
    );
    let event = store.events.last().unwrap();
    assert_eq!(event.wall, Some(ts("2026-11-05T11:00:00Z")));
    assert_eq!(event.effective, Some(at.at()));
    // One more second: the permit expires exactly when it would have without the rollback.
    host.step(1, 1);
    assert_eq!(
        observe_and_verify(&clock, &mut store, &permit),
        Err(Refusal::PermitExpired)
    );
    assert_eq!(
        store.kinds().len(),
        2,
        "an ongoing rollback is recorded once, not on every read"
    );
    // The wall clock is corrected: drift back to normal is recorded.
    host.set_wall("2026-11-05T12:00:31Z");
    host.step(0, 1);
    observe(&clock, &mut store).unwrap();
    assert_eq!(store.kinds().last(), Some(&ClockEventKind::DriftNormal));
    assert!(store.floor_never_lowered());
}

#[test]
fn small_wall_lag_is_not_a_rollback_diagnostic() {
    let host = Host::new(T0);
    let mut store = MemoryLedger::default();
    let clock = host.start(&mut store).unwrap();
    host.step(-120, 0);
    assert_eq!(clock.read().unwrap().drift, Drift::Normal);
    host.step(-1, 0);
    assert_eq!(clock.read().unwrap().drift, Drift::Rollback);
}

#[test]
fn forward_jump_while_running_is_reported_and_does_not_change_effective_time() {
    let host = Host::new(T0);
    let mut store = MemoryLedger::default();
    let clock = host.start(&mut store).unwrap();
    let permit = issue_at(&observe(&clock, &mut store).unwrap());
    host.step(86_405, 5);
    assert_eq!(
        clock.read().unwrap().drift,
        Drift::Normal,
        "24 h is not above 24 h"
    );
    host.step(1, 0);
    let at = observe(&clock, &mut store).unwrap();
    assert_eq!(at.at(), ts("2026-11-05T12:00:05Z"));
    assert!(verify_at(&permit, &at).is_ok());
    assert_eq!(
        store.kinds(),
        vec![ClockEventKind::Startup, ClockEventKind::ForwardJumpDetected]
    );
}

#[test]
fn expiry_observation_persists_despite_refusal_failure_and_restart() {
    let host = Host::new(T0);
    let mut store = MemoryLedger::default();
    let clock = host.start(&mut store).unwrap();
    let permit = issue_at(&observe(&clock, &mut store).unwrap());
    host.step(30, 30);
    // The expiry decision observes (and persists) its time first; then the governed
    // operation is refused and its own audit transaction fails.
    let refusal = observe_and_verify(&clock, &mut store, &permit).unwrap_err();
    assert_eq!(refusal, Refusal::PermitExpired);
    store.fail = Some("audit");
    assert!(store.record_refusal(refusal.code()).is_err());
    assert!(store.refusals.is_empty(), "the governed operation failed");
    assert_eq!(
        store.floor,
        Some(ts("2026-11-05T12:00:30Z")),
        "the floor persisted"
    );
    drop(clock);
    // Restart after the wall clock was set back 30 s and 90 s (within 120 s): the
    // broker starts from the floor and the permit stays expired.
    for back in [30, 90, 120] {
        let restarted = Host::new(T0);
        restarted.step(30 - back, 0);
        let clock = restarted.start(&mut store).unwrap();
        assert_eq!(clock.outcome(), StartOutcome::AbsorbedRollback, "{back}");
        assert_eq!(
            observe_and_verify(&clock, &mut store, &permit),
            Err(Refusal::PermitExpired),
            "{back}"
        );
    }
    // Further back: fail closed.
    let restarted = Host::new(T0);
    restarted.step(30 - 121, 0);
    let clock = restarted.start(&mut store).unwrap();
    assert_eq!(
        observe_and_verify(&clock, &mut store, &permit),
        Err(Refusal::TimeUnavailable)
    );
    assert_eq!(store.floor, Some(ts("2026-11-05T12:00:30Z")));
    assert!(store.floor_never_lowered());
}

#[test]
fn normal_long_downtime_is_accepted() {
    let mut store = ledger_at(T0);
    let host = Host::new("2026-11-07T12:00:00Z");
    let clock = host.start(&mut store).unwrap();
    assert_eq!(clock.outcome(), StartOutcome::Wall);
    assert!(!clock.is_blocked());
    assert_eq!(
        observe(&clock, &mut store).unwrap().at(),
        ts("2026-11-07T12:00:00Z")
    );
    assert_eq!(store.floor, Some(ts("2026-11-07T12:00:00Z")));
    assert_eq!(store.kinds(), vec![ClockEventKind::Startup]);
}

#[test]
fn small_rollback_across_downtime_starts_from_the_floor() {
    let mut store = ledger_at(T0);
    let host = Host::new("2026-11-05T11:58:00Z");
    let clock = host.start(&mut store).unwrap();
    assert_eq!(clock.outcome(), StartOutcome::AbsorbedRollback);
    host.step(1, 1);
    assert_eq!(
        observe(&clock, &mut store).unwrap().at(),
        ts("2026-11-05T12:00:01Z"),
        "lifetimes run from H"
    );
    assert_eq!(store.kinds(), vec![ClockEventKind::StartupRollbackAbsorbed]);
}

#[test]
fn large_backward_step_at_startup_fails_closed() {
    let mut store = ledger_at(T0);
    let host = Host::new("2026-11-05T11:57:59.999999999Z");
    let clock = host.start(&mut store).unwrap();
    assert_eq!(clock.outcome(), StartOutcome::Blocked);
    assert!(clock.is_blocked());
    assert_eq!(clock.read(), Err(ClockError::RollbackUnacknowledged));
    let err = observe(&clock, &mut store).unwrap_err();
    assert_eq!(err, ClockError::RollbackUnacknowledged);
    assert_eq!(err.refusal(), Refusal::TimeUnavailable);
    assert_eq!(err.refusal().external_code(), "PROMOTION_REFUSED");
    // Time passing does not unblock it.
    host.step(3_600, 3_600);
    assert_eq!(clock.read(), Err(ClockError::RollbackUnacknowledged));
    assert_eq!(store.floor, Some(ts(T0)));
    assert_eq!(store.kinds(), vec![ClockEventKind::StartupRollbackBlocked]);
    assert_eq!(store.events[0].floor, Some(ts(T0)));
    assert_eq!(store.events[0].effective, None);
}

#[test]
fn unauthenticated_and_unauthorized_acknowledgements_are_refused() {
    let mut store = ledger_at(T0);
    let host = Host::new("2026-11-04T12:00:00Z");
    let mut clock = host.start(&mut store).unwrap();
    for (credential, refusal, kind) in [
        (
            b"forged".as_slice(),
            OperatorRefusal::Unauthenticated,
            ClockEventKind::AcknowledgementUnauthenticated,
        ),
        (
            b"".as_slice(),
            OperatorRefusal::Unauthenticated,
            ClockEventKind::AcknowledgementUnauthenticated,
        ),
        (
            VIEWER,
            OperatorRefusal::Unauthorized,
            ClockEventKind::AcknowledgementUnauthorized,
        ),
    ] {
        assert_eq!(
            clock.acknowledge(&ack(T0, credential), &Authority, &mut store),
            Err(AckError::Refused(refusal))
        );
        assert!(clock.is_blocked());
        assert_eq!(clock.read(), Err(ClockError::RollbackUnacknowledged));
        assert_eq!(store.kinds().last(), Some(&kind));
        assert_eq!(store.events.last().unwrap().operator, None);
    }
    assert_eq!(store.floor, Some(ts(T0)));
}

#[test]
fn acknowledgement_starts_from_the_floor_and_never_revives_an_expired_permit() {
    let host = Host::new(T0);
    let mut store = MemoryLedger::default();
    let clock = host.start(&mut store).unwrap();
    let permit = issue_at(&observe(&clock, &mut store).unwrap());
    host.step(30, 30);
    assert_eq!(
        observe_and_verify(&clock, &mut store, &permit),
        Err(Refusal::PermitExpired)
    );
    let h = "2026-11-05T12:00:30Z";
    drop(clock);
    // Restart with the wall clock a day behind: blocked until acknowledged.
    let restarted = Host::new("2026-11-04T12:00:00Z");
    let mut clock = restarted.start(&mut store).unwrap();
    assert!(clock.is_blocked());
    clock
        .acknowledge(&ack(h, ADMIN), &Authority, &mut store)
        .unwrap();
    assert!(!clock.is_blocked());
    let event = store.events.last().unwrap();
    assert_eq!(event.kind, ClockEventKind::Acknowledged);
    assert_eq!(event.operator.as_deref(), Some("operator:admin"));
    assert_eq!(event.floor, Some(ts(h)));
    assert_eq!(store.floor, Some(ts(h)), "H not lowered");
    let at = observe(&clock, &mut store).unwrap();
    assert_eq!(at.at(), ts(h), "started from H, not from the wall clock");
    assert_eq!(verify_at(&permit, &at), Err(Refusal::PermitExpired));
    // The wall clock is still a day behind: a rollback diagnostic, not a pause.
    assert_eq!(
        store.kinds().last(),
        Some(&ClockEventKind::RollbackDetected)
    );
    restarted.step(0, 5);
    assert_eq!(
        observe(&clock, &mut store).unwrap().at(),
        ts("2026-11-05T12:00:35Z")
    );
    assert!(store.floor_never_lowered());
}

#[test]
fn stale_or_unneeded_acknowledgements_are_refused() {
    let mut store = ledger_at(T0);
    let host = Host::new("2026-11-04T12:00:00Z");
    let mut clock = host.start(&mut store).unwrap();
    // An acknowledgement naming another floor (an older or a lower one) is refused.
    for floor in ["2026-11-04T12:00:00Z", "2026-11-05T11:59:59Z"] {
        assert_eq!(
            clock.acknowledge(&ack(floor, ADMIN), &Authority, &mut store),
            Err(AckError::Stale)
        );
        assert!(clock.is_blocked());
        assert_eq!(
            store.kinds().last(),
            Some(&ClockEventKind::AcknowledgementStale)
        );
    }
    // A running clock has nothing to acknowledge: no silent reset.
    let running = Host::new("2026-11-06T00:00:00Z");
    let mut fresh = running.start(&mut store).unwrap();
    let events = store.events.len();
    assert_eq!(
        fresh.acknowledge(&ack("2026-11-06T00:00:00Z", ADMIN), &Authority, &mut store),
        Err(AckError::NotBlocked)
    );
    assert_eq!(store.events.len(), events);
    assert_eq!(store.floor, Some(ts("2026-11-06T00:00:00Z")));
}

#[test]
fn acknowledgement_fails_closed_when_the_ledger_or_clock_fails() {
    let mut store = ledger_at(T0);
    let host = Host::new("2026-11-04T12:00:00Z");
    let mut clock = host.start(&mut store).unwrap();
    store.fail = Some("clock_event");
    assert!(matches!(
        clock.acknowledge(&ack(T0, ADMIN), &Authority, &mut store),
        Err(AckError::Clock(ClockError::Floor(_)))
    ));
    assert!(clock.is_blocked(), "not unblocked without an audit record");
    host.elapsed.0.set(None);
    assert_eq!(
        clock.acknowledge(&ack(T0, ADMIN), &Authority, &mut store),
        Err(AckError::Clock(ClockError::Source))
    );
    assert!(clock.is_blocked());
}

#[test]
fn high_water_mark_is_never_lowered_by_any_operation() {
    let mut store = ledger_at("2026-11-05T12:00:30Z");
    // Direct raise with a lower value returns the stored maximum.
    assert_eq!(
        store.raise_time_floor(ts(T0)).unwrap(),
        ts("2026-11-05T12:00:30Z")
    );
    // Absorbed start, blocked start, acknowledgement, observation, a lower fixed clock.
    let absorbed = Host::new("2026-11-05T12:00:00Z");
    let clock = absorbed.start(&mut store).unwrap();
    absorbed.step(-3_600, 2);
    observe(&clock, &mut store).unwrap();
    let blocked = Host::new("2026-11-01T00:00:00Z");
    let mut clock = blocked.start(&mut store).unwrap();
    let h = store.floor.unwrap().to_string();
    clock
        .acknowledge(&ack(&h, ADMIN), &Authority, &mut store)
        .unwrap();
    observe(&clock, &mut store).unwrap();
    assert_eq!(
        observe(&FixedClock::new(ts(T0)), &mut store),
        Err(ClockError::BehindFloor)
    );
    assert_eq!(store.floor, Some(ts("2026-11-05T12:00:32Z")));
    assert!(store.floor_history.len() >= 6);
    assert!(store.floor_never_lowered(), "{:?}", store.floor_history);
}

#[test]
fn clock_source_failures_give_time_unavailable() {
    // At startup.
    let host = Host::new(T0);
    host.wall.0.set(None);
    assert_eq!(
        host.start(&mut MemoryLedger::default()).err(),
        Some(ClockError::Source)
    );
    let host = Host::new(T0);
    host.elapsed.0.set(None);
    assert_eq!(
        host.start(&mut MemoryLedger::default()).err(),
        Some(ClockError::Source)
    );
    // While running: elapsed failure, wall failure, elapsed regression.
    let host = Host::new(T0);
    let mut store = MemoryLedger::default();
    let clock = host.start(&mut store).unwrap();
    let permit = issue_at(&observe(&clock, &mut store).unwrap());
    let before = store.floor;
    host.elapsed.0.set(None);
    assert_eq!(observe(&clock, &mut store), Err(ClockError::Source));
    assert_eq!(
        observe_and_verify(&clock, &mut store, &permit),
        Err(Refusal::TimeUnavailable)
    );
    host.elapsed.0.set(Some(1_000 * NANOS_PER_SECOND));
    host.wall.0.set(None);
    assert_eq!(observe(&clock, &mut store), Err(ClockError::Source));
    host.set_wall(T0);
    host.elapsed.0.set(Some(999 * NANOS_PER_SECOND));
    assert_eq!(
        observe(&clock, &mut store),
        Err(ClockError::ElapsedRegressed)
    );
    assert_eq!(
        observe_and_verify(&clock, &mut store, &permit),
        Err(Refusal::TimeUnavailable)
    );
    assert_eq!(store.floor, before, "nothing persisted from a failed read");
}

#[test]
fn overflow_gives_time_unavailable() {
    // Effective time beyond the representable range.
    let host = Host::new("9999-12-31T23:59:58Z");
    let mut store = MemoryLedger::default();
    let clock = host.start(&mut store).unwrap();
    host.step(1, 1);
    assert!(observe(&clock, &mut store).is_ok());
    host.step(1, 1);
    assert_eq!(observe(&clock, &mut store), Err(ClockError::Overflow));
    assert_eq!(
        ClockError::Overflow.refusal().external_code(),
        "PROMOTION_REFUSED"
    );
    // Elapsed arithmetic overflow.
    let host = Host::new(T0);
    let clock = host.start(&mut MemoryLedger::default()).unwrap();
    host.elapsed.0.set(Some(i128::MAX));
    assert_eq!(clock.read(), Err(ClockError::Overflow));
    host.elapsed.0.set(Some(i128::MIN));
    assert_eq!(clock.read(), Err(ClockError::Overflow));
    // A wall clock beyond the range at startup.
    let host = Host::new(T0);
    host.wall.0.set(Some(i128::MAX / 2));
    assert_eq!(
        host.start(&mut MemoryLedger::default()).err(),
        Some(ClockError::Overflow)
    );
}

#[test]
fn floor_failures_give_time_unavailable() {
    let host = Host::new(T0);
    let mut store = MemoryLedger::default();
    let clock = host.start(&mut store).unwrap();
    let permit = issue_at(&observe(&clock, &mut store).unwrap());
    store.fail = Some("time_floor");
    let err = observe(&clock, &mut store).unwrap_err();
    assert!(matches!(err, ClockError::Floor(_)), "{err:?}");
    assert_eq!(err.refusal(), Refusal::TimeUnavailable);
    // The drift record is part of the observation: its failure fails closed too.
    host.step(-3_600, 1);
    store.fail = Some("clock_event");
    assert!(matches!(
        observe(&clock, &mut store),
        Err(ClockError::Floor(_))
    ));
    assert!(observe_and_verify(&clock, &mut store, &permit).is_ok());
    // Startup cannot read or raise the floor.
    let mut broken = MemoryLedger {
        fail_reads: true,
        ..MemoryLedger::default()
    };
    assert!(matches!(
        Host::new(T0).start(&mut broken),
        Err(ClockError::Floor(_))
    ));
    let mut broken = MemoryLedger {
        fail: Some("time_floor"),
        ..MemoryLedger::default()
    };
    assert!(matches!(
        Host::new(T0).start(&mut broken),
        Err(ClockError::Floor(_))
    ));
}

#[test]
fn decisions_refuse_unavailable_time() {
    let failing = FixedClock::failing(ClockError::Source);
    let active = ActivePolicy::new(PolicyState::loaded(reference_bundle()));
    let shared = active.shared();
    let inputs = shared.trusted_inputs(revision(5));
    let refusal = authorize(&verify(&base_request()), &inputs, &failing).unwrap_err();
    assert_eq!(refusal, Refusal::TimeUnavailable);
    assert_eq!(refusal.code(), "TIME_UNAVAILABLE");
    assert_eq!(refusal.external_code(), "PROMOTION_REFUSED");
    let authorized =
        authorize(&verify(&base_request()), &inputs, &FixedClock::new(ts(T0))).unwrap();
    assert_eq!(
        Permit::issue(
            &PermitId::new("p").unwrap(),
            &authorized,
            &failing,
            &permit_key(PERMIT_KEY)
        ),
        Err(PermitIssueError::TimeUnavailable)
    );
    drop(authorized);
    drop(shared);
    let permit = issue_at(&FixedClock::new(ts(T0)));
    assert_eq!(verify_at(&permit, &failing), Err(Refusal::TimeUnavailable));
}

#[test]
fn boottime_is_available_and_non_decreasing() {
    // The real CLOCK_BOOTTIME of this host; nothing is suspended or changed.
    let first = Boottime.elapsed_nanos().unwrap();
    let second = Boottime.elapsed_nanos().unwrap();
    assert!(first > 0);
    assert!(second >= first, "{first} then {second}");
    assert!(SystemWall.wall_nanos().unwrap() > ns("2020-01-01T00:00:00Z"));
}
