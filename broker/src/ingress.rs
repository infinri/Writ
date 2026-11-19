//! Sealed ingress (D-010) and the request fingerprint (D-012).
//!
//! A trusted host adapter verifies the principal's proof, computes the action
//! digest and seals the complete normalized ActionRequest. The broker accepts
//! a request only through [`verify_ingress`] or [`verify_ingress_keyed`],
//! which are the sole constructors of [`VerifiedRequest`]; every later step
//! (use-time decision, permit issue) takes a `VerifiedRequest`, so an unsealed
//! request cannot reach them.
//!
//! Seal `seal/1` (m1/fixtures/broker/BROKER.md): `{"adapter_id", "mac"}`
//! where `mac` is the hex HMAC-SHA256 under the adapter's registered ingress
//! key of the D-006 canonical bytes of the request. It is valid only if the
//! adapter is registered, the MAC matches and `adapter_id` equals
//! `request.principal.verified_by`. Every failure is
//! [`Refusal::IngressUnauthenticated`].
//!
//! Seal `seal/2` (M5 J-3, D-045 M5-D6): `{"adapter_id", "key_id", "mac"}`,
//! all strings, where `key_id` is a [`KeyId`] of purpose `ingress` and `mac`
//! is the hex HMAC-SHA256 under that key of the D-006 canonical bytes of
//! `{"adapter_id", "contract_version": "seal/2", "key_id", "request"}`, so the
//! key id, the adapter id and the version are authenticated with the request.
//! [`verify_ingress_keyed`] accepts `seal/2` only: the key must be active in
//! the [`KeySource`] at trusted time, bound to the adapter in
//! [`IngressBindings`], and `adapter_id` must equal
//! `request.principal.verified_by`. [`verify_ingress`] accepts `seal/1` only,
//! so a service that runs with the file key source refuses every `seal/1`.
//! The ledger revocation lookup for the sealing key runs inside T-reserve and
//! T-dispatch ([`VerifiedRequest::seal_key`] names the key and its material).

use crate::keys::{IngressKey, KeyId, KeyPurpose, KeyRing, KeySource, KeyStatus, MaterialDigest};
use crate::mac;
use crate::refusal::Refusal;
use std::collections::{BTreeMap, BTreeSet};
use std::fmt;
use writ_kernel::json::{self, Digest, Value};
use writ_kernel::time::Timestamp;

/// `contract_version` inside the MACed bytes of a keyed seal (J-3).
pub const SEAL_CONTRACT_V2: &str = "seal/2";

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
    /// `Some` for `seal/2`, `None` for `seal/1`.
    key_id: Option<String>,
    mac: String,
}

fn keyed_bytes(request: &Value, adapter_id: &str, key_id: &str) -> Option<String> {
    json::canonical(&json::obj([
        ("adapter_id", json::s(adapter_id)),
        ("contract_version", json::s(SEAL_CONTRACT_V2)),
        ("key_id", json::s(key_id)),
        ("request", request.clone()),
    ]))
    .ok()
}

impl Seal {
    /// Seal `request` as adapter `adapter_id` (`seal/1`, the trusted host
    /// adapter's side). `None` when the request is outside the D-006 domain.
    pub fn create(request: &Value, adapter_id: &str, key: &IngressKey) -> Option<Seal> {
        let bytes = json::canonical(request).ok()?;
        Some(Seal {
            adapter_id: adapter_id.to_string(),
            key_id: None,
            mac: mac::sign_hex(key.bytes(), bytes.as_bytes())?,
        })
    }

    /// Seal `request` as adapter `adapter_id` with ingress key `key_id`
    /// (`seal/2`). `None` when the request is outside the D-006 domain.
    pub fn create_keyed(
        request: &Value,
        adapter_id: &str,
        key_id: &KeyId,
        key: &IngressKey,
    ) -> Option<Seal> {
        let bytes = keyed_bytes(request, adapter_id, key_id.as_str())?;
        Some(Seal {
            adapter_id: adapter_id.to_string(),
            key_id: Some(key_id.as_str().to_string()),
            mac: mac::sign_hex(key.bytes(), bytes.as_bytes())?,
        })
    }

    /// A seal from its wire form: exactly `adapter_id` and `mac` (`seal/1`)
    /// or exactly `adapter_id`, `key_id` and `mac` (`seal/2`), all strings;
    /// anything else is `None`.
    pub fn from_value(value: &Value) -> Option<Seal> {
        let members = value.as_obj()?;
        let key_id = match members.len() {
            2 => None,
            3 => Some(members.get("key_id")?.as_str()?.to_string()),
            _ => return None,
        };
        Some(Seal {
            adapter_id: members.get("adapter_id")?.as_str()?.to_string(),
            key_id,
            mac: members.get("mac")?.as_str()?.to_string(),
        })
    }

