//! Broker permits (SR-07, D-012, D-025).
//!
//! A permit authorizes the controlled effect adapter to apply exactly one
//! request. It binds the D-012 fingerprint of the complete request, the digest
//! of the bundle the use-time decision was made under, and an expiry of
//! `min(now + 30 s, request deadline)`; its MAC is HMAC-SHA256 under the
//! broker's permit key over the D-006 canonical bytes of the body
//! `{"expires_at", "permit_id", "policy_bundle_sha", "request_fingerprint"}`
//! (all strings; the M1 reference encoding).
//!
//! Verification order (m1/fixtures/broker/BROKER.md adapter order, extended by
//! owner decision A of 2026-11-12 under D-018 A3), after strict parsing
//! ([`Permit::from_value`], `PERMIT_INVALID`): fingerprint differs from the
//! presented request: `PERMIT_REQUEST_MISMATCH`; MAC invalid: `PERMIT_INVALID`;
//! trusted time unavailable: `TIME_UNAVAILABLE`; trusted time at or after
//! expiry: `PERMIT_EXPIRED`; bound bundle digest is
//! not the currently active authorization bundle: `PERMIT_STALE`.
//!
//! Absent, rejected or unverifiable active policy is never treated as a
//! matching bundle: verification refuses with `PERMIT_STALE` (not
//! `POLICY_UNAVAILABLE`), so every permit verification failure stays in the
//! permit family, and the refusal carries no digest. Externally it is
//! `PROMOTION_REFUSED` (the kernel's generic mapping), like every other permit
//! refusal.
//!
//! Verification has no side effects: it reads the permit, key, clock and the
//! shared view of the active policy and changes none of them. The replay check
//! (`PERMIT_REPLAYED`), consumption and the effect are ledger and adapter
//! operations that arrive in M3-04: the caller holds the [`SharedPolicy`] and
//! begins the adapter transaction before verifying (gate first, then the
//! transaction; D-029 order), so that `LEDGER_UNAVAILABLE` takes precedence
//! over an invalid permit (D-025 B), and records the permit id as consumed
//! before applying the effect, all under the same shared hold.
//!
//! **Contract `permit/2` (M5 J-3, D-045 M5-D6).** A keyed permit names the
//! permit key that made it. Wire form: the five members above plus
//! `"contract_version": "permit/2"` and `"key_id"` (a [`KeyId`] of purpose
//! `permit`), seven members, all strings. The MAC is HMAC-SHA256 under the
//! named key over the D-006 canonical bytes of the six non-`mac` members, so
//! the contract version and the key id are authenticated. The unversioned
//! form above is `permit/1`. [`Permit::verify`] (one explicit [`PermitKey`])
//! accepts `permit/1` only; [`Permit::verify_keyed`] (a [`KeySource`] and the
//! ledger revocation lookup) accepts `permit/2` only, so a service that runs
//! with the file key source refuses every unversioned permit
//! (`PERMIT_INVALID`). Keyed verification order: fingerprint differs:
//! `PERMIT_REQUEST_MISMATCH`; not `permit/2`, or a key id that is not a
//! permit key id: `PERMIT_INVALID`; trusted time unavailable:
//! `TIME_UNAVAILABLE` (read once; the key's validity window needs it, so here
//! it precedes the MAC check); key unknown, revoked (in the source, or in the
//! ledger by id or by material), expired or not yet valid, or the key source
//! or the revocation lookup unavailable: `PERMIT_INVALID`; MAC invalid:
//! `PERMIT_INVALID`; then expiry and staleness as above.

use crate::authorize::Authorized;
use crate::clock::TrustedClock;
use crate::ingress::RequestFingerprint;
use crate::keys::{
    issuing_key_at_use, key_at_use, KeyId, KeyPurpose, KeyRevocations, KeySource, KeyStatus,
    PermitKey,
};
use crate::mac;
use crate::policy::SharedPolicy;
use crate::refusal::Refusal;
use std::fmt;
use writ_kernel::json::{self, Value};
use writ_kernel::time::Timestamp;

/// Permit lifetime before the request deadline bound.
pub const PERMIT_TTL_SECONDS: i64 = 30;

/// Maximum length of a permit id.
pub const MAX_PERMIT_ID_LEN: usize = 128;

/// `contract_version` of a keyed permit (J-3).
pub const PERMIT_CONTRACT_V2: &str = "permit/2";

const MEMBERS: [&str; 5] = [
    "expires_at",
    "mac",
    "permit_id",
    "policy_bundle_sha",
    "request_fingerprint",
];

/// A permit id assigned by the ledger (M3-04): 1 to 128 characters.
#[derive(Clone, Debug, PartialEq, Eq, Hash)]
pub struct PermitId(String);

impl PermitId {
    /// `Some` for 1 to [`MAX_PERMIT_ID_LEN`] characters.
    pub fn new(id: &str) -> Option<PermitId> {
        (!id.is_empty() && id.chars().count() <= MAX_PERMIT_ID_LEN)
            .then(|| PermitId(id.to_string()))
    }

