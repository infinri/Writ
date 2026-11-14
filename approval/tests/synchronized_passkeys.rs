//! Independent passkey fixtures through the real verifier (D-041 "independent
//! synchronized-passkey fixtures and counter/backup negative cases through the real
//! verifier"; D-040 O-1).
//!
//! The fixture authenticator is built here from maintained primitives only: ECDSA P-256
//! signing from `p256` (dev-dependency), SHA-256 from the kernel's `Digest`, and a few lines
//! of CBOR and base64url encoding for the WebAuthn wire format. It is not a verifier and
//! nothing here bypasses one: every registration and assertion goes through
//! [`PasskeyVerifier`], which uses only `webauthn-rs`. Test credentials only; no real user.

#![allow(clippy::unwrap_used, clippy::expect_used, clippy::panic)]

use p256::ecdsa::signature::Signer;
use p256::ecdsa::{Signature, SigningKey};
use serde_json::json;
use std::sync::Arc;

use writ_approval::verifier::{CheckedAssertion, PasskeyVerifier};
use writ_broker::human::{
    ActiveCredentials, ApprovalChannel, CredentialStatus, HumanVerifier, Permission, PrincipalKind,
    RegistrationBinding, RegistrationOperation, RegistrationVerifier, RoleAssignment,
    StoredCredential, SubjectId, SubjectRecord, SubjectStatus, VerifiedRegistration, VerifierError,
    ES256,
};
use writ_broker::writ_kernel::json::Digest;
use writ_broker::writ_kernel::time::Timestamp;

const ORIGIN: &str = "http://localhost:8443";
const UP: u8 = 0x01;
const UV: u8 = 0x04;
const BE: u8 = 0x08;
const BS: u8 = 0x10;
const AT: u8 = 0x40;
/// A synchronized (multi-device) passkey: backup eligible and backed up.
const SYNCED: u8 = UP | UV | BE | BS;
/// A device-bound passkey.
const BOUND: u8 = UP | UV;

// ---------------------------------------------------------------------------
// Wire-format helpers (encoding only).

fn b64url(bytes: &[u8]) -> String {
    const A: &[u8; 64] = b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_";
    let mut out = String::new();
    for chunk in bytes.chunks(3) {
        let n = chunk
            .iter()
            .enumerate()
            .fold(0u32, |acc, (i, b)| acc | (u32::from(*b) << (16 - 8 * i)));
        for i in 0..=chunk.len() {
            out.push(A[((n >> (18 - 6 * i)) & 63) as usize] as char);
        }
    }
    out
}

fn sha256(bytes: &[u8]) -> Vec<u8> {
    let hex = Digest::of_bytes(bytes).hex().to_string();
    (0..hex.len())
        .step_by(2)
        .map(|i| u8::from_str_radix(&hex[i..i + 2], 16).unwrap())
        .collect()
}

fn cbor_head(major: u8, n: u64, out: &mut Vec<u8>) {
    let m = major << 5;
    match n {
        0..=23 => out.push(m | n as u8),
        24..=0xff => out.extend([m | 24, n as u8]),
        0x100..=0xffff => {
            out.push(m | 25);
            out.extend((n as u16).to_be_bytes());
        }
        _ => {
            out.push(m | 26);
            out.extend((n as u32).to_be_bytes());
        }
    }
}

fn cbor_int(i: i64, out: &mut Vec<u8>) {
    if i >= 0 {
        cbor_head(0, i as u64, out);
    } else {
        cbor_head(1, (-1 - i) as u64, out);
    }
}

fn cbor_bytes(b: &[u8], out: &mut Vec<u8>) {
    cbor_head(2, b.len() as u64, out);
    out.extend_from_slice(b);
}

fn cbor_text(t: &str, out: &mut Vec<u8>) {
    cbor_head(3, t.len() as u64, out);
    out.extend_from_slice(t.as_bytes());
}

// ---------------------------------------------------------------------------
// The fixture authenticator.

struct Fixture {
    key: SigningKey,
    credential_id: Vec<u8>,
}

impl Fixture {
    fn new(seed: u8) -> Fixture {
        Fixture {
            key: SigningKey::from_slice(&[seed; 32]).unwrap(),
            credential_id: vec![seed; 16],
        }
    }

