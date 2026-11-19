//! Public test identities and verification keys for the opt-in fixture.

use std::sync::{Arc, RwLock, RwLockReadGuard};

use ed25519_dalek::{Signature, VerifyingKey};
use writ_broker::clock::{ClockError, SystemWall, TrustedClock, WallSource};
use writ_kernel::{ids::is_id, time::Timestamp};
use writ_policy_publication_lab::{
    Approval, CurrentTrust, Error as PublicationError, Intent, Purpose, Scope as PublicationScope,
    TrustAuthority, VerificationKey, VerificationSource,
};

use crate::{Authenticator, Grant, PinnedPrincipal, Principal, Realm, Role, Scope};

pub const TENANT: &str = "m6-lab";
pub const PROJECT: &str = "offline-proof";
pub const AUTHORIZATION_KEY: &str = "management-fixture-authorization";
pub const ACCEPTANCE_KEY: &str = "management-fixture-acceptance";
pub const CEREMONY_DOMAIN: &str = "writ.management.fixture-ceremony/1\0";
const AUTHORIZATION_PUBLIC_KEY: [u8; 32] = [
    0xaf, 0x06, 0xa3, 0xe3, 0x29, 0x17, 0x14, 0xe4, 0xf3, 0x56, 0xc1, 0x9c, 0x9b, 0x15, 0xcd, 0x19,
    0x51, 0xec, 0x6e, 0x66, 0x62, 0xaa, 0x77, 0xbe, 0x07, 0x54, 0x7f, 0x28, 0x93, 0x83, 0x34, 0x1d,
];
const ACCEPTANCE_PUBLIC_KEY: [u8; 32] = [
    0x2d, 0xf0, 0x41, 0x25, 0xf0, 0x01, 0x5a, 0xfb, 0x47, 0xce, 0x85, 0x3a, 0xef, 0x87, 0x72, 0x09,
    0x4f, 0xf9, 0x49, 0x8c, 0x14, 0xcb, 0x1b, 0x9e, 0x12, 0x97, 0x3c, 0x29, 0x27, 0xda, 0x0f, 0xa6,
];
const CEREMONY_PUBLIC_KEY: [u8; 32] = [
    0xa7, 0xf6, 0xdf, 0xaf, 0x8f, 0x38, 0xb8, 0x9b, 0xa8, 0xce, 0x64, 0x9b, 0x59, 0x4f, 0x91, 0xe4,
    0xd0, 0x1f, 0xdc, 0x57, 0xf9, 0xc9, 0x49, 0x3d, 0xf4, 0x3b, 0x5e, 0x50, 0xa9, 0x98, 0x73, 0x67,
];

pub fn authority() -> impl Authenticator {
    FixtureAuthority
}

struct FixtureAuthority;

struct FixturePrincipal(Principal);

impl PinnedPrincipal for FixturePrincipal {
    fn principal(&self) -> &Principal {
        &self.0
    }
}

impl Authenticator for FixtureAuthority {
    fn pin(&self, credential: &[u8]) -> Option<Box<dyn PinnedPrincipal + '_>> {
        Some(Box::new(FixturePrincipal(self.authenticate(credential)?)))
    }

    fn authenticate(&self, credential: &[u8]) -> Option<Principal> {
        use Role::{PolicyAuthor, Publisher, Viewer, WaiverOperator};
        let (realm, subject, roles): (Realm, &str, &[Role]) = match credential {
            b"fixture-waiver-requester" => (
                Realm::Management,
                "fixture-waiver-requester",
                &[WaiverOperator],
            ),
            b"fixture-waiver-operator" => (
                Realm::Management,
                "fixture-waiver-operator",
                &[WaiverOperator],
            ),
            b"fixture-author" => (Realm::Management, "fixture-author", &[PolicyAuthor]),
            b"fixture-viewer" => (Realm::Management, "fixture-viewer", &[Viewer]),
            b"fixture-publisher" => (Realm::Management, "fixture-publisher", &[Publisher]),
            b"fixture-author-publisher" => (
                Realm::Management,
                "fixture-author-publisher",
                &[PolicyAuthor, Publisher],
            ),
            b"fixture-service" => (Realm::ManagementService, "fixture-service", &[Publisher]),
            b"fixture-agent" => (Realm::Agent, "fixture-agent", &[PolicyAuthor, Publisher]),
            b"fixture-adapter" => (
                Realm::Adapter,
                "fixture-adapter",
                &[PolicyAuthor, Publisher],
            ),
            _ => return None,
        };
        Some(Principal {
            realm,
            subject: subject.into(),
            grants: roles
                .iter()
                .map(|role| Grant {
                    scope: Scope {
                        tenant: TENANT.into(),
                        project: PROJECT.into(),
                    },
                    role: *role,
                })
                .collect(),
        })
    }
}

