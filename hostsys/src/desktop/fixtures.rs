use super::evaluate::{evaluate, CHECKS, CONTROLS};
use super::{Account, Check, Control, ControlFinding, Evaluation, Finding, Inventory, Pair, Scope};
use std::collections::BTreeSet;

const OBSERVATIONS: &[(Check, Finding, &[&str])] = &[
    (Check::HomeRead, Finding::AccessDenied, &["home"]),
    (Check::HomeList, Finding::AccessDenied, &["home"]),
    (Check::HomeWrite, Finding::AccessDenied, &["home"]),
    (
        Check::BrowserRead,
        Finding::AccessDenied,
        &["firefox", "chromium", "chrome", "snap-browser"],
    ),
    (
        Check::BrowserList,
        Finding::AccessDenied,
        &["firefox", "chromium", "chrome", "snap-browser"],
    ),
    (
        Check::BrowserWrite,
        Finding::AccessDenied,
        &["firefox", "chromium", "chrome", "snap-browser"],
    ),
    (
        Check::BrowserStateContained,
        Finding::Verified,
        &["cookie", "session-storage", "cache", "approval-server"],
    ),
    (Check::XAuthorityRead, Finding::AccessDenied, &["xauth"]),
    (Check::XhostPolicy, Finding::Verified, &["xhost-policy"]),
    (Check::X11Unix, Finding::ProtocolDenied, &["x11-filesystem"]),
    (
        Check::X11Abstract,
        Finding::ProtocolDenied,
        &["x11-abstract"],
    ),
    (Check::Xwayland, Finding::ProtocolDenied, &["xwayland"]),
    (Check::Wayland, Finding::TransportDenied, &["wayland"]),
    (Check::RuntimeTraverse, Finding::AccessDenied, &["runtime"]),
    (Check::RuntimeMode, Finding::Verified, &["runtime"]),
    (Check::SessionBus, Finding::TransportDenied, &["bus"]),
    (Check::Portal, Finding::TransportDenied, &["portal"]),
    (Check::PipeWire, Finding::TransportDenied, &["pipewire"]),
    (Check::PulseAudio, Finding::TransportDenied, &["pulse"]),
    (
        Check::DesktopKeyring,
        Finding::TransportDenied,
        &["secret-service", "kwallet"],
    ),
    (
        Check::KernelKeyring,
        Finding::AccessDenied,
        &["kernel-keyring"],
    ),
    (Check::SshAgent, Finding::TransportDenied, &["ssh-agent"]),
    (Check::GpgAgent, Finding::TransportDenied, &["gpg-agent"]),
    (Check::FidoOpen, Finding::AccessDenied, &["fixture-hidraw"]),
    (
        Check::PlatformPasskey,
        Finding::AccessDenied,
        &["browser-store", "os-store"],
    ),
    (
        Check::Polkit,
        Finding::PolicyDeniedWithoutChallenge,
        &["admin-action"],
    ),
    (Check::Trace, Finding::AccessDenied, &["desktop", "browser"]),
    (
        Check::Signal,
        Finding::AccessDenied,
        &["desktop", "browser"],
    ),
    (
        Check::Environ,
        Finding::AccessDenied,
        &["desktop", "browser"],
    ),
    (
        Check::Cmdline,
        Finding::CmdlineResidual,
        &["desktop", "browser"],
    ),
    (
        Check::LogindSeparation,
        Finding::Verified,
        &["agent-session"],
    ),
    (Check::SeatInactive, Finding::Verified, &["seat"]),
    (Check::TerminalSeparation, Finding::Verified, &["agent-tty"]),
    (Check::LegacyTiocsti, Finding::Verified, &["legacy-tiocsti"]),
    (
        Check::PtraceScope,
        Finding::Verified,
        &["yama-ptrace-scope"],
    ),
    (
        Check::TcpListeners,
        Finding::TransportDenied,
        &["desktop-tcp-a", "desktop-tcp-b"],
    ),
    (
        Check::UnixSockets,
        Finding::ProtocolDenied,
        &["desktop-unix"],
    ),
    (
        Check::AbstractSockets,
        Finding::ProtocolDenied,
        &["desktop-abstract"],
    ),
];