    /// The id text.
    pub fn as_str(&self) -> &str {
        &self.0
    }
}

/// Permit issue failed; nothing may be dispatched (fail closed).
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum PermitIssueError {
    /// The body could not be encoded or signed.
    Signing,
    /// Trusted time was unavailable (`TIME_UNAVAILABLE`).
    TimeUnavailable,
    /// No permit key qualifies for issuing, or the key source or the
    /// revocation lookup is unavailable (J-3: fail closed).
    KeysUnavailable,
}

impl fmt::Display for PermitIssueError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(match self {
            PermitIssueError::Signing => "permit signing failed",
            PermitIssueError::TimeUnavailable => "trusted time unavailable",
            PermitIssueError::KeysUnavailable => "no permit key available for issuing",
        })
    }
}

impl std::error::Error for PermitIssueError {}

/// A permit as issued or as presented to the effect adapter. Its members are
/// kept exactly as presented; nothing in it is trusted before [`Permit::verify`].
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Permit {
    permit_id: String,
    request_fingerprint: String,
    policy_bundle_sha: String,
    expires_at: String,
    /// `Some` for `permit/2` (the key id as presented), `None` for `permit/1`.
    key_id: Option<String>,
    mac: String,
}

/// The bindings of a permit that verified.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct VerifiedPermit {
    /// Permit id (to be recorded as consumed before the effect).
    pub permit_id: String,
    /// Fingerprint of the request the permit authorizes (equal to the presented one).
    pub request_fingerprint: String,
    /// Digest of the bundle the issuing decision was made under.
    pub policy_bundle_sha: String,
    /// Expiry (later than the trusted time of verification).
    pub expires_at: Timestamp,
    /// The permit key id (`permit/2`); `None` for `permit/1`.
    pub key_id: Option<String>,
}

fn signed_bytes(
    permit_id: &str,
    request_fingerprint: &str,
    policy_bundle_sha: &str,
    expires_at: &str,
    key_id: Option<&str>,
) -> Option<String> {
    let mut body = json::obj([
        ("permit_id", json::s(permit_id)),
        ("request_fingerprint", json::s(request_fingerprint)),
        ("policy_bundle_sha", json::s(policy_bundle_sha)),
        ("expires_at", json::s(expires_at)),
    ]);
    if let (Some(key_id), Value::Obj(m)) = (key_id, &mut body) {
        m.insert("contract_version".into(), json::s(PERMIT_CONTRACT_V2));
        m.insert("key_id".into(), json::s(key_id));
    }
    json::canonical(&body).ok()
}

impl Permit {
    /// Issue permit `permit_id` for `authorized`, expiring at
    /// `min(now + PERMIT_TTL_SECONDS, deadline)` with `now` read once from `clock`.
    pub fn issue(
        permit_id: &PermitId,
        authorized: &Authorized<'_>,
        clock: &dyn TrustedClock,
        key: &PermitKey,
    ) -> Result<Permit, PermitIssueError> {
        Permit::issue_bounded(permit_id, authorized, clock, key, None)
    }

    /// [`Permit::issue`] with an additional bound: the permit expires at
    /// `min(now + PERMIT_TTL_SECONDS, deadline, not_after)`. A grant-backed
    /// permit passes the grant's expiry (D-035 item 1), so a permit never
    /// outlives the approval that authorized it.
    pub fn issue_bounded(
        permit_id: &PermitId,
        authorized: &Authorized<'_>,
        clock: &dyn TrustedClock,
        key: &PermitKey,
        not_after: Option<Timestamp>,
    ) -> Result<Permit, PermitIssueError> {
        let now = clock.now().map_err(|_| PermitIssueError::TimeUnavailable)?;
        Permit::sign(permit_id, authorized, now, key, None, not_after)
    }

    /// Issue a keyed permit (`permit/2`): as [`Permit::issue_bounded`], with
    /// the key selected by [`KeySource::issuing_key`] for a
    /// [`PERMIT_TTL_SECONDS`] lifetime at the trusted time read once from
    /// `clock` and checked against the ledger revocation lookup
    /// `revocations`. No qualifying key: [`PermitIssueError::KeysUnavailable`].
    pub fn issue_keyed(
        permit_id: &PermitId,
        authorized: &Authorized<'_>,
        clock: &dyn TrustedClock,
        keys: &dyn KeySource,
        revocations: &dyn KeyRevocations,
        not_after: Option<Timestamp>,
    ) -> Result<Permit, PermitIssueError> {
        let now = clock.now().map_err(|_| PermitIssueError::TimeUnavailable)?;
        let lifetime = std::time::Duration::from_secs(PERMIT_TTL_SECONDS.unsigned_abs());
        let (key_id, material) =
            issuing_key_at_use(keys, revocations, KeyPurpose::Permit, now, lifetime)
                .map_err(|_| PermitIssueError::KeysUnavailable)?;
        let key = material.permit().ok_or(PermitIssueError::KeysUnavailable)?;
        Permit::sign(permit_id, authorized, now, key, Some(&key_id), not_after)
    }

