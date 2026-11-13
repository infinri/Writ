//! The broker's active authorization policy and its coordination gate (M3-02,
//! owner decision B of 2026-11-12; lock rules of D-029).
//!
//! [`ActivePolicy`] owns the currently active authorization bundle (D-018 A3:
//! resolved from trusted state; the broker's owner supplies it explicitly)
//! behind a shared/exclusive gate:
//!
//! - activating or replacing the policy ([`ActivePolicy::activate`]) takes the
//!   gate exclusively, so it waits until every shared holder has finished;
//! - use-time authorization ([`SharedPolicy::trusted_inputs`], the only way
//!   to build [`TrustedInputs`]; its result [`Authorized`] borrows the hold;
//!   [`ActivePolicy::execute`] acquires the gate internally), permit
//!   verification ([`crate::permit::Permit::verify`]) and, from M3-04, dispatch
//!   and effect authorization take it shared ([`ActivePolicy::shared`]) and hold
//!   the one [`SharedPolicy`] across the check and the effect authorization, so a
//!   policy change cannot slip between them. Independent shared holders run
//!   concurrently.
//!
//! Lock order (D-029): the gate is taken before any per-key lock or storage
//! transaction, and never while holding a database write transaction. The gate
//! is never re-entrant: a holder must not call [`ActivePolicy::shared`] or
//! [`ActivePolicy::activate`] again on the same thread while it holds a
//! [`SharedPolicy`] (a pending activation would make that a deadlock). Guards
//! are released on every return path, including failures, when they drop.
//!
//! Reconciliation (M3-04, D-029) takes the gate exclusively
//! ([`ActivePolicy::exclusive`], crate-private; the [`ExclusiveHold`] it returns
//! is the only proof a store accepts for its `reconcile` transaction), so it
//! never runs while an execution or a permit presentation holds the gate.
//!
//! The gate is process-local; it does not coordinate other processes using the
//! same ledger (the D-029 limitation).
//!
//! Fail closed: an absent, rejected or unverifiable active policy is never a
//! matching bundle. If an activation was interrupted (a poisoned gate), shared
//! holders see [`Policy::Absent`] until the next completed activation.

use std::sync::{PoisonError, RwLock, RwLockReadGuard, RwLockWriteGuard};

use writ_kernel::contracts::StateRevision;
use writ_kernel::decision::Policy;
use writ_kernel::policy::LoadedBundle;

use crate::authorize::{authorize, Authorized, TrustedInputs};
use crate::clock::TrustedClock;
use crate::ingress::VerifiedRequest;
use crate::refusal::Refusal;

/// The active authorization policy as the broker's owner resolved it.
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum PolicyState {
    /// No active bundle (dependency down, missing or unverifiable).
    Absent,
    /// A bundle was supplied and rejected at load.
    Rejected,
    /// A loaded, verified bundle.
    Loaded(Box<LoadedBundle>),
}

impl PolicyState {
    /// A loaded, verified bundle.
    pub fn loaded(bundle: LoadedBundle) -> PolicyState {
        PolicyState::Loaded(Box::new(bundle))
    }
}

/// Holder of the active authorization policy behind the coordination gate.
#[derive(Debug)]
pub struct ActivePolicy {
    gate: RwLock<PolicyState>,
}

impl ActivePolicy {
    /// A holder whose active policy is `state`.
    pub fn new(state: PolicyState) -> ActivePolicy {
        ActivePolicy {
            gate: RwLock::new(state),
        }
    }