    /// The wire form `{"adapter_id", "mac"}`, plus `key_id` for `seal/2`.
    pub fn to_value(&self) -> Value {
        let mut v = json::obj([
            ("adapter_id", json::s(&self.adapter_id)),
            ("mac", json::s(&self.mac)),
        ]);
        if let (Some(key_id), Value::Obj(m)) = (&self.key_id, &mut v) {
            m.insert("key_id".into(), json::s(key_id));
        }
        v
    }

    /// The adapter that claims to have made the seal.
    pub fn adapter_id(&self) -> &str {
        &self.adapter_id
    }

    /// The ingress key id as presented (`seal/2`); `None` for `seal/1`.
    pub fn key_id(&self) -> Option<&str> {
        self.key_id.as_deref()
    }
}

/// Which ingress key ids each registered adapter may seal with (service
/// configuration, J-6). An adapter with no bound key cannot seal `seal/2`.
#[derive(Clone, Debug, Default, PartialEq, Eq)]
pub struct IngressBindings {
    keys: BTreeMap<String, BTreeSet<KeyId>>,
}

impl IngressBindings {
    /// No bindings: every keyed seal refuses.
    pub fn new() -> IngressBindings {
        IngressBindings::default()
    }

    /// Allow `adapter_id` to seal with `key_id`; `false` (nothing bound) when
    /// `key_id` is not an ingress key id.
    pub fn bind(&mut self, adapter_id: &str, key_id: KeyId) -> bool {
        if key_id.purpose() != KeyPurpose::Ingress {
            return false;
        }
        self.keys
            .entry(adapter_id.to_string())
            .or_default()
            .insert(key_id);
        true
    }

    /// `true` when `adapter_id` may seal with `key_id`.
    pub fn allows(&self, adapter_id: &str, key_id: &KeyId) -> bool {
        self.keys
            .get(adapter_id)
            .is_some_and(|keys| keys.contains(key_id))
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
    seal_key: Option<(KeyId, MaterialDigest)>,
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

    /// The ingress key id and material digest of a `seal/2` seal (for the
    /// revocation lookup inside T-reserve and T-dispatch); `None` for `seal/1`.
    pub fn seal_key(&self) -> Option<(&KeyId, &MaterialDigest)> {
        self.seal_key.as_ref().map(|(k, d)| (k, d))
    }
}

fn verified_by_matches(request: &Value, adapter_id: &str) -> bool {
    request
        .get("principal")
        .and_then(|p| p.get("verified_by"))
        .and_then(Value::as_str)
        == Some(adapter_id)
}

/// Verify the `seal/1` seal presented with `request` (`None` when absent)
/// against the currently registered keys in `keys`. A `seal/2` seal is
/// refused here.
pub fn verify_ingress(
    request: &Value,
    seal: Option<&Value>,
    keys: &dyn KeyRing,
) -> Result<VerifiedRequest, Refusal> {
    let refuse = Refusal::IngressUnauthenticated;
    let seal = seal.and_then(Seal::from_value).ok_or(refuse.clone())?;
    if seal.key_id.is_some() {
        return Err(refuse);
    }
    let key = keys.ingress_key(&seal.adapter_id).ok_or(refuse.clone())?;
    let bytes = json::canonical(request).map_err(|_| refuse.clone())?;
    if !mac::verify_hex(key.bytes(), bytes.as_bytes(), &seal.mac) {
        return Err(refuse);
    }
    if !verified_by_matches(request, &seal.adapter_id) {
        return Err(refuse);
    }
    Ok(VerifiedRequest {
        fingerprint: RequestFingerprint(Digest::of_bytes(bytes.as_bytes())),
        request: request.clone(),
        adapter_id: seal.adapter_id,
        seal_key: None,
    })
}

/// Verified ingress for the waiver lifecycle. It cannot be used to dispatch a promotion
/// or effect, including when the disposable waiver fixture feature is enabled.
///
/// ```compile_fail
/// use writ_broker::ingress::{VerifiedRequest, WaiverTransition};
/// fn dispatch(_: &VerifiedRequest) {}
/// fn cannot_dispatch(waiver: &WaiverTransition) { dispatch(waiver); }
/// ```
pub struct WaiverTransition {
    verified: VerifiedRequest,
    disposable_fixture: bool,
}

impl WaiverTransition {
    /// Restrict an ordinarily verified ingress request to the waiver lifecycle.
    pub fn from_verified(verified: VerifiedRequest) -> Self {
        Self {
            verified,
            disposable_fixture: false,
        }
    }

