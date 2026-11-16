//! Peer admission (J-2): decided from `SO_PEERCRED` alone, before any byte is read.

use std::collections::BTreeMap;

/// The overflow uid and gid (`/proc/sys/kernel/overflowuid`): what a peer in another user
/// namespace with an unmapped id appears as. Always refused.
pub const OVERFLOW_ID: u32 = 65534;

/// Peer credentials of a connected Unix socket (`SO_PEERCRED`).
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct PeerCred {
    /// Effective uid of the peer at `connect`.
    pub uid: u32,
    /// Effective gid of the peer at `connect`.
    pub gid: u32,
    /// Pid of the peer at `connect` (audit only, never authorization).
    pub pid: i32,
}

/// Which socket a table admits for.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Endpoint {
    /// `/run/writ/host.sock`: agent uids; uid 0 is never admitted.
    Host,
    /// `/run/writ/admin.sock`: the configured `admin_uids` (default `[0]`, J-11.8).
    Admin,
}

/// Why a peer was refused (the connection is closed without reading).
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum AdmissionRefused {
    /// uid or gid 65534.
    Overflow,
    /// uid 0 on `host.sock`.
    Root,
    /// The uid is not configured for this socket.
    Unlisted,
}

/// Why a table was refused at configuration load.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum TableError {
    /// A uid listed twice (each (socket, uid) has exactly one binding).
    Duplicate(u32),
    /// uid 65534 listed.
    Overflow,
    /// uid 0 listed for `host.sock`.
    Root,
    /// No uid listed.
    Empty,
}

/// The admission table of one socket: each admitted uid with exactly one binding `T`
/// (for `host.sock`: tenant, project and principal configuration; J-2, J-6).
#[derive(Clone, Debug)]
pub struct AdmissionTable<T> {
    endpoint: Endpoint,
    entries: BTreeMap<u32, T>,
}

impl<T> AdmissionTable<T> {
    /// A table for `endpoint` with `entries`, validated as in [`TableError`].
    pub fn new(
        endpoint: Endpoint,
        entries: Vec<(u32, T)>,
    ) -> Result<AdmissionTable<T>, TableError> {
        if entries.is_empty() {
            return Err(TableError::Empty);
        }
        let mut map = BTreeMap::new();
        for (uid, binding) in entries {
            if uid == OVERFLOW_ID {
                return Err(TableError::Overflow);
            }
            if uid == 0 && endpoint == Endpoint::Host {
                return Err(TableError::Root);
            }
            if map.insert(uid, binding).is_some() {
                return Err(TableError::Duplicate(uid));
            }
        }
        Ok(AdmissionTable {
            endpoint,
            entries: map,
        })
    }

    /// The endpoint this table admits for.
    pub fn endpoint(&self) -> Endpoint {
        self.endpoint
    }

    /// The admitted uids.
    pub fn uids(&self) -> impl Iterator<Item = u32> + '_ {
        self.entries.keys().copied()
    }

    /// Admit `peer`: overflow ids first, then uid 0 on `host.sock`, then the table. The
    /// binding returned is the only scope the connection ever has.
    pub fn admit(&self, peer: &PeerCred) -> Result<&T, AdmissionRefused> {
        if peer.uid == OVERFLOW_ID || peer.gid == OVERFLOW_ID {
            return Err(AdmissionRefused::Overflow);
        }
        if peer.uid == 0 && self.endpoint == Endpoint::Host {
            return Err(AdmissionRefused::Root);
        }
        self.entries
            .get(&peer.uid)
            .ok_or(AdmissionRefused::Unlisted)
    }
}

/// The OS principal an admission proves (M5-D7): `"uid:<n>"`.
pub fn os_principal(uid: u32) -> String {
    format!("uid:{uid}")
}

/// Client side (J-2 "clients verify the server"): send nothing unless the server peer is
/// the configured service uid (and not an overflow id).
pub fn verify_server(peer: &PeerCred, service_uid: u32) -> bool {
    peer.uid == service_uid && peer.uid != OVERFLOW_ID && peer.gid != OVERFLOW_ID
}

#[cfg(test)]
#[allow(clippy::unwrap_used, clippy::expect_used, clippy::panic)]
mod tests {
    use super::*;

    fn peer(uid: u32, gid: u32) -> PeerCred {
        PeerCred { uid, gid, pid: 77 }
    }

    #[test]
    fn host_admits_only_listed_uids_with_their_single_binding() {
        let t = AdmissionTable::new(Endpoint::Host, vec![(1001, "acme/web"), (1002, "acme/api")])
            .unwrap();
        assert_eq!(t.admit(&peer(1001, 1001)), Ok(&"acme/web"));
        assert_eq!(t.admit(&peer(1002, 50)), Ok(&"acme/api"));
        assert_eq!(t.admit(&peer(1003, 1003)), Err(AdmissionRefused::Unlisted));
        assert_eq!(t.admit(&peer(0, 0)), Err(AdmissionRefused::Root));
        assert_eq!(
            t.admit(&peer(OVERFLOW_ID, 1001)),
            Err(AdmissionRefused::Overflow)
        );
        assert_eq!(
            t.admit(&peer(1001, OVERFLOW_ID)),
            Err(AdmissionRefused::Overflow)
        );
    }

    #[test]
    fn admin_admits_root_only_when_listed() {
        let t = AdmissionTable::new(Endpoint::Admin, vec![(0, ())]).unwrap();
        assert_eq!(t.admit(&peer(0, 0)), Ok(&()));
        assert_eq!(t.admit(&peer(1001, 1001)), Err(AdmissionRefused::Unlisted));
        assert_eq!(
            t.admit(&peer(0, OVERFLOW_ID)),
            Err(AdmissionRefused::Overflow)
        );
    }

    #[test]
    fn tables_refuse_root_on_host_overflow_duplicates_and_empty() {
        assert_eq!(
            AdmissionTable::new(Endpoint::Host, vec![(0, ())]).err(),
            Some(TableError::Root)
        );
        assert_eq!(
            AdmissionTable::new(Endpoint::Admin, vec![(OVERFLOW_ID, ())]).err(),
            Some(TableError::Overflow)
        );
        assert_eq!(
            AdmissionTable::new(Endpoint::Host, vec![(5, ()), (5, ())]).err(),
            Some(TableError::Duplicate(5))
        );
        assert_eq!(
            AdmissionTable::<()>::new(Endpoint::Admin, vec![]).err(),
            Some(TableError::Empty)
        );
    }

    #[test]
    fn principal_text_and_server_verification() {
        assert_eq!(os_principal(1001), "uid:1001");
        assert!(verify_server(&peer(990, 990), 990));
        assert!(!verify_server(&peer(991, 990), 990));
        assert!(!verify_server(&peer(990, OVERFLOW_ID), 990));
        assert!(!verify_server(&peer(OVERFLOW_ID, 1), OVERFLOW_ID));
    }
}
