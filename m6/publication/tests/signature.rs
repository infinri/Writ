//! Public signature boundary and independent interoperability vectors.

#![allow(clippy::unwrap_used, clippy::expect_used, clippy::panic, missing_docs)]

use writ_kernel::json::{self, Map, Value};
use writ_kernel::time::Timestamp;
use writ_policy_publication_lab::{
    sign, verify, Error, Protected, Purpose, Scope, SignedPolicy, Signer, VerificationKey,
    VerificationSource,
};

const AUTHORIZATION: &str = include_str!("../contracts/authorization-signing-vector.json");
const ACCEPTANCE: &str = include_str!("../contracts/signing-vector.json");
const SEED: &str = "9d61b19deffd5a60ba844af492ec2cc44449c5697b326919703bac031cae7f60";

fn at(text: &str) -> Timestamp {
    Timestamp::parse_strict(text).unwrap()
}

fn hex(text: &str) -> Vec<u8> {
    (0..text.len())
        .step_by(2)
        .map(|i| u8::from_str_radix(&text[i..i + 2], 16).unwrap())
        .collect()
}

fn as_mut_map(value: &mut Value) -> &mut Map {
    match value {
        Value::Obj(map) => map,
        _ => panic!("test fixture must have an object here"),
    }
}

fn set(value: &mut Value, field: &str, new: Value) {
    as_mut_map(value).insert(field.into(), new);
}

fn header(document: &mut SignedPolicy) -> &mut Value {
    as_mut_map(&mut document.envelope)
        .get_mut("protected")
        .unwrap()
}

struct CurrentKey(Option<VerificationKey>);

impl VerificationSource for CurrentKey {
    fn resolve(&self, _key_id: &str) -> Result<VerificationKey, Error> {
        self.0.clone().ok_or(Error::Trust)
    }
}

struct FixtureSigner(ed25519_dalek::SigningKey);

impl Signer for FixtureSigner {
    fn sign(&self, _: &Protected, bytes: &[u8]) -> Result<[u8; 64], Error> {
        use ed25519_dalek::Signer as _;
        Ok(self.0.sign(bytes).to_bytes())
    }
}

struct Vector {
    document: SignedPolicy,
    protected: Protected,
    key: VerificationKey,
    now: Timestamp,
    signing_bytes: Vec<u8>,
    payload_bytes: String,
}

impl Vector {
    fn read(fixture: &str, purpose: Purpose) -> Self {
        let value = json::parse(fixture.as_bytes()).unwrap();
        let document = SignedPolicy {
            payload: value.get("payload").unwrap().clone(),
            envelope: value.get("envelope").unwrap().clone(),
        };
        let protected = Protected::from_value(document.envelope.get("protected").unwrap()).unwrap();
        let key = VerificationKey {
            key_id: protected.key_id().to_owned(),
            purpose,
            scope: Scope::new("m6-lab", "offline-proof").unwrap(),
            public_key: hex(value.get("public_key_hex").unwrap().as_str().unwrap())
                .try_into()
                .unwrap(),
            not_before: at("2026-11-19T11:30:00Z"),
            expires_at: at("2026-11-19T13:30:00Z"),
            revoked: false,
        };
        Self {
            document,
            protected,
            key,
            now: at("2026-11-19T12:05:00Z"),
            signing_bytes: hex(value.get("signing_bytes_hex").unwrap().as_str().unwrap()),
            payload_bytes: value
                .get("canonical_payload_utf8")
                .unwrap()
                .as_str()
                .unwrap()
                .into(),
        }
    }

    fn verify(&self, document: &SignedPolicy) -> Result<(), Error> {
        verify(
            document,
            self.key.purpose,
            &self.key.scope,
            self.now,
            &CurrentKey(Some(self.key.clone())),
        )
    }

    fn verify_key(&self, key: VerificationKey) -> Result<(), Error> {
        verify(
            &self.document,
            self.key.purpose,
            &self.key.scope,
            self.now,
            &CurrentKey(Some(key)),
        )
    }
}