pub fn publication_scope() -> Result<PublicationScope, PublicationError> {
    PublicationScope::new(TENANT, PROJECT)
}

pub fn policy_window() -> Result<(Timestamp, Timestamp), PublicationError> {
    let start = Timestamp::parse_strict("2020-01-01T00:00:00Z").ok_or(PublicationError::Time)?;
    let end = Timestamp::parse_strict("2099-01-01T00:00:00Z").ok_or(PublicationError::Time)?;
    Ok((start, end))
}

pub fn fixture_time() -> Result<Timestamp, ClockError> {
    let seconds = i64::try_from(SystemWall.wall_nanos()?.div_euclid(1_000_000_000))
        .map_err(|_| ClockError::Overflow)?;
    let epoch = Timestamp::parse_strict("1970-01-01T00:00:00Z").ok_or(ClockError::Source)?;
    let now = epoch.plus_seconds(seconds);
    Timestamp::parse_strict(&now.to_string()).ok_or(ClockError::Overflow)
}

#[derive(Clone, Default)]
pub struct FixtureTrust(Arc<RwLock<TrustState>>);

#[derive(Default)]
struct TrustState {
    fixed_now: Option<Timestamp>,
    authorization_revoked: bool,
    acceptance_revoked: bool,
    publisher_revoked: bool,
}

pub struct FixtureView<'a>(RwLockReadGuard<'a, TrustState>);

impl FixtureTrust {
    pub fn set_time(&self, now: Timestamp) -> Result<(), PublicationError> {
        self.0
            .write()
            .map_err(|_| PublicationError::Trust)?
            .fixed_now = Some(now);
        Ok(())
    }

    pub fn revoke_policy_key(&self, purpose: Purpose) -> Result<(), PublicationError> {
        let mut trust = self.0.write().map_err(|_| PublicationError::Trust)?;
        match purpose {
            Purpose::Authorization => trust.authorization_revoked = true,
            Purpose::Acceptance => trust.acceptance_revoked = true,
        }
        Ok(())
    }

    pub fn revoke_publisher(&self) -> Result<(), PublicationError> {
        self.0
            .write()
            .map_err(|_| PublicationError::Trust)?
            .publisher_revoked = true;
        Ok(())
    }
}

impl TrustAuthority for FixtureTrust {
    fn current(&self) -> Result<impl CurrentTrust + '_, PublicationError> {
        self.0
            .read()
            .map(FixtureView)
            .map_err(|_| PublicationError::Trust)
    }
}

impl TrustedClock for FixtureView<'_> {
    fn now(&self) -> Result<Timestamp, ClockError> {
        self.0.fixed_now.map_or_else(fixture_time, Ok)
    }
}

impl VerificationSource for FixtureView<'_> {
    fn resolve(&self, key_id: &str) -> Result<VerificationKey, PublicationError> {
        let (purpose, public_key, revoked) = match key_id {
            AUTHORIZATION_KEY => (
                Purpose::Authorization,
                AUTHORIZATION_PUBLIC_KEY,
                self.0.authorization_revoked,
            ),
            ACCEPTANCE_KEY => (
                Purpose::Acceptance,
                ACCEPTANCE_PUBLIC_KEY,
                self.0.acceptance_revoked,
            ),
            _ => return Err(PublicationError::Trust),
        };
        let (not_before, expires_at) = policy_window()?;
        Ok(VerificationKey {
            key_id: key_id.into(),
            purpose,
            scope: publication_scope()?,
            public_key,
            not_before,
            expires_at,
            revoked,
        })
    }
}