    fn sign(
        permit_id: &PermitId,
        authorized: &Authorized<'_>,
        now: Timestamp,
        key: &PermitKey,
        key_id: Option<&KeyId>,
        not_after: Option<Timestamp>,
    ) -> Result<Permit, PermitIssueError> {
        let ttl = now.plus_seconds(PERMIT_TTL_SECONDS);
        let expires = [authorized.request().deadline_at, not_after]
            .into_iter()
            .flatten()
            .fold(ttl, Timestamp::min);
        let fingerprint = authorized.verified().fingerprint().as_str();
        let bundle_sha = authorized.policy_bundle_sha();
        let expires_at = expires.to_string();
        let key_id = key_id.map(|k| k.as_str().to_string());
        let body = signed_bytes(
            permit_id.as_str(),
            fingerprint,
            bundle_sha,
            &expires_at,
            key_id.as_deref(),
        )
        .ok_or(PermitIssueError::Signing)?;
        let mac = mac::sign_hex(key.bytes(), body.as_bytes()).ok_or(PermitIssueError::Signing)?;
        Ok(Permit {
            permit_id: permit_id.as_str().to_string(),
            request_fingerprint: fingerprint.to_string(),
            policy_bundle_sha: bundle_sha.to_string(),
            expires_at,
            key_id,
            mac,
        })
    }

    /// Verify the permit for `request` (the request presented to the adapter)
    /// under `key` at the time read once from `clock` and against the active
    /// policy seen through `active`, in the order of the module documentation.
    /// The caller keeps `active` held until the effect is authorized.
    pub fn verify(
        &self,
        request: &Value,
        key: &PermitKey,
        clock: &dyn TrustedClock,
        active: &SharedPolicy<'_>,
    ) -> Result<VerifiedPermit, Refusal> {
        self.check_fingerprint(request)?;
        if self.key_id.is_some() {
            return Err(Refusal::PermitInvalid);
        }
        self.check_mac(key)?;
        let now = clock.now().map_err(|e| e.refusal())?;
        self.check_time_and_policy(now, active)
    }

    /// Verify a `permit/2` permit for `request` with the key it names, looked
    /// up in `keys` at the trusted time read once from `clock` and checked
    /// against `revocations` (the ledger transaction opened by the
    /// verification callback, J-3), in the keyed order of the module
    /// documentation. A `permit/1` permit is [`Refusal::PermitInvalid`].
    pub fn verify_keyed(
        &self,
        request: &Value,
        keys: &dyn KeySource,
        revocations: &dyn KeyRevocations,
        clock: &dyn TrustedClock,
        active: &SharedPolicy<'_>,
    ) -> Result<VerifiedPermit, Refusal> {
        self.check_fingerprint(request)?;
        let key_id = self
            .key_id
            .as_deref()
            .and_then(KeyId::parse)
            .filter(|k| k.purpose() == KeyPurpose::Permit)
            .ok_or(Refusal::PermitInvalid)?;
        let now = clock.now().map_err(|e| e.refusal())?;
        let material = match key_at_use(keys, revocations, KeyPurpose::Permit, &key_id, now) {
            Ok(KeyStatus::Active(m)) => m,
            _ => return Err(Refusal::PermitInvalid),
        };
        self.check_mac(material.permit().ok_or(Refusal::PermitInvalid)?)?;
        self.check_time_and_policy(now, active)
    }

    fn check_fingerprint(&self, request: &Value) -> Result<(), Refusal> {
        let presented = RequestFingerprint::of(request);
        if presented.as_ref().map(RequestFingerprint::as_str)
            != Some(self.request_fingerprint.as_str())
        {
            return Err(Refusal::PermitRequestMismatch);
        }
        Ok(())
    }

    fn check_mac(&self, key: &PermitKey) -> Result<(), Refusal> {
        let body = signed_bytes(
            &self.permit_id,
            &self.request_fingerprint,
            &self.policy_bundle_sha,
            &self.expires_at,
            self.key_id.as_deref(),
        )
        .ok_or(Refusal::PermitInvalid)?;
        if !mac::verify_hex(key.bytes(), body.as_bytes(), &self.mac) {
            return Err(Refusal::PermitInvalid);
        }
        Ok(())
    }

    fn check_time_and_policy(
        &self,
        now: Timestamp,
        active: &SharedPolicy<'_>,
    ) -> Result<VerifiedPermit, Refusal> {
        // Signed by this broker, so the expiry is one it wrote; fail closed regardless.
        let expires_at =
            Timestamp::parse_rfc3339(&self.expires_at).ok_or(Refusal::PermitInvalid)?;
        if now >= expires_at {
            return Err(Refusal::PermitExpired);
        }
        if active.active_bundle_sha() != Some(self.policy_bundle_sha.as_str()) {
            return Err(Refusal::PermitStale);
        }
        Ok(VerifiedPermit {
            permit_id: self.permit_id.clone(),
            request_fingerprint: self.request_fingerprint.clone(),
            policy_bundle_sha: self.policy_bundle_sha.clone(),
            expires_at,
            key_id: self.key_id.clone(),
        })
    }

