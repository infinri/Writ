use super::{Check, Control, ControlFinding, Evaluation, Finding, Inventory, Pair, Scope};
use std::collections::{BTreeMap, BTreeSet};

pub(super) const CHECKS: [Check; 38] = [
    Check::HomeRead,
    Check::HomeList,
    Check::HomeWrite,
    Check::BrowserRead,
    Check::BrowserList,
    Check::BrowserWrite,
    Check::BrowserStateContained,
    Check::XAuthorityRead,
    Check::XhostPolicy,
    Check::X11Unix,
    Check::X11Abstract,
    Check::Xwayland,
    Check::Wayland,
    Check::RuntimeTraverse,
    Check::RuntimeMode,
    Check::SessionBus,
    Check::Portal,
    Check::PipeWire,
    Check::PulseAudio,
    Check::DesktopKeyring,
    Check::KernelKeyring,
    Check::SshAgent,
    Check::GpgAgent,
    Check::FidoOpen,
    Check::PlatformPasskey,
    Check::Polkit,
    Check::Trace,
    Check::Signal,
    Check::Environ,
    Check::Cmdline,
    Check::LogindSeparation,
    Check::SeatInactive,
    Check::TerminalSeparation,
    Check::LegacyTiocsti,
    Check::PtraceScope,
    Check::TcpListeners,
    Check::UnixSockets,
    Check::AbstractSockets,
];

pub(super) const CONTROLS: [Control; 8] = [
    Control::ReadableDecoy,
    Control::DesktopGroup,
    Control::DesktopSocket,
    Control::XhostGrant,
    Control::PolkitChallenge,
    Control::TcpListener,
    Control::AbstractSocket,
    Control::ExtraGroup,
];

pub(super) fn evaluate(scope: &Scope, pairs: &[Pair]) -> Evaluation {
    evaluate_all(scope, pairs).map_or_else(|result| result, |()| Evaluation::Pass)
}

fn evaluate_all(scope: &Scope, pairs: &[Pair]) -> Result<(), Evaluation> {
    use Evaluation::{Failed, Missing};
    evaluate_identities(scope)?;
    let agents = &scope.configured_agent_uids;
    let desktops = &scope.configured_desktop_uids;

    let mut observed_pairs = BTreeSet::new();
    for Pair {
        agent_uid,
        desktop_uid,
        observations,
        controls,
    } in pairs
    {
        if !agents.contains(agent_uid)
            || !desktops.contains(desktop_uid)
            || !observed_pairs.insert((*agent_uid, *desktop_uid))
        {
            return Err(Failed);
        }

        let mut checks = BTreeSet::new();
        for inventory in observations {
            if !checks.insert(inventory.check) {
                return Err(Failed);
            }
            evaluate_inventory(inventory)?;
        }

        let mut control_ids = BTreeSet::new();
        for &(control, finding) in controls {
            if !control_ids.insert(control) {
                return Err(Failed);
            }
            if finding == ControlFinding::Unavailable {
                return Err(Missing);
            }
            if finding != required_control_finding(control) {
                return Err(Failed);
            }
        }

        if !CHECKS.iter().all(|check| checks.contains(check))
            || !CONTROLS.iter().all(|control| control_ids.contains(control))
        {
            return Err(Missing);
        }
    }

    if agents.iter().any(|&agent| {
        desktops
            .iter()
            .any(|&desktop| !observed_pairs.contains(&(agent, desktop)))
    }) {
        return Err(Missing);
    }

    Ok(())
}

