use writ_kernel::{
    ids::is_id,
    json::{self, Digest, Map, Value, MAX_SAFE_INTEGER},
    time::Timestamp,
};

use crate::Error;

pub(crate) fn exact<'a>(value: &'a Value, names: &[&str]) -> Result<&'a Map, Error> {
    let map = value.as_obj().ok_or(Error::Format)?;
    if map.len() == names.len() && names.iter().all(|name| map.contains_key(*name)) {
        Ok(map)
    } else {
        Err(Error::Format)
    }
}

pub(crate) fn text<'a>(map: &'a Map, name: &str) -> Result<&'a str, Error> {
    map.get(name).and_then(Value::as_str).ok_or(Error::Format)
}

pub(crate) fn identifier(value: &str) -> Result<&str, Error> {
    is_id(value).then_some(value).ok_or(Error::Format)
}

pub(crate) fn number(value: i64, min: i64) -> Result<i64, Error> {
    (min..=MAX_SAFE_INTEGER)
        .contains(&value)
        .then_some(value)
        .ok_or(Error::Format)
}

pub(crate) fn digest(value: &Value) -> Result<Digest, Error> {
    json::digest(value).map_err(|_| Error::Format)
}

pub(crate) fn canonical(value: &Value) -> Result<String, Error> {
    json::canonical(value).map_err(|_| Error::Format)
}

pub(crate) fn digest_text(value: &str) -> Result<&str, Error> {
    value
        .strip_prefix("sha256:")
        .ok_or(Error::Format)
        .and_then(hex::<32>)?;
    Ok(value)
}

pub(crate) fn hex<const N: usize>(value: &str) -> Result<[u8; N], Error> {
    let mut out = [0; N];
    let bytes = value.as_bytes();
    if bytes.len() != 2 * N {
        return Err(Error::Format);
    }
    let nibble = |byte| match byte {
        b'0'..=b'9' => Ok(byte - b'0'),
        b'a'..=b'f' => Ok(byte - b'a' + 10),
        _ => Err(Error::Format),
    };
    for (pair, target) in bytes.as_chunks::<2>().0.iter().zip(&mut out) {
        *target = nibble(pair[0])? * 16 + nibble(pair[1])?;
    }
    Ok(out)
}

pub(crate) fn time(value: &str) -> Result<Timestamp, Error> {
    Timestamp::parse_strict(value)
        .filter(|parsed| parsed.to_string() == value)
        .ok_or(Error::Format)
}

/// Exact tenant and project for this lab publication.
#[derive(Clone, Debug, PartialEq, Eq, PartialOrd, Ord)]
pub struct Scope {
    tenant: String,
    project: String,
}

impl Scope {
    /// Construct a checked scope.
    pub fn new(tenant: &str, project: &str) -> Result<Self, Error> {
        Ok(Self {
            tenant: identifier(tenant)?.into(),
            project: identifier(project)?.into(),
        })
    }

    /// Tenant ID.
    pub fn tenant(&self) -> &str {
        &self.tenant
    }

    /// Project ID.
    pub fn project(&self) -> &str {
        &self.project
    }

    /// Closed scope document.
    pub fn to_value(&self) -> Value {
        json::obj([
            ("tenant", json::s(self.tenant())),
            ("project", json::s(self.project())),
        ])
    }

    /// Parse a closed scope document.
    pub fn from_value(value: &Value) -> Result<Self, Error> {
        let map = exact(value, &["tenant", "project"])?;
        Self::new(text(map, "tenant")?, text(map, "project")?)
    }
}

/// A payload and its separate detached envelope.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct SignedPolicy {
    /// Exact policy document.
    pub payload: Value,
    /// Closed detached signature document.
    pub envelope: Value,
}

impl SignedPolicy {
    fn from_value(value: &Value) -> Result<Self, Error> {
        let map = exact(value, &["payload", "envelope"])?;
        Ok(Self {
            payload: map["payload"].clone(),
            envelope: map["envelope"].clone(),
        })
    }

    fn to_value(&self) -> Value {
        json::obj([
            ("payload", self.payload.clone()),
            ("envelope", self.envelope.clone()),
        ])
    }
}

/// An immutable candidate combining two detached signatures and proof inputs.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Publication {
    /// Authorization bundle and signature.
    pub authorization: SignedPolicy,
    /// Acceptance bundle and signature.
    pub acceptance: SignedPolicy,
    /// Slice 1 compiler input and expected proof digest.
    pub prerequisites: Value,
}

impl Publication {
    /// Parse the closed publication container.
    pub fn from_value(value: &Value) -> Result<Self, Error> {
        let map = exact(
            value,
            &["version", "authorization", "acceptance", "prerequisites"],
        )?;
        if text(map, "version")? != "writ-policy-publication/1" {
            return Err(Error::Format);
        }
        Ok(Self {
            authorization: SignedPolicy::from_value(&map["authorization"])?,
            acceptance: SignedPolicy::from_value(&map["acceptance"])?,
            prerequisites: map["prerequisites"].clone(),
        })
    }

    /// Encode the publication container.
    pub fn to_value(&self) -> Value {
        json::obj([
            ("version", json::s("writ-policy-publication/1")),
            ("authorization", self.authorization.to_value()),
            ("acceptance", self.acceptance.to_value()),
            ("prerequisites", self.prerequisites.clone()),
        ])
    }

    /// Digest of the complete stored publication, including both envelopes.
    pub fn fingerprint(&self) -> Result<Digest, Error> {
        digest(&self.to_value())
    }