#[test]
fn independent_ed25519_vectors_define_both_purposes_and_exact_signed_bytes() {
    for (fixture, purpose) in [
        (AUTHORIZATION, Purpose::Authorization),
        (ACCEPTANCE, Purpose::Acceptance),
    ] {
        let vector = Vector::read(fixture, purpose);
        assert_eq!(vector.protected.purpose(), purpose);
        assert_eq!(vector.protected.scope(), &vector.key.scope);
        assert_eq!(
            vector.protected.signing_bytes().unwrap(),
            vector.signing_bytes
        );
        assert_eq!(
            json::canonical(&vector.document.payload).unwrap(),
            vector.payload_bytes
        );
        assert_eq!(vector.verify(&vector.document), Ok(()));
        let built = Protected::new(
            purpose,
            vector.key.scope,
            &vector.document.payload,
            &vector.key.key_id,
            at("2026-11-19T12:00:00Z"),
            at("2026-11-19T13:00:00Z"),
        )
        .unwrap();
        assert_eq!(built, vector.protected);
        let signer = FixtureSigner(ed25519_dalek::SigningKey::from_bytes(
            &hex(SEED).try_into().unwrap(),
        ));
        assert_eq!(
            sign(vector.document.payload.clone(), &built, &signer).unwrap(),
            vector.document
        );
    }
}

#[test]
fn payloads_and_signatures_are_bound_and_signer_checks_the_digest_first() {
    let vector = Vector::read(ACCEPTANCE, Purpose::Acceptance);
    let mut changed = vector.document.clone();
    set(&mut changed.payload, "policy_version", json::s("2"));
    assert_eq!(vector.verify(&changed), Err(Error::Signature));

    struct NeverSigns;
    impl Signer for NeverSigns {
        fn sign(&self, _: &Protected, _: &[u8]) -> Result<[u8; 64], Error> {
            panic!("a different payload cannot reach the signer");
        }
    }
    assert_eq!(
        sign(changed.payload, &vector.protected, &NeverSigns),
        Err(Error::Signature)
    );

    let mut bad_signature = vector.document.clone();
    set(
        &mut bad_signature.envelope,
        "signature",
        json::s(&"00".repeat(64)),
    );
    assert_eq!(vector.verify(&bad_signature), Err(Error::Signature));
    set(
        &mut bad_signature.envelope,
        "signature",
        json::s(&"AA".repeat(64)),
    );
    assert_eq!(vector.verify(&bad_signature), Err(Error::Format));
    set(
        &mut bad_signature.envelope,
        "signature",
        json::s(&"aa".repeat(63)),
    );
    assert_eq!(vector.verify(&bad_signature), Err(Error::Format));
}

#[test]
fn protected_metadata_mutations_and_cross_purpose_or_scope_substitution_refuse() {
    let vector = Vector::read(ACCEPTANCE, Purpose::Acceptance);
    for (field, value, error) in [
        ("version", "writ-detached-policy-signature/2", Error::Format),
        ("algorithm", "Ed25519ph", Error::Format),
        ("purpose", "authorization", Error::Format),
        ("payload_format", "writ-acceptance-policy/0", Error::Format),
        ("not_before", "2026-11-19T11:00:00Z", Error::Signature),
        ("expires_at", "2026-11-19T13:01:00Z", Error::Signature),
        ("not_before", "2026-11-19T12:00:00+00:00", Error::Format),
        ("expires_at", "2026-11-19T12:00:00Z", Error::Format),
    ] {
        let mut changed = vector.document.clone();
        set(header(&mut changed), field, json::s(value));
        assert_eq!(vector.verify(&changed), Err(error), "{field}");
    }
    assert_eq!(
        verify(
            &vector.document,
            Purpose::Authorization,
            &vector.key.scope,
            vector.now,
            &CurrentKey(Some(vector.key.clone()))
        ),
        Err(Error::Signature)
    );
    let scope = Scope::new("m6-lab", "other-project").unwrap();
    assert_eq!(
        verify(
            &vector.document,
            vector.key.purpose,
            &scope,
            vector.now,
            &CurrentKey(Some(vector.key.clone()))
        ),
        Err(Error::Scope)
    );

    let mut changed = vector.document.clone();
    let mut key = vector.key.clone();
    key.key_id = "second-trusted-id".into();
    set(
        header(&mut changed),
        "publisher_key_id",
        json::s(&key.key_id),
    );
    assert_eq!(
        verify(
            &changed,
            key.purpose,
            &key.scope,
            vector.now,
            &CurrentKey(Some(key.clone()))
        ),
        Err(Error::Signature)
    );

    let mut changed = vector.document.clone();
    key.key_id = vector.key.key_id.clone();
    key.scope = scope;
    set(header(&mut changed), "scope", key.scope.to_value());
    assert_eq!(
        verify(
            &changed,
            key.purpose,
            &key.scope,
            vector.now,
            &CurrentKey(Some(key.clone()))
        ),
        Err(Error::Signature)
    );
}