    /// COSE_Key of the ES256 public key.
    fn cose_key(&self) -> Vec<u8> {
        let point = self.key.verifying_key().to_sec1_point(false);
        let raw = point.as_bytes();
        assert_eq!(raw.len(), 65);
        let mut out = Vec::new();
        cbor_head(5, 5, &mut out);
        cbor_int(1, &mut out);
        cbor_int(2, &mut out); // kty EC2
        cbor_int(3, &mut out);
        cbor_int(-7, &mut out); // alg ES256
        cbor_int(-1, &mut out);
        cbor_int(1, &mut out); // crv P-256
        cbor_int(-2, &mut out);
        cbor_bytes(&raw[1..33], &mut out);
        cbor_int(-3, &mut out);
        cbor_bytes(&raw[33..65], &mut out);
        out
    }

    fn challenge(options: &[u8]) -> String {
        let v: serde_json::Value = serde_json::from_slice(options).unwrap();
        v["publicKey"]["challenge"].as_str().unwrap().to_string()
    }

    /// A `none`-attestation registration response with `flags` and `counter`.
    fn registration(&self, options: &[u8], flags: u8, counter: u32) -> Vec<u8> {
        let client_data = json!({
            "type": "webauthn.create",
            "challenge": Fixture::challenge(options),
            "origin": ORIGIN,
            "crossOrigin": false,
        })
        .to_string();
        let mut auth_data = sha256(b"localhost");
        auth_data.push(flags | AT);
        auth_data.extend(counter.to_be_bytes());
        auth_data.extend([0u8; 16]);
        auth_data.extend((self.credential_id.len() as u16).to_be_bytes());
        auth_data.extend(&self.credential_id);
        auth_data.extend(self.cose_key());
        let mut att = Vec::new();
        cbor_head(5, 3, &mut att);
        cbor_text("fmt", &mut att);
        cbor_text("none", &mut att);
        cbor_text("attStmt", &mut att);
        cbor_head(5, 0, &mut att);
        cbor_text("authData", &mut att);
        cbor_bytes(&auth_data, &mut att);
        serde_json::to_vec(&json!({
            "id": b64url(&self.credential_id),
            "rawId": b64url(&self.credential_id),
            "type": "public-key",
            "response": {
                "attestationObject": b64url(&att),
                "clientDataJSON": b64url(client_data.as_bytes()),
            },
            "extensions": {},
        }))
        .unwrap()
    }

    /// An assertion response over `options` with `flags` and `counter`, for `origin` and
    /// the RP id `rp_id`.
    fn assertion(
        &self,
        options: &[u8],
        flags: u8,
        counter: u32,
        origin: &str,
        rp_id: &str,
    ) -> Vec<u8> {
        let client_data = json!({
            "type": "webauthn.get",
            "challenge": Fixture::challenge(options),
            "origin": origin,
            "crossOrigin": false,
        })
        .to_string();
        let mut auth_data = sha256(rp_id.as_bytes());
        auth_data.push(flags);
        auth_data.extend(counter.to_be_bytes());
        let mut signed = auth_data.clone();
        signed.extend(sha256(client_data.as_bytes()));
        let signature: Signature = self.key.sign(&signed);
        serde_json::to_vec(&json!({
            "id": b64url(&self.credential_id),
            "rawId": b64url(&self.credential_id),
            "type": "public-key",
            "response": {
                "authenticatorData": b64url(&auth_data),
                "clientDataJSON": b64url(client_data.as_bytes()),
                "signature": b64url(signature.to_der().as_bytes()),
                "userHandle": null,
            },
            "extensions": {},
        }))
        .unwrap()
    }
}

// ---------------------------------------------------------------------------
// Driving the real verifier.

fn t() -> Timestamp {
    Timestamp::parse_strict("2026-11-14T10:00:00Z").unwrap()
}

fn bob() -> SubjectId {
    SubjectId::new("writ-local", "bob")
}

/// The verifier as a configured approval channel (M1: the only way to lend it a verifier
/// capability).
fn channel(v: &Arc<PasskeyVerifier>) -> ApprovalChannel {
    ApprovalChannel::new(v.clone(), v.clone(), "approval-listener").unwrap()
}

fn verifier() -> Arc<PasskeyVerifier> {
    Arc::new(PasskeyVerifier::new("localhost", ORIGIN).unwrap())
}

fn register(
    v: &Arc<PasskeyVerifier>,
    f: &Fixture,
    flags: u8,
    counter: u32,
) -> Result<VerifiedRegistration, VerifierError> {
    let start = v.begin_registration(&bob()).unwrap();
    let binding = RegistrationBinding {
        registration_id: "reg-fixture".into(),
        operation: RegistrationOperation::Enroll,
        subject: bob(),
        relying_party: v.relying_party(),
    };
    let response = f.registration(&start.client_options, flags, counter);
    channel(v).finish_registration(&binding, &start.state, &response)
}

