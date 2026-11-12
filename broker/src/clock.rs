//! Trusted time (risk 14, D-017, D-018 A5).
//!
//! The broker reads time only through [`TrustedClock`]; the agent never
//! supplies authoritative time. This crate provides only [`FixedClock`], an
//! injected clock for tests and harnesses. A production clock source, clock
//! persistence and rollback protection are an open owner design decision and
//! are deliberately not implemented here.

use writ_kernel::time::Timestamp;

/// The broker's only source of time.
pub trait TrustedClock {
    /// The current trusted time.
    fn now(&self) -> Timestamp;
}

/// An injected clock that returns exactly the time it was given.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct FixedClock {
    now: Timestamp,
}

impl FixedClock {
    /// A clock fixed at `now`.
    pub fn new(now: Timestamp) -> FixedClock {
        FixedClock { now }
    }

    /// Set the time.
    pub fn set(&mut self, now: Timestamp) {
        self.now = now;
    }

    /// Move the time by `seconds` (negative values move it back; saturating).
    pub fn advance(&mut self, seconds: i64) {
        self.now = self.now.plus_seconds(seconds);
    }
}

impl TrustedClock for FixedClock {
    fn now(&self) -> Timestamp {
        self.now
    }
}

#[cfg(test)]
#[allow(clippy::unwrap_used, clippy::expect_used, clippy::panic)]
mod tests {
    use super::*;

    #[test]
    fn fixed_clock_returns_the_injected_time() {
        let t = Timestamp::parse_strict("2026-11-05T12:00:00Z").unwrap();
        let mut clock = FixedClock::new(t);
        assert_eq!(clock.now(), t);
        assert_eq!(clock.now(), t, "reading does not move time");
        clock.advance(31);
        assert_eq!(clock.now().to_string(), "2026-11-05T12:00:31Z");
        clock.set(t);
        assert_eq!(clock.now(), t);
    }
}