    /// The wire form: the four body members plus `mac`, all strings; for
    /// `permit/2` also `contract_version` and `key_id`.
    pub fn to_value(&self) -> Value {
        let mut v = json::obj([
            ("permit_id", json::s(&self.permit_id)),
            ("request_fingerprint", json::s(&self.request_fingerprint)),
            ("policy_bundle_sha", json::s(&self.policy_bundle_sha)),
            ("expires_at", json::s(&self.expires_at)),
            ("mac", json::s(&self.mac)),
        ]);
        if let (Some(key_id), Value::Obj(m)) = (&self.key_id, &mut v) {
            m.insert("contract_version".into(), json::s(PERMIT_CONTRACT_V2));
            m.insert("key_id".into(), json::s(key_id));
        }
        v
    }

    /// A presented permit from its wire form: exactly the five `permit/1`
    /// members, or exactly those plus `"contract_version": "permit/2"` and
    /// `key_id` (seven), all strings. Any other shape cannot be verified and
    /// is [`Refusal::PermitInvalid`].
    pub fn from_value(value: &Value) -> Result<Permit, Refusal> {
        let members = value.as_obj().ok_or(Refusal::PermitInvalid)?;
        let text = |k: &str| {
            members
                .get(k)
                .and_then(Value::as_str)
                .map(str::to_string)
                .ok_or(Refusal::PermitInvalid)
        };
        let key_id = match members.len() {
            n if n == MEMBERS.len() => None,
            n if n == MEMBERS.len() + 2 => {
                if text("contract_version")? != PERMIT_CONTRACT_V2 {
                    return Err(Refusal::PermitInvalid);
                }
                Some(text("key_id")?)
            }
            _ => return Err(Refusal::PermitInvalid),
        };
        Ok(Permit {
            permit_id: text("permit_id")?,
            request_fingerprint: text("request_fingerprint")?,
            policy_bundle_sha: text("policy_bundle_sha")?,
            expires_at: text("expires_at")?,
            key_id,
            mac: text("mac")?,
        })
    }

    /// The permit key id as presented (`permit/2`); `None` for `permit/1`.
    pub fn key_id(&self) -> Option<&str> {
        self.key_id.as_deref()
    }

    /// The permit id as presented.
    pub fn permit_id(&self) -> &str {
        &self.permit_id
    }

    /// The bound request fingerprint as presented.
    pub fn request_fingerprint(&self) -> &str {
        &self.request_fingerprint
    }

    /// The bound bundle digest as presented.
    pub fn policy_bundle_sha(&self) -> &str {
        &self.policy_bundle_sha
    }

    /// The expiry as presented.
    pub fn expires_at(&self) -> &str {
        &self.expires_at
    }
}

#[cfg(test)]
#[allow(clippy::unwrap_used, clippy::expect_used, clippy::panic)]
mod tests {
    use super::*;
    use crate::authorize::authorize;
    use crate::clock::FixedClock;
    use crate::policy::{ActivePolicy, PolicyState};
    use crate::test_support::*;
    use writ_kernel::policy::LoadedBundle;

    fn authorized<'g>(shared: &'g SharedPolicy<'g>, req: &Value) -> Authorized<'g> {
        authorize(
            &verify(req),
            &shared.trusted_inputs(revision(5)),
            &FixedClock::new(now()),
        )
        .unwrap()
    }

    fn issue(req: &Value) -> Permit {
        let active = reference_active();
        let shared = active.shared();
        Permit::issue(
            &PermitId::new("permit-1").unwrap(),
            &authorized(&shared, req),
            &FixedClock::new(now()),
            &permit_key(PERMIT_KEY),
        )
        .unwrap()
    }

    fn tampered(permit: &Permit, member: &str, value: &str) -> Permit {
        let mut v = permit.to_value();
        if let Value::Obj(m) = &mut v {
            m.insert(member.into(), json::s(value));
        }
        Permit::from_value(&v).unwrap()
    }

    fn flip_mac(permit: &Permit) -> Permit {
        let first = if permit.mac.starts_with('0') {
            "1"
        } else {
            "0"
        };
        tampered(permit, "mac", &format!("{first}{}", &permit.mac[1..]))
    }

    /// The reference bundle active (the bundle permits are issued under).
    fn reference_active() -> ActivePolicy {
        ActivePolicy::new(PolicyState::loaded(reference_bundle()))
    }

    /// A loaded bundle other than the reference bundle.
    fn replacement_bundle() -> LoadedBundle {
        load(&with(
            &reference_bundle_value(),
            &["version"],
            json::s("0.9.0"),
        ))
    }

    /// The replacement bundle active.
    fn replacement_active() -> ActivePolicy {
        ActivePolicy::new(PolicyState::loaded(replacement_bundle()))
    }

    fn check_under(
        active: &ActivePolicy,
        permit: &Permit,
        req: &Value,
        at: Timestamp,
    ) -> Result<VerifiedPermit, Refusal> {
        permit.verify(
            req,
            &permit_key(PERMIT_KEY),
            &FixedClock::new(at),
            &active.shared(),
        )
    }

