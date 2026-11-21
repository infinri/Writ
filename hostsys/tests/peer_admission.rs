use writ_hostsys::jobprobes::{eval_launcher_admit, REFUSED_PEERS, REQUEST_CASES};
use writ_hostsys::readiness::Status;

#[test]
fn peer_access_denial_does_not_establish_connected_admission() {
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
    assert_eq!(eval_launcher_admit(&report, &[]).status, Status::Ok);
    for observation in [
        "access_denied_before_connect",
        "timeout",
        "malformed_response",
    ] {
        let (_, value) = report
            .iter_mut()
            .find(|(key, _)| key == "peer_agent")
            .unwrap();
        *value = observation.into();
        let result = eval_launcher_admit(&report, &[]);
        assert_eq!(result.status, Status::Missing);
        assert!(result.reason.contains(observation));
    }
}