fn active(credential_id: &str, material: &[u8]) -> ActiveCredentials {
    let record = SubjectRecord {
        subject: bob(),
        kind: PrincipalKind::Human,
        status: SubjectStatus::Active,
    };
    let role = RoleAssignment {
        assignment_id: "r".into(),
        subject: bob(),
        kind: PrincipalKind::Human,
        permission: Permission::Approver,
        tenant_id: "t".into(),
        project_id: "p".into(),
        granted_at: t(),
        revoked_at: None,
    };
    let stored = StoredCredential {
        credential_id: credential_id.into(),
        subject: bob(),
        public_material: material.to_vec(),
        status: CredentialStatus::Active,
        enrolled_at: t(),
        enrolled_by: SubjectId::new("writ-local", "root"),
    };
    ActiveCredentials::select(
        &record,
        [stored],
        &[role],
        Permission::Approver,
        "t",
        "p",
        t(),
    )
    .unwrap()
}

fn assert_with(
    v: &Arc<PasskeyVerifier>,
    f: &Fixture,
    creds: &ActiveCredentials,
    flags: u8,
    counter: u32,
    origin: &str,
    rp_id: &str,
) -> Result<CheckedAssertion, VerifierError> {
    let start = v.begin(&bob(), creds).unwrap();
    let response = f.assertion(&start.client_options, flags, counter, origin, rp_id);
    let checked = v.verify_assertion(&start.state, &response, creds);
    // The trait answer agrees: same outcome, bound to this ceremony's challenge digest.
    match (&checked, channel(v).finish(&start.state, &response, creds)) {
        (Ok(c), Ok(a)) => {
            assert_eq!(a.challenge_digest(), &start.challenge_digest);
            assert_eq!(a.credential_id(), c.credential_id);
            assert_eq!(a.subject(), &bob());
        }
        (Err(e), Err(f)) => assert_eq!(e, &f),
        (c, a) => panic!("{c:?} {a:?}"),
    }
    checked
}

fn assert_ok(
    v: &Arc<PasskeyVerifier>,
    f: &Fixture,
    creds: &ActiveCredentials,
    flags: u8,
    counter: u32,
) -> Result<CheckedAssertion, VerifierError> {
    assert_with(v, f, creds, flags, counter, ORIGIN, "localhost")
}

/// The material after `a`'s credential update, or `material` unchanged.
fn updated(a: &CheckedAssertion, material: &[u8]) -> Vec<u8> {
    a.credential_update
        .as_ref()
        .map_or_else(|| material.to_vec(), |u| u.public_material.clone())
}

// ---------------------------------------------------------------------------

#[test]
fn a_synchronized_passkey_with_counter_zero_registers_and_authenticates_repeatedly() {
    let v = verifier();
    let f = Fixture::new(1);
    let reg = register(&v, &f, SYNCED, 0).unwrap();
    assert_eq!(reg.algorithm(), ES256);
    let creds = active(reg.credential_id(), reg.public_material());
    for _ in 0..3 {
        let a = assert_ok(&v, &f, &creds, SYNCED, 0).unwrap();
        assert_eq!(a.credential_id, reg.credential_id());
        assert_eq!(a.credential_update, None, "counter 0 stays 0");
    }
}

#[test]
fn a_device_bound_counter_must_increase() {
    let v = verifier();
    let f = Fixture::new(2);
    let reg = register(&v, &f, BOUND, 5).unwrap();
    let creds = active(reg.credential_id(), reg.public_material());
    // A higher counter is accepted and returned as a credential update.
    let a = assert_ok(&v, &f, &creds, BOUND, 6).unwrap();
    let material = updated(&a, reg.public_material());
    assert_ne!(material, reg.public_material(), "counter update persisted");
    let creds = active(reg.credential_id(), &material);
    // Equal or lower than the stored counter: possible clone, refused.
    for counter in [6, 3, 0] {
        assert_eq!(
            assert_ok(&v, &f, &creds, BOUND, counter).err(),
            Some(VerifierError::CredentialStateRejected),
            "{counter}"
        );
    }
    assert!(assert_ok(&v, &f, &creds, BOUND, 7).is_ok());
}

