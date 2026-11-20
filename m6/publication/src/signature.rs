//! Closed, domain-separated detached signatures over policy payloads.

use ed25519_dalek::{Signature, VerifyingKey};
use writ_kernel::{
    json::{self, Value},
    time::Timestamp,
};

use crate::{wire, Error, Scope, SignedPolicy};

const ENVELOPE_VERSION: &str = "writ-detached-policy-signature/1";
pub(crate) const ACCEPTANCE_V2: &str = "writ-acceptance-policy/2";
const ALGORITHM: &str = "Ed25519";
pub(crate) const ACCEPTANCE_V3: &str = "writ-acceptance-policy/3";
const SIGNING_DOMAIN: &[u8] = b"writ.policy.detached-signature/1\0";

/// The policy class to which a signature and verification key are bound.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Purpose {
    /// Authorization policy bundle.
    Authorization,
    /// Acceptance policy bundle.
    Acceptance,
}

impl Purpose {
    /// The exact signed purpose tag.
    pub fn as_str(self) -> &'static str {
        match self {
            Self::Authorization => "authorization",
            Self::Acceptance => "acceptance",
        }
    }

    /// The original payload format for this purpose.
    pub fn payload_format(self) -> &'static str {
        match self {
            Self::Authorization => "writ-authorization-bundle/2.0",
            Self::Acceptance => "writ-acceptance-policy/1",
        }
    }

    fn parse(text: &str) -> Result<Self, Error> {
        match text {
            "authorization" => Ok(Self::Authorization),
            "acceptance" => Ok(Self::Acceptance),
            _ => Err(Error::Format),
        }
    }

    fn format(self, payload: &Value) -> Result<&'static str, Error> {
        match self {
            Self::Authorization
                if payload.get("contract_version").and_then(Value::as_str) == Some("2.0")
                    && payload.get("publisher_signature").is_none() =>
            {
                Ok(self.payload_format())
            }
            Self::Acceptance => match payload.get("version").and_then(Value::as_str) {
                Some(ACCEPTANCE_V2) => Ok(ACCEPTANCE_V2),
                Some(ACCEPTANCE_V3) => Ok(ACCEPTANCE_V3),
                Some(version) if version == self.payload_format() => Ok(self.payload_format()),
                _ => Err(Error::Format),
            },
            _ => Err(Error::Format),
        }
    }
}

/// Exact protected fields of a detached signature.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Protected {
    purpose: Purpose,
    payload_format: &'static str,
    scope: Scope,
    payload_digest: String,
    key_id: String,
    not_before: Timestamp,
    expires_at: Timestamp,
}

impl Protected {
    /// Construct the protected header for an exact canonical payload.
    pub fn new(
        purpose: Purpose,
        scope: Scope,
        payload: &Value,
        key_id: &str,
        not_before: Timestamp,
        expires_at: Timestamp,
    ) -> Result<Self, Error> {
        Self::checked(
            purpose,
            purpose.format(payload)?,
            scope,
            wire::digest(payload)?.as_str(),
            key_id,
            not_before,
            expires_at,
        )
    }

    fn checked(
        purpose: Purpose,
        payload_format: &'static str,
        scope: Scope,
        payload_digest: &str,
        key_id: &str,
        not_before: Timestamp,
        expires_at: Timestamp,
    ) -> Result<Self, Error> {
        wire::time(&not_before.to_string())?;
        wire::time(&expires_at.to_string())?;
        if not_before >= expires_at {
            return Err(Error::Format);
        }
        Ok(Self {
            purpose,
            payload_format,
            scope,
            payload_digest: wire::digest_text(payload_digest)?.to_owned(),
            key_id: wire::identifier(key_id)?.to_owned(),
            not_before,
            expires_at,
        })
    }

    /// Parse a closed header and reject unsupported compatibility metadata.
    pub fn from_value(value: &Value) -> Result<Self, Error> {
        let map = wire::exact(
            value,
            &[
                "version",
                "algorithm",
                "purpose",
                "scope",
                "payload_digest",
                "publisher_key_id",
                "payload_format",
                "not_before",
                "expires_at",
            ],
        )?;
        let purpose = Purpose::parse(wire::text(map, "purpose")?)?;
        if wire::text(map, "version")? != ENVELOPE_VERSION
            || wire::text(map, "algorithm")? != ALGORITHM
        {
            return Err(Error::Format);
        }
        let payload_format = match wire::text(map, "payload_format")? {
            ACCEPTANCE_V2 if purpose == Purpose::Acceptance => ACCEPTANCE_V2,
            ACCEPTANCE_V3 if purpose == Purpose::Acceptance => ACCEPTANCE_V3,
            format if format == purpose.payload_format() => purpose.payload_format(),
            _ => return Err(Error::Format),
        };
        Self::checked(
            purpose,
            payload_format,
            Scope::from_value(&map["scope"])?,
            wire::text(map, "payload_digest")?,
            wire::text(map, "publisher_key_id")?,
            wire::time(wire::text(map, "not_before")?)?,
            wire::time(wire::text(map, "expires_at")?)?,
        )
    }