    /// Take the gate shared. Blocks while an activation holds it exclusively.
    pub fn shared(&self) -> SharedPolicy<'_> {
        match self.gate.read() {
            Ok(guard) => SharedPolicy {
                guard,
                interrupted: false,
            },
            Err(poisoned) => SharedPolicy {
                guard: poisoned.into_inner(),
                interrupted: true,
            },
        }
    }

    /// Execution entry point that acquires the gate internally: take the gate
    /// shared, decide `verified` at use time under the active policy, the
    /// trusted `state_revision` and the time read once from `clock`, and run
    /// `effect` with the guard-bound [`Authorized`] while the hold lasts. The
    /// gate is released when `effect` returns, on every path. `effect` cannot
    /// return or store the capability: its result type does not depend on the
    /// hold. Must not be called while the caller already holds the gate (the
    /// gate is not re-entrant).
    ///
    /// ```compile_fail
    /// use writ_broker::clock::FixedClock;
    /// use writ_broker::ingress::VerifiedRequest;
    /// use writ_broker::policy::ActivePolicy;
    /// use writ_kernel::contracts::StateRevision;
    /// use writ_kernel::time::Timestamp;
    ///
    /// fn leak(active: &ActivePolicy, verified: &VerifiedRequest) {
    ///     let clock = FixedClock::new(Timestamp::parse_strict("2026-11-05T12:00:00Z").unwrap());
    ///     let revision = StateRevision::new(5).unwrap();
    ///     let _leaked = active.execute(verified, revision, &clock, |authorized| authorized);
    /// }
    /// ```
    ///
    /// Returning a value that does not borrow the hold compiles:
    ///
    /// ```
    /// use writ_broker::clock::FixedClock;
    /// use writ_broker::ingress::VerifiedRequest;
    /// use writ_broker::policy::ActivePolicy;
    /// use writ_kernel::contracts::StateRevision;
    /// use writ_kernel::time::Timestamp;
    ///
    /// fn status(active: &ActivePolicy, verified: &VerifiedRequest) {
    ///     let clock = FixedClock::new(Timestamp::parse_strict("2026-11-05T12:00:00Z").unwrap());
    ///     let revision = StateRevision::new(5).unwrap();
    ///     let _status = active.execute(verified, revision, &clock, |authorized| authorized.status());
    /// }
    /// ```
    pub fn execute<R>(
        &self,
        verified: &VerifiedRequest,
        state_revision: StateRevision,
        clock: &dyn TrustedClock,
        effect: impl for<'g> FnOnce(Authorized<'g>) -> R,
    ) -> Result<R, Refusal> {
        let shared = self.shared();
        let inputs = shared.trusted_inputs(state_revision);
        let authorized = authorize(verified, &inputs, clock)?;
        Ok(effect(authorized))
    }

    /// Take the gate exclusively for reconciliation (D-029): waits until every
    /// shared holder (execution, permit presentation) has returned and holds
    /// back new ones until the hold is dropped. The policy is unchanged.
    pub(crate) fn exclusive(&self) -> ExclusiveHold<'_> {
        ExclusiveHold {
            _guard: self.gate.write().unwrap_or_else(PoisonError::into_inner),
        }
    }

    /// Activate or replace the policy. Takes the gate exclusively: waits until
    /// every [`SharedPolicy`] has been released, and holds back new shared
    /// holders until the replacement is complete. Returns the previous policy.
    pub fn activate(&self, state: PolicyState) -> PolicyState {
        let mut guard = self.gate.write().unwrap_or_else(PoisonError::into_inner);
        let previous = std::mem::replace(&mut *guard, state);
        drop(guard);
        // The whole value was replaced, so an earlier interruption is repaired.
        self.gate.clear_poison();
        previous
    }
}

/// The gate held exclusively by reconciliation. Only the broker can take it
/// ([`ActivePolicy::exclusive`] is crate-private and the field is private), so
/// a store's `reconcile` transaction cannot be reached without it:
///
/// ```compile_fail
/// fn forge<'a>(g: std::sync::RwLockWriteGuard<'a, writ_broker::policy::PolicyState>) {
///     let _ = writ_broker::policy::ExclusiveHold { _guard: g };
/// }
/// ```
#[derive(Debug)]
pub struct ExclusiveHold<'a> {
    _guard: RwLockWriteGuard<'a, PolicyState>,
}

