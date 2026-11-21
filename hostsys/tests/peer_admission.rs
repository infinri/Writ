//! Launcher peer observations required for governed readiness.
use writ_hostsys::jobprobes::{eval_launcher_admit, REFUSED_PEERS, REQUEST_CASES};
use writ_hostsys::readiness::{verdict, Control, ProbeId, ProbeResult, Status};

fn passing_report() -> Vec<(String, String)> {
    let mut report = vec![
        ("peer_writ".into(), "hello".into()),
        ("dropins".into(), "none".into()),
        ("ctl_admit_agent".into(), "detected".into()),
    ];
    for peer in REFUSED_PEERS {
        report.push((format!("peer_{peer}"), "closed_before_read".into()));
    }
    for (case, expected) in REQUEST_CASES {
        report.push((format!("case_{case}"), expected.into()));
        report.push((format!("case_{case}_job_created"), "0".into()));
    }
    report
}

#[test]
fn peer_access_denial_does_not_establish_connected_admission() {
    let passing = passing_report();
    assert_eq!(eval_launcher_admit(&passing, &[]).status, Status::Ok);
    for peer in REFUSED_PEERS.into_iter().chain(["writ"]) {
        let key = format!("peer_{peer}");
        for observation in [
            "access_denied_before_connect",
            "timeout",
            "malformed_response",
            "unavailable",
        ] {
            let mut report = passing.clone();
            report.retain(|(name, _)| name != &key);
            report.push((key.clone(), observation.into()));
            let admission = eval_launcher_admit(&report, &[]);
            assert_eq!(admission.status, Status::Missing, "{key}={observation}");
            let probes: Vec<_> = ProbeId::ALL
                .into_iter()
                .map(|id| {
                    let status = if id == ProbeId::LauncherAdmit {
                        admission.status
                    } else {
                        Status::Ok
                    };
                    ProbeResult::new(
                        id,
                        status,
                        "",
                        vec![],
                        Control::runtime(true, String::new()),
                    )
                })
                .collect();
            let result = verdict(&probes);
            assert!(!result.governed_ready);
            assert_eq!(result.blocking.len(), 1);
            assert_eq!(result.blocking[0].0, ProbeId::LauncherAdmit);
        }
    }
}
