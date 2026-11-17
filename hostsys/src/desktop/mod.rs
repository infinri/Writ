//! Fixture-only observation and evaluation contract for J-1 and J-9.

mod evaluate;
mod fixtures;

use std::collections::BTreeSet;

#[derive(Clone, Debug)]
struct Account {
    uid: u32,
    primary: u32,
    supplementary: BTreeSet<u32>,
    primary_members: BTreeSet<u32>,
}

#[derive(Clone, Debug)]
struct Scope {
    configured_service_uid: u32,
    configured_job_uid: u32,
    configured_agent_uids: BTreeSet<u32>,
    configured_desktop_uids: BTreeSet<u32>,
    service: Account,
    job: Account,
    agents: Vec<Account>,
    desktops: Vec<Account>,
    service_group: u32,
    job_group: u32,
    agent_group: u32,
    // Complete effective membership, including accounts with these primary groups.
    agent_members: BTreeSet<u32>,
    service_listed_members: BTreeSet<u32>,
    job_listed_members: BTreeSet<u32>,
    identities_complete: bool,
}

#[derive(Clone, Copy, Debug, Eq, Ord, PartialEq, PartialOrd)]
enum Check {
    HomeRead,
    HomeList,
    HomeWrite,
    BrowserRead,
    BrowserList,
    BrowserWrite,
    BrowserStateContained,
    XAuthorityRead,
    XhostPolicy,
    X11Unix,
    X11Abstract,
    Xwayland,
    Wayland,
    RuntimeTraverse,
    RuntimeMode,
    SessionBus,
    Portal,
    PipeWire,
    PulseAudio,
    DesktopKeyring,
    KernelKeyring,
    SshAgent,
    GpgAgent,
    FidoOpen,
    PlatformPasskey,
    Polkit,
    Trace,
    Signal,
    Environ,
    Cmdline,
    LogindSeparation,
    SeatInactive,
    TerminalSeparation,
    LegacyTiocsti,
    PtraceScope,
    TcpListeners,
    UnixSockets,
    AbstractSockets,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
enum Finding {
    AccessDenied,
    AccessGranted,
    TransportDenied,
    Connected,
    ProtocolDenied,
    ProtocolGranted,
    PolicyDeniedWithoutChallenge,
    Challenge,
    Verified,
    Violated,
    CmdlineResidual,
    Unavailable,
}

#[derive(Clone, Debug)]
struct Inventory {
    check: Check,
    // Independent, complete enumerations on either side of the collection window.
    // Target IDs are opaque within one check.
    targets_before: Option<Vec<String>>,
    targets_after: Option<Vec<String>>,
    observed: Vec<(String, Finding)>,
}

#[derive(Clone, Copy, Debug, Eq, Ord, PartialEq, PartialOrd)]
enum Control {
    ReadableDecoy,
    DesktopGroup,
    DesktopSocket,
    XhostGrant,
    PolkitChallenge,
    TcpListener,
    AbstractSocket,
    ExtraGroup,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
enum ControlFinding {
    HomeCanaryRead,
    DesktopGroupAndHomeCanaryRead,
    DesktopSocketConnected,
    TcpListenerConnected,
    AbstractSocketConnected,
    X11AccessGranted,
    PolkitChallenge,
    ExtraGroupObserved,
    Unavailable,
}

#[derive(Clone, Debug)]
struct Pair {
    agent_uid: u32,
    desktop_uid: u32,
    observations: Vec<Inventory>,
    controls: Vec<(Control, ControlFinding)>,
}

#[derive(Clone, Debug, Eq, PartialEq)]
enum Evaluation {
    Pass,
    Missing,
    Failed,
}