/// The gate held shared: one consistent view of the active policy for the
/// whole critical section. Released when dropped.
#[derive(Debug)]
pub struct SharedPolicy<'a> {
    guard: RwLockReadGuard<'a, PolicyState>,
    interrupted: bool,
}

impl SharedPolicy<'_> {
    /// The active policy in the kernel's form. [`Policy::Absent`] when no
    /// bundle is active or an activation was interrupted.
    pub fn policy(&self) -> Policy<'_> {
        if self.interrupted {
            return Policy::Absent;
        }
        match &*self.guard {
            PolicyState::Absent => Policy::Absent,
            PolicyState::Rejected => Policy::Rejected,
            PolicyState::Loaded(bundle) => Policy::Loaded(bundle),
        }
    }

    /// Trusted inputs of a use-time decision under this view of the policy:
    /// the only way to build [`TrustedInputs`]. They borrow the guard, so they
    /// (and any [`Authorized`] decided from them) cannot outlive the shared hold.
    pub fn trusted_inputs(&self, state_revision: StateRevision) -> TrustedInputs<'_> {
        TrustedInputs::new(self, state_revision)
    }

    /// Digest of the active bundle; `None` when no verified bundle is active.
    /// Crate-private: refusals never reveal the active digest.
    pub(crate) fn active_bundle_sha(&self) -> Option<&str> {
        match self.policy() {
            Policy::Loaded(bundle) => Some(bundle.sha.as_str()),
            Policy::Absent | Policy::Rejected => None,
        }
    }
}

#[cfg(test)]
#[allow(clippy::unwrap_used, clippy::expect_used, clippy::panic)]
mod tests {
    use super::*;
    use crate::authorize::authorize;
    use crate::clock::FixedClock;
    use crate::permit::{Permit, PermitId};
    use crate::refusal::Refusal;
    use crate::test_support::*;
    use std::sync::mpsc;
    use std::sync::{Arc, Barrier};
    use std::thread;
    use std::time::Duration;
    use writ_kernel::json;

    /// How long a blocked operation must stay blocked to count as waiting.
    const BLOCKED_FOR: Duration = Duration::from_millis(200);
    /// Upper bound for an operation that must complete.
    const COMPLETES_WITHIN: Duration = Duration::from_secs(10);

    fn other_bundle() -> LoadedBundle {
        load(&with(
            &reference_bundle_value(),
            &["version"],
            json::s("0.9.0"),
        ))
    }

    fn sha_of(shared: &SharedPolicy<'_>) -> Option<String> {
        shared.active_bundle_sha().map(str::to_string)
    }

    #[test]
    fn shared_view_maps_every_state_to_the_kernel_policy() {
        let bundle = reference_bundle();
        let active = ActivePolicy::new(PolicyState::Absent);
        assert!(matches!(active.shared().policy(), Policy::Absent));
        assert_eq!(active.shared().active_bundle_sha(), None);
        assert_eq!(
            active.activate(PolicyState::Rejected),
            PolicyState::Absent,
            "activation returns the previous policy"
        );
        assert!(matches!(active.shared().policy(), Policy::Rejected));
        assert_eq!(active.shared().active_bundle_sha(), None);
        active.activate(PolicyState::loaded(bundle.clone()));
        let shared = active.shared();
        assert!(matches!(shared.policy(), Policy::Loaded(b) if *b == bundle));
        assert_eq!(shared.active_bundle_sha(), Some(bundle.sha.as_str()));
        let inputs = shared.trusted_inputs(revision(5));
        assert!(matches!(inputs.policy(), Policy::Loaded(b) if *b == bundle));
        assert_eq!(inputs.state_revision(), revision(5));
    }

    #[test]
    fn independent_shared_holders_are_concurrent() {
        let active = ActivePolicy::new(PolicyState::loaded(reference_bundle()));
        let first = active.shared();
        let (tx, rx) = mpsc::channel();
        thread::scope(|s| {
            s.spawn(|| {
                let second = active.shared();
                tx.send(sha_of(&second)).unwrap();
            });
            assert_eq!(
                rx.recv_timeout(COMPLETES_WITHIN).unwrap(),
                sha_of(&first),
                "a second shared holder does not wait for the first"
            );
        });
    }

