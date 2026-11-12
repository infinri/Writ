//! Sealed ingress (D-010) and the request fingerprint (D-012).
//!
//! A trusted host adapter verifies the principal's proof, computes the action
//! digest and seals the complete normalized ActionRequest. The broker accepts
//! a request only through [`verify_ingress`], which is the sole constructor of
//! [`VerifiedRequest`]; every later step (use-time decision, permit issue)
//! takes a `VerifiedRequest`, so an unsealed request cannot reach them.
//!
//! Seal (m1/fixtures/broker/BROKER.md): `{"adapter_id", "mac"}` where `mac`
//! is the hex HMAC-SHA256 under the adapter's registered ingress key of the
//! D-006 canonical bytes of the request. It is valid only if the adapter is
//! registered, the MAC matches and `adapter_id` equals
//! `request.principal.verified_by`. Every failure is
//! [`Refusal::IngressUnauthenticated`].

use crate::keys::{IngressKey, KeyRing};
use crate::mac;
use crate::refusal::Refusal;
use std::fmt;
use writ_kernel::json::{self, Digest, Value};

/// D-012 request fingerprint: the D-006 digest (`sha256:<hex>`) of the
/// complete canonical ActionRequest. Every member is bound; member order is not
/// significant (canonical form), array order is.
#[derive(Clone, Debug, PartialEq, Eq, Hash)]
pub struct RequestFingerprint(Digest);

impl RequestFingerprint {
    /// Fingerprint of `request`; `None` when the value is outside the D-006 domain.
    pub fn of(request: &Value) -> Option<RequestFingerprint> {
        json::digest(request).ok().map(RequestFingerprint)
    }

    /// The `sha256:<hex>` text.
    pub fn as_str(&self) -> &str {
        self.0.as_str()
    }
}

impl fmt::Display for RequestFingerprint {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(self.as_str())
    }
}

/// An ingress seal as presented with a request.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Seal {
    adapter_id: String,
    mac: String,
}

impl Seal {
    /// Seal `request` as adapter `adapter_id` (the trusted host adapter's
    /// side). `None` when the request is outside the D-006 domain.
    pub fn create(request: &Value, adapter_id: &str, key: &IngressKey) -> Option<Seal> {
        let bytes = json::canonical(request).ok()?;
        Some(Seal {
            adapter_id: adapter_id.to_string(),
            mac: mac::sign_hex(key.bytes(), bytes.as_bytes())?,
        })
    }

    /// A seal from its wire form: exactly the members `adapter_id` and `mac`,
    /// both strings; anything else is `None`.
    pub fn from_value(value: &Value) -> Option<Seal> {
        let members = value.as_obj()?;
        if members.len() != 2 {
            return None;
        }
        Some(Seal {
            adapter_id: members.get("adapter_id")?.as_str()?.to_string(),
            mac: members.get("mac")?.as_str()?.to_string(),
        })
    }

    /// The wire form `{"adapter_id", "mac"}`.
    pub fn to_value(&self) -> Value {
        json::obj([
            ("adapter_id", json::s(&self.adapter_id)),
            ("mac", json::s(&self.mac)),
        ])
    }

    /// The adapter that claims to have made the seal.
    pub fn adapter_id(&self) -> &str {
        &self.adapter_id
    }
}

/// A request whose seal verified: it is byte-for-byte (canonically) the
/// request a registered adapter sealed, and its principal was verified by that
/// adapter. It is not yet authorized.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct VerifiedRequest {
    request: Value,
    fingerprint: RequestFingerprint,
    adapter_id: String,
}

impl VerifiedRequest {
    /// The sealed request.
    pub fn request(&self) -> &Value {
        &self.request
    }

    /// Its D-012 fingerprint.
    pub fn fingerprint(&self) -> &RequestFingerprint {
        &self.fingerprint
    }

    /// The adapter whose seal verified (equal to `principal.verified_by`).
    pub fn adapter_id(&self) -> &str {
        &self.adapter_id
    }
}

/// Verify the seal presented with `request` (`None` when absent) against the
/// currently registered keys in `keys`.
pub fn verify_ingress(
    request: &Value,
    seal: Option<&Value>,
    keys: &dyn KeyRing,
) -> Result<VerifiedRequest, Refusal> {
    let refuse = Refusal::IngressUnauthenticated;
    let seal = seal.and_then(Seal::from_value).ok_or(refuse.clone())?;
    let key = keys.ingress_key(&seal.adapter_id).ok_or(refuse.clone())?;
    let bytes = json::canonical(request).map_err(|_| refuse.clone())?;
    if !mac::verify_hex(key.bytes(), bytes.as_bytes(), &seal.mac) {
        return Err(refuse);
    }
    let verified_by = request
        .get("principal")
        .and_then(|p| p.get("verified_by"))
        .and_then(Value::as_str);
    if verified_by != Some(seal.adapter_id.as_str()) {
        return Err(refuse);
    }
    Ok(VerifiedRequest {
        fingerprint: RequestFingerprint(Digest::of_bytes(bytes.as_bytes())),
        request: request.clone(),
        adapter_id: seal.adapter_id,
    })
}

