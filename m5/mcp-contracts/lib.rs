//! Candidate agent-side lifecycle gate. The MCP SDK owns wire parsing and negotiation.
#![forbid(unsafe_code)]

pub const PINNED_PROTOCOL: &str = "2025-11-25";

#[derive(Debug, Default, PartialEq, Eq)]
enum Phase {
    #[default]
    Fresh,
    AwaitingInitialized,
    Ready,
    Closed,
}

#[derive(Debug, PartialEq, Eq)]
pub struct Refused;

#[derive(Debug, Default)]
pub struct Lifecycle {
    phase: Phase,
}

impl Lifecycle {
    /// Called after the SDK accepts initialize and selects the pinned protocol.
    pub fn negotiated(&mut self, selected: &str) -> Result<(), Refused> {
        if self.phase != Phase::Fresh || selected != PINNED_PROTOCOL {
            self.close();
            return Err(Refused);
        }
        self.phase = Phase::AwaitingInitialized;
        Ok(())
    }

    /// Called only by the SDK's typed initialized-notification handler.
    pub fn initialized(&mut self) -> Result<(), Refused> {
        if self.phase != Phase::AwaitingInitialized {
            self.close();
            return Err(Refused);
        }
        self.phase = Phase::Ready;
        Ok(())
    }

    /// This result permits protocol dispatch only. The broker authorizes effects.
    pub fn require_ready(&self) -> Result<(), Refused> {
        if self.phase == Phase::Ready {
            Ok(())
        } else {
            Err(Refused)
        }
    }

    pub fn close(&mut self) {
        self.phase = Phase::Closed;
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn stateless_and_preinitialized_requests_cannot_dispatch() {
        let mut lifecycle = Lifecycle::default();
        assert_eq!(lifecycle.require_ready(), Err(Refused));
        lifecycle.negotiated(PINNED_PROTOCOL).unwrap();
        assert_eq!(lifecycle.require_ready(), Err(Refused));
        lifecycle.initialized().unwrap();
        assert_eq!(lifecycle.require_ready(), Ok(()));
    }

    #[test]
    fn wrong_version_and_out_of_order_notification_close_the_session() {
        for version in ["2024-11-05", "2026-07-28", ""] {
            let mut lifecycle = Lifecycle::default();
            assert_eq!(lifecycle.negotiated(version), Err(Refused));
            assert_eq!(lifecycle.negotiated(PINNED_PROTOCOL), Err(Refused));
            assert_eq!(lifecycle.require_ready(), Err(Refused));
        }
        let mut lifecycle = Lifecycle::default();
        assert_eq!(lifecycle.initialized(), Err(Refused));
        assert_eq!(lifecycle.negotiated(PINNED_PROTOCOL), Err(Refused));
    }

    #[test]
    fn duplicate_initialize_or_notification_cannot_reopen_a_session() {
        let mut lifecycle = Lifecycle::default();
        lifecycle.negotiated(PINNED_PROTOCOL).unwrap();
        assert_eq!(lifecycle.negotiated(PINNED_PROTOCOL), Err(Refused));
        assert_eq!(lifecycle.initialized(), Err(Refused));

        let mut lifecycle = Lifecycle::default();
        lifecycle.negotiated(PINNED_PROTOCOL).unwrap();
        lifecycle.initialized().unwrap();
        assert_eq!(lifecycle.initialized(), Err(Refused));
        assert_eq!(lifecycle.require_ready(), Err(Refused));
    }

    #[test]
    fn closure_and_new_connections_have_no_inherited_readiness() {
        let mut first = Lifecycle::default();
        first.negotiated(PINNED_PROTOCOL).unwrap();
        first.initialized().unwrap();
        let second = Lifecycle::default();
        assert_eq!(second.require_ready(), Err(Refused));
        first.close();
        assert_eq!(first.require_ready(), Err(Refused));
    }
}