#[test]
fn contracts_reject_extras_and_inline_root_presentation() {
    let vector = Vector::read(AUTHORIZATION, Purpose::Authorization);
    let mut changed = vector.document.clone();
    set(
        &mut changed.envelope,
        "public_key",
        json::s(&"00".repeat(32)),
    );
    assert_eq!(vector.verify(&changed), Err(Error::Format));
    let mut changed = vector.document.clone();
    set(
        header(&mut changed),
        "public_key",
        json::s(&"00".repeat(32)),
    );
    assert_eq!(vector.verify(&changed), Err(Error::Format));
    let mut changed = vector.document;
    set(&mut changed.payload, "publisher_signature", Value::Null);
    assert_eq!(
        Protected::new(
            Purpose::Authorization,
            vector.key.scope,
            &changed.payload,
            &vector.key.key_id,
            at("2026-11-19T12:00:00Z"),
            at("2026-11-19T13:00:00Z")
        ),
        Err(Error::Format)
    );

    let vector = Vector::read(ACCEPTANCE, Purpose::Acceptance);
    let impostor = FixtureSigner(ed25519_dalek::SigningKey::from_bytes(&[7; 32]));
    let self_signed = sign(
        vector.document.payload.clone(),
        &vector.protected,
        &impostor,
    )
    .unwrap();
    assert_eq!(vector.verify(&self_signed), Err(Error::Signature));
    assert_eq!(
        verify(
            &self_signed,
            vector.key.purpose,
            &vector.key.scope,
            vector.now,
            &CurrentKey(None)
        ),
        Err(Error::Trust)
    );
}

#[test]
fn only_current_exact_public_key_records_work() {
    let vector = Vector::read(ACCEPTANCE, Purpose::Acceptance);
    for key in [
        VerificationKey {
            revoked: true,
            ..vector.key.clone()
        },
        VerificationKey {
            key_id: "different-key".into(),
            ..vector.key.clone()
        },
        VerificationKey {
            purpose: Purpose::Authorization,
            ..vector.key.clone()
        },
        VerificationKey {
            scope: Scope::new("other-tenant", "offline-proof").unwrap(),
            ..vector.key.clone()
        },
        VerificationKey {
            public_key: [0; 32],
            ..vector.key.clone()
        },
    ] {
        assert_eq!(vector.verify_key(key), Err(Error::Trust));
    }
    assert_eq!(
        verify(
            &vector.document,
            vector.key.purpose,
            &vector.key.scope,
            vector.now,
            &CurrentKey(None)
        ),
        Err(Error::Trust)
    );

    let key = VerificationKey {
        expires_at: vector.now,
        ..vector.key.clone()
    };
    assert_eq!(vector.verify_key(key), Err(Error::Time));
    let key = VerificationKey {
        not_before: vector.now.plus_seconds(1),
        ..vector.key.clone()
    };
    assert_eq!(vector.verify_key(key), Err(Error::Time));
    let key = VerificationKey {
        not_before: vector.now,
        ..vector.key.clone()
    };
    assert_eq!(vector.verify_key(key), Ok(()));
    for (now, result) in [
        (at("2026-11-19T11:59:59Z"), Err(Error::Time)),
        (at("2026-11-19T12:00:00Z"), Ok(())),
        (at("2026-11-19T13:00:00Z"), Err(Error::Time)),
    ] {
        assert_eq!(
            verify(
                &vector.document,
                vector.key.purpose,
                &vector.key.scope,
                now,
                &CurrentKey(Some(vector.key.clone()))
            ),
            result
        );
    }
}
