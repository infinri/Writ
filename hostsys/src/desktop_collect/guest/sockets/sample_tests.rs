use super::super::verify_socket_samples;
use super::{DescriptorView, Diagnostic, Problem, Tcp};
use std::net::Ipv4Addr;

fn snapshot(uid: u32, cookie: [u32; 2], fds: [(u32, u32); 2]) -> (Diagnostic, DescriptorView) {
    let inode = 51;
    let listener = Tcp {
        address: Ipv4Addr::LOCALHOST.into(),
        port: 12345,
        uid,
        cookie,
        ifindex: 0,
        ipv6_only: None,
    };
    let diagnostic = Diagnostic {
        tcp: [(inode, listener)].into(),
        ..Diagnostic::default()
    };
    let descriptors = DescriptorView([(inode, fds.into())].into());
    (diagnostic, descriptors)
}

#[test]
fn second_sample_checks_even_an_excluded_shared_listener() {
    let fds = [(1, 9), (42, 12)];
    let cookie = [2, 3];
    let (diag, descriptors) = snapshot(0, cookie, fds);
    let before = (&diag, &descriptors);
    let (same_diag, same_descriptors) = snapshot(0, cookie, [fds[1], fds[0]]);
    assert_eq!(
        verify_socket_samples(before, (&same_diag, &same_descriptors)),
        Ok(())
    );

    for (changed_diag, changed_descriptors) in [
        snapshot(0, [2, 4], fds),
        snapshot(1000, cookie, fds),
        snapshot(0, cookie, [(1, 9), (42, 13)]),
        snapshot(0, cookie, [(1, 9), (43, 12)]),
    ] {
        assert_eq!(
            verify_socket_samples(before, (&changed_diag, &changed_descriptors)),
            Err(Problem::Changed)
        );
    }
}