const CONTROL_EVIDENCE: &[(Control, ControlFinding)] = &[
    (Control::ReadableDecoy, ControlFinding::HomeCanaryRead),
    (
        Control::DesktopGroup,
        ControlFinding::DesktopGroupAndHomeCanaryRead,
    ),
    (
        Control::DesktopSocket,
        ControlFinding::DesktopSocketConnected,
    ),
    (Control::XhostGrant, ControlFinding::X11AccessGranted),
    (Control::PolkitChallenge, ControlFinding::PolkitChallenge),
    (Control::TcpListener, ControlFinding::TcpListenerConnected),
    (
        Control::AbstractSocket,
        ControlFinding::AbstractSocketConnected,
    ),
    (Control::ExtraGroup, ControlFinding::ExtraGroupObserved),
];

fn account(uid: u32, primary: u32, supplementary: &[u32]) -> Account {
    Account {
        uid,
        primary,
        supplementary: supplementary.iter().copied().collect(),
        primary_members: [uid].into(),
    }
}

fn fixture() -> (Scope, Pair) {
    let scope = Scope {
        configured_service_uid: 100,
        configured_job_uid: 101,
        configured_agent_uids: [102].into(),
        configured_desktop_uids: [103].into(),
        service: account(100, 200, &[]),
        job: account(101, 201, &[]),
        agents: vec![account(102, 202, &[204])],
        desktops: vec![account(103, 203, &[])],
        agent_group: 204,
        service_group: 200,
        job_group: 201,
        agent_members: [102].into(),
        service_listed_members: BTreeSet::new(),
        job_listed_members: BTreeSet::new(),
        identities_complete: true,
    };
    let pair = Pair {
        agent_uid: 102,
        desktop_uid: 103,
        observations: OBSERVATIONS
            .iter()
            .map(|&(check, finding, targets)| Inventory {
                check,
                targets_before: Some(targets.iter().map(|name| (*name).to_owned()).collect()),
                targets_after: Some(
                    targets
                        .iter()
                        .rev()
                        .map(|name| (*name).to_owned())
                        .collect(),
                ),
                observed: targets
                    .iter()
                    .map(|name| ((*name).to_owned(), finding))
                    .collect(),
            })
            .collect(),
        controls: CONTROL_EVIDENCE.to_vec(),
    };
    (scope, pair)
}

#[test]
fn complete_isolated_fixture_passes_and_covers_contract() {
    let (scope, pair) = fixture();
    assert_eq!(
        OBSERVATIONS
            .iter()
            .map(|row| row.0)
            .collect::<BTreeSet<_>>(),
        CHECKS.iter().copied().collect()
    );
    assert_eq!(
        CONTROL_EVIDENCE
            .iter()
            .map(|row| row.0)
            .collect::<BTreeSet<_>>(),
        CONTROLS.iter().copied().collect()
    );
    assert_eq!(
        evaluate(&scope, std::slice::from_ref(&pair)),
        Evaluation::Pass
    );
    let mut reordered = pair;
    reordered.controls.reverse();
    reordered.observations.reverse();
    for inventory in &mut reordered.observations {
        inventory.observed.reverse();
    }
    assert_eq!(evaluate(&scope, &[reordered]), Evaluation::Pass);
}