    fn check(permit: &Permit, req: &Value, at: Timestamp) -> Result<VerifiedPermit, Refusal> {
        check_under(&reference_active(), permit, req, at)
    }

    fn mismatched(req: &Value) -> Value {
        with(
            req,
            &["action", "resource", "canonical_resource_id"],
            json::s("workspace:fixture-tenant/fixture-project/src/other.py"),
        )
    }

    #[test]
    fn permit_ids_are_bounded() {
        assert!(PermitId::new("").is_none());
        assert!(PermitId::new(&"p".repeat(129)).is_none());
        assert_eq!(PermitId::new("permit-1").unwrap().as_str(), "permit-1");
    }

    #[test]
    fn issued_permit_binds_fingerprint_bundle_and_expiry() {
        let req = base_request();
        let active = reference_active();
        let shared = active.shared();
        let a = authorized(&shared, &req);
        let p = issue(&req);
        assert_eq!(p.permit_id(), "permit-1");
        assert_eq!(
            p.request_fingerprint(),
            json::digest(&req).unwrap().as_str()
        );
        assert_eq!(p.policy_bundle_sha(), a.policy_bundle_sha());
        drop(a);
        drop(shared);
        assert_eq!(p.policy_bundle_sha(), reference_bundle().sha.as_str());
        assert_eq!(p.expires_at(), "2026-11-05T12:00:30Z");
        let body = json::canonical(&p.to_value().without("mac")).unwrap();
        assert_eq!(
            p.mac,
            mac::sign_hex(&PERMIT_KEY, body.as_bytes()).unwrap(),
            "MAC over the canonical four-member body"
        );
        let v = check(&p, &req, now()).unwrap();
        assert_eq!(
            v,
            VerifiedPermit {
                permit_id: "permit-1".into(),
                request_fingerprint: p.request_fingerprint().into(),
                policy_bundle_sha: p.policy_bundle_sha().into(),
                expires_at: Timestamp::parse_strict("2026-11-05T12:00:30Z").unwrap(),
                key_id: None,
            }
        );
        assert_eq!(Permit::from_value(&p.to_value()).unwrap(), p);
    }

    #[test]
    fn expiry_is_bounded_by_the_deadline() {
        let req = with(
            &base_request(),
            &["deadline_at"],
            json::s("2026-11-05T12:00:10Z"),
        );
        let p = issue(&req);
        assert_eq!(p.expires_at(), "2026-11-05T12:00:10Z");
        assert!(check(&p, &req, now().plus_seconds(9)).is_ok());
        assert_eq!(
            check(&p, &req, now().plus_seconds(10)),
            Err(Refusal::PermitExpired)
        );
        let late = with(
            &base_request(),
            &["deadline_at"],
            json::s("2026-11-05T13:00:00Z"),
        );
        assert_eq!(issue(&late).expires_at(), "2026-11-05T12:00:30Z");
    }

    #[test]
    fn expired_permit_is_refused_at_the_exact_boundary() {
        let req = base_request();
        let p = issue(&req);
        assert!(check(&p, &req, now().plus_seconds(29)).is_ok());
        assert_eq!(
            check(&p, &req, now().plus_seconds(30)),
            Err(Refusal::PermitExpired)
        );
        assert_eq!(
            check(&p, &req, now().plus_seconds(31)),
            Err(Refusal::PermitExpired)
        );
    }

    #[test]
    fn forged_mac_is_invalid() {
        let req = base_request();
        let p = issue(&req);
        assert_eq!(
            check(&flip_mac(&p), &req, now()),
            Err(Refusal::PermitInvalid)
        );
        let other_key = p.verify(
            &req,
            &permit_key([0x45; 32]),
            &FixedClock::new(now()),
            &reference_active().shared(),
        );
        assert_eq!(other_key, Err(Refusal::PermitInvalid));
    }

    #[test]
    fn extended_expiry_without_resigning_is_invalid() {
        let req = base_request();
        let p = issue(&req);
        let extended = tampered(&p, "expires_at", "2026-11-05T13:00:00Z");
        assert_eq!(check(&extended, &req, now()), Err(Refusal::PermitInvalid));
        // Even after the original expiry, the MAC failure is what is reported.
        assert_eq!(
            check(&extended, &req, now().plus_seconds(31)),
            Err(Refusal::PermitInvalid)
        );
    }

    #[test]
    fn rebinding_bundle_or_id_without_resigning_is_invalid() {
        let req = base_request();
        let p = issue(&req);
        let other_bundle = tampered(
            &p,
            "policy_bundle_sha",
            &format!("sha256:{}", "0".repeat(64)),
        );
        assert_eq!(
            check(&other_bundle, &req, now()),
            Err(Refusal::PermitInvalid)
        );
        let other_id = tampered(&p, "permit_id", "permit-2");
        assert_eq!(check(&other_id, &req, now()), Err(Refusal::PermitInvalid));
    }