    #[test]
    fn activation_waits_while_a_shared_holder_is_active() {
        let old = reference_bundle();
        let new = other_bundle();
        let active = ActivePolicy::new(PolicyState::loaded(old.clone()));
        let holder = active.shared();
        let (done_tx, done_rx) = mpsc::channel();
        thread::scope(|s| {
            s.spawn(|| {
                active.activate(PolicyState::loaded(new.clone()));
                done_tx.send(()).unwrap();
            });
            assert_eq!(
                done_rx.recv_timeout(BLOCKED_FOR),
                Err(mpsc::RecvTimeoutError::Timeout),
                "activation must wait for the shared holder"
            );
            assert_eq!(sha_of(&holder), Some(old.sha.as_str().to_string()));
            drop(holder);
            done_rx
                .recv_timeout(COMPLETES_WITHIN)
                .expect("activation completes once the holder releases the gate");
        });
        assert_eq!(sha_of(&active.shared()), Some(new.sha.as_str().to_string()));
    }

    #[test]
    fn a_holder_sees_one_consistent_policy_for_its_whole_critical_section() {
        let old = reference_bundle();
        let new = other_bundle();
        let active = Arc::new(ActivePolicy::new(PolicyState::loaded(old.clone())));
        let start = Arc::new(Barrier::new(2));
        let (done_tx, done_rx) = mpsc::channel();
        let activator = {
            let active = Arc::clone(&active);
            let start = Arc::clone(&start);
            let new = new.clone();
            thread::spawn(move || {
                start.wait();
                active.activate(PolicyState::loaded(new));
                done_tx.send(()).unwrap();
            })
        };
        {
            let holder = active.shared();
            let first = sha_of(&holder);
            start.wait();
            // The activation is attempted while the critical section runs; every
            // read inside it sees the same policy.
            for _ in 0..20 {
                thread::sleep(Duration::from_millis(10));
                assert_eq!(sha_of(&holder), first);
            }
            assert_eq!(
                done_rx.try_recv(),
                Err(mpsc::TryRecvError::Empty),
                "no activation completed inside the critical section"
            );
            assert_eq!(first, Some(old.sha.as_str().to_string()));
        }
        done_rx.recv_timeout(COMPLETES_WITHIN).unwrap();
        activator.join().unwrap();
        assert_eq!(sha_of(&active.shared()), Some(new.sha.as_str().to_string()));
    }

    #[test]
    fn after_activation_a_permit_issued_under_the_old_bundle_is_stale() {
        let old = reference_bundle();
        let active = ActivePolicy::new(PolicyState::loaded(old.clone()));
        let req = base_request();
        let clock = FixedClock::new(now());
        let key = permit_key(PERMIT_KEY);
        let permit = {
            let shared = active.shared();
            let authorized =
                authorize(&verify(&req), &shared.trusted_inputs(revision(5)), &clock).unwrap();
            let permit = Permit::issue(
                &PermitId::new("permit-1").unwrap(),
                &authorized,
                &clock,
                &key,
            )
            .unwrap();
            assert!(permit.verify(&req, &key, &clock, &shared).is_ok());
            permit
        };
        assert_eq!(permit.policy_bundle_sha(), old.sha.as_str());
        thread::scope(|s| {
            s.spawn(|| active.activate(PolicyState::loaded(other_bundle())))
                .join()
                .unwrap();
        });
        let refusal = permit
            .verify(&req, &key, &clock, &active.shared())
            .unwrap_err();
        assert_eq!(refusal, Refusal::PermitStale);
        assert_eq!(refusal.external_code(), "PROMOTION_REFUSED");
    }