#[test]
fn backup_flags_must_stay_consistent_with_the_registered_credential() {
    let v = verifier();
    // A device-bound credential claiming to be backed up without eligibility.
    let f = Fixture::new(3);
    let reg = register(&v, &f, BOUND, 1).unwrap();
    let creds = active(reg.credential_id(), reg.public_material());
    for flags in [UP | UV | BS, SYNCED] {
        assert_eq!(
            assert_ok(&v, &f, &creds, flags, 2).err(),
            Some(VerifierError::CredentialStateRejected),
            "{flags:#x}"
        );
    }
    // The library's passkey flow allows a device-bound credential to become backup
    // eligible (webauthn-rs `allow_backup_eligible_upgrade` for passkeys); the change is
    // returned as a credential update for the ledger to persist, after which it may be
    // backed up.
    let a = assert_ok(&v, &f, &creds, UP | UV | BE, 2).unwrap();
    let material = updated(&a, reg.public_material());
    assert_ne!(material, reg.public_material());
    let creds = active(reg.credential_id(), &material);
    let a = assert_ok(&v, &f, &creds, SYNCED, 3).unwrap();
    assert!(a.credential_update.is_some());
    // A synchronized credential that loses backup eligibility.
    let g = Fixture::new(4);
    let reg = register(&v, &g, SYNCED, 0).unwrap();
    let creds = active(reg.credential_id(), reg.public_material());
    assert_eq!(
        assert_ok(&v, &g, &creds, BOUND, 0).err(),
        Some(VerifierError::CredentialStateRejected)
    );
    // Backed up -> not backed up while still eligible is a state change, not a refusal.
    let a = assert_ok(&v, &g, &creds, UP | UV | BE, 0).unwrap();
    assert!(a.credential_update.is_some());
    // Registration claiming backed up without eligibility is refused.
    assert!(register(&v, &Fixture::new(5), UP | UV | BS, 0).is_err());
}

#[test]
fn missing_user_verification_or_presence_is_refused() {
    let v = verifier();
    let f = Fixture::new(6);
    let reg = register(&v, &f, SYNCED, 0).unwrap();
    let creds = active(reg.credential_id(), reg.public_material());
    assert_eq!(
        assert_ok(&v, &f, &creds, UP | BE | BS, 0).err(),
        Some(VerifierError::UserNotVerified)
    );
    assert_eq!(
        assert_ok(&v, &f, &creds, UV | BE | BS, 0).err(),
        Some(VerifierError::UserNotVerified)
    );
    // A registration without user verification never registers.
    assert_eq!(
        register(&v, &Fixture::new(7), UP | BE | BS, 0).err(),
        Some(VerifierError::UserNotVerified)
    );
}

#[test]
fn wrong_origin_rp_id_signature_or_credential_is_refused() {
    let v = verifier();
    let f = Fixture::new(8);
    let reg = register(&v, &f, SYNCED, 0).unwrap();
    let creds = active(reg.credential_id(), reg.public_material());
    for origin in [
        "http://localhost:9999",
        "https://localhost:8443",
        "http://evil.example",
    ] {
        assert_eq!(
            assert_with(&v, &f, &creds, SYNCED, 0, origin, "localhost").err(),
            Some(VerifierError::BindingMismatch),
            "{origin}"
        );
    }
    assert_eq!(
        assert_with(&v, &f, &creds, SYNCED, 0, ORIGIN, "evil.example").err(),
        Some(VerifierError::BindingMismatch)
    );
    // Another key signs for the registered credential id.
    let impostor = Fixture {
        key: SigningKey::from_slice(&[9; 32]).unwrap(),
        credential_id: f.credential_id.clone(),
    };
    assert_eq!(
        assert_ok(&v, &impostor, &creds, SYNCED, 0).err(),
        Some(VerifierError::SignatureInvalid)
    );
    // An assertion verified for one ceremony, finished with another subject's credentials.
    let start = v.begin(&bob(), &creds).unwrap();
    let response = f.assertion(&start.client_options, SYNCED, 0, ORIGIN, "localhost");
    let other = Fixture::new(10);
    let other_reg = register(&v, &other, SYNCED, 0).unwrap();
    let other_creds = active(other_reg.credential_id(), other_reg.public_material());
    assert!(channel(&v)
        .finish(&start.state, &response, &other_creds)
        .is_err());
    // Replay of a finished assertion against a new ceremony.
    assert!(channel(&v).finish(&start.state, &response, &creds).is_ok());
    let next = v.begin(&bob(), &creds).unwrap();
    assert_eq!(
        channel(&v).finish(&next.state, &response, &creds).err(),
        Some(VerifierError::BindingMismatch)
    );
}