#[cfg(test)]
#[allow(clippy::unwrap_used, clippy::expect_used, clippy::panic)]
mod tests {
    use super::*;
    use crate::keys::MemoryKeyRing;
    use crate::test_support::*;

    fn refused(r: Result<VerifiedRequest, Refusal>) -> bool {
        r == Err(Refusal::IngressUnauthenticated)
    }

    #[test]
    fn valid_seal_verifies_and_binds_the_fingerprint() {
        let ring = fixture_ring();
        let req = base_request();
        let seal = sealed(&req, FIXTURE_ADAPTER, &FIXTURE_KEY);
        let v = verify_ingress(&req, Some(&seal), &ring).unwrap();
        assert_eq!(v.adapter_id(), FIXTURE_ADAPTER);
        assert_eq!(v.request(), &req);
        assert_eq!(v.fingerprint(), &RequestFingerprint::of(&req).unwrap());
        assert_eq!(
            v.fingerprint().as_str(),
            json::digest(&req).unwrap().as_str()
        );
    }

    #[test]
    fn seal_mac_is_hmac_sha256_of_the_canonical_request_bytes() {
        let req = base_request();
        let seal = Seal::create(&req, FIXTURE_ADAPTER, &ingress_key(FIXTURE_KEY)).unwrap();
        let expected =
            mac::sign_hex(&FIXTURE_KEY, json::canonical(&req).unwrap().as_bytes()).unwrap();
        assert_eq!(
            seal.to_value(),
            json::obj([
                ("adapter_id", json::s(FIXTURE_ADAPTER)),
                ("mac", json::s(&expected))
            ])
        );
        assert_eq!(Seal::from_value(&seal.to_value()), Some(seal));
    }

    #[test]
    fn absent_seal_is_unauthenticated() {
        assert!(refused(verify_ingress(
            &base_request(),
            None,
            &fixture_ring()
        )));
    }

    #[test]
    fn malformed_seals_are_unauthenticated() {
        let req = base_request();
        let good = sealed(&req, FIXTURE_ADAPTER, &FIXTURE_KEY);
        let mac = good.get("mac").unwrap().clone();
        for seal in [
            Value::Null,
            json::s("seal"),
            Value::Arr(vec![]),
            json::obj([("adapter_id", json::s(FIXTURE_ADAPTER))]),
            json::obj([("mac", mac.clone())]),
            json::obj([("adapter_id", Value::Int(1)), ("mac", mac.clone())]),
            json::obj([
                ("adapter_id", json::s(FIXTURE_ADAPTER)),
                ("mac", Value::Null),
            ]),
            json::obj([
                ("adapter_id", json::s(FIXTURE_ADAPTER)),
                ("mac", mac.clone()),
                ("extra", Value::Null),
            ]),
        ] {
            assert!(
                refused(verify_ingress(&req, Some(&seal), &fixture_ring())),
                "{}",
                seal.to_json_text()
            );
        }
    }

    #[test]
    fn forged_seal_is_unauthenticated() {
        let req = base_request();
        for mac in [
            "00".to_string(),
            "0".repeat(64),
            mac::sign_hex(&[0x99; 32], json::canonical(&req).unwrap().as_bytes()).unwrap(),
        ] {
            let seal = json::obj([
                ("adapter_id", json::s(FIXTURE_ADAPTER)),
                ("mac", json::s(&mac)),
            ]);
            assert!(refused(verify_ingress(&req, Some(&seal), &fixture_ring())));
        }
        let mut flipped = sealed(&req, FIXTURE_ADAPTER, &FIXTURE_KEY);
        if let Value::Obj(m) = &mut flipped {
            let tag = m.get("mac").and_then(Value::as_str).unwrap().to_string();
            let first = if tag.starts_with('0') { "1" } else { "0" };
            m.insert("mac".into(), json::s(&format!("{first}{}", &tag[1..])));
        }
        assert!(refused(verify_ingress(
            &req,
            Some(&flipped),
            &fixture_ring()
        )));
    }

    #[test]
    fn unregistered_adapter_seal_is_unauthenticated() {
        // The request names the rogue adapter as its verifier and the rogue key
        // seals it correctly, but the rogue adapter is not registered.
        let req = with(
            &base_request(),
            &["principal", "verified_by"],
            json::s(ROGUE_ADAPTER),
        );
        let seal = sealed(&req, ROGUE_ADAPTER, &ROGUE_KEY);
        assert!(refused(verify_ingress(&req, Some(&seal), &fixture_ring())));
    }