    /// Digest of the exact payload pair, independent of re-signing or reordering proof inputs.
    pub fn pair_digest(&self) -> Result<Digest, Error> {
        digest(&self.content_digests()?.without("prerequisites"))
    }

    fn content_digests(&self) -> Result<Value, Error> {
        Ok(json::obj([
            (
                "authorization",
                json::s(digest(&self.authorization.payload)?.as_str()),
            ),
            (
                "acceptance",
                json::s(digest(&self.acceptance.payload)?.as_str()),
            ),
            (
                "prerequisites",
                json::s(digest(&self.prerequisites)?.as_str()),
            ),
        ]))
    }
}

/// Operation approved by a human publisher.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Operation {
    /// Append a new publication and select it.
    Publish,
    /// Explicitly select an older immutable publication.
    Rollback,
    /// Irreversibly retire an inactive publication.
    Retire,
}

impl Operation {
    /// Canonical operation tag.
    pub fn as_str(self) -> &'static str {
        match self {
            Self::Publish => "publish",
            Self::Rollback => "rollback",
            Self::Retire => "retire",
        }
    }

    pub(crate) fn parse(value: &str) -> Result<Self, Error> {
        match value {
            "publish" => Ok(Self::Publish),
            "rollback" => Ok(Self::Rollback),
            "retire" => Ok(Self::Retire),
            _ => Err(Error::Format),
        }
    }
}

/// Exact human publisher approval request.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Intent {
    scope: Scope,
    operation: Operation,
    expected_revision: i64,
    publication_version: i64,
    publication: Digest,
    content: Value,
    management: Option<ManagedIntent>,
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub(crate) struct ManagedIntent {
    pub(crate) digest: String,
    pub(crate) publisher: String,
}

impl Intent {
    /// Bind this command to an immutable publication and every component.
    pub fn new(
        scope: Scope,
        operation: Operation,
        expected_revision: i64,
        publication_version: i64,
        publication: &Publication,
    ) -> Result<Self, Error> {
        Ok(Self {
            scope,
            operation,
            expected_revision: number(expected_revision, 0)?,
            publication_version: number(publication_version, 1)?,
            publication: publication.fingerprint()?,
            content: publication.content_digests()?,
            management: None,
        })
    }

    /// Bind an exact management context and the expected identity of its human publisher.
    pub fn with_management(self, fingerprint: Digest, publisher: &str) -> Result<Self, Error> {
        self.bind_management(fingerprint.as_str(), publisher)
    }

    pub(crate) fn bind_management(mut self, digest: &str, publisher: &str) -> Result<Self, Error> {
        if self.operation == Operation::Retire {
            return Err(Error::Format);
        }
        self.management = Some(ManagedIntent {
            digest: digest_text(digest)?.to_owned(),
            publisher: identifier(publisher)?.to_owned(),
        });
        Ok(self)
    }

    /// Digest of the exact management context, if this operation came through management.
    pub fn management_digest(&self) -> Option<&str> {
        self.management().map(|context| context.digest.as_str())
    }

    pub(crate) fn management(&self) -> Option<&ManagedIntent> {
        self.management.as_ref()
    }

    pub(crate) fn matches_approval(&self, approval: &Approval) -> bool {
        self.management()
            .is_none_or(|management| management.publisher == approval.human)
    }

    /// Exact scope.
    pub fn scope(&self) -> &Scope {
        &self.scope
    }

    /// Approved operation.
    pub fn operation(&self) -> Operation {
        self.operation
    }

    /// Required prior activation revision.
    pub fn expected_revision(&self) -> i64 {
        self.expected_revision
    }

    /// Immutable publication version this intent names.
    pub fn publication_version(&self) -> i64 {
        self.publication_version
    }

    /// Exact publisher authority request to authenticate.
    pub fn to_value(&self) -> Value {
        let mut fields = Map::from([
            ("purpose".into(), json::s("writ-human-policy-publisher/1")),
            ("scope".into(), self.scope.to_value()),
            ("operation".into(), json::s(self.operation.as_str())),
            (
                "expected_revision".into(),
                Value::Int(self.expected_revision),
            ),
            (
                "publication_version".into(),
                Value::Int(self.publication_version),
            ),
            (
                "publication_digest".into(),
                json::s(self.publication.as_str()),
            ),
            ("content_digests".into(), self.content.clone()),
        ]);
        if let Some(context) = self.management() {
            fields.insert(
                "management".into(),
                json::obj([
                    ("digest", json::s(&context.digest)),
                    ("publisher", json::s(&context.publisher)),
                ]),
            );
        }
        Value::Obj(fields)
    }

    /// Digest for the approval challenge, never a substitute for approval.
    pub fn fingerprint(&self) -> Result<Digest, Error> {
        digest(&self.to_value())
    }
}

/// Provenance returned only by the configured human authority after checking the exact intent.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Approval {
    /// Authenticated, scoped human publisher identifier.
    pub human: String,
    /// Globally unique, single-use ceremony identifier.
    pub ceremony: String,
    /// Trusted issuance time.
    pub issued_at: Timestamp,
    /// Exclusive trusted expiry.
    pub expires_at: Timestamp,
}

impl Approval {
    pub(crate) fn check(&self, now: Timestamp) -> Result<(), Error> {
        identifier(&self.human).map_err(|_| Error::Authority)?;
        identifier(&self.ceremony).map_err(|_| Error::Authority)?;
        if self.issued_at <= now
            && now < self.expires_at
            && self.expires_at <= self.issued_at.plus_seconds(120)
        {
            Ok(())
        } else {
            Err(Error::Authority)
        }
    }
}