    #[test]
    fn d025_precedence_mismatch_then_mac_then_expiry() {
        let req = base_request();
        let other = with(
            &req,
            &["action", "resource", "canonical_resource_id"],
            json::s("workspace:fixture-tenant/fixture-project/src/other.py"),
        );
        let p = issue(&req);
        let late = now().plus_seconds(31);
        // Mismatch beats a bad MAC and expiry.
        assert_eq!(
            check(&flip_mac(&p), &other, late),
            Err(Refusal::PermitRequestMismatch)
        );
        // A forged fingerprint pointing at the other request: the fingerprint now
        // matches, so the MAC decides.
        let rebound = tampered(
            &p,
            "request_fingerprint",
            json::digest(&other).unwrap().as_str(),
        );
        assert_eq!(check(&rebound, &other, now()), Err(Refusal::PermitInvalid));
        // A bad MAC beats expiry.
        assert_eq!(
            check(&flip_mac(&p), &req, late),
            Err(Refusal::PermitInvalid)
        );
        // Valid MAC, expired.
        assert_eq!(check(&p, &req, late), Err(Refusal::PermitExpired));
    }

    #[test]
    fn permit_for_a_replaced_policy_is_stale() {
        let req = base_request();
        let p = issue(&req);
        assert!(check(&p, &req, now()).is_ok());
        // The policy is replaced between issue and presentation.
        let active = reference_active();
        active.activate(PolicyState::loaded(replacement_bundle()));
        assert_eq!(
            check_under(&active, &p, &req, now()),
            Err(Refusal::PermitStale)
        );
        assert_eq!(
            check_under(&replacement_active(), &p, &req, now()),
            Err(Refusal::PermitStale)
        );
        // Reactivating the issuing bundle makes the same permit current again.
        active.activate(PolicyState::loaded(reference_bundle()));
        assert!(check_under(&active, &p, &req, now()).is_ok());
    }

    #[test]
    fn stale_check_comes_after_mismatch_mac_and_expiry() {
        let req = base_request();
        let p = issue(&req);
        let stale = replacement_active();
        let late = now().plus_seconds(31);
        assert_eq!(
            check_under(&stale, &p, &mismatched(&req), now()),
            Err(Refusal::PermitRequestMismatch),
            "mismatch plus stale"
        );
        assert_eq!(
            check_under(&stale, &flip_mac(&p), &req, now()),
            Err(Refusal::PermitInvalid),
            "invalid MAC plus stale"
        );
        assert_eq!(
            check_under(&stale, &p, &req, late),
            Err(Refusal::PermitExpired),
            "expired plus stale"
        );
        // All four at once: the fingerprint decides.
        assert_eq!(
            check_under(&stale, &flip_mac(&p), &mismatched(&req), late),
            Err(Refusal::PermitRequestMismatch)
        );
        // A permit re-bound to the active digest without re-signing is invalid,
        // not accepted as current.
        let rebound = tampered(
            &p,
            "policy_bundle_sha",
            &format!("sha256:{}", "0".repeat(64)),
        );
        let active_rebound = tampered(
            &p,
            "policy_bundle_sha",
            stale.shared().active_bundle_sha().unwrap(),
        );
        assert_eq!(
            check_under(&stale, &rebound, &req, now()),
            Err(Refusal::PermitInvalid)
        );
        assert_eq!(
            check_under(&stale, &active_rebound, &req, now()),
            Err(Refusal::PermitInvalid)
        );
    }

    #[test]
    fn no_active_policy_fails_closed_as_stale() {
        let req = base_request();
        let p = issue(&req);
        for state in [PolicyState::Absent, PolicyState::Rejected] {
            let active = ActivePolicy::new(state.clone());
            let refusal = check_under(&active, &p, &req, now()).unwrap_err();
            assert_eq!(refusal, Refusal::PermitStale, "{state:?}");
            assert_eq!(refusal.external_code(), "PROMOTION_REFUSED");
            assert!(
                !refusal.to_string().contains("sha256:"),
                "the refusal reveals no digest"
            );
            // Earlier checks still take precedence over the missing policy.
            assert_eq!(
                check_under(&active, &flip_mac(&p), &req, now()),
                Err(Refusal::PermitInvalid)
            );
        }
    }

    #[test]
    fn verification_has_no_side_effects() {
        let req = base_request();
        let p = issue(&req);
        let before = p.clone();
        let active = reference_active();
        let key = permit_key(PERMIT_KEY);
        let clock = FixedClock::new(now());
        let shared = active.shared();
        let first = p.verify(&req, &key, &clock, &shared);
        // Not consumed: verification alone never records the permit (M3-04 does).
        let second = p.verify(&req, &key, &clock, &shared);
        assert!(first.is_ok());
        assert_eq!(first, second);
        assert_eq!(p, before);
        assert_eq!(clock.now(), Ok(now()));
        assert_eq!(
            shared.active_bundle_sha(),
            Some(reference_bundle().sha.as_str())
        );
        // Refusals leave everything unchanged as well.
        let stale = replacement_active();
        let stale_shared = stale.shared();
        let sha_before = stale_shared.active_bundle_sha().map(str::to_string);
        for _ in 0..2 {
            assert_eq!(
                p.verify(&req, &key, &clock, &stale_shared),
                Err(Refusal::PermitStale)
            );
        }
        assert_eq!(p, before);
        assert_eq!(
            stale_shared.active_bundle_sha().map(str::to_string),
            sha_before
        );
        drop(shared);
        assert!(check_under(&active, &p, &req, now()).is_ok());
    }