    /// Encode exactly the protected members specified by version 1.
    pub fn to_value(&self) -> Value {
        json::obj([
            ("version", json::s(ENVELOPE_VERSION)),
            ("algorithm", json::s(ALGORITHM)),
            ("purpose", json::s(self.purpose.as_str())),
            ("scope", self.scope.to_value()),
            ("payload_digest", json::s(&self.payload_digest)),
            ("publisher_key_id", json::s(&self.key_id)),
            ("payload_format", json::s(self.payload_format)),
            ("not_before", json::s(&self.not_before.to_string())),
            ("expires_at", json::s(&self.expires_at.to_string())),
        ])
    }

    /// Domain prefix including its NUL, followed by the D-006 header bytes.
    pub fn signing_bytes(&self) -> Result<Vec<u8>, Error> {
        let header = wire::canonical(&self.to_value())?;
        let mut bytes = Vec::with_capacity(SIGNING_DOMAIN.len() + header.len());
        bytes.extend_from_slice(SIGNING_DOMAIN);
        bytes.extend_from_slice(header.as_bytes());
        Ok(bytes)
    }

    /// Exact tenant and project named by the envelope.
    pub fn scope(&self) -> &Scope {
        &self.scope
    }

    /// The single purpose named by the envelope.
    pub fn purpose(&self) -> Purpose {
        self.purpose
    }

    /// Identifier to resolve in the trusted key source.
    pub fn key_id(&self) -> &str {
        &self.key_id
    }

    fn check_payload(&self, payload: &Value) -> Result<(), Error> {
        if wire::digest(payload)?.as_str() != self.payload_digest {
            return Err(Error::Signature);
        }
        (self.purpose.format(payload)? == self.payload_format)
            .then_some(())
            .ok_or(Error::Format)
    }
}

/// Public verification material from the configured source.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct VerificationKey {
    /// Configured key identifier.
    pub key_id: String,
    /// The only allowed purpose.
    pub purpose: Purpose,
    /// The only allowed scope.
    pub scope: Scope,
    /// Compressed Ed25519 verification key.
    pub public_key: [u8; 32],
    /// Inclusive beginning of trust.
    pub not_before: Timestamp,
    /// Exclusive end of trust.
    pub expires_at: Timestamp,
    /// Current revocation status.
    pub revoked: bool,
}

/// Explicit configuration boundary for locating current trusted public keys.
pub trait VerificationSource {
    /// Resolve one trusted record by its exact key ID.
    fn resolve(&self, key_id: &str) -> Result<VerificationKey, Error>;
}

/// Separate publisher signing boundary. Consumers only need a verification source.
pub trait Signer {
    /// Sign the exact protected bytes for the supplied key identity, scope and purpose.
    fn sign(&self, protected: &Protected, bytes: &[u8]) -> Result<[u8; 64], Error>;
}

/// Obtain a detached signature from the supplied signer.
pub fn sign(
    payload: Value,
    protected: &Protected,
    signer: &dyn Signer,
) -> Result<SignedPolicy, Error> {
    protected.check_payload(&payload)?;
    let bytes = signer.sign(protected, &protected.signing_bytes()?)?;
    let mut signature = String::with_capacity(2 * bytes.len());
    const HEX: &[u8] = b"0123456789abcdef";
    for byte in bytes {
        signature.push(char::from(HEX[usize::from(byte >> 4)]));
        signature.push(char::from(HEX[usize::from(byte & 15)]));
    }
    Ok(SignedPolicy {
        payload,
        envelope: json::obj([
            ("protected", protected.to_value()),
            ("signature", json::s(&signature)),
        ]),
    })
}

/// Verify purpose, scope, compatibility, payload integrity and current key trust.
pub fn verify(
    document: &SignedPolicy,
    purpose: Purpose,
    scope: &Scope,
    now: Timestamp,
    source: &(impl VerificationSource + ?Sized),
) -> Result<(), Error> {
    verified_until(document, purpose, scope, now, source).map(|_| ())
}

pub(crate) fn verified_until(
    document: &SignedPolicy,
    purpose: Purpose,
    scope: &Scope,
    now: Timestamp,
    source: &(impl VerificationSource + ?Sized),
) -> Result<Timestamp, Error> {
    let envelope = wire::exact(&document.envelope, &["protected", "signature"])?;
    let protected = Protected::from_value(&envelope["protected"])?;
    let signature = Signature::from_bytes(&wire::hex(wire::text(envelope, "signature")?)?);
    if protected.scope() != scope {
        return Err(Error::Scope);
    }
    if protected.purpose() != purpose {
        return Err(Error::Signature);
    }
    protected.check_payload(&document.payload)?;
    let key = source
        .resolve(protected.key_id())
        .map_err(|_| Error::Trust)?;
    if key.revoked
        || key.key_id != protected.key_id()
        || key.purpose != purpose
        || &key.scope != scope
    {
        return Err(Error::Trust);
    }
    if now < protected.not_before
        || now >= protected.expires_at
        || now < key.not_before
        || now >= key.expires_at
    {
        return Err(Error::Time);
    }
    let verifier = VerifyingKey::from_bytes(&key.public_key).map_err(|_| Error::Trust)?;
    let point = verifier.to_edwards();
    if verifier.is_weak()
        || !point.is_torsion_free()
        || point.compress().to_bytes() != key.public_key
    {
        return Err(Error::Trust);
    }
    verifier
        .verify_strict(&protected.signing_bytes()?, &signature)
        .map_err(|_| Error::Signature)?;
    Ok(protected.expires_at.min(key.expires_at))
}