    /// Scope management intent to the exact trusted ingress request.
    pub fn fingerprint(&self) -> &RequestFingerprint {
        self.verified.fingerprint()
    }

    pub(crate) fn for_waiver(&self) -> (&VerifiedRequest, bool) {
        (&self.verified, self.disposable_fixture)
    }
}

/// Resolve the exact immutable synthetic transition used by the opt-in waiver fixture.
#[cfg(feature = "disposable-waiver-fixture-ingress")]
pub fn verify_disposable_waiver_transition(request: &Value) -> Result<WaiverTransition, Refusal> {
    let refused = Refusal::IngressUnauthenticated;
    let expected = json::parse(include_bytes!(
        "../../m6/management/fixtures/waiver-transition.json"
    ))
    .map_err(|_| refused.clone())?;
    let adapter_id = "m6-synthetic";
    if request != &expected || !verified_by_matches(request, adapter_id) {
        return Err(refused);
    }
    Ok(WaiverTransition {
        verified: VerifiedRequest {
            fingerprint: RequestFingerprint::of(request).ok_or(refused)?,
            request: request.clone(),
            adapter_id: adapter_id.to_owned(),
            seal_key: None,
        },
        disposable_fixture: true,
    })
}

/// Verify the `seal/2` seal presented with `request` (`None` when absent):
/// the named ingress key must be active in `keys` at trusted time `now` and
/// bound to the sealing adapter in `bindings`. A `seal/1` seal, an unknown,
/// revoked, expired or not-yet-valid key, an unavailable key source and every
/// other failure are [`Refusal::IngressUnauthenticated`].
pub fn verify_ingress_keyed(
    request: &Value,
    seal: Option<&Value>,
    keys: &dyn KeySource,
    bindings: &IngressBindings,
    now: Timestamp,
) -> Result<VerifiedRequest, Refusal> {
    let refuse = Refusal::IngressUnauthenticated;
    let seal = seal.and_then(Seal::from_value).ok_or(refuse.clone())?;
    let key_id = seal
        .key_id
        .as_deref()
        .and_then(KeyId::parse)
        .filter(|k| k.purpose() == KeyPurpose::Ingress)
        .ok_or(refuse.clone())?;
    if !bindings.allows(&seal.adapter_id, &key_id) {
        return Err(refuse);
    }
    let material = match keys.key(KeyPurpose::Ingress, &key_id, now) {
        Ok(KeyStatus::Active(m)) => m,
        _ => return Err(refuse),
    };
    let key = material.ingress().ok_or(refuse.clone())?;
    let signed = keyed_bytes(request, &seal.adapter_id, key_id.as_str()).ok_or(refuse.clone())?;
    if !mac::verify_hex(key.bytes(), signed.as_bytes(), &seal.mac) {
        return Err(refuse);
    }
    if !verified_by_matches(request, &seal.adapter_id) {
        return Err(refuse);
    }
    let fingerprint = RequestFingerprint::of(request).ok_or(refuse)?;
    Ok(VerifiedRequest {
        fingerprint,
        request: request.clone(),
        adapter_id: seal.adapter_id,
        seal_key: Some((key_id, material.digest().clone())),
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

    use crate::keys::{KeyPurpose, Material, MaterialDigest, MemoryKeySource};

    fn bindings() -> IngressBindings {
        let mut b = IngressBindings::new();
        assert!(b.bind(FIXTURE_ADAPTER, kid("ingress/fixture-1")));
        assert!(
            !b.bind(FIXTURE_ADAPTER, kid("permit/k1")),
            "not an ingress key"
        );
        b
    }

    fn keyed_seal(req: &Value, adapter: &str, key_id: &str, key: [u8; 32]) -> Value {
        Seal::create_keyed(req, adapter, &kid(key_id), &ingress_key(key))
            .unwrap()
            .to_value()
    }

    fn keyed(
        req: &Value,
        seal: &Value,
        keys: &MemoryKeySource,
        at: Timestamp,
    ) -> Result<VerifiedRequest, Refusal> {
        verify_ingress_keyed(req, Some(seal), keys, &bindings(), at)
    }

    #[test]
    fn keyed_seal_binds_key_id_adapter_and_version() {
        let req = base_request();
        let keys = keyed_source();
        let seal = keyed_seal(&req, FIXTURE_ADAPTER, "ingress/fixture-1", FIXTURE_KEY);
        assert_eq!(seal.as_obj().unwrap().len(), 3);
        let expected = mac::sign_hex(
            &FIXTURE_KEY,
            json::canonical(&json::obj([
                ("adapter_id", json::s(FIXTURE_ADAPTER)),
                ("contract_version", json::s("seal/2")),
                ("key_id", json::s("ingress/fixture-1")),
                ("request", req.clone()),
            ]))
            .unwrap()
            .as_bytes(),
        )
        .unwrap();
        assert_eq!(
            seal.get("mac").and_then(Value::as_str),
            Some(expected.as_str())
        );
        let v = keyed(&req, &seal, &keys, now()).unwrap();
        assert_eq!(v.fingerprint(), &RequestFingerprint::of(&req).unwrap());
        assert_eq!(v.adapter_id(), FIXTURE_ADAPTER);
        let (id, digest) = v.seal_key().unwrap();
        assert_eq!(id.as_str(), "ingress/fixture-1");
        assert_eq!(digest, &MaterialDigest::of(&FIXTURE_KEY));
        assert_eq!(Seal::from_value(&seal).unwrap().to_value(), seal);
        // seal/1 verification yields no key binding.
        let v1 = verify_ingress(
            &req,
            Some(&sealed(&req, FIXTURE_ADAPTER, &FIXTURE_KEY)),
            &fixture_ring(),
        )
        .unwrap();
        assert!(v1.seal_key().is_none());
    }

    #[test]
    fn seal_versions_do_not_cross() {
        let req = base_request();
        let keys = keyed_source();
        let v1 = sealed(&req, FIXTURE_ADAPTER, &FIXTURE_KEY);
        let v2 = keyed_seal(&req, FIXTURE_ADAPTER, "ingress/fixture-1", FIXTURE_KEY);
        assert!(
            refused(keyed(&req, &v1, &keys, now())),
            "seal/1 on the keyed path"
        );
        assert!(
            refused(verify_ingress(&req, Some(&v2), &fixture_ring())),
            "seal/2 on the unkeyed path"
        );
        // Dropping key_id from a seal/2 does not give a valid seal/1.
        let stripped = v2.without("key_id");
        assert!(refused(verify_ingress(
            &req,
            Some(&stripped),
            &fixture_ring()
        )));
    }

    #[test]
    fn keyed_seal_refusals() {
        let req = base_request();
        let keys = keyed_source();
        let good = keyed_seal(&req, FIXTURE_ADAPTER, "ingress/fixture-1", FIXTURE_KEY);
        let set = |k: &str, v: Value| {
            let mut w = good.clone();
            if let Value::Obj(m) = &mut w {
                m.insert(k.into(), v);
            }
            w
        };
        // Altered key id (even to a known key of another purpose), adapter or tag.
        for seal in [
            set("key_id", json::s("ingress/fixture-2")),
            set("key_id", json::s("permit/k1")),
            set("key_id", json::s("not an id")),
            set("key_id", Value::Null),
            set("adapter_id", json::s(OTHER_ADAPTER)),
            set("mac", json::s(&"0".repeat(64))),
            set("extra", json::s("x")),
        ] {
            assert!(
                refused(keyed(&req, &seal, &keys, now())),
                "{}",
                seal.to_json_text()
            );
        }
        // Altered request.
        let altered = with(&req, &["tenant_id"], json::s("other-tenant"));
        assert!(refused(keyed(&altered, &good, &keys, now())));
        // A key known to the source but not bound to the adapter.
        let mut more = keyed_source();
        more.insert(
            kid("ingress/unbound"),
            Material::new(KeyPurpose::Ingress, OTHER_KEY.to_vec()).unwrap(),
            november(),
        );
        let unbound = keyed_seal(&req, FIXTURE_ADAPTER, "ingress/unbound", OTHER_KEY);
        assert!(refused(keyed(&req, &unbound, &more, now())));
        // Key outside its window, revoked, unknown, or the source unavailable.
        assert!(refused(keyed(&req, &good, &keys, november().not_after())));
        assert!(refused(keyed(
            &req,
            &good,
            &keys,
            november().not_before().plus_seconds(-1)
        )));
        let mut revoked = keyed_source();
        revoked.revoke(&kid("ingress/fixture-1"));
        assert!(refused(keyed(&req, &good, &revoked, now())));
        assert!(refused(keyed(
            &req,
            &good,
            &MemoryKeySource::new(60),
            now()
        )));
        let mut down = keyed_source();
        down.set_unavailable(true);
        assert!(refused(keyed(&req, &good, &down, now())));
        // verified_by must still name the sealing adapter.
        let other = without(&req, &["principal", "verified_by"]);
        let seal = keyed_seal(&other, FIXTURE_ADAPTER, "ingress/fixture-1", FIXTURE_KEY);
        assert!(refused(keyed(&other, &seal, &keys, now())));
        assert!(refused(verify_ingress_keyed(
            &req,
            None,
            &keys,
            &bindings(),
            now()
        )));
    }
}