    #[test]
    fn execute_runs_the_effect_under_one_hold_and_refuses_without_running_it() {
        let bundle = reference_bundle();
        let active = ActivePolicy::new(PolicyState::loaded(bundle.clone()));
        let req = base_request();
        let clock = FixedClock::new(now());
        let key = permit_key(PERMIT_KEY);
        // Allowed: the effect gets the capability and, through its hold, can
        // verify the permit it issued under the same view of the policy.
        let verified = active
            .execute(&verify(&req), revision(5), &clock, |authorized| {
                assert_eq!(authorized.status(), crate::authorize::UseTimeStatus::Allow);
                let permit = Permit::issue(
                    &PermitId::new("permit-1").unwrap(),
                    &authorized,
                    &clock,
                    &key,
                )
                .unwrap();
                permit.verify(&req, &key, &clock, authorized.hold())
            })
            .unwrap()
            .unwrap();
        assert_eq!(verified.policy_bundle_sha, bundle.sha.as_str());
        // Refused: the effect never runs and the gate is released.
        let mut ran = false;
        for state in [PolicyState::Absent, PolicyState::Rejected] {
            active.activate(state);
            let refusal = active
                .execute(&verify(&req), revision(5), &clock, |_| ran = true)
                .unwrap_err();
            assert!(matches!(refusal, Refusal::Decision { .. }));
        }
        assert!(!ran, "a refused decision never reaches the effect");
        let secrets = crate::test_support::with(
            &req,
            &["action", "resource", "canonical_resource_id"],
            json::s("workspace:fixture-tenant/fixture-project/secrets/k"),
        );
        active.activate(PolicyState::loaded(bundle));
        assert_eq!(
            active
                .execute(&verify(&secrets), revision(5), &clock, |_| ())
                .unwrap_err()
                .code(),
            "SECRETS_PATH_FORBIDDEN"
        );
    }

    #[test]
    fn execute_holds_the_gate_for_the_whole_effect() {
        let old = reference_bundle();
        let new = other_bundle();
        let active = ActivePolicy::new(PolicyState::loaded(old.clone()));
        let req = base_request();
        let clock = FixedClock::new(now());
        let (entered_tx, entered_rx) = mpsc::channel();
        let done = std::sync::atomic::AtomicBool::new(false);
        thread::scope(|s| {
            s.spawn(|| {
                active
                    .execute(&verify(&req), revision(5), &clock, |authorized| {
                        entered_tx.send(()).unwrap();
                        // An activation attempted now must wait for this effect.
                        thread::sleep(BLOCKED_FOR);
                        assert!(
                            !done.load(std::sync::atomic::Ordering::SeqCst),
                            "no activation completed inside the effect"
                        );
                        assert_eq!(
                            authorized.hold().active_bundle_sha(),
                            Some(old.sha.as_str())
                        );
                    })
                    .unwrap();
            });
            entered_rx.recv_timeout(COMPLETES_WITHIN).unwrap();
            s.spawn(|| {
                active.activate(PolicyState::loaded(new.clone()));
                done.store(true, std::sync::atomic::Ordering::SeqCst);
            });
        });
        assert!(done.load(std::sync::atomic::Ordering::SeqCst));
        // Both finished: the gate was released when the effect returned.
        assert_eq!(sha_of(&active.shared()), Some(new.sha.as_str().to_string()));
    }

    #[test]
    fn an_interrupted_activation_fails_closed_until_the_next_activation() {
        let bundle = reference_bundle();
        let active = ActivePolicy::new(PolicyState::loaded(bundle.clone()));
        // Poison the gate: a writer that panics while holding it exclusively.
        let poisoned = thread::scope(|s| {
            s.spawn(|| {
                let _w = active.gate.write().unwrap();
                panic!("interrupted activation");
            })
            .join()
        });
        assert!(poisoned.is_err());
        assert!(matches!(active.shared().policy(), Policy::Absent));
        assert_eq!(active.shared().active_bundle_sha(), None);
        active.activate(PolicyState::loaded(bundle.clone()));
        assert_eq!(
            active.shared().active_bundle_sha(),
            Some(bundle.sha.as_str())
        );
    }
}