#[test]
fn each_obligation_requires_a_complete_target_inventory_and_observations() {
    let (scope, pair) = fixture();
    for (index, &(check, _, _)) in OBSERVATIONS.iter().enumerate() {
        let mut omitted = pair.clone();
        omitted.observations.remove(index);
        assert_ne!(evaluate(&scope, &[omitted]), Evaluation::Pass, "{check:?}");

        let mut incomplete = pair.clone();
        incomplete.observations[index].targets_before = None;
        assert_eq!(
            evaluate(&scope, &[incomplete]),
            Evaluation::Missing,
            "{check:?}"
        );

        let mut missing_after = pair.clone();
        missing_after.observations[index].targets_after = None;
        assert_eq!(
            evaluate(&scope, &[missing_after]),
            Evaluation::Missing,
            "{check:?}"
        );

        let mut no_targets = pair.clone();
        no_targets.observations[index].targets_before = Some(Vec::new());
        no_targets.observations[index].targets_after = Some(Vec::new());
        no_targets.observations[index].observed.clear();
        assert_eq!(
            evaluate(&scope, &[no_targets]),
            Evaluation::Missing,
            "{check:?}"
        );

        let mut changed_targets = pair.clone();
        changed_targets.observations[index].targets_after = Some(vec!["new-target".into()]);
        assert_eq!(
            evaluate(&scope, &[changed_targets]),
            Evaluation::Missing,
            "{check:?}"
        );

        let mut omitted_target = pair.clone();
        omitted_target.observations[index].observed.pop();
        assert_ne!(
            evaluate(&scope, &[omitted_target]),
            Evaluation::Pass,
            "{check:?}"
        );

        let mut unavailable_target = pair.clone();
        unavailable_target.observations[index].observed[0].1 = Finding::Unavailable;
        assert_ne!(
            evaluate(&scope, &[unavailable_target]),
            Evaluation::Pass,
            "{check:?}"
        );
    }
}

#[test]
fn each_check_accepts_only_its_own_typed_findings() {
    let (scope, pair) = fixture();
    for (index, &(check, expected, _)) in OBSERVATIONS.iter().enumerate() {
        for replacement in [
            Finding::AccessDenied,
            Finding::AccessGranted,
            Finding::TransportDenied,
            Finding::Connected,
            Finding::ProtocolDenied,
            Finding::ProtocolGranted,
            Finding::PolicyDeniedWithoutChallenge,
            Finding::Challenge,
            Finding::Verified,
            Finding::Violated,
            Finding::CmdlineResidual,
            Finding::Unavailable,
        ] {
            let accepted = match expected {
                Finding::TransportDenied => {
                    matches!(
                        replacement,
                        Finding::TransportDenied | Finding::ProtocolDenied
                    )
                }
                Finding::ProtocolDenied
                    if matches!(check, Check::UnixSockets | Check::AbstractSockets) =>
                {
                    matches!(
                        replacement,
                        Finding::TransportDenied | Finding::ProtocolDenied
                    )
                }
                Finding::CmdlineResidual => {
                    matches!(
                        replacement,
                        Finding::AccessDenied | Finding::CmdlineResidual
                    )
                }
                _ => replacement == expected,
            };
            let mut changed = pair.clone();
            changed.observations[index].observed[0].1 = replacement;
            assert_eq!(
                evaluate(&scope, &[changed]) == Evaluation::Pass,
                accepted,
                "{check:?} returned {replacement:?}"
            );
        }
    }
}

#[test]
#[allow(clippy::unwrap_used)]
fn malformed_inventories_fail_even_when_all_listed_attempts_would_pass() {
    let (scope, pair) = fixture();
    let corruptions: [fn(&mut Inventory); 8] = [
        |row| {
            row.targets_before
                .as_mut()
                .unwrap()
                .push(row.observed[0].0.clone())
        },
        |row| {
            row.targets_after
                .as_mut()
                .unwrap()
                .push(row.observed[0].0.clone())
        },
        |row| row.observed.push(row.observed[0].clone()),
        |row| row.observed[0].0 = "wrong-target".into(),
        |row| row.targets_before.as_mut().unwrap()[0] = "wrong-target".into(),
        |row| {
            row.targets_before.as_mut().unwrap()[0].clear();
            row.observed[0].0.clear();
        },
        |row| {
            row.targets_before.as_mut().unwrap()[0] = "bad\0target".into();
            row.observed[0].0 = "bad\0target".into();
        },
        |row| row.observed.push(("unlisted".into(), row.observed[0].1)),
    ];
    for corrupt in corruptions {
        let mut changed = pair.clone();
        let row = changed
            .observations
            .iter_mut()
            .find(|row| row.check == Check::TcpListeners)
            .unwrap();
        corrupt(row);
        assert_ne!(evaluate(&scope, &[changed]), Evaluation::Pass);
    }

    let mut repeated = pair.clone();
    repeated.observations.push(repeated.observations[0].clone());
    assert_ne!(evaluate(&scope, &[repeated]), Evaluation::Pass);
}