fn evaluate_identities(scope: &Scope) -> Result<(), Evaluation> {
    use Evaluation::{Failed, Missing};
    if !scope.identities_complete
        || scope.configured_agent_uids.is_empty()
        || scope.configured_desktop_uids.is_empty()
    {
        return Err(Missing);
    }

    let mut uids = BTreeSet::from([0, u32::MAX]);
    if [scope.configured_service_uid, scope.configured_job_uid]
        .into_iter()
        .chain(scope.configured_agent_uids.iter().copied())
        .chain(scope.configured_desktop_uids.iter().copied())
        .any(|uid| !uids.insert(uid))
    {
        return Err(Failed);
    }

    let mut reserved_groups = BTreeSet::from([0, u32::MAX]);
    if [scope.service_group, scope.job_group, scope.agent_group]
        .into_iter()
        .any(|gid| !reserved_groups.insert(gid))
    {
        return Err(Failed);
    }
    for (account, uid, gid) in [
        (
            &scope.service,
            scope.configured_service_uid,
            scope.service_group,
        ),
        (&scope.job, scope.configured_job_uid, scope.job_group),
    ] {
        if account.uid != uid
            || account.primary != gid
            || !account.supplementary.is_empty()
            || account.primary_members != BTreeSet::from([uid])
        {
            return Err(Failed);
        }
    }

    if !scope.service_listed_members.is_empty() || !scope.job_listed_members.is_empty() {
        return Err(Failed);
    }

    let mut agents = BTreeSet::new();
    for agent in &scope.agents {
        if !scope.configured_agent_uids.contains(&agent.uid)
            || !agents.insert(agent.uid)
            || !reserved_groups.insert(agent.primary)
            || agent.supplementary != BTreeSet::from([scope.agent_group])
            || agent.primary_members != BTreeSet::from([agent.uid])
        {
            return Err(Failed);
        }
    }
    if agents != scope.configured_agent_uids {
        return Err(Missing);
    }
    if scope.agent_members != agents {
        return Err(Failed);
    }

    let mut desktops = BTreeMap::new();
    let mut desktop_groups = BTreeMap::new();
    for desktop in &scope.desktops {
        if !scope.configured_desktop_uids.contains(&desktop.uid)
            || desktops.insert(desktop.uid, desktop.primary).is_some()
            || reserved_groups.contains(&desktop.primary)
            || !desktop.supplementary.is_empty()
            || !desktop.primary_members.contains(&desktop.uid)
        {
            return Err(Failed);
        }

        if desktop_groups
            .insert(desktop.primary, &desktop.primary_members)
            .is_some_and(|previous| previous != &desktop.primary_members)
        {
            return Err(Failed);
        }
    }
    if desktops.len() != scope.configured_desktop_uids.len() {
        return Err(Missing);
    }
    for (gid, members) in desktop_groups {
        if members
            .iter()
            .any(|uid| uids.contains(uid) && desktops.get(uid) != Some(&gid))
        {
            return Err(Failed);
        }
    }

    Ok(())
}

fn evaluate_inventory(inventory: &Inventory) -> Result<(), Evaluation> {
    use Evaluation::{Failed, Missing};
    let mut expected = target_set(inventory.targets_before.as_deref())?;
    if expected.is_empty() || expected != target_set(inventory.targets_after.as_deref())? {
        return Err(Missing);
    }
    let mut unavailable = false;
    for (target, finding) in &inventory.observed {
        if !expected.remove(target.as_str()) {
            return Err(Failed);
        }

        if *finding == Finding::Unavailable {
            unavailable = true;
        } else if !meets_check(inventory.check, *finding) {
            return Err(Failed);
        }
    }
    if !expected.is_empty() || unavailable {
        return Err(Missing);
    }
    Ok(())
}

fn target_set(targets: Option<&[String]>) -> Result<BTreeSet<&str>, Evaluation> {
    let mut expected = BTreeSet::new();
    for target in targets.ok_or(Evaluation::Missing)? {
        if target.trim().is_empty()
            || target.chars().any(char::is_control)
            || !expected.insert(target.as_str())
        {
            return Err(Evaluation::Failed);
        }
    }
    Ok(expected)
}

fn meets_check(check: Check, finding: Finding) -> bool {
    use Check::*;
    match check {
        HomeRead | HomeList | HomeWrite | BrowserRead | BrowserList | BrowserWrite
        | XAuthorityRead | RuntimeTraverse | KernelKeyring | FidoOpen | PlatformPasskey | Trace
        | Signal | Environ => finding == Finding::AccessDenied,
        X11Unix | X11Abstract | Xwayland => finding == Finding::ProtocolDenied,
        Wayland | SessionBus | Portal | PipeWire | PulseAudio | DesktopKeyring | SshAgent
        | GpgAgent | TcpListeners | UnixSockets | AbstractSockets => {
            matches!(finding, Finding::TransportDenied | Finding::ProtocolDenied)
        }
        BrowserStateContained
        | XhostPolicy
        | RuntimeMode
        | LogindSeparation
        | SeatInactive
        | TerminalSeparation
        | LegacyTiocsti
        | PtraceScope => finding == Finding::Verified,
        Polkit => finding == Finding::PolicyDeniedWithoutChallenge,
        Cmdline => matches!(finding, Finding::AccessDenied | Finding::CmdlineResidual),
    }
}

fn required_control_finding(control: Control) -> ControlFinding {
    match control {
        Control::ReadableDecoy => ControlFinding::HomeCanaryRead,
        Control::DesktopGroup => ControlFinding::DesktopGroupAndHomeCanaryRead,
        Control::DesktopSocket => ControlFinding::DesktopSocketConnected,
        Control::XhostGrant => ControlFinding::X11AccessGranted,
        Control::PolkitChallenge => ControlFinding::PolkitChallenge,
        Control::TcpListener => ControlFinding::TcpListenerConnected,
        Control::AbstractSocket => ControlFinding::AbstractSocketConnected,
        Control::ExtraGroup => ControlFinding::ExtraGroupObserved,
    }
}