    #[test]
    fn other_adapter_seal_is_unauthenticated() {
        // A registered adapter vouching for a principal verified by another adapter.
        let req = base_request();
        let seal = sealed(&req, OTHER_ADAPTER, &OTHER_KEY);
        assert!(refused(verify_ingress(&req, Some(&seal), &fixture_ring())));
        // The other adapter's id with the fixture adapter's key does not verify either.
        let mut mixed = sealed(&req, FIXTURE_ADAPTER, &FIXTURE_KEY);
        if let Value::Obj(m) = &mut mixed {
            m.insert("adapter_id".into(), json::s(OTHER_ADAPTER));
        }
        assert!(refused(verify_ingress(&req, Some(&mixed), &fixture_ring())));
    }

    #[test]
    fn request_without_verified_by_is_unauthenticated_even_if_sealed() {
        let req = without(&base_request(), &["principal", "verified_by"]);
        let seal = sealed(&req, FIXTURE_ADAPTER, &FIXTURE_KEY);
        assert!(refused(verify_ingress(&req, Some(&seal), &fixture_ring())));
    }

    #[test]
    fn altered_after_sealing_is_unauthenticated() {
        let req = base_request();
        let seal = sealed(&req, FIXTURE_ADAPTER, &FIXTURE_KEY);
        for (path, value) in [
            (
                &["action", "resource", "canonical_resource_id"][..],
                json::s("workspace:fixture-tenant/fixture-project/src/other.py"),
            ),
            (&["action", "kind"][..], json::s("file.read")),
            (&["action", "action_digest"][..], json::s(&"b".repeat(71))),
            (&["principal", "id"][..], json::s("agent:someone-else")),
            (
                &["principal", "roles"][..],
                Value::Arr(vec![json::s("agent"), json::s("operator")]),
            ),
            (&["tenant_id"][..], json::s("other-tenant")),
            (&["idempotency_key"][..], json::s("idem-2")),
            (&["trace_id"][..], json::s("trace-2")),
            (&["deadline_at"][..], json::s("2026-11-05T12:00:10Z")),
            (&["expected_state_revision"][..], Value::Int(5)),
            (
                &["action", "attributes"][..],
                json::obj([("note", json::s("x"))]),
            ),
        ] {
            let altered = with(&req, path, value);
            assert!(
                refused(verify_ingress(&altered, Some(&seal), &fixture_ring())),
                "{path:?}"
            );
        }
        // Removing a member or adding one is an alteration too.
        let removed = without(&req, &["agent_id"]);
        assert!(refused(verify_ingress(
            &removed,
            Some(&seal),
            &fixture_ring()
        )));
    }

    #[test]
    fn member_order_is_not_an_alteration() {
        // Canonical bytes are order-independent: a reordered serialization of the
        // sealed request is the same request.
        let req = base_request();
        let seal = sealed(&req, FIXTURE_ADAPTER, &FIXTURE_KEY);
        let reordered = json::parse(reorder_text(&req).as_bytes()).unwrap();
        assert!(verify_ingress(&reordered, Some(&seal), &fixture_ring()).is_ok());
    }

    #[test]
    fn empty_key_ring_refuses_every_seal() {
        let req = base_request();
        let seal = sealed(&req, FIXTURE_ADAPTER, &FIXTURE_KEY);
        assert!(refused(verify_ingress(
            &req,
            Some(&seal),
            &MemoryKeyRing::new()
        )));
    }

    #[test]
    fn fingerprint_binds_every_member_but_not_member_order() {
        let req = base_request();
        let fp = RequestFingerprint::of(&req).unwrap();
        let reordered = json::parse(reorder_text(&req).as_bytes()).unwrap();
        assert_eq!(RequestFingerprint::of(&reordered).unwrap(), fp);
        let members: Vec<String> = req.as_obj().unwrap().keys().cloned().collect();
        for name in members {
            let changed = with(&req, &[name.as_str()], json::s("changed"));
            assert_ne!(RequestFingerprint::of(&changed).unwrap(), fp, "{name}");
            let removed = without(&req, &[name.as_str()]);
            assert_ne!(RequestFingerprint::of(&removed).unwrap(), fp, "{name}");
        }
        let added = with(&req, &["action", "attributes", "x"], Value::Int(1));
        assert_ne!(RequestFingerprint::of(&added).unwrap(), fp);
        assert!(RequestFingerprint::of(&Value::Int(json::MAX_SAFE_INTEGER + 1)).is_none());
    }
}