    #[test]
    fn malformed_permits_are_invalid() {
        let p = issue(&base_request()).to_value();
        assert_eq!(
            Permit::from_value(&Value::Null),
            Err(Refusal::PermitInvalid)
        );
        assert_eq!(
            Permit::from_value(&p.without("mac")),
            Err(Refusal::PermitInvalid)
        );
        let mut extra = p.clone();
        let mut wrong_type = p.clone();
        if let (Value::Obj(e), Value::Obj(w)) = (&mut extra, &mut wrong_type) {
            e.insert("scope".into(), json::s("all"));
            w.insert("expires_at".into(), Value::Int(1));
        }
        assert_eq!(Permit::from_value(&extra), Err(Refusal::PermitInvalid));
        assert_eq!(Permit::from_value(&wrong_type), Err(Refusal::PermitInvalid));
    }

    use crate::keys::{
        KeyPurpose, KeyRegistry, KeyRevocation, Material, MaterialDigest, MemoryKeyRegistry,
        MemoryKeySource,
    };

    fn issue_keyed_with(
        req: &Value,
        keys: &MemoryKeySource,
        reg: &MemoryKeyRegistry,
    ) -> Result<Permit, PermitIssueError> {
        let active = reference_active();
        let shared = active.shared();
        Permit::issue_keyed(
            &PermitId::new("permit-1").unwrap(),
            &authorized(&shared, req),
            &FixedClock::new(now()),
            keys,
            reg,
            None,
        )
    }

    fn check_keyed(
        permit: &Permit,
        req: &Value,
        keys: &MemoryKeySource,
        reg: &MemoryKeyRegistry,
        at: Timestamp,
    ) -> Result<VerifiedPermit, Refusal> {
        permit.verify_keyed(
            req,
            keys,
            reg,
            &FixedClock::new(at),
            &reference_active().shared(),
        )
    }

    fn revoke(reg: &mut MemoryKeyRegistry, id: &str, material: &[u8]) {
        reg.revoke(KeyRevocation {
            key_id: kid(id),
            material: MaterialDigest::of(material),
            purpose: KeyPurpose::Permit,
            revoked_at: now(),
            actor: "uid:0".into(),
            reason: "test".into(),
        })
        .unwrap();
    }

    #[test]
    fn keyed_permit_authenticates_its_version_and_key_id() {
        let req = base_request();
        let (keys, reg) = (keyed_source(), MemoryKeyRegistry::new());
        let p = issue_keyed_with(&req, &keys, &reg).unwrap();
        assert_eq!(p.key_id(), Some("permit/k1"));
        let wire = p.to_value();
        assert_eq!(wire.as_obj().unwrap().len(), 7);
        assert_eq!(
            wire.get("contract_version").and_then(Value::as_str),
            Some(PERMIT_CONTRACT_V2)
        );
        let body = json::canonical(&wire.without("mac")).unwrap();
        assert!(body.contains("\"key_id\":\"permit/k1\""));
        assert_eq!(
            p.mac,
            mac::sign_hex(&PERMIT_KEY, body.as_bytes()).unwrap(),
            "MAC over the canonical six-member body"
        );
        assert_eq!(Permit::from_value(&wire).unwrap(), p);
        let v = check_keyed(&p, &req, &keys, &reg, now()).unwrap();
        assert_eq!(v.key_id.as_deref(), Some("permit/k1"));
        assert_eq!(v.expires_at, now().plus_seconds(30));
    }

    #[test]
    fn versions_do_not_cross() {
        let req = base_request();
        let (keys, reg) = (keyed_source(), MemoryKeyRegistry::new());
        let v1 = issue(&req);
        let v2 = issue_keyed_with(&req, &keys, &reg).unwrap();
        // The keyed path refuses unversioned permits; the unkeyed path refuses permit/2.
        assert_eq!(
            check_keyed(&v1, &req, &keys, &reg, now()),
            Err(Refusal::PermitInvalid)
        );
        assert_eq!(check(&v2, &req, now()), Err(Refusal::PermitInvalid));
        // Downgrade: strip the version and key id from a permit/2. The MAC covered
        // them, so the result is not a valid permit/1 under the same key.
        let downgraded =
            Permit::from_value(&v2.to_value().without("contract_version").without("key_id"))
                .unwrap();
        assert_eq!(check(&downgraded, &req, now()), Err(Refusal::PermitInvalid));
        // Mismatch still decides first on the keyed path.
        assert_eq!(
            check_keyed(&v1, &mismatched(&req), &keys, &reg, now()),
            Err(Refusal::PermitRequestMismatch)
        );
    }

