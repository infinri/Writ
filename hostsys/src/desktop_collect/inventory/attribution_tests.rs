use super::*;

const DESKTOP: u32 = 1000;
const OTHER_DESKTOP: u32 = 1001;
const SERVICE: u32 = 104;

fn socket(kind: Kind, inode: u64, kernel_uid: u32) -> Socket {
    let endpoint = match kind {
        Kind::Tcp => Endpoint::TcpObserved {
            address: std::net::Ipv4Addr::LOCALHOST.into(),
            port: 1234,
            interface: 0,
            ipv6_only: None,
            cookie: [1, 2],
        },
        Kind::Unix | Kind::Abstract => Endpoint::UnixObserved {
            socket_type: 1,
            name: if kind == Kind::Unix {
                b"/test\0".to_vec()
            } else {
                b"\0test".to_vec()
            },
            state: 10,
            shutdown: 0,
            cookie: [1, 2],
        },
    };
    Socket {
        inode,
        kernel_uid: Some(kernel_uid),
        kind,
        endpoint,
    }
}

fn holders(rows: &[(u32, u32)]) -> BTreeSet<Holder> {
    rows.iter()
        .map(|&(pid, uid)| Holder {
            pid,
            start_ticks: u64::from(pid) + 100,
            uid,
        })
        .collect()
}

#[test]
fn stable_root_service_sharing_preserves_every_desktop_row_and_output_order() -> Result<(), Problem>
{
    for kind in [Kind::Tcp, Kind::Unix, Kind::Abstract] {
        let sample = |reverse| {
            let mut sockets = vec![
                socket(kind, 10, 0),
                socket(kind, 11, SERVICE),
                socket(kind, 12, DESKTOP),
                socket(kind, 13, OTHER_DESKTOP),
            ];
            if reverse {
                sockets.reverse();
            }
            collect_targets(sockets, &[DESKTOP, OTHER_DESKTOP].into(), |inode| {
                let mut rows = match inode {
                    10 | 11 => vec![(1, 0), (20, SERVICE)],
                    12 => vec![(31, DESKTOP), (30, DESKTOP)],
                    13 => vec![(40, OTHER_DESKTOP)],
                    _ => return Err(Problem::Mismatch),
                };
                if reverse {
                    rows.reverse();
                }
                Ok(holders(&rows))
            })
        };
        let inventory = sample(false)?;
        assert_eq!(inventory.len(), 2);
        for (uid, inode, expected) in [
            (DESKTOP, 12, holders(&[(30, DESKTOP), (31, DESKTOP)])),
            (OTHER_DESKTOP, 13, holders(&[(40, OTHER_DESKTOP)])),
        ] {
            let targets = &inventory[&(uid, kind)];
            assert_eq!(targets.len(), 1);
            let target = targets.first().ok_or(Problem::Missing)?;
            assert_eq!(target.inode, inode);
            assert_eq!(target.holders, expected);
        }
        let mut original = String::new();
        render(&mut original, &inventory)?;
        let mut reordered = String::new();
        render(&mut reordered, &sample(true)?)?;
        assert_eq!(original, reordered);
        assert!(original.contains("holder 12 30 130\nholder 12 31 131\n"));
    }
    Ok(())
}

#[test]
fn desktop_associated_sharing_and_sole_remaining_transferees_refuse() {
    let cases = [
        (DESKTOP, &[(1, 0), (20, SERVICE)][..]),
        (DESKTOP, &[(1, 0), (30, DESKTOP)]),
        (DESKTOP, &[(20, SERVICE), (30, DESKTOP)]),
        (DESKTOP, &[(30, DESKTOP), (40, OTHER_DESKTOP)]),
        (DESKTOP, &[(20, SERVICE)]),
        (SERVICE, &[(30, DESKTOP)]),
        (0, &[(30, DESKTOP), (40, SERVICE)]),
        (SERVICE, &[(30, DESKTOP), (40, 0)]),
        (OTHER_DESKTOP, &[(30, DESKTOP)]),
    ];
    for kind in [Kind::Tcp, Kind::Unix, Kind::Abstract] {
        for (uid, rows) in cases {
            let result = collect_targets(
                vec![socket(kind, 10, uid)],
                &[DESKTOP, OTHER_DESKTOP].into(),
                |_| Ok(holders(rows)),
            );
            assert!(result.is_err(), "{kind:?} kernel UID {uid}, {rows:?}");
        }
    }
}

#[test]
fn missing_or_inconsistent_holders_refuse_even_for_a_non_desktop_kernel_uid() {
    for kind in [Kind::Tcp, Kind::Unix, Kind::Abstract] {
        for observation in [
            Ok(BTreeSet::new()),
            Err(Problem::Changed),
            Err(Problem::Ambiguous),
            Err(Problem::Incomplete),
        ] {
            let expected = observation
                .as_ref()
                .err()
                .copied()
                .unwrap_or(Problem::Incomplete);
            let observed =
                collect_targets(vec![socket(kind, 10, SERVICE)], &[DESKTOP].into(), |_| {
                    observation.clone()
                });
            assert_eq!(observed, Err(expected));
        }
    }
}

#[test]
fn observed_endpoint_without_a_kernel_uid_is_unavailable() {
    for kind in [Kind::Tcp, Kind::Unix, Kind::Abstract] {
        for rows in [&[(1, 0)][..], &[(1, 0), (20, SERVICE)], &[(30, DESKTOP)]] {
            let mut unproven = socket(kind, 10, 0);
            unproven.kernel_uid = None;
            let result = collect_targets(vec![unproven], &[DESKTOP].into(), |_| Ok(holders(rows)));
            assert!(result.is_err(), "{kind:?} without kernel UID, {rows:?}");
        }
    }
}