#[test]
fn every_negative_control_must_report_its_own_expected_failure() {
    let (scope, pair) = fixture();
    for (index, &(control, expected)) in CONTROL_EVIDENCE.iter().enumerate() {
        let mut omitted = pair.clone();
        omitted.controls.remove(index);
        assert_ne!(
            evaluate(&scope, &[omitted]),
            Evaluation::Pass,
            "{control:?}"
        );

        let mut duplicate = pair.clone();
        duplicate.controls.push((control, expected));
        assert_ne!(
            evaluate(&scope, &[duplicate]),
            Evaluation::Pass,
            "{control:?}"
        );

        for finding in [
            ControlFinding::HomeCanaryRead,
            ControlFinding::DesktopGroupAndHomeCanaryRead,
            ControlFinding::DesktopSocketConnected,
            ControlFinding::TcpListenerConnected,
            ControlFinding::AbstractSocketConnected,
            ControlFinding::X11AccessGranted,
            ControlFinding::PolkitChallenge,
            ControlFinding::ExtraGroupObserved,
            ControlFinding::Unavailable,
        ] {
            let mut changed = pair.clone();
            changed.controls[index].1 = finding;
            assert_eq!(
                evaluate(&scope, &[changed]) == Evaluation::Pass,
                finding == expected,
                "{control:?} returned {finding:?}"
            );
        }
    }
}

#[test]
fn evidence_is_required_for_every_agent_and_desktop_pair() {
    let (mut scope, pair) = fixture();
    scope.agents.push(account(104, 205, &[204]));
    scope.agent_members.insert(104);
    scope.configured_agent_uids.insert(104);
    scope.desktops.push(account(105, 206, &[]));
    scope.configured_desktop_uids.insert(105);

    let pairs = [
        pair.clone(),
        Pair {
            agent_uid: 104,
            ..pair.clone()
        },
        Pair {
            desktop_uid: 105,
            ..pair.clone()
        },
        Pair {
            agent_uid: 104,
            desktop_uid: 105,
            ..pair
        },
    ];
    assert_eq!(evaluate(&scope, &pairs), Evaluation::Pass);
    assert_ne!(evaluate(&scope, &pairs[..3]), Evaluation::Pass);
    assert_ne!(
        evaluate(
            &scope,
            &[
                pairs[0].clone(),
                pairs[0].clone(),
                pairs[2].clone(),
                pairs[3].clone()
            ]
        ),
        Evaluation::Pass
    );
    let mut wrong = pairs.clone();
    wrong[0].agent_uid = 999;
    assert_ne!(evaluate(&scope, &wrong), Evaluation::Pass);
    wrong[0] = pairs[0].clone();
    wrong[0].desktop_uid = 999;
    assert_ne!(evaluate(&scope, &wrong), Evaluation::Pass);

    let mut agent_missing = scope.clone();
    agent_missing.agents.pop();
    agent_missing.agent_members.remove(&104);
    let remaining_pairs = [pairs[0].clone(), pairs[2].clone()];
    assert_ne!(evaluate(&agent_missing, &remaining_pairs), Evaluation::Pass);

    let mut desktop_missing = scope;
    desktop_missing.desktops.pop();
    assert_ne!(evaluate(&desktop_missing, &pairs[..2]), Evaluation::Pass);
}