    #[test]
    fn malformed_keyed_permits_are_invalid() {
        let req = base_request();
        let (keys, reg) = (keyed_source(), MemoryKeyRegistry::new());
        let wire = issue_keyed_with(&req, &keys, &reg).unwrap().to_value();
        let set = |k: &str, v: Value| {
            let mut w = wire.clone();
            if let Value::Obj(m) = &mut w {
                m.insert(k.into(), v);
            }
            w
        };
        for bad in [
            set("contract_version", json::s("permit/3")),
            set("contract_version", json::s("permit/1")),
            set("key_id", Value::Int(1)),
            wire.without("contract_version"),
            wire.without("key_id"),
            set("extra", json::s("x")),
        ] {
            assert_eq!(
                Permit::from_value(&bad),
                Err(Refusal::PermitInvalid),
                "{}",
                bad.to_json_text()
            );
        }
        // Parsed, but the key id is not a permit key id or is altered: invalid.
        for key_id in ["ingress/fixture-1", "permit k1", "permit/k2"] {
            let p = Permit::from_value(&set("key_id", json::s(key_id))).unwrap();
            assert_eq!(
                check_keyed(&p, &req, &keys, &reg, now()),
                Err(Refusal::PermitInvalid),
                "{key_id}"
            );
        }
    }

    #[test]
    fn keyed_verification_refuses_unusable_keys() {
        let req = base_request();
        let (keys, mut reg) = (keyed_source(), MemoryKeyRegistry::new());
        let p = issue_keyed_with(&req, &keys, &reg).unwrap();
        // Key validity is judged at verification time.
        let mut late = MemoryKeySource::new(60);
        late.insert(
            kid("permit/k1"),
            Material::new(KeyPurpose::Permit, PERMIT_KEY.to_vec()).unwrap(),
            crate::keys::KeyValidity::new(now().plus_seconds(-3600), now().plus_seconds(10))
                .unwrap(),
        );
        assert!(check_keyed(&p, &req, &late, &reg, now().plus_seconds(9)).is_ok());
        assert_eq!(
            check_keyed(&p, &req, &late, &reg, now().plus_seconds(10)),
            Err(Refusal::PermitInvalid),
            "key expired before the permit"
        );
        let mut revoked_in_source = keyed_source();
        revoked_in_source.revoke(&kid("permit/k1"));
        assert_eq!(
            check_keyed(&p, &req, &revoked_in_source, &reg, now()),
            Err(Refusal::PermitInvalid)
        );
        let mut unavailable = keyed_source();
        unavailable.set_unavailable(true);
        assert_eq!(
            check_keyed(&p, &req, &unavailable, &reg, now()),
            Err(Refusal::PermitInvalid)
        );
        assert_eq!(
            check_keyed(&p, &req, &MemoryKeySource::new(60), &reg, now()),
            Err(Refusal::PermitInvalid),
            "unknown key"
        );
        // Ledger revocation of the material under another id refuses the permit.
        revoke(&mut reg, "permit/elsewhere", &PERMIT_KEY);
        assert_eq!(
            check_keyed(&p, &req, &keys, &reg, now()),
            Err(Refusal::PermitInvalid)
        );
        let mut down = MemoryKeyRegistry::new();
        down.set_unavailable(true);
        assert_eq!(
            check_keyed(&p, &req, &keys, &down, now()),
            Err(Refusal::PermitInvalid)
        );
        // Expiry and staleness still apply after the key checks.
        let fresh = MemoryKeyRegistry::new();
        assert_eq!(
            check_keyed(&p, &req, &keys, &fresh, now().plus_seconds(30)),
            Err(Refusal::PermitExpired)
        );
        assert_eq!(
            p.verify_keyed(
                &req,
                &keys,
                &fresh,
                &FixedClock::new(now()),
                &replacement_active().shared()
            ),
            Err(Refusal::PermitStale)
        );
    }

    #[test]
    fn keyed_issue_fails_closed_without_a_qualifying_key() {
        let req = base_request();
        let mut reg = MemoryKeyRegistry::new();
        assert_eq!(
            issue_keyed_with(&req, &MemoryKeySource::new(60), &reg).err(),
            Some(PermitIssueError::KeysUnavailable)
        );
        // A key that ends within lifetime + margin is not selected.
        let mut short = MemoryKeySource::new(60);
        short.insert(
            kid("permit/short"),
            Material::new(KeyPurpose::Permit, PERMIT_KEY.to_vec()).unwrap(),
            crate::keys::KeyValidity::new(now().plus_seconds(-10), now().plus_seconds(89)).unwrap(),
        );
        assert_eq!(
            issue_keyed_with(&req, &short, &reg).err(),
            Some(PermitIssueError::KeysUnavailable)
        );
        revoke(&mut reg, "permit/k1", &PERMIT_KEY);
        assert_eq!(
            issue_keyed_with(&req, &keyed_source(), &reg).err(),
            Some(PermitIssueError::KeysUnavailable),
            "selected key revoked in the ledger"
        );
    }
}
