#![allow(clippy::unwrap_used)]

use std::fs;
use writ_policy_management_lab::{error_response, execute_v1, Error, Store};

#[test]
fn missing_auth_configuration_refuses_a_well_formed_protected_operation() {
    let path =
        std::env::temp_dir().join(format!("writ-auth-defaults-{}.sqlite", std::process::id()));
    let store = Store::create(&path).unwrap();
    let response = execute_v1(
        &store,
        None,
        b"fixture-author",
        br#"{"version":"/manage/v1","operation":"history","scope":{"tenant":"m6-lab","project":"offline-proof"},"id":"draft","after":0,"limit":50}"#,
    );
    fs::remove_file(path).unwrap();
    assert_eq!(response, error_response(Error::Unauthorized));
}