#[test]
fn account_and_group_facts_must_match_j1() {
    type Corruption = (&'static str, fn(&mut Scope));
    let corruptions: &[Corruption] = &[
        ("partial identity inventory", |scope| {
            scope.identities_complete = false
        }),
        ("no observed agents", |scope| scope.agents.clear()),
        ("no observed desktops", |scope| scope.desktops.clear()),
        ("no configured agents", |scope| {
            scope.configured_agent_uids.clear()
        }),
        ("no configured desktops", |scope| {
            scope.configured_desktop_uids.clear()
        }),
        ("wrong configured job", |scope| {
            scope.configured_job_uid = 999
        }),
        ("wrong configured service", |scope| {
            scope.configured_service_uid = 999
        }),
        ("wrong configured job gid", |scope| scope.job_group = 999),
        ("wrong configured service gid", |scope| {
            scope.service_group = 999
        }),
        ("root", |scope| {
            scope.service.uid = 0;
            scope.configured_service_uid = 0;
            scope.service.primary_members = [0].into();
        }),
        ("invalid UID", |scope| {
            scope.service.uid = u32::MAX;
            scope.configured_service_uid = u32::MAX;
            scope.service.primary_members = [u32::MAX].into();
        }),
        ("root group", |scope| {
            scope.service.primary = 0;
            scope.service_group = 0;
        }),
        ("invalid GID", |scope| {
            scope.job.primary = u32::MAX;
            scope.job_group = u32::MAX;
        }),
        ("shared service identity", |scope| {
            scope.job.uid = scope.service.uid;
            scope.configured_job_uid = scope.service.uid;
            scope.job.primary_members = [scope.service.uid].into();
        }),
        ("shared desktop identity", |scope| {
            scope.desktops[0].uid = scope.agents[0].uid;
            scope.configured_desktop_uids = [scope.agents[0].uid].into();
        }),
        ("shared special group", |scope| {
            scope.agent_group = scope.job.primary
        }),
        ("agent primary group is privileged", |scope| {
            scope.agents[0].primary = scope.service.primary
        }),
        ("desktop primary group is privileged", |scope| {
            scope.desktops[0].primary = scope.agent_group
        }),
        ("shared desktop primary group", |scope| {
            scope.agents[0].primary = scope.desktops[0].primary
        }),
        ("service supplements", |scope| {
            scope.service.supplementary.insert(300);
        }),
        ("job supplements", |scope| {
            scope.job.supplementary.insert(300);
        }),
        ("agent missing its access group", |scope| {
            scope.agents[0].supplementary.clear()
        }),
        ("agent extra group", |scope| {
            scope.agents[0].supplementary.insert(300);
        }),
        ("desktop supplements", |scope| {
            scope.desktops[0].supplementary.insert(300);
        }),
        (
            "service listed as supplemental member of its primary group",
            |scope| {
                scope.service_listed_members.insert(scope.service.uid);
            },
        ),
        (
            "job listed as supplemental member of its primary group",
            |scope| {
                scope.job_listed_members.insert(scope.job.uid);
            },
        ),
        ("unexpected service group member", |scope| {
            scope.service.primary_members.insert(999);
        }),
        ("unexpected job group member", |scope| {
            scope.job.primary_members.insert(999);
        }),
        ("unexpected agent group member", |scope| {
            scope.agent_members.insert(999);
        }),
        ("unexpected agent primary group member", |scope| {
            scope.agents[0].primary_members.insert(999);
        }),
        ("agent in desktop group", |scope| {
            scope.desktops[0]
                .primary_members
                .insert(scope.agents[0].uid);
        }),
        ("job in desktop group", |scope| {
            scope.desktops[0].primary_members.insert(scope.job.uid);
        }),
    ];
    let (scope, pair) = fixture();
    for (label, corrupt) in corruptions {
        let mut changed = scope.clone();
        corrupt(&mut changed);
        assert_ne!(
            evaluate(&changed, std::slice::from_ref(&pair)),
            Evaluation::Pass,
            "{label}"
        );
    }
}
