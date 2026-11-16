//! Writ-internal local RPC over Unix sockets (M5a-04; D-045 M5-D3, M5-D7;
//! [M5-INTERFACES](../../docs/implementation/M5-INTERFACES.md) J-2). Not MCP.
//!
//! This crate holds the transport rules that do not depend on the operating system, so they
//! can be tested exactly; the service (`writ-serviced`) obtains peer credentials and reads
//! bytes from the socket (through `writ-hostsys`, which refuses ancillary data) and applies
//! these rules:
//!
//! - [`frame`]: 4-byte big-endian length, 1-byte frame type (`0x01` control, `0x02` ingest
//!   data), payload. The type and the length are checked against the endpoint's
//!   [`frame::FrameLimits`] **before** any payload buffer is allocated (control at most
//!   64 KiB, data at most 1 MiB). A control payload is one strict UTF-8 JSON object parsed by
//!   the kernel parser (INV-41). Any violation is a [`frame::FrameError`]: the connection is
//!   answered with the generic error and closed (INV-18).
//! - [`admission`]: the admission decision from peer credentials only, made before a single
//!   byte is read. Refused: an unlisted uid; uid or gid 65534 (the overflow id of a peer in
//!   another user namespace); uid 0 on `host.sock`. Each admitted (socket, uid) has exactly
//!   one configured binding (tenant, project and the principal configuration); no request
//!   field selects or changes it. The pid is recorded for audit only.
//! - [`caps`]: a global and a per-uid cap on open connections; a connection over a cap is
//!   accepted and closed at once without reading.
//!
//! **What admission proves:** the OS principal of the connecting process only (M5-D7),
//! recorded as `os_principal: "uid:<n>"`, `verified_by: "so_peercred"`. Whoever holds the
//! connected descriptor acts with that uid's authority for the connection's lifetime: a
//! client must never pass a connected descriptor to a less trusted process.

pub mod admission;
pub mod caps;
pub mod frame;

pub use admission::{
    os_principal, verify_server, AdmissionRefused, AdmissionTable, Endpoint, PeerCred,
};
pub use caps::{CapSlot, ConnectionCaps};
pub use frame::{read_frame, write_frame, Frame, FrameError, FrameLimits, FrameType};

/// The generic error answered before closing on any transport violation (INV-18).
pub const GENERIC_ERROR: &str = "REQUEST_REFUSED";