impl CurrentTrust for FixtureView<'_> {
    fn has_active_dependents(
        &self,
        scope: &PublicationScope,
        _publication_version: i64,
        _publication_digest: &str,
    ) -> Result<bool, PublicationError> {
        // This isolated fixture has no pack or integration dependency registry.
        if scope != &publication_scope()? {
            return Err(PublicationError::DependencyUnavailable);
        }
        Ok(false)
    }

    fn waiver_publisher_subject(
        &self,
        publisher: &str,
    ) -> Result<writ_broker::human::SubjectId, PublicationError> {
        if matches!(publisher, "fixture-publisher" | "fixture-author-publisher") {
            Ok(writ_broker::human::SubjectId::new(
                "fixture-publisher-realm",
                publisher,
            ))
        } else {
            Err(PublicationError::Authority)
        }
    }

    fn approve(
        &self,
        intent: &Intent,
        presentation: &[u8],
        _now: Timestamp,
    ) -> Result<Approval, PublicationError> {
        let refused = PublicationError::Authority;
        if self.0.publisher_revoked || intent.scope() != &publication_scope()? {
            return Err(refused);
        }
        let presentation = std::str::from_utf8(presentation).map_err(|_| refused)?;
        let approval = verify_fixture_ceremony(
            presentation,
            CEREMONY_DOMAIN,
            &CEREMONY_PUBLIC_KEY,
            intent.fingerprint()?.as_str(),
        )
        .ok_or(refused)?;
        if !matches!(
            approval.human,
            "fixture-publisher" | "fixture-author-publisher"
        ) {
            return Err(refused);
        }
        Ok(Approval {
            human: approval.human.into(),
            ceremony: approval.ceremony.into(),
            issued_at: approval.issued_at,
            expires_at: approval.issued_at.plus_seconds(60),
        })
    }
}

pub(crate) struct VerifiedFixtureCeremony<'a> {
    pub human: &'a str,
    pub ceremony: &'a str,
    pub issued_at: Timestamp,
}

pub(crate) fn verify_fixture_ceremony<'a>(
    presentation: &'a str,
    domain: &str,
    public_key: &[u8; 32],
    expected_digest: &str,
) -> Option<VerifiedFixtureCeremony<'a>> {
    let (claim, signature) = presentation.rsplit_once('|')?;
    let mut fields = claim.split('|');
    let human = fields.next().filter(|label| is_id(label))?;
    let ceremony = fields.next().filter(|label| is_id(label))?;
    let issued_at = fields.next().and_then(Timestamp::parse_strict)?;
    let digest = fields.next()?;
    if fields.next().is_some() || digest != expected_digest {
        return None;
    }
    let key = VerifyingKey::from_bytes(public_key).ok()?;
    let signature = Signature::from_bytes(&decode_signature(signature)?);
    let signed = format!("{domain}{claim}");
    key.verify_strict(signed.as_bytes(), &signature).ok()?;
    Some(VerifiedFixtureCeremony {
        human,
        ceremony,
        issued_at,
    })
}

pub(crate) fn decode_signature(text: &str) -> Option<[u8; 64]> {
    if text.len() != 128 {
        return None;
    }
    fn nibble(byte: u8) -> Option<u8> {
        match byte {
            b'0'..=b'9' => Some(byte - b'0'),
            b'a'..=b'f' => Some(byte - b'a' + 10),
            _ => None,
        }
    }
    let mut output = [0; 64];
    let (pairs, _) = text.as_bytes().as_chunks::<2>();
    for (result, &[high, low]) in output.iter_mut().zip(pairs) {
        *result = (nibble(high)? << 4) | nibble(low)?;
    }
    Some(output)
}
